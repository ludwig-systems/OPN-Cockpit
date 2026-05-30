"""Audit-Backend-Interface + Factory.

Zwei Implementierungen:

* ``AuditLog`` (File-basiert, JSON-Lines) — Default
* ``SqliteAuditBackend`` (v3.1) — wenn ``AppSettings.storage_backend ==
  "sqlite"`` (z. B. via ``OPNCOCKPIT_STORAGE_BACKEND=sqlite``)

Aufrufer reden nur mit dem Protocol hier und holen die konkrete
Instanz ueber :func:`get_audit_backend`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from opn_cockpit.audit.log import AuditLog, default_audit_path
from opn_cockpit.audit.sqlite_backend import SqliteAuditBackend
from opn_cockpit.config import AppSettings
from opn_cockpit.storage.sqlite_db import SqliteDb, default_db_path

if TYPE_CHECKING:
    from opn_cockpit.audit.log import AuditEventKind, AuditRecord


@runtime_checkable
class AuditBackend(Protocol):
    """Pflichtschnittstelle aller Audit-Backends.

    Aufrufer-Vertrag:

    * ``append`` ist die einzige Schreib-Schnittstelle. Nimmt nur Felder
      aus der ``AuditRecord``-Whitelist; alles andere wirft.
    * ``read_all`` liefert chronologisch geordnete Eintraege.
    * ``filter`` filtert nach allen Kombinationen aus event/action/
      device_id/actor/zeitfenster.

    Backends MUESSEN Thread-safe gegenueber parallelen ``append``-Calls
    sein — das ist v2 schon (File-Append ist atomic genug, SQL wird per
    Connection transactional handhaben).
    """

    def append(self, event: AuditEventKind, /, **fields_in: Any) -> AuditRecord:
        ...

    def read_all(self) -> list[AuditRecord]:
        ...

    def filter(
        self,
        *,
        event: AuditEventKind | None = None,
        action: str | None = None,
        target_device_id: str | None = None,
        actor: str | None = None,
        since_iso: str | None = None,
        until_iso: str | None = None,
    ) -> list[AuditRecord]:
        ...


def get_audit_backend() -> AuditBackend:
    """Liefert das aktuell konfigurierte Audit-Backend.

    File-Default oder SQLite je nach ``AppSettings.storage_backend``.
    Die DB-Verbindung wird einmalig pro Prozess gecached, damit alle
    Aufrufer dieselbe Connection teilen (WAL-Optimierung + weniger
    File-Handles).
    """
    settings = AppSettings.load()
    if settings.storage_backend == "sqlite":
        return SqliteAuditBackend(db=_shared_db())
    return AuditLog(path=default_audit_path())


class _DbCache:
    """Container fuer die prozess-weite SqliteDb-Instanz.

    Statt eines `global`-Statements halten wir den Slot in einem Modul-
    Singleton-Objekt — semantisch dasselbe, aber ohne ruff-PLW0603-Warnung.
    """

    instance: SqliteDb | None = None


def _shared_db() -> SqliteDb:
    """Prozess-weite SqliteDb-Instanz (Lazy-Init).

    Wird auch von den Plan- und Profile-SQL-Backends genutzt — sie teilen
    sich eine einzige Datei ``opn-cockpit.db``.
    """
    if _DbCache.instance is None:
        _DbCache.instance = SqliteDb(path=default_db_path())
    return _DbCache.instance


def reset_db_cache() -> None:
    """Schliesst die geteilte DB-Connection — fuer Tests / Shutdown."""
    if _DbCache.instance is not None:
        _DbCache.instance.close()
        _DbCache.instance = None


def audit_actor(session: object | None) -> str | None:
    """Liefert den Audit-Actor fuer einen Web-Request.

    Im Multi-User-Mode steht der eingeloggte Username im Log statt des
    OS-Users — sonst sieht der Audit-Reviewer nur ``LocalService`` oder
    ``opncockpit``. Im Single-Mode geben wir ``None`` zurueck, dann
    behaelt der Backend seinen Default-Actor.

    Argument ist locker getypt, damit dieses Modul keine Web-Imports
    braucht (kein Circular-Import).
    """
    if session is None:
        return None
    user = getattr(session, "user", None)
    if user is None:
        return None
    username = getattr(user, "username", None)
    return str(username) if username else None


__all__ = [
    "AuditBackend",
    "audit_actor",
    "get_audit_backend",
    "reset_db_cache",
]
