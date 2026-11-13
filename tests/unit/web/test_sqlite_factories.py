"""End-to-End-Smoke: AppSettings.storage_backend=sqlite schaltet Factories um."""

from __future__ import annotations

from pathlib import Path

import pytest

from opn_cockpit.audit.backend import (
    SqliteAuditBackend,
    get_audit_backend,
    reset_db_cache,
)
from opn_cockpit.audit.log import AuditLog
from opn_cockpit.orchestration.backend import get_plan_store_backend
from opn_cockpit.orchestration.plan_store import PlanStore
from opn_cockpit.orchestration.sqlite_store import SqlitePlanStore
from opn_cockpit.profiles.backend import get_profile_store_backend
from opn_cockpit.profiles.sqlite_store import SqliteProfileStore
from opn_cockpit.profiles.store import ProfileStore


@pytest.fixture(autouse=True)
def _isolate_db_cache() -> None:
    """Frischer DB-Cache pro Test — sonst leckt eine Instanz zwischen Tests."""
    reset_db_cache()
    yield
    reset_db_cache()


@pytest.fixture()
def filesystem_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPNCOCKPIT_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("OPNCOCKPIT_STORAGE_BACKEND", raising=False)


@pytest.fixture()
def sqlite_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPNCOCKPIT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("OPNCOCKPIT_STORAGE_BACKEND", "sqlite")


class TestFilesystemDefault:
    def test_audit_is_file_log(self, filesystem_env: None) -> None:
        assert isinstance(get_audit_backend(), AuditLog)

    def test_plans_is_file_store(self, filesystem_env: None) -> None:
        assert isinstance(get_plan_store_backend(), PlanStore)

    def test_profiles_is_file_store(self, filesystem_env: None) -> None:
        assert isinstance(get_profile_store_backend(), ProfileStore)


class TestSqliteSwitch:
    def test_audit_is_sqlite(self, sqlite_env: None) -> None:
        assert isinstance(get_audit_backend(), SqliteAuditBackend)

    def test_plans_is_sqlite(self, sqlite_env: None) -> None:
        assert isinstance(get_plan_store_backend(), SqlitePlanStore)

    def test_profiles_is_sqlite(self, sqlite_env: None) -> None:
        assert isinstance(get_profile_store_backend(), SqliteProfileStore)

    def test_three_backends_share_one_db(self, sqlite_env: None) -> None:
        """Memory-Optimierung: alle drei Factories liefern Backends mit
        derselben Connection."""
        audit = get_audit_backend()
        plans = get_plan_store_backend()
        profiles = get_profile_store_backend()
        assert isinstance(audit, SqliteAuditBackend)
        assert isinstance(plans, SqlitePlanStore)
        assert isinstance(profiles, SqliteProfileStore)
        assert audit.db is plans.db is profiles.db
