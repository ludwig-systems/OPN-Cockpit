"""Thread-safe Session-Token-Registry.

Architektonisch Multi-User-vorbereitet: Ein Token mappt auf eine
Session-Instanz; in v2.0 ist Token-Erzeugung ans erfolgreiche Vault-
Entsperren gekoppelt. Bei spaeterer User-DB kann derselbe Manager
auch User-Logins durchreichen, ohne dass das API-Schema bricht.

Token-Format: ``secrets.token_urlsafe(32)`` — kryptographisch sicher,
URL-safe, 43 Zeichen lang. Bearer-Header-tauglich.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock

from opn_cockpit.security.session import Session
from opn_cockpit.vault.store import OpenedVault

TOKEN_BYTES = 32


@dataclass(slots=True)
class _SessionEntry:
    session: Session
    vault_path: Path


@dataclass(slots=True)
class SessionManager:
    """Token -> Session-Mapping mit Auto-Expiry-Cleanup.

    Threadsafe via internem ``RLock``. Saemtliche oeffentlichen Methoden
    sind reentrant aus demselben Thread aufrufbar.
    """

    _sessions: dict[str, _SessionEntry] = field(default_factory=dict)
    _lock: RLock = field(default_factory=RLock)

    def create(
        self,
        opened: OpenedVault,
        vault_path: Path,
        password: str,
    ) -> tuple[str, Session]:
        """Erzeugt ein neues Token + Session-Eintrag fuer einen entsperrten Tresor.

        ``password`` wird in der Session gecached, damit Schreibvorgaenge
        (Add/Remove/Duplicate Device) ohne erneuten Prompt funktionieren.
        Der Cache lebt nur waehrend der Session und wird beim ``revoke``
        oder Auto-Lock geloescht.
        """
        with self._lock:
            token = secrets.token_urlsafe(TOKEN_BYTES)
            session = Session()
            session.unlock(opened, vault_path, password)
            self._sessions[token] = _SessionEntry(session=session, vault_path=vault_path)
            return token, session

    def get(self, token: str) -> Session | None:
        """Liefert die Session oder ``None`` bei abgelaufenem/unbekanntem Token.

        Auto-Cleanup: Wenn die Session in der Zwischenzeit per
        ``check_inactivity`` abgelaufen ist, wird der Eintrag entfernt
        und ``None`` zurueckgegeben.
        """
        with self._lock:
            entry = self._sessions.get(token)
            if entry is None:
                return None
            if entry.session.check_inactivity():
                # Inaktivitaet hat die Session schon zugemacht.
                self._sessions.pop(token, None)
                return None
            return entry.session

    def revoke(self, token: str) -> bool:
        """Sperrt die Session und entfernt das Token.

        Liefert ``True`` wenn das Token existierte und entfernt wurde,
        ``False`` wenn es schon weg war.
        """
        with self._lock:
            entry = self._sessions.pop(token, None)
            if entry is None:
                return False
            entry.session.lock()
            return True

    def vault_path_for(self, token: str) -> Path | None:
        """Liefert den Tresor-Pfad fuer ein bekanntes Token (auch wenn abgelaufen)."""
        with self._lock:
            entry = self._sessions.get(token)
            return entry.vault_path if entry else None

    def clear(self) -> None:
        """Sperrt alle Sessions. Fuer Tests und Server-Shutdown."""
        with self._lock:
            for entry in self._sessions.values():
                entry.session.lock()
            self._sessions.clear()

    def active_count(self) -> int:
        with self._lock:
            return len(self._sessions)
