"""Bulk-Import-Routen: Firewall-Geraete via CSV oder JSON in den Tresor laden.

User waehlt eine Datei (CSV oder JSON), der Server parsed via ``importers.
csv_devices`` / ``importers.json_devices``, ergaenzt das Inventar im
entsperrten Tresor um die neuen Geraete und persistiert. Bei Parse-Fehlern:
400 mit Fehlerliste, kein Schreiben.

Auf bereits vorhandene Geraet-Namen reagieren wir mit Skip + Notiz im
Response — der User sieht, was uebersprungen wurde.
"""

from __future__ import annotations

import contextlib
import tempfile
from pathlib import Path
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
)

from opn_cockpit.importers.csv_devices import parse_devices_csv
from opn_cockpit.importers.json_devices import parse_devices_json
from opn_cockpit.inventory.model import Device
from opn_cockpit.security.session import Session
from opn_cockpit.vault.errors import (
    CorruptVaultError,
    InvalidPasswordError,
    VaultError,
    VaultIOError,
    VaultVersionError,
)
from opn_cockpit.vault.model import VaultDevice
from opn_cockpit.vault.store import open_vault, open_vault_bytes
from opn_cockpit.web.acl import require_write_role
from opn_cockpit.web.api.bootstrap import get_server_state
from opn_cockpit.web.api.schemas import (
    DeviceImportResponse,
    DeviceResponse,
    VaultImportRequest,
)
from opn_cockpit.web.auth.dependencies import require_session
from opn_cockpit.web.server_state import ServerState
from opn_cockpit.web.vault_path import VaultPathError, resolve_safe_vault_path
from opn_cockpit.web.vault_writes import persist_session_vault

router = APIRouter(prefix="/api/imports", tags=["imports"])

MAX_UPLOAD_BYTES = 2 * 1024 * 1024  # 2 MiB

# ---------------------------------------------------------------------------
# Beispieldateien (kein Login noetig — reine Vorlagen, kein Vault-Zugriff)
# ---------------------------------------------------------------------------

_EXAMPLE_CSV = (
    "# OPN-Cockpit Geraete-Import — CSV-Vorlage\n"
    "# Pflichtfelder: name, host, api_key, api_secret\n"
    "# Optional: port (Default 443), tls_verify (Default true),\n"
    "# tags (semikolon-getrennt — Komma kollidiert mit CSV), descr.\n"
    "# Kommentar-Zeilen beginnen mit '#'. Leere Zeilen werden ignoriert.\n"
    "name,host,port,tls_verify,tags,descr,api_key,api_secret\n"
    "HQ Berlin,opn-berlin.lab,443,true,branches;germany,Hauptsitz,KEY_BER,SECRET_BER\n"
    "Branch Muenchen,opn-muenchen.lab,443,false,branches;germany,Niederlassung,KEY_MUC,SECRET_MUC\n"
    "Lab,opn-lab.lab,8443,false,lab;test,Test-VM,KEY_LAB,SECRET_LAB\n"
)

_EXAMPLE_JSON = (
    "{\n"
    '  "_comment": "OPN-Cockpit Geraete-Import — JSON-Vorlage. Pflichtfelder: name, host, api_key, api_secret.",\n'
    '  "devices": [\n'
    "    {\n"
    '      "name": "HQ Berlin",\n'
    '      "host": "opn-berlin.lab",\n'
    '      "port": 443,\n'
    '      "tls_verify": true,\n'
    '      "tags": ["branches", "germany"],\n'
    '      "descr": "Hauptsitz",\n'
    '      "api_key": "KEY_BER",\n'
    '      "api_secret": "SECRET_BER"\n'
    "    },\n"
    "    {\n"
    '      "name": "Branch Muenchen",\n'
    '      "host": "opn-muenchen.lab",\n'
    '      "port": 443,\n'
    '      "tls_verify": false,\n'
    '      "tags": ["branches", "germany"],\n'
    '      "descr": "Niederlassung",\n'
    '      "api_key": "KEY_MUC",\n'
    '      "api_secret": "SECRET_MUC"\n'
    "    }\n"
    "  ]\n"
    "}\n"
)


@router.get("/examples/devices.csv", include_in_schema=False)
def example_devices_csv() -> "Response":
    """Liefert die CSV-Vorlage zum Download. Anonyme Endpoint, weil kein
    Tresor-Zugriff noetig."""
    from fastapi.responses import Response
    return Response(
        content=_EXAMPLE_CSV,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="devices-example.csv"',
        },
    )


