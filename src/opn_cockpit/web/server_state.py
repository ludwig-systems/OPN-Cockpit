"""Server-weiter Auth-Zustand (Multi-User-Fundament, v3.0).

Heute kennt der Web-Server zwei Modi:

* **Single-User** (``AppSettings.auth_backend == "vault"``, Default):
  Jede Browser-Sitzung entsperrt ihren eigenen Tresor per Master-PW.
  Sessions sind isoliert. ``save_vault`` laeuft per-Session. So
  funktioniert OPN-Cockpit seit v2.0.

* **Multi-User** (``AppSettings.auth_backend == "user-db"``):
  Der Server haelt **einen** entsperrten Vault zentral im Memory. User
  loggen sich gegen die SQLite-User-DB ein und teilen sich diesen Vault.
  ``save_vault`` muss zentralisiert sein, sonst kollidieren Sessions
  ueber den Vault-Header (Nonce / Header-Replace).

``ServerState`` kapselt diese Unterscheidung. Im Single-Mode ist der
``bootstrap_status`` permanent ``"single-user"`` und alle Bootstrap-
Endpunkte schlagen mit 409 fehl. Im Multi-Mode startet der Server in
einem definierten Lifecycle:

1. ``needs-admin`` — User-DB existiert noch nicht (oder leer); Admin muss
   sich per Bootstrap-UI anlegen.
2. ``needs-vault-unlock`` — Admin existiert, aber der zentrale Vault ist
   noch nicht entsperrt (Server-Neustart, kein Auto-Unlock).
3. ``ready`` — User koennen sich einloggen.

Welcher Vault zentral entsperrt wird, kommt aus ``OPNCOCKPIT_VAULT_PATH``
(Env). Fallback: der Default-Vault aus ``AppSettings``. Wenn beides leer
ist, bleibt der Server in ``needs-admin`` haengen und der Setup-Wizard
fragt den Pfad mit ab.

Thread-safety: alle veraendernden Operationen laufen unter einem ``RLock``.
``save_vault`` und Bootstrap-Operationen koennen sich somit nicht
ueberkreuzen.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import Literal

from opn_cockpit.config import AppSettings
from opn_cockpit.security.auth_backend import (
    AuthBackend,
    UserDbAuthBackend,
    VaultAuthBackend,
)
from opn_cockpit.security.users import UserStore, default_users_db_path
from opn_cockpit.vault.store import OpenedVault, open_vault, save_vault

VAULT_PATH_ENV = "OPNCOCKPIT_VAULT_PATH"

# Lifecycle-Stati.
BootstrapStatus = Literal[
    "single-user",        # Single-User-Mode, kein Bootstrap noetig
    "needs-admin",        # Multi-User: noch kein Admin in der DB
    "needs-vault-unlock", # Multi-User: Admin existiert, Vault noch nicht entsperrt
    "ready",              # Multi-User: User koennen sich einloggen
]


class ServerStateError(Exception):
    """Logikfehler im Server-Zustand — z. B. falscher Mode-Aufruf."""


@dataclass(slots=True)
class ServerState:
    """Server-weiter Auth- und Vault-Zustand.

    Wird beim App-Boot via :meth:`from_settings` instanziiert und an
    ``app.state.server_state`` gehaengt. FastAPI-Dependencies greifen
    von dort darauf zu.
    """

    settings: AppSettings
    _user_store: UserStore | None = None
    _opened_vault: OpenedVault | None = None
    _vault_path: Path | None = None
    _master_password: str | None = None
    _lock: RLock = field(default_factory=RLock)

    # ----- Konstruktion -----

    @classmethod
    def from_settings(cls, settings: AppSettings | None = None) -> ServerState:
        """Erzeugt einen ServerState passend zu den Settings.

        Im Multi-User-Mode wird die User-DB sofort geoeffnet, der Vault
        bleibt aber noch geschlossen — er wird beim Server-Admin-Login
        durch :meth:`bootstrap_unlock_vault` entsperrt.
        """
        s = settings or AppSettings.load()
        state = cls(settings=s)
        if state.is_multi_user_mode:
            state._user_store = UserStore(path=default_users_db_path())
        return state

    # ----- Mode-Abfragen -----

    @property
    def is_single_user_mode(self) -> bool:
        return self.settings.auth_backend == "vault"

    @property
    def is_multi_user_mode(self) -> bool:
        return self.settings.auth_backend == "user-db"

    @property
    def user_store(self) -> UserStore | None:
        return self._user_store

    @property
    def vault_path(self) -> Path | None:
        return self._vault_path

    @property
    def is_vault_unlocked(self) -> bool:
        return self._opened_vault is not None

    # ----- Lifecycle / Status -----

    @property
    def bootstrap_status(self) -> BootstrapStatus:
        """Aktueller Bootstrap-Status (siehe Modul-Docstring)."""
        if self.is_single_user_mode:
            return "single-user"
        assert self._user_store is not None
        if self._user_store.count() == 0:
            return "needs-admin"
        if self._opened_vault is None:
            return "needs-vault-unlock"
        return "ready"

    @property
    def suggested_vault_path(self) -> str | None:
        """Vorschlag fuer den Multi-User-Vault — Env > Default-Vault.

        Wird vom Setup-Wizard angeboten, der User kann den Pfad ueberschreiben.
        """
        env_path = os.environ.get(VAULT_PATH_ENV, "").strip()
        if env_path:
            return env_path
        if self.settings.default_vault:
            return self.settings.default_vault
        return None

    # ----- Bootstrap-Aktionen -----

    def bootstrap_create_admin(
        self,
        username: str,
        password: str,
    ) -> None:
        """Legt den ersten Admin an. Schlaegt fehl wenn bereits einer existiert.

        Multi-User-only. Im Single-Mode hat das keinen Sinn — der
        Aufrufer (Bootstrap-Endpoint) muss das via :attr:`bootstrap_status`
        vorher pruefen.
        """
        if not self.is_multi_user_mode:
            raise ServerStateError(
                "bootstrap_create_admin ist nur im Multi-User-Mode verfuegbar.",
            )
        assert self._user_store is not None
        with self._lock:
            if self._user_store.count() > 0:
                raise ServerStateError(
                    "Es existiert bereits mindestens ein User — Bootstrap abgeschlossen.",
                )
            self._user_store.create_user(
                username=username,
                password=password,
                role="admin",
            )

    def bootstrap_unlock_vault(
        self,
        vault_path: Path,
        password: str,
    ) -> None:
        """Entsperrt den zentralen Vault.

        Multi-User-only. Bei falschem Passwort wirft ``InvalidPasswordError``;
        bei kaputter/fehlender Datei wirft ``VaultIOError``/``CorruptVaultError``.
        Diese sollen vom Endpoint in saubere HTTP-Fehler gemappt werden.
        """
        if not self.is_multi_user_mode:
            raise ServerStateError(
                "bootstrap_unlock_vault ist nur im Multi-User-Mode verfuegbar.",
            )
        with self._lock:
            opened = open_vault(vault_path, password)
            self._opened_vault = opened
            self._vault_path = vault_path
            self._master_password = password

    def lock_vault(self) -> None:
        """Vergisst den zentralen Vault. Multi-User: setzt Server zurueck auf needs-vault-unlock."""
        with self._lock:
            self._opened_vault = None
            self._vault_path = None
            self._master_password = None

    # ----- Auth-Backend-Aufloesung -----

    def auth_backend(self) -> AuthBackend:
        """Liefert das aktive AuthBackend.

        * Single-Mode: stets ``VaultAuthBackend`` (jede Session bringt
          Vault-Pfad + Passwort mit).
        * Multi-Mode: ``UserDbAuthBackend``, sobald der Vault entsperrt
          ist. Vorher wirft die Methode ``ServerStateError`` — der
          Bootstrap-Flow muss erst durchlaufen.
        """
        if self.is_single_user_mode:
            return VaultAuthBackend()
        with self._lock:
            if (
                self._user_store is None
                or self._opened_vault is None
                or self._vault_path is None
                or self._master_password is None
            ):
                raise ServerStateError(
                    "Multi-User-Server ist noch nicht ready (Bootstrap unvollstaendig).",
                )
            return UserDbAuthBackend(
                user_store=self._user_store,
                opened_vault=self._opened_vault,
                vault_path=self._vault_path,
                master_password=self._master_password,
            )

    # ----- Save-Vault (zentralisiert im Multi-Mode) -----

    def save_vault_central(
        self,
        opened: OpenedVault,
        vault_path: Path,
        password: str,
    ) -> OpenedVault:
        """Persistiert Aenderungen am Multi-User-Vault.

        Im Multi-Mode aktualisiert das auch die zentral gehaltene
        ``opened_vault``-Referenz, sodass alle nachfolgenden Sessions
        den neuen Header sehen. Vor dem Aufruf MUSS das Lock gehalten
        sein, damit zwei parallele Save-Vorgaenge sich nicht ueberholen.

        Single-Mode darf die zentrale Save-Funktion nicht aufrufen — die
        per-Session-Save-Pfade in inventory.py/imports.py sind dafuer
        zustaendig. Wir akzeptieren das im Single-Mode trotzdem (nuetzlich
        in Tests), aber aktualisieren keine zentrale Referenz.
        """
        with self._lock:
            new_opened = save_vault(vault_path, opened, password)
            if (
                self.is_multi_user_mode
                and self._vault_path == vault_path
            ):
                self._opened_vault = new_opened
                self._master_password = password
            return new_opened

    # ----- Cleanup -----

    def close(self) -> None:
        """Schliesst die UserStore-Connection und vergisst den Vault."""
        with self._lock:
            self.lock_vault()
            if self._user_store is not None:
                self._user_store.close()
                self._user_store = None


__all__ = [
    "VAULT_PATH_ENV",
    "BootstrapStatus",
    "ServerState",
    "ServerStateError",
]
