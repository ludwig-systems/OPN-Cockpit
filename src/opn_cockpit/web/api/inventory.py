"""Inventar-Routen: Geraete listen, anlegen, loeschen, Heartbeat, Test-Connection.

Das Master-Passwort wird beim Unlock einmalig erfragt und in der Session
gecached — Schreibvorgaenge laufen ohne erneuten Prompt. Der Cache lebt
nur waehrend der Session und wird beim Lock/Auto-Lock geloescht.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status

from opn_cockpit.core.health import check_device, tcp_probe
from opn_cockpit.core.http_client import HttpClient, HttpTarget, HttpTuning
from opn_cockpit.inventory.model import Device
from opn_cockpit.security.session import Session
from opn_cockpit.vault.errors import (
    CorruptVaultError,
    SessionLockedError,
    VaultError,
    VaultIOError,
    VaultVersionError,
)
from opn_cockpit.vault.model import VaultDevice
from opn_cockpit.vault.store import save_vault
from opn_cockpit.web.api.schemas import (
    ConnectionTestResponse,
    DeviceCreateRequest,
    DeviceResponse,
    DeviceUpdateRequest,
    HeartbeatEntry,
    HeartbeatRequest,
    HeartbeatResponse,
    InventoryResponse,
    TagSummary,
)
from opn_cockpit.web.auth.dependencies import require_session

router = APIRouter(prefix="/api/inventory", tags=["inventory"])

HEARTBEAT_MAX_WORKERS = 16


# ---------------------------------------------------------------------------
# GET /api/inventory
# ---------------------------------------------------------------------------


@router.get("", response_model=InventoryResponse)
def list_inventory(session: Session = Depends(require_session)) -> InventoryResponse:
    """Liefert alle Geraete (ohne Secrets) und eine Tag-Summary."""
    devices = [Device.from_vault_device(d) for d in session.opened.data.devices]
    session.touch()
    return InventoryResponse(
        devices=[_to_device_response(d) for d in devices],
        tags=_aggregate_tags(devices),
    )


# ---------------------------------------------------------------------------
# POST /api/inventory/devices
# ---------------------------------------------------------------------------


@router.post(
    "/devices",
    response_model=DeviceResponse,
    status_code=status.HTTP_201_CREATED,
)
def add_device(
    payload: DeviceCreateRequest,
    session: Session = Depends(require_session),
) -> DeviceResponse:
    """Legt ein Geraet im Tresor an und persistiert."""
    vault_path = _require_vault_path(session)
    if _name_exists(session, payload.name):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Ein Geraet mit dem Namen '{payload.name}' existiert bereits.",
        )

    new_device = VaultDevice(
        id=VaultDevice.new_id(),
        name=payload.name,
        host=payload.host,
        port=payload.port,
        tls_verify=payload.tls_verify,
        tags=list(payload.tags),
        api_key=payload.api_key,
        api_secret=payload.api_secret,
        descr=payload.descr,
    )
    devices = session.opened.data.devices
    devices.append(new_device)

    def _rollback_add() -> None:
        devices.pop()

    _save_or_rollback(session, vault_path, rollback=_rollback_add)
    return _to_device_response(Device.from_vault_device(new_device))


# ---------------------------------------------------------------------------
# PATCH /api/inventory/devices/{device_id}
# ---------------------------------------------------------------------------


@router.patch("/devices/{device_id}", response_model=DeviceResponse)
def update_device(
    device_id: str,
    payload: DeviceUpdateRequest,
    session: Session = Depends(require_session),
) -> DeviceResponse:
    """Aktualisiert ausgewaehlte Felder eines Geraets und persistiert."""
    vault_path = _require_vault_path(session)
    devices = session.opened.data.devices
    index = next((i for i, d in enumerate(devices) if d.id == device_id), -1)
    if index < 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Geraet mit ID '{device_id}' nicht im Tresor.",
        )

    current = devices[index]

    # Namens-Eindeutigkeit pruefen, falls geaendert.
    if payload.name is not None and payload.name != current.name:
        new_name_lower = payload.name.strip().lower()
        for i, d in enumerate(devices):
            if i != index and d.name.strip().lower() == new_name_lower:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Ein anderes Geraet heisst bereits '{payload.name}'.",
                )

    # Snapshot fuer Rollback.
    snapshot = VaultDevice(
        id=current.id,
        name=current.name,
        host=current.host,
        port=current.port,
        tls_verify=current.tls_verify,
        tags=list(current.tags),
        api_key=current.api_key,
        api_secret=current.api_secret,
        descr=current.descr,
    )

    # In-place mutate. api_key/api_secret nur wenn explizit gesetzt + nicht leer.
    if payload.name is not None:
        current.name = payload.name
    if payload.host is not None:
        current.host = payload.host
    if payload.port is not None:
        current.port = payload.port
    if payload.tls_verify is not None:
        current.tls_verify = payload.tls_verify
    if payload.tags is not None:
        current.tags = list(payload.tags)
    if payload.descr is not None:
        current.descr = payload.descr
    if payload.api_key:
        current.api_key = payload.api_key
    if payload.api_secret:
        current.api_secret = payload.api_secret

    def _rollback_update() -> None:
        devices[index] = snapshot

    _save_or_rollback(session, vault_path, rollback=_rollback_update)
    return _to_device_response(Device.from_vault_device(current))


# ---------------------------------------------------------------------------
# DELETE /api/inventory/devices/{device_id}
# ---------------------------------------------------------------------------


@router.delete("/devices/{device_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_device(
    device_id: str,
    session: Session = Depends(require_session),
) -> None:
    """Entfernt ein Geraet aus dem Tresor."""
    vault_path = _require_vault_path(session)
    devices = session.opened.data.devices
    index = next((i for i, d in enumerate(devices) if d.id == device_id), -1)
    if index < 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Geraet mit ID '{device_id}' nicht im Tresor.",
        )
    backup = devices.pop(index)

    def _rollback_remove() -> None:
        devices.insert(index, backup)

    _save_or_rollback(session, vault_path, rollback=_rollback_remove)


# ---------------------------------------------------------------------------
# POST /api/inventory/heartbeat
# ---------------------------------------------------------------------------


@router.post("/heartbeat", response_model=HeartbeatResponse)
def heartbeat(
    payload: HeartbeatRequest,
    session: Session = Depends(require_session),
) -> HeartbeatResponse:
    """TCP-Probe gegen alle uebergebenen Geraete (oder alle im Tresor).

    Bewusst KEIN HTTP-Aufruf — der Heartbeat soll keine OPNsense-
    Auth-Logs erzeugen und keine Last verursachen.
    """
    devices = session.opened.data.devices
    if payload.device_ids:
        wanted = set(payload.device_ids)
        targets = [d for d in devices if d.id in wanted]
    else:
        targets = list(devices)

    if not targets:
        return HeartbeatResponse(results=[])

    timestamp = _iso_now()
    workers = min(HEARTBEAT_MAX_WORKERS, len(targets))

    def probe(vd: VaultDevice) -> HeartbeatEntry:
        ok = tcp_probe(vd.host, vd.port, timeout_s=payload.timeout_s)
        return HeartbeatEntry(device_id=vd.id, reachable=ok, checked_at_iso=timestamp)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(probe, targets))
    return HeartbeatResponse(results=results)


# ---------------------------------------------------------------------------
# POST /api/inventory/devices/{id}/test-connection
# ---------------------------------------------------------------------------


@router.post(
    "/devices/{device_id}/test-connection",
    response_model=ConnectionTestResponse,
)
def test_connection(
    device_id: str,
    session: Session = Depends(require_session),
) -> ConnectionTestResponse:
    """Vollwertiger HTTP-Auth-Probe gegen ein einzelnes Geraet."""
    vault_device = next(
        (d for d in session.opened.data.devices if d.id == device_id), None
    )
    if vault_device is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Geraet mit ID '{device_id}' nicht im Tresor.",
        )
    target = HttpTarget(
        host=vault_device.host,
        port=vault_device.port,
        verify=vault_device.tls_verify,
    )
    settings = session.opened.data.settings
    tuning = HttpTuning(
        connect_timeout_s=settings.connect_timeout_s,
        read_timeout_s=settings.read_timeout_s,
        reconfigure_timeout_s=settings.reconfigure_timeout_s,
        retry_count=settings.retry_count,
    )
    with HttpClient(targets=[target], tuning=tuning) as client:
        result = check_device(client, target, vault_device.api_key, vault_device.api_secret)
    session.touch()
    return ConnectionTestResponse(
        device_id=device_id,
        reachable=result.reachable,
        authenticated=result.authenticated,
        summary=result.summary,
    )


# ---------------------------------------------------------------------------
# Helfer
# ---------------------------------------------------------------------------


def _require_vault_path(session: Session) -> Path:
    vault_path = session.vault_path
    if vault_path is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Tresor-Pfad fehlt in der Session.",
        )
    return vault_path


def _save_or_rollback(
    session: Session,
    vault_path: Path,
    *,
    rollback: Callable[[], None],
) -> None:
    """Persistiert die aktuelle Session und rollt bei Fehlern zurueck."""
    try:
        password = session.master_password
    except SessionLockedError as exc:
        rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Session ohne Master-Passwort — bitte neu entsperren.",
        ) from exc
    try:
        new_opened = save_vault(vault_path, session.opened, password)
    except (CorruptVaultError, VaultVersionError, VaultIOError) as exc:
        rollback()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except VaultError as exc:
        rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    session.replace_opened(new_opened)


def _to_device_response(device: Device) -> DeviceResponse:
    return DeviceResponse(
        id=device.id,
        name=device.name,
        host=device.host,
        port=device.port,
        tls_verify=device.tls_verify,
        tags=list(device.tags),
        descr=device.descr,
    )


def _aggregate_tags(devices: list[Device]) -> list[TagSummary]:
    counts: dict[str, int] = {}
    for d in devices:
        for tag in d.tags:
            counts[tag] = counts.get(tag, 0) + 1
    return [TagSummary(name=t, count=c) for t, c in sorted(counts.items())]


def _name_exists(session: Session, name: str) -> bool:
    needle = name.strip().lower()
    return any(d.name.strip().lower() == needle for d in session.opened.data.devices)


def _iso_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


__all__ = ["router"]
