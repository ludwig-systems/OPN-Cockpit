"""Vault-Discovery + Anlegen via Web-API."""

from __future__ import annotations

import contextlib
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status

from opn_cockpit.audit.log import AuditEventKind, AuditLog, default_audit_path
from opn_cockpit.config import AppSettings
from opn_cockpit.vault.discovery import default_new_vault_path, discover_vaults
from opn_cockpit.vault.errors import VaultError, VaultIOError, WeakPasswordError
from opn_cockpit.vault.store import create_vault, open_vault
from opn_cockpit.web.api.schemas import (
    CreateVaultRequest,
    CreateVaultResponse,
    VaultEntry,
    VaultListResponse,
)
from opn_cockpit.web.auth.dependencies import get_session_manager
from opn_cockpit.web.auth.manager import SessionManager

router = APIRouter(prefix="/api/vaults", tags=["vaults"])


@router.get("", response_model=VaultListResponse)
def list_vaults() -> VaultListResponse:
    """Liefert alle bekannten Tresor-Dateien fuer das Login-Dropdown."""
    settings = AppSettings.load()
    discovered = discover_vaults(settings)
    default = settings.default_vault
    entries: list[VaultEntry] = []
    for p in discovered:
        entries.append(
            VaultEntry(
                path=str(p),
                filename=p.name,
                is_default=(default is not None and str(p) == default),
            )
        )
    return VaultListResponse(
        vaults=entries,
        suggested_new_path=str(default_new_vault_path()),
    )


@router.post(
    "",
    response_model=CreateVaultResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_new_vault(
    payload: CreateVaultRequest,
    manager: SessionManager = Depends(get_session_manager),
) -> CreateVaultResponse:
    """Legt einen frischen Tresor an und entsperrt ihn direkt.

    Die Auto-Unlock-Logik spart dem User die zweite Passwort-Eingabe
    nach dem Anlegen. Der Client kriegt sofort ein Bearer-Token zurueck
    und kann zur Hauptansicht navigieren.
    """
    path = Path(payload.path)
    if path.exists():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Datei existiert bereits: {path}",
        )
    try:
        create_vault(path, payload.password)
    except WeakPasswordError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc),
        ) from exc
    except VaultIOError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc),
        ) from exc
    except VaultError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc),
        ) from exc

    # Sofort entsperren — der User hat das Passwort gerade getippt.
    opened = open_vault(path, payload.password)
    token, session = manager.create(opened, path)

    # App-Settings: erster Tresor wird Default + Recent-Eintrag.
    settings = AppSettings.load()
    settings.remember_vault(path)
    if settings.default_vault is None:
        settings.default_vault = str(path)
    # Settings-Persistenz ist nicht kritisch.
    with contextlib.suppress(OSError):
        settings.save()

    AuditLog(path=default_audit_path()).append(
        AuditEventKind.VAULT_CREATED,
        vault_path=str(path),
        summary=f"Tresor angelegt (Web): {path}",
    )
    return CreateVaultResponse(
        path=str(path),
        filename=path.name,
        token=token,
        inactivity_timeout_s=int(session.inactivity_timeout_s),
        seconds_until_expiry=int(session.seconds_until_expiry()),
    )
