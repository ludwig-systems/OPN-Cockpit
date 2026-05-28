"""Tests für profiles.store — CRUD + Sanitizer + Persistenz."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from opn_cockpit.profiles.store import (
    Profile,
    ProfileStore,
    ProfileStoreError,
    generate_profile_id,
)


def _store(tmp_path: Path) -> ProfileStore:
    return ProfileStore(path=tmp_path / "profiles.json")


class TestProfileId:
    def test_format(self) -> None:
        for _ in range(10):
            pid = generate_profile_id()
            assert pid.startswith("prof-")
            assert len(pid) == 13  # prof- + 8 hex


class TestSaveAndLoad:
    def test_empty_when_missing(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        assert store.list_profiles() == []

    def test_save_then_list(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        profile = store.save_new(
            name="Standard-Routen",
            action="add_route",
            subsystem="routes",
            default_selector="tag:branches",
            spec={"network": "10.0.0.0/24", "gateway": "WAN_GW"},
        )
        assert profile.id
        listed = store.list_profiles()
        assert len(listed) == 1
        assert listed[0].name == "Standard-Routen"

    def test_persists_across_instances(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.save_new(
            name="X", action="add_route", subsystem="routes",
            default_selector="all", spec={"network": "10.0/8"},
        )
        # zweite Instanz auf derselben Datei
        store2 = _store(tmp_path)
        assert len(store2.list_profiles()) == 1

    def test_get_by_id(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        p = store.save_new(
            name="X", action="add_route", subsystem="routes",
            default_selector="all", spec={},
        )
        fetched = store.get(p.id)
        assert fetched == p

    def test_get_unknown_raises(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        with pytest.raises(ProfileStoreError):
            store.get("nope")

    def test_find_by_name(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.save_new(
            name="Foo", action="a", subsystem="routes",
            default_selector="all", spec={},
        )
        assert store.find_by_name("Foo") is not None
        assert store.find_by_name("Missing") is None


class TestValidation:
    def test_rejects_duplicate_name(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.save_new(
            name="X", action="a", subsystem="routes",
            default_selector="all", spec={},
        )
        with pytest.raises(ProfileStoreError):
            store.save_new(
                name="X", action="b", subsystem="routes",
                default_selector="all", spec={},
            )

    def test_rejects_empty_name(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        with pytest.raises(ProfileStoreError):
            store.save_new(
                name="", action="a", subsystem="routes",
                default_selector="all", spec={},
            )


class TestDelete:
    def test_delete_existing(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        p = store.save_new(
            name="X", action="a", subsystem="routes",
            default_selector="all", spec={},
        )
        assert store.delete(p.id) is True
        assert store.list_profiles() == []

    def test_delete_unknown_returns_false(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        assert store.delete("does-not-exist") is False


class TestSanitizer:
    def test_secret_keys_are_stripped(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        p = store.save_new(
            name="X", action="a", subsystem="routes",
            default_selector="all",
            spec={
                "network": "10/8",
                "api_secret": "SHOULD-NEVER-LAND-HERE",
                "password": "nope",
            },
        )
        # Im gespeicherten Profil und auf Platte: keine Spur der Secrets.
        assert "api_secret" not in p.spec
        assert "password" not in p.spec
        on_disk = (tmp_path / "profiles.json").read_text(encoding="utf-8")
        assert "SHOULD-NEVER-LAND-HERE" not in on_disk
        assert "nope" not in on_disk

    def test_load_also_sanitizes(self, tmp_path: Path) -> None:
        # Jemand legt eine profiles.json manuell an, in der ein
        # api_secret-Feld steht. Beim Lesen muss es entfernt werden.
        (tmp_path / "profiles.json").write_text(
            json.dumps({
                "schema_version": 1,
                "profiles": [{
                    "id": "prof-AAAAAAAA",
                    "name": "Bad",
                    "action": "a",
                    "subsystem": "routes",
                    "default_selector": "all",
                    "spec": {"network": "10/8", "api_secret": "X"},
                }],
            }),
            encoding="utf-8",
        )
        store = _store(tmp_path)
        loaded = store.list_profiles()
        assert len(loaded) == 1
        assert "api_secret" not in loaded[0].spec


class TestDefensiveReader:
    def test_garbage_json_raises(self, tmp_path: Path) -> None:
        (tmp_path / "profiles.json").write_text("not json", encoding="utf-8")
        store = _store(tmp_path)
        with pytest.raises(ProfileStoreError):
            store.list_profiles()

    def test_non_dict_wraps_to_empty(self, tmp_path: Path) -> None:
        (tmp_path / "profiles.json").write_text("[]", encoding="utf-8")
        store = _store(tmp_path)
        assert store.list_profiles() == []

    def test_profile_without_name_skipped(self, tmp_path: Path) -> None:
        (tmp_path / "profiles.json").write_text(
            json.dumps({"profiles": [{"id": "x", "name": "", "action": "a"}]}),
            encoding="utf-8",
        )
        store = _store(tmp_path)
        assert store.list_profiles() == []


class TestToDict:
    def test_to_dict_roundtrip_keys(self) -> None:
        p = Profile(
            id="prof-X", name="Y", action="add_route",
            subsystem="routes", default_selector="all",
            spec={"network": "10/8"},
        )
        d = p.to_dict()
        assert d["id"] == "prof-X"
        assert d["spec"] == {"network": "10/8"}
