"""Tests für orchestration.reporter — Formatierung."""

from __future__ import annotations

from opn_cockpit.core.objects.base import Diff, DiffKind
from opn_cockpit.core.result import Phase, Result, RolloutReport, Status
from opn_cockpit.inventory.model import Device
from opn_cockpit.orchestration.planner import Plan, PlannedDeviceAction
from opn_cockpit.orchestration.reporter import (
    format_plan_preview,
    format_plan_summary,
    format_rollout_matrix,
    format_rollout_summary,
)


def _device(name: str = "Berlin", *, tls: bool = True) -> Device:
    return Device(
        id=f"id-{name}", name=name, host=f"opn-{name.lower()}.lab",
        port=443, tls_verify=tls, tags=(), descr="",
    )


def _plan() -> Plan:
    return Plan(
        plan_id="pl-AB12CD34",
        action="add_route",
        subsystem="routes",
        created_at_utc="2026-05-28T10:00:00.000Z",
        actions=(
            PlannedDeviceAction(
                device=_device("Berlin"),
                target_spec="spec1",
                current_state=None,
                diff=Diff(kind=DiffKind.NEW, summary="Neue Route 10/24"),
                payload_masked={"network": "10.0.0.0/24"},
            ),
            PlannedDeviceAction(
                device=_device("Munich", tls=False),
                target_spec="spec1",
                current_state="spec1",
                diff=Diff(kind=DiffKind.SKIP, summary="vorhanden"),
                payload_masked={"network": "10.0.0.0/24"},
            ),
        ),
    )


class TestPlanSummary:
    def test_includes_plan_id_action_counts(self) -> None:
        out = format_plan_summary(_plan())
        assert "pl-AB12CD34" in out
        assert "add_route" in out
        assert "2 Ziel" in out


class TestPlanPreview:
    def test_lists_each_device(self) -> None:
        out = format_plan_preview(_plan())
        assert "Berlin" in out
        assert "Munich" in out
        assert "NEW" in out
        assert "SKIP" in out

    def test_marks_tls_off(self) -> None:
        out = format_plan_preview(_plan())
        assert "Risiko" in out  # für Munich (tls_verify=False)


class TestRolloutSummary:
    def test_format(self) -> None:
        report = RolloutReport(
            results=(
                Result(device_id="a", subsystem="routes", status=Status.VERIFIED),
                Result(device_id="b", subsystem="routes", status=Status.SKIPPED),
                Result(device_id="c", subsystem="routes", status=Status.FAILED),
            )
        )
        out = format_rollout_summary(report)
        assert "2/3" in out
        assert "1 fehlgeschlagen" in out
        assert "1 übersprungen" in out


class TestRolloutMatrix:
    def test_uses_display_names_when_provided(self) -> None:
        report = RolloutReport(
            results=(
                Result(
                    device_id="d1", subsystem="routes",
                    status=Status.VERIFIED, short_message="ok", duration_ms=120,
                ),
            )
        )
        out = format_rollout_matrix(report, devices_by_id={"d1": "Berlin"})
        assert "Berlin" in out
        assert "OK" in out
        assert "120" in out

    def test_falls_back_to_id(self) -> None:
        report = RolloutReport(
            results=(
                Result(device_id="my-id", subsystem="routes", status=Status.SKIPPED),
            )
        )
        out = format_rollout_matrix(report)
        assert "my-id" in out

    def test_failed_shows_phase(self) -> None:
        report = RolloutReport(
            results=(
                Result(
                    device_id="d1", subsystem="routes",
                    status=Status.WRITTEN,
                    failed_phase=Phase.ACTIVATE,
                    short_message="reconfigure ko",
                ),
            )
        )
        out = format_rollout_matrix(report)
        assert "activate" in out

    def test_empty_report_clean_message(self) -> None:
        out = format_rollout_matrix(RolloutReport())
        assert "keine Ergebnisse" in out
