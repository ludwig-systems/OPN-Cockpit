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
    UNBOUND_DOMAIN_ADD,
    UNBOUND_DOMAIN_DEL,
    UNBOUND_DOMAIN_GET,
    UNBOUND_DOMAIN_SEARCH,
    UNBOUND_DOMAIN_SET,
    UNBOUND_FORWARD_ADD,
    UNBOUND_FORWARD_DEL,
    UNBOUND_FORWARD_GET,
    UNBOUND_FORWARD_SEARCH,
    UNBOUND_FORWARD_SET,
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


# ---------------------------------------------------------------------------
# Domain-Overrides: Adapter + Controller
# ---------------------------------------------------------------------------
#
# Ein Domain-Override in OPNsense leitet ALLE Queries fuer eine bestimmte
# Domain (z.B. ``internal.example.com``) an einen internen Resolver weiter.
# Anders als Host-Overrides (die einzelne A-Records erzeugen) delegiert
# Cockpit hier die komplette Zone.


@dataclass(frozen=True, slots=True)
class UnboundDomainIdentity:
    """Identitaet eines Domain-Override-Eintrags.

    OPNsense erlaubt formal mehrere Overrides fuer dieselbe Domain (nur der
    ``server`` unterscheidet sich). Cockpit nutzt ``domain`` allein als
    Identity — die UI baut auf "eine Zone = eine Weiterleitung" auf. Wer
    das explizit umgehen will, editiert direkt in OPNsense.
    """

    domain: str


@dataclass(frozen=True, slots=True)
class UnboundDomainSpec:
    """Soll-Beschreibung eines Domain-Override-Eintrags."""

    domain: str
    server: str = ""        # Ziel-Resolver (IPv4/IPv6)
    description: str = ""
    enabled: bool = True

    def to_identity(self) -> UnboundDomainIdentity:
        return UnboundDomainIdentity(domain=self.domain)


def _domain_row_to_spec(row: dict[str, Any]) -> UnboundDomainSpec:
    return UnboundDomainSpec(
        domain=str(row.get("domain", "")).strip(),
        server=str(row.get("server", row.get("ip", ""))).strip(),
        description=str(row.get("description", row.get("descr", ""))).strip(),
        enabled=_as_bool(row.get("enabled", "1")),
    )


