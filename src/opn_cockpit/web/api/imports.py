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
from opn_cockpit.vault.model import VaultDevice
from opn_cockpit.web.api.schemas import (
    DeviceImportResponse,
    DeviceResponse,
)
from opn_cockpit.web.auth.dependencies import require_session
from opn_cockpit.web.vault_writes import persist_session_vault

router = APIRouter(prefix="/api/imports", tags=["imports"])

MAX_UPLOAD_BYTES = 2 * 1024 * 1024  # 2 MiB


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
) -> DeviceImportResponse:
    """Laedt Geraete aus einer Datei und fuegt sie dem Tresor hinzu."""
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


__all__ = ["router"]
