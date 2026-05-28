"""Fehlertypen für das Tresor-Subsystem.

Vererben von ``OpnCockpitError``, damit die Orchestrierung Tresor-Fehler im
selben ``try/except``-Block fangen kann wie Transport-Fehler. Jeder Typ hat
eine deutlich unterschiedliche Ursache (R-NFR-3).
"""

from __future__ import annotations

from opn_cockpit.core.errors import OpnCockpitError


class VaultError(OpnCockpitError):
    """Basisklasse aller Tresor-bezogenen Fehler."""

    default_kind = "vault"


class InvalidPasswordError(VaultError):
    """Master-Passwort ist falsch oder die Datei wurde nach dem Schreiben manipuliert.

    Beide Ursachen sind aus Sicht des AES-GCM-Auth-Tags ununterscheidbar — wir
    geben absichtlich keine Hinweise, welcher der beiden Fälle vorliegt
    (verhindert Side-Channel-Auswertung durch einen Angreifer).
    """

    default_kind = "vault_invalid_password"


class CorruptVaultError(VaultError):
    """Datei strukturell defekt: falsche Magic-Bytes, zu kurz oder Header unparsbar."""

    default_kind = "vault_corrupt"


class VaultVersionError(VaultError):
    """Datei wurde mit einer inkompatiblen Format-Version geschrieben."""

    default_kind = "vault_version"


class VaultIOError(VaultError):
    """Datei nicht lesbar/schreibbar (Pfad-Konflikt, Berechtigung, Disk-Fehler)."""

    default_kind = "vault_io"


class WeakPasswordError(VaultError):
    """Master-Passwort erfüllt die Mindest-Anforderungen nicht (siehe ``MIN_PASSWORD_LENGTH``)."""

    default_kind = "vault_weak_password"


class UnknownDeviceError(VaultError):
    """Im offenen Tresor wurde kein Gerät mit der angefragten ID gefunden."""

    default_kind = "vault_unknown_device"


class SessionLockedError(VaultError):
    """Operation verlangt einen entsperrten Tresor, die Session ist aber gesperrt."""

    default_kind = "vault_session_locked"