@router.get("/examples/devices.json", include_in_schema=False)
def example_devices_json() -> "Response":
    """Liefert die JSON-Vorlage zum Download."""
    from fastapi.responses import Response
    return Response(
        content=_EXAMPLE_JSON,
        media_type="application/json; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="devices-example.json"',
        },
    )


@router.post(
    "/devices",
    response_model=DeviceImportResponse,
    status_code=status.HTTP_201_CREATED,
)
async def import_devices(
    request: Request,
    file: Annotated[UploadFile, File(description="CSV oder JSON mit Geraeten")],
    format: Annotated[str, Form(description="csv oder json")] = "csv",
    session: Session = Depends(require_session),
    server: ServerState = Depends(get_server_state),
) -> DeviceImportResponse:
    """Laedt Geraete aus einer Datei und fuegt sie dem Tresor hinzu."""
    require_write_role(session)
    vault_path = session.vault_path
    if vault_path is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Tresor-Pfad fehlt in der Session.",
        )

    fmt = format.lower().strip()
    if fmt not in ("csv", "json"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Format muss 'csv' oder 'json' sein.",
        )

    parsed_devices = await _parse_upload_or_400(file, fmt)
    # Audit #9: Filter + Save unter dem gleichen Lock im Multi-Mode.
    with server.vault_mutation_lock():
        to_add, skipped = _filter_new(session, parsed_devices)
        if not to_add:
            return DeviceImportResponse(
                added=[],
                skipped_existing=skipped,
                parsed_count=len(parsed_devices),
            )
        _persist_or_500(request, session, vault_path, to_add)
    return DeviceImportResponse(
        added=[_to_device_response(d) for d in to_add],
        skipped_existing=skipped,
        parsed_count=len(parsed_devices),
    )


# ---------------------------------------------------------------------------
# Helfer
# ---------------------------------------------------------------------------


async def _parse_upload_or_400(file: UploadFile, fmt: str) -> list[VaultDevice]:
    tmp_path = await _stage_upload(file, suffix=f".{fmt}")
    try:
        if fmt == "csv":
            r_csv = parse_devices_csv(tmp_path)
            parsed = r_csv.devices
            errors = r_csv.errors
        else:
            r_json = parse_devices_json(tmp_path)
            parsed = r_json.devices
            errors = r_json.errors
    finally:
        with contextlib.suppress(OSError):
            tmp_path.unlink()
    if errors:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "message": f"{fmt.upper()}-Parse-Fehler",
                "errors": errors,
                "parsed_count": len(parsed),
            },
        )
    if not parsed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"message": "Keine Geraete in der Datei gefunden.", "errors": []},
        )
    return parsed


def _filter_new(
    session: Session, parsed: list[VaultDevice],
) -> tuple[list[VaultDevice], list[str]]:
    existing = {d.name.strip().lower() for d in session.opened.data.devices}
    to_add: list[VaultDevice] = []
    skipped: list[str] = []
    for device in parsed:
        if device.name.strip().lower() in existing:
            skipped.append(device.name)
            continue
        to_add.append(device)
        existing.add(device.name.strip().lower())
    return to_add, skipped


def _persist_or_500(
    request: Request,
    session: Session,
    vault_path: Path,
    to_add: list[VaultDevice],
) -> None:
    devices_list = session.opened.data.devices
    snapshot_len = len(devices_list)
    devices_list.extend(to_add)

    def _rollback() -> None:
        del devices_list[snapshot_len:]

    persist_session_vault(request, session, vault_path, rollback=_rollback)


