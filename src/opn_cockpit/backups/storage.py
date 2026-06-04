"""Dateisystem-basierte Persistenz fuer Backups.

Verantwortungen:

* **Atomares Schreiben** (Tempfile + ``os.replace``) - kein halb-fertiges
  Backup oder halb-fertiger Index.
* **Per-Geraete-Unterordner** - leicht pro Site zu sichern/wegzuwerfen.
* **Pruning** mit getrennten Limits fuer ``pre-apply``- vs.
  ``scheduled``-Backups. ``manual`` zaehlt zum Pre-Apply-Pool (User-
  Erwartung: "wenn ich manuell ein Backup ziehe, soll's nicht morgen
  wieder rausgepruned werden weil das Scheduled-Limit voll ist").
* **Konsistenter Index** - ``index.json`` listet alle Records, gzip-
  Dateien sind die Backups. Beim Lesen wird der Index als Wahrheit
  genommen; orphaned Files (Backup ohne Index-Eintrag) werden beim
  naechsten Prune mitgeloescht.

Die Funktionen sind **stateless** und nehmen ``storage_root`` als
optionalen Parameter, damit Tests gegen ``tmp_path`` laufen koennen.
Default ist ``get_app_data_dir() / "backups"``.
"""

from __future__ import annotations

import contextlib
import gzip
import hashlib
import json
import os
import shutil
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path

from opn_cockpit.backups.errors import BackupNotFoundError, BackupStoreError
from opn_cockpit.backups.model import BACKUP_TRIGGERS, BackupRecord
from opn_cockpit.config import get_app_data_dir

BACKUPS_DIRNAME = "backups"
INDEX_FILENAME = "index.json"
BACKUP_FILE_SUFFIX = ".xml.gz"

# Default-Retention pro Geraet. Override via VaultSettings (kommt im
# zweiten Commit dieser Iteration).
DEFAULT_RETENTION_PRE_APPLY = 30
DEFAULT_RETENTION_SCHEDULED = 90


def _default_storage_root() -> Path:
    return get_app_data_dir() / BACKUPS_DIRNAME


def _device_dir(device_id: str, storage_root: Path | None) -> Path:
    root = storage_root or _default_storage_root()
    if not device_id:
        raise BackupStoreError("Backup-Operation ohne device_id verweigert.")
    return root / device_id


def _index_path(device_dir: Path) -> Path:
    return device_dir / INDEX_FILENAME


def _backup_file_path(device_dir: Path, backup_id: str) -> Path:
    return device_dir / f"{backup_id}{BACKUP_FILE_SUFFIX}"


def _utc_now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _read_index(device_dir: Path) -> list[BackupRecord]:
    """Liest ``index.json`` defensiv. Fehlende/kaputte Datei -> leere Liste."""
    path = _index_path(device_dir)
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BackupStoreError(
            f"Backup-Index kaputt: {path}: {exc}",
        ) from exc
    if not isinstance(raw, list):
        return []
    records: list[BackupRecord] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        try:
            records.append(BackupRecord.from_dict(entry))
        except (TypeError, ValueError):
            # Defekter Eintrag - ueberspringen statt zu crashen.
            continue
    return records


def _write_index(device_dir: Path, records: list[BackupRecord]) -> None:
    """Atomar: tmpfile -> os.replace.

    Die Liste wird vor dem Schreiben nach Timestamp absteigend sortiert,
    damit die Anzeige in der UI ohne Frontend-Sort klappt.
    """
    device_dir.mkdir(parents=True, exist_ok=True)
    path = _index_path(device_dir)
    payload = json.dumps(
        [r.to_dict() for r in _sort_records_desc(records)],
        ensure_ascii=False,
        indent=2,
    )
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        delete=False,
        dir=str(device_dir),
        prefix=".index-",
        suffix=".tmp",
    ) as tmp:
        tmp.write(payload)
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, path)


def _sort_records_desc(records: list[BackupRecord]) -> list[BackupRecord]:
    return sorted(records, key=lambda r: r.timestamp_utc, reverse=True)


