"""End-to-End-Tests fuer Bootstrap-Flow und Multi-User-Login.

Seit F28 (2026-06-01): kein Bootstrap-Token mehr, Server legt einen
Default-Admin (`admin` / `OPN-Cockpit!`) mit Pflicht-PW-Wechsel an.
Bootstrap-Vault prueft Admin-Username + -Passwort direkt aus der User-DB.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from opn_cockpit.config import AppSettings
from opn_cockpit.vault.model import VaultData, VaultDevice
from opn_cockpit.vault.store import create_vault
from opn_cockpit.web.server import create_app
from opn_cockpit.web.server_state import (
    DEFAULT_ADMIN_PASSWORD,
    DEFAULT_ADMIN_USERNAME,
    VAULT_PATH_ENV,
    ServerState,
)

VAULT_PASSWORD = "korrektes-pferd-batterie-heftklammer"
NEW_ADMIN_PW = "frisches-admin-passwort-12+"


def _make_vault(tmp_path: Path) -> Path:
    path = tmp_path / "shared.opnvault"
    devices = [
        VaultDevice(
            id="dev-001", name="HQ Berlin", host="opn-1.lab", port=443,
            tls_verify=True, tags=[], api_key="k", api_secret="s", descr="",
        ),
    ]
    create_vault(path, VAULT_PASSWORD, VaultData(devices=devices))
    return path


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Isoliert ``OPNCOCKPIT_DATA_DIR`` pro Test (UserStore + settings.json)."""
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("OPNCOCKPIT_DATA_DIR", str(data))
    monkeypatch.delenv(VAULT_PATH_ENV, raising=False)
    yield data


@pytest.fixture()
def single_client(data_dir: Path) -> TestClient:
    """Default-Single-User-Mode (auth_backend=vault)."""
    return TestClient(create_app())


@pytest.fixture()
def multi_client_factory(data_dir: Path):
    """Factory: instanziiert Multi-User-Server mit optionalem Pre-Setup.

    ``admin_pw_changed=True`` wechselt direkt nach Server-Start das
    Default-PW auf NEW_ADMIN_PW — fuer Tests die schon "nach Erst-Setup"
    operieren.
    """

    def _make(
        *,
        vault_path: Path | None = None,
        admin_pw_changed: bool = False,
        vault_pre_unlocked: bool = False,
        env_vault_path: Path | None = None,
    ) -> TestClient:
        settings_path = data_dir / "settings.json"
        AppSettings(
            auth_backend="user-db",
            deployment_mode="multi-server",
            default_vault=str(vault_path) if vault_path else None,
        ).save(settings_path)
        if env_vault_path is not None:
            os.environ[VAULT_PATH_ENV] = str(env_vault_path)
        client = TestClient(create_app())
        server: ServerState = client.app.state.server_state
        if admin_pw_changed:
            assert server.user_store is not None
            default_user = server.user_store.get_user_by_name(DEFAULT_ADMIN_USERNAME)
            assert default_user is not None
            server.user_store.change_password(default_user.id, NEW_ADMIN_PW)
        if vault_pre_unlocked:
            assert vault_path is not None
            server.bootstrap_unlock_vault(vault_path, VAULT_PASSWORD)
        return client

    return _make


# ---------------------------------------------------------------------------
# Status-Endpoint
# ---------------------------------------------------------------------------


class TestBootstrapStatus:
    def test_single_user_mode_reports_single_user(
        self, single_client: TestClient,
    ) -> None:
        response = single_client.get("/api/bootstrap/status")
        assert response.status_code == 200
        body = response.json()
        assert body["mode"] == "vault"
        assert body["status"] == "single-user"

    def test_multi_mode_starts_needs_vault_unlock(
        self, multi_client_factory, tmp_path: Path,
    ) -> None:
        """Default-Admin wird automatisch angelegt, Server springt direkt
        auf needs-vault-unlock (kein needs-admin-Step mehr)."""
        path = _make_vault(tmp_path)
        client = multi_client_factory(vault_path=path)
        response = client.get("/api/bootstrap/status")
        body = response.json()
        assert body["mode"] == "user-db"
        assert body["status"] == "needs-vault-unlock"
        assert body["suggested_vault_path"] == str(path)

    def test_multi_mode_suggests_env_path(
        self, multi_client_factory, tmp_path: Path,
    ) -> None:
        path = _make_vault(tmp_path)
        client = multi_client_factory(env_vault_path=path)
        body = client.get("/api/bootstrap/status").json()
        assert body["suggested_vault_path"] == str(path)


