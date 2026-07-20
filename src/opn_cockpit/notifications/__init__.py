"""Benachrichtigungs-Kanaele (E-Mail, spaeter ggf. Webhook/Slack).

Zweck: Aussenwelt-Kommunikation von Cockpit — Firmware-Rollout ist
fertig, TLS-Cert laeuft ab, Config-Drift entdeckt.

Trennung: dieses Paket kennt keine Vault-Konfiguration direkt. Der
Aufrufer laedt die :class:`SmtpSettings` aus dem entsperrten Vault
und reicht sie hier rein. Damit bleibt Notifications testbar ohne
Vault-Fixture.
"""

from opn_cockpit.notifications.mail import (
    MailError,
    build_rollout_completion_body,
    send_mail,
    send_rollout_completion_mail,
)

__all__ = [
    "MailError",
    "build_rollout_completion_body",
    "send_mail",
    "send_rollout_completion_mail",
]
