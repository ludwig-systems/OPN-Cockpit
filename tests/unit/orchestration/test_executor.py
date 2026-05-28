"""Tests für orchestration.executor — Phasen-Pipeline, Best-Effort, Audit."""

from __future__ import annotations

from pathlib import Path

import pytest

from opn_cockpit.audit.log import AuditEventKind, AuditLog
from opn_cockpit.core.errors import (
    AuthError,
    ReconfigureError,
    UnreachableError,
    make_context,
)
from opn_cockpit.core.objects.base import Diff, DiffKind, VerifyOutcome
from opn_cockpit.core.result import Phase, Status
from opn_cockpit.inventory.model import Device
from opn_cockpit.orchestration.executor import Executor, group_by_device
from opn_cockpit.orchestration.planner import Plan, PlannedDeviceAction
from opn_cockpit.vault.model import VaultDevice
from tests.unit.orchestration.conftest import (
    FakeAdapter,
    FakeController,
    make_client_for_hosts,
    make_session,
)


def _dev(name: str, host: str) -> tuple[Device, VaultDevice]:
    vd = VaultDevice(
        id=f"id-{host}", name=name, host=host, port=443,
        tls_verify=False, api_key="K", api_secret="S",
    )
    return Device.from_vault_device(vd), vd


def _plan(action: str, actions: list[PlannedDeviceAction]) -> Plan:
    return Plan(
        plan_id="pl-TESTTEST",
        action=action,
        subsystem=FakeAdapter.subsystem,
        created_at_utc="2026-05-28T10:00:00.000Z",
        actions=tuple(actions),
    )


def _planned(device: Device, spec: str, *, kind: DiffKind = DiffKind.NEW) -> PlannedDeviceAction:
    return PlannedDeviceAction(
        device=device,
        target_spec=spec,
        current_state=None if kind is DiffKind.NEW else spec,
        diff=Diff(kind=kind, summary="x"),
        payload_masked={"spec": spec},
    )


# ---------------------------------------------------------------------------
# group_by_device
# ---------------------------------------------------------------------------


class TestGroupByDevice:
    def test_groups_actions_per_device(self) -> None:
        dev1, _ = _dev("A", "host-a")
        dev2, _ = _dev("B", "host-b")
        plan = _plan("add", [
            _planned(dev1, "s1"),
            _planned(dev2, "s2"),
            _planned(dev1, "s3"),
        ])
        pipelines = group_by_device(plan)
        assert len(pipelines) == 2
        host_a = next(p for p in pipelines if p.device.host == "host-a")
        assert len(host_a.actions) == 2

    def test_preserves_device_order(self) -> None:
        dev1, _ = _dev("A", "host-a")
        dev2, _ = _dev("B", "host-b")
        plan = _plan("add", [_planned(dev2, "s2"), _planned(dev1, "s1")])
        pipelines = group_by_device(plan)
        assert [p.device.host for p in pipelines] == ["host-b", "host-a"]


# ---------------------------------------------------------------------------
# Pipeline: Erfolgsfall
# ---------------------------------------------------------------------------


class TestSuccessPath:
    def test_verified_status(self, audit: AuditLog) -> None:
        dev, vd = _dev("A", "host-a")
        session = make_session([vd])
        adapter = FakeAdapter()
        controller = FakeController()
        plan = _plan("add_route", [_planned(dev, "spec1")])

        with make_client_for_hosts(["host-a"]) as client:
            executor = Executor(session=session, audit=audit, max_workers=1)
            report = executor.apply(
                plan, adapter=adapter, controller=controller, client=client
            )
        assert report.total == 1
        assert report.results[0].status is Status.VERIFIED
        assert adapter.add_calls == [("host-a", "spec1")]
        assert controller.reconfigure_calls == ["host-a"]
        assert adapter.verify_calls == [("host-a", "id::spec1")]


class TestParallelism:
    def test_multiple_devices_all_succeed(self, audit: AuditLog) -> None:
        dev1, vd1 = _dev("A", "host-a")
        dev2, vd2 = _dev("B", "host-b")
        dev3, vd3 = _dev("C", "host-c")
        session = make_session([vd1, vd2, vd3])
        adapter = FakeAdapter()
        controller = FakeController()
        plan = _plan("add_route", [
            _planned(dev1, "s1"),
            _planned(dev2, "s2"),
            _planned(dev3, "s3"),
        ])
        with make_client_for_hosts(["host-a", "host-b", "host-c"]) as client:
            executor = Executor(session=session, audit=audit, max_workers=3)
            report = executor.apply(
                plan, adapter=adapter, controller=controller, client=client
            )
        assert report.total == 3
        assert report.failures == 0
        assert report.successes == 3
        # ein reconfigure pro Gerät
        assert sorted(controller.reconfigure_calls) == ["host-a", "host-b", "host-c"]


# ---------------------------------------------------------------------------
# Pipeline: Fehler in jeder Phase
# ---------------------------------------------------------------------------


class TestWriteFailure:
    def test_failed_write_status(self, audit: AuditLog) -> None:
        dev, vd = _dev("A", "host-a")
        session = make_session([vd])
        adapter = FakeAdapter(
            add_raises={"host-a": AuthError("nope", context=make_context(error_kind="auth"))}
        )
        controller = FakeController()
        plan = _plan("add_route", [_planned(dev, "spec1")])

        with make_client_for_hosts(["host-a"]) as client:
            executor = Executor(session=session, audit=audit, max_workers=1)
            report = executor.apply(
                plan, adapter=adapter, controller=controller, client=client
            )
        result = report.results[0]
        assert result.status is Status.FAILED
        assert result.failed_phase is Phase.WRITE
        assert result.error_kind == "auth"
        # reconfigure wurde NICHT aufgerufen
        assert controller.reconfigure_calls == []


