"""SMTP-Sender fuer Cockpit-Benachrichtigungen.

Bewusst duenn ueber :mod:`smtplib` gewickelt — keine Async-IO, keine
Queue-Persistenz. Mails werden im auslesenden Thread (Watcher, API-
Handler) synchron abgesetzt. Bei einem SMTP-Timeout blockiert also
kurz der Aufrufer; das ist bei einem einzigen Mail-Send pro Rollout
akzeptabel.

Fehlerpfade werden konsequent in :class:`MailError` gekapselt —
smtplib wirft ~5 verschiedene Exception-Typen, die Aufrufer sollen
sich um genau einen kuemmern.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from opn_cockpit.vault.model import SmtpSettings

logger = logging.getLogger(__name__)


class MailError(Exception):
    """Alle SMTP-Fehler werden hierauf gemappt."""


@dataclass(frozen=True, slots=True)
class MailMessage:
    to: tuple[str, ...]
    subject: str
    body_text: str


def _build_message(
    smtp: SmtpSettings, msg: MailMessage,
) -> EmailMessage:
    email = EmailMessage()
    email["From"] = smtp.from_addr or (smtp.username or "opn-cockpit@localhost")
    email["To"] = ", ".join(msg.to)
    email["Subject"] = msg.subject
    email.set_content(msg.body_text)
    # Kennzeichnen dass die Mail von einem Automaten kommt — die meisten
    # MTAs respektieren das (kein Auto-Reply, kein Vacation-Bounce).
    email["Auto-Submitted"] = "auto-generated"
    email["X-Auto-Response-Suppress"] = "All"
    return email


def send_mail(smtp: SmtpSettings, msg: MailMessage) -> None:
    """Sendet eine Mail via SMTP. Wirft :class:`MailError` bei Problemen.

    Wenn ``smtp.enabled=False`` oder ``msg.to`` leer: silent no-op — der
    Aufrufer muss nicht selbst prufen. Absichtlich, damit
    Notification-Pfade den Config-Check dem Sender ueberlassen koennen.
    """
    if not smtp.enabled:
        logger.debug("SMTP disabled, skipping mail send.")
        return
    if not msg.to:
        logger.debug("Empty recipient list, skipping mail send.")
        return
    if not smtp.host:
        raise MailError("SMTP-Host nicht konfiguriert.")

    email = _build_message(smtp, msg)
    tls_mode = (smtp.tls_mode or "starttls").lower()
    timeout = max(1.0, float(smtp.connect_timeout_s or 15.0))

    try:
        if tls_mode == "tls":
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(
                smtp.host, smtp.port, timeout=timeout, context=context,
            ) as client:
                if smtp.username:
                    client.login(smtp.username, smtp.password)
                client.send_message(email)
        else:
            with smtplib.SMTP(smtp.host, smtp.port, timeout=timeout) as client:
                client.ehlo()
                if tls_mode == "starttls":
                    context = ssl.create_default_context()
                    client.starttls(context=context)
                    client.ehlo()
                if smtp.username:
                    client.login(smtp.username, smtp.password)
                client.send_message(email)
    except smtplib.SMTPAuthenticationError as exc:
        raise MailError(f"SMTP-Auth abgelehnt: {exc.smtp_error!r}") from exc
    except smtplib.SMTPResponseException as exc:
        raise MailError(f"SMTP-Fehler {exc.smtp_code}: {exc.smtp_error!r}") from exc
    except (OSError, smtplib.SMTPException, ssl.SSLError) as exc:
        raise MailError(f"SMTP-Verbindung gescheitert: {exc}") from exc


# ---------------------------------------------------------------------------
# Rollout-spezifische Templates
# ---------------------------------------------------------------------------


def build_rollout_completion_body(
    *, rollout_id: str, status: str,
    finished_at_iso: str,
    total: int, ok: int, failed: int, skipped: int,
    device_lines: list[str],
    initiator: str = "",
) -> str:
    """Baut den Body-Text fuer eine Firmware-Rollout-Ergebnismail.

    Text-Only (Plaintext), MIME-Multipart lohnt fuer eine Status-Mail
    nicht. Zeilenumbrueche via ``\\n``, EmailMessage macht das konform.
    """
    header = f"Firmware-Rollout {rollout_id} beendet: {status.upper()}"
    lines = [
        header,
        "=" * len(header),
        "",
        f"Beendet:    {finished_at_iso or '(unbekannt)'}",
        f"Ergebnis:   {ok} OK, {failed} FAILED, {skipped} SKIPPED (von {total})",
    ]
    if initiator:
        lines.append(f"Ausgeloest: {initiator}")
    lines.extend(["", "Pro Geraet:", ""])
    if device_lines:
        lines.extend(device_lines)
    else:
        lines.append("  (keine Geraete-Details verfuegbar)")
    lines.extend([
        "",
        "-- ",
        "Automatisch generiert von OPN-Cockpit.",
    ])
    return "\n".join(lines)


def send_rollout_completion_mail(
    smtp: SmtpSettings,
    *,
    rollout_id: str,
    status: str,
    finished_at_iso: str,
    total: int, ok: int, failed: int, skipped: int,
    device_lines: list[str],
    recipients: list[str] | None = None,
    initiator: str = "",
) -> None:
    """Convenience-Wrapper — baut Body + verschickt in einem Rutsch.

    Nutzt ``smtp.default_recipients`` wenn ``recipients`` None ist.
    Silent no-op wenn kein Empfaenger existiert (statt Exception —
    der Rollout-Watcher soll nicht wegen fehlender Mail-Config
    abbrechen).
    """
    to = recipients if recipients is not None else list(smtp.default_recipients)
    if not to:
        logger.debug(
            "No recipients for rollout %s completion mail, skipping.",
            rollout_id,
        )
        return
    subject = f"[Cockpit] Firmware-Rollout {rollout_id}: {status.upper()}"
    body = build_rollout_completion_body(
        rollout_id=rollout_id, status=status,
        finished_at_iso=finished_at_iso,
        total=total, ok=ok, failed=failed, skipped=skipped,
        device_lines=device_lines,
        initiator=initiator,
    )
    send_mail(smtp, MailMessage(to=tuple(to), subject=subject, body_text=body))


__all__ = [
    "MailError",
    "MailMessage",
    "build_rollout_completion_body",
    "send_mail",
    "send_rollout_completion_mail",
]
