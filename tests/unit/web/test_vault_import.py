"""Tests fuer den Vault-Import-Endpunkt (Firewalls aus anderem .opnvault)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from opn_cockpit.vault.model import VaultData, VaultDevice
from opn_cockpit.vault.store import create_vault
from opn_cockpit.web.server import create_app

ACTIVE_PASSWORD = "aktiver-master-pw-12+"
SOURCE_PASSWORD = "quell-master-pw-zwoelf-zeichen"


def _vault(path: Path, password: str, devices: list[VaultDevice]) -> Path:
    create_vault(path, password, VaultData(devices=devices))
    return path


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("OPNCOCKPIT_DATA_DIR", str(tmp_path))
    return TestClient(create_app())


@pytest.fixture()
def active_token(client: TestClient, tmp_path: Path) -> str:
    path = _vault(tmp_path / "active.opnvault", ACTIVE_PASSWORD, [
        VaultDevice(
            id="dev-001", name="HQ Berlin", host="opn.lab", port=443,
            tls_verify=True, tags=[], api_key="k", api_secret="s", descr="",
        ),
    ])
    response = client.post("/api/auth/unlock", json={
        "vault_path": str(path), "password": ACTIVE_PASSWORD,
    })
    assert response.status_code == 200, response.text
    return str(response.json()["token"])


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class TestVaultImport:
    def test_imports_devices_from_source(
        self, client: TestClient, active_token: str, tmp_path: Path,
    ) -> None:
        source = _vault(tmp_path / "source.opnvault", SOURCE_PASSWORD, [
            VaultDevice(
                id="src-1", name="Branch Munich", host="opn2.lab", port=443,
                tls_verify=True, tags=["branches"],
                api_key="k", api_secret="s", descr="",
            ),
            VaultDevice(
                id="src-2", name="Wien Office", host="opn3.lab", port=443,
                tls_verify=True, tags=["austria"],
                api_key="k", api_secret="s", descr="",
            ),
        ])
        response = client.post(
            "/api/imports/vault", headers=_h(active_token),
            json={
                "source_path": str(source),
                "source_password": SOURCE_PASSWORD,
            },
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert len(body["added"]) == 2
        names = sorted(d["name"] for d in body["added"])
        assert names == ["Branch Munich", "Wien Office"]
        assert body["skipped_existing"] == []

        # Aktiver Vault hat jetzt alle drei
        inv = client.get("/api/inventory", headers=_h(active_token)).json()
        assert len(inv["devices"]) == 3

    def test_skips_existing_names(
        self, client: TestClient, active_token: str, tmp_path: Path,
    ) -> None:
        source = _vault(tmp_path / "source.opnvault", SOURCE_PASSWORD, [
            VaultDevice(
                id="src-1", name="HQ Berlin",  # gleicher Name wie aktiver
                host="other-host.lab", port=443,
                tls_verify=True, tags=[],
                api_key="k", api_secret="s", descr="",
            ),
            VaultDevice(
                id="src-2", name="NEW Device", host="opn.lab", port=443,
                tls_verify=True, tags=[],
                api_key="k", api_secret="s", descr="",
            ),
        ])
        response = client.post(
            "/api/imports/vault", headers=_h(active_token),
            json={
                "source_path": str(source),
                "source_password": SOURCE_PASSWORD,
            },
        )
        assert response.status_code == 201
        body = response.json()
        assert len(body["added"]) == 1
        assert body["added"][0]["name"] == "NEW Device"
        assert body["skipped_existing"] == ["HQ Berlin"]

    def test_wrong_password_returns_401(
        self, client: TestClient, active_token: str, tmp_path: Path,
    ) -> None:
        source = _vault(tmp_path / "source.opnvault", SOURCE_PASSWORD, [])
        response = client.post(
            "/api/imports/vault", headers=_h(active_token),
            json={
                "source_path": str(source),
                "source_password": "falsch-aber-12+",
            },
        )
        assert response.status_code == 401

    def test_missing_source_returns_404(
        self, client: TestClient, active_token: str, tmp_path: Path,
    ) -> None:
        response = client.post(
            "/api/imports/vault", headers=_h(active_token),
            json={
                "source_path": str(tmp_path / "ghost.opnvault"),
                "source_password": SOURCE_PASSWORD,
            },
        )
        assert response.status_code == 404

    def test_same_as_active_rejected(
        self, client: TestClient, active_token: str, tmp_path: Path,
    ) -> None:
        # User versucht den aktiven Vault als Quelle zu nutzen → 400
        active_path = tmp_path / "active.opnvault"
        response = client.post(
            "/api/imports/vault", headers=_h(active_token),
            json={
                "source_path": str(active_path),
                "source_password": ACTIVE_PASSWORD,
            },
        )
        assert response.status_code == 400
        assert "identisch" in response.json()["detail"].lower()

    def test_unauthorized_without_token(
        self, client: TestClient, tmp_path: Path,
    ) -> None:
        source = _vault(tmp_path / "source.opnvault", SOURCE_PASSWORD, [])
        response = client.post("/api/imports/vault", json={
            "source_path": str(source),
            "source_password": SOURCE_PASSWORD,
        })
        assert response.status_code == 401
