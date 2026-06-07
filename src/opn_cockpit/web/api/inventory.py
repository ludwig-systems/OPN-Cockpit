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
    RouteItem,
    RuleItem,
    UnboundDomainItem,
    UnboundForwardItem,
    UnboundHostItem,
    compare_aliases,
    compare_routes,
    compare_rules,
    compare_unbound_domains,
    compare_unbound_forwards,
    compare_unbound_hosts,
    extract_aliases,
    extract_routes,
    extract_rules,
    extract_unbound_domains,
    extract_unbound_forwards,
    extract_unbound_hosts,
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
from opn_cockpit.core.http_client import (
    HttpClient,
    HttpTarget,
    tuning_from_settings,
)
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
    DeviceRoutesResponse,
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
    DeviceRulesResponse,
    DeviceUnboundDomainsResponse,
    DeviceUnboundForwardsResponse,
    DeviceUnboundHostsResponse,
    RouteEntryResponse,
    RuleEntryResponse,
    SyncAliasRequest,
    SyncAliasResponse,
    TagSummary,
    UnboundDomainEntryResponse,
    UnboundForwardEntryResponse,
    UnboundHostEntryResponse,
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
            ssh_enabled=payload.ssh_enabled,
            ssh_host=payload.ssh_host,
            ssh_port=payload.ssh_port,
            ssh_user=payload.ssh_user,
            ssh_private_key_pem=payload.ssh_private_key_pem,
            maintenance=bool(getattr(payload, "maintenance", False)),
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
    # Wartungsmodus: kein Probe, kein TCP-Handshake. Statt das Geraet als
    # "offline" zu melden geben wir reachable=True zurueck und markieren
    # es als maintenance — die UI rendert daraus den eigenen Status-Dot.
    maintenance_results = [
        HeartbeatEntry(
            device_id=vd.id, reachable=True, checked_at_iso=timestamp,
            maintenance=True,
        )
        for vd in targets if vd.maintenance
    ]
    active_targets = [vd for vd in targets if not vd.maintenance]
    if not active_targets:
        return HeartbeatResponse(results=maintenance_results)
    workers = min(HEARTBEAT_MAX_WORKERS, len(active_targets))

    def probe(vd: VaultDevice) -> HeartbeatEntry:
        ok = tcp_probe(vd.host, vd.port, timeout_s=payload.timeout_s)
        return HeartbeatEntry(
            device_id=vd.id, reachable=ok, checked_at_iso=timestamp,
            maintenance=False,
        )

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(probe, active_targets))
    return HeartbeatResponse(results=maintenance_results + results)


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
    tuning = tuning_from_settings(settings)
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
    tuning = tuning_from_settings(settings)

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
    tuning = tuning_from_settings(settings)

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
    tuning = tuning_from_settings(settings)

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
    tuning = tuning_from_settings(settings)

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
    column_order: list[str] = []
    xml_by_device: dict[str, bytes | None] = {}
    for vd, xml_bytes, summary in probe_results:
        column_order.append(vd.id)
        columns_info.append(CompareColumnInfo(
            device_id=vd.id,
            device_name=vd.name,
            reachable=xml_bytes is not None,
            summary=summary,
        ))
        xml_by_device[vd.id] = xml_bytes

    if payload.subsystem == "aliases":
        per_device_aliases: dict[str, list[AliasItem] | None] = {
            did: extract_aliases(x) if x is not None else None
            for did, x in xml_by_device.items()
        }
        comparison = compare_aliases(per_device_aliases, column_order)
    elif payload.subsystem == "routes":
        per_device_routes: dict[str, list[RouteItem] | None] = {
            did: extract_routes(x) if x is not None else None
            for did, x in xml_by_device.items()
        }
        comparison = compare_routes(per_device_routes, column_order)
    elif payload.subsystem == "rules":
        per_device_rules: dict[str, list[RuleItem] | None] = {
            did: extract_rules(x) if x is not None else None
            for did, x in xml_by_device.items()
        }
        comparison = compare_rules(per_device_rules, column_order)
    elif payload.subsystem == "unbound":
        per_device_unbound: dict[str, list[UnboundHostItem] | None] = {
            did: extract_unbound_hosts(x) if x is not None else None
            for did, x in xml_by_device.items()
        }
        comparison = compare_unbound_hosts(per_device_unbound, column_order)
    elif payload.subsystem == "unbound-domains":
        per_device_dom: dict[str, list[UnboundDomainItem] | None] = {
            did: extract_unbound_domains(x) if x is not None else None
            for did, x in xml_by_device.items()
        }
        comparison = compare_unbound_domains(per_device_dom, column_order)
    elif payload.subsystem == "unbound-forwards":
        per_device_fwd: dict[str, list[UnboundForwardItem] | None] = {
            did: extract_unbound_forwards(x) if x is not None else None
            for did, x in xml_by_device.items()
        }
        comparison = compare_unbound_forwards(per_device_fwd, column_order)
    else:
        # Schema-Validator stellt das eigentlich sicher, defensiver Fallback
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Subsystem '{payload.subsystem}' nicht unterstuetzt.",
        )

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
                    content=list(cell.content),
                )
                for did, cell in row.cells
            ],
        )
        for row in comparison.rows
    ]
    summary = comparison.summary

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
    tuning = tuning_from_settings(settings)
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
    tuning = tuning_from_settings(settings)
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
# GET /api/inventory/devices/{id}/routes  - Live-Routen pro Geraet
# ---------------------------------------------------------------------------


