"""Tests fuer SqlitePlanStore (v3.1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from opn_cockpit.core.objects.base import Diff, DiffKind
from opn_cockpit.core.objects.routes import RouteSpec
from opn_cockpit.core.result import Phase, Result, RolloutReport, Status
from opn_cockpit.inventory.model import Device
from opn_cockpit.orchestration.plan_store import PlanStoreError
from opn_cockpit.orchestration.planner import Plan, PlannedDeviceAction
from opn_cockpit.orchestration.sqlite_store import SqlitePlanStore
from opn_cockpit.storage.sqlite_db import SqliteDb


@pytest.fixture()
def store(tmp_path: Path) -> SqlitePlanStore:
    return SqlitePlanStore(db=SqliteDb(path=tmp_path / "plans.db"))


def _make_plan(plan_id: str = "pl-DEAD1234") -> Plan:
    device = Device(
        id="dev-001", name="HQ", host="opn.lab", port=443,
        tls_verify=True, tags=("germany",), descr="",
    )
    spec = RouteSpec(
        network="10.0.0.0/24", gateway="WAN_GW", descr="", disabled=False,
    )
    action = PlannedDeviceAction(
        device=device,
        target_spec=spec,
        current_state=None,
        diff=Diff(kind=DiffKind.NEW, summary="add 10.0.0.0/24"),
        payload_masked={"network": "10.0.0.0/24"},
    )
    return Plan(
        plan_id=plan_id,
        action="add_route",
        subsystem="routes",
        created_at_utc="2026-01-01T00:00:00.000Z",
        actions=(action,),
    )


class TestSaveLoad:
    def test_roundtrip(self, store: SqlitePlanStore) -> None:
        plan = _make_plan()
        store.save(plan)
        loaded = store.load(plan.plan_id)
        assert loaded.plan_id == plan.plan_id
        assert loaded.action == "add_route"
        assert len(loaded.actions) == 1
        assert loaded.actions[0].device.id == "dev-001"

    def test_load_missing_raises(self, store: SqlitePlanStore) -> None:
        with pytest.raises(PlanStoreError, match="nicht gefunden"):
            store.load("pl-DEAD9999")

    def test_invalid_id_rejected(self, store: SqlitePlanStore) -> None:
        with pytest.raises(PlanStoreError, match="gueltige Plan-ID"):
            store.load("not-an-id")

    def test_overwrite_replaces(self, store: SqlitePlanStore) -> None:
        plan = _make_plan()
        store.save(plan)
        # Plan mit gleicher ID neu speichern
        plan2 = _make_plan()
        store.save(plan2)
        assert store.list_ids() == [plan.plan_id]


class TestListAndDelete:
    def test_list_ids_sorted(self, store: SqlitePlanStore) -> None:
        for pid in ["pl-CCCC1111", "pl-AAAA2222", "pl-BBBB3333"]:
            store.save(_make_plan(plan_id=pid))
        assert store.list_ids() == ["pl-AAAA2222", "pl-BBBB3333", "pl-CCCC1111"]

    def test_delete_existing(self, store: SqlitePlanStore) -> None:
        plan = _make_plan()
        store.save(plan)
        assert store.delete(plan.plan_id) is True
        assert store.list_ids() == []

    def test_delete_missing_returns_false(self, store: SqlitePlanStore) -> None:
        # Gueltige Plan-ID (hex), aber nicht in der DB
        assert store.delete("pl-DEAD9999") is False


class TestReports:
    def test_save_and_load_report(self, store: SqlitePlanStore) -> None:
        plan = _make_plan()
        store.save(plan)
        report = RolloutReport(results=(
            Result(
                device_id="dev-001",
                subsystem="routes",
                status=Status.VERIFIED,
                short_message="ok",
                error_kind=None,
                failed_phase=None,
                duration_ms=120,
            ),
        ))
        store.save_report(plan.plan_id, report)
        loaded = store.load_report(plan.plan_id)
        assert loaded is not None
        assert len(loaded.results) == 1
        assert loaded.results[0].status == Status.VERIFIED

    def test_report_for_missing_plan(self, store: SqlitePlanStore) -> None:
        empty = store.load_report("pl-DEAD1234")
        assert empty is None

    def test_save_report_for_missing_plan_raises(
        self, store: SqlitePlanStore,
    ) -> None:
        report = RolloutReport(results=())
        with pytest.raises(PlanStoreError, match="nicht gefunden"):
            store.save_report("pl-DEAD9999", report)

    def test_report_with_failed_phase(self, store: SqlitePlanStore) -> None:
        plan = _make_plan()
        store.save(plan)
        report = RolloutReport(results=(
            Result(
                device_id="dev-001",
                subsystem="routes",
                status=Status.FAILED,
                short_message="connect timeout",
                error_kind="UnreachableError",
                failed_phase=Phase.WRITE,
                duration_ms=5000,
            ),
        ))
        store.save_report(plan.plan_id, report)
        loaded = store.load_report(plan.plan_id)
        assert loaded is not None
        assert loaded.results[0].failed_phase == Phase.WRITE


class TestPersistence:
    def test_plan_survives_reopen(self, tmp_path: Path) -> None:
        db_path = tmp_path / "plans.db"
        db1 = SqliteDb(path=db_path)
        store1 = SqlitePlanStore(db=db1)
        store1.save(_make_plan())
        db1.close()

        db2 = SqliteDb(path=db_path)
        store2 = SqlitePlanStore(db=db2)
        assert store2.list_ids() == ["pl-DEAD1234"]
