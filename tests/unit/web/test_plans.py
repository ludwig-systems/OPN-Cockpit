"""Tests fuer die Plan/Apply-Routen."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from opn_cockpit.core.objects.aliases import AliasSpec
from opn_cockpit.core.objects.base import Diff, DiffKind
from opn_cockpit.core.objects.routes import RouteSpec
from opn_cockpit.core.result import (
    Phase,
    Result,
    RolloutReport,
    Status,
)
from opn_cockpit.inventory.model import Device
from opn_cockpit.orchestration.planner import Plan, PlannedDeviceAction
from opn_cockpit.vault.model import VaultData, VaultDevice
from opn_cockpit.vault.store import create_vault
from opn_cockpit.web.server import create_app

PASSWORD = "korrektes-pferd-batterie-heftklammer"


def _device(idx: int) -> VaultDevice:
    return VaultDevice(
        id=f"dev-{idx:03d}",
        name=f"Box {idx}",
        host=f"opn-{idx}.lab",
        port=443,
        tls_verify=True,
        tags=["test"],
        api_key=f"key-{idx}",
        api_secret=f"secret-{idx}",
        descr="",
    )


def _make_vault_with_devices(tmp_path: Path, n: int = 2) -> Path:
    path = tmp_path / "test.opnvault"
    create_vault(path, PASSWORD, VaultData(devices=[_device(i) for i in range(1, n + 1)]))
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
    # Plan-Store soll in tmp_path liegen, nicht im echten %APPDATA%.
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    return TestClient(create_app())


@pytest.fixture()
def vault(tmp_path: Path) -> Path:
    return _make_vault_with_devices(tmp_path, n=2)


# ---------------------------------------------------------------------------
# Fake-Plan/Report-Helfer
# ---------------------------------------------------------------------------


def _fake_route_plan(plan_id: str = "pl-ABCD1234") -> Plan:
    spec = RouteSpec(network="10.99.0.0/24", gateway="WAN_GW", descr="", disabled=False)
    actions = (
        PlannedDeviceAction(
            device=Device(
                id="dev-001", name="Box 1", host="opn-1.lab", port=443,
                tls_verify=True, tags=("test",), descr="",
            ),
            target_spec=spec,
            current_state=None,
            diff=Diff(kind=DiffKind.NEW, summary="NEW route"),
            payload_masked={"network": "10.99.0.0/24"},
        ),
        PlannedDeviceAction(
            device=Device(
                id="dev-002", name="Box 2", host="opn-2.lab", port=443,
                tls_verify=True, tags=("test",), descr="",
            ),
            target_spec=spec,
            current_state=spec,
            diff=Diff(kind=DiffKind.SKIP, summary="bereits vorhanden"),
            payload_masked={"network": "10.99.0.0/24"},
        ),
    )
    return Plan(
        plan_id=plan_id,
        action="add_route",
        subsystem="routes",
        created_at_utc="2026-05-29T12:00:00.000Z",
        actions=actions,
    )


def _fake_alias_plan(plan_id: str = "pl-ALIAS001") -> Plan:
    spec = AliasSpec(
        name="branch_ips", type="host", content=("10.99.0.1",), descr="",
        merge_mode="create",
    )
    actions = (
        PlannedDeviceAction(
            device=Device(
                id="dev-001", name="Box 1", host="opn-1.lab", port=443,
                tls_verify=True, tags=("test",), descr="",
            ),
            target_spec=spec,
            current_state=None,
            diff=Diff(kind=DiffKind.NEW, summary="NEW alias"),
            payload_masked={"name": "branch_ips"},
        ),
    )
    return Plan(
        plan_id=plan_id,
        action="add_alias",
        subsystem="firewall_alias",
        created_at_utc="2026-05-29T12:00:00.000Z",
        actions=actions,
    )


def _fake_report(plan_id: str = "pl-ABCD1234") -> RolloutReport:
    return RolloutReport(
        results=(
            Result(
                device_id="dev-001", subsystem="routes",
                status=Status.VERIFIED, short_message="ok",
                duration_ms=420,
            ),
            Result(
                device_id="dev-002", subsystem="routes",
                status=Status.FAILED, short_message="timeout",
                error_kind="network", failed_phase=Phase.WRITE,
                duration_ms=3000,
            ),
        )
    )


# ---------------------------------------------------------------------------
# POST /api/plans/route
# ---------------------------------------------------------------------------


class TestPlanRoute:
    def test_requires_auth(self, client: TestClient) -> None:
        response = client.post(
            "/api/plans/route",
            json={
                "network": "10.0.0.0/24", "gateway": "WAN_GW",
                "target_device_ids": ["dev-001"],
            },
        )
        assert response.status_code == 401

    def test_creates_plan_and_returns_response(
        self,
        client: TestClient,
        vault: Path,
    ) -> None:
        token = _unlock(client, vault)
        with patch(
            "opn_cockpit.web.api.plans.Planner.create_plan",
            return_value=_fake_route_plan(),
        ):
            response = client.post(
                "/api/plans/route",
                json={
                    "network": "10.99.0.0/24",
                    "gateway": "WAN_GW",
                    "descr": "Branch",
                    "disabled": False,
                    "target_device_ids": ["dev-001", "dev-002"],
                },
                headers=_bearer(token),
            )
        assert response.status_code == 201
        body = response.json()
        assert body["plan_id"] == "pl-ABCD1234"
        assert body["action"] == "add_route"
        assert body["target_count"] == 2
        assert body["to_apply_count"] == 1
        assert body["skip_count"] == 1
        kinds = {a["diff_kind"] for a in body["actions"]}
        assert "new" in kinds
        assert "skip" in kinds

    def test_unknown_device_returns_404(
        self,
        client: TestClient,
        vault: Path,
    ) -> None:
        token = _unlock(client, vault)
        response = client.post(
            "/api/plans/route",
            json={
                "network": "10.0.0.0/24",
                "gateway": "WAN_GW",
                "target_device_ids": ["does-not-exist"],
            },
            headers=_bearer(token),
        )
        assert response.status_code == 404

    def test_missing_target_returns_422(
        self,
        client: TestClient,
        vault: Path,
    ) -> None:
        token = _unlock(client, vault)
        response = client.post(
            "/api/plans/route",
            json={
                "network": "10.0.0.0/24",
                "gateway": "WAN_GW",
                "target_device_ids": [],
            },
            headers=_bearer(token),
        )
        assert response.status_code == 422


# ---------------------------------------------------------------------------
# POST /api/plans/alias
# ---------------------------------------------------------------------------


class TestPlanAlias:
    def test_requires_auth(self, client: TestClient) -> None:
        response = client.post(
            "/api/plans/alias",
            json={
                "name": "x", "type": "host", "content": ["1.1.1.1"],
                "target_device_ids": ["dev-001"],
            },
        )
        assert response.status_code == 401

    def test_creates_alias_plan(
        self,
        client: TestClient,
        vault: Path,
    ) -> None:
        token = _unlock(client, vault)
        with patch(
            "opn_cockpit.web.api.plans.Planner.create_plan",
            return_value=_fake_route_plan(),
        ):
            response = client.post(
                "/api/plans/alias",
                json={
                    "name": "branch_ips",
                    "type": "host",
                    "content": ["10.99.0.1", "10.99.1.1"],
                    "descr": "Branches",
                    "merge_mode": "create",
                    "target_device_ids": ["dev-001"],
                },
                headers=_bearer(token),
            )
        assert response.status_code == 201

    def test_append_mode_returns_append_action(
        self,
        client: TestClient,
        vault: Path,
    ) -> None:
        token = _unlock(client, vault)
        fake = _fake_route_plan()
        # Action-name will be set by the route handler based on merge_mode
        with patch(
            "opn_cockpit.web.api.plans.Planner.create_plan",
        ) as create:
            create.return_value = fake
            response = client.post(
                "/api/plans/alias",
                json={
                    "name": "branch_ips",
                    "type": "host",
                    "content": ["10.99.0.1"],
                    "merge_mode": "append",
                    "target_device_ids": ["dev-001"],
                },
                headers=_bearer(token),
            )
        assert response.status_code == 201
        # planner was called with action=append_alias
        kwargs = create.call_args.kwargs
        assert kwargs["action"] == "append_alias"

    def test_empty_content_returns_400(
        self,
        client: TestClient,
        vault: Path,
    ) -> None:
        token = _unlock(client, vault)
        response = client.post(
            "/api/plans/alias",
            json={
                "name": "x",
                "type": "host",
                "content": ["  ", ""],
                "target_device_ids": ["dev-001"],
            },
            headers=_bearer(token),
        )
        assert response.status_code == 400


# ---------------------------------------------------------------------------
# GET /api/plans, GET /api/plans/{id}, DELETE /api/plans/{id}
# ---------------------------------------------------------------------------


class TestPlanListGetDelete:
    def test_list_includes_created_plan(
        self,
        client: TestClient,
        vault: Path,
    ) -> None:
        token = _unlock(client, vault)
        with patch(
            "opn_cockpit.web.api.plans.Planner.create_plan",
            return_value=_fake_route_plan(),
        ):
            client.post(
                "/api/plans/route",
                json={
                    "network": "10.0.0.0/24",
                    "gateway": "WAN_GW",
                    "target_device_ids": ["dev-001"],
                },
                headers=_bearer(token),
            )
        listing = client.get("/api/plans", headers=_bearer(token)).json()
        plan_ids = {p["plan_id"] for p in listing["plans"]}
        assert "pl-ABCD1234" in plan_ids

    def test_get_unknown_id_404(
        self,
        client: TestClient,
        vault: Path,
    ) -> None:
        token = _unlock(client, vault)
        response = client.get("/api/plans/pl-NOTHERE", headers=_bearer(token))
        assert response.status_code == 404

    def test_delete_existing(
        self,
        client: TestClient,
        vault: Path,
    ) -> None:
        token = _unlock(client, vault)
        with patch(
            "opn_cockpit.web.api.plans.Planner.create_plan",
            return_value=_fake_route_plan("pl-DEL01234"),
        ):
            client.post(
                "/api/plans/route",
                json={
                    "network": "10.0.0.0/24",
                    "gateway": "WAN_GW",
                    "target_device_ids": ["dev-001"],
                },
                headers=_bearer(token),
            )
        delete = client.delete("/api/plans/pl-DEL01234", headers=_bearer(token))
        assert delete.status_code == 204
        # Danach 404
        assert client.get(
            "/api/plans/pl-DEL01234", headers=_bearer(token),
        ).status_code == 404


# ---------------------------------------------------------------------------
# POST /api/plans/{id}/apply
# ---------------------------------------------------------------------------


class TestApplyPlan:
    def test_applies_and_returns_report(
        self,
        client: TestClient,
        vault: Path,
    ) -> None:
        token = _unlock(client, vault)
        with patch(
            "opn_cockpit.web.api.plans.Planner.create_plan",
            return_value=_fake_route_plan(),
        ):
            client.post(
                "/api/plans/route",
                json={
                    "network": "10.0.0.0/24",
                    "gateway": "WAN_GW",
                    "target_device_ids": ["dev-001", "dev-002"],
                },
                headers=_bearer(token),
            )
        with patch(
            "opn_cockpit.web.api.plans.Executor.apply",
            return_value=_fake_report(),
        ):
            response = client.post(
                "/api/plans/pl-ABCD1234/apply",
                headers=_bearer(token),
            )
        assert response.status_code == 200
        body = response.json()
        assert body["plan_id"] == "pl-ABCD1234"
        assert body["total"] == 2
        assert body["successes"] == 1
        assert body["failures"] == 1
        statuses = {r["status"] for r in body["results"]}
        assert "Verifiziert" in statuses
        assert "Fehlgeschlagen" in statuses

    def test_apply_unknown_plan_404(
        self,
        client: TestClient,
        vault: Path,
    ) -> None:
        token = _unlock(client, vault)
        response = client.post(
            "/api/plans/pl-NOTHERE/apply",
            headers=_bearer(token),
        )
        assert response.status_code == 404