@router.get(
    "/devices/{device_id}/routes",
    response_model=DeviceRoutesResponse,
)
def get_device_routes(
    device_id: str,
    session: Session = Depends(require_session),
) -> DeviceRoutesResponse:
    """Liefert die Live-Routen eines Geraets via OPNsense-searchroute-API.

    Read-only - Edit/Delete laufen ueber den Plan/Apply-Flow. Pro Eintrag:
    Netzwerk, Gateway, Beschreibung, Disabled-Flag. Wenn das Geraet nicht
    erreichbar ist, kommt eine leere Liste mit ``reachable=False`` zurueck.
    """
    # Spaeter Import, weil routes.py wegen RouteAdapter wiederum HttpClient
    # zieht und ein zirkulaerer Top-Level-Import unerwuenscht waere.
    from opn_cockpit.core.objects._endpoints import ROUTES_SEARCH  # noqa: PLC0415

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
    tuning = tuning_from_settings(settings)
    tgt = HttpTarget(host=device.host, port=device.port, verify=device.tls_verify)
    try:
        with HttpClient(targets=[tgt], tuning=tuning) as client:
            response = client.call(
                tgt, device.api_key, device.api_secret,
                "POST", ROUTES_SEARCH,
                json={"current": 1, "rowCount": -1},
            )
    except OpnCockpitError as exc:
        reason = exc.context.summary or exc.context.error_kind or "unbekannt"
        return DeviceRoutesResponse(
            device_id=device.id,
            device_name=device.name,
            reachable=False,
            summary=f"Routen nicht ladbar: {reason}",
            routes=[],
            checked_at_iso=timestamp,
        )

    try:
        data = response.json()
    except ValueError:
        data = {}
    rows = data.get("rows") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        rows = []

    entries: list[RouteEntryResponse] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw_disabled = row.get("disabled", "0")
        disabled = str(raw_disabled).strip() not in ("", "0", "false", "False")
        entries.append(RouteEntryResponse(
            network=str(row.get("network", "")),
            gateway=str(row.get("gateway", "")),
            descr=str(row.get("descr", "")),
            disabled=disabled,
        ))

    session.touch()
    return DeviceRoutesResponse(
        device_id=device.id,
        device_name=device.name,
        reachable=True,
        summary=f"{len(entries)} Route(n) live geladen.",
        routes=entries,
        checked_at_iso=timestamp,
    )


# ---------------------------------------------------------------------------
# GET /api/inventory/devices/{id}/firewall-rules  - Live-Rules pro Geraet
# ---------------------------------------------------------------------------


