"""Tests für orchestration.plan_store — Save/Load-Roundtrip."""

from __future__ import annotations

from pathlib import Path

import pytest

from opn_cockpit.core.objects.base import Diff, DiffKind
from opn_cockpit.core.objects.routes import RouteSpec
from opn_cockpit.inventory.model import Device
from opn_cockpit.orchestration.plan_store import PlanStore, PlanStoreError
from opn_cockpit.orchestration.planner import Plan, PlannedDeviceAction


def _device(name: str = "Berlin", host: str = "opn-berlin.lab") -> Device:
    return Device(
        id=f"id-{host}",
        name=name,
        host=host,
        port=443,
        tls_verify=True,
        tags=("a", "b"),
        descr="",
    )


def _route_plan(plan_id: str = "pl-AB12CD34") -> Plan:
    spec = RouteSpec(network="10.0.0.0/24", gateway="WAN_GW", descr="x", disabled=False)
    return Plan(
        plan_id=plan_id,
        action="add_route",
        subsystem="routes",
        created_at_utc="2026-05-28T10:00:00.000Z",
        actions=(
            PlannedDeviceAction(
                device=_device(),
                target_spec=spec,
                current_state=None,
                diff=Diff(kind=DiffKind.NEW, summary="Neue Route"),
                payload_masked={"network": "10.0.0.0/24"},
            ),
        ),
    )


class TestSaveLoad:
    def test_roundtrip(self, tmp_path: Path) -> None:
        store = PlanStore(base_dir=tmp_path / "plans")
        plan = _route_plan()
        path = store.save(plan)
        assert path.exists()
        loaded = store.load(plan.plan_id)
        assert loaded.plan_id == plan.plan_id
        assert loaded.action == plan.action
        assert loaded.subsystem == plan.subsystem
        assert len(loaded.actions) == 1
        assert loaded.actions[0].device.name == "Berlin"
        assert isinstance(loaded.actions[0].target_spec, RouteSpec)
        assert loaded.actions[0].target_spec.network == "10.0.0.0/24"

    def test_load_by_path(self, tmp_path: Path) -> None:
        store = PlanStore(base_dir=tmp_path / "plans")
        path = store.save(_route_plan())
        loaded = store.load(str(path))
        assert loaded.plan_id == "pl-AB12CD34"

    def test_current_state_roundtrip(self, tmp_path: Path) -> None:
        store = PlanStore(base_dir=tmp_path / "plans")
        spec = RouteSpec(network="10/24".replace("/24", ".0.0/24"), gateway="GW")
        current = RouteSpec(network="10.0.0.0/24", gateway="GW", descr="alt", disabled=True)
        plan = Plan(
            plan_id="pl-12345678",
            action="add_route",
            subsystem="routes",
            created_at_utc="t",
            actions=(
                PlannedDeviceAction(
                    device=_device(),
                    target_spec=spec,
                    current_state=current,
                    diff=Diff(kind=DiffKind.SKIP, summary="existiert"),
                    payload_masked={},
                ),
            ),
        )
        store.save(plan)
        loaded = store.load(plan.plan_id)
        assert loaded.actions[0].current_state is not None
        assert isinstance(loaded.actions[0].current_state, RouteSpec)
        assert loaded.actions[0].current_state.descr == "alt"


class TestErrors:
    def test_unknown_id_raises(self, tmp_path: Path) -> None:
        store = PlanStore(base_dir=tmp_path / "plans")
        with pytest.raises(PlanStoreError):
            store.load("pl-DEADBEEF")

    def test_invalid_identifier_raises(self, tmp_path: Path) -> None:
        store = PlanStore(base_dir=tmp_path / "plans")
        with pytest.raises(PlanStoreError):
            store.load("not-a-plan-id")

    def test_corrupt_json_raises(self, tmp_path: Path) -> None:
        store = PlanStore(base_dir=tmp_path / "plans")
        (tmp_path / "plans").mkdir()
        (tmp_path / "plans" / "pl-AB12CD34.json").write_text("not json")
        with pytest.raises(PlanStoreError):
            store.load("pl-AB12CD34")

    def test_unknown_subsystem_raises_on_load(self, tmp_path: Path) -> None:
        store = PlanStore(base_dir=tmp_path / "plans")
        (tmp_path / "plans").mkdir()
        (tmp_path / "plans" / "pl-AAAAAAAA.json").write_text(
            '{"plan_id":"pl-AAAAAAAA","subsystem":"nonsense","actions":[]}'
        )
        with pytest.raises(PlanStoreError):
            store.load("pl-AAAAAAAA")


class TestListIds:
    def test_lists_saved_plans_sorted(self, tmp_path: Path) -> None:
        store = PlanStore(base_dir=tmp_path / "plans")
        store.save(_route_plan(plan_id="pl-BBBBBBBB"))
        store.save(_route_plan(plan_id="pl-AAAAAAAA"))
        assert store.list_ids() == ["pl-AAAAAAAA", "pl-BBBBBBBB"]

    def test_empty_dir_returns_empty(self, tmp_path: Path) -> None:
        store = PlanStore(base_dir=tmp_path / "plans")
        assert store.list_ids() == []
