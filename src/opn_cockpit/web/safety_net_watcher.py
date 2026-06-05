"""SafetyNetWatcher: armed Apply mit Countdown + Auto-Rollback via SSH.

Use-Case (Cisco-Style commit-confirmed):

1. User wendet einen Plan mit ``apply_safety_net=True`` an.
2. Cockpit fuehrt den Apply normal aus (inkl. Pre-Apply-Backup).
3. Nach erfolgreichem Verify wird pro Geraet ein ``SafetyNetEntry``
   in den Watcher gelegt - mit Deadline = jetzt + ``window_s``.
4. User klickt **innerhalb** des Windows auf "Bestaetigen" -> Entry
   wird aufgeloest, kein Rollback.
5. Verstreicht das Window ohne Bestaetigung -> Watcher SSH-rollbackt
   das Geraet auf das Pre-Apply-Backup.

Architektur:
* In-Memory, NICHT persistent (anders als RetryWatcher). Server-
  Restart bedeutet: aktive Safety-Nets fallen aus - jeder armed
  Apply muss vor dem Restart bestaetigt sein. Das ist OK weil der
  Window typischerweise wenige Minuten ist.
* Eigener Daemon-Thread, der jeden Tick (1 s) faellige Entries
  abarbeitet. Geringer Tick weil das User-feeling vom Sekunden-
  Countdown profitiert.
* Audit-Eintrag bei Arm, Confirm, Auto-Rollback, Abort.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from threading import RLock, Thread
from typing import TYPE_CHECKING

from opn_cockpit.audit.backend import AuditBackend
from opn_cockpit.audit.log import AuditEventKind
from opn_cockpit.backups import BackupNotFoundError, BackupStoreError, read_backup_content
from opn_cockpit.core.ssh_rollback import SshRollbackResult, perform_ssh_rollback

if TYPE_CHECKING:
    from opn_cockpit.vault.model import VaultDevice

_log = logging.getLogger(__name__)

DEFAULT_WINDOW_S = 120
LOOP_TICK_S = 1.0


@dataclass(slots=True)
class SafetyNetEntry:
    """Ein armed Apply pro (plan_id, device_id) mit Deadline."""

    plan_id: str
    device_id: str
    device_name: str
    pre_apply_backup_id: str
    armed_at_ms: int
    deadline_ms: int
    actor: str
    # Nach Auto-Rollback markieren wir Entry als verarbeitet, lassen ihn
    # aber kurz in der Stats-API stehen damit das UI das Ergebnis lesen
    # kann. Nach REPORT_TTL_S wird er endgueltig entfernt.
    resolved: bool = False
    resolution: str = ""  # "confirmed" | "rolled_back" | "aborted" | "rollback_failed"
    resolution_summary: str = ""
    resolved_at_ms: int = 0


REPORT_TTL_S = 600  # 10 min - dann verschwindet ein resolved Entry


@dataclass(slots=True)
class _DeviceFn:
    """Funktor: device_id -> (VaultDevice, vault_path) | None.

    Wird beim Schedule mitgegeben damit der Watcher beim Auto-Rollback
    den Tresor nicht selbst kennen muss. So bleibt der Watcher Domain-
    unabhaengig (keine Vault-Imports).
    """

    fn: object = None  # Callable[[str], VaultDevice | None]


class SafetyNetWatcher:
    """In-Memory Watchdog fuer Safety-Net-Applies."""

    def __init__(self, audit: AuditBackend) -> None:
        self._audit = audit
        self._entries: dict[tuple[str, str], SafetyNetEntry] = {}
        self._lock = RLock()
        self._thread: Thread | None = None
        self._stop = False
        # Pro plan-Eintrag eine "device-getter"-Closure halten - das
        # haelt VaultDevice/-Pfad-Lookups beim Arm fest, damit der
        # Hintergrund-Tick keinen aktiven Session-Token braucht.
        self._device_lookups: dict[
            tuple[str, str],
            object,  # Callable[[], VaultDevice | None]
        ] = {}

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            self._stop = False
            self._thread = Thread(
                target=self._loop, daemon=True, name="opn-safety-net-watcher",
            )
            self._thread.start()

    def stop(self) -> None:
        with self._lock:
            self._stop = True

    # ----- Arm + Confirm + Abort -----

    def arm(
        self,
        *,
        plan_id: str,
        device_id: str,
        device_name: str,
        pre_apply_backup_id: str,
        window_s: int,
        actor: str,
        device_lookup,  # type: ignore[no-untyped-def]
    ) -> SafetyNetEntry:
        """Legt einen armed Entry an. ``device_lookup`` ist eine 0-arg-
        Closure die beim Auto-Rollback aufgerufen wird und den aktuellen
        VaultDevice (mit SSH-Credentials) liefert.
        """
        now_ms = _now_ms()
        entry = SafetyNetEntry(
            plan_id=plan_id,
            device_id=device_id,
            device_name=device_name,
            pre_apply_backup_id=pre_apply_backup_id,
            armed_at_ms=now_ms,
            deadline_ms=now_ms + max(10, window_s) * 1000,
            actor=actor,
        )
        with self._lock:
            self._entries[(plan_id, device_id)] = entry
            self._device_lookups[(plan_id, device_id)] = device_lookup
        self._audit.append(
            AuditEventKind.PRE_APPLY_BACKUP,
            action="safety_net_arm",
            target_device_id=device_id,
            target_device_name=device_name,
            summary=(
                f"Safety-Net armed fuer '{device_name}' (Plan {plan_id}), "
                f"Window={window_s}s, Pre-Apply-Backup={pre_apply_backup_id}."
            ),
        )
        self.start()
        return entry

    def confirm(self, plan_id: str, device_id: str, actor: str) -> bool:
        """Bestaetigt einen armed Entry. Liefert False wenn unbekannt
        oder schon resolved."""
        with self._lock:
            entry = self._entries.get((plan_id, device_id))
            if entry is None or entry.resolved:
                return False
            entry.resolved = True
            entry.resolution = "confirmed"
            entry.resolution_summary = f"Bestaetigt durch {actor}."
            entry.resolved_at_ms = _now_ms()
        self._audit.append(
            AuditEventKind.PRE_APPLY_BACKUP,
            action="safety_net_confirm",
            target_device_id=device_id,
            target_device_name=entry.device_name,
            summary=(
                f"Safety-Net bestaetigt fuer '{entry.device_name}' "
                f"(Plan {plan_id}) durch {actor}."
            ),
        )
        return True

    def abort(self, plan_id: str, device_id: str, actor: str) -> bool:
        """User-getriebener Sofort-Rollback - identischer Pfad wie der
        Auto-Rollback, nur explizit ausgeloest. Kein Warten auf Deadline.
        """
        with self._lock:
            entry = self._entries.get((plan_id, device_id))
            if entry is None or entry.resolved:
                return False
            lookup = self._device_lookups.get((plan_id, device_id))
        self._rollback(entry, lookup, trigger="abort", actor=actor)
        return True

    def stats(self) -> list[SafetyNetEntry]:
        with self._lock:
            return list(self._entries.values())

    def stats_for_plan(self, plan_id: str) -> list[SafetyNetEntry]:
        with self._lock:
            return [e for e in self._entries.values() if e.plan_id == plan_id]

    # ----- Loop -----

    def _loop(self) -> None:
        while True:
            time.sleep(LOOP_TICK_S)
            if self._stop:
                return
            try:
                self._tick(_now_ms())
            except Exception:  # noqa: BLE001
                _log.exception("SafetyNetWatcher-Tick crashte")

    def _tick(self, now_ms: int) -> None:
        with self._lock:
            snapshot = list(self._entries.items())
        ttl_cutoff_ms = now_ms - REPORT_TTL_S * 1000
        for key, entry in snapshot:
            if entry.resolved and entry.resolved_at_ms < ttl_cutoff_ms:
                with self._lock:
                    self._entries.pop(key, None)
                    self._device_lookups.pop(key, None)
                continue
            if entry.resolved:
                continue
            if now_ms >= entry.deadline_ms:
                with self._lock:
                    lookup = self._device_lookups.get(key)
                self._rollback(
                    entry, lookup, trigger="deadline", actor="safety-net-watcher",
                )

    def _rollback(
        self,
        entry: SafetyNetEntry,
        lookup,  # type: ignore[no-untyped-def]
        *,
        trigger: str,
        actor: str,
    ) -> None:
        """Faehrt den SSH-Rollback aus + markiert Entry als resolved."""
        # Pre-Apply-XML aus dem Backup-Store holen
        try:
            xml_bytes = read_backup_content(entry.device_id, entry.pre_apply_backup_id)
        except (BackupNotFoundError, BackupStoreError) as exc:
            self._finalize_rollback(
                entry,
                resolution="rollback_failed",
                summary=(
                    f"Rollback fuer '{entry.device_name}' nicht moeglich - "
                    f"Pre-Apply-Backup nicht lesbar: {exc}"
                ),
                trigger=trigger,
                actor=actor,
                success=False,
            )
            return

        device = None
        if callable(lookup):
            try:
                device = lookup()
            except Exception:  # noqa: BLE001
                device = None
        if device is None:
            self._finalize_rollback(
                entry,
                resolution="rollback_failed",
                summary=(
                    f"Rollback fuer '{entry.device_name}' nicht moeglich - "
                    "Vault-Lookup hat keinen Geraete-Datensatz geliefert."
                ),
                trigger=trigger,
                actor=actor,
                success=False,
            )
            return

        result: SshRollbackResult = perform_ssh_rollback(
            device,
            xml_bytes,
            pre_apply_backup_id=entry.pre_apply_backup_id,
        )
        self._finalize_rollback(
            entry,
            resolution="rolled_back" if result.success else "rollback_failed",
            summary=result.summary,
            trigger=trigger,
            actor=actor,
            success=result.success,
        )

    def _finalize_rollback(
        self,
        entry: SafetyNetEntry,
        *,
        resolution: str,
        summary: str,
        trigger: str,
        actor: str,
        success: bool,
    ) -> None:
        with self._lock:
            entry.resolved = True
            entry.resolution = resolution
            entry.resolution_summary = summary
            entry.resolved_at_ms = _now_ms()
        action = "safety_net_rollback" if success else "safety_net_rollback_failed"
        self._audit.append(
            AuditEventKind.PRE_APPLY_BACKUP,
            action=action,
            target_device_id=entry.device_id,
            target_device_name=entry.device_name,
            summary=(
                f"Safety-Net {'ROLLBACK ok' if success else 'ROLLBACK FEHLGESCHLAGEN'} "
                f"fuer '{entry.device_name}' (Plan {entry.plan_id}, "
                f"Trigger={trigger}, Actor={actor}): {summary}"
            ),
        )


def _now_ms() -> int:
    return int(time.time() * 1000)


__all__ = [
    "DEFAULT_WINDOW_S",
    "SafetyNetEntry",
    "SafetyNetWatcher",
]
