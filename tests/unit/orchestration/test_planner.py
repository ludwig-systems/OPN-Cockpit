"""Tests für orchestration.planner — Plan-Erzeugung, Pre-Check, Diff."""

from __future__ import annotations

from pathlib import Path

import pytest

from opn_cockpit.audit.log import AuditEventKind, AuditLog
from opn_cockpit.core.errors import OpnCockpitError, make_context
from opn_cockpit.inventory.model import Device
from opn_cockpit.orchestration.planner import (
    Plan,
    Planner,
    generate_plan_id,
)
from opn_cockpit.vault.model import VaultDevice
from tests.unit.orchestration.conftest import (
    FakeAdapter,
    make_client_for_hosts,
    make_session,
)


def _dev(name: str, host: str, *, vault_id: str | None = None) -> tuple[Device, VaultDevice]:
    vd = VaultDevice(
        id=vault_id or f"id-{host}",
        name=name,
        host=host,
        port=443,
        tls_verify=False,
        api_key="K",
        api_secret="S",
    )
    return Device.from_vault_device(vd), vd


class TestPlanId:
    def test_format(self) -> None:
        for _ in range(20):
            pid = generate_plan_id()
            assert pid.startswith("pl-")
            assert len(pid) == 11  # "pl-" + 8 Hex

    def test_unique(self) -> None:
        ids = {generate_plan_id() for _ in range(50)}
        assert len(ids) == 50


class TestEmptyPlan:
    def test_no_devices(self, audit: AuditLog) -> None:
        session = make_session([])
        planner = Planner(audit=audit, session=session)
        adapter = FakeAdapter()
        client = make_client_for_hosts([])
        plan = planner.create_plan(
            action="add_route", spec="netA", devices=[], adapter=adapter, client=client
        )
        assert plan.target_count == 0
        assert plan.actions == ()


class TestPlanCreation:
    def test_marks_new_devices_correctly(self, audit: AuditLog) -> None:
        dev_a, vd_a = _dev("Berlin", "host-a")
        dev_b, vd_b = _dev("Munich", "host-b")
        session = make_session([vd_a, vd_b])
        planner = Planner(audit=audit, session=session, max_workers=2)
        adapter = FakeAdapter()
        client = make_client_for_hosts(["host-a", "host-b"])

        plan = planner.create_plan(
            action="add_route",
            spec="net1",
            devices=[dev_a, dev_b],
            adapter=adapter,
            client=client,
        )
        assert plan.target_count == 2
        assert plan.to_apply_count == 2
        assert plan.skip_count == 0

    def test_existing_devices_marked_as_skip(self, audit: AuditLog) -> None:
        dev_a, vd_a = _dev("A", "host-a")
        dev_b, vd_b = _dev("B", "host-b")
        session = make_session([vd_a, vd_b])
        adapter = FakeAdapter(existing={"host-a": "net1"})  # A hat das schon
        client = make_client_for_hosts(["host-a", "host-b"])
        planner = Planner(audit=audit, session=session, max_workers=2)

        plan = planner.create_plan(
            action="add_route", spec="net1",
            devices=[dev_a, dev_b], adapter=adapter, client=client,
        )
        assert plan.to_apply_count == 1
        assert plan.skip_count == 1


class TestAuditOnPlan:
    def test_writes_plan_generated_event(self, audit: AuditLog) -> None:
        dev, vd = _dev("X", "host-x")
        session = make_session([vd])
        planner = Planner(audit=audit, session=session)
        adapter = FakeAdapter()
        client = make_client_for_hosts(["host-x"])

        planner.create_plan(
            action="add_route", spec="net", devices=[dev], adapter=adapter, client=client
        )
        records = audit.read_all()
        assert len(records) == 1
        assert records[0].event is AuditEventKind.PLAN_GENERATED
        assert records[0].action == "add_route"
        assert records[0].target_count == 1


class TestPreCheckFailureDegradesToNew:
    def test_pre_check_error_does_not_break_plan(self, audit: AuditLog) -> None:
        dev, vd = _dev("X", "host-x")
        session = make_session([vd])
        adapter = FakeAdapter()

        def failing_exists(*_a: object, **_kw: object) -> object:
            raise OpnCockpitError(
                "Pre-Check kaputt",
                context=make_context(error_kind="network", summary="offline"),
            )

        adapter.exists = failing_exists  # type: ignore[assignment]
        planner = Planner(audit=audit, session=session)
        client = make_client_for_hosts(["host-x"])

        plan = planner.create_plan(
            action="add_route", spec="net", devices=[dev], adapter=adapter, client=client
        )
        # Fehler wird als NEW markiert (mit Hinweis im summary), Plan trotzdem erstellt.
        assert plan.target_count == 1
        assert plan.actions[0].diff.summary.startswith("Pre-Check fehlgeschlagen")


class TestPlanPropertyTotals:
    def test_to_apply_and_skip_count(self, audit: AuditLog) -> None:
        dev1, vd1 = _dev("a", "h1")
        dev2, vd2 = _dev("b", "h2")
        session = make_session([vd1, vd2])
        adapter = FakeAdapter(existing={"h1": "spec"})
        client = make_client_for_hosts(["h1", "h2"])
        planner = Planner(audit=audit, session=session)
        plan: Plan = planner.create_plan(
            action="a", spec="spec",
            devices=[dev1, dev2], adapter=adapter, client=client,
        )
        assert plan.skip_count + plan.to_apply_count == plan.target_count


@pytest.fixture()
def audit(tmp_path: Path) -> AuditLog:
    return AuditLog(path=tmp_path / "audit.jsonl", actor="test")
