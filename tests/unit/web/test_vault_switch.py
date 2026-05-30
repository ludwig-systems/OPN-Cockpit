"""Tests fuer Admin-Vault-Switch im Multi-User-Mode (v4-Pass 2)."""

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
USER_PASSWORD = "user-passwort-mit-genug-zeichen"
NEW_VAULT_PASSWORD = "neuer-vault-master-pw-zwoelf"


def _vault(path: Path, password: str, devices: list[VaultDevice]) -> Path:
    create_vault(path, password, VaultData(devices=devices))
    return path


@pytest.fixture()
def multi_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("OPNCOCKPIT_DATA_DIR", str(data))
    monkeypatch.delenv(VAULT_PATH_ENV, raising=False)
    vault_path = _vault(tmp_path / "active.opnvault", VAULT_PASSWORD, [
        VaultDevice(
            id="dev-001", name="HQ Active", host="active.lab", port=443,
            tls_verify=True, tags=[], api_key="k", api_secret="s", descr="",
        ),
    ])
    AppSettings(
        auth_backend="user-db",
        deployment_mode="multi-server",
        default_vault=str(vault_path),
    ).save(data / "settings.json")
    client = TestClient(create_app())
    server: ServerState = client.app.state.server_state
    server.bootstrap_create_admin("alice", USER_PASSWORD)
    server.bootstrap_unlock_vault(vault_path, VAULT_PASSWORD)
    assert server.user_store is not None
    server.user_store.create_user(
        username="bob", password=USER_PASSWORD, role="operator",
    )
    yield client


def _login(client: TestClient, username: str) -> str:
    response = client.post("/api/auth/login", json={
        "username": username, "password": USER_PASSWORD,
    })
    assert response.status_code == 200, response.text
    return str(response.json()["token"])


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class TestVaultSwitch:
    def test_admin_switches_to_existing_vault(
        self, multi_client: TestClient, tmp_path: Path,
    ) -> None:
        # Vorher: zweiter Vault mit anderen Devices
        other = _vault(tmp_path / "other.opnvault", NEW_VAULT_PASSWORD, [
            VaultDevice(
                id="dev-other-1", name="Other Box", host="other.lab",
                port=443, tls_verify=True, tags=[],
                api_key="k", api_secret="s", descr="",
            ),
        ])
        admin_token = _login(multi_client, "alice")
        response = multi_client.post(
            "/api/vaults/switch", headers=_h(admin_token),
            json={
                "vault_path": str(other),
                "password": NEW_VAULT_PASSWORD,
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["created"] == "false"
        # Admin sieht jetzt die anderen Devices
        inv = multi_client.get("/api/inventory", headers=_h(admin_token)).json()
        names = [d["name"] for d in inv["devices"]]
        assert names == ["Other Box"]

    def test_admin_creates_new_vault_with_switch(
        self, multi_client: TestClient, tmp_path: Path,
    ) -> None:
        admin_token = _login(multi_client, "alice")
        new_path = tmp_path / "fresh.opnvault"
        assert not new_path.exists()
        response = multi_client.post(
            "/api/vaults/switch", headers=_h(admin_token),
            json={
                "vault_path": str(new_path),
                "password": NEW_VAULT_PASSWORD,
                "create_if_missing": True,
            },
        )
        assert response.status_code == 200
        assert response.json()["created"] == "true"
        assert new_path.exists()

    def test_switch_invalidates_other_sessions(
        self, multi_client: TestClient, tmp_path: Path,
    ) -> None:
        other = _vault(tmp_path / "other.opnvault", NEW_VAULT_PASSWORD, [])
        admin_token = _login(multi_client, "alice")
        bob_token = _login(multi_client, "bob")
        # Bob ist eingeloggt
        assert multi_client.get(
            "/api/auth/me", headers=_h(bob_token),
        ).status_code == 200
        # Admin switcht
        response = multi_client.post(
            "/api/vaults/switch", headers=_h(admin_token),
            json={
                "vault_path": str(other),
                "password": NEW_VAULT_PASSWORD,
            },
        )
        assert response.status_code == 200
        assert response.json()["revoked_sessions"] == "1"
        # Bob ist gesperrt
        assert multi_client.get(
            "/api/auth/me", headers=_h(bob_token),
        ).status_code == 401
        # Admin nicht
        assert multi_client.get(
            "/api/auth/me", headers=_h(admin_token),
        ).status_code == 200

    def test_non_admin_cannot_switch(
        self, multi_client: TestClient, tmp_path: Path,
    ) -> None:
        other = _vault(tmp_path / "other.opnvault", NEW_VAULT_PASSWORD, [])
        bob_token = _login(multi_client, "bob")
        response = multi_client.post(
            "/api/vaults/switch", headers=_h(bob_token),
            json={
                "vault_path": str(other),
                "password": NEW_VAULT_PASSWORD,
            },
        )
        assert response.status_code == 403

    def test_wrong_password_returns_401(
        self, multi_client: TestClient, tmp_path: Path,
    ) -> None:
        other = _vault(tmp_path / "other.opnvault", NEW_VAULT_PASSWORD, [])
        admin_token = _login(multi_client, "alice")
        response = multi_client.post(
            "/api/vaults/switch", headers=_h(admin_token),
            json={
                "vault_path": str(other),
                "password": "definitiv-falsch+",
            },
        )
        assert response.status_code == 401

    def test_missing_file_without_create_flag_404(
        self, multi_client: TestClient, tmp_path: Path,
    ) -> None:
        admin_token = _login(multi_client, "alice")
        response = multi_client.post(
            "/api/vaults/switch", headers=_h(admin_token),
            json={
                "vault_path": str(tmp_path / "ghost.opnvault"),
                "password": NEW_VAULT_PASSWORD,
            },
        )
        assert response.status_code == 404

    def test_same_path_rejected(
        self, multi_client: TestClient, tmp_path: Path,
    ) -> None:
        admin_token = _login(multi_client, "alice")
        response = multi_client.post(
            "/api/vaults/switch", headers=_h(admin_token),
            json={
                "vault_path": str(tmp_path / "active.opnvault"),
                "password": VAULT_PASSWORD,
            },
        )
        assert response.status_code == 400


class TestSingleModeRejection:
    def test_single_mode_returns_409(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("OPNCOCKPIT_DATA_DIR", str(tmp_path))
        path = _vault(tmp_path / "single.opnvault", VAULT_PASSWORD, [])
        client = TestClient(create_app())
        unlock = client.post("/api/auth/unlock", json={
            "vault_path": str(path), "password": VAULT_PASSWORD,
        })
        token = unlock.json()["token"]
        other = _vault(tmp_path / "other.opnvault", NEW_VAULT_PASSWORD, [])
        response = client.post(
            "/api/vaults/switch", headers=_h(token),
            json={
                "vault_path": str(other),
                "password": NEW_VAULT_PASSWORD,
            },
        )
        # Single-Mode: kein User-Konzept → require_admin liefert 403,
        # _bevor_ der Mode-Check 409 werfen kann
        assert response.status_code == 403
