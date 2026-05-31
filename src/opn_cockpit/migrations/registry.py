"""Migrations-Registry — Liste aller Migrationen + Kontext-Objekt.

Konvention fuer IDs: ``YYYY-MM-DD-NNN-kurzbeschreibung`` (Datum +
laufende Nummer am Tag + Kurzname). Die ID ist die Identitaet einer
Migration ueber alle Releases hinweg — wird sie umbenannt, gilt sie
als neue Migration und wird erneut angewandt.

Eine Migration muss:

* idempotent sein (mehrfacher ``up()``-Aufruf darf nicht schaden)
* nur die Datenstrukturen erweitern, die sie kennt — keine "Aufraeum"-
  Aktionen, die spaetere Features brechen koennten
* Fehler als ``MigrationError`` werfen, damit der Runner sauber abbricht
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from opn_cockpit.config import AppSettings


@dataclass(frozen=True, slots=True)
class MigrationContext:
    """Was eine Migration vom Runner bekommt.

    Bewusst minimal — ``app_data_dir`` reicht heute fuer Datei- und
    SQLite-basierte Migrationen. ``settings`` wird mitgegeben, damit
    Migrationen die aktive Konfiguration sehen (z. B. Default-Vault-Pfad)
    ohne sie selbst zu laden.
    """

    app_data_dir: Path
    settings: AppSettings


@dataclass(frozen=True, slots=True)
class Migration:
    """Definition einer einzelnen Migration."""

    id: str
    description: str
    up: Callable[[MigrationContext], None]


# Registry — heute leer, weil v0.6 das Framework neu einfuehrt und alle
# Schemas dieselben sind wie in v0.5 (= "v5" im Roadmap-Sprech). Neue
# Eintraege werden hier am Ende angehaengt; die Reihenfolge der Liste
# bestimmt die Anwendungsreihenfolge bei Neu-Installationen.
MIGRATIONS: list[Migration] = []


__all__ = ["MIGRATIONS", "Migration", "MigrationContext"]
