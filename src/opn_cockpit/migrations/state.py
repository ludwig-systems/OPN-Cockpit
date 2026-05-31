"""Persistenter Migrations-Status (``migrations.json``).

Schema:

.. code-block:: json

    {
      "applied": [
        {"id": "2026-06-15-001-add-foo", "applied_at_iso": "...", "app_version": "0.6.0"}
      ],
      "last_app_version": "0.6.0"
    }

Speicherort liegt im AppData-Dir, damit ein Code-Upgrade (das nur
``%ProgramFiles%`` ueberschreibt) den Status nicht verliert.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from opn_cockpit.config import get_app_data_dir

STATE_FILENAME = "migrations.json"


def default_state_path() -> Path:
    return get_app_data_dir() / STATE_FILENAME


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(slots=True)
class AppliedMigration:
    """Eine bereits angewandte Migration."""

    id: str
    applied_at_iso: str
    app_version: str


@dataclass(slots=True)
class MigrationState:
    """Persistenter Zustand fuer das Migrations-Framework.

    ``applied`` ist eine Liste in Anwendungsreihenfolge — die Reihenfolge
    ist informativ, fuer die Uniqueness reicht die ID. ``last_app_version``
    wird bei jedem ``save()`` auf den aktuellen ``__version__`` gesetzt
    und erlaubt bei Bedarf Diagnose ("zwischen Boots ist ein Update
    passiert, obwohl keine Migration noetig war").
    """

    applied: list[AppliedMigration] = field(default_factory=list)
    last_app_version: str | None = None

    @property
    def applied_ids(self) -> frozenset[str]:
        return frozenset(m.id for m in self.applied)

    def mark_applied(self, migration_id: str, *, app_version: str) -> AppliedMigration:
        """Vermerkt eine erfolgreich angewandte Migration.

        Idempotent: ein zweiter Aufruf mit derselben ID wird ignoriert,
        damit ein abgebrochener Lauf nach Korrektur sicher neu starten
        kann ohne Dubletten zu erzeugen.
        """
        if migration_id in self.applied_ids:
            for m in self.applied:
                if m.id == migration_id:
                    return m
        entry = AppliedMigration(
            id=migration_id,
            applied_at_iso=_now_iso(),
            app_version=app_version,
        )
        self.applied.append(entry)
        return entry

    @classmethod
    def load(cls, path: Path | None = None) -> MigrationState:
        """Laedt den Zustand. Toleriert fehlende/kaputte Datei (=neuer Zustand)."""
        resolved = path or default_state_path()
        if not resolved.exists():
            return cls()
        try:
            raw = json.loads(resolved.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # Bewusst tolerant — beschaedigte migrations.json blockiert sonst
            # jeden Boot. Der naechste Migrations-Lauf wuerde ggf. Migrationen
            # erneut anwenden; deren ``up()``-Funktionen muessen idempotent
            # sein, deshalb ist das akzeptabel.
            return cls()
        if not isinstance(raw, dict):
            return cls()
        return cls._from_dict(raw)

    def save(self, path: Path | None = None) -> None:
        """Schreibt den Zustand atomar (write+rename)."""
        resolved = path or default_state_path()
        resolved.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "applied": [asdict(m) for m in self.applied],
            "last_app_version": self.last_app_version,
        }
        tmp = resolved.with_suffix(resolved.suffix + ".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, resolved)

    @classmethod
    def _from_dict(cls, raw: dict[str, Any]) -> MigrationState:
        applied_raw = raw.get("applied", [])
        applied: list[AppliedMigration] = []
        if isinstance(applied_raw, list):
            for entry in applied_raw:
                if not isinstance(entry, dict):
                    continue
                mid = entry.get("id")
                ts = entry.get("applied_at_iso")
                ver = entry.get("app_version")
                if not (isinstance(mid, str) and isinstance(ts, str) and isinstance(ver, str)):
                    continue
                applied.append(AppliedMigration(id=mid, applied_at_iso=ts, app_version=ver))
        last_ver = raw.get("last_app_version")
        last_str = last_ver if isinstance(last_ver, str) and last_ver else None
        return cls(applied=applied, last_app_version=last_str)


__all__ = [
    "STATE_FILENAME",
    "AppliedMigration",
    "MigrationState",
    "default_state_path",
]
