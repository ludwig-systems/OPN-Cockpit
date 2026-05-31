"""Migrations-Runner.

Ruft :func:`run_pending_migrations` beim App-Start (Web oder CLI) auf.
Der Aufrufer ist dafuer verantwortlich, das ``RunResult`` zu loggen oder
auf stderr zu drucken — der Runner selbst gibt nichts aus, damit Tests
sauber bleiben.

Reihenfolge:

1. Status laden (`migrations.json`).
2. Pending-Liste bestimmen (alle Eintraege aus :data:`MIGRATIONS`, deren
   ID noch nicht in ``state.applied_ids`` steht).
3. Bei nicht-leerer Pending-Liste: Backup erzeugen.
4. Migrationen der Reihe nach ausfuehren; jede markiert + persistiert
   ihren Erfolg sofort, damit ein Teil-Lauf nicht alles verliert.
5. ``last_app_version`` aktualisieren und speichern.

Wirft eine Migration ``MigrationError``, bricht der Runner ab und reicht
die Exception durch — der Aufrufer entscheidet, ob er den Boot abbricht.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from opn_cockpit import __version__
from opn_cockpit.config import AppSettings, get_app_data_dir
from opn_cockpit.migrations.backup import (
    BackupResult,
    create_pre_migration_backup,
)
from opn_cockpit.migrations.errors import MigrationError
from opn_cockpit.migrations.registry import MIGRATIONS, Migration, MigrationContext
from opn_cockpit.migrations.state import MigrationState


@dataclass(frozen=True, slots=True)
class RunResult:
    """Was der Runner zurueckgibt.

    * ``pending_ids`` — alle vor dem Lauf offenen Migrationen.
    * ``applied_ids`` — die tatsaechlich erfolgreich angewandten in dieser
      Runde. Bei vollstaendigem Erfolg gleich ``pending_ids``; bei Abbruch
      enthaelt sie nur die bis dahin durchgelaufenen.
    * ``backup`` — Beschreibung des Pre-Update-Backups oder ``None`` wenn
      nichts zu tun war / Backup explizit unterdrueckt wurde.
    * ``skipped`` — ``True`` wenn keine Migration offen war (No-Op-Boot).
    * ``previous_app_version`` — was vor dem Lauf in ``migrations.json``
      stand. ``None`` bei Erstinstallation.
    """

    pending_ids: tuple[str, ...]
    applied_ids: tuple[str, ...]
    backup: BackupResult | None
    skipped: bool
    previous_app_version: str | None


def pending_migrations(state: MigrationState) -> list[Migration]:
    """Liefert die offenen Migrationen in Registrierungs-Reihenfolge."""
    applied = state.applied_ids
    return [m for m in MIGRATIONS if m.id not in applied]


def run_pending_migrations(
    *,
    data_dir: Path | None = None,
    settings: AppSettings | None = None,
    state_path: Path | None = None,
    skip_backup: bool = False,
    retention: int | None = None,
) -> RunResult:
    """Arbeitet alle offenen Migrationen ab.

    Args:
        data_dir: AppData-Override (Tests, Container).
        settings: Wenn ``None``, wird via :meth:`AppSettings.load` geladen.
        state_path: Override fuer ``migrations.json`` (Tests).
        skip_backup: Wenn ``True``, wird kein Snapshot angelegt. **Nur fuer
            Tests** — produktive Boots sollten immer ein Backup ziehen.
        retention: Pass-through an :func:`create_pre_migration_backup`.
            ``None`` heisst Default-Retention.

    Returns:
        :class:`RunResult` mit Statistiken zum Lauf.

    Raises:
        MigrationError: Wird durchgereicht, wenn eine Migration fehlschlaegt.
            Bereits angewandte Migrationen bleiben in ``migrations.json``
            eingetragen, das Backup-Verzeichnis bleibt unangetastet.
    """
    resolved_settings = settings or AppSettings.load()
    resolved_data = data_dir or get_app_data_dir()
    state = MigrationState.load(state_path)
    previous_version = state.last_app_version

    pending = pending_migrations(state)
    if not pending:
        if state.last_app_version != __version__:
            state.last_app_version = __version__
            state.save(state_path)
        return RunResult(
            pending_ids=(),
            applied_ids=(),
            backup=None,
            skipped=True,
            previous_app_version=previous_version,
        )

    backup: BackupResult | None = None
    if not skip_backup:
        retention_arg = retention if retention is not None else 5
        backup = create_pre_migration_backup(
            __version__,
            data_dir=resolved_data,
            settings=resolved_settings,
            retention=retention_arg,
        )

    ctx = MigrationContext(
        app_data_dir=resolved_data,
        settings=resolved_settings,
    )
    applied_now: list[str] = []
    try:
        for migration in pending:
            try:
                migration.up(ctx)
            except MigrationError:
                raise
            except Exception as exc:
                raise MigrationError(
                    f"Migration {migration.id!r} fehlgeschlagen: {exc}",
                ) from exc
            state.mark_applied(migration.id, app_version=__version__)
            state.save(state_path)
            applied_now.append(migration.id)
    finally:
        # last_app_version wird nur dann gesetzt, wenn alle pending Migrationen
        # erfolgreich durchgelaufen sind. Andernfalls bleibt der alte Wert
        # stehen — der naechste Boot sieht den Rest als pending und versucht
        # ihn erneut.
        if len(applied_now) == len(pending):
            state.last_app_version = __version__
            state.save(state_path)

    return RunResult(
        pending_ids=tuple(m.id for m in pending),
        applied_ids=tuple(applied_now),
        backup=backup,
        skipped=False,
        previous_app_version=previous_version,
    )


__all__ = [
    "RunResult",
    "pending_migrations",
    "run_pending_migrations",
]
