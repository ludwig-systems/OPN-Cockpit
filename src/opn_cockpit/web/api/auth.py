"""Auth-Routen: Vault entsperren, sperren, Session-Info abrufen."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, status

from opn_cockpit.audit.backend import AuditBackend, audit_actor, get_audit_backend
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
from opn_cockpit.web.rate_limit import RateLimiter
from opn_cockpit.web.server_state import ServerState, ServerStateError
from opn_cockpit.web.vault_path import VaultPathError, resolve_safe_vault_path

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _login_limiter(request: Request) -> RateLimiter:
    limiter = getattr(request.app.state, "login_rate_limiter", None)
    if not isinstance(limiter, RateLimiter):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Rate-Limiter not initialised",
        )
    return limiter


def _client_key(request: Request) -> str:
    """Liefert einen stabilen Client-Schluessel — Audit #4.

    Reverse-Proxy-Setup nutzt X-Forwarded-For (linker erster Eintrag),
    sonst die direkte Client-IP. Defensiv gegen Header-Injection: nur
    ersten Komma-Separierten Wert nehmen, max 64 Zeichen.
    """
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        first = fwd.split(",", 1)[0].strip()
        if first:
            return first[:64]
    client = request.client
    return client.host if client else "unknown"


def _check_rate_limit(request: Request, limiter: RateLimiter) -> None:
    """Wirft 429 wenn der Client gerade gesperrt ist."""
    key = _client_key(request)
    remaining = limiter.check(key)
    if remaining is not None:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                f"Zu viele Versuche — bitte {int(remaining) + 1} s warten."
            ),
            headers={"Retry-After": str(int(remaining) + 1)},
        )


@router.post(
    "/unlock",
    response_model=UnlockResponse,
    status_code=status.HTTP_200_OK,
)
def unlock(
    payload: UnlockRequest,
    request: Request,
    manager: SessionManager = Depends(get_session_manager),
) -> UnlockResponse:
    """Entsperrt einen Tresor und liefert ein Bearer-Token zurueck."""
    limiter = _login_limiter(request)
    _check_rate_limit(request, limiter)
    client_key = _client_key(request)
    try:
        path = resolve_safe_vault_path(payload.vault_path)
    except VaultPathError as exc:
        limiter.register_failure(client_key)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    if not path.exists():
        _audit_login_failed(path, "vault_missing")
        limiter.register_failure(client_key)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Tresor-Datei nicht gefunden: {path}",
        )
    try:
        opened = open_vault(path, payload.password)
    except InvalidPasswordError as exc:
        _audit_login_failed(path, "invalid_password")
        limiter.register_failure(client_key)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Master-Passwort falsch oder Tresor manipuliert.",
        ) from exc
    except (CorruptVaultError, VaultVersionError, VaultIOError) as exc:
        _audit_login_failed(path, "vault_corrupt")
        limiter.register_failure(client_key)
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

    limiter.register_success(client_key)
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
    request: Request,
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
    limiter = _login_limiter(request)
    _check_rate_limit(request, limiter)
    client_key = _client_key(request)
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
        limiter.register_failure(client_key)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Benutzername oder Passwort falsch.",
        )
    limiter.register_success(client_key)
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
    actor = audit_actor(session)
    manager.revoke(token)
    watcher = getattr(request.app.state, "retry_watcher", None)
    if watcher is not None:
        watcher.cancel_for_session(token)
    _audit_vault_locked(vault_path, actor=actor)


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


def _audit_vault_locked(path: Path | None, *, actor: str | None = None) -> None:
    _audit_log().append(
        AuditEventKind.VAULT_LOCKED,
        actor=actor,
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
        actor=username,
        summary=f"Multi-User-Login fehlgeschlagen fuer '{username}'.",
    )


def _audit_user_logged_in(username: str) -> None:
    _audit_log().append(
        AuditEventKind.VAULT_OPENED,
        actor=username,
        summary=f"Multi-User-Login erfolgreich: '{username}'.",
    )
