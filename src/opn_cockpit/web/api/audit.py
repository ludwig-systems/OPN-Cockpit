"""Audit-Log-Routen: gefilterte Sicht auf das append-only JSON-Lines-Log."""

from __future__ import annotations

import csv
import io
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse

from opn_cockpit.audit.backend import get_audit_backend
from opn_cockpit.audit.chain import load_or_generate_secret, verify_chain
from opn_cockpit.audit.log import AuditEventKind
from opn_cockpit.audit.pdf_report import render_pdf
from opn_cockpit.audit.sqlite_backend import SqliteAuditBackend
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


@router.get("/export.csv")
def export_audit_csv(
    session: Session = Depends(require_session),
    event: Annotated[str | None, Query()] = None,
    action: Annotated[str | None, Query()] = None,
    target_device_id: Annotated[str | None, Query()] = None,
    actor: Annotated[str | None, Query()] = None,
    since_iso: Annotated[str | None, Query(alias="since")] = None,
    until_iso: Annotated[str | None, Query(alias="until")] = None,
) -> StreamingResponse:
    """Liefert das Audit-Log als CSV-Download.

    Filter-Parameter sind identisch zur Liste. Reihenfolge: aelteste
    zuerst (forensik-freundlich). Zeitstempel und Felder bleiben
    unveraendert; Hash-Chain-Verifikation ist ein separater Aufruf.
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
    buffer = io.StringIO()
    writer = csv.writer(buffer, quoting=csv.QUOTE_MINIMAL)
    writer.writerow([
        "timestamp_utc", "actor", "event", "summary", "action",
        "target_device_id", "target_device_name", "target_count",
        "status", "error_kind", "failed_phase", "duration_ms", "vault_path",
    ])
    for r in records:
        writer.writerow([
            r.timestamp_utc, r.actor, str(r.event), r.summary,
            r.action or "", r.target_device_id or "", r.target_device_name or "",
            r.target_count if r.target_count is not None else "",
            r.status or "", r.error_kind or "", r.failed_phase or "",
            r.duration_ms if r.duration_ms is not None else "",
            r.vault_path or "",
        ])
    buffer.seek(0)
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="opn-cockpit-audit.csv"',
            "Cache-Control": "no-store",
        },
    )


@router.get("/export.pdf")
def export_audit_pdf(
    session: Session = Depends(require_session),
    event: Annotated[str | None, Query()] = None,
    action: Annotated[str | None, Query()] = None,
    target_device_id: Annotated[str | None, Query()] = None,
    actor: Annotated[str | None, Query()] = None,
    since_iso: Annotated[str | None, Query(alias="since")] = None,
    until_iso: Annotated[str | None, Query(alias="until")] = None,
) -> StreamingResponse:
    """Liefert das Audit-Log als signierten PDF-Download.

    Signatur (HMAC-SHA256 ueber alle Records mit dem Cockpit-Audit-Secret)
    landet sowohl im sichtbaren Footer als auch in den PDF-Metadaten
    (Keywords-Feld, ``OPN-COCKPIT-AUDIT-SIG-v1:<hex>``). Verifizierer mit
    Zugriff auf das Secret koennen das via ``audit/pdf_report.verify_pdf
    _signature`` reproduzieren.
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
    filter_bits: list[str] = []
    if event:
        filter_bits.append(f"event={event}")
    if action:
        filter_bits.append(f"action={action}")
    if target_device_id:
        filter_bits.append(f"device={target_device_id}")
    if actor:
        filter_bits.append(f"actor={actor}")
    if since_iso:
        filter_bits.append(f"since={since_iso}")
    if until_iso:
        filter_bits.append(f"until={until_iso}")
    filter_summary = ", ".join(filter_bits) if filter_bits else "ohne Filter"

    secret = load_or_generate_secret()
    pdf_bytes = render_pdf(
        records,
        secret=secret,
        filter_summary=filter_summary,
        issued_by=session.user.username if getattr(session, "user", None) else "",
    )
    return StreamingResponse(
        iter([pdf_bytes]),
        media_type="application/pdf",
        headers={
            "Content-Disposition": 'attachment; filename="opn-cockpit-audit.pdf"',
            "Cache-Control": "no-store",
        },
    )


@router.get("/verify")
def verify_audit_chain(
    session: Session = Depends(require_session),
) -> dict[str, object]:
    """Verifiziert die HMAC-Hash-Chain des Audit-Logs (v4-Pass 3).

    Nur sinnvoll, wenn SQLite-Backend mit Hash-Chain aktiv ist. Liefert
    Total + Anzahl geprüfter Eintraege + Liste der "broken" Indices
    (Tampering-Verdacht). Bei File-Backend: status='not-available'.
    """
    session.touch()
    backend = get_audit_backend()
    if not isinstance(backend, SqliteAuditBackend):
        return {
            "status": "not-available",
            "reason": "Hash-Chain ist nur im SQLite-Storage-Backend verfuegbar.",
            "total": 0,
            "broken": [],
        }
    chained = backend.read_chain()
    secret = load_or_generate_secret()
    broken = verify_chain(chained, secret)
    return {
        "status": "ok" if not broken else "broken",
        "total": len(chained),
        "broken": broken,
    }


__all__ = ["router"]
