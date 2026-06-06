"""Auth-Routen: Vault entsperren, sperren, Session-Info abrufen."""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, status

from opn_cockpit.audit.backend import AuditBackend, audit_actor, get_audit_backend
from opn_cockpit.audit.log import AuditEventKind
from opn_cockpit.security import totp as totp_mod
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
    TotpChallengeResponse,
    TotpLoginRequest,
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
from opn_cockpit.web.vault_path import (
    VaultPathError,
    resolve_freeform_vault_path,
    resolve_safe_vault_path,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _login_limiter(request: Request) -> RateLimiter:
    limiter = getattr(request.app.state, "login_rate_limiter", None)
    if not isinstance(limiter, RateLimiter):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Rate-Limiter not initialised",
        )
    return limiter


_TRUST_FORWARDED_FOR_ENV = "OPNCOCKPIT_TRUST_FORWARDED_FOR"


def _trust_forwarded_for() -> bool:
    """True wenn der Server hinter einem vertrauenswuerdigen Reverse-Proxy
    laeuft und ``X-Forwarded-For`` als Client-IP gelten soll.

    Audit-Finding G2: Ohne diesen Schalter konnte ein Angreifer, der den
    Cockpit-Server direkt erreicht (kein Proxy davor), beliebige XFF-IPs
    setzen und damit den Rate-Limit-Bucket pro fake-IP umgehen. Default
    ``false`` haerten wir gegen genau diesen Fall.
    """
    return os.environ.get(_TRUST_FORWARDED_FOR_ENV, "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _client_key(request: Request) -> str:
    """Liefert einen stabilen Client-Schluessel fuer Rate-Limiting.

    Audit-Finding G2: ``X-Forwarded-For`` wird NUR akzeptiert wenn die
    Env-Var ``OPNCOCKPIT_TRUST_FORWARDED_FOR=true`` gesetzt ist
    (Reverse-Proxy-Deploy). Sonst zaehlt die direkte ``request.client.host``,
    sodass ein Angreifer ohne Proxy davor den Bucket nicht spoofen kann.
    """
    if _trust_forwarded_for():
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
    server: ServerState = Depends(get_server_state),
) -> UnlockResponse:
    """Entsperrt einen Tresor und liefert ein Bearer-Token zurueck.

    Single-User-Mode: Pfade duerfen beliebig auf der lokalen Maschine
    liegen (USB-Sticks, externe Laufwerke, beliebige Ordner). Der
    Browser-User ist hier identisch mit dem Maschinen-Admin, eine
    Pfad-Restriktion brachte nur Reibung.

    Multi-User-Server-Mode: Pfade muessen unter einer der erlaubten
    Basen liegen (APPDATA / Home / OPNCOCKPIT_VAULT_DIR), sonst koennte
    ein authentifizierter User den Server zwingen, beliebige Dateien
    zu lesen.
    """
    limiter = _login_limiter(request)
    _check_rate_limit(request, limiter)
    client_key = _client_key(request)
    try:
        if server.is_multi_user_mode:
            path = resolve_safe_vault_path(payload.vault_path)
        else:
            path = resolve_freeform_vault_path(payload.vault_path)
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
    response_model=None,  # Union: UnlockResponse oder TotpChallengeResponse
    status_code=status.HTTP_200_OK,
)
def login(
    payload: LoginRequest,
    request: Request,
    manager: SessionManager = Depends(get_session_manager),
    server: ServerState = Depends(get_server_state),
) -> UnlockResponse | TotpChallengeResponse:
    """Multi-User-Login per Username + Passwort.

    Nur im Multi-User-Mode aktiv und nur wenn der Server bereits
    ``ready`` ist (Admin angelegt + zentraler Vault entsperrt). Liefert
    sonst 409.

    Wenn der User TOTP aktiviert hat, ist dies nur Schritt 1: Server
    haelt die Auth-Daten kurz im ``_pending_totp_logins``-Cache und gibt
    eine Challenge zurueck. Schritt 2 ist ``POST /api/auth/login/totp``.
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
    user = result.user
    if user is not None and user.totp_enabled:
        # Schritt 1 erfolgreich, aber wir bauen noch keine Session - der
        # User muss Schritt 2 (TOTP-Code) bestehen.
        challenge = totp_mod.issue_challenge(
            user.id, _challenge_secret(request),
        )
        # Login-Versuch zaehlt noch nicht als Erfolg, aber zaehlen wir
        # auch nicht als Fehler — sonst koennte ein Angreifer den
        # Rate-Limiter durch Passwort-Tippen vor jedem TOTP-Versuch
        # einseitig fuellen. Schritt-2-Fehler werden separat geahndet.
        return TotpChallengeResponse(challenge=challenge.to_token())
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
    "/login/totp",
    response_model=UnlockResponse,
    status_code=status.HTTP_200_OK,
)
def login_totp(
    payload: TotpLoginRequest,
    request: Request,
    manager: SessionManager = Depends(get_session_manager),
    server: ServerState = Depends(get_server_state),
) -> UnlockResponse:
    """Schritt 2 des Logins: TOTP-Code oder Backup-Code.

    Erfordert die ``challenge`` aus Schritt 1. Der Server prueft
    Signatur+Frist der Challenge, danach den Code gegen das User-Secret
    bzw. die Backup-Codes. Bei Erfolg wird die Session erzeugt.
    """
    if not server.is_multi_user_mode:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="TOTP-Login ist nur im Multi-User-Mode verfuegbar.",
        )
    if server.bootstrap_status != "ready":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Server-Status: {server.bootstrap_status}.",
        )
    limiter = _login_limiter(request)
    _check_rate_limit(request, limiter)
    client_key = _client_key(request)

    parsed = totp_mod.TotpChallenge.from_token(payload.challenge)
    if parsed is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Challenge-Format ungueltig.",
        )
    secret = _challenge_secret(request)
    if not totp_mod.verify_challenge(parsed, secret):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Challenge abgelaufen oder gefaelscht - bitte neu einloggen.",
        )
    store = server.user_store
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="User-Store nicht initialisiert.",
        )
    user = store.get_user(parsed.user_id)
    if user is None or user.disabled or not user.totp_enabled:
        # Konsistent gleiche Meldung wie bei falschem Code, damit ein
        # Angreifer nicht aus dem Fehlertext lernt.
        limiter.register_failure(client_key)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="TOTP-Login fehlgeschlagen.",
        )

    code = payload.code.strip()
    user_secret = store.get_totp_secret(user.id)
    used_backup = False
    if not totp_mod.verify_code(user_secret, code):
        # Vielleicht ein Backup-Code?
        hashes = store.get_backup_code_hashes(user.id)
        consumed, remaining = totp_mod.verify_backup_code(code, hashes)
        if not consumed:
            limiter.register_failure(client_key)
            _audit_totp_failed(user.username)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="TOTP-Login fehlgeschlagen.",
            )
        used_backup = True
        store.set_backup_code_hashes(user.id, remaining)

    # Auth komplett — Session bauen wie im Single-Step-Login.
    try:
        backend = server.auth_backend()
    except ServerStateError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    # Wir koennen das Passwort nicht erneut pruefen (haben wir nicht
    # mehr), aber Backend hat alle Daten — die zentralen Vault-Bytes
    # plus das Master-PW liegen in ``UserDbAuthBackend``. Wir bauen die
    # Session direkt aus User + Backend.
    from opn_cockpit.security.auth_backend import (  # noqa: PLC0415 — lokal, vermeidet zirkulaeren Import
        AuthResult,
        UserDbAuthBackend,
    )
    if not isinstance(backend, UserDbAuthBackend):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Auth-Backend unterstuetzt TOTP-Login nicht.",
        )
    auth_result = AuthResult(
        opened_vault=backend.opened_vault,
        vault_path=backend.vault_path,
        master_password=backend.master_password,
        user=user,
    )
    limiter.register_success(client_key)
    token, session = manager.create_from(auth_result)
    _audit_user_logged_in(
        user.username + (" (backup-code)" if used_backup else " (totp)"),
    )
    return UnlockResponse(
        token=token,
        vault_path=str(auth_result.vault_path),
        vault_filename=auth_result.vault_path.name,
        inactivity_timeout_s=int(session.inactivity_timeout_s),
        seconds_until_expiry=int(session.seconds_until_expiry()),
    )


def _challenge_secret(request: Request) -> bytes:
    """Liefert das HMAC-Secret fuer TOTP-Challenges aus dem App-State.

    Wird beim App-Boot in ``app.state.totp_challenge_secret`` gesetzt
    (siehe ``web/server.py``).
    """
    secret = getattr(request.app.state, "totp_challenge_secret", None)
    if not isinstance(secret, bytes):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="TOTP-Challenge-Secret nicht initialisiert.",
        )
    return secret


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

    Retry-Watcher-Jobs werden NICHT geloescht, sondern auf Orphan-Status
    gesetzt (session_token=""). Sobald jemand denselben Tresor wieder
    entsperrt, adoptiert der Watcher die Jobs automatisch ueber den
    vault_path. Beim Auto-Lock haengen also keine Retries verloren -
    sie ueberleben den Lock-Cycle bis ``max_duration_s`` abgelaufen ist.
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


def _audit_totp_failed(username: str) -> None:
    _audit_log().append(
        AuditEventKind.LOGIN_FAILED,
        actor=username,
        summary=f"TOTP-Code-Verifikation fehlgeschlagen fuer '{username}'.",
    )


def _audit_user_logged_in(username: str) -> None:
    # Audit-Finding G7: eigener Event-Kind statt VAULT_OPENED-Reuse,
    # damit Login-Forensik im UI-Audit-Filter sauber abrufbar ist.
    _audit_log().append(
        AuditEventKind.USER_LOGIN_SUCCESS,
        actor=username,
        summary=f"Multi-User-Login erfolgreich: '{username}'.",
    )
