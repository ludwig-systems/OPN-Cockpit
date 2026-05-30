"""Plan/Apply-Routen: Routen + Aliase via Web-UI.

Wraps die bestehende orchestration-Schicht (Planner + Executor + PlanStore).
Der Plan-Store ist derselbe wie bei der CLI (``%APPDATA%/OPN-Cockpit/plans/``)
— Web und CLI teilen sich also die Pläne, was Cross-Tooling erlaubt.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from opn_cockpit.audit.backend import get_audit_backend
from opn_cockpit.core.http_client import HttpClient, HttpTarget, HttpTuning
from opn_cockpit.core.objects.aliases import AliasSpec
from opn_cockpit.core.objects.routes import RouteSpec
from opn_cockpit.core.result import RolloutReport, Status
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
    AliasPlanRequest,
    ApplyRequest,
    DeviceResultResponse,
    OutstandingDeviceEntry,
    OutstandingResponse,
    PlanListResponse,
    PlannedActionResponse,
    PlanResponse,
    PlanSummary,
    RolloutReportResponse,
    RoutePlanRequest,
)
from opn_cockpit.web.auth.dependencies import require_session

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
    devices = _devices_or_404(session, payload.target_device_ids)
    spec = AliasSpec(
        name=payload.name,
        type=payload.type,
        content=tuple(c.strip() for c in payload.content if c.strip()),
        descr=payload.descr,
        merge_mode=payload.merge_mode,  # type: ignore[arg-type]
    )
    if not spec.content:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Mindestens ein Alias-Eintrag erforderlich.",
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
    payload: ApplyRequest | None = None,
    session: Session = Depends(require_session),
) -> RolloutReportResponse:
    """Rollt einen Plan aus. Optional nur fuer eine Untermenge der Geraete.

    ``payload.device_ids`` ist der Retry-Pfad: User hat den Plan vorhin
    schon einmal angewandt, einige Geraete sind fehlgeschlagen, und er
    will nur die fehlgeschlagenen nachziehen. Wenn ``device_ids`` leer
    oder ``None`` ist, wird der Plan auf allen seinen Geraeten ausgerollt.
    """
    require_plan_role(session)
    device_ids = payload.device_ids if payload is not None else None
    # ACL-Pruefung: alle Plan-Geraete (oder gefilterte device_ids) muessen im
    # User-Scope liegen. Sonst 404 (Existenz nicht verraten).
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
    return _report_to_response(plan, full_report)


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
