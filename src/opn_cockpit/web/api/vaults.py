"""Vault-Discovery + Anlegen + Export via Web-API."""

from __future__ import annotations

import contextlib
import tempfile
from dataclasses import replace
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from opn_cockpit.audit.backend import get_audit_backend
from opn_cockpit.audit.log import AuditEventKind
from opn_cockpit.config import AppSettings
from opn_cockpit.security.session import Session
from opn_cockpit.vault.discovery import default_new_vault_path, discover_vaults
from opn_cockpit.vault.errors import VaultError, VaultIOError, WeakPasswordError
from opn_cockpit.vault.model import VaultData
from opn_cockpit.vault.store import create_vault, open_vault
from opn_cockpit.web.api.schemas import (
    CreateVaultRequest,
    CreateVaultResponse,
    TemplateExportRequest,
    VaultEntry,
    VaultListResponse,
)
from opn_cockpit.web.auth.dependencies import get_session_manager, require_session
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
    token, session = manager.create(opened, path, payload.password)

    # App-Settings: erster Tresor wird Default + Recent-Eintrag.
    settings = AppSettings.load()
    settings.remember_vault(path)
    if settings.default_vault is None:
        settings.default_vault = str(path)
    # Settings-Persistenz ist nicht kritisch.
    with contextlib.suppress(OSError):
        settings.save()

    get_audit_backend().append(
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


# ---------------------------------------------------------------------------
# Export-Endpunkte
# ---------------------------------------------------------------------------


@router.get("/export/backup")
def export_backup(
    session: Session = Depends(require_session),
) -> FileResponse:
    """Lädt den aktiven Vault as-is als Download (für Backup-Zwecke).

    Die Datei ist mit dem Master-Passwort AES-256-GCM-verschluesselt —
    der Inhalt bleibt geheim, auch wenn der Download abgefangen wird.
    Backup somit sicher per E-Mail / Cloud-Storage weitergebbar (das
    Passwort bleibt natuerlich beim User).

    Im Multi-User-Mode gleiche Berechtigung wie das Vault-Lesen ueber-
    haupt: jeder eingeloggte User kann das Backup ziehen. Admin-Lock
    ist nicht noetig, weil die Datei nicht geheim ist (verschluesselt).
    """
    path = session.vault_path
    if path is None or not path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Aktiver Tresor nicht erreichbar.",
        )
    session.touch()
    get_audit_backend().append(
        AuditEventKind.TEMPLATE_EXPORTED,
        vault_path=str(path),
        summary=f"Backup-Download des Tresors: {path.name}",
    )
    return FileResponse(
        path=str(path),
        media_type="application/octet-stream",
        filename=path.name,
        headers={"Cache-Control": "no-store"},
    )


@router.post("/export/template")
def export_template(
    payload: TemplateExportRequest,
    session: Session = Depends(require_session),
) -> FileResponse:
    """Exportiert eine Template-Variante des Vaults — leere Credentials.

    Erzeugt eine neue .opnvault-Datei mit allen Geraete-Stammdaten
    (Name, Host, Port, TLS-Verify, Tags, Beschreibung), aber mit
    leeren ``api_key`` und ``api_secret``. Verschluesselt mit
    ``template_password``, das der Empfaenger zum Oeffnen braucht.

    Anwendungs-Fall: Inventar an einen anderen Admin weitergeben, der
    seine eigenen OPNsense-Credentials einsetzt.
    """
    if session.vault_path is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Aktiver Tresor nicht erreichbar.",
        )
    session.touch()
    opened = session.opened
    # Geraete-Liste mit leeren Secrets bauen
    blanked = [
        replace(d, api_key="", api_secret="")
        for d in opened.data.devices
    ]
    template_data = VaultData(
        schema_version=opened.data.schema_version,
        devices=blanked,
        settings=opened.data.settings,
    )

    # In Temp-Datei schreiben, dann als Download liefern.
    src_name = session.vault_path.stem
    with tempfile.NamedTemporaryFile(
        suffix=".opnvault", prefix=f"{src_name}-template-", delete=False,
    ) as tmp_fh:
        tmp_path = Path(tmp_fh.name)
    try:
        create_vault(tmp_path, payload.template_password, template_data, overwrite=True)
    except WeakPasswordError as exc:
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except VaultError as exc:
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    get_audit_backend().append(
        AuditEventKind.TEMPLATE_EXPORTED,
        vault_path=str(session.vault_path),
        summary=(
            f"Template-Export erstellt ({len(blanked)} Geraete, "
            "Credentials geleert)."
        ),
    )

    download_name = f"{src_name}-template.opnvault"

    # FileResponse mit Background-Task fuer Cleanup
    return FileResponse(
        path=str(tmp_path),
        media_type="application/octet-stream",
        filename=download_name,
        headers={"Cache-Control": "no-store"},
        background=BackgroundTask(_cleanup, tmp_path),
    )


def _cleanup(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.unlink()
