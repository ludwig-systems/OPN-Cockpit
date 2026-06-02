"""Lokale Persistenz fuer OPNsense-Konfigurations-Backups.

Sicherheitsnetz-Layer fuer das v0.7-Theme: zieht vor jedem Apply ein
Backup von der OPNsense, speichert es lokal (gzip-XML) und ermoeglicht
Download/Restore. Live spaeter auch fuer geplante (taegliche) Backups
und Config-Drift-Erkennung weitergenutzt.

Storage-Layout::

    <app_data>/backups/
    └── <device_id>/
        ├── index.json        # Liste der Backups, neueste zuerst
        ├── <uuid>.xml.gz     # Gzip-komprimierter Backup-Inhalt
        └── ...

Bewusst Dateisystem statt SQLite-Blob: Out-of-band sicherbar (rsync,
externe Aufbewahrung), pro Geraet trennbar, kein DB-Schema-Druck.
"""

from __future__ import annotations

from opn_cockpit.backups.errors import (
    BackupNotFoundError,
    BackupStoreError,
)
from opn_cockpit.backups.model import (
    BACKUP_TRIGGERS,
    BackupRecord,
)
from opn_cockpit.backups.storage import (
    DEFAULT_RETENTION_PRE_APPLY,
    DEFAULT_RETENTION_SCHEDULED,
    append_backup,
    delete_all_for_device,
    list_backups,
    prune_backups,
    read_backup_content,
)

__all__ = [
    "BACKUP_TRIGGERS",
    "DEFAULT_RETENTION_PRE_APPLY",
    "DEFAULT_RETENTION_SCHEDULED",
    "BackupNotFoundError",
    "BackupRecord",
    "BackupStoreError",
    "append_backup",
    "delete_all_for_device",
    "list_backups",
    "prune_backups",
    "read_backup_content",
]
