"""Lese-Sicht auf ein Gerät — ohne Secret-Felder.

``Device`` ist die Form, in der die Orchestrierung und die GUI Geräte
sehen. Die API-Credentials (``api_key`` / ``api_secret``) bleiben im
``VaultDevice`` (siehe :mod:`opn_cockpit.vault.model`) und werden NIEMALS
in eine ``Device``-Instanz übernommen. So kann ein versehentliches
Serialisieren von Geräten in die Vorschau, ins Audit-Log oder in einen
Crash-Report nie Klartext-Secrets preisgeben.
"""

from __future__ import annotations

from dataclasses import dataclass

from opn_cockpit.vault.model import VaultDevice


@dataclass(frozen=True, slots=True)
class Device:
    """Read-only Sicht eines Geräts für UI/Orchestrierung."""

    id: str
    name: str
    host: str
    port: int
    tls_verify: bool
    tags: tuple[str, ...]
    descr: str

    @classmethod
    def from_vault_device(cls, vd: VaultDevice) -> Device:
        return cls(
            id=vd.id,
            name=vd.name,
            host=vd.host,
            port=vd.port,
            tls_verify=vd.tls_verify,
            tags=tuple(vd.tags),
            descr=vd.descr,
        )

    @property
    def display_label(self) -> str:
        """Bequemer Label-String für Listendarstellung in der GUI."""
        return f"{self.name} ({self.host}:{self.port})"
