"""End-to-End-Tests fuer den TOTP-Enrollment- + Login-Flow."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pyotp
import pytest
from fastapi.testclient import TestClient

from opn_cockpit.config import AppSettings
from opn_cockpit.vault.model import VaultData, VaultDevice
from opn_cockpit.vault.store import create_vault
from opn_cockpit.web.server import create_app
from opn_cockpit.web.server_state import VAULT_PATH_ENV, ServerState

VAULT_PASSWORD = "korrektes-pferd-batterie-heftklammer"
ALICE_PASSWORD = "alice-passwort-mit-genug-zeichen"


def _make_vault(tmp_path: Path) -> Path:
    path = tmp_path / "shared.opnvault"
    devices = [
        VaultDevice(
            id="dev-001",
            name="HQ Berlin",
            host="opn-1.lab",
            port=443,
            tls_verify=True,
            tags=[],
            api_key="k",
            api_secret="s",
            descr="",
        ),
    ]
    create_vault(path, VAULT_PASSWORD, VaultData(devices=devices))
    return path


@pytest.fixture()
def client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("OPNCOCKPIT_DATA_DIR", str(data))
    monkeypatch.delenv(VAULT_PATH_ENV, raising=False)
    vault_path = _make_vault(tmp_path)
    AppSettings(
        auth_backend="user-db",
        deployment_mode="multi-server",
        default_vault=str(vault_path),
    ).save(data / "settings.json")
    c = TestClient(create_app())
    server: ServerState = c.app.state.server_state
    assert server.user_store is not None
    default_user = server.user_store.get_user_by_name("admin")
    if default_user is not None:
        server.user_store.delete_user(default_user.id)
    server.bootstrap_create_admin("alice", ALICE_PASSWORD)
    server.bootstrap_unlock_vault(vault_path, VAULT_PASSWORD)
    yield c


def _login_password(client: TestClient, username: str, password: str) -> dict:
    r = client.post(
        "/api/auth/login",
        json={
            "username": username,
            "password": password,
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class TestEnrollFlow:
    def test_enroll_then_confirm_enables_totp(
        self,
        client: TestClient,
    ) -> None:
        token = _login_password(client, "alice", ALICE_PASSWORD)["token"]
        enroll = client.post("/api/users/me/totp/enroll", headers=_h(token))
        assert enroll.status_code == 200, enroll.text
        body = enroll.json()
        secret = body["secret_base32"]
        assert len(secret) == 32
        assert body["otpauth_uri"].startswith("otpauth://totp/")

        # Wenn man verkehrten Code schickt → 401
        bad = client.post(
            "/api/users/me/totp/confirm",
            headers=_h(token),
            json={"code": "000000"},
        )
        assert bad.status_code == 401

        # Korrekter Code → Backup-Codes + enabled
        correct = pyotp.TOTP(secret).now()
        ok = client.post(
            "/api/users/me/totp/confirm",
            headers=_h(token),
            json={"code": correct},
        )
        assert ok.status_code == 200, ok.text
        ok_body = ok.json()
        assert ok_body["enabled"] is True
        assert len(ok_body["backup_codes"]) == 8
        # status zeigt jetzt enabled
        status = client.get("/api/users/me/totp", headers=_h(token))
        assert status.status_code == 200
        assert status.json() == {
            "enabled": True,
            "backup_codes_remaining": 8,
        }

    def test_enroll_blocked_when_already_enabled(
        self,
        client: TestClient,
    ) -> None:
        token = _login_password(client, "alice", ALICE_PASSWORD)["token"]
        # Setup
        body = client.post("/api/users/me/totp/enroll", headers=_h(token)).json()
        client.post(
            "/api/users/me/totp/confirm",
            headers=_h(token),
            json={"code": pyotp.TOTP(body["secret_base32"]).now()},
        )
        # Zweiter Enroll-Versuch → 409
        again = client.post("/api/users/me/totp/enroll", headers=_h(token))
        assert again.status_code == 409


class TestTotpLogin:
    def test_login_step1_returns_challenge_for_totp_user(
        self,
        client: TestClient,
    ) -> None:
        # Setup: alice aktiviert TOTP
        token = _login_password(client, "alice", ALICE_PASSWORD)["token"]
        enroll = client.post("/api/users/me/totp/enroll", headers=_h(token)).json()
        secret = enroll["secret_base32"]
        client.post(
            "/api/users/me/totp/confirm",
            headers=_h(token),
            json={"code": pyotp.TOTP(secret).now()},
        )
        client.post("/api/auth/lock", headers=_h(token))

        # Step 1: Username+Passwort → Challenge
        r = client.post(
            "/api/auth/login",
            json={
                "username": "alice",
                "password": ALICE_PASSWORD,
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert body.get("totp_required") is True
        assert "challenge" in body
        assert "token" not in body

    def test_login_step2_correct_code_returns_token(
        self,
        client: TestClient,
    ) -> None:
        token = _login_password(client, "alice", ALICE_PASSWORD)["token"]
        enroll = client.post("/api/users/me/totp/enroll", headers=_h(token)).json()
        secret = enroll["secret_base32"]
        client.post(
            "/api/users/me/totp/confirm",
            headers=_h(token),
            json={"code": pyotp.TOTP(secret).now()},
        )
        client.post("/api/auth/lock", headers=_h(token))

        step1 = client.post(
            "/api/auth/login",
            json={
                "username": "alice",
                "password": ALICE_PASSWORD,
            },
        ).json()
        step2 = client.post(
            "/api/auth/login/totp",
            json={
                "challenge": step1["challenge"],
                "code": pyotp.TOTP(secret).now(),
            },
        )
        assert step2.status_code == 200, step2.text
        assert "token" in step2.json()

    def test_login_step2_wrong_code_fails(
        self,
        client: TestClient,
    ) -> None:
        token = _login_password(client, "alice", ALICE_PASSWORD)["token"]
        enroll = client.post("/api/users/me/totp/enroll", headers=_h(token)).json()
        secret = enroll["secret_base32"]
        client.post(
            "/api/users/me/totp/confirm",
            headers=_h(token),
            json={"code": pyotp.TOTP(secret).now()},
        )
        client.post("/api/auth/lock", headers=_h(token))

        step1 = client.post(
            "/api/auth/login",
            json={
                "username": "alice",
                "password": ALICE_PASSWORD,
            },
        ).json()
        bad = client.post(
            "/api/auth/login/totp",
            json={
                "challenge": step1["challenge"],
                "code": "000000",
            },
        )
        assert bad.status_code == 401

    def test_login_step2_backup_code_works_and_consumes(
        self,
        client: TestClient,
    ) -> None:
        token = _login_password(client, "alice", ALICE_PASSWORD)["token"]
        enroll = client.post("/api/users/me/totp/enroll", headers=_h(token)).json()
        secret = enroll["secret_base32"]
        confirm = client.post(
            "/api/users/me/totp/confirm",
            headers=_h(token),
            json={"code": pyotp.TOTP(secret).now()},
        ).json()
        codes = confirm["backup_codes"]
        client.post("/api/auth/lock", headers=_h(token))

        # Step 2 mit Backup-Code
        step1 = client.post(
            "/api/auth/login",
            json={
                "username": "alice",
                "password": ALICE_PASSWORD,
            },
        ).json()
        step2 = client.post(
            "/api/auth/login/totp",
            json={
                "challenge": step1["challenge"],
                "code": codes[0],
            },
        )
        assert step2.status_code == 200
        # Codes-Remaining ist 7
        new_token = step2.json()["token"]
        status = client.get("/api/users/me/totp", headers=_h(new_token))
        assert status.json()["backup_codes_remaining"] == 7

        # Selber Code ein zweites Mal → 401 (wurde verbraucht)
        step1b = client.post(
            "/api/auth/login",
            json={
                "username": "alice",
                "password": ALICE_PASSWORD,
            },
        ).json()
        replay = client.post(
            "/api/auth/login/totp",
            json={
                "challenge": step1b["challenge"],
                "code": codes[0],
            },
        )
        assert replay.status_code == 401


class TestSelfDisable:
    def test_disable_requires_password_and_code(
        self,
        client: TestClient,
    ) -> None:
        token = _login_password(client, "alice", ALICE_PASSWORD)["token"]
        enroll = client.post("/api/users/me/totp/enroll", headers=_h(token)).json()
        secret = enroll["secret_base32"]
        client.post(
            "/api/users/me/totp/confirm",
            headers=_h(token),
            json={"code": pyotp.TOTP(secret).now()},
        )

        # Falsches Passwort
        r1 = client.post(
            "/api/users/me/totp/disable",
            headers=_h(token),
            json={
                "current_password": "FALSCH",
                "code": pyotp.TOTP(secret).now(),
            },
        )
        assert r1.status_code == 401

        # Falscher Code
        r2 = client.post(
            "/api/users/me/totp/disable",
            headers=_h(token),
            json={
                "current_password": ALICE_PASSWORD,
                "code": "000000",
            },
        )
        assert r2.status_code == 401

        # Korrekt: 204
        r3 = client.post(
            "/api/users/me/totp/disable",
            headers=_h(token),
            json={
                "current_password": ALICE_PASSWORD,
                "code": pyotp.TOTP(secret).now(),
            },
        )
        assert r3.status_code == 204
        status = client.get("/api/users/me/totp", headers=_h(token))
        assert status.json()["enabled"] is False


class TestAdminReset:
    def test_admin_can_reset_other_users_totp(
        self,
        client: TestClient,
    ) -> None:
        alice_token = _login_password(client, "alice", ALICE_PASSWORD)["token"]
        # alice legt bob an
        bob_pw = "bob-passwort-mit-genug-zeichen"
        client.post(
            "/api/users",
            headers=_h(alice_token),
            json={
                "username": "bob",
                "password": bob_pw,
                "role": "operator",
                "allowed_tags": [],
            },
        )
        # bob aktiviert TOTP
        bob_token = _login_password(client, "bob", bob_pw)["token"]
        enroll = client.post("/api/users/me/totp/enroll", headers=_h(bob_token)).json()
        client.post(
            "/api/users/me/totp/confirm",
            headers=_h(bob_token),
            json={"code": pyotp.TOTP(enroll["secret_base32"]).now()},
        )
        # alice resettet bobs TOTP
        bob_id = next(
            u["id"]
            for u in client.get("/api/users", headers=_h(alice_token)).json()["users"]
            if u["username"] == "bob"
        )
        r = client.post(
            f"/api/users/{bob_id}/totp/disable",
            headers=_h(alice_token),
        )
        assert r.status_code == 204
        # bob kann jetzt wieder ohne TOTP einloggen
        relogin = client.post(
            "/api/auth/login",
            json={
                "username": "bob",
                "password": bob_pw,
            },
        )
        assert relogin.status_code == 200
        assert "token" in relogin.json()  # nicht totp_required