def append_backup(
    device_id: str,
    content: bytes,
    *,
    trigger: str,
    related_plan_id: str = "",
    device_name_at_creation: str = "",
    storage_root: Path | None = None,
) -> BackupRecord:
    """Schreibt ``content`` als gzip-Backup und aktualisiert den Index.

    Wirft ``BackupStoreError`` bei IO-Problemen oder ungueltigem Trigger.
    Liefert den vollstaendigen ``BackupRecord`` zurueck.
    """
    if trigger not in BACKUP_TRIGGERS:
        raise BackupStoreError(
            f"Unbekannter Backup-Trigger '{trigger}'. Erlaubt: "
            f"{sorted(BACKUP_TRIGGERS)}",
        )
    if not content:
        raise BackupStoreError("Leerer Backup-Inhalt verweigert.")

    device_dir = _device_dir(device_id, storage_root)
    device_dir.mkdir(parents=True, exist_ok=True)

    backup_id = str(uuid.uuid4())
    sha = hashlib.sha256(content).hexdigest()
    size_uncompressed = len(content)

    target_file = _backup_file_path(device_dir, backup_id)
    try:
        with tempfile.NamedTemporaryFile(
            delete=False,
            dir=str(device_dir),
            prefix=f".{backup_id}-",
            suffix=".tmp",
        ) as tmp:
            tmp_path = Path(tmp.name)
        # gzip.open separat - wir wollen den Tempfile-Pfad konstant haben
        with gzip.open(tmp_path, "wb", compresslevel=6) as gz:
            gz.write(content)
        os.replace(tmp_path, target_file)
    except OSError as exc:
        # Falls Tempfile noch lebt, wegraeumen - tmp_path kann hier u. U.
        # noch nicht gebunden sein (z. B. NamedTemporaryFile-Fehler), darum
        # auch UnboundLocalError abfangen.
        with contextlib.suppress(OSError, UnboundLocalError):
            tmp_path.unlink(missing_ok=True)
        raise BackupStoreError(
            f"Backup-Datei konnte nicht geschrieben werden: {exc}",
        ) from exc

    size_compressed = target_file.stat().st_size
    record = BackupRecord(
        id=backup_id,
        device_id=device_id,
        timestamp_utc=_utc_now_iso(),
        trigger=trigger,
        size_bytes=size_uncompressed,
        size_compressed=size_compressed,
        sha256=sha,
        related_plan_id=related_plan_id,
        device_name_at_creation=device_name_at_creation,
    )

    existing = _read_index(device_dir)
    existing.append(record)
    try:
        _write_index(device_dir, existing)
    except OSError as exc:
        # Index-Schreiben fehlgeschlagen - Backup-Datei aufraeumen, damit
        # Index und Disk konsistent bleiben.
        target_file.unlink(missing_ok=True)
        raise BackupStoreError(
            f"Backup-Index konnte nicht aktualisiert werden: {exc}",
        ) from exc

    return record


def list_backups(
    device_id: str,
    *,
    storage_root: Path | None = None,
) -> list[BackupRecord]:
    """Liefert alle Backups eines Geraets, neueste zuerst.

    Geraet ohne Backups -> leere Liste, **kein** Fehler.
    """
    device_dir = _device_dir(device_id, storage_root)
    if not device_dir.exists():
        return []
    return _sort_records_desc(_read_index(device_dir))


def read_backup_content(
    device_id: str,
    backup_id: str,
    *,
    storage_root: Path | None = None,
) -> bytes:
    """Liest den unkomprimierten XML-Inhalt eines Backups.

    Wirft ``BackupNotFoundError`` wenn die Datei fehlt (z. B. inkonsistenter
    Index nach manueller Loeschung).
    """
    device_dir = _device_dir(device_id, storage_root)
    file_path = _backup_file_path(device_dir, backup_id)
    if not file_path.exists():
        raise BackupNotFoundError(
            f"Backup '{backup_id}' fuer Geraet '{device_id}' nicht gefunden.",
        )
    try:
        with gzip.open(file_path, "rb") as gz:
            return gz.read()
    except OSError as exc:
        raise BackupStoreError(
            f"Backup-Datei konnte nicht gelesen werden: {exc}",
        ) from exc