async def _stage_upload(file: UploadFile, *, suffix: str) -> Path:
    raw = await file.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Datei zu gross (>{MAX_UPLOAD_BYTES // 1024} KiB).",
        )
    with tempfile.NamedTemporaryFile(
        mode="wb", suffix=suffix, delete=False,
    ) as tmp:
        tmp.write(raw)
        tmp_name = tmp.name
    return Path(tmp_name)


def _to_device_response(vd: VaultDevice) -> DeviceResponse:
    d = Device.from_vault_device(vd)
    return DeviceResponse(
        id=d.id,
        name=d.name,
        host=d.host,
        port=d.port,
        tls_verify=d.tls_verify,
        tags=list(d.tags),
        descr=d.descr,
    )


@router.post(
    "/vault",
    response_model=DeviceImportResponse,
    status_code=status.HTTP_201_CREATED,
)
def import_from_vault(
    payload: VaultImportRequest,
    request: Request,
    session: Session = Depends(require_session),
    server: ServerState = Depends(get_server_state),
) -> DeviceImportResponse:
    """Geraete aus einer anderen .opnvault-Datei in den aktiven Vault uebernehmen.

    Heute der pragmatische Ersatz fuer einen kompletten Vault-Switch
    (Roadmap). Bestehende Geraete-Namen werden uebersprungen. Der Quell-
    Vault wird nur fuer den Lese-Vorgang entsperrt und sofort wieder
    aus dem Speicher entfernt — Original-Datei bleibt unangetastet.
    """
    require_write_role(session)
    vault_path = session.vault_path
    if vault_path is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Tresor-Pfad fehlt in der Session.",
        )
    try:
        source = resolve_safe_vault_path(payload.source_path)
    except VaultPathError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    if not source.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Quell-Tresor nicht gefunden: {source}",
        )
    if source.resolve() == vault_path.resolve():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Quell-Tresor ist identisch mit dem aktiven Tresor.",
        )
    try:
        source_opened = open_vault(source, payload.source_password)
    except InvalidPasswordError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Master-Passwort des Quell-Tresors falsch.",
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

    source_devices = list(source_opened.data.devices)
    # Quell-Vault loslassen — kein Caching von fremden Credentials.
    del source_opened

    with server.vault_mutation_lock():
        to_add, skipped = _filter_new(session, source_devices)
        if not to_add:
            return DeviceImportResponse(
                added=[],
                skipped_existing=skipped,
                parsed_count=len(source_devices),
            )
        _persist_or_500(request, session, vault_path, to_add)
    return DeviceImportResponse(
        added=[_to_device_response(d) for d in to_add],
        skipped_existing=skipped,
        parsed_count=len(source_devices),
    )


@router.post(
    "/vault-upload",
    response_model=DeviceImportResponse,
    status_code=status.HTTP_201_CREATED,
)
async def import_from_uploaded_vault(
    request: Request,
    file: Annotated[UploadFile, File(description="Quell-Vault als Upload aus dem Browser")],
    password: Annotated[str, Form(description="Master-Passwort des Quell-Vaults")],
    session: Session = Depends(require_session),
    server: ServerState = Depends(get_server_state),
) -> DeviceImportResponse:
    """Geraete aus einer hochgeladenen .opnvault-Datei in den aktiven Vault uebernehmen.

    Wichtiger Unterschied zum aelteren ``/vault``-Endpoint: hier landet die
    Datei als Upload — der Server muss keinen User-Pfad lesen. Funktioniert
    auch im Multi-User-Server-Mode, in dem der LocalService-Account keine
    Sicht auf C:\\Users\\... hat (Audit-Findung F27).

    Quell-Vault wird nur fuer den Lese-Vorgang entsperrt und sofort wieder
    freigegeben — Bytes leben nur als lokale Variable.
    """
    require_write_role(session)
    vault_path = session.vault_path
    if vault_path is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Tresor-Pfad fehlt in der Session.",
        )
    raw = await file.read()
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Hochgeladene Vault-Datei ist leer.",
        )
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Datei zu gross (>{MAX_UPLOAD_BYTES // 1024} KiB).",
        )
    try:
        source_opened = open_vault_bytes(raw, password)
    except InvalidPasswordError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Master-Passwort des Quell-Tresors falsch.",
        ) from exc
    except (CorruptVaultError, VaultVersionError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Datei ist kein gueltiger Vault: {exc}",
        ) from exc
    except VaultError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    source_devices = list(source_opened.data.devices)
    del source_opened
    del raw

    with server.vault_mutation_lock():
        to_add, skipped = _filter_new(session, source_devices)
        if not to_add:
            return DeviceImportResponse(
                added=[],
                skipped_existing=skipped,
                parsed_count=len(source_devices),
            )
        _persist_or_500(request, session, vault_path, to_add)
    return DeviceImportResponse(
        added=[_to_device_response(d) for d in to_add],
        skipped_existing=skipped,
        parsed_count=len(source_devices),
    )


__all__ = ["router"]
