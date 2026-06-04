"""``RouteAdapter`` und ``RoutesController`` für statische Routen.

Erste konkrete Implementierung des in ``base.py`` definierten ``ObjectAdapter``-
Protocols. Dient als Referenzmuster für spätere Objekttypen (Alias, Unbound,
Firewall-Rules) und als End-to-End-Slice bis zum CLI in Schritt 6.

API-Konventionen (zu verifizieren mit Schritt 0 / API-Spike):

* Schreiben:    ``POST /api/routes/routes/addroute``, Body ``{"route": {...}}``
* Suche:        ``POST /api/routes/routes/searchroute``
* Aktivieren:   ``POST /api/routes/routes/reconfigure``
* Identität:    Eine Route wird über das Paar (``network``, ``gateway``)
                eindeutig identifiziert. Gateway-Namen sind case-sensitive.
"""

from __future__ import annotations

import ipaddress
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
    ROUTES_ADD,
    ROUTES_DEL,
    ROUTES_RECONFIGURE,
    ROUTES_SEARCH,
    ROUTES_SET,
)
from opn_cockpit.core.objects.base import (
    AddOutcome,
    Diff,
    DiffKind,
    RequestContext,
    VerifyOutcome,
)
from opn_cockpit.core.validation import parse_cidr, validate_gateway_name

if TYPE_CHECKING:
    from opn_cockpit.core.http_client import HttpClient

# ---------------------------------------------------------------------------
# Datentypen
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RouteIdentity:
    """Eindeutiger Schlüssel einer statischen Route auf einer OPNsense.

    Die Kombination (``network``, ``gateway``) ist nach OPNsense-Logik die
    Identität eines Routen-Eintrags. Eine identische Route mit anderem
    Gateway zählt als andere Route.
    """

    network: str
    gateway: str


@dataclass(frozen=True, slots=True)
class RouteSpec:
    """Vollständige Soll-Beschreibung einer statischen Route.

    Wird vom Aufrufer (CLI/GUI) konstruiert, vom Planner für Vorschau und
    Diff verwendet und vom ``RouteAdapter`` an die OPNsense-API übermittelt.
    """

    network: str
    gateway: str
    descr: str = ""
    disabled: bool = False

    def to_identity(self) -> RouteIdentity:
        return RouteIdentity(network=self.network, gateway=self.gateway)


# ---------------------------------------------------------------------------
# Helfer
# ---------------------------------------------------------------------------


def _normalize_cidr(value: str | None) -> str | None:
    """Normalisiert eine CIDR-Notation für Identitätsvergleiche.

    Tolerant gegenüber Host-Bits in der Eingabe (``strict=False``), weil die
    OPNsense-API in seltenen Edge-Fällen einen Eintrag mit Host-Bits
    zurückliefern könnte und ein Vergleich sonst false-negative wäre.
    """
    if not value:
        return None
    try:
        return str(ipaddress.ip_network(value.strip(), strict=False))
    except ValueError:
        return None


def _raise_if_saved_failed(response: Any, path: str, ctx: RequestContext) -> None:
    """OPNsense kann mit 200 OK + ``{"result":"failed"}`` antworten - das ist
    kein Erfolg. Im Failed-Fall wirft die Funktion einen ApiError mit den
    Validierungs-Details aus dem Body.

    Wird von add/update/delete verwendet damit die Save-Erkennung in einer
    Stelle steht.
    """
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
        detail = "; ".join(
            f"{k}: {v}" for k, v in validations.items() if v
        )
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


