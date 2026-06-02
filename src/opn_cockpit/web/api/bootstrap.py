"""Bootstrap-Endpunkte fuer den Multi-User-Server-Mode.

Seit F28 (2026-06-01) gibt's keine Bootstrap-Token-Mechanik mehr.
Stattdessen:

* Server legt beim Erststart einen Default-Admin an
  (``admin`` / ``OPN-Cockpit!``) mit Pflicht-PW-Wechsel.
* User loggt sich darueber via normales ``POST /api/auth/login`` ein,
  wechselt das Passwort via ``POST /api/users/me/password``.
* ``POST /api/bootstrap/vault`` braucht jetzt eine echte Admin-Session
  (Bearer-Token statt X-Bootstrap-Token) — pragmatisch wie Proxmox.

``POST /api/bootstrap/admin`` bleibt nur noch als 410-Gone-Erinnerung
fuer altes Frontend / CLI bestehen.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, Field

from opn_cockpit.audit.backend import get_audit_backend
from opn_cockpit.audit.log import AuditEventKind
from opn_cockpit.security.session import Session
from opn_cockpit.security.users import UserStoreError  # noqa: F401  (used by legacy 410 path)
from opn_cockpit.vault.errors import (
    CorruptVaultError,
    InvalidPasswordError,
    VaultError,
    VaultIOError,
    VaultVersionError,
)
from opn_cockpit.web.auth.dependencies import (
    get_session_manager,
    require_admin,
)
from opn_cockpit.web.auth.manager import SessionManager
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

    ``admin_requires_password_change`` ist True solange der Default-Admin
    noch sein Initial-Passwort traegt. Das Frontend nutzt das, um die
    "Neues Admin-Passwort"-Felder nur beim Erst-Setup einzublenden und
    nicht bei jedem Restart-Vault-Unlock erneut zu fordern.
    """

    mode: str  # 'vault' (single-user) | 'user-db' (multi-user)
    status: str  # 'single-user' | 'needs-admin' | 'needs-vault-unlock' | 'ready'
    suggested_vault_path: str | None = None
    admin_requires_password_change: bool = False


class BootstrapAdminRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=120)
    password: str = Field(..., min_length=12)


class BootstrapVaultRequest(BaseModel):
    """Bootstrap-Vault — Erst-Setup-Wizard nach Default-Admin-Anlage.

    Enthaelt:

    * ``admin_username`` / ``admin_password`` — User-DB-Login. Default-
      Admin ist `admin` / `OPN-Cockpit!`, vor Vault-Unlock gibt's noch
      keine Session, wir authentifizieren daher direkt.
    * ``new_admin_password`` — pflichtfeld wenn das Admin-Konto noch das
      Default-Passwort traegt (must_change_password=True). Optional sonst.
    * ``vault_path`` / ``vault_password`` — der zentrale Multi-User-Vault.
      ``create_if_missing=True`` legt einen leeren neuen Vault an.
    """

    vault_path: str = Field(..., min_length=1)
    password: str = Field(..., min_length=12)
    create_if_missing: bool = Field(False)
    admin_username: str = Field(..., min_length=1)
    admin_password: str = Field(..., min_length=1)
    new_admin_password: str | None = Field(None, min_length=12)


class BootstrapVaultResponse(BaseModel):
    """Antwort auf ``POST /api/bootstrap/vault``.

    Enthaelt jetzt eine fertige Session — der Wizard hat den Admin
    schon per Username + Passwort + Master-PW autorisiert, ein
    direkt anschliessender Multi-User-Login waere redundant.

    Frontend nimmt ``token`` + ``vault_path`` etc. wie eine normale
    ``UnlockResponse`` und springt direkt in den Main-View.

    ``token`` und die anschliessenden Felder sind optional, damit
    die Antwort auch dann ein Schema hat, wenn die Session-Erzeugung
    aus irgendeinem Grund nicht klappt — Frontend faellt dann auf
    den expliziten Login-Pfad zurueck.
    """

    status: str
    created: str
    token: str | None = None
    vault_path: str | None = None
    vault_filename: str | None = None
    inactivity_timeout_s: int | None = None
    seconds_until_expiry: int | None = None


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
    admin_must_change = False
    if server.is_multi_user_mode and server._user_store is not None:
        from opn_cockpit.web.server_state import DEFAULT_ADMIN_USERNAME
        admin = server._user_store.get_user_by_name(DEFAULT_ADMIN_USERNAME)
        admin_must_change = bool(admin and admin.must_change_password)
    return BootstrapStatusResponse(
        mode=server.settings.auth_backend,
        status=server.bootstrap_status,
        suggested_vault_path=server.suggested_vault_path,
        admin_requires_password_change=admin_must_change,
    )


@router.post(
    "/admin",
    status_code=status.HTTP_410_GONE,
    include_in_schema=False,
)
def bootstrap_admin_gone() -> dict[str, str]:
    """Legacy-Endpoint — seit F28 nicht mehr verwendet.

    Server legt den Default-Admin (`admin` / `OPN-Cockpit!`) automatisch
    an. Wer hier landet, hat ein veraltetes Frontend; Antwort 410 macht
    das offensichtlich.
    """
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail=(
            "Bootstrap-Admin-Endpoint wurde entfernt. Default-Admin "
            "'admin' (PW 'OPN-Cockpit!') wird beim Server-Start automatisch "
            "angelegt; bitte ueber /api/auth/login einloggen."
        ),
    )