# ---------------------------------------------------------------------------
# Default-Admin (F28)
# ---------------------------------------------------------------------------


class TestDefaultAdmin:
    def test_default_admin_is_auto_created_with_must_change_flag(
        self, multi_client_factory, tmp_path: Path,
    ) -> None:
        client = multi_client_factory(vault_path=_make_vault(tmp_path))
        server: ServerState = client.app.state.server_state
        assert server.user_store is not None
        user = server.user_store.get_user_by_name(DEFAULT_ADMIN_USERNAME)
        assert user is not None
        assert user.role == "admin"
        assert user.must_change_password is True

    def test_default_admin_not_recreated_after_pw_change(
        self, multi_client_factory, tmp_path: Path,
    ) -> None:
        client = multi_client_factory(
            vault_path=_make_vault(tmp_path), admin_pw_changed=True,
        )
        server: ServerState = client.app.state.server_state
        assert server.user_store is not None
        user = server.user_store.get_user_by_name(DEFAULT_ADMIN_USERNAME)
        assert user is not None
        assert user.must_change_password is False


# ---------------------------------------------------------------------------
# Bootstrap-Admin-Endpoint ist Legacy (410 Gone)
# ---------------------------------------------------------------------------


class TestBootstrapAdminLegacy:
    def test_admin_endpoint_returns_410_gone(
        self, multi_client_factory, tmp_path: Path,
    ) -> None:
        client = multi_client_factory(vault_path=_make_vault(tmp_path))
        response = client.post(
            "/api/bootstrap/admin",
            json={"username": "alice", "password": "egal-irgendwas-12+"},
        )
        assert response.status_code == 410
        assert "Default-Admin" in response.json()["detail"]


# ---------------------------------------------------------------------------
# Bootstrap-Vault (F28 — kombiniert Admin-Login + PW-Wechsel + Vault-Unlock)
# ---------------------------------------------------------------------------


