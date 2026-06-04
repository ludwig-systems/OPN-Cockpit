"""``RuleAdapter`` und ``RulesController`` fuer Firewall-Filter-Regeln.

OPNsense modelliert Filter-Regeln ueber das ``os-firewall``-Plugin
(``/api/firewall/filter/...``). Anders als bei Aliases und Routes haben
Regeln keinen stabilen User-Schluessel - die OPNsense-UUID ist die einzige
verlaessliche Identitaet. Daher uebergibt das Cockpit-UI immer die UUID
direkt fuer Update + Delete.

Add-Operationen haben keine UUID; sie sind nicht idempotent in dem Sinne,
dass ein zweiter Klick keinen Duplikat-Check macht. Wir akzeptieren das
fuer v0.8 - der User sieht in der Live-Liste sofort wenn er versehentlich
zwei identische Regeln erstellt hat.

API-Endpoints (siehe ``_endpoints.py``):

* ``POST /api/firewall/filter/searchRule`` - Liste mit Pagination
* ``GET /api/firewall/filter/getRule/{uuid}`` - Detail
* ``POST /api/firewall/filter/addRule`` - Anlage
* ``POST /api/firewall/filter/setRule/{uuid}`` - Update
* ``POST /api/firewall/filter/delRule/{uuid}`` - Delete
* ``POST /api/firewall/filter/apply`` - Aktivierung (= Reconfigure)
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
    RULE_ADD,
    RULE_APPLY,
    RULE_DEL,
    RULE_GET,
    RULE_SEARCH,
    RULE_SET,
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
class RuleIdentity:
    """OPNsense-UUID identifiziert die Regel eindeutig.

    Leere UUID = "noch nicht angelegt" (Add-Pfad). Wird vom Cockpit nur
    fuer Update + Delete als nicht-leer erwartet.
    """

    uuid: str = ""


@dataclass(frozen=True, slots=True)
class RuleSpec:
    """Soll-Beschreibung einer Filter-Regel.

    Feldset orientiert sich am OPNsense-Modell, aber bewusst reduziert auf
    das was die Cockpit-UI heute sinnvoll ausfuellen kann. Felder die hier
    fehlen (z. B. statetype, statetimeout, tagging) muss der Admin direkt
    in der OPNsense-UI setzen.
    """

    uuid: str = ""

    enabled: bool = True
    action: str = "pass"  # pass | block | reject
    interface: str = ""  # Interface-Identifier, z. B. "lan", "opt1"
    direction: str = "in"  # in | out
    ipprotocol: str = "inet"  # inet | inet6
    protocol: str = "any"  # any | tcp | udp | icmp | esp | ah | ...

    source_net: str = "any"
    source_port: str = ""
    source_not: bool = False

    destination_net: str = "any"
    destination_port: str = ""
    destination_not: bool = False

    gateway: str = ""
    log: bool = False
    description: str = ""
    sequence: int | None = None

    def to_identity(self) -> RuleIdentity:
        return RuleIdentity(uuid=self.uuid)


# ---------------------------------------------------------------------------
# Helfer
# ---------------------------------------------------------------------------


def _raise_if_saved_failed(response: Any, path: str, ctx: RequestContext) -> None:
    """OPNsense kann mit 200 OK + ``{"result":"failed"}`` antworten.

    Wir werfen dann einen sprechenden ``ApiError`` mit den Validierungs-
    Details. Identisch zur Helper-Implementierung in ``routes.py`` -
    bewusste Duplikation um die Module unabhaengig zu halten.
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


