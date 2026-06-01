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
    ROUTES_RECONFIGURE,
    ROUTES_SEARCH,
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
