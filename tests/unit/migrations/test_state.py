"""Tests fuer MigrationState (Persistenz + Idempotenz)."""

from __future__ import annotations

import json
from pathlib import Path

from opn_cockpit.migrations.state import AppliedMigration, MigrationState


class TestMigrationStatePersistence:
    def test_load_missing_file_returns_empty_state(self, tmp_path: Path) -> None:
        state = MigrationState.load(tmp_path / "missing.json")
        assert state.applied == []
        assert state.last_app_version is None

    def test_load_invalid_json_returns_empty_state(self, tmp_path: Path) -> None:
        target = tmp_path / "broken.json"
        target.write_text("not-json", encoding="utf-8")
        state = MigrationState.load(target)
        assert state.applied == []
        assert state.last_app_version is None

    def test_save_then_load_roundtrip(self, tmp_path: Path) -> None:
        target = tmp_path / "state.json"
        state = MigrationState(
            applied=[
                AppliedMigration(
                    id="2026-06-01-001-init",
                    applied_at_iso="2026-06-01T10:00:00Z",
                    app_version="0.6.0",
                ),
            ],
            last_app_version="0.6.0",
        )
        state.save(target)
        loaded = MigrationState.load(target)
        assert loaded.applied_ids == frozenset({"2026-06-01-001-init"})
        assert loaded.last_app_version == "0.6.0"

    def test_save_is_atomic_via_tempfile(self, tmp_path: Path) -> None:
        target = tmp_path / "state.json"
        state = MigrationState(last_app_version="0.6.0")
        state.save(target)
        # No leftover .tmp file
        leftovers = list(tmp_path.glob("*.tmp"))
        assert leftovers == []

    def test_save_creates_parent_dirs(self, tmp_path: Path) -> None:
        target = tmp_path / "nested" / "deep" / "state.json"
        MigrationState().save(target)
        assert target.exists()

    def test_load_ignores_malformed_entries(self, tmp_path: Path) -> None:
        target = tmp_path / "state.json"
        target.write_text(
            json.dumps({
                "applied": [
                    {"id": "good", "applied_at_iso": "now", "app_version": "0.6.0"},
                    {"id": 42},  # malformed
                    "not-a-dict",  # malformed
                ],
                "last_app_version": "0.6.0",
            }),
            encoding="utf-8",
        )
        loaded = MigrationState.load(target)
        assert loaded.applied_ids == frozenset({"good"})


class TestMarkApplied:
    def test_mark_applied_adds_entry(self) -> None:
        state = MigrationState()
        entry = state.mark_applied("m1", app_version="0.6.0")
        assert entry.id == "m1"
        assert state.applied_ids == frozenset({"m1"})

    def test_mark_applied_is_idempotent(self) -> None:
        state = MigrationState()
        first = state.mark_applied("m1", app_version="0.6.0")
        second = state.mark_applied("m1", app_version="0.6.0")
        assert first is second
        assert len(state.applied) == 1
