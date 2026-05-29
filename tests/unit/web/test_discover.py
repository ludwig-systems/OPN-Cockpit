"""Tests fuer die Discovery-Routen (Gateways + Aliase pro Geraet)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from opn_cockpit.core.discovery import AliasSummary, DiscoveryError, GatewaySummary
from opn_cockpit.core.errors import make_context
from opn_cockpit.vault.model import VaultData, VaultDevice
from opn_cockpit.vault.store import create_vault
from opn_cockpit.web.server import create_app

PASSWORD = "korrektes-pferd-batterie-heftklammer"


def _make_vault(tmp_path: Path) -> Path:
    path = tmp_path / "test.opnvault"
    create_vault(path, PASSWORD, VaultData(devices=[
        VaultDevice(
            id="dev-001", name="HQ Berlin", host="opn-1.lab", port=443,
            tls_verify=True, tags=["test"], api_key="k", api_secret="s", descr="",
        ),
    ]))
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


class TestGatewayDiscovery:
    def test_requires_auth(self, client: TestClient) -> None:
        assert client.get(
            "/api/discover/devices/dev-001/gateways",
        ).status_code == 401

    def test_unknown_device_returns_404(
        self,
        client: TestClient,
        vault: Path,
    ) -> None:
        token = _unlock(client, vault)
        response = client.get(
            "/api/discover/devices/nope/gateways",
            headers=_bearer(token),
        )
        assert response.status_code == 404

    def test_returns_gateway_list(
        self,
        client: TestClient,
        vault: Path,
    ) -> None:
        token = _unlock(client, vault)
        with patch(
            "opn_cockpit.web.api.discover.list_gateways",
            return_value=[
                GatewaySummary(name="WAN_GW", address="1.2.3.4", status="online"),
                GatewaySummary(name="V2_WANBwIn", address="5.6.7.8", status="online"),
            ],
        ):
            response = client.get(
                "/api/discover/devices/dev-001/gateways",
                headers=_bearer(token),
            )
        assert response.status_code == 200
        body = response.json()
        assert body["device_id"] == "dev-001"
        names = {g["name"] for g in body["gateways"]}
        assert names == {"WAN_GW", "V2_WANBwIn"}

    def test_discovery_error_returns_503(
        self,
        client: TestClient,
        vault: Path,
    ) -> None:
        token = _unlock(client, vault)
        with patch(
            "opn_cockpit.web.api.discover.list_gateways",
            side_effect=DiscoveryError(
                "nicht erreichbar",
                context=make_context(host="opn-1.lab", summary="timeout"),
            ),
        ):
            response = client.get(
                "/api/discover/devices/dev-001/gateways",
                headers=_bearer(token),
            )
        assert response.status_code == 503


class TestAliasDiscovery:
    def test_requires_auth(self, client: TestClient) -> None:
        assert client.get(
            "/api/discover/devices/dev-001/aliases",
        ).status_code == 401

    def test_returns_alias_list(
        self,
        client: TestClient,
        vault: Path,
    ) -> None:
        token = _unlock(client, vault)
        with patch(
            "opn_cockpit.web.api.discover.list_aliases",
            return_value=[
                AliasSummary(name="branch_ips", type="host", descr=""),
                AliasSummary(name="lab_ports", type="port", descr="lab"),
            ],
        ):
            response = client.get(
                "/api/discover/devices/dev-001/aliases",
                headers=_bearer(token),
            )
        assert response.status_code == 200
        body = response.json()
        names = {a["name"] for a in body["aliases"]}
        assert names == {"branch_ips", "lab_ports"}