def _selected_key(value: Any, default: str = "") -> str:
    """OPNsense liefert Single-Select-Felder als
    ``{key: {"value": ..., "selected": 0|1}}``. Liefert den selected Key.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for k, v in value.items():
            if isinstance(v, dict) and str(v.get("selected", "0")) in {"1", "true"}:
                return str(k)
    return default


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def _row_to_spec(row: dict[str, Any], uuid: str = "") -> RuleSpec:
    """Mappt eine getRule/searchRule-Zeile auf einen ``RuleSpec``.

    Defensiv gegen fehlende Felder - OPNsense-API-Schema kann zwischen
    Versionen leicht abweichen.
    """
    source = row.get("source") or {}
    destination = row.get("destination") or {}
    if not isinstance(source, dict):
        source = {}
    if not isinstance(destination, dict):
        destination = {}
    sequence_raw = row.get("sequence")
    try:
        sequence = int(sequence_raw) if sequence_raw not in (None, "") else None
    except (TypeError, ValueError):
        sequence = None
    return RuleSpec(
        uuid=uuid or str(row.get("uuid", "")),
        enabled=_as_bool(row.get("enabled", "1")),
        action=_selected_key(row.get("action"), "pass"),
        interface=_selected_key(row.get("interface"), ""),
        direction=_selected_key(row.get("direction"), "in"),
        ipprotocol=_selected_key(row.get("ipprotocol"), "inet"),
        protocol=_selected_key(row.get("protocol"), "any"),
        source_net=_selected_key(source.get("network"), "any"),
        source_port=_selected_key(source.get("port"), ""),
        source_not=_as_bool(source.get("not", "0")),
        destination_net=_selected_key(destination.get("network"), "any"),
        destination_port=_selected_key(destination.get("port"), ""),
        destination_not=_as_bool(destination.get("not", "0")),
        gateway=_selected_key(row.get("gateway"), ""),
        log=_as_bool(row.get("log", "0")),
        description=str(row.get("description", "")),
        sequence=sequence,
    )


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class RuleAdapter:
    """Adapter fuer OPNsense-Firewall-Filter-Regeln (os-firewall Plugin)."""

    subsystem: ClassVar[str] = "firewall_rules"

    def identity(self, spec: RuleSpec) -> RuleIdentity:
        return spec.to_identity()

    def to_payload(self, spec: RuleSpec) -> dict[str, Any]:
        """Erzeugt das inner-payload fuer addRule/setRule.

        Booleans gehen als "0"/"1" raus (OPNsense-Konvention), Selects als
        nackter Key (das setItem-Backend macht daraus selbst die selected-
        Markierung). ``sequence`` wird ausgelassen wenn None, sonst rutscht
        die Regel ohne Grund ans Ende.
        """
        payload: dict[str, Any] = {
            "enabled": "1" if spec.enabled else "0",
            "action": spec.action,
            "interface": spec.interface,
            "direction": spec.direction,
            "ipprotocol": spec.ipprotocol,
            "protocol": spec.protocol,
            "source_net": spec.source_net,
            "source_port": spec.source_port,
            "source_not": "1" if spec.source_not else "0",
            "destination_net": spec.destination_net,
            "destination_port": spec.destination_port,
            "destination_not": "1" if spec.destination_not else "0",
            "gateway": spec.gateway,
            "log": "1" if spec.log else "0",
            "description": spec.description,
        }
        if spec.sequence is not None:
            payload["sequence"] = str(spec.sequence)
        return payload

    def spec_to_dict(self, spec: RuleSpec) -> dict[str, Any]:
        return {
            "uuid": spec.uuid,
            "enabled": spec.enabled,
            "action": spec.action,
            "interface": spec.interface,
            "direction": spec.direction,
            "ipprotocol": spec.ipprotocol,
            "protocol": spec.protocol,
            "source_net": spec.source_net,
            "source_port": spec.source_port,
            "source_not": spec.source_not,
            "destination_net": spec.destination_net,
            "destination_port": spec.destination_port,
            "destination_not": spec.destination_not,
            "gateway": spec.gateway,
            "log": spec.log,
            "description": spec.description,
            "sequence": spec.sequence,
        }

    def spec_from_dict(self, raw: dict[str, Any]) -> RuleSpec:
        sequence_raw = raw.get("sequence")
        try:
            sequence = (
                int(sequence_raw) if sequence_raw not in (None, "") else None
            )
        except (TypeError, ValueError):
            sequence = None
        return RuleSpec(
            uuid=str(raw.get("uuid", "")),
            enabled=bool(raw.get("enabled", True)),
            action=str(raw.get("action", "pass")),
            interface=str(raw.get("interface", "")),
            direction=str(raw.get("direction", "in")),
            ipprotocol=str(raw.get("ipprotocol", "inet")),
            protocol=str(raw.get("protocol", "any")),
            source_net=str(raw.get("source_net", "any")),
            source_port=str(raw.get("source_port", "")),
            source_not=bool(raw.get("source_not", False)),
            destination_net=str(raw.get("destination_net", "any")),
            destination_port=str(raw.get("destination_port", "")),
            destination_not=bool(raw.get("destination_not", False)),
            gateway=str(raw.get("gateway", "")),
            log=bool(raw.get("log", False)),
            description=str(raw.get("description", "")),
            sequence=sequence,
        )

    # ----- exists / verify -----

    def exists(
        self,
        client: HttpClient,
        ctx: RequestContext,
        ident: RuleIdentity,
    ) -> RuleSpec | None:
        """Liefert die Live-Regel fuer eine UUID, oder None wenn weg.

        Leere UUID = "kein Pre-Check moeglich" -> None. Der Add-Pfad nutzt
        das absichtlich (Diff: NEW).
        """
        if not ident.uuid:
            return None
        try:
            response = client.call(
                ctx.target, ctx.key, ctx.secret,
                "GET", RULE_GET.format(uuid=ident.uuid),
            )
        except ValidationError:
            # OPNsense liefert 404 als ValidationError - wir interpretieren
            # das als "Regel existiert nicht".
            return None
        try:
            data: Any = response.json()
        except ValueError:
            return None
        if not isinstance(data, dict):
            return None
        inner = data.get("rule")
        row = inner if isinstance(inner, dict) else data
        return _row_to_spec(row, uuid=ident.uuid)

    def verify(
        self,
        client: HttpClient,
        ctx: RequestContext,
        ident: RuleIdentity,
    ) -> VerifyOutcome:
        current = self.exists(client, ctx, ident)
        if current is None:
            return VerifyOutcome(found=False)
        return VerifyOutcome(
            found=True,
            detail=f"uuid={current.uuid}, descr={current.description}",
        )

    # ----- add / update / delete -----

    def add(
        self,
        client: HttpClient,
        ctx: RequestContext,
        spec: RuleSpec,
    ) -> AddOutcome:
        payload = {"rule": self.to_payload(spec)}
        response = client.call(
            ctx.target, ctx.key, ctx.secret,
            "POST", RULE_ADD,
            json=payload,
        )
        _raise_if_saved_failed(response, RULE_ADD, ctx)
        try:
            body = response.json()
        except ValueError:
            body = {}
        new_uuid = None
        if isinstance(body, dict):
            candidate = body.get("uuid")
            if isinstance(candidate, str) and candidate:
                new_uuid = candidate
        return AddOutcome(uuid=new_uuid, raw_status=response.status_code)

    def update(
        self,
        client: HttpClient,
        ctx: RequestContext,
        spec: RuleSpec,
    ) -> AddOutcome:
        if not spec.uuid:
            raise ValidationError(
                "Update verlangt eine UUID - die Regel muss bereits existieren.",
                context=make_context(
                    host=ctx.target.host,
                    port=ctx.target.port,
                    method="POST",
                    path=RULE_SET,
                    error_kind="rule_uuid_missing",
                ),
            )
        payload = {"rule": self.to_payload(spec)}
        set_path = RULE_SET.format(uuid=spec.uuid)
        response = client.call(
            ctx.target, ctx.key, ctx.secret,
            "POST", set_path,
            json=payload,
        )
        _raise_if_saved_failed(response, set_path, ctx)
        return AddOutcome(uuid=spec.uuid, raw_status=response.status_code)

    def delete(
        self,
        client: HttpClient,
        ctx: RequestContext,
        ident: RuleIdentity,
    ) -> AddOutcome:
        if not ident.uuid:
            return AddOutcome(uuid=None, raw_status=0)
        del_path = RULE_DEL.format(uuid=ident.uuid)
        response = client.call(
            ctx.target, ctx.key, ctx.secret,
            "POST", del_path,
            json={},
        )
        _raise_if_saved_failed(response, del_path, ctx)
        return AddOutcome(uuid=ident.uuid, raw_status=response.status_code)

    # ----- diff -----

    def diff(self, current: RuleSpec | None, target_spec: RuleSpec) -> Diff:
        # ADD-Pfad: UUID leer, current None -> einfach NEW.
        if current is None:
            label = target_spec.description or target_spec.action
            return Diff(
                kind=DiffKind.NEW,
                summary=f"Neue Filter-Regel ({label})",
            )
        # current existiert: das ist die "Add mit gleicher UUID" Anomalie.
        # Sollten wir niemals sehen weil ADD die UUID leer haelt. Defensiv
        # behandeln als SKIP - dasselbe Objekt soll nicht doppelt entstehen.
        return Diff(
            kind=DiffKind.SKIP,
            summary="Regel existiert bereits - kein Add noetig.",
        )

    def diff_for_update(
        self, current: RuleSpec | None, target_spec: RuleSpec,
    ) -> Diff:
        if current is None:
            return Diff(
                kind=DiffKind.NEW,
                summary=(
                    f"Regel {target_spec.uuid} existiert nicht - "
                    "Update wird beim Apply fehlschlagen."
                ),
            )
        diffs = _field_diffs(current, target_spec)
        if not diffs:
            label = target_spec.description or target_spec.uuid
            return Diff(
                kind=DiffKind.SKIP,
                summary=f"Regel '{label}' bereits identisch - uebersprungen.",
            )
        label = target_spec.description or target_spec.uuid
        return Diff(
            kind=DiffKind.UPDATE,
            summary=f"Regel '{label}' aktualisieren ({', '.join(diffs)})",
        )

    def diff_for_delete(
        self, current: RuleSpec | None, ident: RuleIdentity,
    ) -> Diff:
        if current is None:
            return Diff(
                kind=DiffKind.SKIP,
                summary=f"Regel {ident.uuid} existiert nicht - bereits weg.",
            )
        label = current.description or ident.uuid
        return Diff(
            kind=DiffKind.DELETE,
            summary=f"Regel '{label}' wird geloescht",
        )


def _field_diffs(current: RuleSpec, target: RuleSpec) -> list[str]:
    """Liefert die Liste der geaenderten Felder als Kurztext fuer die UI."""
    changes: list[str] = []
    if current.enabled != target.enabled:
        changes.append("aktivieren" if target.enabled else "deaktivieren")
    if current.action != target.action:
        changes.append(f"Action {current.action}->{target.action}")
    if current.interface != target.interface:
        changes.append(f"Interface {current.interface}->{target.interface}")
    if current.direction != target.direction:
        changes.append(f"Direction {current.direction}->{target.direction}")
    if current.protocol != target.protocol:
        changes.append(f"Protocol {current.protocol}->{target.protocol}")
    if (current.source_net, current.source_port, current.source_not) != (
        target.source_net, target.source_port, target.source_not
    ):
        changes.append("Source geaendert")
    if (
        current.destination_net,
        current.destination_port,
        current.destination_not,
    ) != (
        target.destination_net,
        target.destination_port,
        target.destination_not,
    ):
        changes.append("Destination geaendert")
    if current.gateway != target.gateway:
        changes.append(f"Gateway {current.gateway or '-'}->{target.gateway or '-'}")
    if current.log != target.log:
        changes.append("Log " + ("an" if target.log else "aus"))
    if (current.description or "") != (target.description or ""):
        changes.append("Beschreibung geaendert")
    return changes


# ---------------------------------------------------------------------------
# Subsystem-Controller
# ---------------------------------------------------------------------------


class RulesController:
    """Traegt den ``apply``-Aufruf fuer das Filter-Subsystem."""

    subsystem: ClassVar[str] = "firewall_rules"

    def reconfigure(self, client: HttpClient, ctx: RequestContext) -> None:
        try:
            client.call(
                ctx.target, ctx.key, ctx.secret,
                "POST", RULE_APPLY,
                json={},
                timeout_override_s=client.tuning.reconfigure_timeout_s,
            )
        except (
            UnreachableError, AuthError, ValidationError,
            ApiError, EgressDeniedError,
        ) as exc:
            raise ReconfigureError(
                "Apply des Filter-Subsystems fehlgeschlagen.",
                context=make_context(
                    host=ctx.target.host,
                    port=ctx.target.port,
                    method="POST",
                    path=RULE_APPLY,
                    error_kind="reconfigure",
                    summary=exc.context.summary,
                    status_code=exc.context.status_code,
                ),
            ) from exc


__all__ = [
    "RuleAdapter",
    "RuleIdentity",
    "RuleSpec",
    "RulesController",
]
