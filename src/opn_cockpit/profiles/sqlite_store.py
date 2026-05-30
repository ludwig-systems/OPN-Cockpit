"""SQLite-Implementierung des ProfileStoreBackend (v3.1).

Profile als Zeilen mit JSON-Spec. Eindeutigkeit von ``name`` per
UNIQUE-Constraint; ``generate_profile_id`` aus dem File-Store wird
weiterverwendet, damit IDs zwischen den Backends konsistent bleiben.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from opn_cockpit.profiles.store import (
    Profile,
    ProfileStoreError,
    _sanitize_spec,
    generate_profile_id,
)
from opn_cockpit.storage.sqlite_db import SqliteDb

_SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles (
    id TEXT PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    action TEXT NOT NULL,
    subsystem TEXT NOT NULL,
    default_selector TEXT NOT NULL DEFAULT 'all',
    spec_json TEXT NOT NULL
);
"""


@dataclass(slots=True)
class SqliteProfileStore:
    """SQLite-Profile-Store. API-kompatibel zu ``ProfileStore``."""

    db: SqliteDb

    def __post_init__(self) -> None:
        self.db.executescript(_SCHEMA)

    def list_profiles(self) -> list[Profile]:
        with self.db.cursor() as cur:
            rows = cur.execute(
                "SELECT * FROM profiles ORDER BY name ASC",
            ).fetchall()
        return [_row_to_profile(r) for r in rows]

    def get(self, profile_id: str) -> Profile:
        with self.db.cursor() as cur:
            row = cur.execute(
                "SELECT * FROM profiles WHERE id = ?", (profile_id,),
            ).fetchone()
        if row is None:
            raise ProfileStoreError(f"Profil-ID nicht gefunden: {profile_id}")
        return _row_to_profile(row)

    def find_by_name(self, name: str) -> Profile | None:
        with self.db.cursor() as cur:
            row = cur.execute(
                "SELECT * FROM profiles WHERE name = ?", (name,),
            ).fetchone()
        return _row_to_profile(row) if row else None

    def save_new(
        self,
        *,
        name: str,
        action: str,
        subsystem: str,
        default_selector: str,
        spec: dict[str, Any],
    ) -> Profile:
        if not name.strip():
            raise ProfileStoreError("Profil-Name darf nicht leer sein.")
        cleaned_spec = _sanitize_spec(spec)
        profile = Profile(
            id=generate_profile_id(),
            name=name.strip(),
            action=action,
            subsystem=subsystem,
            default_selector=default_selector,
            spec=cleaned_spec,
        )
        try:
            with self.db.transaction() as conn:
                conn.execute(
                    "INSERT INTO profiles (id, name, action, subsystem, "
                    "default_selector, spec_json) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        profile.id,
                        profile.name,
                        profile.action,
                        profile.subsystem,
                        profile.default_selector,
                        json.dumps(profile.spec, ensure_ascii=False),
                    ),
                )
        except Exception as exc:
            msg = str(exc).lower()
            if "unique" in msg and "name" in msg:
                raise ProfileStoreError(
                    f"Profil-Name existiert bereits: {name!r}",
                ) from exc
            raise
        return profile

    def delete(self, profile_id: str) -> bool:
        with self.db.transaction() as conn:
            cur = conn.execute(
                "DELETE FROM profiles WHERE id = ?", (profile_id,),
            )
        return cur.rowcount > 0


def _row_to_profile(row: Any) -> Profile:
    try:
        spec = json.loads(row["spec_json"])
    except json.JSONDecodeError:
        spec = {}
    if not isinstance(spec, dict):
        spec = {}
    return Profile(
        id=str(row["id"]),
        name=str(row["name"]),
        action=str(row["action"]),
        subsystem=str(row["subsystem"]),
        default_selector=str(row["default_selector"]),
        spec=_sanitize_spec(spec),
    )


__all__ = ["SqliteProfileStore"]