@router.get(
    "/devices/{device_id}/firewall-rules",
    response_model=DeviceRulesResponse,
)
def get_device_firewall_rules(
    device_id: str,
    session: Session = Depends(require_session),
) -> DeviceRulesResponse:
    """Liefert die Live-Automation-Filter-Regeln eines Geraets via searchRule.

    Nutzt ``/api/firewall/filter/searchRule`` — die "Automation Rules"-API
    (UI: Firewall -> Automation -> Filter). Ab OPNsense 23.7 in Core
    integriert; vorher als optionales ``os-firewall``-Plugin verfuegbar.
    Klassische "Firewall -> Rules" (XML-Legacy-Editor) sind ueber die API
    NICHT erreichbar — die werden hier auch nicht angezeigt.

    Wenn der Endpoint nicht antwortet (kein API-Privileg, sehr alte
    OPNsense), wird ein Fehler im ``summary`` zurueckgegeben statt 500 -
    die UI rendert das als sauberen Hinweis.
    """
    # Spaete Imports vermeiden zirkulaere Imports zwischen Inventory und
    # objects/firewall_rules.
    from opn_cockpit.core.objects._endpoints import RULE_SEARCH  # noqa: PLC0415
    from opn_cockpit.core.objects.firewall_rules import _row_to_spec  # noqa: PLC0415

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
    tuning = tuning_from_settings(settings)
    tgt = HttpTarget(host=device.host, port=device.port, verify=device.tls_verify)
    try:
        with HttpClient(targets=[tgt], tuning=tuning) as client:
            response = client.call(
                tgt, device.api_key, device.api_secret,
                "POST", RULE_SEARCH,
                json={"current": 1, "rowCount": -1},
            )
    except OpnCockpitError as exc:
        reason = exc.context.summary or exc.context.error_kind or "unbekannt"
        return DeviceRulesResponse(
            device_id=device.id,
            device_name=device.name,
            reachable=False,
            summary=(
                f"Filter-Regeln nicht ladbar: {reason}. "
                "Pruefe das API-Privileg 'Firewall: Rules: Edit' und ob "
                "die OPNsense-Version >=23.7 ist (vorher: optionales "
                "os-firewall-Plugin noetig)."
            ),
            rules=[],
            checked_at_iso=timestamp,
        )

    try:
        data = response.json()
    except ValueError:
        data = {}
    rows = data.get("rows") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        rows = []

    entries: list[RuleEntryResponse] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        uuid = str(row.get("uuid", ""))
        spec = _row_to_spec(row, uuid=uuid)
        entries.append(RuleEntryResponse(
            uuid=spec.uuid,
            enabled=spec.enabled,
            action=spec.action,
            interface=spec.interface,
            direction=spec.direction,
            ipprotocol=spec.ipprotocol,
            protocol=spec.protocol,
            source_net=spec.source_net,
            source_port=spec.source_port,
            source_not=spec.source_not,
            destination_net=spec.destination_net,
            destination_port=spec.destination_port,
            destination_not=spec.destination_not,
            gateway=spec.gateway,
            log=spec.log,
            description=spec.description,
            sequence=spec.sequence,
        ))

    session.touch()
    return DeviceRulesResponse(
        device_id=device.id,
        device_name=device.name,
        reachable=True,
        summary=f"{len(entries)} Regel(n) live geladen.",
        rules=entries,
        checked_at_iso=timestamp,
    )


# ---------------------------------------------------------------------------
# GET /api/inventory/devices/{id}/unbound-hosts  - Live Host-Overrides
# ---------------------------------------------------------------------------


