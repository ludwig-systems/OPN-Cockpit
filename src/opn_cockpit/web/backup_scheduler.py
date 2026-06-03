"""Scheduled Auto-Backup im Hintergrund-Thread (v0.7 Safety-Net #4).

Zieht pro Tresor + Geraet im konfigurierten Intervall (Default 24h) ein
OPNsense-Konfig-Backup, persistiert es als gzip + Index-Eintrag mit
``trigger="scheduled"`` und schreibt jeweils einen Audit-Eintrag.

Design:

* Daemon-Thread, gestartet ueber ``create_app`` und durchlaufend bis
  zum Server-Stop. Tick-Intervall 5 Min — genauer braucht's nicht, da
  das User-konfigurierte Backup-Intervall mindestens 1h ist.
* Pro Tick: aktive Sessions aus dem ``SessionManager`` snapshoten, pro
  Tresor die ``scheduled_backup_enabled``-Settings pruefen, falls aktiv
  pro Geraet den letzten ``scheduled``-Eintrag im Backup-Index lesen
  und gegen ``scheduled_backup_interval_hours`` vergleichen.
* Wenn fällig: ``download_backup`` + ``append_backup`` + ``prune_backups``
  + Audit-Eintrag SCHEDULED_BACKUP. Pro Geraet parallel via
  ThreadPool (max_workers aus VaultSettings).
* Fehler werden im Audit geloggt, der Job laeuft mit dem naechsten Tick
  weiter — kein direktes Re-Spamming wie bei pre-apply.

Best-Effort: ein einzelner Geraete-Fehler reisst den Scheduler nie
runter; der Thread laeuft weiter. Sessions die in der Zwischenzeit
expiren werden im naechsten Tick weggewischt.

Storage-Root injizierbar fuer Tests (``backup_storage_root=tmp_path``).
"""

from __future__ import annotations

import contextlib
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from threading import RLock, Thread
from typing import TYPE_CHECKING

from opn_cockpit.audit.backend import AuditBackend, get_audit_backend
from opn_cockpit.audit.log import AuditEventKind
from opn_cockpit.backups import append_backup, list_backups, prune_backups
from opn_cockpit.backups.errors import BackupStoreError
from opn_cockpit.core.device_info import download_backup
from opn_cockpit.core.errors import OpnCockpitError
from opn_cockpit.core.http_client import HttpClient, HttpTarget, HttpTuning
from opn_cockpit.vault.model import VaultDevice

if TYPE_CHECKING:
    from opn_cockpit.web.auth.manager import SessionManager

LOOP_TICK_S = 300            # 5 Min: pruefe Faelligkeit
MIN_INTERVAL_HOURS = 1       # User-Settings darf nicht unter 1h fallen

_LOG = logging.getLogger(__name__)


@dataclass(slots=True)
class _Tick:
    sessions_seen: int
    devices_checked: int
    backups_taken: int
    failures: int


