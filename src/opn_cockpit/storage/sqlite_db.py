"""Zentrale SQLite-Verbindung fuer alle SQL-Backends (v3.1).

Eine Single-File-DB (Default: ``$OPNCOCKPIT_DATA_DIR/opn-cockpit.db``)
beherbergt Audit-, Plan- und Profile-Tabellen. Vorteile gegenueber der
File-basierten Variante:

* Backups sind eine einzelne Datei
* Atomare Mehrtabellen-Transaktionen
* Schnelles Filtern fuer Audit (Index auf timestamp / event)

Die Backends bekommen einen geteilten ``SqliteDb``-Container per
Factory injiziert. ``check_same_thread=False``, alle Mutationen unter
einem RLock — sqlite3 selbst serialisiert Writes, der Lock vermeidet
nur seltene "database is locked"-Fehler bei sehr hoher Parallelitaet.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock

from opn_cockpit.config import get_app_data_dir

DB_FILENAME = "opn-cockpit.db"


def default_db_path() -> Path:
    return get_app_data_dir() / DB_FILENAME


@dataclass(slots=True)
class SqliteDb:
    """Geteilter SQLite-Connection-Container.

    Backends rufen ``with db.cursor() as cur:`` und ``with db.transaction()
    as conn:``. Lese-Operationen brauchen keine Transaktion explizit;
    sqlite3 verpackt sie implizit autocommit.
    """

    path: Path
    _conn: sqlite3.Connection = None  # type: ignore[assignment]
    _lock: RLock = field(default_factory=RLock)

    def __post_init__(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # WAL-Mode: mehrere Leser + ein Schreiber, deutlich besser bei
        # gemischten Workloads. Persistent (in der Datei abgelegt).
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        # Fremdschluessel an — sind in v3.1 noch nicht referentiell genutzt,
        # aber falls wir spaeter ein FK einbauen, ist die Pragma schon da.
        self._conn.execute("PRAGMA foreign_keys=ON")

    @contextmanager
    def cursor(self) -> Iterator[sqlite3.Cursor]:
        """Read-Kursor unter dem internen Lock."""
        with self._lock:
            cur = self._conn.cursor()
            try:
                yield cur
            finally:
                cur.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Mutations-Transaction. Auto-commit am Ende, Rollback bei Exception."""
        with self._lock, self._conn:
            yield self._conn

    def executescript(self, script: str) -> None:
        with self._lock, self._conn:
            self._conn.executescript(script)

    def close(self) -> None:
        with self._lock:
            self._conn.close()


__all__ = [
    "DB_FILENAME",
    "SqliteDb",
    "default_db_path",
]