class TestBootstrapVault:
    def test_first_run_unlocks_with_default_admin_and_changes_pw(
        self, multi_client_factory, tmp_path: Path,
    ) -> None:
        path = _make_vault(tmp_path)
        client = multi_client_factory(vault_path=path)
        response = client.post(
            "/api/bootstrap/vault",
            json={
                "vault_path": str(path),
                "password": VAULT_PASSWORD,
                "admin_username": DEFAULT_ADMIN_USERNAME,
                "admin_password": DEFAULT_ADMIN_PASSWORD,
                "new_admin_password": NEW_ADMIN_PW,
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "ready"
        # Default-PW wurde gewechselt — must_change_password ist False.
        server: ServerState = client.app.state.server_state
        assert server.user_store is not None
        user = server.user_store.get_user_by_name(DEFAULT_ADMIN_USERNAME)
        assert user is not None
        assert user.must_change_password is False

    def test_must_change_flag_blocks_without_new_password(
        self, multi_client_factory, tmp_path: Path,
    ) -> None:
        path = _make_vault(tmp_path)
        client = multi_client_factory(vault_path=path)
        response = client.post(
            "/api/bootstrap/vault",
            json={
                "vault_path": str(path),
                "password": VAULT_PASSWORD,
                "admin_username": DEFAULT_ADMIN_USERNAME,
                "admin_password": DEFAULT_ADMIN_PASSWORD,
            },
        )
        assert response.status_code == 403
        assert "Default-Admin-Passwort" in response.json()["detail"]

    def test_new_pw_must_differ_from_default(
        self, multi_client_factory, tmp_path: Path,
    ) -> None:
        path = _make_vault(tmp_path)
        client = multi_client_factory(vault_path=path)
        response = client.post(
            "/api/bootstrap/vault",
            json={
                "vault_path": str(path),
                "password": VAULT_PASSWORD,
                "admin_username": DEFAULT_ADMIN_USERNAME,
                "admin_password": DEFAULT_ADMIN_PASSWORD,
                "new_admin_password": DEFAULT_ADMIN_PASSWORD,
            },
        )
        assert response.status_code == 400

    def test_already_changed_pw_can_skip_new_password(
        self, multi_client_factory, tmp_path: Path,
    ) -> None:
        """Wenn Admin sein PW schon gewechselt hat (z. B. Server-Restart
        nach erstem Setup), darf der Unlock ohne new_admin_password
        durchlaufen."""
        path = _make_vault(tmp_path)
        client = multi_client_factory(vault_path=path, admin_pw_changed=True)
        response = client.post(
            "/api/bootstrap/vault",
            json={
                "vault_path": str(path),
                "password": VAULT_PASSWORD,
                "admin_username": DEFAULT_ADMIN_USERNAME,
                "admin_password": NEW_ADMIN_PW,
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "ready"

    def test_wrong_admin_password_returns_401(
        self, multi_client_factory, tmp_path: Path,
    ) -> None:
        path = _make_vault(tmp_path)
        client = multi_client_factory(vault_path=path)
        response = client.post(
            "/api/bootstrap/vault",
            json={
                "vault_path": str(path),
                "password": VAULT_PASSWORD,
                "admin_username": DEFAULT_ADMIN_USERNAME,
                "admin_password": "ist-bestimmt-falsch-12+",
                "new_admin_password": NEW_ADMIN_PW,
            },
        )
        assert response.status_code == 401

    def test_wrong_vault_password_returns_401(
        self, multi_client_factory, tmp_path: Path,
    ) -> None:
        path = _make_vault(tmp_path)
        client = multi_client_factory(vault_path=path, admin_pw_changed=True)
        response = client.post(
            "/api/bootstrap/vault",
            json={
                "vault_path": str(path),
                "password": "falsches-vault-passwort-12+",
                "admin_username": DEFAULT_ADMIN_USERNAME,
                "admin_password": NEW_ADMIN_PW,
            },
        )
        assert response.status_code == 401

    def test_missing_vault_returns_404_without_create_flag(
        self, multi_client_factory, tmp_path: Path,
    ) -> None:
        path = _make_vault(tmp_path)
        client = multi_client_factory(vault_path=path, admin_pw_changed=True)
        response = client.post(
            "/api/bootstrap/vault",
            json={
                "vault_path": str(tmp_path / "nope.opnvault"),
                "password": VAULT_PASSWORD,
                "admin_username": DEFAULT_ADMIN_USERNAME,
                "admin_password": NEW_ADMIN_PW,
            },
        )
        assert response.status_code == 404

    def test_creates_new_vault_when_flag_set(
        self, multi_client_factory, tmp_path: Path,
    ) -> None:
        """Erstinstallations-Pfad: kein Vault da, Server legt ihn an."""
        suggested = _make_vault(tmp_path)
        client = multi_client_factory(vault_path=suggested, admin_pw_changed=True)
        new_path = tmp_path / "fresh.opnvault"
        assert not new_path.exists()
        response = client.post(
            "/api/bootstrap/vault",
            json={
                "vault_path": str(new_path),
                "password": "frischer-master-pw-12+",
                "create_if_missing": True,
                "admin_username": DEFAULT_ADMIN_USERNAME,
                "admin_password": NEW_ADMIN_PW,
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "ready"
        assert body["created"] == "true"
        assert new_path.exists()


# ---------------------------------------------------------------------------
# Multi-User-Login (nach Vault-Unlock)
# ---------------------------------------------------------------------------


class TestMultiUserLogin:
    def test_default_admin_can_log_in_after_first_setup(
        self, multi_client_factory, tmp_path: Path,
    ) -> None:
        path = _make_vault(tmp_path)
        client = multi_client_factory(
            vault_path=path,
            admin_pw_changed=True,
            vault_pre_unlocked=True,
        )
        response = client.post("/api/auth/login", json={
            "username": DEFAULT_ADMIN_USERNAME,
            "password": NEW_ADMIN_PW,
        })
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["token"]
        assert body["vault_filename"] == "shared.opnvault"

    def test_wrong_password_returns_401(
        self, multi_client_factory, tmp_path: Path,
    ) -> None:
        path = _make_vault(tmp_path)
        client = multi_client_factory(
            vault_path=path,
            admin_pw_changed=True,
            vault_pre_unlocked=True,
        )
        response = client.post("/api/auth/login", json={
            "username": DEFAULT_ADMIN_USERNAME,
            "password": "wrong-pw-but-12-chars-long",
        })
        assert response.status_code == 401

    def test_token_works_for_me_endpoint(
        self, multi_client_factory, tmp_path: Path,
    ) -> None:
        path = _make_vault(tmp_path)
        client = multi_client_factory(
            vault_path=path,
            admin_pw_changed=True,
            vault_pre_unlocked=True,
        )
        login = client.post("/api/auth/login", json={
            "username": DEFAULT_ADMIN_USERNAME,
            "password": NEW_ADMIN_PW,
        })
        token = login.json()["token"]
        me = client.get(
            "/api/auth/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert me.status_code == 200
