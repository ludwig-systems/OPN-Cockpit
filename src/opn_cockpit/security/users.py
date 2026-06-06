"""User-Datenbank fuer den Multi-User-Server-Modus (v3.0).

SQLite-basiert, Argon2id-Passwort-Hashing. Roles: ``viewer`` (read-only),
``operator`` (plan+apply), ``admin`` (User-Management + alles).

Allowed-Tags optional: ein User kann auf eine Tag-Whitelist eingeschraenkt
werden, sodass er nur Geraete mit diesen Tags im Inventar sieht.

Im Single-User-Modus wird dieses Modul nicht verwendet — dort entsperrt das
Master-Passwort den Vault direkt (siehe :mod:`opn_cockpit.web.auth.manager`
und ``VaultAuthBackend``).
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Literal

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from opn_cockpit.config import get_app_data_dir

USERS_FILENAME = "users.db"
MIN_PASSWORD_LENGTH = 12

Role = Literal["viewer", "operator", "admin"]
VALID_ROLES: tuple[Role, ...] = ("viewer", "operator", "admin")


class UserStoreError(Exception):
    """Fehler in der User-Verwaltung — z. B. doppelter Username."""


@dataclass(frozen=True, slots=True)
class User:
    """User-Eintrag (ohne Passwort-Hash).

    ``must_change_password`` wird gesetzt wenn der User mit einem Default-
    oder Initial-Passwort angelegt wurde (z. B. der Default-Admin
    `admin` / `OPN-Cockpit!`). Solange das Flag steht, sperrt der Web-
    Layer alle Aktionen ausser dem Self-Service-Passwort-Wechsel.

    ``totp_enabled`` ist True, sobald der User die TOTP-Einrichtung
    abgeschlossen hat (Secret + Bestaetigung mit aktuellem Code). Wird
    Login erzwingt dann den 2-Schritt-Flow.
    """

    id: int
    username: str
    role: Role
    allowed_tags: tuple[str, ...]  # leer = alle
    created_at_iso: str
    last_login_at_iso: str | None
    disabled: bool
    must_change_password: bool = False
    totp_enabled: bool = False


def default_users_db_path() -> Path:
    return get_app_data_dir() / USERS_FILENAME


@dataclass(slots=True)
class UserStore:
    """Persistente User-Verwaltung in SQLite.

    Thread-safe via internem ``RLock`` — sqlite3 mit ``check_same_thread=False``
    waere die Alternative, aber RLock ist robuster gegen versehentliche
    Race-Conditions in den UPSERT-Pfaden.
    """

    path: Path
    _lock: RLock = None  # type: ignore[assignment]
    _conn: sqlite3.Connection = None  # type: ignore[assignment]
    _hasher: PasswordHasher = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._hasher = PasswordHasher()
        self._init_schema()

    # ----- Schema -----

    def _init_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL CHECK (role IN ('viewer', 'operator', 'admin')),
                    allowed_tags TEXT NOT NULL DEFAULT '',
                    created_at_iso TEXT NOT NULL,
                    last_login_at_iso TEXT,
                    disabled INTEGER NOT NULL DEFAULT 0
                );
            """)
            # Migration: must_change_password fuer bestehende DBs nachziehen.
            # SQLite hat kein IF NOT EXISTS fuer ADD COLUMN, daher Probe.
            cols = {row[1] for row in self._conn.execute("PRAGMA table_info(users)")}
            if "must_change_password" not in cols:
                self._conn.execute(
                    "ALTER TABLE users ADD COLUMN must_change_password "
                    "INTEGER NOT NULL DEFAULT 0"
                )
            # TOTP-Felder (v0.8). totp_secret bleibt leer bis Enrollment.
            # totp_backup_codes_json haelt eine JSON-Liste von SHA-256-
            # Hashes; jeder verbrauchte Backup-Code wird daraus geloescht.
            if "totp_secret" not in cols:
                self._conn.execute(
                    "ALTER TABLE users ADD COLUMN totp_secret TEXT NOT NULL DEFAULT ''",
                )
            if "totp_enabled" not in cols:
                self._conn.execute(
                    "ALTER TABLE users ADD COLUMN totp_enabled INTEGER NOT NULL DEFAULT 0",
                )
            if "totp_backup_codes_json" not in cols:
                self._conn.execute(
                    "ALTER TABLE users ADD COLUMN totp_backup_codes_json "
                    "TEXT NOT NULL DEFAULT '[]'",
                )

    # ----- CRUD -----

    def create_user(
        self,
        *,
        username: str,
        password: str,
        role: Role,
        allowed_tags: tuple[str, ...] = (),
        must_change_password: bool = False,
    ) -> User:
        """Legt einen neuen User an. Wirft ``UserStoreError`` bei Dubletten.

        ``must_change_password=True`` markiert den User als
        "Default-Passwort, muss beim ersten Login wechseln". Wird vom
        Default-Admin-Bootstrap genutzt (siehe ServerState).
        """
        username = username.strip()
        if not username:
            raise UserStoreError("Username darf nicht leer sein.")
        if len(password) < MIN_PASSWORD_LENGTH:
            raise UserStoreError(
                f"Passwort muss mindestens {MIN_PASSWORD_LENGTH} Zeichen lang sein.",
            )
        if role not in VALID_ROLES:
            raise UserStoreError(f"Ungueltige Rolle: {role}")
        password_hash = self._hasher.hash(password)
        now = _now_iso()
        tags_str = ",".join(t.strip() for t in allowed_tags if t.strip())
        mcp = 1 if must_change_password else 0
        try:
            with self._lock, self._conn:
                cursor = self._conn.execute(
                    "INSERT INTO users (username, password_hash, role, allowed_tags, "
                    "created_at_iso, must_change_password) VALUES (?, ?, ?, ?, ?, ?)",
                    (username, password_hash, role, tags_str, now, mcp),
                )
                user_id = cursor.lastrowid
        except sqlite3.IntegrityError as exc:
            raise UserStoreError(f"Username '{username}' existiert bereits.") from exc
        assert user_id is not None
        return User(
            id=user_id,
            username=username,
            role=role,
            allowed_tags=tuple(t for t in tags_str.split(",") if t),
            created_at_iso=now,
            last_login_at_iso=None,
            disabled=False,
            must_change_password=must_change_password,
        )

    def authenticate(self, username: str, password: str) -> User | None:
        """Verifiziert Username + Passwort. Liefert User bei Erfolg, sonst None.

        Updated ``last_login_at_iso`` bei Erfolg. Bei deaktivierten Usern
        verweigert der Login auch bei korrektem Passwort.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM users WHERE username = ?",
                (username.strip(),),
            ).fetchone()
        if row is None:
            return None
        if row["disabled"]:
            return None
        try:
            self._hasher.verify(row["password_hash"], password)
        except VerifyMismatchError:
            return None
        # Update last_login_at — best-effort, kein Fehler nach aussen.
        now = _now_iso()
        try:
            with self._lock, self._conn:
                self._conn.execute(
                    "UPDATE users SET last_login_at_iso = ? WHERE id = ?",
                    (now, row["id"]),
                )
        except sqlite3.Error:
            pass
        return _row_to_user(row, last_login_override=now)

    def get_user(self, user_id: int) -> User | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM users WHERE id = ?", (user_id,),
            ).fetchone()
        return _row_to_user(row) if row else None

    def get_user_by_name(self, username: str) -> User | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM users WHERE username = ?", (username.strip(),),
            ).fetchone()
        return _row_to_user(row) if row else None

    def list_users(self) -> list[User]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM users ORDER BY username",
            ).fetchall()
        return [_row_to_user(r) for r in rows]

    def update_user(
        self,
        user_id: int,
        *,
        role: Role | None = None,
        allowed_tags: tuple[str, ...] | None = None,
        disabled: bool | None = None,
    ) -> User:
        """Updated Felder. Wirft ``UserStoreError`` wenn User nicht existiert."""
        if role is not None and role not in VALID_ROLES:
            raise UserStoreError(f"Ungueltige Rolle: {role}")
        sets: list[str] = []
        values: list[object] = []
        if role is not None:
            sets.append("role = ?")
            values.append(role)
        if allowed_tags is not None:
            sets.append("allowed_tags = ?")
            values.append(",".join(t.strip() for t in allowed_tags if t.strip()))
        if disabled is not None:
            sets.append("disabled = ?")
            values.append(1 if disabled else 0)
        if not sets:
            existing = self.get_user(user_id)
            if existing is None:
                raise UserStoreError(f"User-ID {user_id} nicht gefunden.")
            return existing
        values.append(user_id)
        with self._lock, self._conn:
            cursor = self._conn.execute(
                f"UPDATE users SET {', '.join(sets)} WHERE id = ?",
                values,
            )
        if cursor.rowcount == 0:
            raise UserStoreError(f"User-ID {user_id} nicht gefunden.")
        result = self.get_user(user_id)
        assert result is not None
        return result

    def change_password(self, user_id: int, new_password: str) -> None:
        """Aendert das Passwort und loescht ein etwaiges
        ``must_change_password``-Flag (User hat den Wechsel jetzt vollzogen).
        """
        if len(new_password) < MIN_PASSWORD_LENGTH:
            raise UserStoreError(
                f"Passwort muss mindestens {MIN_PASSWORD_LENGTH} Zeichen lang sein.",
            )
        password_hash = self._hasher.hash(new_password)
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE users SET password_hash = ?, must_change_password = 0 "
                "WHERE id = ?",
                (password_hash, user_id),
            )
        if cursor.rowcount == 0:
            raise UserStoreError(f"User-ID {user_id} nicht gefunden.")

    def delete_user(self, user_id: int) -> bool:
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "DELETE FROM users WHERE id = ?", (user_id,),
            )
        return cursor.rowcount > 0

    # ----- TOTP -----

    def get_totp_secret(self, user_id: int) -> str:
        """Liefert das aktuelle TOTP-Secret (leer wenn nicht gesetzt)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT totp_secret FROM users WHERE id = ?", (user_id,),
            ).fetchone()
        if row is None:
            return ""
        return str(row["totp_secret"] or "")

    def set_totp_secret(self, user_id: int, secret: str) -> None:
        """Schreibt das TOTP-Secret (ohne ``totp_enabled`` zu setzen).

        Wird beim Enrollment-Start aufgerufen. Erst wenn der User den ersten
        Code bestaetigt, wird via :meth:`enable_totp` das Flag gesetzt.
        Solange ``totp_enabled=0`` ist, geht der Login wie bisher ohne 2FA.
        """
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE users SET totp_secret = ? WHERE id = ?",
                (secret, user_id),
            )
        if cursor.rowcount == 0:
            raise UserStoreError(f"User-ID {user_id} nicht gefunden.")

    def enable_totp(self, user_id: int, backup_code_hashes: list[str]) -> None:
        """Setzt ``totp_enabled=1`` + speichert die Backup-Code-Hashes.

        Wird nach erfolgreicher Code-Bestaetigung im Enrollment-Flow
        aufgerufen. Backup-Codes liegen als SHA-256-Hashes in einer
        JSON-Liste.
        """
        payload = json.dumps(backup_code_hashes)
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE users SET totp_enabled = 1, totp_backup_codes_json = ? "
                "WHERE id = ?",
                (payload, user_id),
            )
        if cursor.rowcount == 0:
            raise UserStoreError(f"User-ID {user_id} nicht gefunden.")

    def disable_totp(self, user_id: int) -> None:
        """Deaktiviert TOTP komplett: Secret + Flag + Backup-Codes weg."""
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE users SET totp_secret = '', totp_enabled = 0, "
                "totp_backup_codes_json = '[]' WHERE id = ?",
                (user_id,),
            )
        if cursor.rowcount == 0:
            raise UserStoreError(f"User-ID {user_id} nicht gefunden.")

    def get_backup_code_hashes(self, user_id: int) -> list[str]:
        """Liefert die aktuelle Liste der Backup-Code-Hashes."""
        with self._lock:
            row = self._conn.execute(
                "SELECT totp_backup_codes_json FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
        if row is None:
            return []
        raw = row["totp_backup_codes_json"] or "[]"
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return []
        if not isinstance(parsed, list):
            return []
        return [str(h) for h in parsed if isinstance(h, str)]

    def set_backup_code_hashes(self, user_id: int, hashes: list[str]) -> None:
        """Persistiert die aktualisierte Backup-Code-Liste.

        Wird vom Login-Flow aufgerufen, wenn ein Backup-Code verbraucht
        wurde (Liste hat ein Element weniger).
        """
        payload = json.dumps(hashes)
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE users SET totp_backup_codes_json = ? WHERE id = ?",
                (payload, user_id),
            )
        if cursor.rowcount == 0:
            raise UserStoreError(f"User-ID {user_id} nicht gefunden.")

    def count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()
        return int(row["n"])

    def close(self) -> None:
        with self._lock:
            self._conn.close()


# ---------------------------------------------------------------------------
# Helfer
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _row_to_user(row: sqlite3.Row, *, last_login_override: str | None = None) -> User:
    tags_str = row["allowed_tags"] or ""
    # must_change_password kann fehlen wenn die DB von vor dem Schema-Update
    # kommt — _init_schema-Migration zieht es im naechsten Open nach.
    mcp = False
    with contextlib.suppress(IndexError, KeyError):
        mcp = bool(row["must_change_password"])
    totp_enabled = False
    with contextlib.suppress(IndexError, KeyError):
        totp_enabled = bool(row["totp_enabled"])
    return User(
        id=row["id"],
        username=row["username"],
        role=row["role"],
        allowed_tags=tuple(t for t in tags_str.split(",") if t),
        created_at_iso=row["created_at_iso"],
        last_login_at_iso=last_login_override or row["last_login_at_iso"],
        disabled=bool(row["disabled"]),
        must_change_password=mcp,
        totp_enabled=totp_enabled,
    )


__all__ = [
    "MIN_PASSWORD_LENGTH",
    "USERS_FILENAME",
    "VALID_ROLES",
    "Role",
    "User",
    "UserStore",
    "UserStoreError",
    "default_users_db_path",
]
