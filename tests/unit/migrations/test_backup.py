"""Tests fuer Pre-Update-Backup-Helper."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from opn_cockpit.config import AppSettings
from opn_cockpit.migrations.backup import (
    BackupError,
    backup_root,
    create_pre_migration_backup,
    list_backups,
    prune_backups,
)
from opn_cockpit.web.server_state import VAULT_PATH_ENV


def _seed_app_data(data_dir: Path) -> None:
    """Erzeugt typische App-Daten-Dateien zum Sichern."""
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "settings.json").write_text(json.dumps({"foo": 1}), encoding="utf-8")
    (data_dir / "users.db").write_bytes(b"sqlite-bytes")
    (data_dir / "opn-cockpit.db").write_bytes(b"more-sqlite")
    (data_dir / "audit.jsonl").write_text("{}\n", encoding="utf-8")
    plans_dir = data_dir / "plans"
    plans_dir.mkdir()
    (plans_dir / "pl-001.json").write_text("{}", encoding="utf-8")


class TestCreateBackup:
    def test_creates_snapshot_dir_under_backups_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        data_dir = tmp_path / "appdata"
        _seed_app_data(data_dir)
        monkeypatch.delenv(VAULT_PATH_ENV, raising=False)

        result = create_pre_migration_backup(
            "0.6.0",
            data_dir=data_dir,
            settings=AppSettings(),
            retention=None,
        )
        assert result.path.exists()
        assert result.path.parent == backup_root(data_dir)
        assert "-pre-0.6.0" in result.path.name

    def test_data_files_are_copied(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        data_dir = tmp_path / "appdata"
        _seed_app_data(data_dir)
        monkeypatch.delenv(VAULT_PATH_ENV, raising=False)

        result = create_pre_migration_backup(
            "0.6.0",
            data_dir=data_dir,
            settings=AppSettings(),
            retention=None,
        )
        backed_up = result.path / "data"
        assert (backed_up / "settings.json").read_text(encoding="utf-8") == '{"foo": 1}'
        assert (backed_up / "users.db").read_bytes() == b"sqlite-bytes"
        assert (backed_up / "plans" / "pl-001.json").exists()

    def test_skips_backups_subdir_to_prevent_recursion(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        data_dir = tmp_path / "appdata"
        _seed_app_data(data_dir)
        (data_dir / "backups").mkdir()
        (data_dir / "backups" / "previous").mkdir()
        (data_dir / "backups" / "previous" / "junk").write_text("x", encoding="utf-8")
        monkeypatch.delenv(VAULT_PATH_ENV, raising=False)

        result = create_pre_migration_backup(
            "0.6.0",
            data_dir=data_dir,
            settings=AppSettings(),
            retention=None,
        )
        # backup-Verzeichnis selbst darf nicht in den neuen Snapshot kopiert sein.
        assert not (result.path / "data" / "backups").exists()

    def test_backs_up_known_vaults(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        data_dir = tmp_path / "appdata"
        _seed_app_data(data_dir)
        vault_path = tmp_path / "secret" / "main.opnvault"
        vault_path.parent.mkdir()
        vault_path.write_bytes(b"vault-bytes")
        monkeypatch.delenv(VAULT_PATH_ENV, raising=False)
        settings = AppSettings(default_vault=str(vault_path))

        result = create_pre_migration_backup(
            "0.6.0",
            data_dir=data_dir,
            settings=settings,
            retention=None,
        )
        assert "main.opnvault" in result.vault_files
        assert (result.path / "vaults" / "main.opnvault").read_bytes() == b"vault-bytes"

    def test_collects_vault_path_from_env_too(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        data_dir = tmp_path / "appdata"
        _seed_app_data(data_dir)
        env_vault = tmp_path / "env.opnvault"
        env_vault.write_bytes(b"env-vault")
        monkeypatch.setenv(VAULT_PATH_ENV, str(env_vault))

        result = create_pre_migration_backup(
            "0.6.0",
            data_dir=data_dir,
            settings=AppSettings(),
            retention=None,
        )
        assert "env.opnvault" in result.vault_files

    def test_deduplicates_same_vault_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        data_dir = tmp_path / "appdata"
        _seed_app_data(data_dir)
        vault = tmp_path / "same.opnvault"
        vault.write_bytes(b"v")
        monkeypatch.setenv(VAULT_PATH_ENV, str(vault))
        settings = AppSettings(
            default_vault=str(vault),
            recent_vaults=[str(vault)],
        )

        result = create_pre_migration_backup(
            "0.6.0", data_dir=data_dir, settings=settings, retention=None,
        )
        assert result.vault_files == ("same.opnvault",)

    def test_disambiguates_same_name_from_different_dirs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        data_dir = tmp_path / "appdata"
        _seed_app_data(data_dir)
        a_dir = tmp_path / "a"
        a_dir.mkdir()
        b_dir = tmp_path / "b"
        b_dir.mkdir()
        (a_dir / "shared.opnvault").write_bytes(b"A")
        (b_dir / "shared.opnvault").write_bytes(b"B")
        monkeypatch.delenv(VAULT_PATH_ENV, raising=False)
        settings = AppSettings(
            default_vault=str(a_dir / "shared.opnvault"),
            recent_vaults=[str(b_dir / "shared.opnvault")],
        )

        result = create_pre_migration_backup(
            "0.6.0", data_dir=data_dir, settings=settings, retention=None,
        )
        assert sorted(result.vault_files) == sorted(
            ["shared.opnvault", "shared-1.opnvault"],
        )

    def test_manifest_is_written(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        data_dir = tmp_path / "appdata"
        _seed_app_data(data_dir)
        monkeypatch.delenv(VAULT_PATH_ENV, raising=False)

        result = create_pre_migration_backup(
            "0.6.0", data_dir=data_dir, settings=AppSettings(), retention=None,
        )
        manifest = json.loads((result.path / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["app_version"] == "0.6.0"
        assert "settings.json" in manifest["data_files"]

    def test_raises_backup_error_on_double_snapshot_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Zwei Backups im selben Sekundenfenster -> BackupError."""
        data_dir = tmp_path / "appdata"
        _seed_app_data(data_dir)
        monkeypatch.delenv(VAULT_PATH_ENV, raising=False)
        # Konstanter Timestamp simuliert den Sub-Sekunden-Konflikt.
        monkeypatch.setattr(
            "opn_cockpit.migrations.backup._timestamp",
            lambda: "20260101T000000Z",
        )
        create_pre_migration_backup(
            "0.6.0", data_dir=data_dir, settings=AppSettings(), retention=None,
        )
        with pytest.raises(BackupError):
            create_pre_migration_backup(
                "0.6.0", data_dir=data_dir, settings=AppSettings(), retention=None,
            )


