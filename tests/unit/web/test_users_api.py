"""Tests fuer die User-Verwaltungs-Routen (admin-only)."""

from __future__ import annotations

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
ADMIN_PASSWORD = "admin-passwort-mit-genug-zeichen"
USER_PASSWORD = "user-passwort-mit-genug-zeichen"


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
def multi_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
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
    client = TestClient(create_app())
    server: ServerState = client.app.state.server_state
    server.bootstrap_create_admin("alice", ADMIN_PASSWORD)
    server.bootstrap_unlock_vault(vault_path, VAULT_PASSWORD)
    yield client


@pytest.fixture()
def single_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("OPNCOCKPIT_DATA_DIR", str(data))
    monkeypatch.delenv(VAULT_PATH_ENV, raising=False)
    monkeypatch.delenv("OPNCOCKPIT_AUTH_BACKEND", raising=False)
    yield TestClient(create_app())


def _login(client: TestClient, username: str, password: str) -> str:
    response = client.post("/api/auth/login", json={
        "username": username, "password": password,
    })
    assert response.status_code == 200, response.text
    return str(response.json()["token"])


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class TestPermissions:
    def test_single_user_mode_blocks_all_user_routes(
        self, single_client: TestClient, tmp_path: Path,
    ) -> None:
        # Single-Mode: ohne Token = 401, mit Vault-Token = 403 (kein Admin)
        path = _make_vault(tmp_path)
        unlock = single_client.post("/api/auth/unlock", json={
            "vault_path": str(path), "password": VAULT_PASSWORD,
        })
        assert unlock.status_code == 200
        token = unlock.json()["token"]
        for url in (
            "/api/users",
        ):
            response = single_client.get(url, headers=_h(token))
            assert response.status_code == 403

    def test_unauthorized_returns_401(self, multi_client: TestClient) -> None:
        assert multi_client.get("/api/users").status_code == 401

    def test_viewer_cannot_list_users(self, multi_client: TestClient) -> None:
        admin_token = _login(multi_client, "alice", ADMIN_PASSWORD)
        multi_client.post("/api/users", headers=_h(admin_token), json={
            "username": "bob", "password": USER_PASSWORD, "role": "viewer",
        })
        bob_token = _login(multi_client, "bob", USER_PASSWORD)
        response = multi_client.get("/api/users", headers=_h(bob_token))
        assert response.status_code == 403


class TestCrud:
    def test_admin_can_list_users(self, multi_client: TestClient) -> None:
        token = _login(multi_client, "alice", ADMIN_PASSWORD)
        response = multi_client.get("/api/users", headers=_h(token))
        assert response.status_code == 200
        body = response.json()
        assert len(body["users"]) == 1
        assert body["users"][0]["username"] == "alice"
        assert body["users"][0]["role"] == "admin"

    def test_admin_can_create_user(self, multi_client: TestClient) -> None:
        token = _login(multi_client, "alice", ADMIN_PASSWORD)
        response = multi_client.post(
            "/api/users", headers=_h(token),
            json={
                "username": "bob",
                "password": USER_PASSWORD,
                "role": "operator",
                "allowed_tags": ["branches"],
            },
        )
        assert response.status_code == 201
        body = response.json()
        assert body["username"] == "bob"
        assert body["role"] == "operator"
        assert body["allowed_tags"] == ["branches"]

    def test_duplicate_username_returns_409(
        self, multi_client: TestClient,
    ) -> None:
        token = _login(multi_client, "alice", ADMIN_PASSWORD)
        multi_client.post(
            "/api/users", headers=_h(token),
            json={
                "username": "bob",
                "password": USER_PASSWORD,
                "role": "viewer",
            },
        )
        dup = multi_client.post(
            "/api/users", headers=_h(token),
            json={
                "username": "bob",
                "password": USER_PASSWORD,
                "role": "viewer",
            },
        )
        assert dup.status_code == 409

    def test_short_password_rejected(self, multi_client: TestClient) -> None:
        token = _login(multi_client, "alice", ADMIN_PASSWORD)
        response = multi_client.post(
            "/api/users", headers=_h(token),
            json={
                "username": "bob", "password": "zu-kurz", "role": "viewer",
            },
        )
        assert response.status_code == 422

    def test_admin_can_update_role(self, multi_client: TestClient) -> None:
        token = _login(multi_client, "alice", ADMIN_PASSWORD)
        created = multi_client.post(
            "/api/users", headers=_h(token),
            json={
                "username": "bob", "password": USER_PASSWORD, "role": "viewer",
            },
        ).json()
        update = multi_client.patch(
            f"/api/users/{created['id']}", headers=_h(token),
            json={"role": "operator"},
        )
        assert update.status_code == 200
        assert update.json()["role"] == "operator"

    def test_admin_can_disable_other_user(
        self, multi_client: TestClient,
    ) -> None:
        token = _login(multi_client, "alice", ADMIN_PASSWORD)
        created = multi_client.post(
            "/api/users", headers=_h(token),
            json={"username": "bob", "password": USER_PASSWORD, "role": "viewer"},
        ).json()
        disable = multi_client.patch(
            f"/api/users/{created['id']}", headers=_h(token),
            json={"disabled": True},
        )
        assert disable.status_code == 200
        assert disable.json()["disabled"] is True

    def test_admin_can_delete_user(self, multi_client: TestClient) -> None:
        token = _login(multi_client, "alice", ADMIN_PASSWORD)
        created = multi_client.post(
            "/api/users", headers=_h(token),
            json={"username": "bob", "password": USER_PASSWORD, "role": "viewer"},
        ).json()
        response = multi_client.delete(
            f"/api/users/{created['id']}", headers=_h(token),
        )
        assert response.status_code == 204
        listed = multi_client.get("/api/users", headers=_h(token)).json()
        usernames = [u["username"] for u in listed["users"]]
        assert "bob" not in usernames


