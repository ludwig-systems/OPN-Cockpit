"""Tests für core.result — Status-Hierarchie und Aggregation."""

from __future__ import annotations

import pytest

from opn_cockpit.core.result import (
    AddOutcome,
    Phase,
    Result,
    RolloutReport,
    Status,
    VerifyOutcome,
)


class TestStatus:
    def test_verified_and_skipped_count_as_success(self) -> None:
        verified = Result(device_id="d", subsystem="routes", status=Status.VERIFIED)
        skipped = Result(device_id="d", subsystem="routes", status=Status.SKIPPED)
        assert verified.is_success()
        assert skipped.is_success()

    @pytest.mark.parametrize("status", [Status.WRITTEN, Status.ACTIVATED, Status.FAILED])
    def test_partial_or_failed_are_not_success(self, status: Status) -> None:
        result = Result(device_id="d", subsystem="routes", status=status)
        assert not result.is_success()


class TestResult:
    def test_failed_carries_phase_and_kind(self) -> None:
        result = Result(
            device_id="opn-lab",
            subsystem="routes",
            status=Status.FAILED,
            error_kind="auth",
            failed_phase=Phase.WRITE,
            short_message="Auth abgelehnt",
        )
        assert result.failed_phase is Phase.WRITE
        assert result.error_kind == "auth"

    def test_add_and_verify_outcomes_are_attachable(self) -> None:
        result = Result(
            device_id="d",
            subsystem="routes",
            status=Status.VERIFIED,
            add_outcome=AddOutcome(uuid="11111111", raw_status=200),
            verify_outcome=VerifyOutcome(found=True, detail="uuid=11111111"),
        )
        assert result.add_outcome is not None
        assert result.add_outcome.uuid == "11111111"
        assert result.verify_outcome is not None
        assert result.verify_outcome.found


class TestRolloutReport:
    def test_aggregates_counts(self) -> None:
        report = RolloutReport(
            results=(
                Result(device_id="a", subsystem="routes", status=Status.VERIFIED),
                Result(device_id="b", subsystem="routes", status=Status.SKIPPED),
                Result(
                    device_id="c",
                    subsystem="routes",
                    status=Status.FAILED,
                    failed_phase=Phase.VERIFY,
                ),
            )
        )
        assert report.total == 3
        assert report.successes == 2
        assert report.failures == 1
        assert report.skipped == 1
        assert not report.all_successful()

    def test_empty_report_is_not_successful(self) -> None:
        empty = RolloutReport()
        assert empty.total == 0
        assert not empty.all_successful()

    def test_all_verified_is_all_successful(self) -> None:
        report = RolloutReport(
            results=(
                Result(device_id="a", subsystem="routes", status=Status.VERIFIED),
                Result(device_id="b", subsystem="routes", status=Status.VERIFIED),
            )
        )
        assert report.all_successful()


class TestOutcomeContracts:
    """Vertragstest: Outcome-Typen halten keine rohen Bodies."""

    def test_add_outcome_fields(self) -> None:
        slots = AddOutcome.__slots__
        forbidden = {"body", "raw", "response"}
        assert not (set(slots) & forbidden)

    def test_verify_outcome_fields(self) -> None:
        slots = VerifyOutcome.__slots__
        forbidden = {"body", "raw", "response"}
        assert not (set(slots) & forbidden)
