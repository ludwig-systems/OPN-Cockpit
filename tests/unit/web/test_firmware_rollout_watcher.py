"""Tests fuer den FirmwareRolloutWatcher (Iteration B).

Fokus: Submit-Kontrakt + Cancel + Persistenz-Round-Trip + State-Filter.
Die volle State-Machine (queued -> triggered -> running -> rebooting ->
verifying -> done) wird nicht End-to-End getestet, weil sie gegen echte
OPNsense-Boxen ohnehin nur qualitativ verifizierbar ist.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from opn_cockpit.web.firmware_rollout_watcher import (
    DEV_STATE_QUEUED,
    ROLLOUT_STATE_DONE,
    ROLLOUT_STATE_RUNNING,
    ROLLOUT_STATE_SCHEDULED,
    FirmwareRollout,
    FirmwareRolloutDevice,
    FirmwareRolloutWatcher,
    RolloutBusyError,
)


def _mk_manager() -> MagicMock:
    mgr = MagicMock()
    # Der Watcher fragt den SessionManager ueber snapshot_active() ab.
    # Leere Liste = keine entsperrte Session, also keine Adoption.
    mgr.snapshot_active.return_value = []
    return mgr


def _mk_audit() -> MagicMock:
    audit = MagicMock()
    return audit


class TestSubmit:
    def test_submit_creates_queued_entries(self, tmp_path: Path) -> None:
        watcher = FirmwareRolloutWatcher(
            _mk_manager(),
            _mk_audit(),
            queue_path=tmp_path / "rollout.json",
        )
        rollout = watcher.submit(
            initiator="admin",
            vault_path=str(tmp_path / "vault.opnvault"),
            devices=[("d1", "HQ", ""), ("d2", "Branch", "")],
            mode="update",
            continue_on_error=False,
        )
        assert rollout.state == ROLLOUT_STATE_RUNNING
        assert len(rollout.devices) == 2
        assert all(d.state == DEV_STATE_QUEUED for d in rollout.devices)
        assert rollout.devices[0].device_name == "HQ"
        assert rollout.devices[1].position == 1

    def test_submit_rejects_invalid_mode(self, tmp_path: Path) -> None:
        watcher = FirmwareRolloutWatcher(_mk_manager(), _mk_audit())
        with pytest.raises(ValueError, match="mode"):
            watcher.submit(
                initiator="a",
                vault_path=str(tmp_path / "v.opnvault"),
                devices=[("d1", "n", "")],
                mode="rollback",
                continue_on_error=False,
            )

    def test_submit_rejects_empty_devices(self, tmp_path: Path) -> None:
        watcher = FirmwareRolloutWatcher(_mk_manager(), _mk_audit())
        with pytest.raises(ValueError, match="mindestens"):
            watcher.submit(
                initiator="a",
                vault_path="x",
                devices=[],
                mode="update",
                continue_on_error=False,
            )

    def test_second_submit_busy_error(self, tmp_path: Path) -> None:
        watcher = FirmwareRolloutWatcher(
            _mk_manager(),
            _mk_audit(),
            queue_path=tmp_path / "rollout.json",
        )
        watcher.submit(
            initiator="admin",
            vault_path="v",
            devices=[("d1", "HQ", "")],
            mode="update",
            continue_on_error=False,
        )
        with pytest.raises(RolloutBusyError):
            watcher.submit(
                initiator="admin",
                vault_path="v",
                devices=[("d2", "Branch", "")],
                mode="update",
                continue_on_error=False,
            )


class TestScheduling:
    def test_immediate_when_time_is_zero(self, tmp_path: Path) -> None:
        watcher = FirmwareRolloutWatcher(
            _mk_manager(),
            _mk_audit(),
            queue_path=tmp_path / "rollout.json",
        )
        rollout = watcher.submit(
            initiator="admin",
            vault_path="v",
            devices=[("d1", "HQ", "")],
            mode="update",
            continue_on_error=False,
            scheduled_start_at_ms=0,
        )
        assert rollout.state == ROLLOUT_STATE_RUNNING
        assert rollout.scheduled_start_at_ms == 0

    def test_scheduled_when_time_in_future(self, tmp_path: Path) -> None:
        import time

        future_ms = int(time.time() * 1000) + 3600 * 1000  # 1h in Zukunft
        watcher = FirmwareRolloutWatcher(
            _mk_manager(),
            _mk_audit(),
            queue_path=tmp_path / "rollout.json",
        )
        rollout = watcher.submit(
            initiator="admin",
            vault_path="v",
            devices=[("d1", "HQ", "")],
            mode="update",
            continue_on_error=False,
            scheduled_start_at_ms=future_ms,
        )
        assert rollout.state == ROLLOUT_STATE_SCHEDULED
        assert rollout.scheduled_start_at_ms == future_ms

    def test_past_scheduled_time_starts_immediately(self, tmp_path: Path) -> None:
        # UI-Uhr-Skew: wenn der User "vor 5 min" waehlt, soll das kein
        # Fehler sein — Watcher startet sofort.
        watcher = FirmwareRolloutWatcher(
            _mk_manager(),
            _mk_audit(),
            queue_path=tmp_path / "rollout.json",
        )
        rollout = watcher.submit(
            initiator="admin",
            vault_path="v",
            devices=[("d1", "HQ", "")],
            mode="update",
            continue_on_error=False,
            scheduled_start_at_ms=1,  # 1970 - sehr weit in der Vergangenheit
        )
        assert rollout.state == ROLLOUT_STATE_RUNNING
        assert rollout.scheduled_start_at_ms == 0

    def test_second_submit_blocked_while_scheduled(self, tmp_path: Path) -> None:
        import time

        future_ms = int(time.time() * 1000) + 3600 * 1000
        watcher = FirmwareRolloutWatcher(
            _mk_manager(),
            _mk_audit(),
            queue_path=tmp_path / "rollout.json",
        )
        watcher.submit(
            initiator="admin",
            vault_path="v",
            devices=[("d1", "HQ", "")],
            mode="update",
            continue_on_error=False,
            scheduled_start_at_ms=future_ms,
        )
        with pytest.raises(RolloutBusyError):
            watcher.submit(
                initiator="admin",
                vault_path="v",
                devices=[("d2", "Branch", "")],
                mode="update",
                continue_on_error=False,
            )

    def test_cancel_scheduled_immediate(self, tmp_path: Path) -> None:
        import time

        future_ms = int(time.time() * 1000) + 3600 * 1000
        watcher = FirmwareRolloutWatcher(
            _mk_manager(),
            _mk_audit(),
            queue_path=tmp_path / "rollout.json",
        )
        watcher.submit(
            initiator="admin",
            vault_path="v",
            devices=[("d1", "HQ", "")],
            mode="update",
            continue_on_error=False,
            scheduled_start_at_ms=future_ms,
        )
        assert watcher.cancel(initiator="admin") is True
        rollout = watcher.stats()
        assert rollout is not None
        assert rollout.cancel_requested is True

    def test_scheduled_persists_across_watcher_restart(self, tmp_path: Path) -> None:
        import time

        future_ms = int(time.time() * 1000) + 3600 * 1000
        queue = tmp_path / "rollout.json"
        w1 = FirmwareRolloutWatcher(
            _mk_manager(),
            _mk_audit(),
            queue_path=queue,
        )
        w1.submit(
            initiator="admin",
            vault_path="v",
            devices=[("d1", "HQ", "")],
            mode="upgrade",
            continue_on_error=False,
            scheduled_start_at_ms=future_ms,
        )
        # neue Watcher-Instanz -> laedt aus disk
        w2 = FirmwareRolloutWatcher(
            _mk_manager(),
            _mk_audit(),
            queue_path=queue,
        )
        rollout = w2.stats()
        assert rollout is not None
        assert rollout.state == ROLLOUT_STATE_SCHEDULED
        assert rollout.scheduled_start_at_ms == future_ms


class TestCancel:
    def test_cancel_marks_flag(self, tmp_path: Path) -> None:
        watcher = FirmwareRolloutWatcher(_mk_manager(), _mk_audit())
        watcher.submit(
            initiator="admin",
            vault_path="v",
            devices=[("d1", "HQ", "")],
            mode="update",
            continue_on_error=False,
        )
        assert watcher.cancel(initiator="admin") is True
        rollout = watcher.stats()
        assert rollout is not None
        assert rollout.cancel_requested is True

    def test_cancel_without_rollout_returns_false(self, tmp_path: Path) -> None:
        watcher = FirmwareRolloutWatcher(_mk_manager(), _mk_audit())
        assert watcher.cancel(initiator="admin") is False


class TestClearTerminal:
    def test_clear_running_rejected(self, tmp_path: Path) -> None:
        watcher = FirmwareRolloutWatcher(_mk_manager(), _mk_audit())
        watcher.submit(
            initiator="admin",
            vault_path="v",
            devices=[("d1", "HQ", "")],
            mode="update",
            continue_on_error=False,
        )
        assert watcher.clear_terminal() is False
        assert watcher.stats() is not None

    def test_clear_terminal_ok(self, tmp_path: Path) -> None:
        watcher = FirmwareRolloutWatcher(_mk_manager(), _mk_audit())
        # Rollout direkt in DONE injizieren (State-Machine-Umgehung fuer Test).
        watcher._rollout = FirmwareRollout(
            rollout_id="abc",
            created_at_ms=0,
            vault_path="v",
            mode="update",
            continue_on_error=False,
            state=ROLLOUT_STATE_DONE,
            finished_at_ms=1,
        )
        assert watcher.clear_terminal() is True
        assert watcher.stats() is None


class TestPersistence:
    def test_write_and_read_round_trip(self, tmp_path: Path) -> None:
        queue = tmp_path / "rollout.json"
        w1 = FirmwareRolloutWatcher(
            _mk_manager(),
            _mk_audit(),
            queue_path=queue,
        )
        w1.submit(
            initiator="admin",
            vault_path="v",
            devices=[("d1", "HQ", "26.1"), ("d2", "Branch", "")],
            mode="upgrade",
            continue_on_error=True,
        )
        assert queue.exists()

        # Neue Instanz - laedt aus disk.
        w2 = FirmwareRolloutWatcher(
            _mk_manager(),
            _mk_audit(),
            queue_path=queue,
        )
        rollout = w2.stats()
        assert rollout is not None
        assert rollout.mode == "upgrade"
        assert rollout.continue_on_error is True
        assert len(rollout.devices) == 2
        assert rollout.devices[0].target_version == "26.1"
        assert rollout.devices[1].device_name == "Branch"

    def test_json_shape(self, tmp_path: Path) -> None:
        queue = tmp_path / "rollout.json"
        watcher = FirmwareRolloutWatcher(
            _mk_manager(),
            _mk_audit(),
            queue_path=queue,
        )
        watcher.submit(
            initiator="a",
            vault_path="v",
            devices=[("d1", "HQ", "")],
            mode="update",
            continue_on_error=False,
        )
        data = json.loads(queue.read_text(encoding="utf-8"))
        assert data["version"] == 1
        assert data["rollout"]["mode"] == "update"
        assert data["rollout"]["devices"][0]["device_id"] == "d1"


class TestDeviceEntries:
    def test_device_position_and_state_default(self) -> None:
        d = FirmwareRolloutDevice(
            device_id="d1",
            device_name="HQ",
            position=0,
            state=DEV_STATE_QUEUED,
        )
        assert d.state == DEV_STATE_QUEUED
        assert d.version_before == ""
        assert d.version_after == ""
        assert d.summary == ""


# ===========================================================================
# Wartungsfenster (v0.12 V2)
# ===========================================================================


from datetime import datetime  # noqa: E402

from opn_cockpit.web.firmware_rollout_watcher import (  # noqa: E402
    ROLLOUT_STATE_CANCELLED,
    ROLLOUT_STATE_PAUSED,
    is_within_window,
    next_window_open_ms,
    parse_hhmm,
)


class TestHhmmHelpers:
    def test_parse_hhmm_valid(self) -> None:
        t = parse_hhmm("22:00")
        assert t is not None
        assert t.hour == 22 and t.minute == 0

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "22",
            "22:",
            "22:00:00",
            "25:00",
            "22:60",
            "abc",
            "-1:00",
        ],
    )
    def test_parse_hhmm_invalid(self, bad: str) -> None:
        assert parse_hhmm(bad) is None

    def test_same_day_window(self) -> None:
        # 09:00 - 17:00 (Business Hours)
        assert is_within_window(
            datetime(2026, 7, 20, 10, 0),
            "09:00",
            "17:00",
        )
        assert not is_within_window(
            datetime(2026, 7, 20, 8, 59),
            "09:00",
            "17:00",
        )
        # 17:00 selbst ist "end" -> exclusive
        assert not is_within_window(
            datetime(2026, 7, 20, 17, 0),
            "09:00",
            "17:00",
        )

    def test_overnight_window(self) -> None:
        # 22:00 - 06:00 (Nacht-Wartungsfenster)
        assert is_within_window(
            datetime(2026, 7, 20, 22, 30),
            "22:00",
            "06:00",
        )
        assert is_within_window(
            datetime(2026, 7, 20, 3, 0),
            "22:00",
            "06:00",
        )
        # Nachmittags: draussen
        assert not is_within_window(
            datetime(2026, 7, 20, 15, 0),
            "22:00",
            "06:00",
        )
        # 06:00 exklusive
        assert not is_within_window(
            datetime(2026, 7, 20, 6, 0),
            "22:00",
            "06:00",
        )

    def test_no_window_always_open(self) -> None:
        assert is_within_window(datetime(2026, 7, 20, 15, 0), "", "")

    def test_next_open_computed(self) -> None:
        # Aktuell 15:00, Fenster 22:00-06:00 → naechste Oeffnung heute 22:00
        now = datetime(2026, 7, 20, 15, 0)
        next_ms = next_window_open_ms(now, "22:00", "06:00")
        expected = datetime(2026, 7, 20, 22, 0).timestamp() * 1000
        assert abs(next_ms - expected) < 1

        # Aktuell 23:00 (im Fenster), naechste Oeffnung ist morgen 22:00
        # (Aufrufer prueft vorher ob Fenster offen ist - Helper geht davon aus).
        now = datetime(2026, 7, 20, 23, 0)
        next_ms = next_window_open_ms(now, "22:00", "06:00")
        expected = datetime(2026, 7, 21, 22, 0).timestamp() * 1000
        assert abs(next_ms - expected) < 1


class TestWindowSubmitValidation:
    def test_both_or_neither_enforced(self, tmp_path: Path) -> None:
        watcher = FirmwareRolloutWatcher(_mk_manager(), _mk_audit())
        with pytest.raises(ValueError, match="BEIDE"):
            watcher.submit(
                initiator="a",
                vault_path="v",
                devices=[("d1", "HQ", "")],
                mode="update",
                continue_on_error=False,
                window_start_hhmm="22:00",
                window_end_hhmm="",
            )

    def test_invalid_hhmm_rejected(self, tmp_path: Path) -> None:
        watcher = FirmwareRolloutWatcher(_mk_manager(), _mk_audit())
        with pytest.raises(ValueError, match="Wartungsfenster"):
            watcher.submit(
                initiator="a",
                vault_path="v",
                devices=[("d1", "HQ", "")],
                mode="update",
                continue_on_error=False,
                window_start_hhmm="25:00",
                window_end_hhmm="06:00",
            )

    def test_identical_start_end_rejected(self, tmp_path: Path) -> None:
        watcher = FirmwareRolloutWatcher(_mk_manager(), _mk_audit())
        with pytest.raises(ValueError, match="identisch"):
            watcher.submit(
                initiator="a",
                vault_path="v",
                devices=[("d1", "HQ", "")],
                mode="update",
                continue_on_error=False,
                window_start_hhmm="22:00",
                window_end_hhmm="22:00",
            )

    def test_valid_window_stored(self, tmp_path: Path) -> None:
        watcher = FirmwareRolloutWatcher(_mk_manager(), _mk_audit())
        rollout = watcher.submit(
            initiator="a",
            vault_path="v",
            devices=[("d1", "HQ", "")],
            mode="update",
            continue_on_error=False,
            window_start_hhmm="22:00",
            window_end_hhmm="06:00",
        )
        assert rollout.window_start_hhmm == "22:00"
        assert rollout.window_end_hhmm == "06:00"


class TestWindowTickBehavior:
    def _fresh_watcher(self, tmp_path: Path) -> FirmwareRolloutWatcher:
        # Custom tick_s hoch damit der Loop-Thread nichts unterwegs macht.
        watcher = FirmwareRolloutWatcher(
            _mk_manager(),
            _mk_audit(),
            queue_path=tmp_path / "rollout.json",
            tick_s=999.0,
        )
        # Session-Adoption ist unwichtig - wir fangen den Trigger ab.
        return watcher

    def test_tick_pauses_when_out_of_window(self, tmp_path: Path) -> None:
        watcher = self._fresh_watcher(tmp_path)
        watcher.submit(
            initiator="a",
            vault_path="v",
            devices=[("d1", "HQ", "")],
            mode="update",
            continue_on_error=False,
            window_start_hhmm="22:00",
            window_end_hhmm="06:00",
        )
        # 15:00 heute - Fenster geschlossen
        now_ms = int(datetime(2026, 7, 20, 15, 0).timestamp() * 1000)
        watcher._tick(now_ms)
        r = watcher.stats()
        assert r is not None
        assert r.state == ROLLOUT_STATE_PAUSED
        assert r.paused_until_ms > now_ms

    def test_tick_running_when_in_window(self, tmp_path: Path) -> None:
        watcher = self._fresh_watcher(tmp_path)
        watcher.submit(
            initiator="a",
            vault_path="v",
            devices=[("d1", "HQ", "")],
            mode="update",
            continue_on_error=False,
            window_start_hhmm="22:00",
            window_end_hhmm="06:00",
        )
        # 23:00 - Fenster offen. Ohne Session bleibt der Trigger im process()
        # zwar untaetig, aber State ist running (nicht paused).
        now_ms = int(datetime(2026, 7, 20, 23, 0).timestamp() * 1000)
        watcher._tick(now_ms)
        r = watcher.stats()
        assert r is not None
        assert r.state == ROLLOUT_STATE_RUNNING

    def test_paused_resumes_when_window_opens(self, tmp_path: Path) -> None:
        watcher = self._fresh_watcher(tmp_path)
        watcher.submit(
            initiator="a",
            vault_path="v",
            devices=[("d1", "HQ", "")],
            mode="update",
            continue_on_error=False,
            window_start_hhmm="22:00",
            window_end_hhmm="06:00",
        )
        # Erst pausieren (14:00)
        watcher._tick(
            int(
                datetime(2026, 7, 20, 14, 0).timestamp() * 1000,
            )
        )
        assert watcher.stats().state == ROLLOUT_STATE_PAUSED
        # Dann Fenster oeffnen (23:00)
        watcher._tick(
            int(
                datetime(2026, 7, 20, 23, 0).timestamp() * 1000,
            )
        )
        assert watcher.stats().state == ROLLOUT_STATE_RUNNING
        assert watcher.stats().paused_until_ms == 0

    def test_cancel_in_paused_terminates_immediately(self, tmp_path: Path) -> None:
        watcher = self._fresh_watcher(tmp_path)
        watcher.submit(
            initiator="a",
            vault_path="v",
            devices=[("d1", "HQ", "")],
            mode="update",
            continue_on_error=False,
            window_start_hhmm="22:00",
            window_end_hhmm="06:00",
        )
        watcher._tick(
            int(
                datetime(2026, 7, 20, 14, 0).timestamp() * 1000,
            )
        )
        assert watcher.stats().state == ROLLOUT_STATE_PAUSED
        assert watcher.cancel(initiator="a") is True
        watcher._tick(
            int(
                datetime(2026, 7, 20, 14, 30).timestamp() * 1000,
            )
        )
        assert watcher.stats().state == ROLLOUT_STATE_CANCELLED

    def test_no_window_no_pause(self, tmp_path: Path) -> None:
        # Ohne Fenster darf der Watcher nie in paused gehen, auch nicht
        # nachts.
        watcher = self._fresh_watcher(tmp_path)
        watcher.submit(
            initiator="a",
            vault_path="v",
            devices=[("d1", "HQ", "")],
            mode="update",
            continue_on_error=False,
        )
        watcher._tick(
            int(
                datetime(2026, 7, 20, 3, 0).timestamp() * 1000,
            )
        )
        assert watcher.stats().state == ROLLOUT_STATE_RUNNING
