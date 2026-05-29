"""Auth-Routen: Vault entsperren, sperren, Session-Info abrufen."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, status

from opn_cockpit.audit.log import AuditEventKind, AuditLog, default_audit_path
from opn_cockpit.security.session import Session
from opn_cockpit.vault.errors import (
    CorruptVaultError,
    InvalidPasswordError,
    VaultError,
    VaultIOError,
    VaultVersionError,
)
from opn_cockpit.vault.store import open_vault
from opn_cockpit.web.api.schemas import (
    CurrentSessionResponse,
    UnlockRequest,
    UnlockResponse,
)
from opn_cockpit.web.auth.dependencies import (
    get_session_manager,
    require_session,
    require_session_with_token,
)
from opn_cockpit.web.auth.manager import SessionManager

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


def _audit_log() -> AuditLog:
    return AuditLog(path=default_audit_path())


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