def _row_to_spec(row: dict[str, Any]) -> RouteSpec:
    """Mappt eine Such-API-Zeile auf einen ``RouteSpec``.

    OPNsense gibt boolesche Felder häufig als ``"0"`` / ``"1"`` zurück; das
    fangen wir hier defensiv ab.
    """
    raw_disabled = row.get("disabled", "0")
    disabled = str(raw_disabled).strip() not in ("", "0", "false", "False")
    return RouteSpec(
        network=str(row.get("network", "")),
        gateway=str(row.get("gateway", "")),
        descr=str(row.get("descr", "")),
        disabled=disabled,
    )


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class RouteAdapter:
    """Adapter für statische Routen."""

    subsystem: ClassVar[str] = "routes"

    def identity(self, spec: RouteSpec) -> RouteIdentity:
        return spec.to_identity()

    def to_payload(self, spec: RouteSpec) -> dict[str, Any]:
        """Erzeugt das Inner-Payload für ``addroute``.

        Das äußere Wrapping in ``{"route": ...}`` macht ``add`` — die rohe
        Payload-Form hier ist auch das, was die Vorschau anzeigt.
        """
        return {
            "network": spec.network,
            "gateway": spec.gateway,
            "descr": spec.descr,
            "disabled": "1" if spec.disabled else "0",
        }

    def spec_to_dict(self, spec: RouteSpec) -> dict[str, Any]:
        """Serialisiert für den Plan-Store (zwischen ``plan`` und ``apply``)."""
        return {
            "network": spec.network,
            "gateway": spec.gateway,
            "descr": spec.descr,
            "disabled": spec.disabled,
        }

    def spec_from_dict(self, raw: dict[str, Any]) -> RouteSpec:
        return RouteSpec(
            network=str(raw.get("network", "")),
            gateway=str(raw.get("gateway", "")),
            descr=str(raw.get("descr", "")),
            disabled=bool(raw.get("disabled", False)),
        )

    def exists(
        self,
        client: HttpClient,
        ctx: RequestContext,
        ident: RouteIdentity,
    ) -> RouteSpec | None:
        """Sucht eine bestehende Route mit derselben (network, gateway)-Identität.

        Antwortformat der Suche: ``{"rows": [...], "rowCount": N, ...}``.
        Wir lesen ``rows`` defensiv aus und vergleichen Netze CIDR-normalisiert.
        """
        response = client.call(
            ctx.target,
            ctx.key,
            ctx.secret,
            "POST",
            ROUTES_SEARCH,
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
        ident_network_norm = _normalize_cidr(ident.network)
        for row in rows:
            if not isinstance(row, dict):
                continue
            row_network = _normalize_cidr(row.get("network"))
            if row_network is None or ident_network_norm is None:
                continue
            if row_network != ident_network_norm:
                continue
            if str(row.get("gateway", "")) != ident.gateway:
                continue
            return _row_to_spec(row)
        return None

    def add(
        self,
        client: HttpClient,
        ctx: RequestContext,
        spec: RouteSpec,
    ) -> AddOutcome:
        """Schreibt eine Route per ``addroute``.

        Validiert clientseitig CIDR und Gateway-Name, damit der Admin
        Fehleingaben sofort und ohne API-Round-Trip sieht.
        """
        parse_cidr(spec.network)
        validate_gateway_name(spec.gateway)
        payload = {"route": self.to_payload(spec)}
        response = client.call(
            ctx.target,
            ctx.key,
            ctx.secret,
            "POST",
            ROUTES_ADD,
            json=payload,
        )
        try:
            body = response.json()
        except ValueError:
            body = {}
        # OPNsense liefert bei Validation-Fehlern 200 OK plus
        # {"result":"failed", "validations":{...}} — sonst wuerde der
        # Apply als Erfolg gelten und der Eintrag waere doch nicht da.
        if isinstance(body, dict):
            result = body.get("result")
            if isinstance(result, str) and result.lower() in {"failed", "error"}:
                validations = body.get("validations")
                detail = ""
                if isinstance(validations, dict) and validations:
                    detail = "; ".join(
                        f"{k}: {v}" for k, v in validations.items() if v
                    )
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
                        path=ROUTES_ADD,
                        error_kind="opnsense_save_failed",
                        summary=f"OPNsense lehnte ab: {detail}" if detail else msg,
                    ),
                )
        uuid: str | None = None
        if isinstance(body, dict):
            candidate = body.get("uuid")
            if isinstance(candidate, str) and candidate:
                uuid = candidate
        return AddOutcome(uuid=uuid, raw_status=response.status_code)

    def update(
        self,
        client: HttpClient,
        ctx: RequestContext,
        spec: RouteSpec,
    ) -> AddOutcome:
        """Modifiziert eine bestehende Route (setroute/{uuid}).

        Identitaet aus ``spec`` (network + gateway) - der Adapter sucht
        die UUID dazu, schickt dann ``setroute``. Wenn die Route nicht
        existiert: ValidationError.
        """
        parse_cidr(spec.network)
        validate_gateway_name(spec.gateway)
        existing_uuid = self._search_uuid(client, ctx, spec.to_identity())
        if existing_uuid is None:
            raise ValidationError(
                f"Route {spec.network} via {spec.gateway} existiert nicht - "
                "Update nicht moeglich.",
                context=make_context(
                    host=ctx.target.host,
                    port=ctx.target.port,
                    method="POST",
                    path=ROUTES_SEARCH,
                    error_kind="route_not_found",
                ),
            )
        payload = {"route": self.to_payload(spec)}
        set_path = ROUTES_SET.format(uuid=existing_uuid)
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
        ident: RouteIdentity,
    ) -> AddOutcome:
        """Loescht eine bestehende Route (delroute/{uuid}).

        Idempotent: wenn die Route schon weg ist, gibt's einen leeren
        AddOutcome zurueck - Planner sollte das ohnehin als SKIP gemeldet
        haben, das hier ist die Defense-Line.
        """
        existing_uuid = self._search_uuid(client, ctx, ident)
        if existing_uuid is None:
            return AddOutcome(uuid=None, raw_status=0)
        del_path = ROUTES_DEL.format(uuid=existing_uuid)
        response = client.call(
            ctx.target, ctx.key, ctx.secret,
            "POST", del_path,
            json={},
        )
        _raise_if_saved_failed(response, del_path, ctx)
        return AddOutcome(uuid=existing_uuid, raw_status=response.status_code)

    def diff_for_update(
        self,
        current: RouteSpec | None,
        target_spec: RouteSpec,
    ) -> Diff:
        if current is None:
            return Diff(
                kind=DiffKind.NEW,
                summary=(
                    f"Route {target_spec.network} via {target_spec.gateway} "
                    "existiert nicht - Update wird beim Apply fehlschlagen."
                ),
            )
        same_descr = (current.descr or "") == (target_spec.descr or "")
        same_disabled = current.disabled == target_spec.disabled
        if same_descr and same_disabled:
            return Diff(
                kind=DiffKind.SKIP,
                summary=(
                    f"Route {target_spec.network} via {target_spec.gateway} "
                    "bereits identisch - uebersprungen."
                ),
            )
        changes = []
        if not same_disabled:
            changes.append(
                "aktivieren" if not target_spec.disabled else "deaktivieren",
            )
        if not same_descr:
            changes.append("Beschreibung geaendert")
        return Diff(
            kind=DiffKind.UPDATE,
            summary=(
                f"Route {target_spec.network} via {target_spec.gateway} "
                f"aktualisieren ({', '.join(changes)})"
            ),
        )

    def diff_for_delete(
        self,
        current: RouteSpec | None,
        ident: RouteIdentity,
    ) -> Diff:
        if current is None:
            return Diff(
                kind=DiffKind.SKIP,
                summary=(
                    f"Route {ident.network} via {ident.gateway} existiert "
                    "nicht - bereits weg."
                ),
            )
        return Diff(
            kind=DiffKind.DELETE,
            summary=(
                f"Route {ident.network} via {ident.gateway} wird geloescht"
            ),
        )

    def _search_uuid(
        self,
        client: HttpClient,
        ctx: RequestContext,
        ident: RouteIdentity,
    ) -> str | None:
        """Liefert die OPNsense-UUID einer bestehenden Route oder None.

        Sucht ueber den searchroute-Endpoint und matched per
        CIDR-normalisiertem Netz + Gateway-Name. Wird von update/delete
        gerufen - exists liefert nur den Spec, nicht die UUID.
        """
        response = client.call(
            ctx.target, ctx.key, ctx.secret,
            "POST", ROUTES_SEARCH,
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
        ident_net = _normalize_cidr(ident.network)
        for row in rows:
            if not isinstance(row, dict):
                continue
            if _normalize_cidr(row.get("network")) != ident_net:
                continue
            if str(row.get("gateway", "")) != ident.gateway:
                continue
            uuid = row.get("uuid")
            if isinstance(uuid, str) and uuid:
                return uuid
        return None

    def verify(
        self,
        client: HttpClient,
        ctx: RequestContext,
        ident: RouteIdentity,
    ) -> VerifyOutcome:
        """Read-back: existiert der Eintrag nach ``reconfigure``?

        Erfolg ist hart definiert über die Existenz im Such-Endpoint, nicht
        über die ``add``-Antwort (R-RUN-2).
        """
        current = self.exists(client, ctx, ident)
        if current is None:
            return VerifyOutcome(found=False)
        return VerifyOutcome(
            found=True,
            detail=f"network={current.network}, gateway={current.gateway}",
        )

    def diff(self, current: RouteSpec | None, target_spec: RouteSpec) -> Diff:
        """Bestimmt die Aktion für die Vorschau.

        Statische Routen werden in v1 **nicht** in-place aktualisiert (kein
        ``setroute``-Endpoint im Standardfall). Wenn ein Eintrag mit
        passender Identität existiert, melden wir ``SKIP`` (Idempotenz,
        R-RUN-5), unabhängig davon, ob ``descr``/``disabled`` drifteten —
        Drift muss der Admin im OPNsense-UI auflösen.
        """
        if current is None:
            return Diff(
                kind=DiffKind.NEW,
                summary=(
                    f"Neue Route {target_spec.network} via {target_spec.gateway}"
                ),
            )
        return Diff(
            kind=DiffKind.SKIP,
            summary=(
                f"Route {target_spec.network} via {target_spec.gateway} "
                "ist bereits vorhanden — wird übersprungen."
            ),
        )


# ---------------------------------------------------------------------------
# Subsystem-Controller
# ---------------------------------------------------------------------------


class RoutesController:
    """Trägt den ``reconfigure``-Aufruf für das Routen-Subsystem.

    Wird vom Executor pro Gerät **einmal** aufgerufen, nachdem alle
    ``add``-Calls von ``RouteAdapter`` durch sind. Ein Fehlschlag landet
    als ``ReconfigureError`` beim Executor, der das Gerät als
    ``Status.WRITTEN`` mit ``failed_phase=ACTIVATE`` markiert.
    """

    subsystem: ClassVar[str] = "routes"

    def reconfigure(self, client: HttpClient, ctx: RequestContext) -> None:
        try:
            client.call(
                ctx.target,
                ctx.key,
                ctx.secret,
                "POST",
                ROUTES_RECONFIGURE,
                json={},
                timeout_override_s=client.tuning.reconfigure_timeout_s,
            )
        except (UnreachableError, AuthError, ValidationError, ApiError, EgressDeniedError) as exc:
            raise ReconfigureError(
                "reconfigure des Routen-Subsystems fehlgeschlagen.",
                context=make_context(
                    host=ctx.target.host,
                    port=ctx.target.port,
                    method="POST",
                    path=ROUTES_RECONFIGURE,
                    error_kind="reconfigure",
                    summary=exc.context.summary,
                    status_code=exc.context.status_code,
                ),
            ) from exc