class TestReconfigureFailure:
    def test_failed_reconfigure_status(self, audit: AuditLog) -> None:
        dev, vd = _dev("A", "host-a")
        session = make_session([vd])
        adapter = FakeAdapter()
        controller = FakeController(
            raises_for={"host-a": ReconfigureError(
                "kaputt",
                context=make_context(error_kind="reconfigure", summary="503"),
            )}
        )
        plan = _plan("add_route", [_planned(dev, "spec1")])
        with make_client_for_hosts(["host-a"]) as client:
            executor = Executor(session=session, audit=audit, max_workers=1)
            report = executor.apply(
                plan, adapter=adapter, controller=controller, client=client
            )
        result = report.results[0]
        assert result.status is Status.WRITTEN
        assert result.failed_phase is Phase.ACTIVATE
        assert result.error_kind == "reconfigure"


class TestVerifyFailure:
    def test_verify_returns_not_found(self, audit: AuditLog) -> None:
        dev, vd = _dev("A", "host-a")
        session = make_session([vd])
        adapter = FakeAdapter(
            verify_returns={"host-a": VerifyOutcome(found=False, detail="leer")}
        )
        controller = FakeController()
        plan = _plan("add_route", [_planned(dev, "spec1")])
        with make_client_for_hosts(["host-a"]) as client:
            executor = Executor(session=session, audit=audit, max_workers=1)
            report = executor.apply(
                plan, adapter=adapter, controller=controller, client=client
            )
        result = report.results[0]
        assert result.status is Status.ACTIVATED
        assert result.failed_phase is Phase.VERIFY
        assert result.error_kind == "verification"


# ---------------------------------------------------------------------------
# Best-Effort
# ---------------------------------------------------------------------------


class TestBestEffort:
    def test_one_failed_device_does_not_block_others(
        self, audit: AuditLog
    ) -> None:
        dev1, vd1 = _dev("A", "host-a")
        dev2, vd2 = _dev("B", "host-b")
        session = make_session([vd1, vd2])
        adapter = FakeAdapter(
            add_raises={
                "host-a": UnreachableError(
                    "offline",
                    context=make_context(error_kind="network"),
                )
            }
        )
        controller = FakeController()
        plan = _plan("add_route", [_planned(dev1, "s1"), _planned(dev2, "s2")])
        with make_client_for_hosts(["host-a", "host-b"]) as client:
            executor = Executor(session=session, audit=audit, max_workers=2)
            report = executor.apply(
                plan, adapter=adapter, controller=controller, client=client
            )
        assert report.total == 2
        assert report.successes == 1
        assert report.failures == 1


# ---------------------------------------------------------------------------
# Idempotenz (SKIP)
# ---------------------------------------------------------------------------


class TestSkip:
    def test_all_skip_results_in_skipped_status(self, audit: AuditLog) -> None:
        dev, vd = _dev("A", "host-a")
        session = make_session([vd])
        adapter = FakeAdapter()
        controller = FakeController()
        plan = _plan("add_route", [_planned(dev, "spec1", kind=DiffKind.SKIP)])
        with make_client_for_hosts(["host-a"]) as client:
            executor = Executor(session=session, audit=audit, max_workers=1)
            report = executor.apply(
                plan, adapter=adapter, controller=controller, client=client
            )
        assert report.results[0].status is Status.SKIPPED
        assert adapter.add_calls == []
        assert controller.reconfigure_calls == []


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


class TestAuditEvents:
    def test_writes_apply_started_completed_and_device_results(
        self, audit: AuditLog
    ) -> None:
        dev1, vd1 = _dev("A", "host-a")
        dev2, vd2 = _dev("B", "host-b")
        session = make_session([vd1, vd2])
        adapter = FakeAdapter()
        controller = FakeController()
        plan = _plan("add_route", [_planned(dev1, "s1"), _planned(dev2, "s2")])
        with make_client_for_hosts(["host-a", "host-b"]) as client:
            executor = Executor(session=session, audit=audit, max_workers=2)
            executor.apply(plan, adapter=adapter, controller=controller, client=client)

        records = audit.read_all()
        events = [r.event for r in records]
        assert AuditEventKind.APPLY_STARTED in events
        assert AuditEventKind.APPLY_COMPLETED in events
        device_results = [r for r in records if r.event is AuditEventKind.DEVICE_RESULT]
        assert len(device_results) == 2


# ---------------------------------------------------------------------------
# Unerwartete Exception
# ---------------------------------------------------------------------------


class TestUnexpectedException:
    def test_pool_does_not_crash_on_runtime_error(
        self, audit: AuditLog
    ) -> None:
        dev, vd = _dev("A", "host-a")
        session = make_session([vd])
        adapter = FakeAdapter(add_raises={"host-a": RuntimeError("oops")})
        controller = FakeController()
        plan = _plan("add_route", [_planned(dev, "spec1")])
        with make_client_for_hosts(["host-a"]) as client:
            executor = Executor(session=session, audit=audit, max_workers=1)
            report = executor.apply(
                plan, adapter=adapter, controller=controller, client=client
            )
        result = report.results[0]
        assert result.status is Status.FAILED
        assert result.error_kind == "unexpected"


@pytest.fixture()
def audit(tmp_path: Path) -> AuditLog:
    return AuditLog(path=tmp_path / "audit.jsonl", actor="test")