class UnboundDomainAdapter:
    """Adapter fuer Unbound-DNS Domain-Overrides (Zone-Weiterleitungen)."""

    subsystem: ClassVar[str] = "unbound_domains"

    def identity(self, spec: UnboundDomainSpec) -> UnboundDomainIdentity:
        return spec.to_identity()

    def to_payload(self, spec: UnboundDomainSpec) -> dict[str, Any]:
        return {
            "enabled": "1" if spec.enabled else "0",
            "domain": spec.domain,
            "server": spec.server,
            "description": spec.description,
        }

    def spec_to_dict(self, spec: UnboundDomainSpec) -> dict[str, Any]:
        return {
            "domain": spec.domain,
            "server": spec.server,
            "description": spec.description,
            "enabled": spec.enabled,
        }

    def spec_from_dict(self, raw: dict[str, Any]) -> UnboundDomainSpec:
        return UnboundDomainSpec(
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
        ident: UnboundDomainIdentity,
    ) -> UnboundDomainSpec | None:
        uuid = self._search_uuid(client, ctx, ident)
        if uuid is None:
            return None
        return self._get_by_uuid(client, ctx, uuid)

    def verify(
        self,
        client: HttpClient,
        ctx: RequestContext,
        ident: UnboundDomainIdentity,
    ) -> VerifyOutcome:
        current = self.exists(client, ctx, ident)
        if current is None:
            return VerifyOutcome(found=False)
        return VerifyOutcome(
            found=True,
            detail=f"{current.domain} -> {current.server}",
        )

    # ----- add / update / delete -----

    def add(
        self,
        client: HttpClient,
        ctx: RequestContext,
        spec: UnboundDomainSpec,
    ) -> AddOutcome:
        payload = {"domain": self.to_payload(spec)}
        response = client.call(
            ctx.target, ctx.key, ctx.secret,
            "POST", UNBOUND_DOMAIN_ADD,
            json=payload,
        )
        _raise_if_saved_failed(response, UNBOUND_DOMAIN_ADD, ctx)
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
        spec: UnboundDomainSpec,
    ) -> AddOutcome:
        existing_uuid = self._search_uuid(client, ctx, spec.to_identity())
        if existing_uuid is None:
            raise ValidationError(
                f"Domain-Override {spec.domain} existiert nicht - "
                "Update nicht moeglich.",
                context=make_context(
                    host=ctx.target.host,
                    port=ctx.target.port,
                    method="POST",
                    path=UNBOUND_DOMAIN_SEARCH,
                    error_kind="unbound_domain_not_found",
                ),
            )
        payload = {"domain": self.to_payload(spec)}
        set_path = UNBOUND_DOMAIN_SET.format(uuid=existing_uuid)
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
        ident: UnboundDomainIdentity,
    ) -> AddOutcome:
        existing_uuid = self._search_uuid(client, ctx, ident)
        if existing_uuid is None:
            return AddOutcome(uuid=None, raw_status=0)
        del_path = UNBOUND_DOMAIN_DEL.format(uuid=existing_uuid)
        response = client.call(
            ctx.target, ctx.key, ctx.secret,
            "POST", del_path,
            json={},
        )
        _raise_if_saved_failed(response, del_path, ctx)
        return AddOutcome(uuid=existing_uuid, raw_status=response.status_code)

    # ----- diff -----

    def diff(
        self, current: UnboundDomainSpec | None, target_spec: UnboundDomainSpec,
    ) -> Diff:
        if current is None:
            return Diff(
                kind=DiffKind.NEW,
                summary=(
                    f"Neuer Domain-Override "
                    f"{target_spec.domain} -> {target_spec.server}"
                ),
            )
        same_server = current.server == target_spec.server
        same_descr = (current.description or "") == (target_spec.description or "")
        same_enabled = current.enabled == target_spec.enabled
        if same_server and same_descr and same_enabled:
            return Diff(
                kind=DiffKind.SKIP,
                summary=(
                    f"Domain-Override {target_spec.domain} bereits "
                    "identisch - uebersprungen."
                ),
            )
        return Diff(
            kind=DiffKind.UPDATE,
            summary=(
                f"Konflikt: Domain-Override {target_spec.domain} existiert "
                "bereits mit anderem Server/Beschreibung. Nutze Update-Plan "
                "zum Aendern."
            ),
        )

    def diff_for_update(
        self, current: UnboundDomainSpec | None, target_spec: UnboundDomainSpec,
    ) -> Diff:
        if current is None:
            return Diff(
                kind=DiffKind.NEW,
                summary=(
                    f"Domain-Override {target_spec.domain} existiert nicht - "
                    "Update wird beim Apply fehlschlagen."
                ),
            )
        same_server = current.server == target_spec.server
        same_descr = (current.description or "") == (target_spec.description or "")
        same_enabled = current.enabled == target_spec.enabled
        if same_server and same_descr and same_enabled:
            return Diff(
                kind=DiffKind.SKIP,
                summary=(
                    f"Domain-Override {target_spec.domain} bereits "
                    "identisch - uebersprungen."
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
                f"Domain-Override {target_spec.domain} aktualisieren "
                f"({', '.join(changes)})"
            ),
        )

    def diff_for_delete(
        self, current: UnboundDomainSpec | None, ident: UnboundDomainIdentity,
    ) -> Diff:
        if current is None:
            return Diff(
                kind=DiffKind.SKIP,
                summary=(
                    f"Domain-Override {ident.domain} existiert nicht - "
                    "bereits weg."
                ),
            )
        return Diff(
            kind=DiffKind.DELETE,
            summary=f"Domain-Override {ident.domain} wird geloescht",
        )

    # ----- API-Helfer -----

    def _search_uuid(
        self,
        client: HttpClient,
        ctx: RequestContext,
        ident: UnboundDomainIdentity,
    ) -> str | None:
        response = client.call(
            ctx.target, ctx.key, ctx.secret,
            "POST", UNBOUND_DOMAIN_SEARCH,
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
            domain = str(row.get("domain", "")).strip()
            if domain != ident.domain:
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
    ) -> UnboundDomainSpec:
        response = client.call(
            ctx.target, ctx.key, ctx.secret,
            "GET", UNBOUND_DOMAIN_GET.format(uuid=uuid),
        )
        try:
            data: Any = response.json()
        except ValueError:
            return UnboundDomainSpec(domain="")
        if not isinstance(data, dict):
            return UnboundDomainSpec(domain="")
        inner = data.get("domain")
        row = inner if isinstance(inner, dict) else data
        return _domain_row_to_spec(row)


