"""Tresor-Datei-Format: Layout, Magic, Header-Serialisierung.

```
Offset  Size  Field
0       8     Magic "OPNVAULT"
8       2     Version (uint16 LE) — aktuell 1
10      2     Reserviert (= 0)
12      16    KDF-Salt
28      4     KDF time_cost (uint32 LE)
32      4     KDF memory_cost in KiB (uint32 LE)
36      1     KDF parallelism (uint8)
37      3     Reserviert (Nullen)
40      12    AES-GCM-Nonce
52+     ...   Ciphertext + 16-Byte GCM-Tag (am Ende)
```

Gesamtgröße Header: ``HEADER_SIZE`` Bytes. Der Header (alle 52 Bytes) ist
zugleich Additional Authenticated Data (AAD) für AES-GCM — Manipulation an
KDF-Parametern oder Magic lässt die Entschlüsselung fehlschlagen.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from opn_cockpit.vault.errors import CorruptVaultError, VaultVersionError

# ---------------------------------------------------------------------------
# Konstanten
# ---------------------------------------------------------------------------

HEADER_MAGIC: bytes = b"OPNVAULT"
HEADER_MAGIC_SIZE: int = len(HEADER_MAGIC)
HEADER_VERSION: int = 1
HEADER_SIZE: int = 52
SALT_SIZE: int = 16
NONCE_SIZE: int = 12
GCM_TAG_SIZE: int = 16

# Struct-Format für den Anteil hinter der Magic.
# < = little-endian, H = uint16, B = uint8, I = uint32
_HEADER_TAIL_FORMAT = "<HH 16s I I B 3x 12s"
_HEADER_TAIL_SIZE = struct.calcsize(_HEADER_TAIL_FORMAT)
assert HEADER_MAGIC_SIZE + _HEADER_TAIL_SIZE == HEADER_SIZE


@dataclass(frozen=True, slots=True)
class VaultHeader:
    """Parsbarer Header einer Tresor-Datei.

    Wird beim Erzeugen einer neuen Datei aufgebaut und beim Öffnen aus den
    ersten 52 Bytes der Datei rekonstruiert.
    """

    version: int
    kdf_salt: bytes
    kdf_time_cost: int
    kdf_memory_cost_kib: int
    kdf_parallelism: int
    nonce: bytes

    def __post_init__(self) -> None:
        if len(self.kdf_salt) != SALT_SIZE:
            raise CorruptVaultError(
                f"KDF-Salt muss {SALT_SIZE} Bytes lang sein, ist aber {len(self.kdf_salt)}."
            )
        if len(self.nonce) != NONCE_SIZE:
            raise CorruptVaultError(
                f"Nonce muss {NONCE_SIZE} Bytes lang sein, ist aber {len(self.nonce)}."
            )

    def pack(self) -> bytes:
        """Serialisiert den Header in exakt ``HEADER_SIZE`` Bytes."""
        tail = struct.pack(
            _HEADER_TAIL_FORMAT,
            self.version,
            0,  # reserviert
            self.kdf_salt,
            self.kdf_time_cost,
            self.kdf_memory_cost_kib,
            self.kdf_parallelism,
            self.nonce,
        )
        return HEADER_MAGIC + tail

    @classmethod
    def unpack(cls, raw: bytes) -> VaultHeader:
        """Parst ``raw`` (mind. ``HEADER_SIZE`` Bytes) zu einem Header.

        Wirft ``CorruptVaultError`` bei zu kurzem Input oder falscher Magic
        und ``VaultVersionError`` bei unbekannter Format-Version.
        """
        if len(raw) < HEADER_SIZE:
            raise CorruptVaultError(
                f"Tresor-Datei zu kurz ({len(raw)} < {HEADER_SIZE} Bytes)."
            )
        magic = raw[:HEADER_MAGIC_SIZE]
        if magic != HEADER_MAGIC:
            raise CorruptVaultError(
                "Falsche Magic-Bytes — Datei ist kein OPN-Cockpit-Tresor."
            )
        try:
            (
                version,
                _reserved,
                kdf_salt,
                kdf_time_cost,
                kdf_memory_cost_kib,
                kdf_parallelism,
                nonce,
            ) = struct.unpack(
                _HEADER_TAIL_FORMAT,
                raw[HEADER_MAGIC_SIZE:HEADER_SIZE],
            )
        except struct.error as exc:
            raise CorruptVaultError(
                f"Tresor-Header nicht parsbar: {exc}"
            ) from exc

        if version != HEADER_VERSION:
            raise VaultVersionError(
                f"Tresor-Datei hat Version {version}, unterstützt wird {HEADER_VERSION}."
            )

        return cls(
            version=version,
            kdf_salt=kdf_salt,
            kdf_time_cost=kdf_time_cost,
            kdf_memory_cost_kib=kdf_memory_cost_kib,
            kdf_parallelism=kdf_parallelism,
            nonce=nonce,
        )
