"""Tests fuer run_pending_migrations."""

from __future__ import annotations

from pathlib import Path

import pytest

from opn_cockpit import __version__
from opn_cockpit.config import AppSettings
from opn_cockpit.migrations import registry as registry_mod
from opn_cockpit.migrations.errors import MigrationError
from opn_cockpit.migrations.registry import Migration, MigrationContext
from opn_cockpit.migrations.runner import (
    pending_migrations,
    run_pending_migrations,
)
from opn_cockpit.migrations.state import MigrationState
from opn_cockpit.web.server_state import VAULT_PATH_ENV


def _isolate_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Hebelt die Vault-Path-Env aus und liefert ein leeres Daten-Verzeichnis."""
    data_dir = tmp_path / "appdata"
    data_dir.mkdir()
    monkeypatch.delenv(VAULT_PATH_ENV, raising=False)
    return data_dir


def _set_registry(
    monkeypatch: pytest.MonkeyPatch, migrations: list[Migration],
) -> None:
    monkeypatch.setattr(registry_mod, "MIGRATIONS", migrations)
    monkeypatch.setattr(
        "opn_cockpit.migrations.runner.MIGRATIONS",
        migrations,
    )


class TestSkippedWhenNothingPending:
    def test_no_migrations_returns_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        data_dir = _isolate_data_dir(tmp_path, monkeypatch)
        _set_registry(monkeypatch, [])
        state_path = data_dir / "migrations.json"

        result = run_pending_migrations(
            data_dir=data_dir,
            settings=AppSettings(),
            state_path=state_path,
        )
        assert result.skipped is True
        assert result.applied_ids == ()
        assert result.backup is None

    def test_last_app_version_is_recorded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        data_dir = _isolate_data_dir(tmp_path, monkeypatch)
        _set_registry(monkeypatch, [])
        state_path = data_dir / "migrations.json"

        run_pending_migrations(
            data_dir=data_dir,
            settings=AppSettings(),
            state_path=state_path,
        )
        state = MigrationState.load(state_path)
        assert state.last_app_version == __version__


class TestAppliesPending:
    def test_applies_single_migration_with_backup(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        data_dir = _isolate_data_dir(tmp_path, monkeypatch)
        called: list[Path] = []

        def up(ctx: MigrationContext) -> None:
            called.append(ctx.app_data_dir)

        _set_registry(monkeypatch, [
            Migration(id="2026-06-01-001-test", description="test", up=up),
        ])
        state_path = data_dir / "migrations.json"

        result = run_pending_migrations(
            data_dir=data_dir,
            settings=AppSettings(),
            state_path=state_path,
        )
        assert result.skipped is False
        assert result.applied_ids == ("2026-06-01-001-test",)
        assert result.backup is not None
        assert result.backup.path.exists()
        assert called == [data_dir]

        state = MigrationState.load(state_path)
        assert "2026-06-01-001-test" in state.applied_ids
        assert state.last_app_version == __version__

    def test_already_applied_is_skipped_next_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        data_dir = _isolate_data_dir(tmp_path, monkeypatch)
        counter = {"n": 0}

        def up(_ctx: MigrationContext) -> None:
            counter["n"] += 1

        _set_registry(monkeypatch, [
            Migration(id="m1", description="x", up=up),
        ])
        state_path = data_dir / "migrations.json"

        run_pending_migrations(
            data_dir=data_dir, settings=AppSettings(), state_path=state_path,
        )
        run_pending_migrations(
            data_dir=data_dir, settings=AppSettings(), state_path=state_path,
        )
        assert counter["n"] == 1

    def test_skip_backup_flag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        data_dir = _isolate_data_dir(tmp_path, monkeypatch)
        _set_registry(monkeypatch, [
            Migration(id="m1", description="x", up=lambda ctx: None),
        ])
        result = run_pending_migrations(
            data_dir=data_dir,
            settings=AppSettings(),
            state_path=data_dir / "migrations.json",
            skip_backup=True,
        )
        assert result.backup is None


class TestFailurePropagation:
    def test_migration_error_is_propagated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        data_dir = _isolate_data_dir(tmp_path, monkeypatch)

        def bad(_ctx: MigrationContext) -> None:
            raise MigrationError("boom")

        _set_registry(monkeypatch, [
            Migration(id="m1", description="x", up=bad),
        ])
        with pytest.raises(MigrationError):
            run_pending_migrations(
                data_dir=data_dir,
                settings=AppSettings(),
                state_path=data_dir / "migrations.json",
            )

    def test_arbitrary_exception_is_wrapped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        data_dir = _isolate_data_dir(tmp_path, monkeypatch)

        def bad(_ctx: MigrationContext) -> None:
            raise RuntimeError("unexpected")

        _set_registry(monkeypatch, [
            Migration(id="m1", description="x", up=bad),
        ])
        with pytest.raises(MigrationError) as exc_info:
            run_pending_migrations(
                data_dir=data_dir,
                settings=AppSettings(),
                state_path=data_dir / "migrations.json",
            )
        assert "unexpected" in str(exc_info.value)

    def test_partial_progress_is_persisted_after_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        data_dir = _isolate_data_dir(tmp_path, monkeypatch)

        def good(_ctx: MigrationContext) -> None:
            return None

        def bad(_ctx: MigrationContext) -> None:
            raise MigrationError("nope")

        _set_registry(monkeypatch, [
            Migration(id="m1", description="ok", up=good),
            Migration(id="m2", description="fail", up=bad),
        ])
        state_path = data_dir / "migrations.json"
        with pytest.raises(MigrationError):
            run_pending_migrations(
                data_dir=data_dir,
                settings=AppSettings(),
                state_path=state_path,
            )
        state = MigrationState.load(state_path)
        # m1 sollte als applied gelten, m2 nicht.
        assert "m1" in state.applied_ids
        assert "m2" not in state.applied_ids
        # last_app_version darf nicht aktualisiert worden sein.
        assert state.last_app_version != __version__


class TestPendingFilter:
    def test_pending_migrations_filters_applied(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _set_registry(monkeypatch, [
            Migration(id="m1", description="x", up=lambda c: None),
            Migration(id="m2", description="y", up=lambda c: None),
        ])
        state = MigrationState()
        state.mark_applied("m1", app_version="0.6.0")
        result = pending_migrations(state)
        assert [m.id for m in result] == ["m2"]
