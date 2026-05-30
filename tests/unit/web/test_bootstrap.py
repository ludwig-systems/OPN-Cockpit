"""End-to-End-Tests fuer Bootstrap-Flow und Multi-User-Login."""

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
from opn_cockpit.web.server_state import VAULT_PATH_ENV, ServerState

VAULT_PASSWORD = "korrektes-pferd-batterie-heftklammer"
USER_PASSWORD = "user-passwort-mit-genug-zeichen"


def _bootstrap_headers(client: TestClient) -> dict[str, str]:
    """Holt den aktuellen Bootstrap-Token aus der Server-State als Header.

    Tests muessen den Token kennen — im Production-Code laesst ihn der
    Server beim Start in stderr (siehe Audit #5).
    """
    server: ServerState = client.app.state.server_state
    token = server.bootstrap_token or ""
    return {"X-Bootstrap-Token": token}


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
    """Factory: instanziiert Multi-User-Server mit optionalem Pre-Setup."""

    def _make(
        *,
        vault_path: Path | None = None,
        admin_pre_created: bool = False,
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
        if admin_pre_created:
            server: ServerState = client.app.state.server_state
            server.bootstrap_create_admin("alice", USER_PASSWORD)
        if vault_pre_unlocked:
            assert vault_path is not None
            server2: ServerState = client.app.state.server_state
            server2.bootstrap_unlock_vault(vault_path, VAULT_PASSWORD)
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

    def test_multi_mode_starts_needs_admin(
        self, multi_client_factory, tmp_path: Path,
    ) -> None:
        path = _make_vault(tmp_path)
        client = multi_client_factory(vault_path=path)
        response = client.get("/api/bootstrap/status")
        body = response.json()
        assert body["mode"] == "user-db"
        assert body["status"] == "needs-admin"
        assert body["suggested_vault_path"] == str(path)

    def test_multi_mode_suggests_env_path(
        self, multi_client_factory, tmp_path: Path,
    ) -> None:
        path = _make_vault(tmp_path)
        client = multi_client_factory(env_vault_path=path)
        body = client.get("/api/bootstrap/status").json()
        assert body["suggested_vault_path"] == str(path)


# ---------------------------------------------------------------------------
# Bootstrap-Admin
# ---------------------------------------------------------------------------


class TestBootstrapAdmin:
    def test_creates_first_admin(
        self, multi_client_factory, tmp_path: Path,
    ) -> None:
        path = _make_vault(tmp_path)
        client = multi_client_factory(vault_path=path)
        response = client.post(
            "/api/bootstrap/admin",
            headers=_bootstrap_headers(client),
            json={"username": "alice", "password": USER_PASSWORD},
        )
        assert response.status_code == 201
        assert response.json()["status"] == "needs-vault-unlock"

    def test_short_password_rejected(
        self, multi_client_factory, tmp_path: Path,
    ) -> None:
        path = _make_vault(tmp_path)
        client = multi_client_factory(vault_path=path)
        response = client.post(
            "/api/bootstrap/admin",
            headers=_bootstrap_headers(client),
            json={"username": "alice", "password": "zu-kurz"},
        )
        # Pydantic validiert min_length=12 → 422.
        assert response.status_code == 422

    def test_second_admin_rejected_with_409(
        self, multi_client_factory, tmp_path: Path,
    ) -> None:
        path = _make_vault(tmp_path)
        client = multi_client_factory(
            vault_path=path, admin_pre_created=True,
        )
        response = client.post(
            "/api/bootstrap/admin",
            headers=_bootstrap_headers(client),
            json={"username": "bob", "password": USER_PASSWORD},
        )
        assert response.status_code == 409

    def test_admin_in_single_mode_rejected_with_409(
        self, single_client: TestClient,
    ) -> None:
        response = single_client.post("/api/bootstrap/admin", json={
            "username": "alice",
            "password": USER_PASSWORD,
        })
        assert response.status_code == 409


# ---------------------------------------------------------------------------
# Bootstrap-Vault
# ---------------------------------------------------------------------------


class TestBootstrapVault:
    def test_unlocks_central_vault(
        self, multi_client_factory, tmp_path: Path,
    ) -> None:
        path = _make_vault(tmp_path)
        client = multi_client_factory(
            vault_path=path, admin_pre_created=True,
        )
        response = client.post(
            "/api/bootstrap/vault",
            headers=_bootstrap_headers(client),
            json={"vault_path": str(path), "password": VAULT_PASSWORD},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "ready"

    def test_wrong_password_returns_401(
        self, multi_client_factory, tmp_path: Path,
    ) -> None:
        path = _make_vault(tmp_path)
        client = multi_client_factory(
            vault_path=path, admin_pre_created=True,
        )
        response = client.post(
            "/api/bootstrap/vault",
            headers=_bootstrap_headers(client),
            json={"vault_path": str(path), "password": "falsch-aber-12+"},
        )
        assert response.status_code == 401

    def test_missing_vault_returns_404_without_create_flag(
        self, multi_client_factory, tmp_path: Path,
    ) -> None:
        path = _make_vault(tmp_path)
        client = multi_client_factory(
            vault_path=path, admin_pre_created=True,
        )
        response = client.post(
            "/api/bootstrap/vault",
            headers=_bootstrap_headers(client),
            json={
                "vault_path": str(tmp_path / "nope.opnvault"),
                "password": VAULT_PASSWORD,
            },
        )
        assert response.status_code == 404

    def test_creates_new_vault_when_flag_set(
        self, multi_client_factory, tmp_path: Path,
    ) -> None:
        """UX: Erstinstallations-Pfad — neuer Server, kein Vault existiert."""
        # Vault-Pfad als suggested mitgeben; dann auf einem neuen Pfad anlegen
        suggested = _make_vault(tmp_path)
        client = multi_client_factory(
            vault_path=suggested, admin_pre_created=True,
        )
        new_path = tmp_path / "fresh.opnvault"
        assert not new_path.exists()
        response = client.post(
            "/api/bootstrap/vault",
            headers=_bootstrap_headers(client),
            json={
                "vault_path": str(new_path),
                "password": "frischer-master-pw-12+",
                "create_if_missing": True,
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "ready"
        assert body["created"] == "true"
        assert new_path.exists()

    def test_without_admin_returns_409(
        self, multi_client_factory, tmp_path: Path,
    ) -> None:
        path = _make_vault(tmp_path)
        client = multi_client_factory(vault_path=path)
        response = client.post(
            "/api/bootstrap/vault",
            headers=_bootstrap_headers(client),
            json={"vault_path": str(path), "password": VAULT_PASSWORD},
        )
        assert response.status_code == 409


# ---------------------------------------------------------------------------
# Multi-User-Login
# ---------------------------------------------------------------------------


class TestMultiUserLogin:
    def test_correct_credentials_return_token(
        self, multi_client_factory, tmp_path: Path,
    ) -> None:
        path = _make_vault(tmp_path)
        client = multi_client_factory(
            vault_path=path,
            admin_pre_created=True,
            vault_pre_unlocked=True,
        )
        response = client.post("/api/auth/login", json={
            "username": "alice",
            "password": USER_PASSWORD,
        })
        assert response.status_code == 200
        body = response.json()
        assert body["token"]
        assert body["vault_filename"] == "shared.opnvault"

    def test_wrong_password_returns_401(
        self, multi_client_factory, tmp_path: Path,
    ) -> None:
        path = _make_vault(tmp_path)
        client = multi_client_factory(
            vault_path=path,
            admin_pre_created=True,
            vault_pre_unlocked=True,
        )
        response = client.post("/api/auth/login", json={
            "username": "alice",
            "password": "falsches-passwort-12+",
        })
        assert response.status_code == 401

    def test_login_in_single_mode_returns_409(
        self, single_client: TestClient,
    ) -> None:
        response = single_client.post("/api/auth/login", json={
            "username": "alice",
            "password": USER_PASSWORD,
        })
        assert response.status_code == 409

    def test_login_before_ready_returns_409(
        self, multi_client_factory, tmp_path: Path,
    ) -> None:
        path = _make_vault(tmp_path)
        client = multi_client_factory(vault_path=path)  # noch needs-admin
        response = client.post("/api/auth/login", json={
            "username": "alice",
            "password": USER_PASSWORD,
        })
        assert response.status_code == 409

    def test_token_works_for_me_endpoint(
        self, multi_client_factory, tmp_path: Path,
    ) -> None:
        path = _make_vault(tmp_path)
        client = multi_client_factory(
            vault_path=path,
            admin_pre_created=True,
            vault_pre_unlocked=True,
        )
        login = client.post("/api/auth/login", json={
            "username": "alice",
            "password": USER_PASSWORD,
        }).json()
        me = client.get(
            "/api/auth/me",
            headers={"Authorization": f"Bearer {login['token']}"},
        )
        assert me.status_code == 200
        assert me.json()["vault_filename"] == "shared.opnvault"

    def test_two_users_share_same_vault(
        self, multi_client_factory, tmp_path: Path,
    ) -> None:
        path = _make_vault(tmp_path)
        client = multi_client_factory(
            vault_path=path,
            admin_pre_created=True,
            vault_pre_unlocked=True,
        )
        server: ServerState = client.app.state.server_state
        assert server.user_store is not None
        server.user_store.create_user(
            username="bob", password=USER_PASSWORD, role="viewer",
        )
        login_a = client.post("/api/auth/login", json={
            "username": "alice", "password": USER_PASSWORD,
        }).json()
        login_b = client.post("/api/auth/login", json={
            "username": "bob", "password": USER_PASSWORD,
        }).json()
        # Beide haben einen Token und sehen die gleichen Devices via /me.
        for tok in (login_a["token"], login_b["token"]):
            assert client.get(
                "/api/auth/me", headers={"Authorization": f"Bearer {tok}"},
            ).status_code == 200
