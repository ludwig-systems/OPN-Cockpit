"""SafetyNetWatcher v2: Cleanup-Retry + Fire-Marker-Detection.

Anders als die v1-Variante (Cisco-Style commit-confirmed mit Cockpit-
seitigem Auto-Rollback) ist dieser Watcher **passiv**: das eigentliche
Sicherheitsnetz haengt auf der OPNsense in Form eines ``daemon(8)``-
Timers, den der Executor vor jedem Apply armed. Der Watcher hat zwei
Aufgaben:

1. **Cleanup-Retry**: wenn der Executor den Disarm direkt nach dem
   Apply nicht hingekriegt hat (3 Sofortversuche schlagen fehl), uebergibt
   er den Job hierhin. Der Watcher probiert alle 30 s erneut, bis
   entweder der Disarm gelingt (= Cockpit hat den Daemon gestoppt) oder
   das Window abgelaufen ist (= der Daemon hat selbst gefeuert).

2. **Fire-Marker-Detection**: wenn das Window abgelaufen ist, sucht der
   Watcher beim naechsten erfolgreichen SSH-Tick die Marker-Datei
   ``/var/log/cockpit-safety-<jobid>.fired`` auf der Box. Wenn sie
   existiert, hat der Dead-Man's-Switch ausgeloest. Audit-Eintrag +
   Status fuer UI-Banner. Wenn sie fehlt: die Box war einfach offline,
   keine Aenderung passiert (kein Reboot).

Persistenz: Cockpit-Restart darf die Detection nicht killen, deshalb
liegt der Zustand in ``%APPDATA%\\OPN-Cockpit\\state\\safety-net-pending.json``.
Nach jeder Mutation atomar geschrieben (write-to-tmp + replace).

UI-Lifecycle (REPORT_TTL_S): ein resolved Entry (disarmed_late /
fire_detected / expired_unresolved) bleibt 10 Minuten in der Liste,
damit das UI das Resultat anzeigen kann. Danach wird er entfernt.

Session-Adoption analog ``retry_watcher.RetryWatcher``: nach
Server-Restart sind alle ``session_token`` leer; auf den naechsten Tick
adoptiert der Watcher den Eintrag ueber ``vault_path``.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from threading import RLock, Thread

from opn_cockpit.audit.backend import AuditBackend
from opn_cockpit.audit.log import AuditEventKind
from opn_cockpit.core import ssh_safety_net
from opn_cockpit.web.auth.manager import SessionManager

_log = logging.getLogger(__name__)

DEFAULT_TICK_S = 30.0
QUEUE_FILE_NAME = "safety-net-pending.json"
QUEUE_FORMAT_VERSION = 1
REPORT_TTL_S = 600  # 10 min


# ----- Status-Strings (auch fuer UI-Polling) -----
STATUS_PENDING_DISARM = "pending_disarm"
STATUS_DISARMED_LATE = "disarmed_late"
STATUS_FIRE_DETECTED = "fire_detected"
STATUS_EXPIRED_UNRESOLVED = "expired_unresolved"


@dataclass(slots=True)
class SafetyNetEntry:
    """Ein pending-Eintrag des Safety-Net-Watchers.

    ``vault_path`` macht den Eintrag Server-Restart-resistent: nach
    Reload adoptieren wir den Entry ueber den Tresor, der ihn ja
    sowieso bei der Apply-Anfrage entsperrt hatte.
    """

    plan_id: str
    device_id: str
    device_name: str
    jobid: str
    vault_path: str
    pre_apply_backup_id: str
    armed_at_ms: int
    window_s: int
    status: str
    next_attempt_at_ms: int
    last_summary: str = ""
    resolved_at_ms: int = 0


class SafetyNetWatcher:
    """Persistenter Cleanup + Marker-Detector. Threadsafe."""

    def __init__(
        self,
        manager: SessionManager,
        audit: AuditBackend,
        *,
        queue_path: Path | None = None,
        tick_s: float = DEFAULT_TICK_S,
    ) -> None:
        self._manager = manager
        self._audit = audit
        self._entries: dict[tuple[str, str], SafetyNetEntry] = {}
        self._lock = RLock()
        self._thread: Thread | None = None
        self._stop = False
        self._queue_path = queue_path
        self._tick_s = tick_s
        if queue_path is not None:
            self._load_from_disk()
            if self._entries:
                self.start()

    # ----- Lifecycle -----

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop = False
            self._thread = Thread(
                target=self._loop,
                daemon=True,
                name="opn-safety-net-watcher",
            )
            self._thread.start()

    def stop(self) -> None:
        with self._lock:
            self._stop = True

    # ----- Public API: Enqueue + Stats -----

    def enqueue_pending_disarm(
        self,
        *,
        plan_id: str,
        device_id: str,
        device_name: str,
        jobid: str,
        pre_apply_backup_id: str,
        window_s: int,
        vault_path: str | None = None,
    ) -> SafetyNetEntry:
        """Nimmt einen pending-disarm Entry vom Executor entgegen.

        ``vault_path`` ist optional - wenn nicht angegeben, versuchen wir
        ihn aus einer aktiven Session zu ziehen. Ohne vault_path keine
        Adoption nach Server-Restart.
        """
        if vault_path is None:
            vault_path = self._infer_vault_path()
        now_ms = _now_ms()
        entry = SafetyNetEntry(
            plan_id=plan_id,
            device_id=device_id,
            device_name=device_name,
            jobid=jobid,
            vault_path=vault_path,
            pre_apply_backup_id=pre_apply_backup_id,
            armed_at_ms=now_ms,
            window_s=max(60, int(window_s)),
            status=STATUS_PENDING_DISARM,
            next_attempt_at_ms=now_ms + int(self._tick_s * 1000),
        )
        with self._lock:
            self._entries[(plan_id, device_id)] = entry
            self._save_to_disk()
        self.start()
        return entry

    def stats(self) -> list[SafetyNetEntry]:
        with self._lock:
            return list(self._entries.values())

    def stats_for_plan(self, plan_id: str) -> list[SafetyNetEntry]:
        with self._lock:
            return [e for e in self._entries.values() if e.plan_id == plan_id]

    def acknowledge(self, plan_id: str, device_id: str) -> bool:
        """User-Acknowledge eines resolved Entry. Entfernt ihn sofort.

        Sinnvoll fuer ``fire_detected`` / ``expired_unresolved`` -
        ``pending_disarm`` darf der User nicht wegklicken (sonst weiss
        niemand mehr ob die Box rebootet hat).
        """
        with self._lock:
            entry = self._entries.get((plan_id, device_id))
            if entry is None:
                return False
            if entry.status == STATUS_PENDING_DISARM:
                return False
            self._entries.pop((plan_id, device_id), None)
            self._save_to_disk()
        return True

    # ----- Loop -----

    def _loop(self) -> None:
        while True:
            time.sleep(self._tick_s)
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
        dirty = False
        for key, entry in snapshot:
            # TTL-Reap fuer resolved Entries.
            if entry.status != STATUS_PENDING_DISARM and entry.resolved_at_ms > 0:
                if entry.resolved_at_ms < ttl_cutoff_ms:
                    with self._lock:
                        self._entries.pop(key, None)
                    dirty = True
                continue
            if entry.status != STATUS_PENDING_DISARM:
                continue
            if now_ms < entry.next_attempt_at_ms:
                continue
            # Pending-Disarm Tick: SSH probieren.
            self._process_pending(entry)
            dirty = True
        if dirty:
            with self._lock:
                self._save_to_disk()

    # ----- Pending-Disarm processing -----

    def _process_pending(self, entry: SafetyNetEntry) -> None:
        """Einzelner SSH-Versuch fuer einen pending Entry.

        Drei mögliche Outcomes:
        - SSH unreachable: next_attempt_at_ms hochsetzen, status bleibt
        - SSH reachable + disarm ok: status -> disarmed_late
        - SSH reachable + window abgelaufen: marker pruefen -> fire_detected
          oder expired_unresolved
        """
        vault_device = self._lookup_vault_device(entry)
        if vault_device is None:
            # Vault nicht entsperrt - wir koennen nichts tun ausser
            # warten bis der User wieder oeffnet (Adoption laeuft im
            # naechsten Tick automatisch).
            self._reschedule(entry, summary="Vault nicht entsperrt.")
            return

        now_ms = _now_ms()
        window_elapsed = (
            now_ms - entry.armed_at_ms
        ) > (entry.window_s * 1000)

        if window_elapsed:
            # Window ist abgelaufen - entweder ist der Daemon schon
            # gefeuert (Marker da, Box hat rebootet, nach Reboot wieder
            # online), oder er feuert gleich. Marker pruefen.
            marker_res = ssh_safety_net.check_marker(
                vault_device, jobid=entry.jobid, cleanup_if_found=True,
            )
            if not marker_res.success:
                # Box weiterhin nicht erreichbar (eventuell mitten im
                # Reboot). Reschedule und warten.
                self._reschedule(entry, summary=marker_res.summary)
                return
            if marker_res.fired:
                self._finalize(
                    entry,
                    status=STATUS_FIRE_DETECTED,
                    summary=(
                        "Dead-Man-Switch hat ausgeloest - die Apply-Aenderung "
                        "hat die Firewall aus Cockpit-Sicht ausgesperrt. "
                        "Pre-Apply-Backup wurde zurueck eingespielt und die "
                        "Box rebootet."
                    ),
                    audit_action="safety_net_fired",
                )
                return
            # Window vorbei, aber kein Marker: die Box war offline,
            # ist jetzt wieder da, der Daemon hat aber NICHT gefeuert.
            # Theoretisch unmoeglich (Daemon wuerde sleep(N) machen);
            # praktisch wenn die Box waehrend des Windows hart aus war
            # und neu gestartet ist, ist der daemon weg. Fuer den User
            # heisst das: Apply ist passiert, kein Rollback. Best-Effort
            # Cleanup.
            disarm_res = ssh_safety_net.disarm(vault_device, jobid=entry.jobid)
            self._finalize(
                entry,
                status=STATUS_EXPIRED_UNRESOLVED,
                summary=(
                    "Safety-Net-Window abgelaufen, kein Fire-Marker auf der "
                    "Box - vermutlich war die Box im Window offline und der "
                    "Daemon-Job ist mit ihr verschwunden. Apply-Aenderung "
                    f"steht. Cleanup: {disarm_res.summary}"
                ),
                audit_action="safety_net_expired_unresolved",
            )
            return

        # Window noch nicht abgelaufen - normaler Disarm-Versuch.
        disarm_res = ssh_safety_net.disarm(vault_device, jobid=entry.jobid)
        if disarm_res.success:
            self._finalize(
                entry,
                status=STATUS_DISARMED_LATE,
                summary=(
                    "Safety-Net im Hintergrund disarmed - der direkte Disarm "
                    "nach dem Apply hatte gehakelt, der Watcher hat nachgezogen."
                ),
                audit_action="safety_net_disarmed_late",
            )
            return
        self._reschedule(entry, summary=disarm_res.summary)

    def _reschedule(self, entry: SafetyNetEntry, *, summary: str) -> None:
        with self._lock:
            stored = self._entries.get((entry.plan_id, entry.device_id))
            if stored is None:
                return
            stored.next_attempt_at_ms = _now_ms() + int(self._tick_s * 1000)
            stored.last_summary = summary

    def _finalize(
        self,
        entry: SafetyNetEntry,
        *,
        status: str,
        summary: str,
        audit_action: str,
    ) -> None:
        with self._lock:
            stored = self._entries.get((entry.plan_id, entry.device_id))
            if stored is None:
                return
            stored.status = status
            stored.resolved_at_ms = _now_ms()
            stored.last_summary = summary
        self._audit.append(
            AuditEventKind.PRE_APPLY_BACKUP,
            action=audit_action,
            target_device_id=entry.device_id,
            target_device_name=entry.device_name,
            summary=(
                f"Safety-Net auf '{entry.device_name}' "
                f"(Plan {entry.plan_id}, jobid={entry.jobid}): {summary}"
            ),
        )

    # ----- Session / Device-Lookup -----

    def _lookup_vault_device(self, entry: SafetyNetEntry):  # type: ignore[no-untyped-def]
        """Sucht eine aktive Session zum Vault-Path und liefert das
        VaultDevice (mit SSH-Credentials).
        """
        if not entry.vault_path:
            return None
        target = Path(entry.vault_path)
        try:
            sessions = list(self._manager.sessions())
        except Exception:  # noqa: BLE001
            return None
        for session in sessions:
            opened = getattr(session, "opened", None)
            if opened is None:
                continue
            vp = getattr(opened, "vault_path", None)
            if vp is None or not _same_path(vp, target):
                continue
            for d in opened.data.devices:
                if d.id == entry.device_id:
                    return d
        return None

    def _infer_vault_path(self) -> str:
        """Greift sich den vault_path der zuletzt entsperrten Session ab.

        Best-Effort: wenn keine Session offen ist (Tests, CLI), liefert
        leeren String. Dann hat der Entry keine Adoption-Quelle nach
        Restart.
        """
        try:
            sessions = list(self._manager.sessions())
        except Exception:  # noqa: BLE001
            return ""
        for session in sessions:
            opened = getattr(session, "opened", None)
            if opened is None:
                continue
            vp = getattr(opened, "vault_path", None)
            if vp is not None:
                return str(vp)
        return ""

    # ----- Persistenz -----

    def _save_to_disk(self) -> None:
        if self._queue_path is None:
            return
        try:
            self._queue_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": QUEUE_FORMAT_VERSION,
                "entries": [asdict(e) for e in self._entries.values()],
            }
            tmp = self._queue_path.with_suffix(
                self._queue_path.suffix + ".tmp",
            )
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self._queue_path)
        except OSError:
            _log.exception(
                "SafetyNetWatcher: Persistenz fehlgeschlagen (%s)",
                self._queue_path,
            )

    def _load_from_disk(self) -> None:
        if self._queue_path is None or not self._queue_path.exists():
            return
        try:
            with self._queue_path.open(encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, json.JSONDecodeError):
            _log.exception(
                "SafetyNetWatcher: Persistierte Queue nicht lesbar (%s)",
                self._queue_path,
            )
            return
        if not isinstance(raw, dict):
            return
        entries_raw = raw.get("entries", [])
        if not isinstance(entries_raw, list):
            return
        loaded = 0
        for item in entries_raw:
            if not isinstance(item, dict):
                continue
            try:
                entry = SafetyNetEntry(
                    plan_id=str(item.get("plan_id", "")),
                    device_id=str(item.get("device_id", "")),
                    device_name=str(item.get("device_name", "")),
                    jobid=str(item.get("jobid", "")),
                    vault_path=str(item.get("vault_path", "")),
                    pre_apply_backup_id=str(item.get("pre_apply_backup_id", "")),
                    armed_at_ms=int(item.get("armed_at_ms", 0)),
                    window_s=int(item.get("window_s", 300)),
                    status=str(item.get("status", STATUS_PENDING_DISARM)),
                    next_attempt_at_ms=int(item.get("next_attempt_at_ms", 0)),
                    last_summary=str(item.get("last_summary", "")),
                    resolved_at_ms=int(item.get("resolved_at_ms", 0)),
                )
            except (TypeError, ValueError):
                continue
            if not entry.plan_id or not entry.device_id:
                continue
            self._entries[(entry.plan_id, entry.device_id)] = entry
            loaded += 1
        if loaded:
            _log.info(
                "SafetyNetWatcher: %d persistente Entries geladen aus %s",
                loaded, self._queue_path,
            )


def _same_path(a: Path | None, b: Path) -> bool:
    if a is None:
        return False
    with contextlib.suppress(OSError):
        return a.resolve() == b.resolve()
    return str(a) == str(b)


def _now_ms() -> int:
    return int(time.time() * 1000)


__all__ = [
    "DEFAULT_TICK_S",
    "REPORT_TTL_S",
    "STATUS_DISARMED_LATE",
    "STATUS_EXPIRED_UNRESOLVED",
    "STATUS_FIRE_DETECTED",
    "STATUS_PENDING_DISARM",
    "SafetyNetEntry",
    "SafetyNetWatcher",
]
