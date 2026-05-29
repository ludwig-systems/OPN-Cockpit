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

from fastapi import APIRouter, Depends, HTTPException, Request, status
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
from opn_cockpit.web.server_state import ServerState, ServerStateError

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
    server: ServerState = Depends(get_server_state),
) -> dict[str, str]:
    """Legt den ersten Admin an.

    Schlaegt mit 409 fehl, wenn der Server nicht im ``needs-admin``-Status
    ist (Single-Mode, bereits Admin angelegt, oder Vault schon entsperrt).
    """
    if server.bootstrap_status != "needs-admin":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Bootstrap-Admin nicht erlaubt — Server-Status: "
                f"{server.bootstrap_status}."
            ),
        )
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
    _audit_admin_created(payload.username)
    return {"status": server.bootstrap_status}


@router.post(
    "/vault",
    status_code=status.HTTP_200_OK,
)
def bootstrap_vault(
    payload: BootstrapVaultRequest,
    server: ServerState = Depends(get_server_state),
) -> dict[str, str]:
    """Entsperrt den zentralen Multi-User-Vault.

    Schlaegt mit 409 fehl, wenn der Server nicht im ``needs-vault-unlock``-
    Status ist. Bei falschem Passwort: 401. Bei kaputter/fehlender Datei:
    503/404.
    """
    if server.bootstrap_status != "needs-vault-unlock":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Bootstrap-Vault nicht erlaubt — Server-Status: "
                f"{server.bootstrap_status}."
            ),
        )
    path = Path(payload.vault_path)
    if not path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Tresor-Datei nicht gefunden: {path}",
        )
    try:
        server.bootstrap_unlock_vault(path, payload.password)
    except InvalidPasswordError as exc:
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
