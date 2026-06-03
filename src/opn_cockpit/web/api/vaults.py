"""Vault-Discovery + Anlegen + Export via Web-API."""

from __future__ import annotations

import contextlib
import tempfile
from dataclasses import replace
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from opn_cockpit.audit.backend import audit_actor, get_audit_backend
from opn_cockpit.audit.log import AuditEventKind
from opn_cockpit.config import AppSettings
from opn_cockpit.security.session import Session
from opn_cockpit.vault.discovery import (
    default_new_vault_path,
    default_vault_basename,
    discover_vaults,
    suggested_vault_locations,
)
from opn_cockpit.vault.errors import (
    CorruptVaultError,
    InvalidPasswordError,
    VaultError,
    VaultIOError,
    VaultVersionError,
    WeakPasswordError,
)
from opn_cockpit.vault.model import VaultData, VaultSettings
from opn_cockpit.vault.store import change_password as vault_change_password
from opn_cockpit.vault.store import create_vault, open_vault
from opn_cockpit.web.api.bootstrap import get_server_state
from opn_cockpit.web.api.schemas import (
    ChangeVaultPasswordRequest,
    CreateVaultRequest,
    CreateVaultResponse,
    PathSuggestion,
    TemplateExportRequest,
    VaultEntry,
    VaultListResponse,
    VaultSettingsResponse,
    VaultSettingsUpdateRequest,
    VaultSwitchRequest,
)
from opn_cockpit.web.auth.dependencies import (
    get_session_manager,
    require_admin,
    require_session,
    require_session_with_token,
)
from opn_cockpit.web.auth.manager import SessionManager
from opn_cockpit.web.server_state import ServerState, ServerStateError
from opn_cockpit.web.vault_path import VaultPathError, resolve_safe_vault_path
from opn_cockpit.web.vault_writes import persist_session_vault

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
    suggestions = [
        PathSuggestion(label=label, path=str(directory))
        for label, directory in suggested_vault_locations()
    ]
    default_full = default_new_vault_path()
    return VaultListResponse(
        vaults=entries,
        suggested_new_path=str(default_full),
        suggested_new_directory=str(default_full.parent),
        suggested_new_name=default_vault_basename(),
        path_suggestions=suggestions,
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
        vault_path=str(path),
        vault_filename=path.name,
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
        actor=audit_actor(session),
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
        actor=audit_actor(session),
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


# ---------------------------------------------------------------------------
# Admin: Vault wechseln (Multi-User-Mode)
# ---------------------------------------------------------------------------


@router.post("/switch", status_code=status.HTTP_200_OK)
def switch_vault(
    payload: VaultSwitchRequest,
    request: Request,
    server: ServerState = Depends(get_server_state),
    admin: Session = Depends(require_admin),
    pair: tuple[Session, str] = Depends(require_session_with_token),
    manager: SessionManager = Depends(get_session_manager),
) -> dict[str, str]:
    """Admin wechselt den aktiven Multi-User-Vault ohne Server-Restart.

    Aktive Sessions anderer User werden invalidiert (Token gesperrt) —
    die Browser-Tabs landen beim naechsten Request auf dem Login-Screen
    zurueck. Der Admin behaelt seinen Token; seine Session zeigt nach
    dem Switch auf den neuen Vault.

    Im Single-Mode nicht verfuegbar (Single-User-Sessions haben jeweils
    ihren eigenen Vault — Logout + Vault-Picker reicht).
    """
    if not server.is_multi_user_mode:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Vault-Switch ist nur im Multi-User-Mode verfuegbar. "
                "Im Single-Mode: Logout und anderen Tresor waehlen."
            ),
        )
    _admin_session, admin_token = pair
    try:
        new_path = resolve_safe_vault_path(payload.vault_path)
    except VaultPathError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    # Self-Check: gleicher Pfad wie aktiver -> nix tun
    if server.vault_path and new_path.resolve() == server.vault_path.resolve():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Neuer Pfad ist identisch mit dem aktiven Tresor.",
        )
    if not new_path.exists() and not payload.create_if_missing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"Tresor-Datei nicht gefunden: {new_path}. "
                "Setze create_if_missing=true, um einen neuen anzulegen."
            ),
        )
    try:
        created = server.switch_vault(
            new_path, payload.password,
            create_if_missing=payload.create_if_missing,
        )
    except InvalidPasswordError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Master-Passwort des neuen Tresors falsch.",
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

    # Andere Sessions invalidieren — die Admin-Session behalten + auf neuen Vault zeigen.
    revoked = manager.revoke_all_except(admin_token)
    # Admin-Session selbst aktualisieren (Vault-Pfad + opened-Referenz).
    new_opened = server._opened_vault
    if new_opened is not None:
        admin.replace_opened(new_opened)
        # Session-internen vault_path-Slot ueberschreiben — replace_opened
        # taucht den vault_path nicht an, also rufen wir unlock() neu auf
        # mit dem neuen Pfad. Sauberer als private Slots zu pokern.
        admin.unlock(new_opened, new_path, payload.password, user=admin.user)
    # Retry-Watcher fuer revoked Sessions wurden in revoke nicht abgebrochen —
    # der einfacheren Implementierung halber sammeln wir das nicht. Watcher
    # bemerkt fehlende Session beim naechsten Tick und beendet sich.

    get_audit_backend().append(
        AuditEventKind.VAULT_OPENED,
        actor=audit_actor(admin),
        vault_path=str(new_path),
        summary=(
            f"Vault-Switch durchgefuehrt: {new_path} "
            f"({'neu angelegt' if created else 'entsperrt'}, "
            f"{revoked} Session(s) invalidiert)."
        ),
    )

    return {
        "status": server.bootstrap_status,
        "created": "true" if created else "false",
        "revoked_sessions": str(revoked),
    }


