"""Tests für vault.discovery — discover_vaults + default_new_vault_path."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from opn_cockpit.config import AppSettings
from opn_cockpit.vault.discovery import (
    VAULT_EXTENSION,
    default_new_vault_path,
    discover_vaults,
)


def _touch(path: Path, content: bytes = b"") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


class TestDefaultPath:
    def test_uses_app_data_dir(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"APPDATA": str(tmp_path)}):
            p = default_new_vault_path()
        assert p.name == f"main{VAULT_EXTENSION}"
        assert str(tmp_path) in str(p)


class TestDiscover:
    def test_empty_when_nothing_exists(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"APPDATA": str(tmp_path / "nada")}):
            settings = AppSettings()
            assert discover_vaults(settings) == []

    def test_picks_up_app_data_files(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"APPDATA": str(tmp_path)}):
            app_dir = tmp_path / "OPN-Cockpit"
            _touch(app_dir / f"alpha{VAULT_EXTENSION}")
            _touch(app_dir / f"beta{VAULT_EXTENSION}")
            settings = AppSettings()
            found = discover_vaults(settings)
        names = [p.name for p in found]
        assert sorted(names) == [f"alpha{VAULT_EXTENSION}", f"beta{VAULT_EXTENSION}"]

    def test_default_vault_comes_first(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"APPDATA": str(tmp_path)}):
            app_dir = tmp_path / "OPN-Cockpit"
            a = _touch(app_dir / f"alpha{VAULT_EXTENSION}")
            b = _touch(app_dir / f"beta{VAULT_EXTENSION}")
            settings = AppSettings(default_vault=str(b))
            found = discover_vaults(settings)
        assert found[0] == b
        # alpha kommt danach
        assert a in found

    def test_includes_recents_outside_app_dir(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"APPDATA": str(tmp_path)}):
            outside = _touch(
                tmp_path / "andere" / f"work{VAULT_EXTENSION}"
            )
            settings = AppSettings(recent_vaults=[str(outside)])
            found = discover_vaults(settings)
        assert outside in found

    def test_skips_recent_paths_that_no_longer_exist(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"APPDATA": str(tmp_path)}):
            settings = AppSettings(
                recent_vaults=[str(tmp_path / "gone.opnvault")]
            )
            found = discover_vaults(settings)
        assert found == []

    def test_deduplicates_paths(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"APPDATA": str(tmp_path)}):
            app_dir = tmp_path / "OPN-Cockpit"
            p = _touch(app_dir / f"alpha{VAULT_EXTENSION}")
            settings = AppSettings(
                default_vault=str(p),
                recent_vaults=[str(p), str(p)],
            )
            found = discover_vaults(settings)
        # Trotz dreifachem Eintrag (default, app-dir, recents) nur einmal drin.
        assert len(found) == 1
        assert found[0] == p

    def test_ignores_non_opnvault_files(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"APPDATA": str(tmp_path)}):
            app_dir = tmp_path / "OPN-Cockpit"
            _touch(app_dir / "notes.txt")
            _touch(app_dir / f"real{VAULT_EXTENSION}")
            settings = AppSettings()
            found = discover_vaults(settings)
        names = [p.name for p in found]
        assert names == [f"real{VAULT_EXTENSION}"]


class TestDefaultLoad:
    def test_load_when_no_settings_passed(self, tmp_path: Path) -> None:
        """discover_vaults() ohne Settings darf nicht crashen."""
        with patch.dict(os.environ, {"APPDATA": str(tmp_path / "nada")}):
            # Sollte keine Exception werfen, auch wenn keine AppSettings da sind.
            result = discover_vaults()
        assert isinstance(result, list)


@pytest.fixture()
def empty_settings() -> AppSettings:
    return AppSettings()
