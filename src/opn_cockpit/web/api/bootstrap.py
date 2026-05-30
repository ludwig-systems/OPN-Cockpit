"""Bootstrap-Endpunkte fuer den Multi-User-Server-Mode.

Der Frontend-Bootloader ruft beim Page-Load als erstes
``GET /api/bootstrap/status`` auf und entscheidet anhand der Antwort,
ob er den klassischen Single-User-Vault-Picker, den Setup-Wizard
(Admin + Vault anlegen) oder den Multi-User-Login zeigt.

Im Single-User-Mode liefert ``status`` immer ``single-user``; die POST-
Routen schlagen mit 409 fehl. Im Multi-User-Mode laufen die POSTs durch
den ``ServerState``-Lifecycle:

* ``POST /api/bootstrap/admin`` — legt den ersten Admin an. Nur erlaubt,
  solange noch kein Admin existiert.
* ``POST /api/bootstrap/vault`` — entsperrt den zentralen Vault. Nur
  erlaubt, wenn Admin existiert und Vault noch nicht entsperrt ist.

Beide Endpunkte sind absichtlich oeffentlich (kein Bearer-Token), weil
sie zur Initialisierung des Tokens dienen. Sobald der Server ``ready``
ist, antworten beide mit 409.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, Field

from opn_cockpit.audit.backend import get_audit_backend
from opn_cockpit.audit.log import AuditEventKind
from opn_cockpit.security.users import UserStoreError
from opn_cockpit.vault.errors import (
    CorruptVaultError,
    InvalidPasswordError,
    VaultError,
    VaultIOError,
    VaultVersionError,
)
from opn_cockpit.web.rate_limit import RateLimiter
from opn_cockpit.web.server_state import ServerState, ServerStateError
from opn_cockpit.web.vault_path import (
    VaultPathError,
    resolve_safe_vault_path,
)

router = APIRouter(prefix="/api/bootstrap", tags=["bootstrap"])


# ---------------------------------------------------------------------------
# Schemas (lokal, weil bootstrap-spezifisch)
# ---------------------------------------------------------------------------


class BootstrapStatusResponse(BaseModel):
    """Antwort auf ``GET /api/bootstrap/status``.

    ``mode`` ist der konfigurierte Auth-Backend-Wert. ``status`` ist der
    aktuelle Lifecycle-Punkt — Frontend wertet das aus, um den richtigen
    Screen zu zeigen.
    """

    mode: str  # 'vault' (single-user) | 'user-db' (multi-user)
    status: str  # 'single-user' | 'needs-admin' | 'needs-vault-unlock' | 'ready'
    suggested_vault_path: str | None = None


class BootstrapAdminRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=120)
    password: str = Field(..., min_length=12)


class BootstrapVaultRequest(BaseModel):
    vault_path: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)


# ---------------------------------------------------------------------------
# Dependency
# ---------------------------------------------------------------------------


def get_server_state(request: Request) -> ServerState:
    state = getattr(request.app.state, "server_state", None)
    if not isinstance(state, ServerState):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="ServerState not initialised",
        )
    return state


def _bootstrap_limiter(request: Request) -> RateLimiter:
    limiter = getattr(request.app.state, "bootstrap_rate_limiter", None)
    if not isinstance(limiter, RateLimiter):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Bootstrap-Rate-Limiter not initialised",
        )
    return limiter


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        first = fwd.split(",", 1)[0].strip()
        if first:
            return first[:64]
    client = request.client
    return client.host if client else "unknown"


def _check_bootstrap_token(
    server: ServerState, supplied_token: str | None,
) -> None:
    """Wirft 403 wenn der gelieferte Token nicht passt (Audit #5).

    Im Single-Mode haben wir hier nichts zu tun (Bootstrap-Endpunkte
    lehnen vorher schon ab).
    """
    if not server.is_multi_user_mode:
        return
    if not supplied_token:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Bootstrap-Token fehlt. Token steht im Server-Log "
                "(docker compose logs / journalctl)."
            ),
        )
    if not server.verify_bootstrap_token(supplied_token):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Bootstrap-Token ungueltig.",
        )


# ---------------------------------------------------------------------------
# Routen
# ---------------------------------------------------------------------------


@router.get("/status", response_model=BootstrapStatusResponse)
def bootstrap_status(
    server: ServerState = Depends(get_server_state),
) -> BootstrapStatusResponse:
    """Liefert Auth-Mode + Lifecycle-Status fuer das Frontend.

    Oeffentlicher Endpunkt — der Frontend-Boot ruft das ohne Token auf.
    """
    return BootstrapStatusResponse(
        mode=server.settings.auth_backend,
        status=server.bootstrap_status,
        suggested_vault_path=server.suggested_vault_path,
    )


@router.post(
    "/admin",
    status_code=status.HTTP_201_CREATED,
)
def bootstrap_admin(
    payload: BootstrapAdminRequest,
    request: Request,
    server: ServerState = Depends(get_server_state),
    x_bootstrap_token: str | None = Header(None, alias="X-Bootstrap-Token"),
) -> dict[str, str]:
    """Legt den ersten Admin an.

    Erfordert ``X-Bootstrap-Token`` aus dem Server-Log (Audit #5).
    Rate-Limit: 5 Versuche pro Stunde pro IP (Audit #4).
    """
    if server.bootstrap_status != "needs-admin":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Bootstrap-Admin nicht erlaubt — Server-Status: "
                f"{server.bootstrap_status}."
            ),
        )
    limiter = _bootstrap_limiter(request)
    client_key = _client_ip(request)
    remaining = limiter.check(client_key)
    if remaining is not None:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Zu viele Bootstrap-Versuche — {int(remaining) + 1} s warten.",
            headers={"Retry-After": str(int(remaining) + 1)},
        )
    try:
        _check_bootstrap_token(server, x_bootstrap_token)
    except HTTPException:
        limiter.register_failure(client_key)
        raise
    try:
        server.bootstrap_create_admin(payload.username, payload.password)
    except UserStoreError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except ServerStateError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    # Admin angelegt — alter Token wird verbraucht, neuer fuer Vault-Unlock
    # generiert (rotiert; Server-Admin muss neu in die Logs schauen).
    server.invalidate_bootstrap_token()
    server._mint_bootstrap_token_if_needed()
    limiter.register_success(client_key)
    _audit_admin_created(payload.username)
    return {"status": server.bootstrap_status}


@router.post(
    "/vault",
    status_code=status.HTTP_200_OK,
)
def bootstrap_vault(
    payload: BootstrapVaultRequest,
    request: Request,
    server: ServerState = Depends(get_server_state),
    x_bootstrap_token: str | None = Header(None, alias="X-Bootstrap-Token"),
) -> dict[str, str]:
    """Entsperrt den zentralen Multi-User-Vault.

    Erfordert ``X-Bootstrap-Token`` aus dem Server-Log (Audit #5).
    Rate-Limit: 5 Versuche pro Stunde pro IP (Audit #4).
    """
    if server.bootstrap_status != "needs-vault-unlock":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Bootstrap-Vault nicht erlaubt — Server-Status: "
                f"{server.bootstrap_status}."
            ),
        )
    limiter = _bootstrap_limiter(request)
    client_key = _client_ip(request)
    remaining = limiter.check(client_key)
    if remaining is not None:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Zu viele Bootstrap-Versuche — {int(remaining) + 1} s warten.",
            headers={"Retry-After": str(int(remaining) + 1)},
        )
    try:
        _check_bootstrap_token(server, x_bootstrap_token)
    except HTTPException:
        limiter.register_failure(client_key)
        raise
    try:
        path = resolve_safe_vault_path(payload.vault_path)
    except VaultPathError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    if not path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Tresor-Datei nicht gefunden: {path}",
        )
    try:
        server.bootstrap_unlock_vault(path, payload.password)
    except InvalidPasswordError as exc:
        limiter.register_failure(client_key)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Master-Passwort falsch oder Tresor manipuliert.",
        ) from exc
    except (CorruptVaultError, VaultVersionError, VaultIOError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except VaultError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except ServerStateError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    server.invalidate_bootstrap_token()
    limiter.register_success(client_key)
    _audit_vault_bootstrap(path)
    return {"status": server.bootstrap_status}


# ---------------------------------------------------------------------------
# Audit-Helfer
# ---------------------------------------------------------------------------


def _audit_admin_created(username: str) -> None:
    get_audit_backend().append(
        AuditEventKind.USER_CREATED,
        summary=f"Bootstrap: Admin-User '{username}' angelegt.",
    )


def _audit_vault_bootstrap(path: Path) -> None:
    get_audit_backend().append(
        AuditEventKind.VAULT_OPENED,
        vault_path=str(path),
        summary=f"Bootstrap: zentraler Multi-User-Vault entsperrt ({path}).",
    )
