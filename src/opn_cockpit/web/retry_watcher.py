"""Hintergrund-Auto-Retry fuer fehlgeschlagene Geraete in einem Plan.

Spielt das Szenario: User rollt eine Aktion auf 25 Boxen aus, 3 sind
gerade offline. Statt manuell stunden spaeter ein Karten-Badge zu
klicken, schiebt der RetryWatcher die fehlgeschlagenen Geraete in eine
in-memory Queue und probiert sie alle X Minuten erneut. Sobald ein
Geraet erfolgreich ist, sinkt der Outstanding-Count auf der Karte
automatisch — der User sieht ohne Klick, wie sich die Liste leert.

Design-Constraints:
* Watcher laeuft als Daemon-Thread im uvicorn-Prozess. Bei Server-Stopp
  endet er mit.
* Pro Job halten wir einen ``session_token`` — der Watcher loest die
  Session beim Naechste-Iteration ueber den SessionManager auf. Wenn
  der User die Session sperrt (manuell oder Inaktivitaet), verfaellt
  das Token, der Watcher cancelt den Job.
* Wenn ein Job erfolgreich abschliesst (failures == 0), wird er
  entfernt.
* ``max_duration_s`` deckelt jeden Job (Default 1 h) — sonst koennte
  ein dauerhaft offline Box den Watcher endlos beschaeftigen.
* Alle Audit-relevanten Events landen automatisch im Audit-Log ueber
  den existierenden Executor-Pfad (run_apply -> Executor -> Audit).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from threading import RLock, Thread

from opn_cockpit.web.auth.manager import SessionManager

DEFAULT_INTERVAL_S = 180     # 3 Min zwischen Versuchen pro Job
DEFAULT_MAX_DURATION_S = 3600  # 1 h Gesamtdauer pro Job
LOOP_TICK_S = 10              # wie oft der Loop checkt, ob ein Job faellig ist


@dataclass(slots=True)
class RetryJob:
    """Ein laufender Auto-Retry pro Plan + ausgewaehlter Geraete."""

    plan_id: str
    session_token: str
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
    device_ids: list[str]
    attempts: int
    last_failure_count: int
    started_at_ms: int
    next_attempt_at_ms: int
    paused: bool


class RetryWatcher:
    """In-Memory Auto-Retry-Queue. Threadsafe.

    Wird beim ``create_app`` als ``app.state.retry_watcher`` instanziiert.
    """

    def __init__(self, manager: SessionManager) -> None:
        self._manager = manager
        self._jobs: dict[str, RetryJob] = {}
        self._lock = RLock()
        self._thread: Thread | None = None
        self._stop = False

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
        device_ids: list[str],
        interval_s: int = DEFAULT_INTERVAL_S,
        max_duration_s: int = DEFAULT_MAX_DURATION_S,
    ) -> RetryJob:
        """Legt einen neuen Retry-Job an oder ueberschreibt einen vorhandenen."""
        now_ms = _now_ms()
        job = RetryJob(
            plan_id=plan_id,
            session_token=session_token,
            device_ids=list(device_ids),
            started_at_ms=now_ms,
            next_attempt_at_ms=now_ms + interval_s * 1000,
            interval_s=interval_s,
            max_duration_s=max_duration_s,
        )
        with self._lock:
            self._jobs[plan_id] = job
        self.start()
        return job

    def cancel(self, plan_id: str) -> bool:
        with self._lock:
            return self._jobs.pop(plan_id, None) is not None

    def pause(self, plan_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(plan_id)
            if job is None:
                return False
            job.paused = True
            return True

    def resume(self, plan_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(plan_id)
            if job is None:
                return False
            job.paused = False
            # Naechster Versuch sofort
            job.next_attempt_at_ms = _now_ms()
            return True

    def stats(self) -> list[_Stats]:
        with self._lock:
            return [
                _Stats(
                    plan_id=j.plan_id,
                    session_token=j.session_token,
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
        """Beendet alle Jobs, die zu diesem Token gehoeren (z. B. beim Lock)."""
        with self._lock:
            to_remove = [pid for pid, j in self._jobs.items() if j.session_token == session_token]
            for pid in to_remove:
                del self._jobs[pid]
            return len(to_remove)

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
        session = self._manager.get(job.session_token)
        if session is None:
            self.cancel(job.plan_id)
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
                return
            pending.device_ids = new_failed
            pending.next_attempt_at_ms = now_ms + pending.interval_s * 1000


def _now_ms() -> int:
    return int(time.time() * 1000)


__all__ = ["DEFAULT_INTERVAL_S", "DEFAULT_MAX_DURATION_S", "RetryJob", "RetryWatcher"]
