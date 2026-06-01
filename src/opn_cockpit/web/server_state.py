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

import contextlib
import os
import secrets
import sys
from collections.abc import Iterator
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
from opn_cockpit.vault.model import VaultData
from opn_cockpit.vault.store import OpenedVault, create_vault, open_vault, save_vault

VAULT_PATH_ENV = "OPNCOCKPIT_VAULT_PATH"

# Default-Admin fuer Multi-User-Erststart. Wer das aendern will, kann's
# direkt im Code tun — bewusst nicht per Env, weil sonst der Inno-Setup-
# Dialog die Anzeige nicht mehr weiss.
DEFAULT_ADMIN_USERNAME = "admin"
DEFAULT_ADMIN_PASSWORD = "OPN-Cockpit!"  # 12 Zeichen — entspricht MIN_PASSWORD_LENGTH

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
    _bootstrap_token: str | None = None
    _lock: RLock = field(default_factory=RLock)

    # ----- Konstruktion -----

    @classmethod
    def from_settings(cls, settings: AppSettings | None = None) -> ServerState:
        """Erzeugt einen ServerState passend zu den Settings.

        Im Multi-User-Mode wird die User-DB sofort geoeffnet, der Vault
        bleibt aber noch geschlossen — er wird beim Server-Admin-Login
        durch :meth:`bootstrap_unlock_vault` entsperrt.

        Falls die User-DB leer ist, wird ein Default-Admin angelegt
        (`admin` / `OPN-Cockpit!`) mit Pflicht-Passwort-Wechsel beim
        ersten Login. Pragmatisch wie Proxmox: keine Token-Logistik
        noetig, der Installer-Dialog/-Container-Log nennt das Default-PW,
        User wechselt es sofort.
        """
        s = settings or AppSettings.load()
        state = cls(settings=s)
        if state.is_multi_user_mode:
            state._user_store = UserStore(path=default_users_db_path())
            state._ensure_default_admin()
        return state

    def _ensure_default_admin(self) -> None:
        """Legt den Default-Admin an wenn die User-DB leer ist.

        Username: ``admin``. Passwort: ``OPN-Cockpit!`` (12 Zeichen).
        ``must_change_password=True`` zwingt den User beim ersten Login
        auf den Self-Service-PW-Wechsel.

        Wenn die DB schon User enthaelt (Upgrade einer bestehenden
        Multi-User-Installation, oder manueller Admin), passiert nichts.
        """
        if self._user_store is None:
            return
        if self._user_store.count() > 0:
            return
        from opn_cockpit.security.users import UserStoreError
        with contextlib.suppress(UserStoreError):
            self._user_store.create_user(
                username=DEFAULT_ADMIN_USERNAME,
                password=DEFAULT_ADMIN_PASSWORD,
                role="admin",
                must_change_password=True,
            )
        # Sichtbare Notiz fuer Container-Logs (Docker/journalctl) — auf
        # Windows-Service zeigt install-service.ps1 das gleiche im Dialog.
        msg = (
            "\n"
            + "=" * 60 + "\n"
            + "  OPN-Cockpit Default-Admin angelegt\n"
            + "  Username: " + DEFAULT_ADMIN_USERNAME + "\n"
            + "  Passwort: " + DEFAULT_ADMIN_PASSWORD + "\n"
            + "  Beim ersten Login MUSS das Passwort geaendert werden.\n"
            + "=" * 60 + "\n"
        )
        sys.stderr.write(msg)
        sys.stderr.flush()

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

    @property
    def bootstrap_token(self) -> str | None:
        """Aktueller Bootstrap-Token (None wenn nicht im Bootstrap-Status)."""
        return self._bootstrap_token

    def verify_bootstrap_token(self, supplied: str) -> bool:
        """Konstant-Zeit-Vergleich des Bootstrap-Tokens."""
        if not self._bootstrap_token or not supplied:
            return False
        return secrets.compare_digest(self._bootstrap_token, supplied)

    def invalidate_bootstrap_token(self) -> None:
        """Verbraucht den Bootstrap-Token (nach erfolgreichem Bootstrap-Schritt).

        Loescht auch die Token-Datei aus ``<data_dir>/BOOTSTRAP-TOKEN.txt``,
        damit dort kein veralteter Token herumliegt — sonst koennte ein
        spaeterer Nutzer den File-Inhalt missverstehen.
        """
        self._bootstrap_token = None
        with contextlib.suppress(OSError, ImportError):
            from opn_cockpit.config import get_app_data_dir
            token_file = get_app_data_dir() / "BOOTSTRAP-TOKEN.txt"
            if token_file.exists():
                token_file.unlink()

    @contextlib.contextmanager
    def vault_mutation_lock(self) -> Iterator[None]:
        """Serialisiert Read-Modify-Write auf dem zentralen Vault (Audit #9).

        Im Single-Mode ein No-Op — jede Session hat ihren eigenen
        Vault. Im Multi-Mode haelt der Lock von "lese opened.data.devices"
        bis "persist_session_vault hat gespeichert" gegen andere Sessions
        ab, die auf denselben OpenedVault zeigen.
        """
        if self.is_multi_user_mode:
            with self._lock:
                yield
        else:
            yield

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
        """Legt einen Admin-User an.

        Multi-User-only. Seit F28 verzichten wir auf den frueheren Count-
        Check, weil der Default-Admin (`admin` / `OPN-Cockpit!`) den Count
        immer auf >=1 setzt. Die Methode bleibt aber im Werkzeugkasten
        bestehen, weil sie von der CLI + Tests genutzt wird, um zusaetzliche
        Admins direkt programmatisch anzulegen. Sie wirft jetzt nur noch
        bei strukturellen Fehlern (kein Multi-Mode, Username-Konflikt).
        """
        if not self.is_multi_user_mode:
            raise ServerStateError(
                "bootstrap_create_admin ist nur im Multi-User-Mode verfuegbar.",
            )
        assert self._user_store is not None
        with self._lock:
            self._user_store.create_user(
                username=username,
                password=password,
                role="admin",
            )

    def bootstrap_unlock_vault(
        self,
        vault_path: Path,
        password: str,
        *,
        create_if_missing: bool = False,
    ) -> bool:
        """Entsperrt den zentralen Vault oder legt ihn neu an.

        Multi-User-only. Bei falschem Passwort wirft ``InvalidPasswordError``;
        bei kaputter Datei wirft ``VaultIOError``/``CorruptVaultError``.

        ``create_if_missing=True``: wenn die Datei nicht existiert, wird ein
        neuer leerer Vault mit dem uebergebenen Passwort angelegt. Auf
        einem frischen Multi-User-Server ist das der Default-Pfad — der
        Admin gibt sein Master-Passwort ein, der Server erstellt den
        Vault, danach ist der Server ``ready``.

        Returns:
            ``True`` wenn ein neuer Vault angelegt wurde, ``False`` bei Open.
        """
        if not self.is_multi_user_mode:
            raise ServerStateError(
                "bootstrap_unlock_vault ist nur im Multi-User-Mode verfuegbar.",
            )
        with self._lock:
            created = False
            if not vault_path.exists():
                if not create_if_missing:
                    # Aufrufer hat das nicht erlaubt — Endpoint mappt zu 404.
                    open_vault(vault_path, password)  # wirft VaultIOError
                vault_path.parent.mkdir(parents=True, exist_ok=True)
                create_vault(vault_path, password, VaultData())
                created = True
            opened = open_vault(vault_path, password)
            self._opened_vault = opened
            self._vault_path = vault_path
            self._master_password = password
            return created

    def switch_vault(
        self,
        new_path: Path,
        new_password: str,
        *,
        create_if_missing: bool = False,
    ) -> bool:
        """Wechselt den aktiven Multi-User-Vault zur Laufzeit.

        Vergisst den aktuellen Vault + Master-PW und entsperrt einen
        neuen (legt ihn ggf. an). Aufrufer muss alle nicht-Admin-
        Sessions ueber den SessionManager invalidieren — sonst zeigen
        sie auf den alten OpenedVault.

        Returns:
            ``True`` wenn ein neuer Vault angelegt wurde, ``False`` bei Open.
        """
        if not self.is_multi_user_mode:
            raise ServerStateError(
                "switch_vault ist nur im Multi-User-Mode verfuegbar.",
            )
        with self._lock:
            # Erst den neuen Vault validieren, dann erst den alten freigeben.
            created = False
            if not new_path.exists():
                if not create_if_missing:
                    open_vault(new_path, new_password)  # wirft VaultIOError
                new_path.parent.mkdir(parents=True, exist_ok=True)
                create_vault(new_path, new_password, VaultData())
                created = True
            opened = open_vault(new_path, new_password)
            self._opened_vault = opened
            self._vault_path = new_path
            self._master_password = new_password
            return created

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
