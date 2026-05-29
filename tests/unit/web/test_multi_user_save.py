"""Multi-User-Spezifika: zentraler Save-Vault + Session-Synchronisation."""

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
) -> Iterator[tuple[TestClient, Path]]:
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("OPNCOCKPIT_DATA_DIR", str(data))
    monkeypatch.delenv(VAULT_PATH_ENV, raising=False)
    vault_path = _make_vault(tmp_path)
    settings_path = data / "settings.json"
    AppSettings(
        auth_backend="user-db",
        deployment_mode="multi-server",
        default_vault=str(vault_path),
    ).save(settings_path)
    client = TestClient(create_app())
    server: ServerState = client.app.state.server_state
    server.bootstrap_create_admin("alice", USER_PASSWORD)
    server.bootstrap_unlock_vault(vault_path, VAULT_PASSWORD)
    assert server.user_store is not None
    server.user_store.create_user(
        username="bob", password=USER_PASSWORD, role="operator",
    )
    yield client, vault_path


def _login(client: TestClient, username: str) -> str:
    response = client.post("/api/auth/login", json={
        "username": username, "password": USER_PASSWORD,
    })
    assert response.status_code == 200
    return str(response.json()["token"])


class TestMultiUserSharedInventory:
    def test_both_users_see_same_inventory(
        self, multi_client: tuple[TestClient, Path],
    ) -> None:
        client, _ = multi_client
        alice = _login(client, "alice")
        bob = _login(client, "bob")
        for tok in (alice, bob):
            response = client.get(
                "/api/inventory",
                headers={"Authorization": f"Bearer {tok}"},
            )
            assert response.status_code == 200
            names = [d["name"] for d in response.json()["devices"]]
            assert names == ["HQ Berlin"]

    def test_alice_write_visible_to_bob(
        self, multi_client: tuple[TestClient, Path],
    ) -> None:
        """Wenn Alice ein Geraet anlegt, sieht Bob es sofort."""
        client, _ = multi_client
        alice = _login(client, "alice")
        bob = _login(client, "bob")

        # Alice fuegt hinzu.
        add = client.post(
            "/api/inventory/devices",
            headers={"Authorization": f"Bearer {alice}"},
            json={
                "name": "Branch Munich",
                "host": "opn-2.lab",
                "port": 443,
                "tls_verify": True,
                "tags": ["branches"],
                "descr": "",
                "api_key": "k2",
                "api_secret": "s2",
            },
        )
        assert add.status_code == 201

        # Bob sieht es ohne Re-Login.
        inv = client.get(
            "/api/inventory",
            headers={"Authorization": f"Bearer {bob}"},
        ).json()
        names = sorted(d["name"] for d in inv["devices"])
        assert names == ["Branch Munich", "HQ Berlin"]

    def test_consecutive_writes_from_both_users_succeed(
        self, multi_client: tuple[TestClient, Path],
    ) -> None:
        """Header-Nonce wird zentral aktualisiert — beide Save-Pfade laufen
        nacheinander durch, ohne dass der zweite mit Header-Mismatch fehlschlaegt.
        """
        client, _ = multi_client
        alice = _login(client, "alice")
        bob = _login(client, "bob")

        r1 = client.post(
            "/api/inventory/devices",
            headers={"Authorization": f"Bearer {alice}"},
            json={
                "name": "A1", "host": "a1.lab", "port": 443,
                "tls_verify": True, "tags": [], "descr": "",
                "api_key": "k", "api_secret": "s",
            },
        )
        assert r1.status_code == 201

        r2 = client.post(
            "/api/inventory/devices",
            headers={"Authorization": f"Bearer {bob}"},
            json={
                "name": "B1", "host": "b1.lab", "port": 443,
                "tls_verify": True, "tags": [], "descr": "",
                "api_key": "k", "api_secret": "s",
            },
        )
        assert r2.status_code == 201

        inv = client.get(
            "/api/inventory",
            headers={"Authorization": f"Bearer {alice}"},
        ).json()
        names = sorted(d["name"] for d in inv["devices"])
        assert names == ["A1", "B1", "HQ Berlin"]


class TestSingleUserUnaffected:
    """Sicherheitsnetz: Single-User-Pfad funktioniert unveraendert."""

    def test_single_user_add_and_get(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        data = tmp_path / "data"
        data.mkdir()
        monkeypatch.setenv("OPNCOCKPIT_DATA_DIR", str(data))
        vault_path = _make_vault(tmp_path)
        client = TestClient(create_app())

        unlock = client.post("/api/auth/unlock", json={
            "vault_path": str(vault_path), "password": VAULT_PASSWORD,
        })
        assert unlock.status_code == 200
        token = unlock.json()["token"]

        add = client.post(
            "/api/inventory/devices",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "name": "Solo", "host": "s.lab", "port": 443,
                "tls_verify": True, "tags": [], "descr": "",
                "api_key": "k", "api_secret": "s",
            },
        )
        assert add.status_code == 201
        inv = client.get(
            "/api/inventory",
            headers={"Authorization": f"Bearer {token}"},
        ).json()
        names = sorted(d["name"] for d in inv["devices"])
        assert names == ["HQ Berlin", "Solo"]
