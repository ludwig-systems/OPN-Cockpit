"""``UnboundHostAdapter`` und ``UnboundController`` fuer Unbound-Host-Overrides.

OPNsense ``Unbound DNS`` ist Core (kein Plugin noetig). Host-Overrides
sind eine Liste von (hostname, domain) -> Ziel-IP-Mappings, die Unbound
intern fuer Recursive-Resolution verwendet.

API-Endpoints (siehe ``_endpoints.py``):

* ``POST /api/unbound/settings/searchHostOverride`` - Liste mit Pagination
* ``GET /api/unbound/settings/getHostOverride/{uuid}`` - Detail
* ``POST /api/unbound/settings/addHostOverride`` - Anlage
* ``POST /api/unbound/settings/setHostOverride/{uuid}`` - Update
* ``POST /api/unbound/settings/delHostOverride/{uuid}`` - Delete
* ``POST /api/unbound/service/reconfigure`` - Aktivierung

Identitaet ist (``host``, ``domain``) - ein stabiler User-Schluessel wie
bei Routen. Cockpit sucht beim Update/Delete die UUID anhand dieser
beiden Felder.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

from opn_cockpit.core.errors import (
    ApiError,
    AuthError,
    EgressDeniedError,
    ReconfigureError,
    UnreachableError,
    ValidationError,
    make_context,
)
from opn_cockpit.core.objects._endpoints import (
    UNBOUND_HOST_ADD,
    UNBOUND_HOST_DEL,
    UNBOUND_HOST_GET,
    UNBOUND_HOST_SEARCH,
    UNBOUND_HOST_SET,
    UNBOUND_RECONFIGURE,
)
from opn_cockpit.core.objects.base import (
    AddOutcome,
    Diff,
    DiffKind,
    RequestContext,
    VerifyOutcome,
)

if TYPE_CHECKING:
    from opn_cockpit.core.http_client import HttpClient


# ---------------------------------------------------------------------------
# Datentypen
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UnboundHostIdentity:
    """Identitaet eines Host-Override-Eintrags.

    OPNsense erlaubt mehrere Eintraege mit gleichem (host, domain) - das
    Cockpit deduppt das nicht, weil ein doppelter Eintrag dort sehr selten
    Absicht ist und ein nachgelagertes Cleanup in der OPNsense-UI besser
    aufgehoben ist.
    """

    host: str
    domain: str


@dataclass(frozen=True, slots=True)
class UnboundHostSpec:
    """Soll-Beschreibung eines Host-Override-Eintrags."""

    host: str
    domain: str
    server: str = ""        # Ziel-IP (IPv4 oder IPv6)
    description: str = ""
    enabled: bool = True

    def to_identity(self) -> UnboundHostIdentity:
        return UnboundHostIdentity(host=self.host, domain=self.domain)


# ---------------------------------------------------------------------------
# Helfer
# ---------------------------------------------------------------------------


def _raise_if_saved_failed(response: Any, path: str, ctx: RequestContext) -> None:
    """Identisches Muster wie in routes/firewall_rules - hier dupliziert
    damit Module unabhaengig voneinander erweiterbar bleiben."""
    try:
        body = response.json()
    except ValueError:
        return
    if not isinstance(body, dict):
        return
    result = body.get("result")
    if not isinstance(result, str) or result.lower() not in {"failed", "error"}:
        return
    validations = body.get("validations")
    detail = ""
    if isinstance(validations, dict) and validations:
        detail = "; ".join(f"{k}: {v}" for k, v in validations.items() if v)
    msg = (
        f"OPNsense lehnte den Schreibvorgang ab "
        f"(result='{result}'{(': ' + detail) if detail else ''})."
    )
    raise ApiError(
        msg,
        context=make_context(
            host=ctx.target.host,
            port=ctx.target.port,
            method="POST",
            path=path,
            error_kind="opnsense_save_failed",
            summary=f"OPNsense lehnte ab: {detail}" if detail else msg,
        ),
    )


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def _row_to_spec(row: dict[str, Any]) -> UnboundHostSpec:
    return UnboundHostSpec(
        host=str(row.get("hostname", row.get("host", ""))).strip(),
        domain=str(row.get("domain", "")).strip(),
        server=str(row.get("server", row.get("rr", ""))).strip(),
        description=str(row.get("description", row.get("descr", ""))).strip(),
        enabled=_as_bool(row.get("enabled", "1")),
    )


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class UnboundHostAdapter:
    """Adapter fuer Unbound-DNS-Host-Overrides."""

    subsystem: ClassVar[str] = "unbound_hosts"

    def identity(self, spec: UnboundHostSpec) -> UnboundHostIdentity:
        return spec.to_identity()

    def to_payload(self, spec: UnboundHostSpec) -> dict[str, Any]:
        return {
            "enabled": "1" if spec.enabled else "0",
            "hostname": spec.host,
            "domain": spec.domain,
            "server": spec.server,
            "description": spec.description,
        }

    def spec_to_dict(self, spec: UnboundHostSpec) -> dict[str, Any]:
        return {
            "host": spec.host,
            "domain": spec.domain,
            "server": spec.server,
            "description": spec.description,
            "enabled": spec.enabled,
        }

    def spec_from_dict(self, raw: dict[str, Any]) -> UnboundHostSpec:
        return UnboundHostSpec(
            host=str(raw.get("host", "")),
            domain=str(raw.get("domain", "")),
            server=str(raw.get("server", "")),
            description=str(raw.get("description", "")),
            enabled=bool(raw.get("enabled", True)),
        )

    # ----- exists / verify -----

    def exists(
        self,
        client: HttpClient,
        ctx: RequestContext,
        ident: UnboundHostIdentity,
    ) -> UnboundHostSpec | None:
        uuid = self._search_uuid(client, ctx, ident)
        if uuid is None:
            return None
        return self._get_by_uuid(client, ctx, uuid)

    def verify(
        self,
        client: HttpClient,
        ctx: RequestContext,
        ident: UnboundHostIdentity,
    ) -> VerifyOutcome:
        current = self.exists(client, ctx, ident)
        if current is None:
            return VerifyOutcome(found=False)
        return VerifyOutcome(
            found=True,
            detail=f"{current.host}.{current.domain} -> {current.server}",
        )

    # ----- add / update / delete -----

    def add(
        self,
        client: HttpClient,
        ctx: RequestContext,
        spec: UnboundHostSpec,
    ) -> AddOutcome:
        payload = {"host": self.to_payload(spec)}
        response = client.call(
            ctx.target, ctx.key, ctx.secret,
            "POST", UNBOUND_HOST_ADD,
            json=payload,
        )
        _raise_if_saved_failed(response, UNBOUND_HOST_ADD, ctx)
        try:
            body = response.json()
        except ValueError:
            body = {}
        uuid = None
        if isinstance(body, dict):
            candidate = body.get("uuid")
            if isinstance(candidate, str) and candidate:
                uuid = candidate
        return AddOutcome(uuid=uuid, raw_status=response.status_code)

    def update(
        self,
        client: HttpClient,
        ctx: RequestContext,
        spec: UnboundHostSpec,
    ) -> AddOutcome:
        existing_uuid = self._search_uuid(client, ctx, spec.to_identity())
        if existing_uuid is None:
            raise ValidationError(
                f"Host-Override {spec.host}.{spec.domain} existiert nicht - "
                "Update nicht moeglich.",
                context=make_context(
                    host=ctx.target.host,
                    port=ctx.target.port,
                    method="POST",
                    path=UNBOUND_HOST_SEARCH,
                    error_kind="unbound_host_not_found",
                ),
            )
        payload = {"host": self.to_payload(spec)}
        set_path = UNBOUND_HOST_SET.format(uuid=existing_uuid)
        response = client.call(
            ctx.target, ctx.key, ctx.secret,
            "POST", set_path,
            json=payload,
        )
        _raise_if_saved_failed(response, set_path, ctx)
        return AddOutcome(uuid=existing_uuid, raw_status=response.status_code)

    def delete(
        self,
        client: HttpClient,
        ctx: RequestContext,
        ident: UnboundHostIdentity,
    ) -> AddOutcome:
        existing_uuid = self._search_uuid(client, ctx, ident)
        if existing_uuid is None:
            return AddOutcome(uuid=None, raw_status=0)
        del_path = UNBOUND_HOST_DEL.format(uuid=existing_uuid)
        response = client.call(
            ctx.target, ctx.key, ctx.secret,
            "POST", del_path,
            json={},
        )
        _raise_if_saved_failed(response, del_path, ctx)
        return AddOutcome(uuid=existing_uuid, raw_status=response.status_code)

    # ----- diff -----

    def diff(
        self, current: UnboundHostSpec | None, target_spec: UnboundHostSpec,
    ) -> Diff:
        if current is None:
            return Diff(
                kind=DiffKind.NEW,
                summary=(
                    f"Neuer Host-Override "
                    f"{target_spec.host}.{target_spec.domain} -> {target_spec.server}"
                ),
            )
        same_server = current.server == target_spec.server
        same_descr = (current.description or "") == (target_spec.description or "")
        same_enabled = current.enabled == target_spec.enabled
        if same_server and same_descr and same_enabled:
            return Diff(
                kind=DiffKind.SKIP,
                summary=(
                    f"Host-Override {target_spec.host}.{target_spec.domain} "
                    "bereits identisch - uebersprungen."
                ),
            )
        return Diff(
            kind=DiffKind.UPDATE,
            summary=(
                f"Konflikt: Host-Override {target_spec.host}.{target_spec.domain} "
                "existiert bereits mit anderem Server/Beschreibung. Nutze "
                "Update-Plan zum Aendern."
            ),
        )

    def diff_for_update(
        self, current: UnboundHostSpec | None, target_spec: UnboundHostSpec,
    ) -> Diff:
        if current is None:
            return Diff(
                kind=DiffKind.NEW,
                summary=(
                    f"Host-Override {target_spec.host}.{target_spec.domain} "
                    "existiert nicht - Update wird beim Apply fehlschlagen."
                ),
            )
        same_server = current.server == target_spec.server
        same_descr = (current.description or "") == (target_spec.description or "")
        same_enabled = current.enabled == target_spec.enabled
        if same_server and same_descr and same_enabled:
            return Diff(
                kind=DiffKind.SKIP,
                summary=(
                    f"Host-Override {target_spec.host}.{target_spec.domain} "
                    "bereits identisch - uebersprungen."
                ),
            )
        changes = []
        if not same_server:
            changes.append(f"Server {current.server or '-'}->{target_spec.server}")
        if not same_enabled:
            changes.append("aktivieren" if target_spec.enabled else "deaktivieren")
        if not same_descr:
            changes.append("Beschreibung geaendert")
        return Diff(
            kind=DiffKind.UPDATE,
            summary=(
                f"Host-Override {target_spec.host}.{target_spec.domain} "
                f"aktualisieren ({', '.join(changes)})"
            ),
        )

    def diff_for_delete(
        self, current: UnboundHostSpec | None, ident: UnboundHostIdentity,
    ) -> Diff:
        if current is None:
            return Diff(
                kind=DiffKind.SKIP,
                summary=(
                    f"Host-Override {ident.host}.{ident.domain} existiert "
                    "nicht - bereits weg."
                ),
            )
        return Diff(
            kind=DiffKind.DELETE,
            summary=f"Host-Override {ident.host}.{ident.domain} wird geloescht",
        )

    # ----- API-Helfer -----

    def _search_uuid(
        self,
        client: HttpClient,
        ctx: RequestContext,
        ident: UnboundHostIdentity,
    ) -> str | None:
        response = client.call(
            ctx.target, ctx.key, ctx.secret,
            "POST", UNBOUND_HOST_SEARCH,
            json={"current": 1, "rowCount": -1},
        )
        try:
            data: Any = response.json()
        except ValueError:
            return None
        if not isinstance(data, dict):
            return None
        rows = data.get("rows")
        if not isinstance(rows, list):
            return None
        for row in rows:
            if not isinstance(row, dict):
                continue
            host = str(row.get("hostname", row.get("host", ""))).strip()
            domain = str(row.get("domain", "")).strip()
            if host != ident.host or domain != ident.domain:
                continue
            uuid = row.get("uuid")
            if isinstance(uuid, str) and uuid:
                return uuid
        return None

    def _get_by_uuid(
        self,
        client: HttpClient,
        ctx: RequestContext,
        uuid: str,
    ) -> UnboundHostSpec:
        response = client.call(
            ctx.target, ctx.key, ctx.secret,
            "GET", UNBOUND_HOST_GET.format(uuid=uuid),
        )
        try:
            data: Any = response.json()
        except ValueError:
            return UnboundHostSpec(host="", domain="")
        if not isinstance(data, dict):
            return UnboundHostSpec(host="", domain="")
        inner = data.get("host")
        row = inner if isinstance(inner, dict) else data
        return _row_to_spec(row)


# ---------------------------------------------------------------------------
# Subsystem-Controller
# ---------------------------------------------------------------------------


class UnboundController:
    """Traegt den Reconfigure-Aufruf fuer das Unbound-Subsystem."""

    subsystem: ClassVar[str] = "unbound_hosts"

    def reconfigure(self, client: HttpClient, ctx: RequestContext) -> None:
        try:
            client.call(
                ctx.target, ctx.key, ctx.secret,
                "POST", UNBOUND_RECONFIGURE,
                json={},
                timeout_override_s=client.tuning.reconfigure_timeout_s,
            )
        except (
            UnreachableError, AuthError, ValidationError,
            ApiError, EgressDeniedError,
        ) as exc:
            raise ReconfigureError(
                "Reconfigure des Unbound-Subsystems fehlgeschlagen.",
                context=make_context(
                    host=ctx.target.host,
                    port=ctx.target.port,
                    method="POST",
                    path=UNBOUND_RECONFIGURE,
                    error_kind="reconfigure",
                    summary=exc.context.summary,
                    status_code=exc.context.status_code,
                ),
            ) from exc


__all__ = [
    "UnboundController",
    "UnboundHostAdapter",
    "UnboundHostIdentity",
    "UnboundHostSpec",
]
