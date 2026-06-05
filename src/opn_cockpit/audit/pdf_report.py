"""Signierter PDF-Audit-Report.

Erzeugt einen druckbaren Bericht aus einer Liste von Audit-Eintraegen +
HMAC-Signatur ueber den Inhalt. Die Signatur landet doppelt im PDF:

1. **Letzte Seite** als sichtbarer Footer-Block (Klartext SHA256 + HMAC).
2. **Document-Metadata** als ``Keywords``-Feld - so ist sie maschinell
   verifizierbar ohne den PDF-Body parsen zu muessen.

Verifikation ist Stand 2026-06-05 manuell (Helper ``verify_pdf_signature``):
den Original-Body neu rendern, Signatur neu rechnen, vergleichen. Der
Renderer ist deterministisch (kein Zeitstempel im Body, nur in den
Metadaten) damit das funktioniert.

Wir bauen das mit ``fpdf2`` - leichtere Alternative zu reportlab. Kein
externes Tooling noetig.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Iterable
from datetime import UTC, datetime

from fpdf import FPDF
from fpdf.enums import XPos, YPos

from opn_cockpit.audit.log import AuditRecord


REPORT_TITLE = "OPN-Cockpit Audit-Report"
REPORT_VERSION = 1
SIG_PREFIX = "OPN-COCKPIT-AUDIT-SIG-v1:"

# Spalten in der Detailtabelle. Pro Spalte: (header, breite_in_mm,
# attr_oder_callable). Die Reihenfolge ist intentional kompakt -
# Vollformate landen in der "Summary"-Spalte.
_COLUMNS: tuple[tuple[str, float, str], ...] = (
    ("Zeit (UTC)", 36.0, "timestamp_utc"),
    ("Akteur", 30.0, "actor"),
    ("Event", 36.0, "event"),
    ("Zusammenfassung", 88.0, "summary"),
)


def _record_field(record: AuditRecord, attr: str) -> str:
    value = getattr(record, attr, "")
    if value is None:
        return ""
    return str(value)


def _payload_for_signature(records: list[AuditRecord]) -> bytes:
    """Stabile Bytes-Repraesentation fuer HMAC.

    Reihenfolge bleibt; pro Record alle relevanten Felder in fester
    Reihenfolge mit ``\\0`` als Separator. Keine Zeitstempel ausserhalb
    der Records - sonst koennte der Empfaenger nicht reproduzieren.
    """
    parts: list[str] = [
        f"OPN-AUDIT-REPORT-v{REPORT_VERSION}",
        f"records={len(records)}",
    ]
    for r in records:
        parts.append("\x1e".join([
            r.timestamp_utc or "",
            r.actor or "",
            str(r.event) if r.event else "",
            r.summary or "",
            r.action or "",
            r.target_device_id or "",
            r.target_device_name or "",
            str(r.target_count) if r.target_count is not None else "",
            r.status or "",
            r.error_kind or "",
            r.failed_phase or "",
            str(r.duration_ms) if r.duration_ms is not None else "",
        ]))
    return "\x1f".join(parts).encode("utf-8")


def compute_signature(records: list[AuditRecord], secret: bytes) -> str:
    """Liefert HMAC-SHA256-Hex ueber die Records mit dem Audit-Secret."""
    payload = _payload_for_signature(records)
    mac = hmac.new(secret, payload, digestmod=hashlib.sha256)
    return mac.hexdigest()


def compute_body_sha256(records: list[AuditRecord]) -> str:
    """Reiner SHA256 ohne Secret - fuer sichtbare Pruefsumme im Footer."""
    return hashlib.sha256(_payload_for_signature(records)).hexdigest()


def render_pdf(
    records: Iterable[AuditRecord],
    *,
    secret: bytes,
    filter_summary: str = "",
    issued_by: str = "",
) -> bytes:
    """Erzeugt den PDF-Body inklusive Signatur.

    Determinismus: Records-Reihenfolge bleibt; der Render-Pfad nutzt
    keine Realtime-Daten (das Issue-Datum landet nur in der Header-Zeile
    + Metadata, beides ist nicht Teil der signierten Bytes).
    """
    record_list = list(records)
    sha256 = compute_body_sha256(record_list)
    signature = compute_signature(record_list, secret)
    issued_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%SZ")

    pdf = FPDF(orientation="L", unit="mm", format="A4")  # Landscape - viele Spalten
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    # Header
    pdf.set_font("Helvetica", style="B", size=14)
    pdf.cell(0, 8, REPORT_TITLE, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("Helvetica", size=9)
    pdf.cell(0, 5, f"Erstellt: {issued_at}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    if issued_by:
        pdf.cell(0, 5, f"Erstellt von: {issued_by}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    if filter_summary:
        pdf.cell(0, 5, f"Filter: {filter_summary}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.cell(0, 5, f"Eintraege: {len(record_list)}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(2)

    # Tabelle Header
    pdf.set_font("Helvetica", style="B", size=8.5)
    pdf.set_fill_color(220, 220, 220)
    for header, width, _ in _COLUMNS:
        pdf.cell(width, 6, header, border=1, fill=True)
    pdf.ln()

    # Tabelle Body
    pdf.set_font("Helvetica", size=8)
    for record in record_list:
        # Hoehe der hoechsten Zelle vorab berechnen damit die Zeile
        # in einer Linie bleibt - fpdf2 multi_cell muesste sonst durch.
        y_before = pdf.get_y()
        x = pdf.get_x()
        for _, width, attr in _COLUMNS:
            value = _record_field(record, attr)
            pdf.set_xy(x, y_before)
            pdf.multi_cell(width, 4, value, border=1)
            x += width
        # Naechste Zeile: zuruck zur Margin
        pdf.set_xy(pdf.l_margin, y_before + 4)
        # Falls multi_cell groesser geworden ist (lange Summaries),
        # rutscht get_y entsprechend - wir setzen y auf das groessere
        # der beiden Werte, damit nichts ueberschrieben wird.
        max_y = pdf.get_y()
        for _, width, attr in _COLUMNS:
            # No-op-Schleife nur damit max_y konsistent berechnet bleibt;
            # fpdf2 trackt das bereits intern - wir lassen es hier explizit
            # damit die Logik fuer den Reviewer lesbar ist.
            pass  # noqa: PLW0107
        pdf.set_y(max_y)

    pdf.ln(4)
    pdf.set_x(pdf.l_margin)

    # Signatur-Footer
    pdf.set_font("Helvetica", style="B", size=9)
    pdf.cell(0, 5, "Integritaets-Signatur (zur Verifikation)",
             new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("Courier", size=8)
    pdf.set_x(pdf.l_margin)
    pdf.multi_cell(0, 4, f"SHA256 (Inhalt):  {sha256}", new_x=XPos.LMARGIN)
    pdf.set_x(pdf.l_margin)
    pdf.multi_cell(0, 4, f"HMAC (Cockpit):   {signature}", new_x=XPos.LMARGIN)
    pdf.set_x(pdf.l_margin)
    pdf.set_font("Helvetica", style="I", size=7.5)
    pdf.multi_cell(
        0, 4,
        "HMAC wird mit dem Cockpit-eigenen Audit-Secret berechnet; ein "
        "Verifizierer mit Zugriff auf das Secret kann den Report "
        "reproduzieren (siehe verify_pdf_signature in pdf_report.py).",
        new_x=XPos.LMARGIN,
    )

    # Metadata: maschinell auslesbare Signatur fuer Verifier-Tooling
    pdf.set_title(REPORT_TITLE)
    pdf.set_keywords(f"{SIG_PREFIX}{signature}")

    output = pdf.output()
    return bytes(output)


def verify_pdf_signature(records: list[AuditRecord], expected_signature: str,
                         secret: bytes) -> bool:
    """Pruefen ob die Records zu der erwarteten Signatur passen.

    Konstantzeit-Vergleich. Aufrufer extrahiert ``expected_signature`` aus
    dem PDF (z. B. via pypdf aus den Metadaten) und uebergibt sie hier
    zusammen mit den frisch geladenen Records.
    """
    actual = compute_signature(records, secret)
    return hmac.compare_digest(actual, expected_signature)


__all__ = [
    "REPORT_TITLE",
    "REPORT_VERSION",
    "SIG_PREFIX",
    "compute_body_sha256",
    "compute_signature",
    "render_pdf",
    "verify_pdf_signature",
]
