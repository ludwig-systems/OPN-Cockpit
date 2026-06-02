"""Fehlertypen des Backup-Store-Layers.

Bewusst klein gehalten: der Storage hat zwei Fehlerklassen — entweder
Schreib-/Lese-Probleme (``BackupStoreError``) oder nicht-gefundene IDs
(``BackupNotFoundError``). Hoehere Schichten (Service, API) wrappen das
ggf. in HTTP-Statuscodes.
"""

from __future__ import annotations


class BackupStoreError(Exception):
    """Backup konnte nicht geschrieben/gelesen werden — IO-Layer-Problem."""


class BackupNotFoundError(BackupStoreError):
    """Eine angeforderte Backup-ID existiert (mehr) nicht im Store."""


__all__ = ["BackupNotFoundError", "BackupStoreError"]
