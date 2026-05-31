"""Fehlerklassen fuer das Migrations-Framework."""

from __future__ import annotations


class MigrationError(Exception):
    """Fehler waehrend einer Migration. Boot soll abbrechen, Backup bleibt liegen."""


__all__ = ["MigrationError"]
