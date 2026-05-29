"""Tests fuer die Profile-CRUD-Routen."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from opn_cockpit.vault.model import VaultData
from opn_cockpit.vault.store import create_vault
from opn_cockpit.web.server import create_app

PASSWORD = "korrektes-pferd-batterie-heftklammer"


def _make_vault(tmp_path: Path) -> Path:
    path = tmp_path / "test.opnvault"
    create_vault(path, PASSWORD, VaultData(devices=[]))
    return path


def _unlock(client: TestClient, vault_path: Path) -> str:
    response = client.post(
        "/api/auth/unlock",
        json={"vault_path": str(vault_path), "password": PASSWORD},
    )
    assert response.status_code == 200
    return str(response.json()["token"])


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    return TestClient(create_app())


@pytest.fixture()
def vault(tmp_path: Path) -> Path:
    return _make_vault(tmp_path)


@pytest.fixture()
def token(client: TestClient, vault: Path) -> str:
    return _unlock(client, vault)


class TestProfiles:
    def test_requires_auth(self, client: TestClient) -> None:
        assert client.get("/api/profiles").status_code == 401

    def test_initially_empty(self, client: TestClient, token: str) -> None:
        response = client.get("/api/profiles", headers=_bearer(token))
        assert response.status_code == 200
        assert response.json()["profiles"] == []

    def test_create_and_list(self, client: TestClient, token: str) -> None:
        create = client.post(
            "/api/profiles",
            json={
                "name": "Branch-Routes",
                "action": "add_route",
                "subsystem": "routes",
                "default_selector": "tag:branches",
                "spec": {
                    "network": "10.99.0.0/24",
                    "gateway": "WAN_GW",
                    "descr": "Branch",
                    "disabled": False,
                },
            },
            headers=_bearer(token),
        )
        assert create.status_code == 201
        body = create.json()
        assert body["name"] == "Branch-Routes"
        assert body["id"].startswith("prof-")

        listing = client.get("/api/profiles", headers=_bearer(token)).json()
        names = {p["name"] for p in listing["profiles"]}
        assert "Branch-Routes" in names

    def test_duplicate_name_409(self, client: TestClient, token: str) -> None:
        body = {
            "name": "Dup",
            "action": "add_route",
            "subsystem": "routes",
            "default_selector": "all",
            "spec": {"network": "1.1.1.0/24", "gateway": "WAN_GW"},
        }
        client.post("/api/profiles", json=body, headers=_bearer(token))
        second = client.post("/api/profiles", json=body, headers=_bearer(token))
        assert second.status_code == 409

    def test_get_unknown_404(self, client: TestClient, token: str) -> None:
        response = client.get(
            "/api/profiles/prof-NOTHERE", headers=_bearer(token),
        )
        assert response.status_code == 404

    def test_delete_and_404_after(self, client: TestClient, token: str) -> None:
        create = client.post(
            "/api/profiles",
            json={
                "name": "Del",
                "action": "add_route",
                "subsystem": "routes",
                "default_selector": "all",
                "spec": {"network": "2.2.2.0/24", "gateway": "GW"},
            },
            headers=_bearer(token),
        ).json()
        pid = create["id"]
        d1 = client.delete(f"/api/profiles/{pid}", headers=_bearer(token))
        assert d1.status_code == 204
        d2 = client.delete(f"/api/profiles/{pid}", headers=_bearer(token))
        assert d2.status_code == 404

    def test_secrets_in_spec_get_sanitized(
        self,
        client: TestClient,
        token: str,
    ) -> None:
        create = client.post(
            "/api/profiles",
            json={
                "name": "Secrety",
                "action": "add_route",
                "subsystem": "routes",
                "default_selector": "all",
                "spec": {
                    "network": "3.3.3.0/24",
                    "gateway": "GW",
                    "api_key": "leak",       # darf NICHT durchkommen
                    "api_secret": "leak",
                    "password": "leak",
                },
            },
            headers=_bearer(token),
        )
        assert create.status_code == 201
        spec = create.json()["spec"]
        for forbidden in ("api_key", "api_secret", "password"):
            assert forbidden not in spec