def prune_backups(
    device_id: str,
    *,
    retention_pre_apply: int = DEFAULT_RETENTION_PRE_APPLY,
    retention_scheduled: int = DEFAULT_RETENTION_SCHEDULED,
    storage_root: Path | None = None,
) -> list[BackupRecord]:
    """Loescht alte Backups gemaess Retention-Limits.

    ``manual``, ``pre-apply`` und ``post-apply`` teilen sich den
    Pre-Apply-Pool (User erwartet, dass manuelle Backups nicht
    ploetzlich weg sind und Pre/Post-Apply-Paare zusammen verfallen).
    ``scheduled`` hat einen separaten Pool.

    Limit <= 0 deaktiviert das Pruning fuer den Pool (defensiv -
    "0" wird oft als "unlimited" interpretiert; wir halten uns daran).

    Bereinigt zusaetzlich orphaned ``.xml.gz``-Dateien (Files auf Disk
    ohne Index-Eintrag).

    Liefert die geloeschten Records zurueck (fuer Audit-Logging).
    """
    device_dir = _device_dir(device_id, storage_root)
    if not device_dir.exists():
        return []
    records = _read_index(device_dir)
    pre_apply_pool = [
        r for r in records if r.trigger in ("manual", "pre-apply", "post-apply")
    ]
    scheduled_pool = [r for r in records if r.trigger == "scheduled"]

    keep_pre = _keep_newest(pre_apply_pool, retention_pre_apply)
    keep_sched = _keep_newest(scheduled_pool, retention_scheduled)
    keep_ids = {r.id for r in keep_pre} | {r.id for r in keep_sched}

    to_delete = [r for r in records if r.id not in keep_ids]
    for record in to_delete:
        _backup_file_path(device_dir, record.id).unlink(missing_ok=True)

    # Orphaned Files (im Dir, nicht im Index) - mitloeschen.
    indexed_ids = {r.id for r in records}
    for path in device_dir.glob(f"*{BACKUP_FILE_SUFFIX}"):
        stem = path.name.removesuffix(BACKUP_FILE_SUFFIX)
        if stem not in indexed_ids:
            path.unlink(missing_ok=True)

    remaining = [r for r in records if r.id in keep_ids]
    if to_delete:
        try:
            _write_index(device_dir, remaining)
        except OSError as exc:
            raise BackupStoreError(
                f"Backup-Index konnte nicht aktualisiert werden: {exc}",
            ) from exc
    return to_delete


def _keep_newest(records: list[BackupRecord], limit: int) -> list[BackupRecord]:
    if limit <= 0:
        return list(records)  # 0/negativ = unlimited
    return _sort_records_desc(records)[:limit]


def delete_all_for_device(
    device_id: str,
    *,
    storage_root: Path | None = None,
) -> int:
    """Loescht den kompletten Backup-Unterordner eines Geraets.

    Zu rufen wenn ein Geraet aus dem Tresor entfernt wird - die Backups
    sollen das Geraet nicht ueberleben (sonst Datenhalde mit Cert-/Key-
    Material in den XMLs). Liefert die Anzahl der geloeschten Backups.
    """
    device_dir = _device_dir(device_id, storage_root)
    if not device_dir.exists():
        return 0
    records = _read_index(device_dir)
    count = len(records)
    try:
        shutil.rmtree(device_dir)
    except OSError as exc:
        raise BackupStoreError(
            f"Backup-Verzeichnis konnte nicht entfernt werden: {exc}",
        ) from exc
    return count


__all__ = [
    "BACKUPS_DIRNAME",
    "BACKUP_FILE_SUFFIX",
    "DEFAULT_RETENTION_PRE_APPLY",
    "DEFAULT_RETENTION_SCHEDULED",
    "INDEX_FILENAME",
    "append_backup",
    "delete_all_for_device",
    "list_backups",
    "prune_backups",
    "read_backup_content",
]
