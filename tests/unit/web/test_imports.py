"""Tests fuer die Bulk-Import-Routen (CSV-Routen / JSON-Aliase)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from opn_cockpit.core.objects.base import Diff, DiffKind
from opn_cockpit.core.objects.routes import RouteSpec
from opn_cockpit.inventory.model import Device
from opn_cockpit.orchestration.planner import Plan, PlannedDeviceAction
from opn_cockpit.vault.model import VaultData, VaultDevice
from opn_cockpit.vault.store import create_vault
from opn_cockpit.web.server import create_app

PASSWORD = "korrektes-pferd-batterie-heftklammer"

ROUTE_CSV = """network,gateway,descr,disabled
10.99.0.0/24,WAN_GW,HQ Berlin,0
10.99.1.0/24,WAN_GW,Branch Munich,0
"""

ROUTE_CSV_BROKEN = """network,gateway,descr,disabled
nonsense,WAN_GW,broken,0
10.99.1.0/24,,empty-gateway,0
"""

ALIASES_JSON = """[
  {"name": "branch_ips", "type": "host", "content": ["10.1.1.1", "10.1.1.2"]},
  {"name": "lab_ports", "type": "port", "content": [22, 443]}
]
"""


def _make_vault(tmp_path: Path) -> Path:
    path = tmp_path / "test.opnvault"
    create_vault(path, PASSWORD, VaultData(devices=[
        VaultDevice(
            id="dev-001", name="Box 1", host="opn-1.lab", port=443,
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


def _fake_bulk_plan() -> Plan:
    spec = RouteSpec(network="10.99.0.0/24", gateway="WAN_GW", descr="", disabled=False)
    actions = (
        PlannedDeviceAction(
            device=Device(
                id="dev-001", name="Box 1", host="opn-1.lab", port=443,
                tls_verify=True, tags=("test",), descr="",
            ),
            target_spec=spec,
            current_state=None,
            diff=Diff(kind=DiffKind.NEW, summary="NEW"),
            payload_masked={"network": "10.99.0.0/24"},
        ),
    )
    return Plan(
        plan_id="pl-BULK1234",
        action="bulk_add_route",
        subsystem="routes",
        created_at_utc="2026-05-29T13:00:00.000Z",
        actions=actions,
    )


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


class TestImportRoutes:
    def test_requires_auth(self, client: TestClient) -> None:
        response = client.post(
            "/api/imports/routes",
            files={"file": ("a.csv", ROUTE_CSV, "text/csv")},
            data={"target_device_ids": "dev-001"},
        )
        assert response.status_code == 401

    def test_valid_csv_creates_plan(
        self,
        client: TestClient,
        token: str,
    ) -> None:
        with patch(
            "opn_cockpit.web.api.imports.Planner.create_bulk_plan",
            return_value=_fake_bulk_plan(),
        ):
            response = client.post(
                "/api/imports/routes",
                files={"file": ("routes.csv", ROUTE_CSV, "text/csv")},
                data={"target_device_ids": "dev-001"},
                headers=_bearer(token),
            )
        assert response.status_code == 201
        body = response.json()
        assert body["plan_id"] == "pl-BULK1234"
        assert body["action"] == "bulk_add_route"

    def test_broken_csv_returns_400_with_errors(
        self,
        client: TestClient,
        token: str,
    ) -> None:
        response = client.post(
            "/api/imports/routes",
            files={"file": ("bad.csv", ROUTE_CSV_BROKEN, "text/csv")},
            data={"target_device_ids": "dev-001"},
            headers=_bearer(token),
        )
        assert response.status_code == 400
        detail = response.json()["detail"]
        assert "errors" in detail
        assert len(detail["errors"]) >= 1

    def test_no_devices_400(
        self,
        client: TestClient,
        token: str,
    ) -> None:
        response = client.post(
            "/api/imports/routes",
            files={"file": ("routes.csv", ROUTE_CSV, "text/csv")},
            data={},
            headers=_bearer(token),
        )
        # FastAPI gibt 422 wenn Form-Field fehlt
        assert response.status_code in (400, 422)


class TestImportAliases:
    def test_valid_json_creates_plan(
        self,
        client: TestClient,
        token: str,
    ) -> None:
        with patch(
            "opn_cockpit.web.api.imports.Planner.create_bulk_plan",
            return_value=_fake_bulk_plan(),
        ):
            response = client.post(
                "/api/imports/aliases",
                files={"file": ("aliases.json", ALIASES_JSON, "application/json")},
                data={"target_device_ids": "dev-001"},
                headers=_bearer(token),
            )
        assert response.status_code == 201

    def test_broken_json_returns_400(
        self,
        client: TestClient,
        token: str,
    ) -> None:
        response = client.post(
            "/api/imports/aliases",
            files={"file": ("bad.json", "not json", "application/json")},
            data={"target_device_ids": "dev-001"},
            headers=_bearer(token),
        )
        assert response.status_code == 400

    def test_append_mode_action_name(
        self,
        client: TestClient,
        token: str,
    ) -> None:
        with patch(
            "opn_cockpit.web.api.imports.Planner.create_bulk_plan",
            return_value=_fake_bulk_plan(),
        ) as create:
            client.post(
                "/api/imports/aliases",
                files={"file": ("a.json", ALIASES_JSON, "application/json")},
                data={"target_device_ids": "dev-001", "append_mode": "true"},
                headers=_bearer(token),
            )
        kwargs = create.call_args.kwargs
        assert kwargs["action"] == "bulk_append_alias"


class TestImportUnknownDevice:
    def test_unknown_device_returns_404(
        self,
        client: TestClient,
        token: str,
    ) -> None:
        response = client.post(
            "/api/imports/routes",
            files={"file": ("a.csv", ROUTE_CSV, "text/csv")},
            data={"target_device_ids": "dev-999"},
            headers=_bearer(token),
        )
        assert response.status_code == 404
