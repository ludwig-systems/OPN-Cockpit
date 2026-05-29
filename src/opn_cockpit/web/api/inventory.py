"""Inventar-Routen: Geraete listen, anlegen, loeschen, Heartbeat, Test-Connection.

Schreibvorgaenge verlangen das Master-Passwort im Body — gleicher
Sicherheits-Pakt wie CLI/GUI: der Server haelt das Passwort nicht
laenger als die Dauer eines einzelnen Aufrufs.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status

from opn_cockpit.core.health import check_device, tcp_probe
from opn_cockpit.core.http_client import HttpClient, HttpTarget, HttpTuning
from opn_cockpit.inventory.model import Device
from opn_cockpit.security.session import Session
from opn_cockpit.vault.errors import (
    CorruptVaultError,
    InvalidPasswordError,
    VaultError,
    VaultIOError,
    VaultVersionError,
    WeakPasswordError,
)
from opn_cockpit.vault.model import VaultDevice
from opn_cockpit.vault.store import open_vault, save_vault
from opn_cockpit.web.api.schemas import (
    ConnectionTestResponse,
    DeviceCreateRequest,
    DeviceDeleteRequest,
    DeviceResponse,
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
    vault_path = session.vault_path
    if vault_path is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Tresor-Pfad fehlt in der Session.",
        )
    if _name_exists(session, payload.name):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Ein Geraet mit dem Namen '{payload.name}' existiert bereits.",
        )

    # Passwort vor dem Schreiben validieren — sonst koennte ein Tippfehler
    # den Tresor mit einem neuen Key ueberschreiben und den Original-Owner
    # aussperren. save_vault selbst prueft das nicht.
    try:
        open_vault(vault_path, payload.master_password)
    except InvalidPasswordError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Master-Passwort falsch — Aenderung nicht gespeichert.",
        ) from exc

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
    session.opened.data.devices.append(new_device)
    try:
        new_opened = save_vault(vault_path, session.opened, payload.master_password)
    except InvalidPasswordError as exc:
        session.opened.data.devices.pop()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Master-Passwort falsch — Aenderung nicht gespeichert.",
        ) from exc
    except WeakPasswordError as exc:
        session.opened.data.devices.pop()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except (CorruptVaultError, VaultVersionError, VaultIOError) as exc:
        session.opened.data.devices.pop()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except VaultError as exc:
        session.opened.data.devices.pop()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    session.replace_opened(new_opened)
    return _to_device_response(Device.from_vault_device(new_device))


# ---------------------------------------------------------------------------
# DELETE /api/inventory/devices/{device_id}
# ---------------------------------------------------------------------------


@router.delete("/devices/{device_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_device(
    device_id: str,
    payload: DeviceDeleteRequest,
    session: Session = Depends(require_session),
) -> None:
    """Entfernt ein Geraet aus dem Tresor."""
    vault_path = session.vault_path
    if vault_path is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Tresor-Pfad fehlt in der Session.",
        )
    devices = session.opened.data.devices
    index = next((i for i, d in enumerate(devices) if d.id == device_id), -1)
    if index < 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Geraet mit ID '{device_id}' nicht im Tresor.",
        )

    # Passwort vor dem Schreiben verifizieren, gleiche Begruendung wie in add_device.
    try:
        open_vault(vault_path, payload.master_password)
    except InvalidPasswordError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Master-Passwort falsch — Aenderung nicht gespeichert.",
        ) from exc

    backup = devices.pop(index)
    try:
        new_opened = save_vault(vault_path, session.opened, payload.master_password)
    except InvalidPasswordError as exc:
        devices.insert(index, backup)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Master-Passwort falsch — Aenderung nicht gespeichert.",
        ) from exc
    except (CorruptVaultError, VaultVersionError, VaultIOError) as exc:
        devices.insert(index, backup)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except VaultError as exc:
        devices.insert(index, backup)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    session.replace_opened(new_opened)


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
    """Vollwertiger HTTP-Auth-Probe gegen ein einzelnes Geraet.

    Anders als der Heartbeat schickt das einen echten API-Call und
    verifiziert Auth.
    """
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