class UnboundDomainsController:
    """Reconfigure-Aufruf fuer Unbound-Domain-Overrides.

    Nutzt denselben ``UNBOUND_RECONFIGURE``-Endpoint wie der Host-Controller;
    das eigene ``subsystem`` sorgt dafuer dass der Executor die Aktivierung
    korrekt pro-Subsystem gruppiert.
    """

    subsystem: ClassVar[str] = "unbound_domains"

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


# ---------------------------------------------------------------------------
# Query-Forwards: Adapter + Controller
# ---------------------------------------------------------------------------
#
# Query-Forwards sind die globalen Weiterleiter, an die Unbound entweder ALLE
# Queries (leere ``domain``) oder Queries fuer eine bestimmte Zone schickt.
# Typischer Use-Case: DoT/DoH-Upstreams (Cloudflare, Quad9). Anders als
# Domain-Overrides sind mehrere Forwards fuer dieselbe Domain zulaessig
# (Failover-Kette) — die Identity muss deshalb (domain, server, port)
# umfassen.


@dataclass(frozen=True, slots=True)
class UnboundForwardIdentity:
    """Identitaet eines Query-Forwards.

    ``domain`` kann leer sein ("alle Queries"). ``server`` + ``port`` sind
    zusammen mit der Domain der eindeutige User-Schluessel; das Cockpit
    ignoriert dabei ``type``/``verify``, da diese Metadaten sind und der
    Betreiber sie durch Update aendern will, ohne dass ein neuer Eintrag
    entsteht.
    """

    domain: str
    server: str
    port: int


_FORWARD_DEFAULT_PORT = 53
_FORWARD_TYPE_FORWARD = "forward"
_FORWARD_ALLOWED_TYPES: tuple[str, ...] = ("forward", "dot")


@dataclass(frozen=True, slots=True)
class UnboundForwardSpec:
    """Soll-Beschreibung eines Query-Forwards."""

    domain: str = ""                       # leer = "alle Queries"
    server: str = ""                       # Upstream-IP
    port: int = _FORWARD_DEFAULT_PORT      # 53 (plain), 853 (DoT), ...
    type: str = _FORWARD_TYPE_FORWARD      # "forward" oder "dot"
    verify: str = ""                       # DoT: Server-CN fuer Cert-Verify
    description: str = ""
    enabled: bool = True

    def to_identity(self) -> UnboundForwardIdentity:
        return UnboundForwardIdentity(
            domain=self.domain, server=self.server, port=self.port,
        )


def _parse_forward_port(raw: Any, default: int = _FORWARD_DEFAULT_PORT) -> int:
    if raw in ("", None):
        return default
    try:
        return int(str(raw))
    except (TypeError, ValueError):
        return default


def _forward_row_to_spec(row: dict[str, Any]) -> UnboundForwardSpec:
    return UnboundForwardSpec(
        domain=str(row.get("domain", "")).strip(),
        server=str(row.get("server", row.get("forward_addr", ""))).strip(),
        port=_parse_forward_port(row.get("port")),
        type=str(row.get("type", _FORWARD_TYPE_FORWARD)).strip()
        or _FORWARD_TYPE_FORWARD,
        verify=str(row.get("verify", "")).strip(),
        description=str(row.get("description", row.get("descr", ""))).strip(),
        enabled=_as_bool(row.get("enabled", "1")),
    )


