"""Tests für config — AppPaths und AppSettings."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

from opn_cockpit.config import (
    APP_NAME,
    DEFAULT_RECENT_LIMIT,
    AppSettings,
    get_app_data_dir,
    get_settings_path,
)

# ---------------------------------------------------------------------------
# AppPaths
# ---------------------------------------------------------------------------


class TestAppDataDir:
    def test_uses_appdata_env_when_set(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"APPDATA": str(tmp_path)}):
            result = get_app_data_dir()
        assert result == tmp_path / APP_NAME

    def test_falls_back_to_xdg_or_home_dir(self, tmp_path: Path) -> None:
        env = dict(os.environ)
        env.pop("APPDATA", None)
        env.pop("XDG_DATA_HOME", None)
        env.pop("OPNCOCKPIT_DATA_DIR", None)
        with patch.dict(os.environ, env, clear=True):
            result = get_app_data_dir()
        # Auf einem System mit ~/.local existiert: XDG-Pfad.
        # Sonst: Dotfile-Fallback ~/.opn-cockpit.
        assert result.name.lower() in {APP_NAME.lower(), f".{APP_NAME.lower()}"}

    def test_explicit_override_via_env(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"OPNCOCKPIT_DATA_DIR": str(tmp_path / "custom")}):
            assert get_app_data_dir() == tmp_path / "custom"

    def test_xdg_data_home_used_when_set(self, tmp_path: Path) -> None:
        env = dict(os.environ)
        env.pop("APPDATA", None)
        env.pop("OPNCOCKPIT_DATA_DIR", None)
        env["XDG_DATA_HOME"] = str(tmp_path / "xdg")
        with patch.dict(os.environ, env, clear=True):
            result = get_app_data_dir()
        assert result == tmp_path / "xdg" / APP_NAME.lower()

    def test_settings_path_inside_app_data_dir(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"APPDATA": str(tmp_path)}):
            assert get_settings_path().parent == tmp_path / APP_NAME


# ---------------------------------------------------------------------------
# Load / Save
# ---------------------------------------------------------------------------


class TestLoadAndSave:
    def test_load_missing_returns_defaults(self, tmp_path: Path) -> None:
        path = tmp_path / "settings.json"
        settings = AppSettings.load(path)
        assert settings.recent_vaults == []
        assert settings.default_vault is None
        assert settings.recent_limit == DEFAULT_RECENT_LIMIT

    def test_load_corrupted_returns_defaults(self, tmp_path: Path) -> None:
        path = tmp_path / "settings.json"
        path.write_text("not-json-at-all")
        settings = AppSettings.load(path)
        assert settings.recent_vaults == []

    def test_load_non_dict_returns_defaults(self, tmp_path: Path) -> None:
        path = tmp_path / "settings.json"
        path.write_text(json.dumps(["wrong", "shape"]))
        settings = AppSettings.load(path)
        assert settings.recent_vaults == []

    def test_save_and_load_roundtrip(self, tmp_path: Path) -> None:
        path = tmp_path / "settings.json"
        settings = AppSettings(
            recent_vaults=["a.opnvault", "b.opnvault"],
            default_vault="a.opnvault",
        )
        settings.save(path)
        loaded = AppSettings.load(path)
        assert loaded.recent_vaults == ["a.opnvault", "b.opnvault"]
        assert loaded.default_vault == "a.opnvault"

    def test_save_creates_parent_directories(self, tmp_path: Path) -> None:
        path = tmp_path / "sub" / "dir" / "settings.json"
        AppSettings().save(path)
        assert path.exists()


# ---------------------------------------------------------------------------
# Recent-Vaults-Liste
# ---------------------------------------------------------------------------


class TestRememberVault:
    def test_adds_to_top(self) -> None:
        s = AppSettings()
        s.remember_vault("x.opnvault")
        s.remember_vault("y.opnvault")
        assert s.recent_vaults == ["y.opnvault", "x.opnvault"]

    def test_dedupes_existing_entry(self) -> None:
        s = AppSettings(recent_vaults=["a", "b", "c"])
        s.remember_vault("b")
        assert s.recent_vaults == ["b", "a", "c"]

    def test_respects_recent_limit(self) -> None:
        s = AppSettings(recent_limit=3)
        for name in ["a", "b", "c", "d", "e"]:
            s.remember_vault(name)
        assert s.recent_vaults == ["e", "d", "c"]

    def test_accepts_path_objects(self, tmp_path: Path) -> None:
        s = AppSettings()
        s.remember_vault(tmp_path / "x.opnvault")
        assert s.recent_vaults[0] == str(tmp_path / "x.opnvault")


class TestForgetVault:
    def test_removes_entry(self) -> None:
        s = AppSettings(recent_vaults=["a", "b", "c"])
        s.forget_vault("b")
        assert s.recent_vaults == ["a", "c"]

    def test_removes_default_if_matched(self) -> None:
        s = AppSettings(recent_vaults=["a", "b"], default_vault="b")
        s.forget_vault("b")
        assert s.default_vault is None

    def test_no_op_for_unknown(self) -> None:
        s = AppSettings(recent_vaults=["a"])
        s.forget_vault("b")
        assert s.recent_vaults == ["a"]


# ---------------------------------------------------------------------------
# Env-Overrides (Docker/systemd-friendly)
# ---------------------------------------------------------------------------


class TestEnvOverrides:
    def test_auth_backend_env_overrides_json(self, tmp_path: Path) -> None:
        path = tmp_path / "settings.json"
        AppSettings(auth_backend="vault").save(path)
        with patch.dict(os.environ, {"OPNCOCKPIT_AUTH_BACKEND": "user-db"}):
            loaded = AppSettings.load(path)
        assert loaded.auth_backend == "user-db"

    def test_deployment_mode_env_overrides_json(self, tmp_path: Path) -> None:
        path = tmp_path / "settings.json"
        AppSettings(deployment_mode="single-local").save(path)
        with patch.dict(os.environ, {"OPNCOCKPIT_DEPLOYMENT_MODE": "multi-server"}):
            loaded = AppSettings.load(path)
        assert loaded.deployment_mode == "multi-server"

    def test_storage_backend_env_overrides_json(self, tmp_path: Path) -> None:
        path = tmp_path / "settings.json"
        AppSettings(storage_backend="filesystem").save(path)
        with patch.dict(os.environ, {"OPNCOCKPIT_STORAGE_BACKEND": "sqlite"}):
            loaded = AppSettings.load(path)
        assert loaded.storage_backend == "sqlite"

    def test_unknown_env_value_ignored(self, tmp_path: Path) -> None:
        path = tmp_path / "settings.json"
        AppSettings(auth_backend="vault").save(path)
        with patch.dict(os.environ, {"OPNCOCKPIT_AUTH_BACKEND": "garbage"}):
            loaded = AppSettings.load(path)
        assert loaded.auth_backend == "vault"  # JSON-Wert bleibt

    def test_env_works_without_settings_json(self, tmp_path: Path) -> None:
        path = tmp_path / "missing.json"
        with patch.dict(os.environ, {"OPNCOCKPIT_AUTH_BACKEND": "user-db"}):
            loaded = AppSettings.load(path)
        assert loaded.auth_backend == "user-db"

    def test_empty_env_keeps_json(self, tmp_path: Path) -> None:
        path = tmp_path / "settings.json"
        AppSettings(auth_backend="user-db").save(path)
        with patch.dict(os.environ, {"OPNCOCKPIT_AUTH_BACKEND": ""}):
            loaded = AppSettings.load(path)
        assert loaded.auth_backend == "user-db"


class TestUpdateCheckSettings:
    def test_default_enabled_24h(self) -> None:
        s = AppSettings()
        assert s.update_check_enabled is True
        assert s.update_check_interval_hours == 24

    def test_env_disables(self, tmp_path: Path) -> None:
        path = tmp_path / "settings.json"
        with patch.dict(os.environ, {"OPNCOCKPIT_UPDATE_CHECK_ENABLED": "0"}):
            loaded = AppSettings.load(path)
        assert loaded.update_check_enabled is False

    def test_env_overrides_interval(self, tmp_path: Path) -> None:
        path = tmp_path / "settings.json"
        with patch.dict(
            os.environ, {"OPNCOCKPIT_UPDATE_CHECK_INTERVAL_HOURS": "6"},
        ):
            loaded = AppSettings.load(path)
        assert loaded.update_check_interval_hours == 6

    def test_invalid_interval_keeps_default(self, tmp_path: Path) -> None:
        path = tmp_path / "settings.json"
        with patch.dict(
            os.environ, {"OPNCOCKPIT_UPDATE_CHECK_INTERVAL_HOURS": "abc"},
        ):
            loaded = AppSettings.load(path)
        assert loaded.update_check_interval_hours == 24

    def test_json_roundtrip(self, tmp_path: Path) -> None:
        path = tmp_path / "settings.json"
        AppSettings(
            update_check_enabled=False,
            update_check_interval_hours=12,
        ).save(path)
        loaded = AppSettings.load(path)
        assert loaded.update_check_enabled is False
        assert loaded.update_check_interval_hours == 12