# ---------------------------------------------------------------------------
# Settings (F5b) + Change-Password (F5a) fuer den aktiven Tresor
# ---------------------------------------------------------------------------


@router.get("/settings", response_model=VaultSettingsResponse)
def get_vault_settings(
    session: Session = Depends(require_session),
) -> VaultSettingsResponse:
    """Liefert die Tresor-eigenen Settings (Inaktivitaets-Timeout etc.) fuer
    das Settings-Modal."""
    s = session.opened.data.settings
    return VaultSettingsResponse(
        inactivity_minutes=int(s.inactivity_minutes),
        max_workers=int(s.max_workers),
        auto_backup_before_apply=bool(s.auto_backup_before_apply),
        backup_retention_pre_apply=int(s.backup_retention_pre_apply),
        backup_retention_scheduled=int(s.backup_retention_scheduled),
        scheduled_backup_enabled=bool(s.scheduled_backup_enabled),
        scheduled_backup_interval_hours=int(s.scheduled_backup_interval_hours),
        drift_detection_enabled=bool(s.drift_detection_enabled),
    )


@router.post("/settings", response_model=VaultSettingsResponse)
def update_vault_settings(
    payload: VaultSettingsUpdateRequest,
    request: Request,
    session: Session = Depends(require_session),
) -> VaultSettingsResponse:
    """Aktualisiert die Tresor-Settings und persistiert.

    Pydantic prueft die Wertebereiche schon. Felder die None sind, bleiben
    auf ihrem alten Wert. Schreibreihenfolge wichtig: erst mutieren, dann
    persistieren - bei Save-Fehler faellt der Rollback die In-Memory-
    Aenderung zurueck.
    """
    if session.vault_path is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Aktiver Tresor-Pfad fehlt — bitte neu entsperren.",
        )
    old_settings = session.opened.data.settings
    new_settings = VaultSettings(
        inactivity_minutes=payload.inactivity_minutes,
        max_workers=old_settings.max_workers,
        connect_timeout_s=old_settings.connect_timeout_s,
        read_timeout_s=old_settings.read_timeout_s,
        reconfigure_timeout_s=old_settings.reconfigure_timeout_s,
        retry_count=old_settings.retry_count,
        auto_backup_before_apply=(
            payload.auto_backup_before_apply
            if payload.auto_backup_before_apply is not None
            else old_settings.auto_backup_before_apply
        ),
        backup_retention_pre_apply=(
            payload.backup_retention_pre_apply
            if payload.backup_retention_pre_apply is not None
            else old_settings.backup_retention_pre_apply
        ),
        backup_retention_scheduled=(
            payload.backup_retention_scheduled
            if payload.backup_retention_scheduled is not None
            else old_settings.backup_retention_scheduled
        ),
        scheduled_backup_enabled=(
            payload.scheduled_backup_enabled
            if payload.scheduled_backup_enabled is not None
            else old_settings.scheduled_backup_enabled
        ),
        scheduled_backup_interval_hours=(
            payload.scheduled_backup_interval_hours
            if payload.scheduled_backup_interval_hours is not None
            else old_settings.scheduled_backup_interval_hours
        ),
        drift_detection_enabled=(
            payload.drift_detection_enabled
            if payload.drift_detection_enabled is not None
            else old_settings.drift_detection_enabled
        ),
    )
    session.opened.data.settings = new_settings

    def _rollback() -> None:
        session.opened.data.settings = old_settings

    persist_session_vault(request, session, session.vault_path, rollback=_rollback)

    get_audit_backend().append(
        AuditEventKind.VAULT_OPENED,
        actor=audit_actor(session),
        vault_path=str(session.vault_path),
        summary=(
            f"Tresor-Settings aktualisiert: "
            f"inactivity={new_settings.inactivity_minutes}min, "
            f"auto_backup={new_settings.auto_backup_before_apply}"
        ),
    )
    return VaultSettingsResponse(
        inactivity_minutes=int(new_settings.inactivity_minutes),
        max_workers=int(new_settings.max_workers),
        auto_backup_before_apply=bool(new_settings.auto_backup_before_apply),
        backup_retention_pre_apply=int(new_settings.backup_retention_pre_apply),
        backup_retention_scheduled=int(new_settings.backup_retention_scheduled),
        scheduled_backup_enabled=bool(new_settings.scheduled_backup_enabled),
        scheduled_backup_interval_hours=int(new_settings.scheduled_backup_interval_hours),
        drift_detection_enabled=bool(new_settings.drift_detection_enabled),
    )


