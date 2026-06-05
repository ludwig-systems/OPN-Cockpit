"""Plan/Apply-Routen: Routen + Aliase via Web-UI.

Wraps die bestehende orchestration-Schicht (Planner + Executor + PlanStore).
Der Plan-Store ist derselbe wie bei der CLI (``%APPDATA%/OPN-Cockpit/plans/``)
— Web und CLI teilen sich also die Pläne, was Cross-Tooling erlaubt.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status

from opn_cockpit.audit.backend import get_audit_backend
from opn_cockpit.core.errors import ValidationError
from opn_cockpit.core.http_client import HttpClient, HttpTarget, HttpTuning
from opn_cockpit.core.objects.aliases import AliasSpec
from opn_cockpit.core.objects.base import ActionKind
from opn_cockpit.core.objects.firewall_rules import RuleSpec
from opn_cockpit.core.objects.routes import RouteSpec
from opn_cockpit.core.objects.unbound import UnboundHostSpec
from opn_cockpit.core.result import RolloutReport, Status
from opn_cockpit.core.validation import (
    parse_cidr,
    validate_alias_content,
    validate_alias_name,
    validate_alias_type,
    validate_gateway_name,
)
from opn_cockpit.inventory.model import Device
from opn_cockpit.orchestration.backend import PlanStoreBackend, get_plan_store_backend
from opn_cockpit.orchestration.executor import Executor
from opn_cockpit.orchestration.plan_store import PlanStoreError
from opn_cockpit.orchestration.planner import Plan, PlannedDeviceAction, Planner
from opn_cockpit.orchestration.registry import get_binding
from opn_cockpit.security.session import Session
from opn_cockpit.web.acl import (
    device_visible_to,
    require_device_ids_accessible,
    require_plan_role,
)
from opn_cockpit.web.api.schemas import (
    AliasDeletePlanRequest,
    AliasPlanRequest,
    AliasUpdatePlanRequest,
    ApplyRequest,
    DeviceResultResponse,
    OutstandingDeviceEntry,
    OutstandingResponse,
    PlanListResponse,
    PlannedActionResponse,
    PlanResponse,
    PlanSummary,
    RolloutReportResponse,
    RouteDeletePlanRequest,
    RoutePlanRequest,
    RouteUpdatePlanRequest,
    RuleDeletePlanRequest,
    RulePlanRequest,
    RuleUpdatePlanRequest,
    UnboundHostDeletePlanRequest,
    UnboundHostPlanRequest,
    UnboundHostUpdatePlanRequest,
)
from opn_cockpit.web.auth.dependencies import (
    require_session,
    require_session_with_token,
)
from opn_cockpit.web.retry_watcher import RetryWatcher

router = APIRouter(prefix="/api/plans", tags=["plans"])


# ---------------------------------------------------------------------------
# POST /api/plans/route
# ---------------------------------------------------------------------------


@router.post(
    "/route",
    response_model=PlanResponse,
    status_code=status.HTTP_201_CREATED,
)
def plan_route(
    payload: RoutePlanRequest,
    session: Session = Depends(require_session),
) -> PlanResponse:
    """Erzeugt einen Plan fuer eine neue statische Route auf den gewaehlten Geraeten."""
    require_plan_role(session)
    require_device_ids_accessible(
        payload.target_device_ids, session.opened.data.devices, session,
    )
    # Plausibilitaetspruefung vorab — User soll Fehler im Modal sehen,
    # nicht erst beim Apply gegen die OPNsense.
    try:
        parse_cidr(payload.network)
        validate_gateway_name(payload.gateway)
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    devices = _devices_or_404(session, payload.target_device_ids)
    spec = RouteSpec(
        network=payload.network,
        gateway=payload.gateway,
        descr=payload.descr,
        disabled=payload.disabled,
    )
    plan = _generate_and_save_plan(
        session=session,
        action="add_route",
        subsystem="routes",
        spec=spec,
        devices=devices,
    )
    return _plan_to_response(plan)


# ---------------------------------------------------------------------------
# POST /api/plans/route-update  - Route-Edit
# ---------------------------------------------------------------------------


@router.post(
    "/route-update",
    response_model=PlanResponse,
    status_code=status.HTTP_201_CREATED,
)
def plan_route_update(
    payload: RouteUpdatePlanRequest,
    session: Session = Depends(require_session),
) -> PlanResponse:
    """Erzeugt einen Plan fuer einen Route-Edit auf den gewaehlten Geraeten.

    Identitaet (network + gateway) bleibt erhalten - der Edit aendert
    descr/disabled. Wenn die Route nicht existiert, schlaegt der Apply
    fehl (Diff zeigt den Hinweis schon in der Preview).
    """
    require_plan_role(session)
    require_device_ids_accessible(
        payload.target_device_ids, session.opened.data.devices, session,
    )
    try:
        parse_cidr(payload.network)
        validate_gateway_name(payload.gateway)
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    devices = _devices_or_404(session, payload.target_device_ids)
    spec = RouteSpec(
        network=payload.network,
        gateway=payload.gateway,
        descr=payload.descr,
        disabled=payload.disabled,
    )
    plan = _generate_and_save_plan(
        session=session,
        action="update_route",
        subsystem="routes",
        spec=spec,
        devices=devices,
        action_kind=ActionKind.UPDATE,
    )
    return _plan_to_response(plan)


# ---------------------------------------------------------------------------
# POST /api/plans/route-delete  - Route entfernen
# ---------------------------------------------------------------------------


@router.post(
    "/route-delete",
    response_model=PlanResponse,
    status_code=status.HTTP_201_CREATED,
)
def plan_route_delete(
    payload: RouteDeletePlanRequest,
    session: Session = Depends(require_session),
) -> PlanResponse:
    """Erzeugt einen Plan fuer Route-Delete auf den gewaehlten Geraeten.

    Idempotent: Geraete, auf denen die Route schon weg ist, werden als
    SKIP gefuehrt und beim Apply nicht angefasst.
    """
    require_plan_role(session)
    require_device_ids_accessible(
        payload.target_device_ids, session.opened.data.devices, session,
    )
    try:
        parse_cidr(payload.network)
        validate_gateway_name(payload.gateway)
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    devices = _devices_or_404(session, payload.target_device_ids)
    spec = RouteSpec(
        network=payload.network,
        gateway=payload.gateway,
        descr="",
        disabled=False,
    )
    plan = _generate_and_save_plan(
        session=session,
        action="delete_route",
        subsystem="routes",
        spec=spec,
        devices=devices,
        action_kind=ActionKind.DELETE,
    )
    return _plan_to_response(plan)


# ---------------------------------------------------------------------------
# Firewall-Regeln (Filter-Subsystem)
# ---------------------------------------------------------------------------


def _rule_spec_from_request(payload: object) -> RuleSpec:
    # Duck-typed - akzeptiert sowohl RulePlanRequest (ohne uuid) als auch
    # RuleUpdatePlanRequest (mit uuid).
    return RuleSpec(
        uuid=getattr(payload, "uuid", "") or "",
        enabled=payload.enabled,
        action=payload.action,
        interface=payload.interface,
        direction=payload.direction,
        ipprotocol=payload.ipprotocol,
        protocol=payload.protocol,
        source_net=payload.source_net,
        source_port=payload.source_port,
        source_not=payload.source_not,
        destination_net=payload.destination_net,
        destination_port=payload.destination_port,
        destination_not=payload.destination_not,
        gateway=payload.gateway,
        log=payload.log,
        description=payload.description,
        sequence=payload.sequence,
    )


@router.post(
    "/rule",
    response_model=PlanResponse,
    status_code=status.HTTP_201_CREATED,
)
def plan_rule(
    payload: RulePlanRequest,
    session: Session = Depends(require_session),
) -> PlanResponse:
    """Erzeugt einen Plan fuer eine neue Filter-Regel auf den Ziel-Geraeten.

    Anders als bei Aliases/Routes hat Cockpit hier keinen User-Schluessel
    fuer Idempotenz - ein zweiter Klick erzeugt eine zweite Regel. Der
    User sieht das sofort in der Live-Liste.
    """
    require_plan_role(session)
    require_device_ids_accessible(
        payload.target_device_ids, session.opened.data.devices, session,
    )
    devices = _devices_or_404(session, payload.target_device_ids)
    spec = _rule_spec_from_request(payload)
    plan = _generate_and_save_plan(
        session=session,
        action="add_rule",
        subsystem="firewall_rules",
        spec=spec,
        devices=devices,
        action_kind=ActionKind.ADD,
    )
    return _plan_to_response(plan)


@router.post(
    "/rule-update",
    response_model=PlanResponse,
    status_code=status.HTTP_201_CREATED,
)
def plan_rule_update(
    payload: RuleUpdatePlanRequest,
    session: Session = Depends(require_session),
) -> PlanResponse:
    """Erzeugt einen Plan fuer einen Filter-Regel-Edit. UUID ist Pflicht."""
    require_plan_role(session)
    require_device_ids_accessible(
        payload.target_device_ids, session.opened.data.devices, session,
    )
    devices = _devices_or_404(session, payload.target_device_ids)
    spec = _rule_spec_from_request(payload)
    plan = _generate_and_save_plan(
        session=session,
        action="update_rule",
        subsystem="firewall_rules",
        spec=spec,
        devices=devices,
        action_kind=ActionKind.UPDATE,
    )
    return _plan_to_response(plan)


@router.post(
    "/rule-delete",
    response_model=PlanResponse,
    status_code=status.HTTP_201_CREATED,
)
def plan_rule_delete(
    payload: RuleDeletePlanRequest,
    session: Session = Depends(require_session),
) -> PlanResponse:
    """Erzeugt einen Plan fuer Filter-Regel-Delete. UUID ist Pflicht."""
    require_plan_role(session)
    require_device_ids_accessible(
        payload.target_device_ids, session.opened.data.devices, session,
    )
    devices = _devices_or_404(session, payload.target_device_ids)
    # Minimal-Spec - der Executor ruft adapter.delete(ident) und ignoriert
    # alles ausser der UUID. Interface ist Pflicht im Pydantic-Modell, hier
    # nicht relevant, deshalb Dummy "lan".
    spec = RuleSpec(uuid=payload.uuid, interface="lan")
    plan = _generate_and_save_plan(
        session=session,
        action="delete_rule",
        subsystem="firewall_rules",
        spec=spec,
        devices=devices,
        action_kind=ActionKind.DELETE,
    )
    return _plan_to_response(plan)


# ---------------------------------------------------------------------------
# Unbound-DNS Host-Overrides
# ---------------------------------------------------------------------------


def _unbound_spec(payload: object) -> UnboundHostSpec:
    return UnboundHostSpec(
        host=payload.host,            # type: ignore[attr-defined]
        domain=payload.domain,        # type: ignore[attr-defined]
        server=getattr(payload, "server", ""),
        description=getattr(payload, "description", ""),
        enabled=getattr(payload, "enabled", True),
    )


@router.post(
    "/unbound-host",
    response_model=PlanResponse,
    status_code=status.HTTP_201_CREATED,
)
def plan_unbound_host(
    payload: UnboundHostPlanRequest,
    session: Session = Depends(require_session),
) -> PlanResponse:
    """Plan: neuer Unbound-Host-Override auf den gewaehlten Geraeten."""
    require_plan_role(session)
    require_device_ids_accessible(
        payload.target_device_ids, session.opened.data.devices, session,
    )
    devices = _devices_or_404(session, payload.target_device_ids)
    plan = _generate_and_save_plan(
        session=session,
        action="add_unbound_host",
        subsystem="unbound_hosts",
        spec=_unbound_spec(payload),
        devices=devices,
        action_kind=ActionKind.ADD,
    )
    return _plan_to_response(plan)


@router.post(
    "/unbound-host-update",
    response_model=PlanResponse,
    status_code=status.HTTP_201_CREATED,
)
def plan_unbound_host_update(
    payload: UnboundHostUpdatePlanRequest,
    session: Session = Depends(require_session),
) -> PlanResponse:
    """Plan: Unbound-Host-Override-Edit. Identitaet = (host, domain)."""
    require_plan_role(session)
    require_device_ids_accessible(
        payload.target_device_ids, session.opened.data.devices, session,
    )
    devices = _devices_or_404(session, payload.target_device_ids)
    plan = _generate_and_save_plan(
        session=session,
        action="update_unbound_host",
        subsystem="unbound_hosts",
        spec=_unbound_spec(payload),
        devices=devices,
        action_kind=ActionKind.UPDATE,
    )
    return _plan_to_response(plan)


@router.post(
    "/unbound-host-delete",
    response_model=PlanResponse,
    status_code=status.HTTP_201_CREATED,
)
def plan_unbound_host_delete(
    payload: UnboundHostDeletePlanRequest,
    session: Session = Depends(require_session),
) -> PlanResponse:
    """Plan: Unbound-Host-Override-Delete. Identitaet = (host, domain)."""
    require_plan_role(session)
    require_device_ids_accessible(
        payload.target_device_ids, session.opened.data.devices, session,
    )
    devices = _devices_or_404(session, payload.target_device_ids)
    spec = UnboundHostSpec(
        host=payload.host, domain=payload.domain, server="placeholder",
    )
    plan = _generate_and_save_plan(
        session=session,
        action="delete_unbound_host",
        subsystem="unbound_hosts",
        spec=spec,
        devices=devices,
        action_kind=ActionKind.DELETE,
    )
    return _plan_to_response(plan)


# ---------------------------------------------------------------------------
# POST /api/plans/alias
# ---------------------------------------------------------------------------


@router.post(
    "/alias",
    response_model=PlanResponse,
    status_code=status.HTTP_201_CREATED,
)
def plan_alias(
    payload: AliasPlanRequest,
    session: Session = Depends(require_session),
) -> PlanResponse:
    """Erzeugt einen Plan fuer einen Alias (create oder append)."""
    require_plan_role(session)
    require_device_ids_accessible(
        payload.target_device_ids, session.opened.data.devices, session,
    )
    # Plausibilitaetspruefung: Name + Typ + typabhaengige Content-Validierung.
    try:
        validate_alias_name(payload.name)
        validate_alias_type(payload.type)
        cleaned_content = validate_alias_content(payload.type, payload.content)
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    devices = _devices_or_404(session, payload.target_device_ids)
    spec = AliasSpec(
        name=payload.name,
        type=payload.type,
        content=tuple(cleaned_content),
        descr=payload.descr,
        merge_mode=payload.merge_mode,  # type: ignore[arg-type]
    )
    action_name = "append_alias" if payload.merge_mode == "append" else "add_alias"
    plan = _generate_and_save_plan(
        session=session,
        action=action_name,
        subsystem="firewall_alias",
        spec=spec,
        devices=devices,
    )
    return _plan_to_response(plan)


# ---------------------------------------------------------------------------
# POST /api/plans/alias-update  - Alias-Edit (bestehenden Alias modifizieren)
# ---------------------------------------------------------------------------


@router.post(
    "/alias-update",
    response_model=PlanResponse,
    status_code=status.HTTP_201_CREATED,
)
def plan_alias_update(
    payload: AliasUpdatePlanRequest,
    session: Session = Depends(require_session),
) -> PlanResponse:
    """Erzeugt einen Plan fuer einen Alias-Edit auf den gewaehlten Geraeten.

    Anders als ``/alias`` legt das hier KEINEN neuen Alias an, sondern ersetzt
    Typ + Inhalt + Beschreibung eines bestehenden Alias. Wenn der Alias auf
    einem Geraet nicht existiert, fliegt der Apply mit "Alias existiert
    nicht" - der Diff zeigt das schon in der Preview an.
    """
    require_plan_role(session)
    require_device_ids_accessible(
        payload.target_device_ids, session.opened.data.devices, session,
    )
    try:
        validate_alias_name(payload.name)
        validate_alias_type(payload.type)
        cleaned_content = validate_alias_content(payload.type, payload.content)
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    devices = _devices_or_404(session, payload.target_device_ids)
    spec = AliasSpec(
        name=payload.name,
        type=payload.type,
        content=tuple(cleaned_content),
        descr=payload.descr,
        merge_mode="create",
    )
    plan = _generate_and_save_plan(
        session=session,
        action="update_alias",
        subsystem="firewall_alias",
        spec=spec,
        devices=devices,
        action_kind=ActionKind.UPDATE,
    )
    return _plan_to_response(plan)


# ---------------------------------------------------------------------------
# POST /api/plans/alias-delete  - Alias entfernen
# ---------------------------------------------------------------------------


@router.post(
    "/alias-delete",
    response_model=PlanResponse,
    status_code=status.HTTP_201_CREATED,
)
def plan_alias_delete(
    payload: AliasDeletePlanRequest,
    session: Session = Depends(require_session),
) -> PlanResponse:
    """Erzeugt einen Plan fuer Alias-Delete auf den gewaehlten Geraeten.

    Idempotent: Geraete, auf denen der Alias schon weg ist, werden im Plan
    als SKIP gefuehrt und beim Apply nicht angefasst.
    """
    require_plan_role(session)
    require_device_ids_accessible(
        payload.target_device_ids, session.opened.data.devices, session,
    )
    try:
        validate_alias_name(payload.name)
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    devices = _devices_or_404(session, payload.target_device_ids)
    # Spec mit minimalem Inhalt - der Executor ruft adapter.delete(ident)
    # und ignoriert content/type. content muss nicht-leer sein damit
    # AliasSpec konstruierbar bleibt, der Wert spielt aber keine Rolle.
    spec = AliasSpec(
        name=payload.name,
        type="host",
        content=("placeholder",),
        descr="",
        merge_mode="create",
    )
    plan = _generate_and_save_plan(
        session=session,
        action="delete_alias",
        subsystem="firewall_alias",
        spec=spec,
        devices=devices,
        action_kind=ActionKind.DELETE,
    )
    return _plan_to_response(plan)


# ---------------------------------------------------------------------------
# GET /api/plans
# ---------------------------------------------------------------------------


@router.get("", response_model=PlanListResponse)
def list_plans(session: Session = Depends(require_session)) -> PlanListResponse:
    """Listet alle Plaene, deren Geraete fuer den User sichtbar sind."""
    session.touch()
    store = _plan_store()
    summaries: list[PlanSummary] = []
    visible_ids = {
        d.id for d in session.opened.data.devices if device_visible_to(d, session)
    }
    for plan_id in store.list_ids():
        try:
            plan = store.load(plan_id)
        except PlanStoreError:
            continue
        plan_device_ids = {a.device.id for a in plan.actions}
        if plan_device_ids and not plan_device_ids.issubset(visible_ids):
            # Plan beruehrt Geraete ausserhalb des User-Scopes — verstecken.
            continue
        summaries.append(
            PlanSummary(
                plan_id=plan.plan_id,
                action=plan.action,
                subsystem=plan.subsystem,
                created_at_utc=plan.created_at_utc,
                target_count=plan.target_count,
            )
        )
    return PlanListResponse(plans=summaries)


# ---------------------------------------------------------------------------
# GET /api/plans/outstanding (MUSS vor /{plan_id} stehen — sonst frisst FastAPI das)
# ---------------------------------------------------------------------------


@router.get("/outstanding", response_model=OutstandingResponse)
def outstanding(session: Session = Depends(require_session)) -> OutstandingResponse:
    """Aggregiert pro sichtbarem Geraet die offenen Plaene."""
    session.touch()
    store = _plan_store()
    # device_id -> (count, [plan_ids in load-order], name)
    counts: dict[str, int] = {}
    plan_lists: dict[str, list[str]] = {}
    name_lookup = {d.id: d.name for d in session.opened.data.devices}
    visible_ids = {
        d.id for d in session.opened.data.devices if device_visible_to(d, session)
    }

    for plan_id in store.list_ids():
        try:
            plan = store.load(plan_id)
        except PlanStoreError:
            continue
        plan_devices = {a.device.id for a in plan.actions}
        if not plan_devices:
            continue
        try:
            report = store.load_report(plan_id)
        except PlanStoreError:
            report = None
        if report is None:
            success_ids: set[str] = set()
        else:
            success_ids = {
                r.device_id for r in report.results
                if r.status in {Status.VERIFIED, Status.SKIPPED}
            }
        for did in plan_devices:
            if did in success_ids:
                continue
            if did not in visible_ids:
                continue  # Outstanding nur fuer sichtbare Geraete melden
            counts[did] = counts.get(did, 0) + 1
            plan_lists.setdefault(did, []).append(plan_id)

    entries = [
        OutstandingDeviceEntry(
            device_id=did,
            device_name=name_lookup.get(did, did),
            outstanding_count=counts[did],
            plans=list(reversed(plan_lists[did])),
        )
        for did in counts
    ]
    entries.sort(key=lambda e: (-e.outstanding_count, e.device_name.lower()))
    return OutstandingResponse(devices=entries)


# ---------------------------------------------------------------------------
# GET /api/plans/{plan_id}
# ---------------------------------------------------------------------------


@router.get("/{plan_id}", response_model=PlanResponse)
def get_plan(
    plan_id: str,
    session: Session = Depends(require_session),
) -> PlanResponse:
    """Liefert die Vorschau eines Plans — nur wenn alle Geraete sichtbar sind."""
    session.touch()
    plan = _load_plan_or_404(plan_id)
    # Plan-Devices muessen alle im User-Scope liegen, sonst 404 (Existenz nicht
    # verraten).
    visible_ids = {
        d.id for d in session.opened.data.devices if device_visible_to(d, session)
    }
    plan_device_ids = {a.device.id for a in plan.actions}
    if plan_device_ids and not plan_device_ids.issubset(visible_ids):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Plan '{plan_id}' nicht gefunden.",
        )
    return _plan_to_response(plan)


# ---------------------------------------------------------------------------
# DELETE /api/plans/{plan_id}
# ---------------------------------------------------------------------------


@router.delete("/{plan_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_plan(
    plan_id: str,
    session: Session = Depends(require_session),
) -> None:
    """Entfernt einen gespeicherten Plan (z. B. wenn der User abbricht)."""
    require_plan_role(session)
    session.touch()
    store = _plan_store()
    try:
        existed = store.delete(plan_id)
    except PlanStoreError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Plan konnte nicht entfernt werden: {exc}",
        ) from exc
    if not existed:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Plan '{plan_id}' nicht gefunden.",
        )


# ---------------------------------------------------------------------------
# POST /api/plans/{plan_id}/apply
# ---------------------------------------------------------------------------


@router.post("/{plan_id}/apply", response_model=RolloutReportResponse)
def apply_plan(
    plan_id: str,
    request: Request,
    payload: ApplyRequest | None = None,
    pair: tuple[Session, str] = Depends(require_session_with_token),
) -> RolloutReportResponse:
    """Rollt einen Plan aus. Optional nur fuer eine Untermenge der Geraete.

    ``payload.device_ids`` ist der Retry-Pfad: User hat den Plan vorhin
    schon einmal angewandt, einige Geraete sind fehlgeschlagen, und er
    will nur die fehlgeschlagenen nachziehen. Wenn ``device_ids`` leer
    oder ``None`` ist, wird der Plan auf allen seinen Geraeten ausgerollt.

    Mobile-Rack-Workflow: wenn ``auto_retry_enabled`` im Tresor an ist und
    Geraete nach dem Apply als FAILED zurueckkommen (typisch: offline,
    Timeout), werden sie automatisch in den RetryWatcher eingequeued.
    Sobald sie wieder erreichbar sind, zieht der Watcher den Apply nach -
    ohne dass der User nochmal klicken muss. Der Job laeuft bis zur
    konfigurierten Max-Dauer (default 7 Tage) oder bis er Erfolg hat.
    """
    session, token = pair
    require_plan_role(session)
    device_ids = payload.device_ids if payload is not None else None
    plan = _load_plan_or_404(plan_id)
    target_ids = device_ids if device_ids else [a.device.id for a in plan.actions]
    require_device_ids_accessible(
        target_ids, session.opened.data.devices, session,
    )
    try:
        plan, full_report = run_apply(session, plan_id, device_ids=device_ids)
    except PlanStoreError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except _NoMatchingDevices as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    # Auto-Retry-Queue fuer Failed-Devices (v0.7 Mobile-Rack-Feature).
    _auto_arm_retry_for_failures(
        request=request, session=session, token=token,
        plan_id=plan_id, report=full_report,
    )

    return _report_to_response(plan, full_report)


def _auto_arm_retry_for_failures(
    *,
    request: Request,
    session: Session,
    token: str,
    plan_id: str,
    report: RolloutReport,
) -> None:
    """Schedules RetryWatcher-Job fuer alle FAILED-Geraete des Reports.

    Best-Effort: wenn der Watcher nicht initialisiert ist oder Settings
    auto_retry_enabled=False sagen, bleibt der Apply unveraendert. Greift
    auch nicht wenn der User aktuell schon einen Retry-Filter angewandt
    hat - dann ist das ein expliziter Retry-Klick und kein Bedarf fuer
    Auto-Watcher.
    """
    settings = session.opened.data.settings
    if not settings.auto_retry_enabled:
        return
    watcher = getattr(request.app.state, "retry_watcher", None)
    if not isinstance(watcher, RetryWatcher):
        return
    failed_ids = [
        r.device_id for r in report.results
        if r.status == Status.FAILED
    ]
    if not failed_ids:
        return
    interval_s = max(60, settings.auto_retry_interval_minutes * 60)
    max_duration_s = max(3600, settings.auto_retry_max_hours * 3600)
    vault_path = session.vault_path
    watcher.schedule(
        plan_id=plan_id,
        session_token=token,
        vault_path=str(vault_path) if vault_path is not None else "",
        device_ids=failed_ids,
        interval_s=interval_s,
        max_duration_s=max_duration_s,
    )


# ---------------------------------------------------------------------------
# Helfer fuer Apply: extrahiert, damit der RetryWatcher das gleiche aufruft
# ---------------------------------------------------------------------------


class _NoMatchingDevices(ValueError):
    """Filter lieferte keine Aktionen — z. B. weil alle device_ids unbekannt sind."""


def run_apply(
    session: Session,
    plan_id: str,
    *,
    device_ids: list[str] | None = None,
) -> tuple[Plan, RolloutReport]:
    """Wendet ``plan_id`` an, optional gefiltert auf ``device_ids``.

    Liefert ``(applied_plan, merged_report)``. Der gemergte Report wird
    bereits persistiert. Wird von ``apply_plan`` (HTTP-Endpoint) und vom
    Auto-Retry-Watcher gleichermassen aufgerufen.

    Wirft ``PlanStoreError`` wenn der Plan nicht existiert, und
    ``_NoMatchingDevices`` wenn ein device_ids-Filter alle Aktionen rausfiltert.
    """
    store = _plan_store()
    plan = store.load(plan_id)
    if device_ids:
        plan = _filter_plan_by_devices(plan, device_ids)
        if not plan.actions:
            raise _NoMatchingDevices(
                "Keine der angegebenen Geraete-IDs ist im Plan.",
            )
    binding = get_binding(plan.subsystem)
    devices_in_plan = [a.device for a in plan.actions]
    targets = [
        HttpTarget(host=d.host, port=d.port, verify=d.tls_verify)
        for d in devices_in_plan
    ]
    tuning = _tuning(session)
    audit = get_audit_backend()
    executor = Executor(
        session=session,
        audit=audit,
        max_workers=session.opened.data.settings.max_workers,
    )
    with HttpClient(targets=targets, tuning=tuning) as client:
        report = executor.apply(
            plan,
            adapter=binding.adapter,
            controller=binding.controller,
            client=client,
        )

    if device_ids:
        previous = store.load_report(plan_id)
        if previous is not None:
            report = _merge_reports(previous, report)
    store.save_report(plan_id, report)
    return plan, report


def _report_to_response(plan: Plan, report: RolloutReport) -> RolloutReportResponse:
    device_names = {a.device.id: a.device.name for a in plan.actions}
    results = [
        DeviceResultResponse(
            device_id=r.device_id,
            device_name=device_names.get(r.device_id, r.device_id),
            status=str(r.status),
            short_message=r.short_message,
            error_kind=r.error_kind,
            failed_phase=str(r.failed_phase) if r.failed_phase else None,
            duration_ms=r.duration_ms,
        )
        for r in report.results
    ]
    return RolloutReportResponse(
        plan_id=plan.plan_id,
        action=plan.action,
        subsystem=plan.subsystem,
        total=report.total,
        successes=report.successes,
        failures=report.failures,
        skipped=report.skipped,
        results=results,
    )


# ---------------------------------------------------------------------------
# Helfer
# ---------------------------------------------------------------------------


def _plan_store() -> PlanStoreBackend:
    return get_plan_store_backend()


def _tuning(session: Session) -> HttpTuning:
    s = session.opened.data.settings
    return HttpTuning(
        connect_timeout_s=s.connect_timeout_s,
        read_timeout_s=s.read_timeout_s,
        reconfigure_timeout_s=s.reconfigure_timeout_s,
        retry_count=s.retry_count,
    )


def _devices_or_404(session: Session, target_ids: list[str]) -> list[Device]:
    by_id = {d.id: d for d in session.opened.data.devices}
    devices: list[Device] = []
    missing: list[str] = []
    for tid in target_ids:
        vd = by_id.get(tid)
        if vd is None:
            missing.append(tid)
        else:
            devices.append(Device.from_vault_device(vd))
    if missing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unbekannte Geräte-ID(s): {', '.join(missing)}",
        )
    if not devices:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Mindestens ein Gerät erforderlich.",
        )
    return devices


def _generate_and_save_plan(
    *,
    session: Session,
    action: str,
    subsystem: str,
    spec: object,
    devices: list[Device],
    action_kind: ActionKind = ActionKind.ADD,
) -> Plan:
    binding = get_binding(subsystem)
    tuning = _tuning(session)
    targets = [
        HttpTarget(host=d.host, port=d.port, verify=d.tls_verify) for d in devices
    ]
    audit = get_audit_backend()
    planner = Planner(
        audit=audit,
        session=session,
        max_workers=session.opened.data.settings.max_workers,
    )
    with HttpClient(targets=targets, tuning=tuning) as client:
        plan = planner.create_plan(
            action=action,
            spec=spec,
            devices=devices,
            adapter=binding.adapter,
            client=client,
            action_kind=action_kind,
        )
    _plan_store().save(plan)
    return plan


def _load_plan_or_404(plan_id: str) -> Plan:
    try:
        return _plan_store().load(plan_id)
    except PlanStoreError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc


def _filter_plan_by_devices(plan: Plan, device_ids: list[str]) -> Plan:
    """Liefert einen Plan, der nur Aktionen fuer die genannten Geraete enthaelt."""
    wanted = set(device_ids)
    filtered: list[PlannedDeviceAction] = [
        a for a in plan.actions if a.device.id in wanted
    ]
    return Plan(
        plan_id=plan.plan_id,
        action=plan.action,
        subsystem=plan.subsystem,
        created_at_utc=plan.created_at_utc,
        actions=tuple(filtered),
    )


def _merge_reports(previous: RolloutReport, current: RolloutReport) -> RolloutReport:
    """Mischt vorigen Report mit dem aktuellen — current ueberschreibt previous.

    Verwendet beim Retry: alte Erfolge bleiben drin, fuer die retryten
    Geraete kommt das neue Resultat.
    """
    by_device = {r.device_id: r for r in previous.results}
    for r in current.results:
        by_device[r.device_id] = r
    return RolloutReport(results=tuple(by_device.values()))


def _plan_to_response(plan: Plan) -> PlanResponse:
    return PlanResponse(
        plan_id=plan.plan_id,
        action=plan.action,
        subsystem=plan.subsystem,
        created_at_utc=plan.created_at_utc,
        target_count=plan.target_count,
        to_apply_count=plan.to_apply_count,
        skip_count=plan.skip_count,
        actions=[
            PlannedActionResponse(
                device_id=a.device.id,
                device_name=a.device.name,
                device_host=a.device.host,
                diff_kind=str(a.diff.kind),
                diff_summary=a.diff.summary,
                payload_masked=a.payload_masked,
            )
            for a in plan.actions
        ],
    )


__all__ = ["router"]