class TestSelfProtection:
    def test_admin_cannot_self_delete(self, multi_client: TestClient) -> None:
        token = _login(multi_client, "alice", ADMIN_PASSWORD)
        me = multi_client.get("/api/users", headers=_h(token)).json()
        alice_id = me["users"][0]["id"]
        response = multi_client.delete(
            f"/api/users/{alice_id}", headers=_h(token),
        )
        assert response.status_code == 400
        assert "selbst" in response.json()["detail"].lower()

    def test_admin_cannot_self_disable(self, multi_client: TestClient) -> None:
        token = _login(multi_client, "alice", ADMIN_PASSWORD)
        me = multi_client.get("/api/users", headers=_h(token)).json()
        alice_id = me["users"][0]["id"]
        response = multi_client.patch(
            f"/api/users/{alice_id}", headers=_h(token),
            json={"disabled": True},
        )
        assert response.status_code == 400

    def test_admin_cannot_drop_own_role(self, multi_client: TestClient) -> None:
        token = _login(multi_client, "alice", ADMIN_PASSWORD)
        alice_id = multi_client.get(
            "/api/users", headers=_h(token),
        ).json()["users"][0]["id"]
        response = multi_client.patch(
            f"/api/users/{alice_id}", headers=_h(token),
            json={"role": "viewer"},
        )
        assert response.status_code == 400

    def test_cannot_demote_last_admin_via_other_admin(
        self, multi_client: TestClient,
    ) -> None:
        """Bob ist auch Admin und versucht Alice zu degradieren — geht
        nur, wenn noch ein anderer aktiver Admin uebrig bleibt.
        """
        alice_token = _login(multi_client, "alice", ADMIN_PASSWORD)
        # Bob als zweiter Admin
        bob = multi_client.post(
            "/api/users", headers=_h(alice_token),
            json={"username": "bob", "password": USER_PASSWORD, "role": "admin"},
        ).json()
        bob_token = _login(multi_client, "bob", USER_PASSWORD)
        # Bob demoted Alice — sollte gehen, weil bob noch aktiv-Admin ist
        users = multi_client.get(
            "/api/users", headers=_h(bob_token),
        ).json()["users"]
        alice_id = next(u["id"] for u in users if u["username"] == "alice")
        response = multi_client.patch(
            f"/api/users/{alice_id}", headers=_h(bob_token),
            json={"role": "viewer"},
        )
        assert response.status_code == 200
        # Jetzt versucht Alice (als viewer) Bob zu degradieren — Permission denied
        response2 = multi_client.patch(
            f"/api/users/{bob['id']}", headers=_h(alice_token),
            json={"role": "viewer"},
        )
        # Alice ist seit dem Demote nur noch viewer in der DB, aber ihre
        # Session traegt noch die Admin-Role. Sie KANN technisch noch
        # mutieren, aber sie sollte den letzten aktiven Admin nicht
        # entfernen koennen.
        assert response2.status_code == 400


class TestPasswordOps:
    def test_admin_can_reset_user_password(
        self, multi_client: TestClient,
    ) -> None:
        token = _login(multi_client, "alice", ADMIN_PASSWORD)
        bob = multi_client.post(
            "/api/users", headers=_h(token),
            json={"username": "bob", "password": USER_PASSWORD, "role": "viewer"},
        ).json()
        new_pw = "neues-bob-passwort-12+"
        reset = multi_client.post(
            f"/api/users/{bob['id']}/password",
            headers=_h(token),
            json={"new_password": new_pw},
        )
        assert reset.status_code == 204
        # Altes PW funktioniert nicht mehr
        assert multi_client.post("/api/auth/login", json={
            "username": "bob", "password": USER_PASSWORD,
        }).status_code == 401
        # Neues geht
        assert multi_client.post("/api/auth/login", json={
            "username": "bob", "password": new_pw,
        }).status_code == 200

    def test_self_service_password_change(
        self, multi_client: TestClient,
    ) -> None:
        admin_token = _login(multi_client, "alice", ADMIN_PASSWORD)
        multi_client.post(
            "/api/users", headers=_h(admin_token),
            json={"username": "bob", "password": USER_PASSWORD, "role": "viewer"},
        )
        bob_token = _login(multi_client, "bob", USER_PASSWORD)
        new_pw = "bobs-neues-passwort-12+"
        change = multi_client.post(
            "/api/users/me/password",
            headers=_h(bob_token),
            json={
                "current_password": USER_PASSWORD,
                "new_password": new_pw,
            },
        )
        assert change.status_code == 204
        # Login mit neuem PW
        assert multi_client.post("/api/auth/login", json={
            "username": "bob", "password": new_pw,
        }).status_code == 200

    def test_self_service_wrong_current_password_rejected(
        self, multi_client: TestClient,
    ) -> None:
        admin_token = _login(multi_client, "alice", ADMIN_PASSWORD)
        multi_client.post(
            "/api/users", headers=_h(admin_token),
            json={"username": "bob", "password": USER_PASSWORD, "role": "viewer"},
        )
        bob_token = _login(multi_client, "bob", USER_PASSWORD)
        change = multi_client.post(
            "/api/users/me/password",
            headers=_h(bob_token),
            json={
                "current_password": "falsch-aber-12+",
                "new_password": "neues-pw-mit-genug-zeichen",
            },
        )
        assert change.status_code == 401
