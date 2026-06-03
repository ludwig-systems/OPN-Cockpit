"""Inventar-Routen: Geraete listen, anlegen, loeschen, Heartbeat, Test-Connection.

Das Master-Passwort wird beim Unlock einmalig erfragt und in der Session
gecached — Schreibvorgaenge laufen ohne erneuten Prompt. Der Cache lebt
nur waehrend der Session und wird beim Lock/Auto-Lock geloescht.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import Response

from opn_cockpit.audit.backend import audit_actor, get_audit_backend
from opn_cockpit.audit.log import AuditEventKind
from opn_cockpit.backups import (
    BackupNotFoundError,
    BackupRecord,
    BackupStoreError,
    append_backup,
    list_backups,
    prune_backups,
    read_backup_content,
)
from opn_cockpit.backups.storage import (
    BACKUP_FILE_SUFFIX,
    _default_storage_root,
)
from opn_cockpit.core.config_compare import (
    AliasItem,
    compare_aliases,
    extract_aliases,
)
from opn_cockpit.core.config_drift import compute_drift_hash
from opn_cockpit.core.device_info import (
    CertificateStatus,
    FirmwareStatus,
    download_backup,
    fetch_certificates,
    fetch_firmware_status,
    trigger_firmware_check,
)
from opn_cockpit.core.errors import (
    ApiError,
    AuthError,
    EgressDeniedError,
    OpnCockpitError,
    UnreachableError,
    ValidationError,
)
from opn_cockpit.core.health import check_device, tcp_probe
from opn_cockpit.core.http_client import HttpClient, HttpTarget, HttpTuning
from opn_cockpit.core.validation import validate_host
from opn_cockpit.inventory.model import Device
from opn_cockpit.security.session import Session
from opn_cockpit.vault.model import VaultDevice
from opn_cockpit.web.acl import (
    filter_devices_for,
    require_device_access,
    require_write_role,
)
from opn_cockpit.web.api.bootstrap import get_server_state
from opn_cockpit.web.api.schemas import (
    AliasEntryResponse,
    BackupListResponse,
    BackupResponse,
    CertEntryResponse,
    CertStatusEntry,
    CertStatusRequest,
    CertStatusResponse,
    CompareCellResponse,
    CompareColumnInfo,
    CompareRequest,
    CompareResponse,
    CompareRowResponse,
    ConnectionTestResponse,
    DeviceAliasesResponse,
    DeviceApiKeyResponse,
    DeviceCreateRequest,
    DeviceResponse,
    DeviceUpdateRequest,
    DriftStatusEntry,
    DriftStatusRequest,
    DriftStatusResponse,
    FirmwareStatusEntry,
    FirmwareStatusRequest,
    FirmwareStatusResponse,
    HeartbeatEntry,
    HeartbeatRequest,
    HeartbeatResponse,
    InventoryResponse,
    SyncAliasRequest,
    SyncAliasResponse,
    TagSummary,
)
from opn_cockpit.web.auth.dependencies import require_session
from opn_cockpit.web.server_state import ServerState
from opn_cockpit.web.vault_writes import persist_session_vault

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/inventory", tags=["inventory"])

HEARTBEAT_MAX_WORKERS = 16


# ---------------------------------------------------------------------------
# GET /api/inventory
# ---------------------------------------------------------------------------


@router.get("", response_model=InventoryResponse)
def list_inventory(session: Session = Depends(require_session)) -> InventoryResponse:
    """Liefert alle fuer den User sichtbaren Geraete + Tag-Summary.

    Multi-User-Mode: allowed_tags-Whitelist greift; admins und
    Single-Mode sehen alles. Die Tag-Summary basiert ausschliesslich
    auf den sichtbaren Geraeten — der User soll keine Existenz von
    Tags ausserhalb seines Scopes erfahren.
    """
    raw_devices = filter_devices_for(session.opened.data.devices, session)
    devices = [Device.from_vault_device(d) for d in raw_devices]
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
    request: Request,
    session: Session = Depends(require_session),
    server: ServerState = Depends(get_server_state),
) -> DeviceResponse:
    """Legt ein Geraet im Tresor an und persistiert."""
    require_write_role(session)
    vault_path = _require_vault_path(session)
    # Plausibilitaetspruefung Host (IP oder Hostname) vor dem Anlegen.
    # validate_host normalisiert (https://...-Praefix, Pfade weg) - den
    # bereinigten Wert nehmen wir ab hier statt payload.host.
    try:
        clean_host = validate_host(payload.host)
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    # Audit #9: Read-Modify-Write unter Lock im Multi-Mode.
    with server.vault_mutation_lock():
        if _name_exists(session, payload.name):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Ein Geraet mit dem Namen '{payload.name}' existiert bereits.",
            )
        new_device = VaultDevice(
            id=VaultDevice.new_id(),
            name=payload.name,
            host=clean_host,
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

        persist_session_vault(request, session, vault_path, rollback=_rollback_add)
        return _to_device_response(Device.from_vault_device(new_device))


# ---------------------------------------------------------------------------
# PATCH /api/inventory/devices/{device_id}
# ---------------------------------------------------------------------------


@router.patch("/devices/{device_id}", response_model=DeviceResponse)
def update_device(
    device_id: str,
    payload: DeviceUpdateRequest,
    request: Request,
    session: Session = Depends(require_session),
    server: ServerState = Depends(get_server_state),
) -> DeviceResponse:
    """Aktualisiert ausgewaehlte Felder eines Geraets und persistiert."""
    require_write_role(session)
    vault_path = _require_vault_path(session)
    # Host-Plausibilitaet pruefen, falls Host geaendert werden soll.
    # validate_host normalisiert (Schema/Pfad weg); wir schreiben den
    # bereinigten Wert zurueck in payload damit _apply_device_update ihn nutzt.
    if payload.host is not None:
        try:
            payload.host = validate_host(payload.host)
        except ValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(exc),
            ) from exc
    with server.vault_mutation_lock():
        devices = session.opened.data.devices
        index = next((i for i, d in enumerate(devices) if d.id == device_id), -1)
        if index < 0:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Geraet mit ID '{device_id}' nicht im Tresor.",
            )

        current = devices[index]
        require_device_access(current, session)

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

        # In-place mutate via Helper, um Branch-Count niedrig zu halten.
        _apply_device_update(current, payload)

        def _rollback_update() -> None:
            devices[index] = snapshot

        persist_session_vault(request, session, vault_path, rollback=_rollback_update)
        return _to_device_response(Device.from_vault_device(current))


# ---------------------------------------------------------------------------
# DELETE /api/inventory/devices/{device_id}
# ---------------------------------------------------------------------------


@router.delete("/devices/{device_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_device(
    device_id: str,
    request: Request,
    session: Session = Depends(require_session),
    server: ServerState = Depends(get_server_state),
) -> None:
    """Entfernt ein Geraet aus dem Tresor."""
    require_write_role(session)
    vault_path = _require_vault_path(session)
    with server.vault_mutation_lock():
        devices = session.opened.data.devices
        index = next((i for i, d in enumerate(devices) if d.id == device_id), -1)
        if index < 0:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Geraet mit ID '{device_id}' nicht im Tresor.",
            )
        require_device_access(devices[index], session)
        backup = devices.pop(index)

        def _rollback_remove() -> None:
            devices.insert(index, backup)

        persist_session_vault(request, session, vault_path, rollback=_rollback_remove)


# ---------------------------------------------------------------------------
# POST /api/inventory/heartbeat
# ---------------------------------------------------------------------------


@router.post("/heartbeat", response_model=HeartbeatResponse)
def heartbeat(
    payload: HeartbeatRequest,
    session: Session = Depends(require_session),
) -> HeartbeatResponse:
    """TCP-Probe gegen alle sichtbaren Geraete (gefiltert per allowed_tags).

    Bewusst KEIN HTTP-Aufruf — der Heartbeat soll keine OPNsense-
    Auth-Logs erzeugen und keine Last verursachen.
    """
    visible_devices = filter_devices_for(session.opened.data.devices, session)
    if payload.device_ids:
        wanted = set(payload.device_ids)
        targets = [d for d in visible_devices if d.id in wanted]
    else:
        targets = list(visible_devices)

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
    require_device_access(vault_device, session)
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
# GET /api/inventory/devices/{id}/api-key
# ---------------------------------------------------------------------------


@router.get(
    "/devices/{device_id}/api-key",
    response_model=DeviceApiKeyResponse,
)
def reveal_device_api_key(
    device_id: str,
    session: Session = Depends(require_session),
) -> DeviceApiKeyResponse:
    """Liefert den API-Key (nicht das Secret) fuer den Edit-Dialog.

    Der Key ist semantisch ein Identifier, nicht ein Secret - er taucht
    auf der OPNsense im Trust-Section sichtbar auf, ohne dass man als
    User extra Privilegien braucht. Trotzdem audit-logged, damit
    Reveal-Vorgaenge nachweisbar bleiben.

    Secret bleibt unsichtbar: kein Endpoint, kein Schema, kein Frontend-
    Pfad.
    """
    require_write_role(session)
    vault_device = next(
        (d for d in session.opened.data.devices if d.id == device_id), None
    )
    if vault_device is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Geraet mit ID '{device_id}' nicht im Tresor.",
        )
    require_device_access(vault_device, session)
    get_audit_backend().append(
        AuditEventKind.API_KEY_REVEALED,
        actor=audit_actor(session),
        action="api_key_revealed",
        target_device_id=device_id,
        target_device_name=vault_device.name,
        summary=(
            f"API-Key fuer Geraet '{vault_device.name}' im Edit-Dialog sichtbar gemacht."
        ),
    )
    session.touch()
    return DeviceApiKeyResponse(
        device_id=device_id,
        api_key=vault_device.api_key,
    )


# ---------------------------------------------------------------------------
# POST /api/inventory/firmware-status
# ---------------------------------------------------------------------------


@router.post("/firmware-status", response_model=FirmwareStatusResponse)
def firmware_status(
    payload: FirmwareStatusRequest,
    session: Session = Depends(require_session),
) -> FirmwareStatusResponse:
    """Batch-Abruf der OPNsense-Version pro Geraet.

    Im Unterschied zum Heartbeat *macht* das einen authentifizierten
    HTTP-Call (``/api/core/firmware/status``) — wird also im Audit der
    OPNsense sichtbar. Frontend sollte das daher nicht im 30s-Takt
    pollen, sondern einmal beim Inventar-Laden + Manual-Refresh.
    """
    visible_devices = filter_devices_for(session.opened.data.devices, session)
    if payload.device_ids:
        wanted = set(payload.device_ids)
        targets = [d for d in visible_devices if d.id in wanted]
    else:
        targets = list(visible_devices)

    if not targets:
        return FirmwareStatusResponse(results=[])

    timestamp = _iso_now()
    settings = session.opened.data.settings
    tuning = HttpTuning(
        connect_timeout_s=settings.connect_timeout_s,
        read_timeout_s=settings.read_timeout_s,
        reconfigure_timeout_s=settings.reconfigure_timeout_s,
        retry_count=settings.retry_count,
    )

    def probe(vd: VaultDevice) -> FirmwareStatusEntry:
        target = HttpTarget(host=vd.host, port=vd.port, verify=vd.tls_verify)
        with HttpClient(targets=[target], tuning=tuning) as client:
            fw: FirmwareStatus = fetch_firmware_status(
                client, target, vd.api_key, vd.api_secret,
            )
        return FirmwareStatusEntry(
            device_id=vd.id,
            reachable=fw.reachable,
            authenticated=fw.authenticated,
            version=fw.version,
            status=fw.status,
            update_available=fw.update_available,
            summary=fw.summary,
            checked_at_iso=timestamp,
            new_version=fw.new_version,
            status_msg=fw.status_msg,
        )

    workers = min(HEARTBEAT_MAX_WORKERS, len(targets))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(probe, targets))
    session.touch()
    return FirmwareStatusResponse(results=results)


# ---------------------------------------------------------------------------
# POST /api/inventory/cert-status  - Batch-Cert-Inventur
# ---------------------------------------------------------------------------


@router.post("/cert-status", response_model=CertStatusResponse)
def cert_status(
    payload: CertStatusRequest,
    session: Session = Depends(require_session),
) -> CertStatusResponse:
    """Batch-Abruf der Trust-Zertifikate pro Geraet.

    Erscheint im OPNsense-Audit wie der Firmware-Status, also auch nicht
    im 30s-Takt pollen - einmal pro Inventar-Load und manueller Refresh
    reicht. Liefert pro Geraet die volle Cert-Liste + die geringste
    Tagesanzahl bis Ablauf, die das Frontend fuer den Kachel-Badge nutzt.
    """
    visible_devices = filter_devices_for(session.opened.data.devices, session)
    if payload.device_ids:
        wanted = set(payload.device_ids)
        targets = [d for d in visible_devices if d.id in wanted]
    else:
        targets = list(visible_devices)
    if not targets:
        return CertStatusResponse(results=[])

    timestamp = _iso_now()
    settings = session.opened.data.settings
    tuning = HttpTuning(
        connect_timeout_s=settings.connect_timeout_s,
        read_timeout_s=settings.read_timeout_s,
        reconfigure_timeout_s=settings.reconfigure_timeout_s,
        retry_count=settings.retry_count,
    )

    def probe(vd: VaultDevice) -> CertStatusEntry:
        tgt = HttpTarget(host=vd.host, port=vd.port, verify=vd.tls_verify)
        with HttpClient(targets=[tgt], tuning=tuning) as client:
            cs: CertificateStatus = fetch_certificates(
                client, tgt, vd.api_key, vd.api_secret,
            )
        return CertStatusEntry(
            device_id=vd.id,
            reachable=cs.reachable,
            authenticated=cs.authenticated,
            summary=cs.summary,
            checked_at_iso=timestamp,
            certs=[
                CertEntryResponse(
                    uuid=c.uuid,
                    descr=c.descr,
                    common_name=c.common_name,
                    issuer=c.issuer,
                    not_after_iso=c.not_after_iso,
                    days_until_expiry=c.days_until_expiry,
                    in_use=c.in_use,
                )
                for c in cs.certs
            ],
            soonest_days=cs.soonest_days,
        )

    workers = min(HEARTBEAT_MAX_WORKERS, len(targets))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(probe, targets))
    session.touch()
    return CertStatusResponse(results=results)


# ---------------------------------------------------------------------------
# POST /api/inventory/drift-status  - Config-Drift gegen letztes Backup
# ---------------------------------------------------------------------------


@router.post("/drift-status", response_model=DriftStatusResponse)
def drift_status(
    payload: DriftStatusRequest,
    session: Session = Depends(require_session),
) -> DriftStatusResponse:
    """Vergleicht Live-Config-Hash mit dem letzten lokalen Backup.

    Pro Geraet: holt aktuelle Konfig via OPNsense-Backup-Endpoint, rechnet
    den normalisierten SHA256 (volatile <revision>/<lastchange>-Bloecke
    gestrippt). Vergleicht mit dem Hash des juengsten lokalen Backups.
    Wenn ungleich: Drift erkannt.

    Geraete ohne lokales Backup koennen nicht verglichen werden -
    ``has_baseline=False``, ``drift_detected=None``. UI zeigt das als
    neutrale Info (kein Drift-Badge).
    """
    visible_devices = filter_devices_for(session.opened.data.devices, session)
    if payload.device_ids:
        wanted = set(payload.device_ids)
        targets = [d for d in visible_devices if d.id in wanted]
    else:
        targets = list(visible_devices)
    if not targets:
        return DriftStatusResponse(results=[])

    timestamp = _iso_now()
    settings = session.opened.data.settings
    tuning = HttpTuning(
        connect_timeout_s=settings.connect_timeout_s,
        read_timeout_s=settings.read_timeout_s,
        reconfigure_timeout_s=settings.reconfigure_timeout_s,
        retry_count=settings.retry_count,
    )

    def probe(vd: VaultDevice) -> DriftStatusEntry:
        baseline_records = list_backups(vd.id)
        if not baseline_records:
            return DriftStatusEntry(
                device_id=vd.id,
                reachable=False, authenticated=False,
                summary="Kein lokales Backup als Baseline vorhanden.",
                checked_at_iso=timestamp,
                has_baseline=False, drift_detected=None,
            )
        baseline = baseline_records[0]  # Neueste zuerst
        try:
            baseline_bytes = read_backup_content(vd.id, baseline.id)
        except (BackupNotFoundError, BackupStoreError) as exc:
            return DriftStatusEntry(
                device_id=vd.id,
                reachable=False, authenticated=False,
                summary=f"Baseline-Backup nicht lesbar: {exc}",
                checked_at_iso=timestamp,
                has_baseline=False, drift_detected=None,
            )
        baseline_hash = compute_drift_hash(baseline_bytes)

        tgt = HttpTarget(host=vd.host, port=vd.port, verify=vd.tls_verify)
        try:
            with HttpClient(targets=[tgt], tuning=tuning) as client:
                live_bytes = download_backup(client, tgt, vd.api_key, vd.api_secret)
        except OpnCockpitError as exc:
            reason = exc.context.summary or exc.context.error_kind
            return DriftStatusEntry(
                device_id=vd.id,
                reachable=exc.context.error_kind != "connect_timeout",
                authenticated=False,
                summary=f"Drift-Check fehlgeschlagen: {reason}",
                checked_at_iso=timestamp,
                has_baseline=True, drift_detected=None,
                baseline_backup_id=baseline.id,
                baseline_backup_iso=baseline.timestamp_utc,
                baseline_trigger=baseline.trigger,
            )
        live_hash = compute_drift_hash(live_bytes)
        drift = live_hash != baseline_hash
        if drift:
            summary = (
                f"Drift erkannt gegen Backup vom {baseline.timestamp_utc[:19]} "
                f"({baseline.trigger})."
            )
        else:
            summary = (
                f"Keine Drift gegen Backup vom {baseline.timestamp_utc[:19]} "
                f"({baseline.trigger})."
            )
        return DriftStatusEntry(
            device_id=vd.id,
            reachable=True, authenticated=True,
            summary=summary,
            checked_at_iso=timestamp,
            has_baseline=True, drift_detected=drift,
            baseline_backup_id=baseline.id,
            baseline_backup_iso=baseline.timestamp_utc,
            baseline_trigger=baseline.trigger,
        )

    workers = min(HEARTBEAT_MAX_WORKERS, len(targets))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(probe, targets))
    session.touch()
    return DriftStatusResponse(results=results)


# ---------------------------------------------------------------------------
# POST /api/inventory/compare  - Konfig-Vergleich zwischen N Geraeten
# ---------------------------------------------------------------------------


@router.post("/compare", response_model=CompareResponse)
def compare_configs(
    payload: CompareRequest,
    session: Session = Depends(require_session),
) -> CompareResponse:
    """Vergleicht ein Subsystem (heute: aliases) ueber N Geraete hinweg.

    Pro Geraet wird das Live-Konfig-XML geholt, das Subsystem strukturiert
    extrahiert und in eine Matrix verglichen. Nicht erreichbare Geraete
    erscheinen als eigene Spalte mit Status "unreachable".
    """
    min_devices_for_compare = 2
    visible_devices = filter_devices_for(session.opened.data.devices, session)
    wanted = set(payload.device_ids)
    targets_by_id = {d.id: d for d in visible_devices if d.id in wanted}
    if len(targets_by_id) < min_devices_for_compare:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Mindestens 2 sichtbare Geraete fuer einen Vergleich noetig.",
        )
    # Reihenfolge entsprechend der vom Caller geschickten Liste — Sortierung
    # ist Aufgabe des Aufrufers, wir bewahren sie.
    targets = [targets_by_id[did] for did in payload.device_ids if did in targets_by_id]

    settings = session.opened.data.settings
    tuning = HttpTuning(
        connect_timeout_s=settings.connect_timeout_s,
        read_timeout_s=settings.read_timeout_s,
        reconfigure_timeout_s=settings.reconfigure_timeout_s,
        retry_count=settings.retry_count,
    )

    def probe(vd: VaultDevice) -> tuple[VaultDevice, bytes | None, str]:
        tgt = HttpTarget(host=vd.host, port=vd.port, verify=vd.tls_verify)
        try:
            with HttpClient(targets=[tgt], tuning=tuning) as client:
                content = download_backup(client, tgt, vd.api_key, vd.api_secret)
            return vd, content, "OK"
        except OpnCockpitError as exc:
            reason = exc.context.summary or exc.context.error_kind or "unbekannt"
            return vd, None, reason

    workers = min(HEARTBEAT_MAX_WORKERS, len(targets))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        probe_results = list(pool.map(probe, targets))

    columns_info: list[CompareColumnInfo] = []
    per_device_aliases: dict[str, list[AliasItem] | None] = {}
    column_order: list[str] = []
    for vd, xml_bytes, summary in probe_results:
        column_order.append(vd.id)
        columns_info.append(CompareColumnInfo(
            device_id=vd.id,
            device_name=vd.name,
            reachable=xml_bytes is not None,
            summary=summary,
        ))
        per_device_aliases[vd.id] = (
            extract_aliases(xml_bytes) if xml_bytes is not None else None
        )

    if payload.subsystem == "aliases":
        comparison = compare_aliases(per_device_aliases, column_order)
        rows = [
            CompareRowResponse(
                name=row.name,
                uniform=row.uniform,
                cells=[
                    CompareCellResponse(
                        device_id=did,
                        status=cell.status,
                        type=cell.type,
                        content_fingerprint=cell.content_fingerprint,
                        content_count=cell.content_count,
                        description=cell.description,
                    )
                    for did, cell in row.cells
                ],
            )
            for row in comparison.rows
        ]
        summary = comparison.summary
    else:
        # Schema-Validator stellt das eigentlich sicher, defensiver Fallback
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Subsystem '{payload.subsystem}' nicht unterstuetzt.",
        )

    session.touch()
    return CompareResponse(
        subsystem=payload.subsystem,
        columns=columns_info,
        rows=rows,
        summary=summary,
        checked_at_iso=_iso_now(),
    )


# ---------------------------------------------------------------------------
# POST /api/inventory/compare/sync-aliases  - Master -> Targets uebernehmen
# ---------------------------------------------------------------------------


@router.post("/compare/sync-aliases", response_model=SyncAliasResponse)
def sync_alias_from_master(
    payload: SyncAliasRequest,
    session: Session = Depends(require_session),
) -> SyncAliasResponse:
    """Holt einen Alias vom Master-Geraet und erzeugt einen Plan fuer die Targets.

    Workflow von der Compare-Matrix aus:
    1. User klickt auf "Sync" einer Drift-/Absent-Zeile
    2. Picked das Geraet das er als Master will (per Klick auf eine
       'present'-Cell)
    3. Backend zieht den live config des Masters, extrahiert den Alias,
       erzeugt ueber das existierende Plan-Pattern (firewall_alias-Subsystem)
       einen Plan mit den Target-Geraeten
    4. Frontend springt direkt in den Plan-View

    Die Sync-Action ist 'add_alias' (= create on target). Wenn das Target
    den Alias bereits in identischer Form hat, markiert der Planner ihn als
    SKIP - keine Doppel-Anwendung.
    """
    # Spaete Imports: vermeidet Zirkular-Imports zur plans.py.
    from opn_cockpit.core.objects.aliases import AliasSpec  # noqa: PLC0415
    from opn_cockpit.web.api.plans import (  # noqa: PLC0415
        _devices_or_404,
        _generate_and_save_plan,
    )

    require_write_role(session)

    visible = filter_devices_for(session.opened.data.devices, session)
    visible_by_id = {d.id: d for d in visible}
    master = visible_by_id.get(payload.master_device_id)
    if master is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Master-Geraet '{payload.master_device_id}' nicht sichtbar.",
        )

    # Master-Config holen + Alias extrahieren
    settings = session.opened.data.settings
    tuning = HttpTuning(
        connect_timeout_s=settings.connect_timeout_s,
        read_timeout_s=settings.read_timeout_s,
        reconfigure_timeout_s=settings.reconfigure_timeout_s,
        retry_count=settings.retry_count,
    )
    tgt = HttpTarget(host=master.host, port=master.port, verify=master.tls_verify)
    try:
        with HttpClient(targets=[tgt], tuning=tuning) as client:
            master_xml = download_backup(client, tgt, master.api_key, master.api_secret)
    except OpnCockpitError as exc:
        reason = exc.context.summary or exc.context.error_kind or "unbekannt"
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Master-Geraet '{master.name}' nicht erreichbar: {reason}",
        ) from exc

    master_aliases = {a.name: a for a in extract_aliases(master_xml)}
    source = master_aliases.get(payload.alias_name)
    if source is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"Alias '{payload.alias_name}' existiert auf Master-Geraet "
                f"'{master.name}' nicht."
            ),
        )

    # Targets aufloesen + Plan erzeugen via existing pipeline
    target_devices = _devices_or_404(session, payload.target_device_ids)
    spec = AliasSpec(
        name=source.name,
        type=source.type,
        content=tuple(source.content),
        descr=source.description,
        merge_mode="create",
    )
    plan = _generate_and_save_plan(
        session=session,
        action="add_alias",
        subsystem="firewall_alias",
        spec=spec,
        devices=target_devices,
    )

    return SyncAliasResponse(
        plan_id=plan.plan_id,
        alias_name=source.name,
        target_count=len(target_devices),
        source_summary=(
            f"{source.name} ({source.type}, "
            f"{len(source.content)} Eintraege) von {master.name}"
        ),
    )


# ---------------------------------------------------------------------------
# GET /api/inventory/devices/{id}/aliases  - Live-Aliase pro Geraet
# ---------------------------------------------------------------------------


@router.get(
    "/devices/{device_id}/aliases",
    response_model=DeviceAliasesResponse,
)
def get_device_aliases(
    device_id: str,
    session: Session = Depends(require_session),
) -> DeviceAliasesResponse:
    """Liefert die Live-Aliase eines Geraets (extrahiert aus dem aktuellen
    Backup-XML). Read-only - Edit/Delete geht heute via OPNsense-UI.

    Aufgerufen vom Alias-Manager-View im Device-Modal. Pro Eintrag:
    Name, Typ, sortierter Content, Beschreibung, content_fingerprint
    (kann fuer Drift-Vergleich mit Compare-Matrix abgeglichen werden).
    """
    devices_by_id = {d.id: d for d in session.opened.data.devices}
    device = devices_by_id.get(device_id)
    if device is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Geraet '{device_id}' nicht im Tresor.",
        )
    require_device_access(device, session)
    timestamp = _iso_now()

    settings = session.opened.data.settings
    tuning = HttpTuning(
        connect_timeout_s=settings.connect_timeout_s,
        read_timeout_s=settings.read_timeout_s,
        reconfigure_timeout_s=settings.reconfigure_timeout_s,
        retry_count=settings.retry_count,
    )
    tgt = HttpTarget(host=device.host, port=device.port, verify=device.tls_verify)
    try:
        with HttpClient(targets=[tgt], tuning=tuning) as client:
            xml_bytes = download_backup(client, tgt, device.api_key, device.api_secret)
    except OpnCockpitError as exc:
        reason = exc.context.summary or exc.context.error_kind or "unbekannt"
        return DeviceAliasesResponse(
            device_id=device.id,
            device_name=device.name,
            reachable=False,
            summary=f"Konfig nicht ladbar: {reason}",
            aliases=[],
            checked_at_iso=timestamp,
        )

    extracted: list[AliasItem] = extract_aliases(xml_bytes)
    session.touch()
    return DeviceAliasesResponse(
        device_id=device.id,
        device_name=device.name,
        reachable=True,
        summary=f"{len(extracted)} Alias(e) live geladen.",
        aliases=[
            AliasEntryResponse(
                name=a.name,
                type=a.type,
                content=list(a.content),
                description=a.description,
                content_fingerprint=a.content_fingerprint,
            )
            for a in extracted
        ],
        checked_at_iso=timestamp,
    )


# ---------------------------------------------------------------------------
# POST /api/inventory/devices/{id}/firmware-check
# ---------------------------------------------------------------------------

# OPNsense's Check laeuft asynchron auf der Box. Nach dem POST warten wir
# kurz und holen den frischen Status. Defaults sind so gewaehlt, dass die
# meisten Boxes in 6-10s fertig sind; gross verzoegerte Mirrors kommen
# evtl. nicht durch und der User klickt halt nochmal.
FIRMWARE_CHECK_INITIAL_WAIT_S = 3.0
FIRMWARE_CHECK_POLL_INTERVAL_S = 2.0
FIRMWARE_CHECK_POLL_TIMEOUT_S = 20.0


@router.post(
    "/devices/{device_id}/firmware-check",
    response_model=FirmwareStatusEntry,
)
def trigger_device_firmware_check(
    device_id: str,
    session: Session = Depends(require_session),
) -> FirmwareStatusEntry:
    """Stoesst OPNsense's "Check for updates" an und liefert frischen Status.

    Synchron: triggert den Check (POST /api/core/firmware/check), wartet
    auf den Hintergrund-Job (Anfangs-Sleep + Polling), und gibt den
    aktualisierten Firmware-Status zurueck. Frontend braucht keinen
    zweiten Aufruf - die "Update verfuegbar"-Badge wird sofort mit dem
    Ergebnis aktualisiert.

    Blockiert die Verbindung typischerweise 5-12 Sekunden. Bewusst nicht
    fire-and-forget, weil das User-Erlebnis "klick - kurz warten - Badge
    aktualisiert" sauberer ist als "klick - irgendwann mal manuell
    nachladen".
    """
    vault_device = next(
        (d for d in session.opened.data.devices if d.id == device_id), None
    )
    if vault_device is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Geraet mit ID '{device_id}' nicht im Tresor.",
        )
    require_device_access(vault_device, session)
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
    timestamp = _iso_now()
    with HttpClient(targets=[target], tuning=tuning) as client:
        ok, msg = trigger_firmware_check(
            client, target, vault_device.api_key, vault_device.api_secret,
        )
        if not ok:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Update-Check konnte nicht angestossen werden: {msg}",
            )
        fw = _wait_for_firmware_check(
            client, target, vault_device.api_key, vault_device.api_secret,
        )
    session.touch()
    return FirmwareStatusEntry(
        device_id=vault_device.id,
        reachable=fw.reachable,
        authenticated=fw.authenticated,
        version=fw.version,
        status=fw.status,
        update_available=fw.update_available,
        summary=fw.summary,
        checked_at_iso=timestamp,
        new_version=fw.new_version,
        status_msg=fw.status_msg,
    )


def _wait_for_firmware_check(
    client: HttpClient,
    target: HttpTarget,
    key: str,
    secret: str,
) -> FirmwareStatus:
    """Pollt OPNsense's firmware/status nach erfolgtem Check.

    Strategie:
    1. Kurz schlafen (Anfangs-Wait), weil OPNsense's Job-Queue meist
       schon nach ~3s Resultate hat.
    2. Status holen. Wenn status_msg signalisiert "Currently checking"
       (oder leer ist), weiter pollen bis Timeout.
    3. Letzte Antwort zurueckgeben - lieber etwas Stalefood als haengen.
    """
    time.sleep(FIRMWARE_CHECK_INITIAL_WAIT_S)
    deadline = time.monotonic() + FIRMWARE_CHECK_POLL_TIMEOUT_S
    fw = fetch_firmware_status(client, target, key, secret)
    while time.monotonic() < deadline:
        # OPNsense signalisiert laufenden Check teils ueber "Currently checking"
        # im status_msg oder ueber leeren status_msg + status="" - wenn wir
        # einen "fertigen" Status-Wort wie update/none/ok/upgrade haben,
        # akzeptieren wir das als fertig.
        if fw.status and fw.status.lower() in {"ok", "none", "update", "upgrade"}:
            return fw
        time.sleep(FIRMWARE_CHECK_POLL_INTERVAL_S)
        fw = fetch_firmware_status(client, target, key, secret)
    return fw


# ---------------------------------------------------------------------------
# GET /api/inventory/devices/{id}/backup
# ---------------------------------------------------------------------------


@router.get("/devices/{device_id}/backup")
def download_device_backup(
    device_id: str,
    session: Session = Depends(require_session),
) -> Response:
    """Laedt die aktuelle OPNsense-Konfiguration als XML-Download.

    Streamed direkt zum Browser (Content-Disposition: attachment).
    Audit-Eintrag BACKUP_DOWNLOADED mit Device-ID + Datei-Groesse.

    Jede unerwartete Exception wird hier zentral gefangen und als 502 mit
    sprechender Detail-Message zurueckgegeben — wir wollen niemals ein
    nacktes FastAPI-500 an die UI durchreichen, weil das den User ohne
    Diagnose stehen laesst.
    """
    try:
        return _download_device_backup_impl(device_id, session)
    except HTTPException:
        raise
    except Exception as exc:
        # Alles, was wir hier sehen, ist ein Bug oder eine Umgebungsstoerung
        # (DB-Lock im Audit, Disk voll, OPNsense-Antwort die wir nicht
        # einordnen koennen). Vollen Traceback ins Server-Log; nur den
        # Exception-Typ + kurze Message an den Client.
        _log.exception(
            "Backup-Download fuer device_id=%s unerwartet fehlgeschlagen",
            device_id,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                f"Backup-Download fehlgeschlagen ({exc.__class__.__name__}): "
                f"{str(exc) or 'kein Detail'}. Details im Server-Log."
            ),
        ) from exc


def _download_device_backup_impl(device_id: str, session: Session) -> Response:
    vault_device = next(
        (d for d in session.opened.data.devices if d.id == device_id), None
    )
    if vault_device is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Geraet mit ID '{device_id}' nicht im Tresor.",
        )
    require_device_access(vault_device, session)
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
    try:
        with HttpClient(targets=[target], tuning=tuning) as client:
            content = download_backup(client, target, vault_device.api_key, vault_device.api_secret)
    except AuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=(
                f"Backup abgelehnt: {exc.context.summary or 'API-Schluessel ungueltig'}. "
                "Pruefe in OPNsense unter System -> Access -> Users, ob der API-User "
                "das Privileg 'Diagnostics: Configuration History' besitzt."
            ),
        ) from exc
    except UnreachableError as exc:
        if exc.context.error_kind == "tls":
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=(
                    f"TLS-Verifikation fehlgeschlagen: "
                    f"{exc.context.summary or 'Zertifikat nicht vertrauenswuerdig'}. "
                    "Fixe das Zertifikat auf der OPNsense (SAN/Hostname pruefen) "
                    "oder schalte TLS-Pruefung fuer dieses Geraet im Inventar ab."
                ),
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Geraet nicht erreichbar: {exc.context.summary or exc.context.error_kind}.",
        ) from exc
    except ValidationError as exc:
        # OPNsense lehnt den Request strukturell ab (404/405/400). Haeufigster
        # Grund: Endpunkt /api/core/backup/download/this existiert auf dieser
        # OPNsense-Version nicht oder der User darf ihn nicht aufrufen.
        sc = exc.context.status_code or "?"
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                f"Backup-Endpunkt antwortet HTTP {sc}: "
                f"{exc.context.summary or exc.context.error_kind}. "
                "Vermutlich fehlt dem API-User das Privileg "
                "'Diagnostics: Configuration History' oder die OPNsense-Version "
                "kennt /api/core/backup/download/this nicht (zu alt)."
            ),
        ) from exc
    except EgressDeniedError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Egress verweigert: {exc.context.summary or exc.context.error_kind}.",
        ) from exc
    except ApiError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"OPNsense-Antwort fehlerhaft: {exc.context.summary or exc.context.error_kind}.",
        ) from exc
    except OpnCockpitError as exc:
        # Defense-in-depth: weitere Core-Fehlertypen sauber auf 502 mappen
        # statt FastAPI 500 zuruecksenden zu lassen.
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Backup-Download fehlgeschlagen ({exc.context.error_kind}): "
                   f"{exc.context.summary or '-'}.",
        ) from exc

    # Datei-Name: device-name + ISO-Datum (ohne Doppelpunkte, sonst meckert Windows).
    safe_chars = (c if c.isalnum() or c in "_-" else "_" for c in vault_device.name)
    safe_name = "".join(safe_chars).strip("_") or "device"
    ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    filename = f"opnsense-config-{safe_name}-{ts}.xml"

    # Backup zusaetzlich lokal persistieren (trigger="manual"), damit er
    # in der Backup-History auftaucht und spaeter aus Cockpit nochmal
    # heruntergeladen werden kann. Fehler hier blockieren den User-Download
    # NICHT - er kriegt seine Datei in jedem Fall.
    persisted_record_id: str | None = None
    try:
        record = append_backup(
            device_id,
            content,
            trigger="manual",
            device_name_at_creation=vault_device.name,
        )
        persisted_record_id = record.id
        # Best-Effort Prune mit den Vault-Settings.
        try:
            prune_backups(
                device_id,
                retention_pre_apply=settings.backup_retention_pre_apply,
                retention_scheduled=settings.backup_retention_scheduled,
            )
        except BackupStoreError:
            _log.exception(
                "Backup-Pruning fuer device_id=%s nach manuellem Download fehlgeschlagen.",
                device_id,
            )
    except BackupStoreError:
        _log.exception(
            "Manuelles Backup konnte nicht lokal persistiert werden fuer device_id=%s",
            device_id,
        )

    audit_summary = (
        f"Konfig-Backup geladen von {vault_device.name} ({len(content)} Bytes)"
    )
    if persisted_record_id:
        audit_summary += f", lokal persistiert als {persisted_record_id}"
    get_audit_backend().append(
        AuditEventKind.BACKUP_DOWNLOADED,
        actor=audit_actor(session),
        action="backup_download",
        target_device_id=device_id,
        target_device_name=vault_device.name,
        summary=audit_summary,
    )
    session.touch()
    return Response(
        content=content,
        media_type="application/xml; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


# ---------------------------------------------------------------------------
# GET /api/inventory/devices/{id}/backups  - Liste lokaler Backups
# GET /api/inventory/devices/{id}/backups/{backup_id}  - Download lokaler Backup
# DELETE /api/inventory/devices/{id}/backups/{backup_id}  - Loeschen
# ---------------------------------------------------------------------------


@router.get(
    "/devices/{device_id}/backups",
    response_model=BackupListResponse,
)
def list_device_backups(
    device_id: str,
    session: Session = Depends(require_session),
) -> BackupListResponse:
    """Liefert die lokal gespeicherten Backups eines Geraets, neueste zuerst."""
    vault_device = next(
        (d for d in session.opened.data.devices if d.id == device_id), None
    )
    if vault_device is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Geraet mit ID '{device_id}' nicht im Tresor.",
        )
    require_device_access(vault_device, session)
    try:
        records = list_backups(device_id)
    except BackupStoreError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Backup-Index nicht lesbar: {exc}",
        ) from exc
    session.touch()
    return BackupListResponse(
        device_id=device_id,
        backups=[_record_to_response(r) for r in records],
    )


@router.get("/devices/{device_id}/backups/{backup_id}")
def download_stored_backup(
    device_id: str,
    backup_id: str,
    session: Session = Depends(require_session),
) -> Response:
    """Liefert einen lokal gespeicherten Backup als XML-Download."""
    vault_device = next(
        (d for d in session.opened.data.devices if d.id == device_id), None
    )
    if vault_device is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Geraet mit ID '{device_id}' nicht im Tresor.",
        )
    require_device_access(vault_device, session)
    try:
        content = read_backup_content(device_id, backup_id)
    except BackupNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except BackupStoreError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc

    # Trigger/Timestamp aus Index ziehen fuer den Dateinamen
    records = list_backups(device_id)
    record = next((r for r in records if r.id == backup_id), None)
    safe_chars = (c if c.isalnum() or c in "_-" else "_" for c in vault_device.name)
    safe_name = "".join(safe_chars).strip("_") or "device"
    trigger_label = (record.trigger if record else "stored").replace("-", "_")
    ts_for_name = (
        (record.timestamp_utc if record else _iso_now())
        .replace(":", "").replace("-", "").replace(".", "_")
    )
    filename = f"opnsense-config-{safe_name}-{trigger_label}-{ts_for_name}.xml"
    get_audit_backend().append(
        AuditEventKind.BACKUP_DOWNLOADED,
        actor=audit_actor(session),
        action="backup_download_stored",
        target_device_id=device_id,
        target_device_name=vault_device.name,
        summary=(
            f"Lokal gespeichertes Backup heruntergeladen "
            f"({vault_device.name}, id={backup_id})"
        ),
    )
    session.touch()
    return Response(
        content=content,
        media_type="application/xml; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.delete(
    "/devices/{device_id}/backups/{backup_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_stored_backup(
    device_id: str,
    backup_id: str,
    session: Session = Depends(require_session),
) -> None:
    """Loescht ein einzelnes lokal gespeichertes Backup."""
    require_write_role(session)
    vault_device = next(
        (d for d in session.opened.data.devices if d.id == device_id), None
    )
    if vault_device is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Geraet mit ID '{device_id}' nicht im Tresor.",
        )
    require_device_access(vault_device, session)
    # Storage hat kein "delete one" - wir lesen den Index, schreiben ihn ohne
    # den Eintrag zurueck, und loeschen die Datei.
    try:
        records = list_backups(device_id)
    except BackupStoreError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc
    if not any(r.id == backup_id for r in records):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Backup '{backup_id}' nicht gefunden.",
        )
    # Vereinfachung: wir loeschen direkt die Datei und ueberlassen den
    # naechsten prune-Run die Index-Bereinigung (orphans werden mit-gepruned).
    device_dir = _default_storage_root() / device_id
    file_path = device_dir / f"{backup_id}{BACKUP_FILE_SUFFIX}"
    file_path.unlink(missing_ok=True)
    # Index direkt mit-aufraeumen (orphan ist konsistent, aber der Index
    # wuerde sonst kurzzeitig den geloeschten Eintrag noch zeigen).
    try:
        prune_backups(device_id)
    except BackupStoreError:
        _log.exception(
            "Backup-Index-Update nach DELETE fehlgeschlagen fuer %s/%s",
            device_id, backup_id,
        )
    get_audit_backend().append(
        AuditEventKind.BACKUP_DOWNLOADED,
        actor=audit_actor(session),
        action="backup_deleted",
        target_device_id=device_id,
        target_device_name=vault_device.name,
        summary=(
            f"Lokales Backup geloescht ({vault_device.name}, id={backup_id})"
        ),
    )
    session.touch()


def _record_to_response(record: BackupRecord) -> BackupResponse:
    return BackupResponse(
        id=record.id,
        device_id=record.device_id,
        timestamp_utc=record.timestamp_utc,
        trigger=record.trigger,
        size_bytes=record.size_bytes,
        size_compressed=record.size_compressed,
        sha256=record.sha256,
        related_plan_id=record.related_plan_id,
        device_name_at_creation=record.device_name_at_creation,
    )


# ---------------------------------------------------------------------------
# Helfer
# ---------------------------------------------------------------------------


def _apply_device_update(current: VaultDevice, payload: DeviceUpdateRequest) -> None:
    """Updated ``current`` in-place mit den gesetzten Feldern aus ``payload``.

    api_key/api_secret werden nur gesetzt wenn explizit nicht-leer, damit
    User Host/Port aendern koennen, ohne die Credentials neu zu tippen.
    """
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


def _require_vault_path(session: Session) -> Path:
    vault_path = session.vault_path
    if vault_path is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Tresor-Pfad fehlt in der Session.",
        )
    return vault_path




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