class UnboundForwardAdapter:
    """Adapter fuer Unbound Query-Forwards (Upstream-Resolver)."""

    subsystem: ClassVar[str] = "unbound_forwards"

    def identity(self, spec: UnboundForwardSpec) -> UnboundForwardIdentity:
        return spec.to_identity()

    def to_payload(self, spec: UnboundForwardSpec) -> dict[str, Any]:
        return {
            "enabled": "1" if spec.enabled else "0",
            "domain": spec.domain,
            "type": spec.type or _FORWARD_TYPE_FORWARD,
            "server": spec.server,
            "port": str(spec.port),
            "verify": spec.verify,
            "description": spec.description,
        }

    def spec_to_dict(self, spec: UnboundForwardSpec) -> dict[str, Any]:
        return {
            "domain": spec.domain,
            "server": spec.server,
            "port": spec.port,
            "type": spec.type,
            "verify": spec.verify,
            "description": spec.description,
            "enabled": spec.enabled,
        }

    def spec_from_dict(self, raw: dict[str, Any]) -> UnboundForwardSpec:
        return UnboundForwardSpec(
            domain=str(raw.get("domain", "")),
            server=str(raw.get("server", "")),
            port=_parse_forward_port(raw.get("port")),
            type=str(raw.get("type", _FORWARD_TYPE_FORWARD)) or _FORWARD_TYPE_FORWARD,
            verify=str(raw.get("verify", "")),
            description=str(raw.get("description", "")),
            enabled=bool(raw.get("enabled", True)),
        )

    # ----- exists / verify -----

    def exists(
        self,
        client: HttpClient,
        ctx: RequestContext,
        ident: UnboundForwardIdentity,
    ) -> UnboundForwardSpec | None:
        uuid = self._search_uuid(client, ctx, ident)
        if uuid is None:
            return None
        return self._get_by_uuid(client, ctx, uuid)

    def verify(
        self,
        client: HttpClient,
        ctx: RequestContext,
        ident: UnboundForwardIdentity,
    ) -> VerifyOutcome:
        current = self.exists(client, ctx, ident)
        if current is None:
            return VerifyOutcome(found=False)
        domain_label = current.domain or "(alle)"
        return VerifyOutcome(
            found=True,
            detail=(
                f"{domain_label} -> {current.server}:{current.port} "
                f"({current.type})"
            ),
        )

    # ----- add / update / delete -----

    def add(
        self,
        client: HttpClient,
        ctx: RequestContext,
        spec: UnboundForwardSpec,
    ) -> AddOutcome:
        payload = {"forward": self.to_payload(spec)}
        response = client.call(
            ctx.target, ctx.key, ctx.secret,
            "POST", UNBOUND_FORWARD_ADD,
            json=payload,
        )
        _raise_if_saved_failed(response, UNBOUND_FORWARD_ADD, ctx)
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
        spec: UnboundForwardSpec,
    ) -> AddOutcome:
        existing_uuid = self._search_uuid(client, ctx, spec.to_identity())
        if existing_uuid is None:
            raise ValidationError(
                f"Query-Forward {spec.domain or '(alle)'} -> "
                f"{spec.server}:{spec.port} existiert nicht - "
                "Update nicht moeglich.",
                context=make_context(
                    host=ctx.target.host,
                    port=ctx.target.port,
                    method="POST",
                    path=UNBOUND_FORWARD_SEARCH,
                    error_kind="unbound_forward_not_found",
                ),
            )
        payload = {"forward": self.to_payload(spec)}
        set_path = UNBOUND_FORWARD_SET.format(uuid=existing_uuid)
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
        ident: UnboundForwardIdentity,
    ) -> AddOutcome:
        existing_uuid = self._search_uuid(client, ctx, ident)
        if existing_uuid is None:
            return AddOutcome(uuid=None, raw_status=0)
        del_path = UNBOUND_FORWARD_DEL.format(uuid=existing_uuid)
        response = client.call(
            ctx.target, ctx.key, ctx.secret,
            "POST", del_path,
            json={},
        )
        _raise_if_saved_failed(response, del_path, ctx)
        return AddOutcome(uuid=existing_uuid, raw_status=response.status_code)

    # ----- diff -----

    def diff(
        self,
        current: UnboundForwardSpec | None,
        target_spec: UnboundForwardSpec,
    ) -> Diff:
        domain_label = target_spec.domain or "(alle)"
        if current is None:
            return Diff(
                kind=DiffKind.NEW,
                summary=(
                    f"Neuer Query-Forward {domain_label} -> "
                    f"{target_spec.server}:{target_spec.port} "
                    f"({target_spec.type})"
                ),
            )
        same_type = (current.type or "") == (target_spec.type or "")
        same_verify = (current.verify or "") == (target_spec.verify or "")
        same_descr = (current.description or "") == (target_spec.description or "")
        same_enabled = current.enabled == target_spec.enabled
        if same_type and same_verify and same_descr and same_enabled:
            return Diff(
                kind=DiffKind.SKIP,
                summary=(
                    f"Query-Forward {domain_label} -> "
                    f"{target_spec.server}:{target_spec.port} "
                    "bereits identisch - uebersprungen."
                ),
            )
        return Diff(
            kind=DiffKind.UPDATE,
            summary=(
                f"Konflikt: Query-Forward {domain_label} -> "
                f"{target_spec.server}:{target_spec.port} existiert bereits "
                "mit anderen Metadaten. Nutze Update-Plan zum Aendern."
            ),
        )

    def diff_for_update(
        self,
        current: UnboundForwardSpec | None,
        target_spec: UnboundForwardSpec,
    ) -> Diff:
        domain_label = target_spec.domain or "(alle)"
        if current is None:
            return Diff(
                kind=DiffKind.NEW,
                summary=(
                    f"Query-Forward {domain_label} -> "
                    f"{target_spec.server}:{target_spec.port} "
                    "existiert nicht - Update wird beim Apply fehlschlagen."
                ),
            )
        same_type = (current.type or "") == (target_spec.type or "")
        same_verify = (current.verify or "") == (target_spec.verify or "")
        same_descr = (current.description or "") == (target_spec.description or "")
        same_enabled = current.enabled == target_spec.enabled
        if same_type and same_verify and same_descr and same_enabled:
            return Diff(
                kind=DiffKind.SKIP,
                summary=(
                    f"Query-Forward {domain_label} -> "
                    f"{target_spec.server}:{target_spec.port} "
                    "bereits identisch - uebersprungen."
                ),
            )
        changes = []
        if not same_type:
            changes.append(
                f"Typ {current.type or '-'}->{target_spec.type or '-'}",
            )
        if not same_verify:
            changes.append("Verify-CN geaendert")
        if not same_enabled:
            changes.append("aktivieren" if target_spec.enabled else "deaktivieren")
        if not same_descr:
            changes.append("Beschreibung geaendert")
        return Diff(
            kind=DiffKind.UPDATE,
            summary=(
                f"Query-Forward {domain_label} -> "
                f"{target_spec.server}:{target_spec.port} "
                f"aktualisieren ({', '.join(changes)})"
            ),
        )

    def diff_for_delete(
        self,
        current: UnboundForwardSpec | None,
        ident: UnboundForwardIdentity,
    ) -> Diff:
        domain_label = ident.domain or "(alle)"
        if current is None:
            return Diff(
                kind=DiffKind.SKIP,
                summary=(
                    f"Query-Forward {domain_label} -> "
                    f"{ident.server}:{ident.port} "
                    "existiert nicht - bereits weg."
                ),
            )
        return Diff(
            kind=DiffKind.DELETE,
            summary=(
                f"Query-Forward {domain_label} -> "
                f"{ident.server}:{ident.port} wird geloescht"
            ),
        )

    # ----- API-Helfer -----

    def _search_uuid(
        self,
        client: HttpClient,
        ctx: RequestContext,
        ident: UnboundForwardIdentity,
    ) -> str | None:
        response = client.call(
            ctx.target, ctx.key, ctx.secret,
            "POST", UNBOUND_FORWARD_SEARCH,
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
            domain = str(row.get("domain", "")).strip()
            server = str(row.get("server", row.get("forward_addr", ""))).strip()
            port = _parse_forward_port(row.get("port"))
            if (
                domain != ident.domain
                or server != ident.server
                or port != ident.port
            ):
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
    ) -> UnboundForwardSpec:
        response = client.call(
            ctx.target, ctx.key, ctx.secret,
            "GET", UNBOUND_FORWARD_GET.format(uuid=uuid),
        )
        try:
            data: Any = response.json()
        except ValueError:
            return UnboundForwardSpec()
        if not isinstance(data, dict):
            return UnboundForwardSpec()
        inner = data.get("forward")
        row = inner if isinstance(inner, dict) else data
        return _forward_row_to_spec(row)


class UnboundForwardsController:
    """Reconfigure-Aufruf fuer Unbound Query-Forwards.

    Wie ``UnboundDomainsController``: eigenes ``subsystem`` fuer die
    Executor-Gruppierung, teilt sich aber physisch den ``reconfigure``-
    Endpoint mit den anderen Unbound-Subsystemen.
    """

    subsystem: ClassVar[str] = "unbound_forwards"

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
    "UnboundDomainAdapter",
    "UnboundDomainIdentity",
    "UnboundDomainsController",
    "UnboundDomainSpec",
    "UnboundForwardAdapter",
    "UnboundForwardIdentity",
    "UnboundForwardSpec",
    "UnboundForwardsController",
    "UnboundHostAdapter",
    "UnboundHostIdentity",
    "UnboundHostSpec",
]
