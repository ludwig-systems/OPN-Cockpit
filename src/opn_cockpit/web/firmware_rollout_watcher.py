"""FirmwareRolloutWatcher (Iteration B): sequenzielle Sammelaktion.

Startet ein Firmware-Update auf einer Liste von Geraeten, arbeitet sie
**eine nach der anderen** ab (nicht parallel — sonst rebooten mehrere
Boxen gleichzeitig und ganze VLANs koennten kappen). Pro Device durchlaeuft
der Watcher eine State-Machine:

  ``queued`` (in der Queue, noch nicht dran)
    -> ``triggered`` (POST /firmware/update oder /upgrade abgesetzt)
    -> ``running`` (OPNsense meldet ``upgradestatus=running``)
    -> ``rebooting`` (nur bei mode=upgrade: Box unreachable, Reboot laeuft)
    -> ``verifying`` (Box wieder erreichbar, Firmware-Version pruefen)
    -> ``done``    (Version passt oder Update ohne Reboot fertig)
    -> ``failed``  (Fehler an irgendeiner Stelle - Rollout je nach
                    ``continue_on_error`` weiter oder Abbruch)
    -> ``skipped`` (bei Trigger-Zeit war kein Update mehr verfuegbar)

Persistenz: der Watcher schreibt seinen kompletten State nach jeder
Mutation atomar in ``<app_data>/state/firmware-rollout.json``. Beim
Cockpit-Restart lesen wir die Datei und uebernehmen den laufenden
Rollout, sobald jemand den Tresor entsperrt (Session-Adoption via
``vault_path``, analog RetryWatcher/SafetyNetWatcher).

Ein Rollout zur Zeit — der Watcher lehnt einen zweiten ``submit`` ab
solange der aktuelle ``running`` ist. Grund: die Ressourcen (SSH-User,
Netzwerk-Blips beim Reboot) sind global; parallele Rollouts wuerden nur
Chaos machen.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from threading import RLock, Thread

from opn_cockpit.audit.backend import AuditBackend
from opn_cockpit.audit.log import AuditEventKind
from opn_cockpit.core.device_info import (
    UPGRADE_STATUS_DONE,
    UPGRADE_STATUS_ERROR,
    UPGRADE_STATUS_RUNNING,
    UPGRADE_STATUS_UNKNOWN,
    fetch_firmware_status,
    fetch_upgrade_status,
    trigger_firmware_update,
    trigger_firmware_upgrade,
)
from opn_cockpit.core.http_client import (
    HttpClient,
    HttpTarget,
    HttpTuning,
    tuning_from_settings,
)
from opn_cockpit.web.auth.manager import SessionManager

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Konstanten
# ---------------------------------------------------------------------------

DEFAULT_TICK_S = 30.0
DEFAULT_UNREACHABLE_TOLERANCE_S = 15 * 60   # 15 min Reboot-Fenster
DEFAULT_UPGRADE_MAX_S = 30 * 60             # 30 min pro Box (running-Phase)
DEFAULT_ROLLOUT_MAX_S = 6 * 3600            # 6 h Rollout-Total-Cap
REPORT_TTL_S = 3600                          # fertige Rollouts bleiben 1 h sichtbar
QUEUE_FILE_NAME = "firmware-rollout.json"
QUEUE_FORMAT_VERSION = 1

# Device-States
DEV_STATE_QUEUED = "queued"
DEV_STATE_TRIGGERED = "triggered"
DEV_STATE_RUNNING = "running"
DEV_STATE_REBOOTING = "rebooting"
DEV_STATE_VERIFYING = "verifying"
DEV_STATE_DONE = "done"
DEV_STATE_FAILED = "failed"
DEV_STATE_SKIPPED = "skipped"

_DEVICE_TERMINAL_STATES = {DEV_STATE_DONE, DEV_STATE_FAILED, DEV_STATE_SKIPPED}

# Rollout-States
ROLLOUT_STATE_SCHEDULED = "scheduled"   # wartet auf scheduled_start_at_ms
ROLLOUT_STATE_RUNNING = "running"
ROLLOUT_STATE_DONE = "done"
ROLLOUT_STATE_FAILED = "failed"
ROLLOUT_STATE_CANCELLED = "cancelled"

_ROLLOUT_TERMINAL_STATES = {
    ROLLOUT_STATE_DONE, ROLLOUT_STATE_FAILED, ROLLOUT_STATE_CANCELLED,
}
_ROLLOUT_ACTIVE_STATES = {ROLLOUT_STATE_SCHEDULED, ROLLOUT_STATE_RUNNING}


# ---------------------------------------------------------------------------
# Datentypen
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class FirmwareRolloutDevice:
    device_id: str
    device_name: str
    position: int
    state: str
    mode: str = "update"                # update | upgrade
    version_before: str = ""
    target_version: str = ""
    version_after: str = ""
    started_at_ms: int = 0
    finished_at_ms: int = 0
    last_reachable_at_ms: int = 0
    log: str = ""
    summary: str = ""


@dataclass(slots=True)
class FirmwareRollout:
    rollout_id: str
    created_at_ms: int
    vault_path: str
    mode: str                            # update | upgrade
    continue_on_error: bool
    devices: list[FirmwareRolloutDevice] = field(default_factory=list)
    state: str = ROLLOUT_STATE_RUNNING
    finished_at_ms: int = 0
    cancel_requested: bool = False
    initiator: str = ""
    scheduled_start_at_ms: int = 0
    """Unix-ms wann der Rollout starten soll.

    Bei ``0`` startet der Watcher sofort beim naechsten Tick (bisheriges
    Verhalten). Bei ``> now`` bleibt der Rollout in ``ROLLOUT_STATE_SCHEDULED``
    bis der Zeitpunkt erreicht ist, dann wird er auf ``running`` gesetzt und
    die erste Box getriggert. Cancel funktioniert im scheduled-State genauso
    wie im running-State — nur alle noch queued Devices werden skipped.
    """


# ---------------------------------------------------------------------------
# Watcher
# ---------------------------------------------------------------------------


class FirmwareRolloutWatcher:
    """Persistente Rollout-State-Machine. Threadsafe.

    Ein Rollout zur Zeit; zweiter submit wird abgelehnt bis der aktuelle
    in einem terminalen State (done/failed/cancelled) ist. Terminale
    Rollouts bleiben zusaetzlich noch REPORT_TTL_S lang sichtbar damit
    der UI-Banner das Ergebnis anzeigt.
    """

    def __init__(
        self,
        manager: SessionManager,
        audit: AuditBackend,
        *,
        queue_path: Path | None = None,
        tick_s: float = DEFAULT_TICK_S,
    ) -> None:
        self._manager = manager
        self._audit = audit
        self._rollout: FirmwareRollout | None = None
        self._lock = RLock()
        self._thread: Thread | None = None
        self._stop = False
        self._queue_path = queue_path
        self._tick_s = tick_s
        if queue_path is not None:
            self._load_from_disk()
            if (
                self._rollout is not None
                and self._rollout.state == ROLLOUT_STATE_RUNNING
            ):
                self.start()

    # ----- Lifecycle -----

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop = False
            self._thread = Thread(
                target=self._loop,
                daemon=True,
                name="opn-firmware-rollout-watcher",
            )
            self._thread.start()

    def stop(self) -> None:
        with self._lock:
            self._stop = True

    # ----- Public API -----

    def submit(
        self,
        *,
        initiator: str,
        vault_path: str,
        devices: list[tuple[str, str, str]],
        mode: str,
        continue_on_error: bool,
        scheduled_start_at_ms: int = 0,
    ) -> FirmwareRollout:
        """Startet einen neuen Rollout — sofort oder geplant.

        ``devices`` ist eine Liste von Tupeln
        ``(device_id, device_name, target_version)``. ``target_version``
        darf leer sein — Watcher haelt sich dann an OPNsense's Angabe
        beim Trigger-Zeitpunkt.

        ``scheduled_start_at_ms=0`` (Default) startet sofort. Ein Wert
        in der Zukunft schaltet den Rollout in ``ROLLOUT_STATE_SCHEDULED``
        bis der Zeitpunkt erreicht ist. Werte in der Vergangenheit werden
        als "sofort" interpretiert — kein Fehler, damit UI-Uhr-Skew nicht
        zum Absturz fuehrt.

        Wirft ``RolloutBusyError``, wenn bereits ein Rollout aktiv ist
        (running ODER scheduled). ``ValueError`` bei ungueltigem mode oder
        leerer Device-Liste.
        """
        if mode not in ("update", "upgrade"):
            msg = f"Ungueltiger mode: {mode!r} - erwartet 'update' oder 'upgrade'."
            raise ValueError(msg)
        if not devices:
            msg = "Rollout braucht mindestens ein Ziel-Geraet."
            raise ValueError(msg)

        with self._lock:
            if (
                self._rollout is not None
                and self._rollout.state in _ROLLOUT_ACTIVE_STATES
            ):
                raise RolloutBusyError(
                    "Es laeuft bereits ein Firmware-Rollout (oder ist "
                    "geplant). Bitte warten oder erst abbrechen "
                    "(POST /api/firmware/rollout/cancel)."
                )
            now_ms = _now_ms()

            # Scheduled? Nur wenn Zeitpunkt echt in der Zukunft. Werte
            # <= now werden als sofort behandelt (defensiv gegen UI-Skew).
            if scheduled_start_at_ms > now_ms:
                initial_state = ROLLOUT_STATE_SCHEDULED
                scheduled_at = scheduled_start_at_ms
            else:
                initial_state = ROLLOUT_STATE_RUNNING
                scheduled_at = 0

            rollout = FirmwareRollout(
                rollout_id=uuid.uuid4().hex[:12],
                created_at_ms=now_ms,
                vault_path=vault_path,
                mode=mode,
                continue_on_error=continue_on_error,
                initiator=initiator,
                state=initial_state,
                scheduled_start_at_ms=scheduled_at,
                devices=[
                    FirmwareRolloutDevice(
                        device_id=did,
                        device_name=name,
                        position=pos,
                        state=DEV_STATE_QUEUED,
                        mode=mode,
                        target_version=target_v,
                    )
                    for pos, (did, name, target_v) in enumerate(devices)
                ],
            )
            self._rollout = rollout
            self._save_to_disk()

        if initial_state == ROLLOUT_STATE_SCHEDULED:
            self._audit.append(
                AuditEventKind.FIRMWARE_ROLLOUT_STARTED,
                actor=initiator,
                action=f"firmware_rollout_{mode}_scheduled",
                target_count=len(devices),
                summary=(
                    f"Firmware-Rollout ({mode}) geplant fuer "
                    f"{len(devices)} Geraet(e), Start: "
                    f"{scheduled_at} ms."
                ),
            )
        else:
            self._audit.append(
                AuditEventKind.FIRMWARE_ROLLOUT_STARTED,
                actor=initiator,
                action=f"firmware_rollout_{mode}",
                target_count=len(devices),
                summary=(
                    f"Firmware-Rollout ({mode}) gestartet fuer "
                    f"{len(devices)} Geraet(e)."
                ),
            )
        self.start()
        return rollout

    def cancel(self, *, initiator: str) -> bool:
        """Bricht den laufenden oder geplanten Rollout ab.

        * ``running``: die aktuell laufende Box wird fertiggemacht
          (Watcher pollt den upgradestatus zu Ende), noch nicht getriggerte
          Devices werden auf ``skipped`` gesetzt, danach terminal-State
          ``cancelled``.
        * ``scheduled``: sofortiger Uebergang zu ``cancelled``, keine
          Aktion auf Boxen. Alle Devices bleiben ``queued`` → ``skipped``.

        Liefert True wenn ein aktiver Rollout gefunden und markiert
        wurde, False sonst.
        """
        with self._lock:
            rollout = self._rollout
            if rollout is None or rollout.state not in _ROLLOUT_ACTIVE_STATES:
                return False
            rollout.cancel_requested = True
            self._save_to_disk()

        self._audit.append(
            AuditEventKind.FIRMWARE_ROLLOUT_CANCELLED,
            actor=initiator,
            action="firmware_rollout_cancel_requested",
            summary=(
                f"Firmware-Rollout {rollout.rollout_id} ({rollout.state}) "
                "auf Abbruch gesetzt."
            ),
        )
        return True

    def stats(self) -> FirmwareRollout | None:
        with self._lock:
            return self._rollout

    def clear_terminal(self) -> bool:
        """Loescht den zuletzt fertiggestellten Rollout aus der Anzeige.

        Nur erlaubt wenn Rollout in terminalem State ist. Wird vom
        UI aufgerufen wenn der User den Banner wegklickt.
        """
        with self._lock:
            if self._rollout is None:
                return False
            if self._rollout.state not in _ROLLOUT_TERMINAL_STATES:
                return False
            self._rollout = None
            self._save_to_disk()
        return True

    # ----- Loop -----

    def _loop(self) -> None:
        while True:
            time.sleep(self._tick_s)
            if self._stop:
                return
            try:
                self._tick(_now_ms())
            except Exception:  # noqa: BLE001
                _log.exception("FirmwareRolloutWatcher-Tick crashte")

    def _tick(self, now_ms: int) -> None:
        with self._lock:
            rollout = self._rollout

        if rollout is None:
            return

        # Terminal + TTL abgelaufen: aufraeumen.
        if rollout.state in _ROLLOUT_TERMINAL_STATES:
            if rollout.finished_at_ms > 0:
                if now_ms - rollout.finished_at_ms > REPORT_TTL_S * 1000:
                    with self._lock:
                        self._rollout = None
                        self._save_to_disk()
            return

        # Cancel angefragt: alle noch-queued Devices skippen, dann prüfen ob
        # die aktive Box terminal ist -> Rollout terminieren.
        # Bei scheduled reicht Cancel-Flag -> sofortiger Uebergang.
        if rollout.cancel_requested:
            if rollout.state == ROLLOUT_STATE_SCHEDULED:
                self._skip_remaining(
                    rollout, reason="Rollout vor Start abgebrochen.",
                )
                self._finalize(rollout, ROLLOUT_STATE_CANCELLED, now_ms)
                return
            active = self._find_active_device(rollout)
            if active is None or active.state in _DEVICE_TERMINAL_STATES:
                self._skip_remaining(rollout, reason="Rollout abgebrochen.")
                self._finalize(rollout, ROLLOUT_STATE_CANCELLED, now_ms)
                return

        # Scheduled: warten bis der Zeitpunkt da ist, dann uebergehen auf
        # running. Der Rest des Ticks laeuft dann normal weiter.
        if rollout.state == ROLLOUT_STATE_SCHEDULED:
            if now_ms < rollout.scheduled_start_at_ms:
                return  # noch warten, nichts zu tun
            with self._lock:
                rollout.state = ROLLOUT_STATE_RUNNING
                self._save_to_disk()
            self._audit.append(
                AuditEventKind.FIRMWARE_ROLLOUT_STARTED,
                actor=rollout.initiator,
                action=f"firmware_rollout_{rollout.mode}_start_from_schedule",
                target_count=len(rollout.devices),
                summary=(
                    f"Firmware-Rollout {rollout.rollout_id} startet jetzt "
                    "(geplanter Zeitpunkt erreicht)."
                ),
            )

        # Total-Cap greift? Bei scheduled zaehlen wir ab created_at_ms,
        # inklusive Wartezeit — sonst blockiert ein "in 4h starten"-Rollout
        # einen zweiten. Aber 6h ist grosszuegig.
        if now_ms - rollout.created_at_ms > DEFAULT_ROLLOUT_MAX_S * 1000:
            self._skip_remaining(rollout, reason="Rollout-Total-Timeout (6h).")
            self._finalize(rollout, ROLLOUT_STATE_FAILED, now_ms)
            return

        # Aktive Box abarbeiten (oder naechste queued starten).
        self._process(rollout, now_ms)

        # State persistieren.
        with self._lock:
            self._save_to_disk()

        # Alle terminal? Dann rollout finalen State setzen.
        if all(d.state in _DEVICE_TERMINAL_STATES for d in rollout.devices):
            any_failed = any(d.state == DEV_STATE_FAILED for d in rollout.devices)
            self._finalize(
                rollout,
                ROLLOUT_STATE_FAILED if any_failed else ROLLOUT_STATE_DONE,
                now_ms,
            )

    def _find_active_device(
        self, rollout: FirmwareRollout,
    ) -> FirmwareRolloutDevice | None:
        for d in rollout.devices:
            if d.state not in {DEV_STATE_QUEUED, *_DEVICE_TERMINAL_STATES}:
                return d
        return None

    def _process(self, rollout: FirmwareRollout, now_ms: int) -> None:
        """Sequenzieller Kern: eine Box zur Zeit vorwaertsbringen."""
        active = self._find_active_device(rollout)
        if active is None:
            # Keine aktive Box - naechste queued starten (wenn nicht cancel).
            if rollout.cancel_requested:
                return
            next_queued = next(
                (d for d in rollout.devices if d.state == DEV_STATE_QUEUED),
                None,
            )
            if next_queued is None:
                return
            self._trigger_device(rollout, next_queued, now_ms)
            return

        # Aktive Box vorantreiben.
        self._advance_device(rollout, active, now_ms)

    def _trigger_device(
        self,
        rollout: FirmwareRollout,
        device: FirmwareRolloutDevice,
        now_ms: int,
    ) -> None:
        """Setzt POST /update oder /upgrade auf einer Box ab."""
        vault_device = self._lookup_vault_device(rollout, device.device_id)
        if vault_device is None:
            device.summary = "Tresor nicht entsperrt - warte auf Session."
            device.last_reachable_at_ms = now_ms
            return

        with self._build_client(rollout, vault_device) as client:
            target = HttpTarget(
                host=vault_device.host,
                port=vault_device.port,
                verify=vault_device.tls_verify,
            )
            fw = fetch_firmware_status(
                client, target, vault_device.api_key, vault_device.api_secret,
            )
            if not fw.reachable or not fw.authenticated:
                device.summary = f"Nicht erreichbar/Auth-Fehler: {fw.summary}"
                device.last_reachable_at_ms = now_ms
                self._mark_device_failed(rollout, device, now_ms)
                return
            device.version_before = fw.version
            if not fw.update_available:
                device.state = DEV_STATE_SKIPPED
                device.started_at_ms = now_ms
                device.finished_at_ms = now_ms
                device.summary = f"Kein Update verfuegbar (aktuell v{fw.version})."
                device.log = ""
                return

            device.target_version = device.target_version or fw.new_version
            if rollout.mode == "upgrade":
                ok, msg = trigger_firmware_upgrade(
                    client, target, vault_device.api_key, vault_device.api_secret,
                )
            else:
                ok, msg = trigger_firmware_update(
                    client, target, vault_device.api_key, vault_device.api_secret,
                )

        if not ok:
            device.summary = f"Trigger fehlgeschlagen: {msg}"
            self._mark_device_failed(rollout, device, now_ms)
            return

        device.state = DEV_STATE_TRIGGERED
        device.started_at_ms = now_ms
        device.last_reachable_at_ms = now_ms
        device.summary = msg

        self._audit.append(
            AuditEventKind.FIRMWARE_UPDATE_STARTED,
            actor=rollout.initiator,
            action=f"firmware_rollout_{rollout.mode}_started",
            target_device_id=device.device_id,
            target_device_name=device.device_name,
            summary=(
                f"[Rollout {rollout.rollout_id}] {rollout.mode} "
                f"auf {device.device_name}: {msg}"
            ),
        )

    def _advance_device(
        self,
        rollout: FirmwareRollout,
        device: FirmwareRolloutDevice,
        now_ms: int,
    ) -> None:
        """State-Machine-Schritt fuer eine bereits laufende Box."""
        vault_device = self._lookup_vault_device(rollout, device.device_id)
        if vault_device is None:
            device.summary = "Tresor nicht entsperrt - warte auf Session."
            return

        # Zeit-Timeout pro Box?
        if now_ms - device.started_at_ms > DEFAULT_UPGRADE_MAX_S * 1000:
            # Aber Reboot ist erlaubt, solange Unreachable-Toleranz noch da.
            reachable_lag = now_ms - device.last_reachable_at_ms
            if device.state == DEV_STATE_REBOOTING and reachable_lag < DEFAULT_UNREACHABLE_TOLERANCE_S * 1000:
                pass  # noch im Reboot-Fenster
            else:
                device.summary = f"Timeout ({DEFAULT_UPGRADE_MAX_S//60} min) ueberschritten."
                self._mark_device_failed(rollout, device, now_ms)
                return

        with self._build_client(rollout, vault_device) as client:
            target = HttpTarget(
                host=vault_device.host,
                port=vault_device.port,
                verify=vault_device.tls_verify,
            )

            # Reboot-Zustand: nur health-check, kein upgradestatus (Box down).
            if device.state == DEV_STATE_REBOOTING:
                fw = fetch_firmware_status(
                    client, target, vault_device.api_key, vault_device.api_secret,
                )
                if fw.reachable:
                    device.state = DEV_STATE_VERIFYING
                    device.last_reachable_at_ms = now_ms
                    device.summary = (
                        f"Box wieder erreichbar - Version pruefen "
                        f"({fw.version})"
                    )
                    device.version_after = fw.version
                    self._maybe_finish_verifying(rollout, device, fw, now_ms)
                    return
                # Immer noch weg - checken ob wir das Fenster gerissen haben.
                lag = now_ms - device.last_reachable_at_ms
                if lag > DEFAULT_UNREACHABLE_TOLERANCE_S * 1000:
                    device.summary = (
                        f"Box seit {lag // 60000} min nicht erreichbar - "
                        "Reboot-Fenster ueberschritten."
                    )
                    self._mark_device_failed(rollout, device, now_ms)
                    return
                device.summary = "Box im Reboot - warte weiter…"
                return

            # Verifying-Zustand: Version-Check erneut.
            if device.state == DEV_STATE_VERIFYING:
                fw = fetch_firmware_status(
                    client, target, vault_device.api_key, vault_device.api_secret,
                )
                if fw.reachable:
                    device.version_after = fw.version
                    device.last_reachable_at_ms = now_ms
                    self._maybe_finish_verifying(rollout, device, fw, now_ms)
                return

            # Standard-Fall: upgradestatus abfragen.
            us = fetch_upgrade_status(
                client, target, vault_device.api_key, vault_device.api_secret,
            )

        # Nicht erreichbar - moeglicherweise Reboot begonnen.
        if not us.reachable:
            if rollout.mode == "upgrade":
                device.state = DEV_STATE_REBOOTING
                device.summary = "Box unreachable - Reboot vermutet."
                return
            # update-Modus sollte nicht rebooten. Wir tolerieren ein
            # kurzes unreachable (Service-Restart?), zaehlen die Lag-Zeit.
            lag = now_ms - device.last_reachable_at_ms
            if lag > 3 * 60 * 1000:  # 3 min Toleranz im update-Modus
                device.summary = f"Box seit {lag // 60000} min nicht erreichbar (update)."
                self._mark_device_failed(rollout, device, now_ms)
            else:
                device.summary = "Kurz nicht erreichbar - warte…"
            return

        device.last_reachable_at_ms = now_ms
        if us.log:
            device.log = us.log

        if us.status == UPGRADE_STATUS_RUNNING:
            device.state = DEV_STATE_RUNNING
            device.summary = us.summary
            return

        if us.status == UPGRADE_STATUS_DONE:
            # update-Modus: fertig, verifying uebersprungen (kein Reboot).
            # upgrade-Modus: OPNsense meldet done aber Reboot kommt noch;
            # bei rebooting-Detection greift der State-Wechsel oben.
            if rollout.mode == "update":
                device.state = DEV_STATE_VERIFYING
                # Ein direkter Version-Check falls die Box schon fertig ist.
                with self._build_client(rollout, vault_device) as client:
                    target = HttpTarget(
                        host=vault_device.host,
                        port=vault_device.port,
                        verify=vault_device.tls_verify,
                    )
                    fw = fetch_firmware_status(
                        client, target,
                        vault_device.api_key, vault_device.api_secret,
                    )
                if fw.reachable:
                    device.version_after = fw.version
                    self._maybe_finish_verifying(rollout, device, fw, now_ms)
                return
            # upgrade-Modus: warte auf Reboot-Detection im naechsten Tick.
            device.summary = "Upgrade fertig, Reboot steht bevor…"
            return

        if us.status == UPGRADE_STATUS_ERROR:
            device.summary = f"OPNsense meldet Fehler: {us.summary}"
            self._mark_device_failed(rollout, device, now_ms)
            return

        # UNKNOWN: nicht dramatisch, weiter pollen. Nur summary aktualisieren.
        device.summary = us.summary

    def _maybe_finish_verifying(
        self,
        rollout: FirmwareRollout,
        device: FirmwareRolloutDevice,
        fw: "object",
        now_ms: int,
    ) -> None:
        """Version-Check nach dem Update: passt sie? Dann done."""
        # duck-typed FirmwareStatus - hat .update_available, .version, .new_version
        target = device.target_version or ""
        current = getattr(fw, "version", "")
        still_available = getattr(fw, "update_available", False)
        device.version_after = current

        if target and current and current != target and still_available:
            device.summary = (
                f"Version {current} - Target war {target}, weiter Update verfuegbar."
            )
            self._mark_device_failed(rollout, device, now_ms)
            return

        if still_available and not target:
            # Ohne target koennen wir nur schauen ob's noch was gibt.
            device.summary = f"Update fertig, aktuell v{current} - weiter verfuegbar."
            self._mark_device_failed(rollout, device, now_ms)
            return

        # Alles gut.
        device.state = DEV_STATE_DONE
        device.finished_at_ms = now_ms
        device.summary = (
            f"Fertig - v{device.version_before} -> v{current}"
            if device.version_before and current
            else "Fertig."
        )
        self._audit.append(
            AuditEventKind.FIRMWARE_UPDATE_COMPLETED,
            actor=rollout.initiator,
            action=f"firmware_rollout_{rollout.mode}_completed",
            target_device_id=device.device_id,
            target_device_name=device.device_name,
            summary=(
                f"[Rollout {rollout.rollout_id}] {device.device_name}: "
                f"v{device.version_before} -> v{current}"
            ),
        )

    def _mark_device_failed(
        self,
        rollout: FirmwareRollout,
        device: FirmwareRolloutDevice,
        now_ms: int,
    ) -> None:
        device.state = DEV_STATE_FAILED
        device.finished_at_ms = now_ms
        self._audit.append(
            AuditEventKind.FIRMWARE_UPDATE_FAILED,
            actor=rollout.initiator,
            action=f"firmware_rollout_{rollout.mode}_failed",
            target_device_id=device.device_id,
            target_device_name=device.device_name,
            summary=(
                f"[Rollout {rollout.rollout_id}] {device.device_name} "
                f"fehlgeschlagen: {device.summary}"
            ),
        )
        # continue_on_error=False: Rest der Queue skippen.
        if not rollout.continue_on_error:
            for d in rollout.devices:
                if d.state == DEV_STATE_QUEUED:
                    d.state = DEV_STATE_SKIPPED
                    d.finished_at_ms = now_ms
                    d.summary = "Uebersprungen (Rollout-Abbruch nach Fehler)."

    def _skip_remaining(
        self, rollout: FirmwareRollout, *, reason: str,
    ) -> None:
        now_ms = _now_ms()
        for d in rollout.devices:
            if d.state == DEV_STATE_QUEUED:
                d.state = DEV_STATE_SKIPPED
                d.finished_at_ms = now_ms
                d.summary = reason

    def _finalize(
        self,
        rollout: FirmwareRollout,
        final_state: str,
        now_ms: int,
    ) -> None:
        rollout.state = final_state
        rollout.finished_at_ms = now_ms
        with self._lock:
            self._save_to_disk()
        self._audit.append(
            AuditEventKind.FIRMWARE_ROLLOUT_COMPLETED,
            actor=rollout.initiator,
            action=f"firmware_rollout_{final_state}",
            summary=(
                f"Firmware-Rollout {rollout.rollout_id} beendet "
                f"({final_state})."
            ),
        )

    # ----- Session-Adoption + HTTP-Client -----

    def _find_session_for_rollout(self, rollout: FirmwareRollout):
        """Sucht eine aktive Session zum Vault-Pfad des Rollouts.

        Bei Server-Restart oder Session-Lock sind keine Sessions offen -
        dann kommt None zurueck und der Watcher wartet den naechsten
        Tick ab (User oeffnet Tresor, dann adoptieren wir).
        """
        if not rollout.vault_path:
            return None
        target = Path(rollout.vault_path)
        try:
            sessions = list(self._manager.sessions())
        except Exception:  # noqa: BLE001
            return None
        for session in sessions:
            opened = getattr(session, "opened", None)
            if opened is None:
                continue
            vp = getattr(opened, "vault_path", None)
            if vp is None or not _same_path(vp, target):
                continue
            return session
        return None

    def _lookup_vault_device(self, rollout: FirmwareRollout, device_id: str):
        session = self._find_session_for_rollout(rollout)
        if session is None:
            return None
        for d in session.opened.data.devices:
            if d.id == device_id:
                return d
        return None

    def _build_client(self, rollout: FirmwareRollout, vault_device):
        """Baut einen HttpClient mit denselben Trust-CAs wie der User-Vault."""
        session = self._find_session_for_rollout(rollout)
        settings = session.opened.data.settings if session else None
        tuning = (
            tuning_from_settings(settings) if settings is not None else HttpTuning()
        )
        target = HttpTarget(
            host=vault_device.host,
            port=vault_device.port,
            verify=vault_device.tls_verify,
        )
        return HttpClient(targets=[target], tuning=tuning)

    # ----- Persistenz -----

    def _save_to_disk(self) -> None:
        if self._queue_path is None:
            return
        try:
            self._queue_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": QUEUE_FORMAT_VERSION,
                "rollout": asdict(self._rollout) if self._rollout is not None else None,
            }
            tmp_path = self._queue_path.with_suffix(".tmp")
            tmp_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp_path, self._queue_path)
            # D1 (SECURITY-AUDIT-v0.11): 0600 damit lokale Non-Cockpit-User
            # nicht den Vault-Pfad + Rollout-Metadaten lesen. Auf Windows
            # ist chmod ein No-Op; die NTFS-ACL des Service-Users greift
            # dort schon durch <app_data>-Owner.
            with contextlib.suppress(OSError):
                os.chmod(self._queue_path, 0o600)
        except OSError as exc:
            _log.warning(
                "FirmwareRolloutWatcher: Persistenz fehlgeschlagen (%s)", exc,
            )

    def _load_from_disk(self) -> None:
        if self._queue_path is None or not self._queue_path.exists():
            return
        try:
            raw = self._queue_path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, ValueError) as exc:
            _log.warning(
                "FirmwareRolloutWatcher: Persistierte Queue nicht lesbar (%s)", exc,
            )
            return
        version = data.get("version")
        if version != QUEUE_FORMAT_VERSION:
            return
        raw_rollout = data.get("rollout")
        if raw_rollout is None:
            return
        with contextlib.suppress(Exception):
            devices_raw = raw_rollout.pop("devices", [])
            rollout = FirmwareRollout(**raw_rollout)
            rollout.devices = [FirmwareRolloutDevice(**d) for d in devices_raw]
            self._rollout = rollout


# ---------------------------------------------------------------------------
# Fehler
# ---------------------------------------------------------------------------


class RolloutBusyError(RuntimeError):
    """Ein zweiter submit(), waehrend bereits ein Rollout laeuft."""


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _now_ms() -> int:
    return int(time.time() * 1000)


def _same_path(a: Path | None, b: Path) -> bool:
    """Vergleicht zwei Pfade tolerant (mit resolve, Fallback auf str).

    Identisches Muster wie in safety_net_watcher._same_path — bewusst
    hier dupliziert damit die Watcher-Module unabhaengig voneinander
    veraenderbar bleiben.
    """
    if a is None:
        return False
    with contextlib.suppress(OSError):
        return a.resolve() == b.resolve()
    return str(a) == str(b)


__all__ = [
    "DEV_STATE_DONE",
    "DEV_STATE_FAILED",
    "DEV_STATE_QUEUED",
    "DEV_STATE_REBOOTING",
    "DEV_STATE_RUNNING",
    "DEV_STATE_SKIPPED",
    "DEV_STATE_TRIGGERED",
    "DEV_STATE_VERIFYING",
    "FirmwareRollout",
    "FirmwareRolloutDevice",
    "FirmwareRolloutWatcher",
    "QUEUE_FILE_NAME",
    "ROLLOUT_STATE_CANCELLED",
    "ROLLOUT_STATE_DONE",
    "ROLLOUT_STATE_FAILED",
    "ROLLOUT_STATE_RUNNING",
    "ROLLOUT_STATE_SCHEDULED",
    "RolloutBusyError",
]
