"""In-Memory Sliding-Window Rate-Limiter fuer Login-Brute-Force-Schutz.

Schuetzt die Auth- und Bootstrap-Endpunkte gegen Massen-Brute-Force.
Pro Client-IP wird ein Fenster der letzten ``window_s`` Sekunden gezaehlt;
ueberschreitet die Anzahl ``max_attempts``, wird fuer ``cooldown_s``
Sekunden mit 429 geantwortet — egal ob das eingegebene Passwort richtig
waere.

Trade-offs:
* In-Memory pro Prozess. Bei Multi-Worker-Deployment laeuft jeder Worker
  mit eigenem Zaehler — fuer 2-5 Admin-User akzeptabel, bei groesseren
  Setups gehoerte hierhin Redis. Single-Worker (heute uvicorn-Standard
  fuer dieses Tool) ist ohnehin sinnvoller wegen des geteilten
  OpenedVault.
* IP-basiert. Wer hinter NAT sitzt teilt sich das Budget. Akzeptabel im
  Office-LAN, weniger ideal hinter Carrier-Grade-NAT.
* Erfolgreiche Logins setzen das Fenster auf 0 zurueck. Verhindert das
  Aussperren von legitimen Usern, die sich vertippt haben.

Heutige Defaults: 10 fehlgeschlagene Versuche in 15 min -> 5 min Sperre.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from threading import RLock

DEFAULT_WINDOW_S: float = 15 * 60.0
DEFAULT_MAX_ATTEMPTS: int = 10
DEFAULT_COOLDOWN_S: float = 5 * 60.0


@dataclass(slots=True)
class _Bucket:
    """Ringpuffer der Failure-Zeitstempel + optionale Sperre."""

    failures: deque[float] = field(default_factory=deque)
    locked_until: float = 0.0


@dataclass(slots=True)
class RateLimiter:
    """Sliding-Window-Failure-Counter pro Schluessel (typisch: Client-IP).

    Threadsafe via internem RLock. Speicher-Footprint pro aktiver IP ist
    ``max_attempts * 8 Bytes`` + ein paar Bytes Overhead — bei 100 IPs
    deutlich unter 100 KB.
    """

    window_s: float = DEFAULT_WINDOW_S
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    cooldown_s: float = DEFAULT_COOLDOWN_S
    _buckets: dict[str, _Bucket] = field(default_factory=dict)
    _lock: RLock = field(default_factory=RLock)

    def check(self, key: str, *, now: float | None = None) -> float | None:
        """Pruefe ob ``key`` gerade gesperrt ist. Liefert verbleibende Sperr-Sekunden.

        Returns:
            ``None`` wenn der Key nicht gesperrt ist.
            ``float`` mit Sekunden bis zur Wiederfreischaltung sonst.
        """
        t = time.monotonic() if now is None else now
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                return None
            self._prune(bucket, t)
            if bucket.locked_until > t:
                return bucket.locked_until - t
            return None

    def register_failure(self, key: str, *, now: float | None = None) -> float | None:
        """Vermerkt einen Fehlversuch. Liefert Sperr-Sekunden wenn jetzt geschlossen wird."""
        t = time.monotonic() if now is None else now
        with self._lock:
            bucket = self._buckets.setdefault(key, _Bucket())
            self._prune(bucket, t)
            bucket.failures.append(t)
            if len(bucket.failures) >= self.max_attempts:
                bucket.locked_until = t + self.cooldown_s
                return self.cooldown_s
            return None

    def register_success(self, key: str) -> None:
        """Setzt den Counter zurueck — legitimes Login soll spaeteres Tippeln nicht bestrafen."""
        with self._lock:
            self._buckets.pop(key, None)

    def reset(self) -> None:
        """Loescht alle Buckets. Fuer Tests."""
        with self._lock:
            self._buckets.clear()

    # ----- Internals -----

    def _prune(self, bucket: _Bucket, now: float) -> None:
        threshold = now - self.window_s
        while bucket.failures and bucket.failures[0] < threshold:
            bucket.failures.popleft()


__all__ = [
    "DEFAULT_COOLDOWN_S",
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_WINDOW_S",
    "RateLimiter",
]