@router.get(
    "/devices/{device_id}/unbound-hosts",
    response_model=DeviceUnboundHostsResponse,
)
def get_device_unbound_hosts(
    device_id: str,
    session: Session = Depends(require_session),
) -> DeviceUnboundHostsResponse:
    """Liefert die Live-Unbound-Host-Overrides eines Geraets."""
    from opn_cockpit.core.objects._endpoints import UNBOUND_HOST_SEARCH  # noqa: PLC0415

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
    tuning = tuning_from_settings(settings)
    tgt = HttpTarget(host=device.host, port=device.port, verify=device.tls_verify)
    try:
        with HttpClient(targets=[tgt], tuning=tuning) as client:
            response = client.call(
                tgt, device.api_key, device.api_secret,
                "POST", UNBOUND_HOST_SEARCH,
                json={"current": 1, "rowCount": -1},
            )
    except OpnCockpitError as exc:
        reason = exc.context.summary or exc.context.error_kind or "unbekannt"
        return DeviceUnboundHostsResponse(
            device_id=device.id,
            device_name=device.name,
            reachable=False,
            summary=f"Host-Overrides nicht ladbar: {reason}",
            hosts=[],
            checked_at_iso=timestamp,
        )

    try:
        data = response.json()
    except ValueError:
        data = {}
    rows = data.get("rows") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        rows = []

    entries: list[UnboundHostEntryResponse] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        uuid = str(row.get("uuid", ""))
        if not uuid:
            continue
        enabled_raw = str(row.get("enabled", "1"))
        entries.append(UnboundHostEntryResponse(
            uuid=uuid,
            enabled=enabled_raw not in ("", "0", "false", "False"),
            host=str(row.get("hostname", row.get("host", ""))).strip(),
            domain=str(row.get("domain", "")).strip(),
            server=str(row.get("server", row.get("rr", ""))).strip(),
            description=str(row.get("description", row.get("descr", ""))).strip(),
        ))

    session.touch()
    return DeviceUnboundHostsResponse(
        device_id=device.id,
        device_name=device.name,
        reachable=True,
        summary=f"{len(entries)} Host-Override(s) live geladen.",
        hosts=entries,
        checked_at_iso=timestamp,
    )


# ---------------------------------------------------------------------------
# GET /api/inventory/devices/{id}/unbound-domains  - Live Domain-Overrides
# ---------------------------------------------------------------------------


@router.get(
    "/devices/{device_id}/unbound-domains",
    response_model=DeviceUnboundDomainsResponse,
)
def get_device_unbound_domains(
    device_id: str,
    session: Session = Depends(require_session),
) -> DeviceUnboundDomainsResponse:
    """Liefert die Live-Unbound-Domain-Overrides (DNS-Weiterleitungen).

    Read-only: Cockpit zeigt sie an, aber das CRUD ist (noch) nicht
    implementiert. Wer Domain-Overrides anlegen/aendern will, muss das
    in der OPNsense-Web-GUI tun (Services -> Unbound DNS -> Overrides).
    """
    from opn_cockpit.core.objects._endpoints import UNBOUND_DOMAIN_SEARCH  # noqa: PLC0415

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
    tuning = tuning_from_settings(settings)
    tgt = HttpTarget(host=device.host, port=device.port, verify=device.tls_verify)
    try:
        with HttpClient(targets=[tgt], tuning=tuning) as client:
            response = client.call(
                tgt, device.api_key, device.api_secret,
                "POST", UNBOUND_DOMAIN_SEARCH,
                json={"current": 1, "rowCount": -1},
            )
    except OpnCockpitError as exc:
        reason = exc.context.summary or exc.context.error_kind or "unbekannt"
        return DeviceUnboundDomainsResponse(
            device_id=device.id,
            device_name=device.name,
            reachable=False,
            summary=f"Domain-Overrides nicht ladbar: {reason}",
            domains=[],
            checked_at_iso=timestamp,
        )

    try:
        data = response.json()
    except ValueError:
        data = {}
    rows = data.get("rows") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        rows = []

    entries: list[UnboundDomainEntryResponse] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        uuid = str(row.get("uuid", ""))
        if not uuid:
            continue
        enabled_raw = str(row.get("enabled", "1"))
        entries.append(UnboundDomainEntryResponse(
            uuid=uuid,
            enabled=enabled_raw not in ("", "0", "false", "False"),
            domain=str(row.get("domain", "")).strip(),
            server=str(row.get("server", row.get("ip", ""))).strip(),
            description=str(row.get("description", row.get("descr", ""))).strip(),
        ))

    session.touch()
    return DeviceUnboundDomainsResponse(
        device_id=device.id,
        device_name=device.name,
        reachable=True,
        summary=f"{len(entries)} Domain-Override(s) live geladen.",
        domains=entries,
        checked_at_iso=timestamp,
    )