@router.post("/change-password", status_code=status.HTTP_200_OK)
def change_vault_password(
    payload: ChangeVaultPasswordRequest,
    session: Session = Depends(require_session),
) -> dict[str, str]:
    """Aendert das Master-Passwort des aktiven Tresors.

    Verlangt das aktuelle Passwort als Bestaetigung (Schutz vor Hijack
    durch offene Browser-Session). Bei Erfolg wird die Session unter dem
    neuen Passwort weitergefuehrt; Tabs muessen nicht neu einloggen.
    """
    if session.vault_path is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Aktiver Tresor-Pfad fehlt — bitte Tresor erneut entsperren.",
        )
    if payload.new_password != payload.new_password_repeat:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Die beiden Eingaben fuer das neue Passwort stimmen nicht ueberein.",
        )
    if payload.current_password == payload.new_password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Neues Passwort darf nicht mit dem aktuellen identisch sein.",
        )
    try:
        new_opened = vault_change_password(
            session.vault_path,
            payload.current_password,
            payload.new_password,
        )
    except InvalidPasswordError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Das aktuelle Master-Passwort ist falsch.",
        ) from exc
    except WeakPasswordError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except VaultError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    # Session unter dem neuen Passwort weiterfuehren — sonst schlagen
    # spaetere Schreibvorgaenge fehl (master_password-Cache war alt).
    session.unlock(
        new_opened,
        session.vault_path,
        password=payload.new_password,
        user=session.user,
    )
    get_audit_backend().append(
        AuditEventKind.VAULT_OPENED,
        actor=audit_actor(session),
        vault_path=str(session.vault_path),
        summary="Tresor-Master-Passwort geaendert.",
    )
    return {"status": "ok"}