@router.post(
    "/vault",
    response_model=BootstrapVaultResponse,
    status_code=status.HTTP_200_OK,
)
def bootstrap_vault(
    payload: BootstrapVaultRequest,
    request: Request,
    server: ServerState = Depends(get_server_state),
    manager: SessionManager = Depends(get_session_manager),
) -> BootstrapVaultResponse:
    """Entsperrt den zentralen Multi-User-Vault.

    Seit F28: Endpoint pruef ``admin_username`` + ``admin_password`` direkt
    gegen die User-DB (Henne-Ei: vor Vault-Unlock gibt's keine Session).
    User muss Admin-Rolle haben, und seit F28 darf er kein
    ``must_change_password``-Flag mehr haben — sprich, das Default-PW
    muss vorher gewechselt sein. Rate-Limit auf IP bleibt.
    """
    if server.bootstrap_status != "needs-vault-unlock":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Bootstrap-Vault nicht erlaubt — Server-Status: "
                f"{server.bootstrap_status}."
            ),
        )
    if server.user_store is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="User-Store nicht initialisiert.",
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
    # Direkter Username/Passwort-Check gegen die User-DB.
    admin_user = server.user_store.authenticate(payload.admin_username, payload.admin_password)
    if admin_user is None or admin_user.role != "admin":
        limiter.register_failure(client_key)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Admin-Login fehlgeschlagen (User unbekannt, Passwort falsch oder keine Admin-Rolle).",
        )
    if admin_user.must_change_password:
        if not payload.new_admin_password:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    "Default-Admin-Passwort muss in diesem Schritt gewechselt werden. "
                    "Feld 'new_admin_password' (min. 12 Zeichen) mitschicken."
                ),
            )
        if payload.new_admin_password == payload.admin_password:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Neues Admin-Passwort darf nicht mit dem Default identisch sein.",
            )
        try:
            server.user_store.change_password(admin_user.id, payload.new_admin_password)
        except UserStoreError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"PW-Wechsel fehlgeschlagen: {exc}",
            ) from exc
    try:
        path = resolve_safe_vault_path(payload.vault_path)
    except VaultPathError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    if not path.exists() and not payload.create_if_missing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"Tresor-Datei nicht gefunden: {path}. "
                "Setze create_if_missing=true, um einen neuen anzulegen."
            ),
        )
    try:
        created = server.bootstrap_unlock_vault(
            path, payload.password,
            create_if_missing=payload.create_if_missing,
        )
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
    if created:
        _audit_vault_created(path)
    else:
        _audit_vault_bootstrap(path)
    # Direkt eine Session fuer den Admin anlegen, der gerade autorisiert hat -
    # spart dem User den separaten Multi-User-Login Sekunden spaeter. Der
    # Server hat hier alle drei Faktoren bekommen: User-DB-Login + Master-PW +
    # (optional) neues Admin-PW. Quaequivalent zu einem expliziten Login.
    token: str | None = None
    vault_filename: str | None = None
    inactivity_timeout_s: int | None = None
    seconds_until_expiry: int | None = None
    try:
        effective_pw = payload.new_admin_password or payload.admin_password
        backend = server.auth_backend()
        auth_result = backend.authenticate({
            "username": payload.admin_username,
            "password": effective_pw,
        })
        if auth_result is not None:
            token, session = manager.create_from(auth_result)
            vault_filename = auth_result.vault_path.name
            inactivity_timeout_s = int(session.inactivity_timeout_s)
            seconds_until_expiry = int(session.seconds_until_expiry())
    except Exception:  # noqa: BLE001 - Session-Erzeugung darf den Bootstrap-Erfolg nie aushebeln
        # Fallback: kein Token mit zurueck, Frontend zeigt regulaeren Login.
        # Wir lassen das Unlock-Ergebnis stehen, der Vault ist offen.
        token = None
    return BootstrapVaultResponse(
        status=server.bootstrap_status,
        created="true" if created else "false",
        token=token,
        vault_path=str(path) if token else None,
        vault_filename=vault_filename,
        inactivity_timeout_s=inactivity_timeout_s,
        seconds_until_expiry=seconds_until_expiry,
    )


# ---------------------------------------------------------------------------
# Audit-Helfer
# ---------------------------------------------------------------------------


def _audit_admin_created(username: str) -> None:
    get_audit_backend().append(
        AuditEventKind.USER_CREATED,
        actor=username,
        summary=f"Bootstrap: Admin-User '{username}' angelegt.",
    )


def _audit_vault_bootstrap(path: Path) -> None:
    get_audit_backend().append(
        AuditEventKind.VAULT_OPENED,
        vault_path=str(path),
        summary=f"Bootstrap: zentraler Multi-User-Vault entsperrt ({path}).",
    )


def _audit_vault_created(path: Path) -> None:
    get_audit_backend().append(
        AuditEventKind.VAULT_CREATED,
        vault_path=str(path),
        summary=f"Bootstrap: neuer Multi-User-Vault angelegt ({path}).",
    )
