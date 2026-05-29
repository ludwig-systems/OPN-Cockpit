"""Tests fuer die Inventar-Routen (GET/POST/DELETE/heartbeat/test-connection)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from opn_cockpit.core.health import HealthResult
from opn_cockpit.vault.model import VaultData, VaultDevice
from opn_cockpit.vault.store import create_vault, open_vault
from opn_cockpit.web.server import create_app

PASSWORD = "korrektes-pferd-batterie-heftklammer"


def _make_vault(tmp_path: Path, devices: list[VaultDevice] | None = None) -> Path:
    path = tmp_path / "test.opnvault"
    create_vault(path, PASSWORD, VaultData(devices=devices or []))
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
def client() -> TestClient:
    return TestClient(create_app())


@pytest.fixture()
def vault_with_devices(tmp_path: Path) -> Path:
    devices = [
        VaultDevice(
            id="dev-001",
            name="HQ Berlin",
            host="opn-berlin.lab",
            port=443,
            tls_verify=True,
            tags=["branches", "germany"],
            api_key="key-001",
            api_secret="secret-001",
            descr="HQ",
        ),
        VaultDevice(
            id="dev-002",
            name="Lab Box",
            host="opn-lab.intern",
            port=8443,
            tls_verify=False,
            tags=["lab"],
            api_key="key-002",
            api_secret="secret-002",
            descr="",
        ),
    ]
    return _make_vault(tmp_path, devices)


# ---------------------------------------------------------------------------
# GET /api/inventory
# ---------------------------------------------------------------------------


class TestListInventory:
    def test_requires_auth(self, client: TestClient) -> None:
        assert client.get("/api/inventory").status_code == 401

    def test_returns_devices_without_secrets(
        self,
        client: TestClient,
        vault_with_devices: Path,
    ) -> None:
        token = _unlock(client, vault_with_devices)
        response = client.get("/api/inventory", headers=_bearer(token))
        assert response.status_code == 200
        body = response.json()
        assert len(body["devices"]) == 2
        names = {d["name"] for d in body["devices"]}
        assert names == {"HQ Berlin", "Lab Box"}
        # Niemals Secrets im Response
        for d in body["devices"]:
            assert "api_key" not in d
            assert "api_secret" not in d

    def test_returns_tag_summary(
        self,
        client: TestClient,
        vault_with_devices: Path,
    ) -> None:
        token = _unlock(client, vault_with_devices)
        body = client.get("/api/inventory", headers=_bearer(token)).json()
        tags = {t["name"]: t["count"] for t in body["tags"]}
        assert tags == {"branches": 1, "germany": 1, "lab": 1}

    def test_empty_vault(self, client: TestClient, tmp_path: Path) -> None:
        vault = _make_vault(tmp_path)
        token = _unlock(client, vault)
        body = client.get("/api/inventory", headers=_bearer(token)).json()
        assert body["devices"] == []
        assert body["tags"] == []


# ---------------------------------------------------------------------------
# POST /api/inventory/devices
# ---------------------------------------------------------------------------


class TestAddDevice:
    def _payload(self, **overrides: object) -> dict[str, object]:
        base: dict[str, object] = {
            "name": "Frankfurt RZ",
            "host": "opn-frankfurt.lab",
            "port": 443,
            "tls_verify": True,
            "tags": ["core", "germany"],
            "descr": "Datacenter",
            "api_key": "fra-key",
            "api_secret": "fra-secret",
        }
        base.update(overrides)
        return base

    def test_requires_auth(self, client: TestClient) -> None:
        response = client.post("/api/inventory/devices", json=self._payload())
        assert response.status_code == 401

    def test_creates_device_and_persists(
        self,
        client: TestClient,
        tmp_path: Path,
    ) -> None:
        vault = _make_vault(tmp_path)
        token = _unlock(client, vault)
        response = client.post(
            "/api/inventory/devices",
            json=self._payload(),
            headers=_bearer(token),
        )
        assert response.status_code == 201
        body = response.json()
        assert body["name"] == "Frankfurt RZ"
        assert body["host"] == "opn-frankfurt.lab"
        assert "api_key" not in body
        # Auf Platte landet das verschluesselt
        opened = open_vault(vault, PASSWORD)
        assert len(opened.data.devices) == 1
        on_disk = opened.data.devices[0]
        assert on_disk.api_key == "fra-key"
        assert on_disk.api_secret == "fra-secret"
        # Token bleibt nach save_vault gueltig
        me = client.get("/api/auth/me", headers=_bearer(token))
        assert me.status_code == 200

    def test_duplicate_name_returns_409(
        self,
        client: TestClient,
        vault_with_devices: Path,
    ) -> None:
        token = _unlock(client, vault_with_devices)
        response = client.post(
            "/api/inventory/devices",
            json=self._payload(name="HQ Berlin"),
            headers=_bearer(token),
        )
        assert response.status_code == 409

    def test_invalid_port_returns_422(
        self,
        client: TestClient,
        tmp_path: Path,
    ) -> None:
        vault = _make_vault(tmp_path)
        token = _unlock(client, vault)
        response = client.post(
            "/api/inventory/devices",
            json=self._payload(port=70000),
            headers=_bearer(token),
        )
        assert response.status_code == 422


# ---------------------------------------------------------------------------
# PATCH /api/inventory/devices/{id}
# ---------------------------------------------------------------------------


class TestUpdateDevice:
    def test_requires_auth(self, client: TestClient) -> None:
        response = client.patch(
            "/api/inventory/devices/dev-001",
            json={"name": "Neu"},
        )
        assert response.status_code == 401

    def test_updates_fields_and_persists(
        self,
        client: TestClient,
        vault_with_devices: Path,
    ) -> None:
        token = _unlock(client, vault_with_devices)
        response = client.patch(
            "/api/inventory/devices/dev-001",
            json={
                "name": "HQ Berlin 2",
                "host": "berlin-2.lab",
                "port": 8443,
                "tls_verify": False,
                "tags": ["branches", "germany", "primary"],
                "descr": "Updated",
            },
            headers=_bearer(token),
        )
        assert response.status_code == 200
        body = response.json()
        assert body["name"] == "HQ Berlin 2"
        assert body["host"] == "berlin-2.lab"
        assert body["port"] == 8443
        assert body["tls_verify"] is False
        # Auf Platte
        opened = open_vault(vault_with_devices, PASSWORD)
        on_disk = next(d for d in opened.data.devices if d.id == "dev-001")
        assert on_disk.name == "HQ Berlin 2"
        assert on_disk.host == "berlin-2.lab"
        assert on_disk.port == 8443
        # Secrets unveraendert (kein api_key/api_secret im Patch)
        assert on_disk.api_key == "key-001"
        assert on_disk.api_secret == "secret-001"

    def test_empty_api_key_does_not_overwrite_secret(
        self,
        client: TestClient,
        vault_with_devices: Path,
    ) -> None:
        token = _unlock(client, vault_with_devices)
        response = client.patch(
            "/api/inventory/devices/dev-001",
            json={"name": "Berlin", "api_key": "", "api_secret": ""},
            headers=_bearer(token),
        )
        assert response.status_code == 200
        opened = open_vault(vault_with_devices, PASSWORD)
        on_disk = next(d for d in opened.data.devices if d.id == "dev-001")
        assert on_disk.api_key == "key-001"
        assert on_disk.api_secret == "secret-001"

    def test_api_key_rotation(
        self,
        client: TestClient,
        vault_with_devices: Path,
    ) -> None:
        token = _unlock(client, vault_with_devices)
        response = client.patch(
            "/api/inventory/devices/dev-001",
            json={"api_key": "new-key", "api_secret": "new-secret"},
            headers=_bearer(token),
        )
        assert response.status_code == 200
        opened = open_vault(vault_with_devices, PASSWORD)
        on_disk = next(d for d in opened.data.devices if d.id == "dev-001")
        assert on_disk.api_key == "new-key"
        assert on_disk.api_secret == "new-secret"

    def test_unknown_id_returns_404(
        self,
        client: TestClient,
        vault_with_devices: Path,
    ) -> None:
        token = _unlock(client, vault_with_devices)
        response = client.patch(
            "/api/inventory/devices/does-not-exist",
            json={"name": "X"},
            headers=_bearer(token),
        )
        assert response.status_code == 404

    def test_name_conflict_returns_409(
        self,
        client: TestClient,
        vault_with_devices: Path,
    ) -> None:
        token = _unlock(client, vault_with_devices)
        response = client.patch(
            "/api/inventory/devices/dev-001",
            json={"name": "Lab Box"},
            headers=_bearer(token),
        )
        assert response.status_code == 409


# ---------------------------------------------------------------------------
# DELETE /api/inventory/devices/{id}
# ---------------------------------------------------------------------------


class TestRemoveDevice:
    def test_requires_auth(self, client: TestClient) -> None:
        response = client.delete("/api/inventory/devices/dev-001")
        assert response.status_code == 401

    def test_removes_existing_device(
        self,
        client: TestClient,
        vault_with_devices: Path,
    ) -> None:
        token = _unlock(client, vault_with_devices)
        response = client.delete(
            "/api/inventory/devices/dev-001",
            headers=_bearer(token),
        )
        assert response.status_code == 204
        body = client.get("/api/inventory", headers=_bearer(token)).json()
        assert {d["id"] for d in body["devices"]} == {"dev-002"}
        # Auf Platte persistiert
        opened = open_vault(vault_with_devices, PASSWORD)
        assert {d.id for d in opened.data.devices} == {"dev-002"}

    def test_unknown_id_returns_404(
        self,
        client: TestClient,
        vault_with_devices: Path,
    ) -> None:
        token = _unlock(client, vault_with_devices)
        response = client.delete(
            "/api/inventory/devices/does-not-exist",
            headers=_bearer(token),
        )
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/inventory/heartbeat
# ---------------------------------------------------------------------------


class TestHeartbeat:
    def test_requires_auth(self, client: TestClient) -> None:
        response = client.post(
            "/api/inventory/heartbeat",
            json={"device_ids": [], "timeout_s": 0.1},
        )
        assert response.status_code == 401

    def test_returns_entry_per_device_with_mocked_probe(
        self,
        client: TestClient,
        vault_with_devices: Path,
    ) -> None:
        token = _unlock(client, vault_with_devices)
        with patch(
            "opn_cockpit.web.api.inventory.tcp_probe",
            side_effect=lambda host, port, timeout_s: host == "opn-berlin.lab",
        ):
            response = client.post(
                "/api/inventory/heartbeat",
                json={"device_ids": [], "timeout_s": 0.5},
                headers=_bearer(token),
            )
        assert response.status_code == 200
        body = response.json()
        by_id = {r["device_id"]: r for r in body["results"]}
        assert by_id["dev-001"]["reachable"] is True
        assert by_id["dev-002"]["reachable"] is False
        assert by_id["dev-001"]["checked_at_iso"].endswith("Z")

    def test_filters_by_device_ids(
        self,
        client: TestClient,
        vault_with_devices: Path,
    ) -> None:
        token = _unlock(client, vault_with_devices)
        with patch(
            "opn_cockpit.web.api.inventory.tcp_probe",
            return_value=True,
        ):
            response = client.post(
                "/api/inventory/heartbeat",
                json={"device_ids": ["dev-002"], "timeout_s": 0.5},
                headers=_bearer(token),
            )
        body = response.json()
        assert [r["device_id"] for r in body["results"]] == ["dev-002"]

    def test_empty_inventory_returns_empty_results(
        self,
        client: TestClient,
        tmp_path: Path,
    ) -> None:
        vault = _make_vault(tmp_path)
        token = _unlock(client, vault)
        response = client.post(
            "/api/inventory/heartbeat",
            json={"device_ids": [], "timeout_s": 0.5},
            headers=_bearer(token),
        )
        assert response.status_code == 200
        assert response.json()["results"] == []


# ---------------------------------------------------------------------------
# POST /api/inventory/devices/{id}/test-connection
# ---------------------------------------------------------------------------


class TestTestConnection:
    def test_requires_auth(self, client: TestClient) -> None:
        response = client.post("/api/inventory/devices/dev-001/test-connection")
        assert response.status_code == 401

    def test_unknown_device_returns_404(
        self,
        client: TestClient,
        vault_with_devices: Path,
    ) -> None:
        token = _unlock(client, vault_with_devices)
        response = client.post(
            "/api/inventory/devices/unknown/test-connection",
            headers=_bearer(token),
        )
        assert response.status_code == 404

    def test_success_returns_authenticated_true(
        self,
        client: TestClient,
        vault_with_devices: Path,
    ) -> None:
        token = _unlock(client, vault_with_devices)
        with patch(
            "opn_cockpit.web.api.inventory.check_device",
            return_value=HealthResult(
                reachable=True, authenticated=True, summary="ok",
            ),
        ):
            response = client.post(
                "/api/inventory/devices/dev-001/test-connection",
                headers=_bearer(token),
            )
        assert response.status_code == 200
        body = response.json()
        assert body["device_id"] == "dev-001"
        assert body["reachable"] is True
        assert body["authenticated"] is True
        assert body["summary"] == "ok"

    def test_auth_failure_returns_authenticated_false(
        self,
        client: TestClient,
        vault_with_devices: Path,
    ) -> None:
        token = _unlock(client, vault_with_devices)
        with patch(
            "opn_cockpit.web.api.inventory.check_device",
            return_value=HealthResult(
                reachable=True, authenticated=False, summary="auth abgelehnt",
            ),
        ):
            response = client.post(
                "/api/inventory/devices/dev-001/test-connection",
                headers=_bearer(token),
            )
        body = response.json()
        assert body["reachable"] is True
        assert body["authenticated"] is False
