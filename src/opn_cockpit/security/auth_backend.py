"""Authentifizierungs-Backend: trennt Vault-Master-PW (v2) von User-DB (v3).

Heute aktiv: ``VaultAuthBackend``. User entsperrt einen Vault per Master-
Passwort, bekommt eine Session zurueck. Single-User-Modell.

Spaeter (v3.0 Multi-User-Server): ``UserDbAuthBackend``. Der Server hat
einen einzelnen entsperrten Vault im Memory (vom Server-Admin beim Boot
oder per Bootstrap-UI geliefert). User loggen sich per Username +
Passwort gegen die SQLite-User-DB ein und teilen sich diesen entsperrten
Vault.

Welches Backend aktiv ist, entscheidet ``AppSettings.auth_backend``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from opn_cockpit.security.session import Session
from opn_cockpit.security.users import User, UserStore
from opn_cockpit.vault.errors import InvalidPasswordError
from opn_cockpit.vault.store import OpenedVault, open_vault


@dataclass(frozen=True, slots=True)
class AuthResult:
    """Ergebnis eines erfolgreichen Logins.

    ``user`` ist None im Single-User-Modus (kein User-DB-Eintrag).
    ``opened_vault`` traegt die entsperrten Tresor-Daten. ``master_password``
    wird in der Session gecached, damit save_vault ohne Re-Prompt geht.
    """

    opened_vault: OpenedVault
    vault_path: Path
    master_password: str
    user: User | None


@runtime_checkable
class AuthBackend(Protocol):
    """Pflichtschnittstelle aller Auth-Backends."""

    def authenticate(self, credentials: dict[str, str]) -> AuthResult | None:
        """Validiert Credentials. Liefert ``AuthResult`` bei Erfolg, sonst None.

        Bei korrektem Passwort + nicht-deaktiviertem User: Erfolg.
        Bei falschem Passwort, unbekanntem User, deaktiviertem User: None.
        Bei strukturellen Fehlern (Vault-Datei fehlt, DB nicht erreichbar):
        wirft eine Exception.
        """
        ...


# ---------------------------------------------------------------------------
# VaultAuthBackend (v2 default, Single-User)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class VaultAuthBackend:
    """Single-User-Auth: Master-Passwort entsperrt direkt einen Vault.

    Credentials-Schema: ``{"vault_path": str, "password": str}``.
    """

    def authenticate(self, credentials: dict[str, str]) -> AuthResult | None:
        vault_path_raw = credentials.get("vault_path", "")
        password = credentials.get("password", "")
        if not vault_path_raw or not password:
            return None
        path = Path(vault_path_raw)
        if not path.exists():
            return None
        try:
            opened = open_vault(path, password)
        except InvalidPasswordError:
            return None
        return AuthResult(
            opened_vault=opened,
            vault_path=path,
            master_password=password,
            user=None,
        )


# ---------------------------------------------------------------------------
# UserDbAuthBackend (v3 Multi-User)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class UserDbAuthBackend:
    """Multi-User-Auth: User+Passwort gegen DB, Vault ist serverseitig entsperrt.

    Credentials-Schema: ``{"username": str, "password": str}``.

    Der zentrale, schon entsperrte Vault wird beim Konstruktor uebergeben
    (typisch: vom Server-Bootstrap). Alle Sessions teilen sich diesen
    Vault — pro Session wird beim Login eine Session-Instanz erzeugt, die
    auf dasselbe ``OpenedVault`` zeigt.
    """

    user_store: UserStore
    opened_vault: OpenedVault
    vault_path: Path
    master_password: str  # vom Server-Bootstrap entgegengenommen

    def authenticate(self, credentials: dict[str, str]) -> AuthResult | None:
        username = credentials.get("username", "")
        password = credentials.get("password", "")
        if not username or not password:
            return None
        user = self.user_store.authenticate(username, password)
        if user is None:
            return None
        return AuthResult(
            opened_vault=self.opened_vault,
            vault_path=self.vault_path,
            master_password=self.master_password,
            user=user,
        )


def make_session(result: AuthResult) -> Session:
    """Helper: AuthResult -> Session. Wird vom SessionManager.create_from aufgerufen."""
    session = Session()
    session.unlock(
        result.opened_vault,
        result.vault_path,
        result.master_password,
        user=result.user,
    )
    return session


__all__ = [
    "AuthBackend",
    "AuthResult",
    "UserDbAuthBackend",
    "VaultAuthBackend",
    "make_session",
]