class BackupScheduler:
    """Hintergrund-Scheduler fuer ``trigger='scheduled'``-Backups.

    Threadsafe via internem ``RLock``. Storage-Root und Audit-Backend
    sind injizierbar damit Tests einen tmp_path bzw. ein In-Memory-
    Audit nutzen koennen.
    """

    def __init__(
        self,
        manager: SessionManager,
        *,
        backup_storage_root: Path | None = None,
        audit_backend: AuditBackend | None = None,
        tick_interval_s: int = LOOP_TICK_S,
    ) -> None:
        self._manager = manager
        self._backup_storage_root = backup_storage_root
        self._audit = audit_backend  # lazy via get_audit_backend wenn None
        self._tick_interval_s = tick_interval_s
        self._lock = RLock()
        self._thread: Thread | None = None
        self._stop = False
        self._last_tick: _Tick | None = None

    # ----- Lifecycle -----

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            self._stop = False
            self._thread = Thread(
                target=self._loop, daemon=True, name="opn-backup-scheduler",
            )
            self._thread.start()

    def stop(self) -> None:
        with self._lock:
            self._stop = True

    def last_tick(self) -> _Tick | None:
        with self._lock:
            return self._last_tick

    # ----- Loop -----

    def _loop(self) -> None:
        # Erster Tick verzoegert um den uvicorn-Startup nicht zu belasten.
        time.sleep(min(30, self._tick_interval_s))
        while True:
            if self._stop:
                return
            try:
                tick = self._tick_once()
                with self._lock:
                    self._last_tick = tick
            except (OpnCockpitError, BackupStoreError, OSError, RuntimeError):
                _LOG.exception("backup-scheduler tick failed")
            time.sleep(self._tick_interval_s)

    def _audit_backend(self) -> AuditBackend:
        if self._audit is not None:
            return self._audit
        return get_audit_backend()

    def _tick_once(self) -> _Tick:
        snapshots = self._manager.snapshot_active()
        sessions_seen = len(snapshots)
        devices_checked = 0
        backups_taken = 0
        failures = 0

        seen_vaults: set[str] = set()
        for _token, session, vault_path in snapshots:
            # Pro Tresor nur einmal arbeiten - mehrere Sessions auf
            # demselben Vault wuerden sonst doppelt sichern.
            vault_key = str(vault_path.resolve())
            if vault_key in seen_vaults:
                continue
            seen_vaults.add(vault_key)

            settings = session.opened.data.settings
            if not settings.scheduled_backup_enabled:
                continue
            interval_h = max(MIN_INTERVAL_HOURS, settings.scheduled_backup_interval_hours)
            devices = list(session.opened.data.devices)
            devices_checked += len(devices)

            due_devices = [d for d in devices if self._is_due(d, interval_h)]
            if not due_devices:
                continue

            tuning = HttpTuning(
                connect_timeout_s=settings.connect_timeout_s,
                read_timeout_s=settings.read_timeout_s,
                reconfigure_timeout_s=settings.reconfigure_timeout_s,
                retry_count=settings.retry_count,
            )
            workers = max(1, min(settings.max_workers, len(due_devices)))
            # functools.partial bindet die Schleifen-Locals an die Worker-
            # Aufrufe (ruff B023). Saubere Variante statt lambda mit
            # Closure-Falle bei mehreren Sessions im selben Tick.
            worker = partial(
                self._take_one,
                tuning=tuning,
                retention_pre_apply=settings.backup_retention_pre_apply,
                retention_scheduled=settings.backup_retention_scheduled,
            )
            with ThreadPoolExecutor(max_workers=workers) as pool:
                results = list(pool.map(worker, due_devices))
            for ok in results:
                if ok:
                    backups_taken += 1
                else:
                    failures += 1

        return _Tick(
            sessions_seen=sessions_seen,
            devices_checked=devices_checked,
            backups_taken=backups_taken,
            failures=failures,
        )

    # ----- Per-Device -----

    def _is_due(self, device: VaultDevice, interval_hours: int) -> bool:
        """True wenn das letzte ``scheduled``-Backup laenger her ist als das Intervall."""
        try:
            records = list_backups(device.id, storage_root=self._backup_storage_root)
        except BackupStoreError:
            # Kein Index lesbar -> wir wagen einen frischen Start
            return True
        scheduled = [r for r in records if r.trigger == "scheduled"]
        if not scheduled:
            return True
        latest = max(scheduled, key=lambda r: r.timestamp_utc)
        try:
            last_dt = datetime.fromisoformat(latest.timestamp_utc.replace("Z", "+00:00"))
        except ValueError:
            return True
        return datetime.now(UTC) - last_dt >= timedelta(hours=interval_hours)

    def _take_one(
        self,
        device: VaultDevice,
        *,
        tuning: HttpTuning,
        retention_pre_apply: int,
        retention_scheduled: int,
    ) -> bool:
        """Holt + persistiert ein scheduled Backup. True bei Erfolg."""
        target = HttpTarget(host=device.host, port=device.port, verify=device.tls_verify)
        try:
            with HttpClient(targets=[target], tuning=tuning) as client:
                content = download_backup(client, target, device.api_key, device.api_secret)
        except OpnCockpitError as exc:
            self._audit_backend().append(
                AuditEventKind.SCHEDULED_BACKUP,
                action="scheduled_backup",
                target_device_id=device.id,
                target_device_name=device.name,
                error_kind=exc.context.error_kind,
                summary=(
                    f"Scheduled-Backup FEHLGESCHLAGEN bei '{device.name}': "
                    f"{exc.context.summary or exc.context.error_kind or 'unbekannt'}"
                ),
            )
            return False

        try:
            record = append_backup(
                device.id,
                content,
                trigger="scheduled",
                device_name_at_creation=device.name,
                storage_root=self._backup_storage_root,
            )
        except BackupStoreError as exc:
            self._audit_backend().append(
                AuditEventKind.SCHEDULED_BACKUP,
                action="scheduled_backup",
                target_device_id=device.id,
                target_device_name=device.name,
                error_kind="store_error",
                summary=(
                    f"Scheduled-Backup KONNTE NICHT GESPEICHERT werden bei "
                    f"'{device.name}': {exc}"
                ),
            )
            return False

        # Best-Effort pruning - Fehler hier nicht eskalieren
        with contextlib.suppress(BackupStoreError, OSError):
            prune_backups(
                device.id,
                retention_pre_apply=retention_pre_apply,
                retention_scheduled=retention_scheduled,
                storage_root=self._backup_storage_root,
            )

        self._audit_backend().append(
            AuditEventKind.SCHEDULED_BACKUP,
            action="scheduled_backup",
            target_device_id=device.id,
            target_device_name=device.name,
            summary=(
                f"Scheduled-Backup ok fuer '{device.name}' "
                f"({record.size_bytes} Bytes -> {record.size_compressed} Bytes gzip)."
            ),
        )
        return True


__all__ = ["BackupScheduler"]
