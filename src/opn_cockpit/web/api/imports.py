"""Bulk-Import-Routen: CSV-Routen / JSON-Aliase hochladen, parsen und planen.

Der User waehlt eine Datei + Zielgeraete, der Server parsed via dem
bestehenden ``importers``-Modul. Bei Parse-Fehlern: 400 mit Fehlerliste,
kein Plan. Sonst: Bulk-Plan erzeugen, speichern, ``PlanResponse``
zurueck — Frontend kann das in die bekannte Plan-Vorschau-Phase
einkippen.
"""

from __future__ import annotations

import contextlib
import tempfile
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status

from opn_cockpit.audit.log import AuditLog, default_audit_path
from opn_cockpit.config import get_app_data_dir
from opn_cockpit.core.http_client import HttpClient, HttpTarget, HttpTuning
from opn_cockpit.importers.csv_routes import parse_routes_csv
from opn_cockpit.importers.json_aliases import parse_aliases_json
from opn_cockpit.inventory.model import Device
from opn_cockpit.orchestration.plan_store import PlanStore
from opn_cockpit.orchestration.planner import Plan, Planner
from opn_cockpit.orchestration.registry import get_binding
from opn_cockpit.security.session import Session
from opn_cockpit.vault.model import VaultDevice
from opn_cockpit.web.api.schemas import (
    PlannedActionResponse,
    PlanResponse,
)
from opn_cockpit.web.auth.dependencies import require_session

router = APIRouter(prefix="/api/imports", tags=["imports"])

MAX_UPLOAD_BYTES = 2 * 1024 * 1024  # 2 MiB - genug fuer realistische Inventories


@router.post(
    "/routes",
    response_model=PlanResponse,
    status_code=status.HTTP_201_CREATED,
)
async def import_routes(
    file: Annotated[UploadFile, File(description="CSV-Datei mit Routen")],
    target_device_ids: Annotated[
        list[str], Form(description="Geraete-IDs, kommt mehrfach als form-field")
    ],
    session: Session = Depends(require_session),
) -> PlanResponse:
    """Liest die CSV, baut einen Bulk-Plan ueber die uebergebenen Geraete."""
    devices = _devices_or_404(session, target_device_ids)
    tmp_path = await _stage_upload(file, suffix=".csv")
    try:
        result = parse_routes_csv(tmp_path)
    finally:
        with contextlib.suppress(OSError):
            tmp_path.unlink()
    if result.has_errors:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "message": "CSV-Parse-Fehler",
                "errors": result.errors,
                "parsed_count": len(result.specs),
            },
        )
    if not result.specs:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"message": "Keine Routen in der CSV gefunden.", "errors": []},
        )
    plan = _generate_bulk_plan(
        session=session,
        action="bulk_add_route",
        subsystem="routes",
        specs=list(result.specs),
        devices=devices,
    )
    return _plan_to_response(plan)


@router.post(
    "/aliases",
    response_model=PlanResponse,
    status_code=status.HTTP_201_CREATED,
)
async def import_aliases(
    file: Annotated[UploadFile, File(description="JSON-Datei mit Aliasen")],
    target_device_ids: Annotated[list[str], Form()],
    append_mode: Annotated[bool, Form()] = False,
    session: Session = Depends(require_session),
) -> PlanResponse:
    """Liest die JSON-Datei, baut einen Bulk-Plan."""
    devices = _devices_or_404(session, target_device_ids)
    tmp_path = await _stage_upload(file, suffix=".json")
    try:
        result = parse_aliases_json(
            tmp_path,
            override_merge_mode="append" if append_mode else None,
        )
    finally:
        with contextlib.suppress(OSError):
            tmp_path.unlink()
    if result.has_errors:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "message": "JSON-Parse-Fehler",
                "errors": result.errors,
                "parsed_count": len(result.specs),
            },
        )
    if not result.specs:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"message": "Keine Aliase in der JSON gefunden.", "errors": []},
        )
    action = "bulk_append_alias" if append_mode else "bulk_add_alias"
    plan = _generate_bulk_plan(
        session=session,
        action=action,
        subsystem="firewall_alias",
        specs=list(result.specs),
        devices=devices,
    )
    return _plan_to_response(plan)


# ---------------------------------------------------------------------------
# Helfer
# ---------------------------------------------------------------------------


async def _stage_upload(file: UploadFile, *, suffix: str) -> Path:
    raw = await file.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Datei zu gross (>{MAX_UPLOAD_BYTES // 1024} KiB).",
        )
    with tempfile.NamedTemporaryFile(
        mode="wb", suffix=suffix, delete=False,
    ) as tmp:
        tmp.write(raw)
        tmp_name = tmp.name
    return Path(tmp_name)


def _devices_or_404(session: Session, target_ids: list[str]) -> list[Device]:
    by_id: dict[str, VaultDevice] = {d.id: d for d in session.opened.data.devices}
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
            detail=f"Unbekannte Geraete-ID(s): {', '.join(missing)}",
        )
    if not devices:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Mindestens ein Geraet erforderlich.",
        )
    return devices


def _generate_bulk_plan(
    *,
    session: Session,
    action: str,
    subsystem: str,
    specs: list[object],
    devices: list[Device],
) -> Plan:
    binding = get_binding(subsystem)
    s = session.opened.data.settings
    tuning = HttpTuning(
        connect_timeout_s=s.connect_timeout_s,
        read_timeout_s=s.read_timeout_s,
        reconfigure_timeout_s=s.reconfigure_timeout_s,
        retry_count=s.retry_count,
    )
    targets = [HttpTarget(host=d.host, port=d.port, verify=d.tls_verify) for d in devices]
    audit = AuditLog(path=default_audit_path())
    planner = Planner(
        audit=audit, session=session, max_workers=s.max_workers,
    )
    with HttpClient(targets=targets, tuning=tuning) as client:
        plan = planner.create_bulk_plan(
            action=action,
            specs=specs,
            devices=devices,
            adapter=binding.adapter,
            client=client,
        )
    PlanStore(base_dir=get_app_data_dir() / "plans").save(plan)
    return plan


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
