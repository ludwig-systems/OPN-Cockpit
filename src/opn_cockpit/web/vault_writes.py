"""Zentraler Save-Vault-Pfad fuer Multi-User-Sicherheit.

Im Single-User-Mode (heute Default) ruft das einfach ``vault.store.save_vault``
auf und aktualisiert die ``Session``. Im Multi-User-Mode laeuft alles
ueber ``ServerState.save_vault_central``, das den Schreibvorgang unter
einem zentralen Lock haelt und die zentrale ``OpenedVault``-Referenz mit
frischem Header tauscht. Anschliessend werden **alle** aktiven Sessions
desselben Vaults aktualisiert — sonst halten sie veraltete Header und
ihr naechster Save scheitert (Nonce-Reuse oder Header-Mismatch).

Aufrufer (heute ``inventory.py`` + ``imports.py``): nutzen
``persist_session_vault(request, session, vault_path)`` als Drop-in-
Replacement fuer den frueheren direkten ``save_vault``-Aufruf.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from fastapi import HTTPException, Request, status

from opn_cockpit.security.session import Session
from opn_cockpit.vault.errors import (
    CorruptVaultError,
    SessionLockedError,
    VaultError,
    VaultIOError,
    VaultVersionError,
)
from opn_cockpit.vault.store import save_vault
from opn_cockpit.web.auth.manager import SessionManager
from opn_cockpit.web.server_state import ServerState


def persist_session_vault(
    request: Request,
    session: Session,
    vault_path: Path,
    *,
    rollback: Callable[[], None],
) -> None:
    """Persistiert die Vault-Aenderungen der Session und rollt bei Fehlern zurueck.

    Im Multi-User-Mode wird das ``ServerState.save_vault_central`` benutzt,
    das den Save unter Lock haelt und die zentrale ``OpenedVault``-
    Referenz aktualisiert. Anschliessend werden alle anderen Sessions des
    gleichen Vaults via ``SessionManager.replace_opened_everywhere``
    aufgefrischt.

    Im Single-User-Mode laeuft der klassische Pfad direkt via
    ``vault.store.save_vault``.
    """
    try:
        password = session.master_password
    except SessionLockedError as exc:
        rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Session ohne Master-Passwort - bitte neu entsperren.",
        ) from exc

    server_state = getattr(request.app.state, "server_state", None)
    is_multi_user = isinstance(server_state, ServerState) and server_state.is_multi_user_mode

    try:
        if is_multi_user:
            assert isinstance(server_state, ServerState)
            new_opened = server_state.save_vault_central(
                session.opened, vault_path, password,
            )
        else:
            new_opened = save_vault(vault_path, session.opened, password)
    except (CorruptVaultError, VaultVersionError, VaultIOError) as exc:
        rollback()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except VaultError as exc:
        rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    session.replace_opened(new_opened)
    if is_multi_user:
        manager = getattr(request.app.state, "session_manager", None)
        if isinstance(manager, SessionManager):
            manager.replace_opened_everywhere(new_opened, vault_path)


__all__ = ["persist_session_vault"]
