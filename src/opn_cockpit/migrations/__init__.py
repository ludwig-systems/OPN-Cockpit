"""Migrations- und Pre-Update-Backup-Fundament (v6).

Trennung Code <-> Daten: ein Update darf nur den Code-Pfad anfassen.
Die Daten unter ``%APPDATA%\\OPN-Cockpit\\`` (bzw. ``$OPNCOCKPIT_DATA_DIR``)
bleiben stehen. Wenn ein neues Release jedoch Schema-Aenderungen mitbringt,
laeuft hier eine versionierte Migration:

1. ``MigrationState`` traegt die bereits angewandten Migrations-IDs in
   ``migrations.json`` ein (neben den restlichen App-Daten).
2. ``run_pending_migrations()`` sieht in ``registry.MIGRATIONS`` nach,
   bestimmt die offenen Eintraege, legt einen Pre-Update-Backup-Snapshot
   an (``backups/<timestamp>-pre-<version>/``) und arbeitet die Liste der
   Reihe nach ab.
3. Tritt waehrend einer Migration ein Fehler auf, bleibt der Backup-Stand
   intakt — der Aufrufer (Web-Boot, CLI) entscheidet, ob er weiterstartet.

Heute existieren noch keine Migrationen. Der Boot-Pfad ruft das Framework
trotzdem auf, damit kuenftige Releases keine Sonderfaelle brauchen.
"""

from opn_cockpit.migrations.backup import (
    BackupError,
    BackupResult,
    backup_root,
    create_pre_migration_backup,
    list_backups,
    prune_backups,
)
from opn_cockpit.migrations.errors import MigrationError
from opn_cockpit.migrations.registry import (
    MIGRATIONS,
    Migration,
    MigrationContext,
)
from opn_cockpit.migrations.runner import (
    RunResult,
    pending_migrations,
    run_pending_migrations,
)
from opn_cockpit.migrations.state import (
    STATE_FILENAME,
    AppliedMigration,
    MigrationState,
    default_state_path,
)

__all__ = [
    "MIGRATIONS",
    "STATE_FILENAME",
    "AppliedMigration",
    "BackupError",
    "BackupResult",
    "Migration",
    "MigrationContext",
    "MigrationError",
    "MigrationState",
    "RunResult",
    "backup_root",
    "create_pre_migration_backup",
    "default_state_path",
    "list_backups",
    "pending_migrations",
    "prune_backups",
    "run_pending_migrations",
]
