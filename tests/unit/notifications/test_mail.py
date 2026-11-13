"""Tests fuer notifications/mail.py.

smtplib.SMTP / SMTP_SSL werden gemockt — kein echter Netzwerk-Aufruf.
Fokus: TLS-Mode-Verzweigung, Fehler-Mapping, silent-no-op wenn
disabled, Body-Formatierung.
"""

from __future__ import annotations

import smtplib
from unittest.mock import MagicMock, patch

import pytest

from opn_cockpit.notifications.mail import (
    MailError,
    MailMessage,
    build_rollout_completion_body,
    send_mail,
    send_rollout_completion_mail,
)
from opn_cockpit.vault.model import SmtpSettings


def _base_smtp(**overrides) -> SmtpSettings:
    kwargs = dict(
        enabled=True,
        host="mail.example.com",
        port=587,
        tls_mode="starttls",
        username="alice",
        password="s3cret",
        from_addr="cockpit@example.com",
        default_recipients=["ops@example.com"],
        connect_timeout_s=10.0,
    )
    kwargs.update(overrides)
    return SmtpSettings(**kwargs)


def _msg(**overrides) -> MailMessage:
    kwargs = dict(
        to=("ops@example.com",),
        subject="hello",
        body_text="body",
    )
    kwargs.update(overrides)
    return MailMessage(**kwargs)


class TestSendMail:
    def test_silent_noop_if_disabled(self) -> None:
        smtp = _base_smtp(enabled=False)
        with patch("smtplib.SMTP") as smtp_cls:
            send_mail(smtp, _msg())
        assert not smtp_cls.called

    def test_silent_noop_if_empty_recipients(self) -> None:
        with patch("smtplib.SMTP") as smtp_cls:
            send_mail(_base_smtp(), _msg(to=()))
        assert not smtp_cls.called

    def test_raises_if_enabled_without_host(self) -> None:
        smtp = _base_smtp(host="")
        with pytest.raises(MailError, match="Host"):
            send_mail(smtp, _msg())

    def test_starttls_flow_calls_ehlo_starttls_login(self) -> None:
        smtp = _base_smtp(tls_mode="starttls")
        client = MagicMock()
        with patch("smtplib.SMTP") as smtp_cls:
            smtp_cls.return_value.__enter__.return_value = client
            send_mail(smtp, _msg())
        client.ehlo.assert_called()
        client.starttls.assert_called_once()
        client.login.assert_called_once_with("alice", "s3cret")
        client.send_message.assert_called_once()

    def test_tls_wrapped_flow_uses_smtp_ssl(self) -> None:
        smtp = _base_smtp(tls_mode="tls", port=465)
        client = MagicMock()
        with patch("smtplib.SMTP_SSL") as smtp_ssl_cls, patch("smtplib.SMTP") as smtp_cls:
            smtp_ssl_cls.return_value.__enter__.return_value = client
            send_mail(smtp, _msg())
        smtp_ssl_cls.assert_called_once()
        assert not smtp_cls.called
        client.login.assert_called_once()
        client.send_message.assert_called_once()

    def test_no_tls_flow_skips_starttls(self) -> None:
        smtp = _base_smtp(tls_mode="none")
        client = MagicMock()
        with patch("smtplib.SMTP") as smtp_cls:
            smtp_cls.return_value.__enter__.return_value = client
            send_mail(smtp, _msg())
        client.starttls.assert_not_called()

    def test_empty_username_skips_login(self) -> None:
        smtp = _base_smtp(username="", password="")
        client = MagicMock()
        with patch("smtplib.SMTP") as smtp_cls:
            smtp_cls.return_value.__enter__.return_value = client
            send_mail(smtp, _msg())
        client.login.assert_not_called()

    def test_auth_error_becomes_mailerror(self) -> None:
        smtp = _base_smtp()
        client = MagicMock()
        client.login.side_effect = smtplib.SMTPAuthenticationError(
            535,
            b"5.7.8 Auth failed",
        )
        with patch("smtplib.SMTP") as smtp_cls:
            smtp_cls.return_value.__enter__.return_value = client
            with pytest.raises(MailError, match="Auth"):
                send_mail(smtp, _msg())

    def test_generic_smtp_exception_becomes_mailerror(self) -> None:
        smtp = _base_smtp()
        with patch("smtplib.SMTP") as smtp_cls:
            smtp_cls.side_effect = ConnectionRefusedError("nope")
            with pytest.raises(MailError, match="Verbindung"):
                send_mail(smtp, _msg())


class TestRolloutCompletionBody:
    def test_body_contains_rollout_id_and_status(self) -> None:
        body = build_rollout_completion_body(
            rollout_id="r-abc",
            status="done",
            finished_at_iso="2026-07-20T20:00:00Z",
            total=3,
            ok=2,
            failed=1,
            skipped=0,
            device_lines=["  [DONE    ] a: ok"],
        )
        assert "r-abc" in body
        assert "DONE" in body
        assert "2 OK" in body
        assert "1 FAILED" in body
        assert "2026-07-20T20:00:00Z" in body

    def test_empty_device_lines_still_valid(self) -> None:
        body = build_rollout_completion_body(
            rollout_id="r-abc",
            status="cancelled",
            finished_at_iso="",
            total=0,
            ok=0,
            failed=0,
            skipped=0,
            device_lines=[],
        )
        assert "keine Geraete-Details" in body


class TestSendRolloutCompletionMail:
    def test_silent_noop_if_no_recipients(self) -> None:
        smtp = _base_smtp(default_recipients=[])
        with patch("smtplib.SMTP") as smtp_cls:
            send_rollout_completion_mail(
                smtp,
                rollout_id="r-1",
                status="done",
                finished_at_iso="",
                total=0,
                ok=0,
                failed=0,
                skipped=0,
                device_lines=[],
            )
        assert not smtp_cls.called

    def test_explicit_recipients_win_over_defaults(self) -> None:
        smtp = _base_smtp(default_recipients=["default@example.com"])
        client = MagicMock()
        with patch("smtplib.SMTP") as smtp_cls:
            smtp_cls.return_value.__enter__.return_value = client
            send_rollout_completion_mail(
                smtp,
                rollout_id="r-2",
                status="failed",
                finished_at_iso="2026-07-20T21:00:00Z",
                total=1,
                ok=0,
                failed=1,
                skipped=0,
                device_lines=["  [FAILED  ] sg2: boom"],
                recipients=["explicit@example.com"],
            )
        client.send_message.assert_called_once()
        msg = client.send_message.call_args.args[0]
        assert msg["To"] == "explicit@example.com"
        assert "r-2" in msg["Subject"]
        assert "FAILED" in msg["Subject"]
