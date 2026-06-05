"""Hintergrund-Auto-Retry fuer fehlgeschlagene Geraete in einem Plan.

Spielt das Szenario: User rollt eine Aktion auf 25 Boxen aus, 3 sind
gerade offline. Statt manuell stunden spaeter ein Karten-Badge zu
klicken, schiebt der RetryWatcher die fehlgeschlagenen Geraete in eine
Queue und probiert sie alle X Minuten erneut. Sobald ein Geraet
erfolgreich ist, sinkt der Outstanding-Count auf der Karte automatisch.

Design-Constraints:
* Watcher laeuft als Daemon-Thread im uvicorn-Prozess. Bei Server-Stopp
  endet er mit.
* Pro Job halten wir einen ``session_token`` und den ``vault_path`` der
  Session. Beim Naechste-Iteration wird zuerst der Token versucht; wenn
  er nicht mehr gilt (Lock, Inaktivitaet, Restart), versucht der Watcher
  eine **Orphan-Adoption**: er sucht eine aktive Session mit demselben
  vault_path und uebernimmt deren Token. Damit ueberlebt der Job sowohl
  Session-Lock als auch Server-Restart, sobald jemand wieder den Tresor
  entsperrt.
* Wenn ein Job erfolgreich abschliesst (failures == 0), wird er
  entfernt.
* ``max_duration_s`` deckelt jeden Job (Default 1 h).
* **Persistenz**: alle Job-Zustaende landen in
  ``<app_data>/state/retry-queue.json``. Nach Mutationen wird atomar
  geschrieben (write-to-tmp + replace). Beim Start liest der Watcher
  die Datei und beginnt mit ``session_token=""`` (alle Jobs Orphan).
  Auf den naechsten Tick adoptiert er sie via vault_path.
* Alle Audit-relevanten Events landen automatisch im Audit-Log ueber
  den existierenden Executor-Pfad (run_apply -> Executor -> Audit).
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

from opn_cockpit.web.auth.manager import SessionManager

_log = logging.getLogger(__name__)

DEFAULT_INTERVAL_S = 180     # 3 Min zwischen Versuchen pro Job
DEFAULT_MAX_DURATION_S = 3600  # 1 h Gesamtdauer pro Job
LOOP_TICK_S = 10              # wie oft der Loop checkt, ob ein Job faellig ist
QUEUE_FILE_NAME = "retry-queue.json"
QUEUE_FORMAT_VERSION = 1


@dataclass(slots=True)
class RetryJob:
    """Ein laufender Auto-Retry pro Plan + ausgewaehlter Geraete."""

    plan_id: str
    session_token: str
    vault_path: str
    device_ids: list[str]
    started_at_ms: int
    next_attempt_at_ms: int
    interval_s: int
    max_duration_s: int
    attempts: int = 0
    last_failure_count: int = 0
    paused: bool = False

    def is_expired(self, now_ms: int) -> bool:
        return (now_ms - self.started_at_ms) > self.max_duration_s * 1000

    def is_due(self, now_ms: int) -> bool:
        return not self.paused and now_ms >= self.next_attempt_at_ms


@dataclass(slots=True)
class _Stats:
    plan_id: str
    session_token: str
    vault_path: str
    device_ids: list[str]
    attempts: int
    last_failure_count: int
    started_at_ms: int
    next_attempt_at_ms: int
    paused: bool


class RetryWatcher:
    """Persistente Auto-Retry-Queue. Threadsafe.

    Wird beim ``create_app`` als ``app.state.retry_watcher`` instanziiert.
    ``queue_path`` ist optional; ohne Pfad bleibt der Watcher in-memory
    (Tests). Mit Pfad: laedt beim Start die persistierte Queue und
    schreibt nach jeder Mutation atomar zurueck.
    """

    def __init__(
        self,
        manager: SessionManager,
        *,
        queue_path: Path | None = None,
    ) -> None:
        self._manager = manager
        self._jobs: dict[str, RetryJob] = {}
        self._lock = RLock()
        self._thread: Thread | None = None
        self._stop = False
        self._queue_path = queue_path
        if queue_path is not None:
            self._load_from_disk()
            if self._jobs:
                # Persistente Jobs vorhanden -> Daemon-Thread sofort starten,
                # damit Orphan-Adoption ablaeuft sobald die erste Session
                # erscheint. Sonst startet der Loop erst beim ersten
                # schedule()-Call.
                self.start()

    # ----- Lifecycle -----

    def start(self) -> None:
        """Startet den Hintergrund-Thread, falls noch nicht laeuft."""
        with self._lock:
            if self._thread is not None:
                return
            self._stop = False
            self._thread = Thread(target=self._loop, daemon=True, name="opn-retry-watcher")
            self._thread.start()

    def stop(self) -> None:
        """Signalisiert Stopp. Thread laeuft Tick-Intervall noch zu Ende."""
        with self._lock:
            self._stop = True

    # ----- Job-CRUD -----

    def schedule(
        self,
        *,
        plan_id: str,
        session_token: str,
        vault_path: str,
        device_ids: list[str],
        interval_s: int = DEFAULT_INTERVAL_S,
        max_duration_s: int = DEFAULT_MAX_DURATION_S,
    ) -> RetryJob:
        """Legt einen neuen Retry-Job an oder ueberschreibt einen vorhandenen.

        ``vault_path`` ist der absolute Pfad des entsperrten Tresors -
        wird bei Orphan-Adoption als Match-Key verwendet wenn der
        ``session_token`` nicht mehr gilt (Lock, Server-Restart).
        """
        now_ms = _now_ms()
        job = RetryJob(
            plan_id=plan_id,
            session_token=session_token,
            vault_path=vault_path,
            device_ids=list(device_ids),
            started_at_ms=now_ms,
            next_attempt_at_ms=now_ms + interval_s * 1000,
            interval_s=interval_s,
            max_duration_s=max_duration_s,
        )
        with self._lock:
            self._jobs[plan_id] = job
            self._save_to_disk()
        self.start()
        return job

    def cancel(self, plan_id: str) -> bool:
        with self._lock:
            removed = self._jobs.pop(plan_id, None) is not None
            if removed:
                self._save_to_disk()
            return removed

    def pause(self, plan_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(plan_id)
            if job is None:
                return False
            job.paused = True
            self._save_to_disk()
            return True

    def resume(self, plan_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(plan_id)
            if job is None:
                return False
            job.paused = False
            # Naechster Versuch sofort
            job.next_attempt_at_ms = _now_ms()
            self._save_to_disk()
            return True

    def stats(self) -> list[_Stats]:
        with self._lock:
            return [
                _Stats(
                    plan_id=j.plan_id,
                    session_token=j.session_token,
                    vault_path=j.vault_path,
                    device_ids=list(j.device_ids),
                    attempts=j.attempts,
                    last_failure_count=j.last_failure_count,
                    started_at_ms=j.started_at_ms,
                    next_attempt_at_ms=j.next_attempt_at_ms,
                    paused=j.paused,
                )
                for j in self._jobs.values()
            ]

    def cancel_for_session(self, session_token: str) -> int:
        """Beendet alle Jobs, die zu diesem Token gehoeren (z. B. beim Lock).

        Anders als frueher: wir setzen Jobs hier auf Orphan-Status zurueck
        (session_token=""), damit sie ueberleben und beim naechsten Unlock
        adoptiert werden koennen. Nur explizites ``cancel()`` loescht den
        Job. So verliert der User bei Inaktivitaets-Lock keine offenen
        Retries.
        """
        with self._lock:
            count = 0
            for j in self._jobs.values():
                if j.session_token == session_token:
                    j.session_token = ""
                    count += 1
            if count > 0:
                self._save_to_disk()
            return count

    # ----- Loop -----

    def _loop(self) -> None:
        """Daemon-Thread: alle ``LOOP_TICK_S`` Sekunden faellige Jobs abarbeiten."""
        while True:
            time.sleep(LOOP_TICK_S)
            if self._stop:
                return
            self._tick(_now_ms())

    def _tick(self, now_ms: int) -> None:
        with self._lock:
            snapshot = list(self._jobs.values())
        for job in snapshot:
            self._maybe_run(job, now_ms)

    def _maybe_run(self, job: RetryJob, now_ms: int) -> None:
        if not job.is_due(now_ms):
            return
        if job.is_expired(now_ms):
            self.cancel(job.plan_id)
            return
        session = self._resolve_session(job)
        if session is None:
            # Orphan: kein passender Token + keine adoptierbare Session.
            # Job stehen lassen - sobald jemand den Tresor entsperrt,
            # adoptieren wir im naechsten Tick.
            return
        # Late import - vermeidet Circular dependency mit web/api/plans.py
        from opn_cockpit.web.api.plans import run_apply  # noqa: PLC0415

        try:
            _plan, report = run_apply(
                session, job.plan_id, device_ids=list(job.device_ids),
            )
        except Exception:  # Job darf nie crashen
            with self._lock:
                pending = self._jobs.get(job.plan_id)
                if pending is not None:
                    pending.attempts += 1
                    pending.next_attempt_at_ms = now_ms + pending.interval_s * 1000
                    self._save_to_disk()
            return

        failures = report.failures
        with self._lock:
            pending = self._jobs.get(job.plan_id)
            if pending is None:
                return
            pending.attempts += 1
            pending.last_failure_count = failures
            new_failed = [
                r.device_id for r in report.results
                if str(r.status) not in ("Verifiziert", "Übersprungen")
            ]
            if not new_failed:
                del self._jobs[job.plan_id]
                self._save_to_disk()
                return
            pending.device_ids = new_failed
            pending.next_attempt_at_ms = now_ms + pending.interval_s * 1000
            self._save_to_disk()

    def _resolve_session(self, job: RetryJob):  # type: ignore[no-untyped-def]
        """Versucht den Token aufzuloesen; bei Fehlschlag Orphan-Adoption.

        Adoption: durchsucht aktive Sessions nach einer mit demselben
        ``vault_path`` und uebernimmt deren Token. Damit ueberleben Jobs
        sowohl Session-Lock (Inaktivitaet) als auch Server-Restart -
        sobald jemand den Tresor wieder entsperrt, springt der Watcher
        an.
        """
        if job.session_token:
            session = self._manager.get(job.session_token)
            if session is not None:
                return session
        # Orphan-Adoption ueber vault_path
        if not job.vault_path:
            return None
        target = Path(job.vault_path)
        for token, session, vault_path in self._manager.snapshot_active():
            if _same_path(vault_path, target):
                with self._lock:
                    current = self._jobs.get(job.plan_id)
                    if current is not None:
                        current.session_token = token
                        self._save_to_disk()
                _log.info(
                    "RetryWatcher: Job %s adoptiert via vault_path %s",
                    job.plan_id, target,
                )
                return session
        return None

    # ----- Persistenz -----

    def _save_to_disk(self) -> None:
        if self._queue_path is None:
            return
        try:
            self._queue_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": QUEUE_FORMAT_VERSION,
                "jobs": [asdict(j) for j in self._jobs.values()],
            }
            tmp = self._queue_path.with_suffix(
                self._queue_path.suffix + ".tmp",
            )
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self._queue_path)
        except OSError:
            _log.exception(
                "RetryWatcher: Persistenz fehlgeschlagen (%s)",
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
                "RetryWatcher: Persistierte Queue nicht lesbar (%s)",
                self._queue_path,
            )
            return
        if not isinstance(raw, dict):
            return
        jobs_raw = raw.get("jobs", [])
        if not isinstance(jobs_raw, list):
            return
        loaded = 0
        for entry in jobs_raw:
            if not isinstance(entry, dict):
                continue
            try:
                job = RetryJob(
                    plan_id=str(entry.get("plan_id", "")),
                    # Beim Reload sind alle Tokens stale - Adoption uebernimmt
                    session_token="",
                    vault_path=str(entry.get("vault_path", "")),
                    device_ids=[str(d) for d in entry.get("device_ids", [])],
                    started_at_ms=int(entry.get("started_at_ms", 0)),
                    next_attempt_at_ms=int(entry.get("next_attempt_at_ms", 0)),
                    interval_s=int(entry.get("interval_s", DEFAULT_INTERVAL_S)),
                    max_duration_s=int(
                        entry.get("max_duration_s", DEFAULT_MAX_DURATION_S),
                    ),
                    attempts=int(entry.get("attempts", 0)),
                    last_failure_count=int(entry.get("last_failure_count", 0)),
                    paused=bool(entry.get("paused", False)),
                )
            except (TypeError, ValueError):
                continue
            if not job.plan_id:
                continue
            self._jobs[job.plan_id] = job
            loaded += 1
        if loaded:
            _log.info(
                "RetryWatcher: %d persistente Jobs geladen aus %s",
                loaded, self._queue_path,
            )


def _same_path(a: Path | None, b: Path) -> bool:
    """Vergleicht zwei Pfade tolerant gegen Case/Trailing-Slashes.

    Pfade werden via ``resolve`` normalisiert; bei FileNotFoundError
    (z. B. wenn der Tresor nicht mehr existiert) wird der String-Vergleich
    als Fallback genommen.
    """
    if a is None:
        return False
    with contextlib.suppress(OSError):
        return a.resolve() == b.resolve()
    return str(a) == str(b)


def _now_ms() -> int:
    return int(time.time() * 1000)


__all__ = [
    "DEFAULT_INTERVAL_S",
    "DEFAULT_MAX_DURATION_S",
    "QUEUE_FILE_NAME",
    "RetryJob",
    "RetryWatcher",
]
