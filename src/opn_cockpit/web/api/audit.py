"""Audit-Log-Routen: gefilterte Sicht auf das append-only JSON-Lines-Log."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from opn_cockpit.audit.backend import get_audit_backend
from opn_cockpit.audit.log import AuditEventKind
from opn_cockpit.security.session import Session
from opn_cockpit.web.api.schemas import (
    AuditEntryResponse,
    AuditListResponse,
)
from opn_cockpit.web.auth.dependencies import require_session

router = APIRouter(prefix="/api/audit", tags=["audit"])

MAX_LIMIT = 1000
DEFAULT_LIMIT = 200


@router.get("", response_model=AuditListResponse)
def list_audit(
    session: Session = Depends(require_session),
    event: Annotated[str | None, Query()] = None,
    action: Annotated[str | None, Query()] = None,
    target_device_id: Annotated[str | None, Query()] = None,
    actor: Annotated[str | None, Query()] = None,
    since_iso: Annotated[str | None, Query(alias="since")] = None,
    until_iso: Annotated[str | None, Query(alias="until")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
) -> AuditListResponse:
    """Liefert die zuletzt N Audit-Eintraege, optional gefiltert.

    Reihenfolge: neueste zuerst. ``limit`` deckelt den Response auf MAX_LIMIT.
    ``truncated`` zeigt an, ob noch mehr Eintraege im Log liegen wuerden.
    """
    session.touch()
    audit = get_audit_backend()

    event_enum: AuditEventKind | None = None
    if event:
        try:
            event_enum = AuditEventKind(event)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unbekannter event-Wert: {event}",
            ) from exc

    records = audit.filter(
        event=event_enum,
        action=action,
        target_device_id=target_device_id,
        actor=actor,
        since_iso=since_iso,
        until_iso=until_iso,
    )
    total = len(records)
    # Neueste zuerst
    records.reverse()
    truncated = total > limit
    visible = records[:limit]
    entries = [
        AuditEntryResponse(
            timestamp_utc=r.timestamp_utc,
            actor=r.actor,
            event=str(r.event),
            summary=r.summary,
            action=r.action,
            target_device_id=r.target_device_id,
            target_device_name=r.target_device_name,
            target_count=r.target_count,
            status=r.status,
            error_kind=r.error_kind,
            failed_phase=r.failed_phase,
            duration_ms=r.duration_ms,
            vault_path=r.vault_path,
        )
        for r in visible
    ]
    return AuditListResponse(entries=entries, total=total, truncated=truncated)


@router.get("/events", response_model=list[str])
def list_event_kinds(session: Session = Depends(require_session)) -> list[str]:
    """Liefert alle bekannten Event-Kinds als String-Liste — fuer Filter-UI."""
    session.touch()
    return [str(e) for e in AuditEventKind]


__all__ = ["router"]
