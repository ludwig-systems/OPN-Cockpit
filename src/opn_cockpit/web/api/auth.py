"""Auth-Routen: Vault entsperren, sperren, Session-Info abrufen."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, status

from opn_cockpit.audit.backend import AuditBackend, get_audit_backend
from opn_cockpit.audit.log import AuditEventKind
from opn_cockpit.security.session import Session
from opn_cockpit.vault.errors import (
    CorruptVaultError,
    InvalidPasswordError,
    VaultError,
    VaultIOError,
    VaultVersionError,
)
from opn_cockpit.vault.store import open_vault
from opn_cockpit.web.api.bootstrap import get_server_state
from opn_cockpit.web.api.schemas import (
    CurrentSessionResponse,
    LoginRequest,
    UnlockRequest,
    UnlockResponse,
)
from opn_cockpit.web.auth.dependencies import (
    get_session_manager,
    require_session,
    require_session_with_token,
)
from opn_cockpit.web.auth.manager import SessionManager
from opn_cockpit.web.server_state import ServerState, ServerStateError

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post(
    "/unlock",
    response_model=UnlockResponse,
    status_code=status.HTTP_200_OK,
)
def unlock(
    payload: UnlockRequest,
    manager: SessionManager = Depends(get_session_manager),
) -> UnlockResponse:
    """Entsperrt einen Tresor und liefert ein Bearer-Token zurueck."""
    path = Path(payload.vault_path)
    if not path.exists():
        _audit_login_failed(path, "vault_missing")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Tresor-Datei nicht gefunden: {path}",
        )
    try:
        opened = open_vault(path, payload.password)
    except InvalidPasswordError as exc:
        _audit_login_failed(path, "invalid_password")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Master-Passwort falsch oder Tresor manipuliert.",
        ) from exc
    except (CorruptVaultError, VaultVersionError, VaultIOError) as exc:
        _audit_login_failed(path, "vault_corrupt")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except VaultError as exc:
        _audit_login_failed(path, "vault_error")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    token, session = manager.create(opened, path, payload.password)
    _audit_vault_opened(path)
    return UnlockResponse(
        token=token,
        vault_path=str(path),
        vault_filename=path.name,
        inactivity_timeout_s=int(session.inactivity_timeout_s),
        seconds_until_expiry=int(session.seconds_until_expiry()),
    )


@router.post(
    "/login",
    response_model=UnlockResponse,
    status_code=status.HTTP_200_OK,
)
def login(
    payload: LoginRequest,
    manager: SessionManager = Depends(get_session_manager),
    server: ServerState = Depends(get_server_state),
) -> UnlockResponse:
    """Multi-User-Login per Username + Passwort.

    Nur im Multi-User-Mode aktiv und nur wenn der Server bereits
    ``ready`` ist (Admin angelegt + zentraler Vault entsperrt). Liefert
    sonst 409.
    """
    if not server.is_multi_user_mode:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Login per Username ist im Single-User-Mode nicht verfuegbar.",
        )
    if server.bootstrap_status != "ready":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Server noch nicht ready — Bootstrap-Status: "
                f"{server.bootstrap_status}."
            ),
        )
    try:
        backend = server.auth_backend()
    except ServerStateError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    result = backend.authenticate({
        "username": payload.username,
        "password": payload.password,
    })
    if result is None:
        _audit_login_failed_user(payload.username)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Benutzername oder Passwort falsch.",
        )
    token, session = manager.create_from(result)
    _audit_user_logged_in(payload.username)
    return UnlockResponse(
        token=token,
        vault_path=str(result.vault_path),
        vault_filename=result.vault_path.name,
        inactivity_timeout_s=int(session.inactivity_timeout_s),
        seconds_until_expiry=int(session.seconds_until_expiry()),
    )


@router.post(
    "/lock",
    status_code=status.HTTP_204_NO_CONTENT,
)
def lock(
    request: Request,
    pair: tuple[Session, str] = Depends(require_session_with_token),
    manager: SessionManager = Depends(get_session_manager),
) -> None:
    """Sperrt die aktuelle Session und revoked das Token.

    Beendet auch alle laufenden Retry-Watcher-Jobs dieses Tokens — sonst
    haetten die nach Auto-Lock noch eine Weile (unsinnig) weiterprobiert.
    """
    session, token = pair
    vault_path = session.vault_path
    manager.revoke(token)
    watcher = getattr(request.app.state, "retry_watcher", None)
    if watcher is not None:
        watcher.cancel_for_session(token)
    _audit_vault_locked(vault_path)


@router.get(
    "/me",
    response_model=CurrentSessionResponse,
)
def me(session: Session = Depends(require_session)) -> CurrentSessionResponse:
    """Liefert Info zur aktuellen Session — fuer Frontend-Boot-Check."""
    path = session.vault_path
    if path is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session ohne Tresor-Pfad — bitte erneut entsperren.",
        )
    return CurrentSessionResponse(
        vault_path=str(path),
        vault_filename=path.name,
        inactivity_timeout_s=int(session.inactivity_timeout_s),
        seconds_until_expiry=int(session.seconds_until_expiry()),
    )


# ---------------------------------------------------------------------------
# Audit-Helfer
# ---------------------------------------------------------------------------


def _audit_log() -> AuditBackend:
    return get_audit_backend()


def _audit_vault_opened(path: Path) -> None:
    _audit_log().append(
        AuditEventKind.VAULT_OPENED,
        vault_path=str(path),
        summary=f"Tresor entsperrt (Web): {path}",
    )


def _audit_vault_locked(path: Path | None) -> None:
    _audit_log().append(
        AuditEventKind.VAULT_LOCKED,
        vault_path=str(path) if path else None,
        summary="Tresor gesperrt (Web).",
    )


def _audit_login_failed(path: Path, reason: str) -> None:
    _audit_log().append(
        AuditEventKind.LOGIN_FAILED,
        vault_path=str(path),
        summary=f"Web-Login fehlgeschlagen ({reason}) fuer {path}.",
    )


def _audit_login_failed_user(username: str) -> None:
    _audit_log().append(
        AuditEventKind.LOGIN_FAILED,
        summary=f"Multi-User-Login fehlgeschlagen fuer '{username}'.",
    )


def _audit_user_logged_in(username: str) -> None:
    _audit_log().append(
        AuditEventKind.VAULT_OPENED,
        summary=f"Multi-User-Login erfolgreich: '{username}'.",
    )
