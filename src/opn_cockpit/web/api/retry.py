"""Auto-Retry-Watcher: schedule/cancel/pause/resume + Status."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status

from opn_cockpit.security.session import Session
from opn_cockpit.web.acl import require_device_ids_accessible, require_plan_role
from opn_cockpit.web.api.schemas import (
    RetryJobResponse,
    RetryScheduleRequest,
    RetryStatusResponse,
)
from opn_cockpit.web.auth.dependencies import require_session_with_token
from opn_cockpit.web.retry_watcher import RetryWatcher

router = APIRouter(prefix="/api/retry", tags=["retry"])


def _watcher(request: Request) -> RetryWatcher:
    watcher = getattr(request.app.state, "retry_watcher", None)
    if not isinstance(watcher, RetryWatcher):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="RetryWatcher not initialised",
        )
    return watcher


@router.get("/status", response_model=RetryStatusResponse)
def retry_status(
    request: Request,
    pair: tuple[Session, str] = Depends(require_session_with_token),
) -> RetryStatusResponse:
    """Listet alle Jobs des Watchers — nur die des aufrufenden Tokens.

    Andere Tokens (in einer spaeteren Multi-User-Welt) sollen ihre
    Jobs nicht sehen.
    """
    _session, token = pair
    watcher = _watcher(request)
    return RetryStatusResponse(jobs=[
        RetryJobResponse(
            plan_id=s.plan_id,
            device_ids=s.device_ids,
            attempts=s.attempts,
            last_failure_count=s.last_failure_count,
            started_at_ms=s.started_at_ms,
            next_attempt_at_ms=s.next_attempt_at_ms,
            paused=s.paused,
        )
        for s in watcher.stats() if s.session_token == token
    ])


@router.post("/schedule", response_model=RetryJobResponse, status_code=status.HTTP_201_CREATED)
def schedule_retry(
    payload: RetryScheduleRequest,
    request: Request,
    pair: tuple[Session, str] = Depends(require_session_with_token),
) -> RetryJobResponse:
    """Startet einen Auto-Retry-Job fuer den uebergebenen Plan + Geraete."""
    session, token = pair
    require_plan_role(session)
    require_device_ids_accessible(
        payload.device_ids, session.opened.data.devices, session,
    )
    watcher = _watcher(request)
    vault_path = session.vault_path
    job = watcher.schedule(
        plan_id=payload.plan_id,
        session_token=token,
        vault_path=str(vault_path) if vault_path is not None else "",
        device_ids=payload.device_ids,
        interval_s=payload.interval_s,
        max_duration_s=payload.max_duration_s,
    )
    return RetryJobResponse(
        plan_id=job.plan_id,
        device_ids=job.device_ids,
        attempts=job.attempts,
        last_failure_count=job.last_failure_count,
        started_at_ms=job.started_at_ms,
        next_attempt_at_ms=job.next_attempt_at_ms,
        paused=job.paused,
    )


@router.delete("/jobs/{plan_id}", status_code=status.HTTP_204_NO_CONTENT)
def cancel_retry(
    plan_id: str,
    request: Request,
    pair: tuple[Session, str] = Depends(require_session_with_token),
) -> None:
    """Beendet einen Auto-Retry-Job."""
    _session, token = pair
    watcher = _watcher(request)
    # Nur eigene Jobs cancelbar.
    matching = [s for s in watcher.stats() if s.plan_id == plan_id and s.session_token == token]
    if not matching:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Kein Retry-Job fuer Plan {plan_id} unter dieser Session.",
        )
    watcher.cancel(plan_id)


@router.post("/jobs/{plan_id}/pause", status_code=status.HTTP_204_NO_CONTENT)
def pause_retry(
    plan_id: str,
    request: Request,
    pair: tuple[Session, str] = Depends(require_session_with_token),
) -> None:
    _session, token = pair
    watcher = _watcher(request)
    matching = [s for s in watcher.stats() if s.plan_id == plan_id and s.session_token == token]
    if not matching:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="kein Job")
    watcher.pause(plan_id)


@router.post("/jobs/{plan_id}/resume", status_code=status.HTTP_204_NO_CONTENT)
def resume_retry(
    plan_id: str,
    request: Request,
    pair: tuple[Session, str] = Depends(require_session_with_token),
) -> None:
    _session, token = pair
    watcher = _watcher(request)
    matching = [s for s in watcher.stats() if s.plan_id == plan_id and s.session_token == token]
    if not matching:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="kein Job")
    watcher.resume(plan_id)


__all__ = ["router"]