# ---------------------------------------------------------------------------
# GET /api/inventory/devices/{id}/unbound-forwards  - Live Query-Forwards
# ---------------------------------------------------------------------------


@router.get(
    "/devices/{device_id}/unbound-forwards",
    response_model=DeviceUnboundForwardsResponse,
)
def get_device_unbound_forwards(
    device_id: str,
    session: Session = Depends(require_session),
) -> DeviceUnboundForwardsResponse:
    """Liefert die Live-Unbound-Query-Forwards (UI-Tab "Query Forwarding").

    Diese Liste sind die globalen Forward-Server, an die Unbound DNS-
    Anfragen weiterleitet (oft DoT/DoH). ``domain`` ist leer fuer
    "alle Queries" oder auf eine Ziel-Domain eingeschraenkt.

    Read-only — CRUD waere ein eigener Adapter.
    """
    from opn_cockpit.core.objects._endpoints import UNBOUND_FORWARD_SEARCH  # noqa: PLC0415

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
    tuning = tuning_from_settings(settings)
    tgt = HttpTarget(host=device.host, port=device.port, verify=device.tls_verify)
    try:
        with HttpClient(targets=[tgt], tuning=tuning) as client:
            response = client.call(
                tgt, device.api_key, device.api_secret,
                "POST", UNBOUND_FORWARD_SEARCH,
                json={"current": 1, "rowCount": -1},
            )
    except OpnCockpitError as exc:
        reason = exc.context.summary or exc.context.error_kind or "unbekannt"
        return DeviceUnboundForwardsResponse(
            device_id=device.id,
            device_name=device.name,
            reachable=False,
            summary=f"Query-Forwards nicht ladbar: {reason}",
            forwards=[],
            checked_at_iso=timestamp,
        )

    try:
        data = response.json()
    except ValueError:
        data = {}
    rows = data.get("rows") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        rows = []

    entries: list[UnboundForwardEntryResponse] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        uuid = str(row.get("uuid", ""))
        if not uuid:
            continue
        enabled_raw = str(row.get("enabled", "1"))
        # OPNsense gibt den Port oft als String zurueck; defensiv parsen.
        port_raw = row.get("port", row.get("forward_tcp_upstream", "53"))
        try:
            port = int(str(port_raw)) if port_raw not in ("", None) else 53
        except (TypeError, ValueError):
            port = 53
        entries.append(UnboundForwardEntryResponse(
            uuid=uuid,
            enabled=enabled_raw not in ("", "0", "false", "False"),
            domain=str(row.get("domain", "")).strip(),
            server=str(row.get("server", row.get("forward_addr", ""))).strip(),
            port=port,
            type=str(row.get("type", "forward")).strip(),
            verify=str(row.get("verify", "")).strip(),
            description=str(row.get("description", row.get("descr", ""))).strip(),
        ))

    session.touch()
    return DeviceUnboundForwardsResponse(
        device_id=device.id,
        device_name=device.name,
        reachable=True,
        summary=f"{len(entries)} Query-Forward(s) live geladen.",
        forwards=entries,
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
    tuning = tuning_from_settings(settings)
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
    tuning = tuning_from_settings(settings)
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
# POST /api/inventory/devices/{id}/backups  - Backup erzeugen (Server-only)
# ---------------------------------------------------------------------------


@router.post(
    "/devices/{device_id}/backups",
    response_model=BackupResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_device_backup(
    device_id: str,
    session: Session = Depends(require_session),
) -> BackupResponse:
    """Erzeugt ein Konfig-Backup ausschliesslich auf dem Server.

    Anders als der GET-Endpoint laedt diese Route NICHTS zum Browser herunter -
    sie holt die aktuelle Konfig von der OPNsense, persistiert sie als
    ``trigger="manual"`` lokal (sichtbar im Backups-Tab) und liefert nur
    die Metadaten. Use-Case: User will "schnell mal sichern" ohne
    Save-Dialog.
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
    tuning = tuning_from_settings(settings)
    try:
        with HttpClient(targets=[target], tuning=tuning) as client:
            content = download_backup(
                client, target, vault_device.api_key, vault_device.api_secret,
            )
    except AuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=(
                f"Backup abgelehnt: {exc.context.summary or 'API-Schluessel ungueltig'}."
            ),
        ) from exc
    except UnreachableError as exc:
        sc = (
            status.HTTP_502_BAD_GATEWAY
            if exc.context.error_kind == "tls"
            else status.HTTP_503_SERVICE_UNAVAILABLE
        )
        raise HTTPException(
            status_code=sc,
            detail=f"Geraet nicht erreichbar: {exc.context.summary or exc.context.error_kind}.",
        ) from exc
    except (ValidationError, ApiError) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"OPNsense-Antwort fehlerhaft: {exc.context.summary or exc.context.error_kind}.",
        ) from exc
    except EgressDeniedError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Egress verweigert: {exc.context.summary or exc.context.error_kind}.",
        ) from exc
    except OpnCockpitError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Backup-Erzeugung fehlgeschlagen ({exc.context.error_kind}): "
                   f"{exc.context.summary or '-'}.",
        ) from exc

    try:
        record = append_backup(
            device_id,
            content,
            trigger="manual",
            device_name_at_creation=vault_device.name,
        )
    except BackupStoreError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Backup-Persistenz fehlgeschlagen: {exc}",
        ) from exc

    try:
        prune_backups(
            device_id,
            retention_pre_apply=settings.backup_retention_pre_apply,
            retention_scheduled=settings.backup_retention_scheduled,
        )
    except BackupStoreError:
        _log.exception(
            "Backup-Pruning fuer device_id=%s nach manueller Erzeugung fehlgeschlagen.",
            device_id,
        )

    get_audit_backend().append(
        AuditEventKind.BACKUP_DOWNLOADED,
        actor=audit_actor(session),
        action="backup_create_server",
        target_device_id=device_id,
        target_device_name=vault_device.name,
        summary=(
            f"Konfig-Backup erzeugt (Server) fuer {vault_device.name} "
            f"({len(content)} Bytes, id={record.id})"
        ),
    )
    session.touch()
    return _record_to_response(record)


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
    # SSH-Felder analog: nur was uebergeben wird, wird gesetzt.
    if payload.ssh_enabled is not None:
        current.ssh_enabled = payload.ssh_enabled
    if payload.ssh_host is not None:
        current.ssh_host = payload.ssh_host
    if payload.ssh_port is not None:
        current.ssh_port = payload.ssh_port
    if payload.ssh_user is not None:
        current.ssh_user = payload.ssh_user
    if payload.ssh_private_key_pem:
        # Key nur ueberschreiben wenn explizit gesetzt - leerer String
        # bedeutet "lass den vorhandenen Key in Ruhe" (analog api_secret).
        current.ssh_private_key_pem = payload.ssh_private_key_pem
    if payload.maintenance is not None:
        current.maintenance = payload.maintenance


def _require_vault_path(session: Session) -> Path:
    vault_path = session.vault_path
    if vault_path is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Tresor-Pfad fehlt in der Session.",
        )
    return vault_path




def _to_device_response(device: Device) -> DeviceResponse:
    # Falls die Device-Quelle eine VaultDevice ist, ziehen wir die SSH-
    # Felder mit; bei reinen Inventory-Devices (Audit-Sicht) defaulten
    # die Felder auf 0/leer.
    ssh_enabled = bool(getattr(device, "ssh_enabled", False))
    ssh_host = str(getattr(device, "ssh_host", ""))
    ssh_port = int(getattr(device, "ssh_port", 22))
    ssh_user = str(getattr(device, "ssh_user", ""))
    ssh_key_present = bool(
        str(getattr(device, "ssh_private_key_pem", "")).strip(),
    )
    return DeviceResponse(
        id=device.id,
        name=device.name,
        host=device.host,
        port=device.port,
        tls_verify=device.tls_verify,
        tags=list(device.tags),
        descr=device.descr,
        ssh_enabled=ssh_enabled,
        ssh_host=ssh_host,
        ssh_port=ssh_port,
        ssh_user=ssh_user,
        ssh_key_present=ssh_key_present,
        maintenance=bool(getattr(device, "maintenance", False)),
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
