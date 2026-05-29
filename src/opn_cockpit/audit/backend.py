"""Audit-Backend-Interface + Factory.

Heute existiert genau eine Implementierung: ``AuditLog`` in
:mod:`opn_cockpit.audit.log` (File-basiert, JSON-Lines). In v3 kommt ein
SQL-basiertes Backend dazu, ohne dass die Aufrufer (web/api/...,
cli/main.py) eine Zeile aendern muessen — sie reden nur mit dem Protocol
hier und holen die konkrete Instanz ueber :func:`get_audit_backend`.

Auswahl welches Backend aktiv ist passiert ueber
``AppSettings.storage_backend`` (heute fix auf ``"filesystem"`` ⇒
``AuditLog``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from opn_cockpit.audit.log import AuditLog, default_audit_path

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

    Heute: immer ``AuditLog`` mit dem Standard-Pfad. Wird in v3 ein
    SqlAuditBackend gebaut, schaltet diese Factory anhand der App-Settings
    um — Aufrufer aendern sich nicht.

    log.py importiert backend.py nicht, also keine Circular-Imports.
    """
    return AuditLog(path=default_audit_path())


__all__ = ["AuditBackend", "get_audit_backend"]