class TestListAndPrune:
    def _make_n_backups(self, data_dir: Path, n: int) -> list[Path]:
        root = backup_root(data_dir)
        root.mkdir(parents=True, exist_ok=True)
        created: list[Path] = []
        for i in range(n):
            p = root / f"2026010{i}T000000Z-pre-0.6.0"
            p.mkdir()
            (p / "manifest.json").write_text("{}", encoding="utf-8")
            created.append(p)
        return created

    def test_list_backups_sorted_by_name(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "appdata"
        data_dir.mkdir()
        self._make_n_backups(data_dir, 3)
        result = list_backups(data_dir)
        assert [p.name for p in result] == sorted(p.name for p in result)

    def test_prune_keeps_newest_n(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "appdata"
        data_dir.mkdir()
        self._make_n_backups(data_dir, 7)
        removed = prune_backups(3, data_dir=data_dir)
        assert len(removed) == 4
        assert len(list_backups(data_dir)) == 3

    def test_prune_zero_removes_all(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "appdata"
        data_dir.mkdir()
        self._make_n_backups(data_dir, 3)
        removed = prune_backups(0, data_dir=data_dir)
        assert len(removed) == 3
        assert list_backups(data_dir) == []

    def test_prune_no_op_when_under_limit(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "appdata"
        data_dir.mkdir()
        self._make_n_backups(data_dir, 2)
        removed = prune_backups(5, data_dir=data_dir)
        assert removed == []

    def test_prune_rejects_negative(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            prune_backups(-1, data_dir=tmp_path)
