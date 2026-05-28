"""Dünne Fassade über dem entsperrten Tresor.

Stellt das Geräte-Inventar als ``Device``-Liste bereit, ohne dass der
Aufrufer den ``VaultData`` direkt sieht. Mutationen (in Schritt 8 nötig:
add/update/delete) gehen über entsprechende Methoden, die das ``VaultData``
im Speicher anpassen — die Persistenz auf Platte ist Verantwortung des
Aufrufers via ``vault.store.save_vault``.
"""

from __future__ import annotations

from dataclasses import dataclass

from opn_cockpit.inventory.model import Device
from opn_cockpit.inventory.selectors import apply_selector
from opn_cockpit.security.session import Session
from opn_cockpit.vault.errors import UnknownDeviceError


@dataclass(slots=True)
class InventoryStore:
    """Read-Fassade über der ``Session``.

    Schreibende Operationen kommen mit Schritt 8 (GUI-Inventar-Verwaltung).
    Für v1-Schritt-4 reicht die Lesesicht plus Selektoren.
    """

    session: Session

    def list_devices(self) -> list[Device]:
        if self.session.is_locked:
            return []
        return [Device.from_vault_device(d) for d in self.session.opened.data.devices]

    def get_device(self, device_id: str) -> Device:
        for vd in self.session.opened.data.devices:
            if vd.id == device_id:
                return Device.from_vault_device(vd)
        raise UnknownDeviceError(f"Gerät mit ID '{device_id}' nicht im Tresor.")

    def select(self, selector: str) -> list[Device]:
        return apply_selector(self.list_devices(), selector)

    @property
    def tags(self) -> list[str]:
        """Alphabetische Liste aller im Inventar verwendeten Tags."""
        if self.session.is_locked:
            return []
        seen: set[str] = set()
        for d in self.session.opened.data.devices:
            for t in d.tags:
                seen.add(t)
        return sorted(seen)
