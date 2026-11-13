"""Tests fuer die SMTP-Settings-Endpoints unter /api/vaults/settings/smtp.

Fokus:
* GET liefert Password maskiert
* PUT mit password="***" behaelt gespeichertes Password
* PUT mit leerem password loescht es
* Test-Endpoint ruft send_mail und mappt MailError auf ok=False
* enabled ohne Host -> 400
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from opn_cockpit.notifications.mail import MailError
from opn_cockpit.vault.model import SmtpSettings, VaultData, VaultDevice, VaultSettings
from opn_cockpit.vault.store import create_vault, open_vault
from opn_cockpit.web.server import create_app

PASSWORD = "korrektes-pferd-batterie-heftklammer"


def _make_vault(tmp_path: Path, *, smtp: SmtpSettings | None = None) -> Path:
    devices = [
        VaultDevice(
            id="dev-001",
            name="HQ",
            host="opn.lab",
            port=443,
            tls_verify=True,
            api_key="k",
            api_secret="s",
            descr="",
        ),
    ]
    settings = VaultSettings()
    if smtp is not None:
        settings.smtp = smtp
    path = tmp_path / "test.opnvault"
    create_vault(path, PASSWORD, VaultData(devices=devices, settings=settings))
    return path


def _unlock(client: TestClient, vault_path: Path) -> str:
    r = client.post(
        "/api/auth/unlock",
        json={"vault_path": str(vault_path), "password": PASSWORD},
    )
    assert r.status_code == 200
    return str(r.json()["token"])


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def client() -> TestClient:
    return TestClient(create_app())


class TestGetSmtp:
    def test_requires_auth(self, client: TestClient) -> None:
        assert client.get("/api/vaults/settings/smtp").status_code == 401

    def test_password_masked_when_present(
        self,
        client: TestClient,
        tmp_path: Path,
    ) -> None:
        vault = _make_vault(
            tmp_path,
            smtp=SmtpSettings(
                enabled=True,
                host="mail.example.com",
                password="s3cret",
                default_recipients=["ops@example.com"],
            ),
        )
        token = _unlock(client, vault)
        r = client.get("/api/vaults/settings/smtp", headers=_bearer(token))
        assert r.status_code == 200
        d = r.json()
        assert d["enabled"] is True
        assert d["host"] == "mail.example.com"
        assert d["password"] == "***"
        assert d["default_recipients"] == ["ops@example.com"]

    def test_empty_password_returned_empty(
        self,
        client: TestClient,
        tmp_path: Path,
    ) -> None:
        vault = _make_vault(tmp_path)
        token = _unlock(client, vault)
        r = client.get("/api/vaults/settings/smtp", headers=_bearer(token))
        assert r.status_code == 200
        assert r.json()["password"] == ""


class TestPutSmtp:
    def test_masked_password_keeps_stored(
        self,
        client: TestClient,
        tmp_path: Path,
    ) -> None:
        vault = _make_vault(
            tmp_path,
            smtp=SmtpSettings(
                enabled=True,
                host="mail.example.com",
                password="original-secret",
            ),
        )
        token = _unlock(client, vault)
        r = client.put(
            "/api/vaults/settings/smtp",
            headers=_bearer(token),
            json={
                "enabled": True,
                "host": "mail.example.com",
                "port": 587,
                "tls_mode": "starttls",
                "username": "alice",
                "password": "***",
                "from_addr": "",
                "default_recipients": [],
                "connect_timeout_s": 15.0,
            },
        )
        assert r.status_code == 200
        # Response ist immer maskiert wenn password gespeichert
        assert r.json()["password"] == "***"
        # Reopen: password ist noch der alte Wert
        opened = open_vault(vault, PASSWORD)
        assert opened.data.settings.smtp.password == "original-secret"

    def test_empty_password_clears_stored(
        self,
        client: TestClient,
        tmp_path: Path,
    ) -> None:
        vault = _make_vault(
            tmp_path,
            smtp=SmtpSettings(
                host="mail.example.com",
                password="original-secret",
            ),
        )
        token = _unlock(client, vault)
        r = client.put(
            "/api/vaults/settings/smtp",
            headers=_bearer(token),
            json={
                "enabled": False,
                "host": "mail.example.com",
                "port": 587,
                "tls_mode": "starttls",
                "username": "",
                "password": "",
                "from_addr": "",
                "default_recipients": [],
                "connect_timeout_s": 15.0,
            },
        )
        assert r.status_code == 200
        assert r.json()["password"] == ""
        opened = open_vault(vault, PASSWORD)
        assert opened.data.settings.smtp.password == ""

    def test_enabled_without_host_rejected(
        self,
        client: TestClient,
        tmp_path: Path,
    ) -> None:
        vault = _make_vault(tmp_path)
        token = _unlock(client, vault)
        r = client.put(
            "/api/vaults/settings/smtp",
            headers=_bearer(token),
            json={
                "enabled": True,
                "host": "",
                "port": 587,
                "tls_mode": "starttls",
                "username": "",
                "password": "",
                "from_addr": "",
                "default_recipients": [],
                "connect_timeout_s": 15.0,
            },
        )
        assert r.status_code == 400
        assert "Host" in r.json()["detail"]

    def test_invalid_tls_mode_rejected(
        self,
        client: TestClient,
        tmp_path: Path,
    ) -> None:
        vault = _make_vault(tmp_path)
        token = _unlock(client, vault)
        r = client.put(
            "/api/vaults/settings/smtp",
            headers=_bearer(token),
            json={
                "enabled": False,
                "host": "mail",
                "port": 587,
                "tls_mode": "sneakynewproto",
                "username": "",
                "password": "",
                "from_addr": "",
                "default_recipients": [],
                "connect_timeout_s": 15.0,
            },
        )
        assert r.status_code == 422

    def test_invalid_recipient_rejected(
        self,
        client: TestClient,
        tmp_path: Path,
    ) -> None:
        vault = _make_vault(tmp_path)
        token = _unlock(client, vault)
        r = client.put(
            "/api/vaults/settings/smtp",
            headers=_bearer(token),
            json={
                "enabled": False,
                "host": "mail",
                "port": 587,
                "tls_mode": "starttls",
                "username": "",
                "password": "",
                "from_addr": "",
                "default_recipients": ["nicht-eine-mail"],
                "connect_timeout_s": 15.0,
            },
        )
        assert r.status_code == 422


class TestTestSmtp:
    def test_returns_ok_true_on_success(
        self,
        client: TestClient,
        tmp_path: Path,
    ) -> None:
        vault = _make_vault(
            tmp_path,
            smtp=SmtpSettings(
                host="mail.example.com",
                password="s3cret",
            ),
        )
        token = _unlock(client, vault)
        with patch("opn_cockpit.notifications.mail.send_mail") as sm:
            r = client.post(
                "/api/vaults/settings/smtp/test",
                headers=_bearer(token),
                json={"to": "ops@example.com"},
            )
        assert r.status_code == 200
        assert r.json()["ok"] is True
        sm.assert_called_once()

    def test_returns_ok_false_on_mailerror(
        self,
        client: TestClient,
        tmp_path: Path,
    ) -> None:
        vault = _make_vault(
            tmp_path,
            smtp=SmtpSettings(
                host="mail.example.com",
                password="s3cret",
            ),
        )
        token = _unlock(client, vault)
        with patch(
            "opn_cockpit.notifications.mail.send_mail",
            side_effect=MailError("Verbindung refused"),
        ):
            r = client.post(
                "/api/vaults/settings/smtp/test",
                headers=_bearer(token),
                json={"to": "ops@example.com"},
            )
        assert r.status_code == 200
        d = r.json()
        assert d["ok"] is False
        assert "refused" in d["detail"]

    def test_no_host_configured_returns_false(
        self,
        client: TestClient,
        tmp_path: Path,
    ) -> None:
        vault = _make_vault(tmp_path)  # SMTP default (host="")
        token = _unlock(client, vault)
        r = client.post(
            "/api/vaults/settings/smtp/test",
            headers=_bearer(token),
            json={"to": "ops@example.com"},
        )
        assert r.status_code == 200
        d = r.json()
        assert d["ok"] is False
        assert "Host" in d["detail"]

    def test_invalid_to_rejected(
        self,
        client: TestClient,
        tmp_path: Path,
    ) -> None:
        vault = _make_vault(tmp_path, smtp=SmtpSettings(host="mail"))
        token = _unlock(client, vault)
        r = client.post(
            "/api/vaults/settings/smtp/test",
            headers=_bearer(token),
            json={"to": "notanemail"},
        )
        assert r.status_code == 422
