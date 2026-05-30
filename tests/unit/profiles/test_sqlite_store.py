"""Tests fuer SqliteProfileStore (v3.1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from opn_cockpit.profiles.sqlite_store import SqliteProfileStore
from opn_cockpit.profiles.store import ProfileStoreError
from opn_cockpit.storage.sqlite_db import SqliteDb


@pytest.fixture()
def store(tmp_path: Path) -> SqliteProfileStore:
    return SqliteProfileStore(db=SqliteDb(path=tmp_path / "profiles.db"))


class TestSaveAndList:
    def test_save_and_get(self, store: SqliteProfileStore) -> None:
        profile = store.save_new(
            name="Standard-Route",
            action="add_route",
            subsystem="routes",
            default_selector="tag:branches",
            spec={"network": "10.0.0.0/24", "gateway": "WAN_GW"},
        )
        assert profile.id.startswith("prof-")
        fetched = store.get(profile.id)
        assert fetched.name == "Standard-Route"
        assert fetched.spec["network"] == "10.0.0.0/24"

    def test_list_sorted_by_name(self, store: SqliteProfileStore) -> None:
        store.save_new(
            name="Charlie", action="add_route", subsystem="routes",
            default_selector="all", spec={},
        )
        store.save_new(
            name="Alpha", action="add_route", subsystem="routes",
            default_selector="all", spec={},
        )
        store.save_new(
            name="Bravo", action="add_route", subsystem="routes",
            default_selector="all", spec={},
        )
        names = [p.name for p in store.list_profiles()]
        assert names == ["Alpha", "Bravo", "Charlie"]


class TestUniqueness:
    def test_duplicate_name_rejected(self, store: SqliteProfileStore) -> None:
        store.save_new(
            name="X", action="add_route", subsystem="routes",
            default_selector="all", spec={},
        )
        with pytest.raises(ProfileStoreError, match="existiert bereits"):
            store.save_new(
                name="X", action="add_route", subsystem="routes",
                default_selector="all", spec={},
            )

    def test_empty_name_rejected(self, store: SqliteProfileStore) -> None:
        with pytest.raises(ProfileStoreError, match="leer"):
            store.save_new(
                name="   ", action="add_route", subsystem="routes",
                default_selector="all", spec={},
            )


class TestSecuritySanitize:
    def test_secrets_stripped_from_spec(self, store: SqliteProfileStore) -> None:
        profile = store.save_new(
            name="Y", action="add_route", subsystem="routes",
            default_selector="all",
            spec={"network": "1.2.3.0/24", "api_secret": "leak", "password": "p"},
        )
        # Beim Re-Read aus der DB muessen verbotene Felder weg sein
        fetched = store.get(profile.id)
        assert "api_secret" not in fetched.spec
        assert "password" not in fetched.spec
        assert fetched.spec["network"] == "1.2.3.0/24"


class TestDelete:
    def test_delete_existing(self, store: SqliteProfileStore) -> None:
        p = store.save_new(
            name="Z", action="add_route", subsystem="routes",
            default_selector="all", spec={},
        )
        assert store.delete(p.id) is True
        assert store.find_by_name("Z") is None

    def test_delete_missing_returns_false(self, store: SqliteProfileStore) -> None:
        assert store.delete("prof-NOTREAL") is False


class TestFindByName:
    def test_find_existing(self, store: SqliteProfileStore) -> None:
        store.save_new(
            name="Beta", action="add_alias", subsystem="firewall_alias",
            default_selector="all", spec={"name": "branch_ips"},
        )
        found = store.find_by_name("Beta")
        assert found is not None
        assert found.subsystem == "firewall_alias"

    def test_find_missing_returns_none(self, store: SqliteProfileStore) -> None:
        assert store.find_by_name("NotThere") is None


class TestPersistence:
    def test_survives_reopen(self, tmp_path: Path) -> None:
        db_path = tmp_path / "profiles.db"
        db1 = SqliteDb(path=db_path)
        s1 = SqliteProfileStore(db=db1)
        s1.save_new(
            name="P", action="add_route", subsystem="routes",
            default_selector="all", spec={"x": 1},
        )
        db1.close()

        db2 = SqliteDb(path=db_path)
        s2 = SqliteProfileStore(db=db2)
        listed = s2.list_profiles()
        assert len(listed) == 1
        assert listed[0].name == "P"
        assert listed[0].spec["x"] == 1
