"""Plan/Apply-Routen: Routen + Aliase via Web-UI.

Wraps die bestehende orchestration-Schicht (Planner + Executor + PlanStore).
Der Plan-Store ist derselbe wie bei der CLI (``%APPDATA%/OPN-Cockpit/plans/``)
— Web und CLI teilen sich also die Pläne, was Cross-Tooling erlaubt.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from opn_cockpit.audit.log import AuditLog, default_audit_path
from opn_cockpit.config import get_app_data_dir
from opn_cockpit.core.http_client import HttpClient, HttpTarget, HttpTuning
from opn_cockpit.core.objects.aliases import AliasSpec
from opn_cockpit.core.objects.routes import RouteSpec
from opn_cockpit.inventory.model import Device
from opn_cockpit.orchestration.executor import Executor
from opn_cockpit.orchestration.plan_store import PlanStore, PlanStoreError
from opn_cockpit.orchestration.planner import Plan, Planner
from opn_cockpit.orchestration.registry import get_binding
from opn_cockpit.security.session import Session
from opn_cockpit.web.api.schemas import (
    AliasPlanRequest,
    DeviceResultResponse,
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
    """Listet alle aktuell gespeicherten Pläne."""
    session.touch()
    store = _plan_store()
    summaries: list[PlanSummary] = []
    for plan_id in store.list_ids():
        try:
            plan = store.load(plan_id)
        except PlanStoreError:
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
# GET /api/plans/{plan_id}
# ---------------------------------------------------------------------------


@router.get("/{plan_id}", response_model=PlanResponse)
def get_plan(
    plan_id: str,
    session: Session = Depends(require_session),
) -> PlanResponse:
    """Liefert die vollstaendige Vorschau eines gespeicherten Plans."""
    session.touch()
    plan = _load_plan_or_404(plan_id)
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
    session.touch()
    store = _plan_store()
    try:
        path = store.base_dir / f"{plan_id}.json"
        if not path.exists():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Plan '{plan_id}' nicht gefunden.",
            )
        path.unlink()
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Plan konnte nicht entfernt werden: {exc}",
        ) from exc


# ---------------------------------------------------------------------------
# POST /api/plans/{plan_id}/apply
# ---------------------------------------------------------------------------


@router.post("/{plan_id}/apply", response_model=RolloutReportResponse)
def apply_plan(
    plan_id: str,
    session: Session = Depends(require_session),
) -> RolloutReportResponse:
    """Rollt einen vorher erzeugten Plan aus.

    Lädt den Plan aus dem Store, baut HttpClient + Executor, ruft die
    bestehende Orchestration-Pipeline auf. Beim Apply NICHT den Plan
    löschen — der User kann sich danach das Ergebnis ansehen oder den
    Plan erneut anwenden.
    """
    plan = _load_plan_or_404(plan_id)
    binding = get_binding(plan.subsystem)
    devices_in_plan = [a.device for a in plan.actions]
    targets = [
        HttpTarget(host=d.host, port=d.port, verify=d.tls_verify)
        for d in devices_in_plan
    ]
    tuning = _tuning(session)
    audit = AuditLog(path=default_audit_path())
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

    device_names = {d.id: d.name for d in devices_in_plan}
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


def _plan_store() -> PlanStore:
    return PlanStore(base_dir=get_app_data_dir() / "plans")


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
    audit = AuditLog(path=default_audit_path())
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
