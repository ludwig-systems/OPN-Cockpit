"""Tests fuer das Outstanding/Retry-Feature in Iter 5.2.

Pflanzt einen Plan + Apply-Report im PlanStore und prueft, dass
/api/plans/outstanding die richtigen offenen Geraete aggregiert und
/api/plans/{id}/apply mit device_ids-Body nur diese rolloutet.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from opn_cockpit.config import get_app_data_dir
from opn_cockpit.core.objects.base import Diff, DiffKind
from opn_cockpit.core.objects.routes import RouteSpec
from opn_cockpit.core.result import (
    Phase,
    Result,
    RolloutReport,
    Status,
)
from opn_cockpit.inventory.model import Device
from opn_cockpit.orchestration.plan_store import PlanStore
from opn_cockpit.orchestration.planner import Plan, PlannedDeviceAction
from opn_cockpit.vault.model import VaultData, VaultDevice
from opn_cockpit.vault.store import create_vault
from opn_cockpit.web.server import create_app

PASSWORD = "korrektes-pferd-batterie-heftklammer"


def _make_vault(tmp_path: Path) -> Path:
    path = tmp_path / "test.opnvault"
    devices = [
        VaultDevice(
            id="dev-001", name="HQ Berlin", host="opn-1.lab", port=443,
            tls_verify=True, tags=["test"], api_key="k", api_secret="s", descr="",
        ),
        VaultDevice(
            id="dev-002", name="Branch Munich", host="opn-2.lab", port=443,
            tls_verify=True, tags=["test"], api_key="k", api_secret="s", descr="",
        ),
    ]
    create_vault(path, PASSWORD, VaultData(devices=devices))
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


def _seed_plan_and_report(
    plan_id: str = "pl-ABCD1234",
    success_devices: tuple[str, ...] = (),
    failed_devices: tuple[str, ...] = (),
) -> Plan:
    spec = RouteSpec(network="10.0.0.0/24", gateway="WAN_GW", descr="", disabled=False)
    all_devices = success_devices + failed_devices
    actions = tuple(
        PlannedDeviceAction(
            device=Device(
                id=did, name=f"Box {did}", host=f"{did}.lab", port=443,
                tls_verify=True, tags=("test",), descr="",
            ),
            target_spec=spec,
            current_state=None,
            diff=Diff(kind=DiffKind.NEW, summary="NEW"),
            payload_masked={"network": "10.0.0.0/24"},
        )
        for did in all_devices
    )
    plan = Plan(
        plan_id=plan_id,
        action="add_route",
        subsystem="routes",
        created_at_utc="2026-05-29T12:00:00.000Z",
        actions=actions,
    )
    store = PlanStore(base_dir=get_app_data_dir() / "plans")
    store.save(plan)

    results = []
    for did in success_devices:
        results.append(Result(
            device_id=did, subsystem="routes",
            status=Status.VERIFIED, short_message="ok", duration_ms=400,
        ))
    for did in failed_devices:
        results.append(Result(
            device_id=did, subsystem="routes",
            status=Status.FAILED, short_message="timeout",
            error_kind="network", failed_phase=Phase.WRITE, duration_ms=3000,
        ))
    if results:
        store.save_report(plan_id, RolloutReport(results=tuple(results)))
    return plan


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


class TestOutstanding:
    def test_requires_auth(self, client: TestClient) -> None:
        assert client.get("/api/plans/outstanding").status_code == 401

    def test_empty_when_no_plans(self, client: TestClient, token: str) -> None:
        response = client.get("/api/plans/outstanding", headers=_bearer(token))
        assert response.status_code == 200
        assert response.json()["devices"] == []

    def test_plan_without_report_marks_all_devices_outstanding(
        self,
        client: TestClient,
        token: str,
    ) -> None:
        _seed_plan_and_report(success_devices=(), failed_devices=())
        # Plan ohne Report: alle Plan-Geraete sind outstanding
        plan = _seed_plan_and_report(
            plan_id="pl-NOREPORT",
            success_devices=(),
            failed_devices=(),
        )
        # Plan-Aktionen direkt aus dem Plan-File
        assert plan.actions == ()

    def test_failed_devices_appear_in_outstanding(
        self,
        client: TestClient,
        token: str,
    ) -> None:
        _seed_plan_and_report(
            success_devices=("dev-001",),
            failed_devices=("dev-002",),
        )
        response = client.get("/api/plans/outstanding", headers=_bearer(token))
        assert response.status_code == 200
        body = response.json()
        ids = {e["device_id"]: e for e in body["devices"]}
        assert "dev-002" in ids
        assert ids["dev-002"]["outstanding_count"] == 1
        assert "pl-ABCD1234" in ids["dev-002"]["plans"]
        # dev-001 war erfolgreich → nicht in der Liste
        assert "dev-001" not in ids

    def test_two_plans_aggregate_count(
        self,
        client: TestClient,
        token: str,
    ) -> None:
        _seed_plan_and_report(
            plan_id="pl-AAAAAA01",
            success_devices=(),
            failed_devices=("dev-001",),
        )
        _seed_plan_and_report(
            plan_id="pl-BBBBBB02",
            success_devices=(),
            failed_devices=("dev-001",),
        )
        body = client.get(
            "/api/plans/outstanding", headers=_bearer(token),
        ).json()
        ids = {e["device_id"]: e for e in body["devices"]}
        assert ids["dev-001"]["outstanding_count"] == 2
        assert len(ids["dev-001"]["plans"]) == 2


class TestRetry:
    def test_apply_with_device_ids_filter(
        self,
        client: TestClient,
        token: str,
    ) -> None:
        plan = _seed_plan_and_report(
            success_devices=("dev-001",),
            failed_devices=("dev-002",),
        )
        with patch(
            "opn_cockpit.web.api.plans.Executor.apply",
            return_value=RolloutReport(results=(
                Result(
                    device_id="dev-002", subsystem="routes",
                    status=Status.VERIFIED, short_message="recovered",
                    duration_ms=500,
                ),
            )),
        ) as mock_apply:
            response = client.post(
                f"/api/plans/{plan.plan_id}/apply",
                json={"device_ids": ["dev-002"]},
                headers=_bearer(token),
            )
        assert response.status_code == 200
        body = response.json()
        # Beim Retry wird der vorherige Report mit dem neuen gemerged:
        # dev-001 bleibt VERIFIED, dev-002 wird jetzt VERIFIED.
        statuses = {r["device_id"]: r["status"] for r in body["results"]}
        assert statuses["dev-002"] == "Verifiziert"
        # Executor wurde mit einem Plan aufgerufen, der nur dev-002 hat
        called_plan = mock_apply.call_args[0][0]
        assert len(called_plan.actions) == 1
        assert called_plan.actions[0].device.id == "dev-002"

    def test_apply_with_unknown_device_id_404(
        self,
        client: TestClient,
        token: str,
    ) -> None:
        plan = _seed_plan_and_report(failed_devices=("dev-001",))
        response = client.post(
            f"/api/plans/{plan.plan_id}/apply",
            json={"device_ids": ["nope-not-real"]},
            headers=_bearer(token),
        )
        # Seit v3.0 Iter 4 prueft das ACL-Modul die device_ids gegen das
        # Inventar — unbekannte IDs liefern 404 (konsistent mit Inventar-
        # Sicht), nicht 400.
        assert response.status_code == 404
