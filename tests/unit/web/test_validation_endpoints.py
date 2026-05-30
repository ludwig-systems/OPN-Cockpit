"""End-to-End-Tests fuer vorab-Validierung in Plan- und Inventory-Endpunkten (v5-Pass 2).

Ziel: kaputte CIDR/Gateway/Alias-Content/Host-Eingaben werden bereits
beim Plan-/Device-Anlegen mit 422 zurueckgewiesen, nicht erst beim Apply.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from opn_cockpit.vault.model import VaultData, VaultDevice
from opn_cockpit.vault.store import create_vault
from opn_cockpit.web.server import create_app

VAULT_PASSWORD = "korrektes-pferd-batterie-heftklammer"


def _make_vault(tmp_path: Path) -> Path:
    path = tmp_path / "active.opnvault"
    create_vault(path, VAULT_PASSWORD, VaultData(devices=[
        VaultDevice(
            id="dev-001", name="HQ", host="opn.lab", port=443,
            tls_verify=True, tags=[], api_key="k", api_secret="s", descr="",
        ),
    ]))
    return path


@pytest.fixture()
def client_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[TestClient, str]]:
    monkeypatch.setenv("OPNCOCKPIT_DATA_DIR", str(tmp_path))
    path = _make_vault(tmp_path)
    client = TestClient(create_app())
    unlock = client.post("/api/auth/unlock", json={
        "vault_path": str(path), "password": VAULT_PASSWORD,
    })
    yield client, str(unlock.json()["token"])


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class TestRoutePlanValidation:
    def test_cidr_with_host_bits_rejected(
        self, client_token: tuple[TestClient, str],
    ) -> None:
        client, token = client_token
        response = client.post(
            "/api/plans/route", headers=_h(token),
            json={
                "network": "10.0.0.5/24",  # Host-Bits != 0
                "gateway": "WAN_GW",
                "descr": "",
                "disabled": False,
                "target_device_ids": ["dev-001"],
            },
        )
        assert response.status_code == 422
        assert "CIDR" in response.json()["detail"]

    def test_invalid_gateway_name_rejected(
        self, client_token: tuple[TestClient, str],
    ) -> None:
        client, token = client_token
        response = client.post(
            "/api/plans/route", headers=_h(token),
            json={
                "network": "10.0.0.0/24",
                "gateway": "with space",  # invalid
                "descr": "", "disabled": False,
                "target_device_ids": ["dev-001"],
            },
        )
        assert response.status_code == 422
        assert "Gateway" in response.json()["detail"]


class TestAliasPlanValidation:
    def test_host_content_validated(
        self, client_token: tuple[TestClient, str],
    ) -> None:
        client, token = client_token
        response = client.post(
            "/api/plans/alias", headers=_h(token),
            json={
                "name": "branch_ips",
                "type": "host",
                "content": ["10.0.0.1", "not a host"],
                "descr": "",
                "merge_mode": "create",
                "target_device_ids": ["dev-001"],
            },
        )
        assert response.status_code == 422
        assert "Alias-Eintrag" in response.json()["detail"]

    def test_port_content_validated(
        self, client_token: tuple[TestClient, str],
    ) -> None:
        client, token = client_token
        response = client.post(
            "/api/plans/alias", headers=_h(token),
            json={
                "name": "ports", "type": "port",
                "content": ["80", "99999"],  # 99999 ueberschreitet 65535
                "descr": "", "merge_mode": "create",
                "target_device_ids": ["dev-001"],
            },
        )
        assert response.status_code == 422

    def test_network_content_validated(
        self, client_token: tuple[TestClient, str],
    ) -> None:
        client, token = client_token
        response = client.post(
            "/api/plans/alias", headers=_h(token),
            json={
                "name": "nets", "type": "network",
                "content": ["10.0.0.5/24"],  # Host-Bits
                "descr": "", "merge_mode": "create",
                "target_device_ids": ["dev-001"],
            },
        )
        assert response.status_code == 422

    def test_valid_alias_accepted(
        self, client_token: tuple[TestClient, str],
    ) -> None:
        client, token = client_token
        response = client.post(
            "/api/plans/alias", headers=_h(token),
            json={
                "name": "branch_ips", "type": "host",
                "content": ["10.0.0.1", "10.0.0.2", "opn-1.lab"],
                "descr": "", "merge_mode": "create",
                "target_device_ids": ["dev-001"],
            },
        )
        assert response.status_code == 201

    def test_invalid_alias_name_rejected(
        self, client_token: tuple[TestClient, str],
    ) -> None:
        client, token = client_token
        response = client.post(
            "/api/plans/alias", headers=_h(token),
            json={
                "name": "1starts-with-digit", "type": "host",
                "content": ["10.0.0.1"],
                "descr": "", "merge_mode": "create",
                "target_device_ids": ["dev-001"],
            },
        )
        assert response.status_code == 422


class TestDeviceHostValidation:
    def test_invalid_host_on_create_rejected(
        self, client_token: tuple[TestClient, str],
    ) -> None:
        client, token = client_token
        response = client.post(
            "/api/inventory/devices", headers=_h(token),
            json={
                "name": "X", "host": "not a host",
                "port": 443, "tls_verify": True,
                "tags": [], "descr": "",
                "api_key": "k", "api_secret": "s",
            },
        )
        assert response.status_code == 422

    def test_ip_accepted(
        self, client_token: tuple[TestClient, str],
    ) -> None:
        client, token = client_token
        response = client.post(
            "/api/inventory/devices", headers=_h(token),
            json={
                "name": "Y", "host": "192.168.1.1",
                "port": 443, "tls_verify": True,
                "tags": [], "descr": "",
                "api_key": "k", "api_secret": "s",
            },
        )
        assert response.status_code == 201

    def test_hostname_accepted(
        self, client_token: tuple[TestClient, str],
    ) -> None:
        client, token = client_token
        response = client.post(
            "/api/inventory/devices", headers=_h(token),
            json={
                "name": "Z", "host": "opn-3.lab",
                "port": 443, "tls_verify": True,
                "tags": [], "descr": "",
                "api_key": "k", "api_secret": "s",
            },
        )
        assert response.status_code == 201

    def test_invalid_host_on_update_rejected(
        self, client_token: tuple[TestClient, str],
    ) -> None:
        client, token = client_token
        response = client.patch(
            "/api/inventory/devices/dev-001", headers=_h(token),
            json={"host": "not a host"},
        )
        assert response.status_code == 422
