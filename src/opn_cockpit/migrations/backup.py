"""Pre-Update-Backup-Snapshots.

Vor jedem Migrationslauf wird ein vollstaendiger Snapshot der relevanten
Daten in ``<app_data_dir>/backups/<timestamp>-pre-<version>/`` abgelegt:

* ``data/`` — Spiegel des AppData-Verzeichnisses ohne den ``backups/``-
  Unterordner. Dort liegen ``users.db``, ``opn-cockpit.db``,
  ``settings.json``, ``audit.jsonl``, ``plans/``, ``profiles.json``,
  ``migrations.json`` — alles, was der Server aktiv schreibt.
* ``vaults/`` — Kopien aller bekannten ``.opnvault``-Dateien
  (``OPNCOCKPIT_VAULT_PATH``, ``settings.default_vault``,
  ``settings.recent_vaults``). Vaults liegen oft ausserhalb des
  AppData-Dirs, deshalb separat.
* ``manifest.json`` — Liste der mitgesicherten Pfade plus die
  ``app_version``, gegen die das Backup erzeugt wurde.

Retention: nach dem Schreiben werden alle Backups bis auf die neuesten
``DEFAULT_RETENTION`` Verzeichnisse geloescht. Wer das nicht will, ruft
``create_pre_migration_backup(..., retention=None)``.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from opn_cockpit.config import AppSettings, get_app_data_dir
from opn_cockpit.web.server_state import VAULT_PATH_ENV

BACKUPS_DIRNAME = "backups"
DATA_SUBDIR = "data"
VAULTS_SUBDIR = "vaults"
MANIFEST_FILENAME = "manifest.json"
BACKUP_DIR_PREFIX = ""  # historisch leer — der Name ist bereits sprechend
DEFAULT_RETENTION = 5


class BackupError(Exception):
    """Backup konnte nicht geschrieben werden — Migration sollte abbrechen."""


@dataclass(frozen=True, slots=True)
class BackupResult:
    """Ergebnis eines Backup-Vorgangs."""

    path: Path
    data_files: tuple[str, ...]
    vault_files: tuple[str, ...]
    pruned: tuple[str, ...]


def backup_root(data_dir: Path | None = None) -> Path:
    """Verzeichnis, in dem alle Backups liegen."""
    return (data_dir or get_app_data_dir()) / BACKUPS_DIRNAME


def _timestamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _safe_dirname(target_version: str) -> str:
    """Macht aus ``0.6.0`` ein dateisystemfreundliches Suffix."""
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in target_version)


def _collect_vault_paths(settings: AppSettings) -> list[Path]:
    """Sammelt alle bekannten Vault-Pfade — Duplikate werden eliminiert."""
    seen: set[str] = set()
    result: list[Path] = []

    env_path = os.environ.get(VAULT_PATH_ENV, "").strip()
    candidates: list[str] = []
    if env_path:
        candidates.append(env_path)
    if settings.default_vault:
        candidates.append(settings.default_vault)
    candidates.extend(settings.recent_vaults)

    for raw in candidates:
        if not raw:
            continue
        try:
            resolved = Path(raw).expanduser().resolve(strict=False)
        except (OSError, RuntimeError):
            continue
        key = str(resolved)
        if key in seen:
            continue
        seen.add(key)
        if resolved.exists() and resolved.is_file():
            result.append(resolved)
    return result


def _copy_app_data(src: Path, dst: Path) -> list[str]:
    """Kopiert AppData-Inhalt ohne ``backups/`` rekursiv nach ``dst``.

    Liefert die relativen Pfade der gesicherten Eintraege (fuer das Manifest).
    """
    if not src.exists():
        return []
    dst.mkdir(parents=True, exist_ok=True)
    relative: list[str] = []
    for entry in src.iterdir():
        if entry.name == BACKUPS_DIRNAME:
            continue
        target = dst / entry.name
        try:
            if entry.is_dir():
                shutil.copytree(entry, target, symlinks=False, dirs_exist_ok=True)
            else:
                shutil.copy2(entry, target)
        except OSError as exc:
            raise BackupError(f"Konnte {entry} nicht sichern: {exc}") from exc
        relative.append(entry.name)
    return sorted(relative)


def _copy_vaults(paths: list[Path], dst: Path) -> list[str]:
    """Kopiert die Vault-Dateien.

    Bei Namens-Kollisionen (zwei Vaults mit gleichem Dateinamen aus
    verschiedenen Verzeichnissen) bekommt der zweite ein ``-N`` Suffix,
    damit nichts ueberschrieben wird.
    """
    if not paths:
        return []
    dst.mkdir(parents=True, exist_ok=True)
    used: dict[str, int] = {}
    written: list[str] = []
    for src in paths:
        base = src.name
        if base in used:
            used[base] += 1
            stem = src.stem
            suffix = src.suffix
            name = f"{stem}-{used[base]}{suffix}"
        else:
            used[base] = 0
            name = base
        target = dst / name
        try:
            shutil.copy2(src, target)
        except OSError as exc:
            raise BackupError(f"Konnte Vault {src} nicht sichern: {exc}") from exc
        written.append(name)
    return written


def create_pre_migration_backup(
    target_version: str,
    *,
    data_dir: Path | None = None,
    settings: AppSettings | None = None,
    retention: int | None = DEFAULT_RETENTION,
) -> BackupResult:
    """Erzeugt einen vollstaendigen Snapshot vor dem Migrationslauf.

    Args:
        target_version: Versions-Label fuer das Backup-Verzeichnis und das
            Manifest. Idiomatisch ``opn_cockpit.__version__``.
        data_dir: Override fuer das App-Daten-Verzeichnis (Tests).
        settings: Settings (wenn None, werden sie geladen).
        retention: Anzahl der zu behaltenden Backups. ``None`` deaktiviert
            das Pruning.

    Returns:
        Beschreibung des erzeugten Snapshots, inkl. gepruneter Verzeichnisse.

    Raises:
        BackupError: Wenn ein Copy-Schritt fehlschlaegt. Das Backup-Verzeichnis
            bleibt liegen — fuer Diagnose nicht aufgeraeumt.
    """
    resolved_data = data_dir or get_app_data_dir()
    resolved_settings = settings or AppSettings.load()
    root = backup_root(resolved_data)
    root.mkdir(parents=True, exist_ok=True)

    snapshot_name = f"{_timestamp()}-pre-{_safe_dirname(target_version)}"
    snapshot_dir = root / snapshot_name
    if snapshot_dir.exists():
        # Sehr unwahrscheinlich (Sekundengenau), aber wir wollen kein Merging.
        raise BackupError(f"Backup-Verzeichnis existiert bereits: {snapshot_dir}")
    snapshot_dir.mkdir(parents=True, exist_ok=False)

    data_dst = snapshot_dir / DATA_SUBDIR
    vaults_dst = snapshot_dir / VAULTS_SUBDIR

    data_files = _copy_app_data(resolved_data, data_dst)
    vault_paths = _collect_vault_paths(resolved_settings)
    vault_files = _copy_vaults(vault_paths, vaults_dst)

    manifest = {
        "app_version": target_version,
        "created_at_iso": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "data_files": data_files,
        "vault_files": vault_files,
        "vault_origins": [str(p) for p in vault_paths],
    }
    (snapshot_dir / MANIFEST_FILENAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    pruned: tuple[str, ...] = ()
    if retention is not None:
        pruned_paths = prune_backups(retention, data_dir=resolved_data)
        pruned = tuple(p.name for p in pruned_paths)

    return BackupResult(
        path=snapshot_dir,
        data_files=tuple(data_files),
        vault_files=tuple(vault_files),
        pruned=pruned,
    )


def list_backups(data_dir: Path | None = None) -> list[Path]:
    """Liefert vorhandene Backup-Verzeichnisse, sortiert nach Name (= Zeit).

    Da die Verzeichnisnamen mit einem UTC-Timestamp im ``YYYYMMDDTHHMMSSZ``-
    Format beginnen, sortiert lexikographisch == chronologisch.
    """
    root = backup_root(data_dir)
    if not root.exists():
        return []
    return sorted([p for p in root.iterdir() if p.is_dir()], key=lambda p: p.name)


def prune_backups(retention: int, data_dir: Path | None = None) -> list[Path]:
    """Loescht alte Backup-Verzeichnisse, behaelt nur die neuesten ``retention``.

    Returns:
        Liste der entfernten Pfade (zur Anzeige im Logging).
    """
    if retention < 0:
        raise ValueError("retention darf nicht negativ sein.")
    backups = list_backups(data_dir)
    if len(backups) <= retention:
        return []
    to_remove = backups[:-retention] if retention > 0 else backups
    removed: list[Path] = []
    for entry in to_remove:
        try:
            shutil.rmtree(entry)
            removed.append(entry)
        except OSError:
            # Best-effort — wenn der Loesch-Schritt scheitert (Datei in Use
            # auf Windows o.ae.), lassen wir den Eintrag liegen.
            continue
    return removed


__all__ = [
    "BACKUPS_DIRNAME",
    "DATA_SUBDIR",
    "DEFAULT_RETENTION",
    "MANIFEST_FILENAME",
    "VAULTS_SUBDIR",
    "BackupError",
    "BackupResult",
    "backup_root",
    "create_pre_migration_backup",
    "list_backups",
    "prune_backups",
]
