"""Firmware-Rollout-Endpoints (Iteration B).

Drei Endpoints fuer die sequenzielle Sammelaktion pro Tag-Gruppe:

* ``POST   /api/firmware/rollout``        - startet einen Rollout
* ``GET    /api/firmware/rollout``        - aktueller Zustand fuer Banner-Poll
* ``POST   /api/firmware/rollout/cancel`` - laufenden Rollout abbrechen

Der Watcher (siehe ``web/firmware_rollout_watcher.py``) uebernimmt die
State-Machine im Hintergrund. Diese Endpoints sind duenne HTTP-Shells.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status

from opn_cockpit.audit.backend import audit_actor
from opn_cockpit.security.session import Session
from opn_cockpit.web.acl import (
    require_device_ids_accessible,
    require_write_role,
)
from opn_cockpit.web.api.schemas import (
    FirmwareRolloutDeviceEntry,
    FirmwareRolloutRequest,
    FirmwareRolloutResponse,
)
from opn_cockpit.web.auth.dependencies import require_session
from opn_cockpit.web.firmware_rollout_watcher import (
    DEV_STATE_DONE,
    DEV_STATE_FAILED,
    DEV_STATE_SKIPPED,
    FirmwareRolloutWatcher,
    RolloutBusyError,
)

router = APIRouter(prefix="/api/firmware", tags=["firmware"])


def _get_watcher(request: Request) -> FirmwareRolloutWatcher:
    watcher = getattr(request.app.state, "firmware_rollout_watcher", None)
    if not isinstance(watcher, FirmwareRolloutWatcher):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Firmware-Rollout-Watcher nicht verfuegbar.",
        )
    return watcher


def _stats_to_response(watcher: FirmwareRolloutWatcher) -> FirmwareRolloutResponse:
    rollout = watcher.stats()
    if rollout is None:
        return FirmwareRolloutResponse(active=False)
    return FirmwareRolloutResponse(
        active=True,
        rollout_id=rollout.rollout_id,
        state=rollout.state,
        mode=rollout.mode,
        initiator=rollout.initiator,
        continue_on_error=rollout.continue_on_error,
        cancel_requested=rollout.cancel_requested,
        created_at_ms=rollout.created_at_ms,
        finished_at_ms=rollout.finished_at_ms,
        devices=[
            FirmwareRolloutDeviceEntry(
                device_id=d.device_id,
                device_name=d.device_name,
                position=d.position,
                state=d.state,
                version_before=d.version_before,
                target_version=d.target_version,
                version_after=d.version_after,
                started_at_ms=d.started_at_ms,
                finished_at_ms=d.finished_at_ms,
                log=d.log,
                summary=d.summary,
            )
            for d in rollout.devices
        ],
        total=len(rollout.devices),
        done_count=sum(1 for d in rollout.devices if d.state == DEV_STATE_DONE),
        failed_count=sum(1 for d in rollout.devices if d.state == DEV_STATE_FAILED),
        skipped_count=sum(1 for d in rollout.devices if d.state == DEV_STATE_SKIPPED),
    )


# ---------------------------------------------------------------------------
# POST /api/firmware/rollout
# ---------------------------------------------------------------------------


@router.post(
    "/rollout",
    response_model=FirmwareRolloutResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def start_rollout(
    payload: FirmwareRolloutRequest,
    request: Request,
    session: Session = Depends(require_session),
) -> FirmwareRolloutResponse:
    """Startet eine sequenzielle Firmware-Sammelaktion.

    Die genannten Geraete werden **eine nach der anderen** abgearbeitet.
    Bei ``mode="upgrade"`` (Major-Release) wartet der Watcher pro Box
    einen Reboot ab (bis 15 min Toleranz) bevor die naechste dran ist.
    ``continue_on_error=false`` (Default) laesst den Rollout beim ersten
    Fehler stoppen.
    """
    require_write_role(session)
    require_device_ids_accessible(
        payload.device_ids, session.opened.data.devices, session,
    )

    devices_by_id = {d.id: d for d in session.opened.data.devices}
    ordered: list[tuple[str, str, str]] = []
    missing: list[str] = []
    for did in payload.device_ids:
        vd = devices_by_id.get(did)
        if vd is None:
            missing.append(did)
            continue
        # target_version bleibt leer - der Watcher zieht sie beim Trigger
        # aus /firmware/status.
        ordered.append((vd.id, vd.name, ""))
    if missing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unbekannte Geraete-ID(s): {', '.join(missing)}",
        )

    vault_path = ""
    opened = getattr(session, "opened", None)
    if opened is not None:
        vp = getattr(opened, "vault_path", None)
        if vp is not None:
            vault_path = str(vp)

    watcher = _get_watcher(request)
    try:
        watcher.submit(
            initiator=audit_actor(session),
            vault_path=vault_path,
            devices=ordered,
            mode=payload.mode,
            continue_on_error=payload.continue_on_error,
        )
    except RolloutBusyError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    session.touch()
    return _stats_to_response(watcher)


# ---------------------------------------------------------------------------
# GET /api/firmware/rollout
# ---------------------------------------------------------------------------


@router.get(
    "/rollout",
    response_model=FirmwareRolloutResponse,
)
def get_rollout(
    request: Request,
    session: Session = Depends(require_session),
) -> FirmwareRolloutResponse:
    """Aktueller Rollout-Zustand fuer den UI-Banner-Poll."""
    session.touch()
    watcher = _get_watcher(request)
    return _stats_to_response(watcher)


# ---------------------------------------------------------------------------
# POST /api/firmware/rollout/cancel
# ---------------------------------------------------------------------------


@router.post(
    "/rollout/cancel",
    response_model=FirmwareRolloutResponse,
)
def cancel_rollout(
    request: Request,
    session: Session = Depends(require_session),
) -> FirmwareRolloutResponse:
    """Bricht den laufenden Rollout ab.

    Die aktuell aktive Box wird zu Ende gefahren (kein hartes Kill),
    danach markiert der Watcher den Rollout als ``cancelled``. Noch nicht
    gestartete Boxen werden auf ``skipped`` gesetzt.
    """
    require_write_role(session)
    watcher = _get_watcher(request)
    initiator = audit_actor(session)
    if not watcher.cancel(initiator=initiator):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Kein aktiver Rollout zum Abbrechen.",
        )
    session.touch()
    return _stats_to_response(watcher)


# ---------------------------------------------------------------------------
# DELETE /api/firmware/rollout - terminierten Rollout aus der Anzeige raus
# ---------------------------------------------------------------------------


@router.delete(
    "/rollout",
    response_model=FirmwareRolloutResponse,
)
def clear_rollout(
    request: Request,
    session: Session = Depends(require_session),
) -> FirmwareRolloutResponse:
    """Entfernt einen terminierten Rollout aus dem Banner-State.

    Nur erlaubt wenn der Rollout in ``done``/``failed``/``cancelled`` ist.
    Der Banner verschwindet danach im UI.
    """
    require_write_role(session)
    watcher = _get_watcher(request)
    if not watcher.clear_terminal():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Rollout laeuft noch - bitte erst abbrechen (POST "
                "/api/firmware/rollout/cancel) oder auf Fertigstellung warten."
            ),
        )
    session.touch()
    return _stats_to_response(watcher)


__all__ = ["router"]
