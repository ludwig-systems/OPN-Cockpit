"""Phasen-Pipeline-Executor für die Plan-Apply-Hälfte.

Pro Gerät pro Subsystem (R-RUN-1):

1. **WRITE-Phase** — alle ``adapter.add``-Calls für nicht-SKIP-Aktionen.
2. **ACTIVATE-Phase** — genau **ein** ``controller.reconfigure`` aufrufen.
3. **VERIFY-Phase** — alle ``adapter.verify``-Calls; Erfolg gilt nur,
   wenn der Read-back jedes geschriebene Objekt findet (R-RUN-2).

Best-Effort (R-ACT-4, R-RUN-4): ein Gerätefehler blockiert die übrigen
nicht; jede unerwartete Exception wird im Per-Gerät-Wrapper gefangen und
zu einem FAILED-Result. Die Geräte laufen parallel im ThreadPoolExecutor
(R-NFR-2).

Audit-Schreibvorgänge (R-LOG-1): APPLY_STARTED am Anfang, DEVICE_RESULT
pro Gerät, APPLY_COMPLETED am Ende — alle mit maskierten Parametern.
"""

from __future__ import annotations

import contextlib
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from opn_cockpit.audit.backend import AuditBackend
from opn_cockpit.audit.log import AuditEventKind
from opn_cockpit.backups import (
    BackupRecord,
    BackupStoreError,
    append_backup,
    prune_backups,
    read_backup_content,
)
from opn_cockpit.core import ssh_safety_net
from opn_cockpit.core.device_info import download_backup
from opn_cockpit.core.errors import (
    OpnCockpitError,
    ReconfigureError,
    VerificationError,
    make_context,
)
from opn_cockpit.core.http_client import HttpClient, HttpTarget
from opn_cockpit.core.objects.base import (
    ActionKind,
    DiffKind,
    ObjectAdapter,
    RequestContext,
    SubsystemController,
)
from opn_cockpit.core.result import (
    AddOutcome,
    Phase,
    Result,
    RolloutReport,
    Status,
    VerifyOutcome,
)
from opn_cockpit.inventory.model import Device
from opn_cockpit.orchestration.planner import Plan, PlannedDeviceAction
from opn_cockpit.security.masking import mask_dict
from opn_cockpit.security.session import Session
from opn_cockpit.vault.model import VaultDevice

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# DevicePipeline
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DevicePipeline:
    """Alle Aktionen, die ein einzelnes Gerät in dieser Apply-Welle bekommt.

    In v1 ist das genau eine Aktion (eine Route bzw. ein Alias); die
    Struktur ist so geschnitten, dass Bulk-Import (Schritt 10) mehrere
    Specs für dasselbe Gerät bündeln kann.
    """

    device: Device
    actions: tuple[PlannedDeviceAction, ...]


def group_by_device(plan: Plan) -> list[DevicePipeline]:
    grouped: dict[str, list[PlannedDeviceAction]] = {}
    order: list[str] = []
    for action in plan.actions:
        if action.device.id not in grouped:
            grouped[action.device.id] = []
            order.append(action.device.id)
        grouped[action.device.id].append(action)
    return [
        DevicePipeline(
            device=grouped[dev_id][0].device,
            actions=tuple(grouped[dev_id]),
        )
        for dev_id in order
    ]


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SafetyNetContext:
    """Pro-Apply Konfiguration des Dead-Man's-Switch.

    ``active`` ist die User-Entscheidung im Confirm-Modal (Checkbox).
    Wenn False, laeuft der Apply komplett ohne Safety-Net.

    Wenn True, prueft der Executor pro Geraet:
    - hat die VaultDevice SSH-Config? Falls nein: Apply normal,
      ``safety_net_status=""`` im Result, kein Net.
    - falls ja: Pre-Apply-XML auf die Box pushen, daemon-Timer starten,
      apply, disarmen.

    ``watcher`` ist die optionale ``SafetyNetWatcher``-Instanz (oder
    None im CLI-Modus). Wenn der Executor disarm nicht hinkriegt,
    bekommt der Watcher die pending-Aufgabe.
    """

    active: bool = False
    window_s: int = 300
    watcher: Any = None  # Forward-Decl statt zirkulaerem Import


@dataclass(slots=True)
class Executor:
    """Führt einen :class:`Plan` aus.

    Bekommt Session (für Credentials), Audit-Log und HttpClient bei der
    Anwendung — keine versteckten Mitglieder, die Tests umgehen müssen.

    ``backup_storage_root`` ist eine Test-Injection — produktiv None,
    dann nutzt der Backup-Layer ``get_app_data_dir() / "backups"``.
    """

    session: Session
    audit: AuditBackend
    max_workers: int = 8
    backup_storage_root: Path | None = field(default=None)

    def apply(
        self,
        plan: Plan,
        *,
        adapter: ObjectAdapter[Any, Any],
        controller: SubsystemController,
        client: HttpClient,
        safety_net: SafetyNetContext | None = None,
    ) -> RolloutReport:
        """Rollt ``plan`` aus und liefert einen aggregierten Report."""
        pipelines = group_by_device(plan)
        sn = safety_net or SafetyNetContext()
        self.audit.append(
            AuditEventKind.APPLY_STARTED,
            action=plan.action,
            target_count=len(pipelines),
            summary=(
                f"Plan {plan.plan_id} wird auf {len(pipelines)} Gerät(e) ausgerollt"
                + (
                    f" (Safety-Net armed, Window={sn.window_s}s)."
                    if sn.active
                    else "."
                )
            ),
        )

        if not pipelines:
            self.audit.append(
                AuditEventKind.APPLY_COMPLETED,
                action=plan.action,
                target_count=0,
                summary=f"Plan {plan.plan_id} hatte keine Ziele.",
            )
            return RolloutReport(results=())

        workers = min(self.max_workers, len(pipelines))
        results: list[Result] = []
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(
                    self._execute_one,
                    pipeline=pipeline,
                    adapter=adapter,
                    controller=controller,
                    client=client,
                    action_name=plan.action,
                    action_kind=plan.action_kind,
                    plan_id=plan.plan_id,
                    safety_net=sn,
                ): pipeline
                for pipeline in pipelines
            }
            for future in as_completed(futures):
                result = future.result()
                results.append(result)
                self._audit_device_result(plan, futures[future].device, result)

        report = RolloutReport(results=tuple(results))
        self.audit.append(
            AuditEventKind.APPLY_COMPLETED,
            action=plan.action,
            target_count=report.total,
            summary=(
                f"Plan {plan.plan_id} fertig: "
                f"{report.successes} ok, {report.failures} fehlgeschlagen, "
                f"{report.skipped} übersprungen."
            ),
        )
        return report

    # ----- Per-Gerät-Pipeline -----

    def _execute_one(
        self,
        *,
        pipeline: DevicePipeline,
        adapter: ObjectAdapter[Any, Any],
        controller: SubsystemController,
        client: HttpClient,
        action_name: str,
        action_kind: ActionKind,
        plan_id: str,
        safety_net: SafetyNetContext,
    ) -> Result:
        """Phasen-Pipeline für genau ein Gerät. Wirft NIE eine Exception."""
        start = time.monotonic()
        device = pipeline.device
        try:
            return self._run_phases(
                pipeline=pipeline,
                adapter=adapter,
                controller=controller,
                client=client,
                plan_id=plan_id,
                action_kind=action_kind,
                start=start,
                safety_net=safety_net,
            )
        except OpnCockpitError as exc:
            # Letzte Bastion: jeder durchgeschlüpfte Tool-Fehler wird zu FAILED.
            return _make_failed(
                device=device,
                subsystem=adapter.subsystem,
                phase=Phase.WRITE,
                error_kind=exc.context.error_kind or "unknown",
                summary=exc.context.summary or str(exc),
                start=start,
            )
        except Exception as exc:
            # Wirklich alles fangen — der Pool darf nicht crashen wegen eines
            # Programmierfehlers in einem Adapter.
            return _make_failed(
                device=device,
                subsystem=adapter.subsystem,
                phase=Phase.WRITE,
                error_kind="unexpected",
                summary=f"Unerwarteter Fehler: {type(exc).__name__}",
                start=start,
            )

    def _run_phases(
        self,
        *,
        pipeline: DevicePipeline,
        adapter: ObjectAdapter[Any, Any],
        controller: SubsystemController,
        client: HttpClient,
        plan_id: str,
        action_kind: ActionKind,
        start: float,
        safety_net: SafetyNetContext,
    ) -> Result:
        device = pipeline.device
        to_write = [a for a in pipeline.actions if a.diff.kind is not DiffKind.SKIP]

        if not to_write:
            return Result(
                device_id=device.id,
                subsystem=adapter.subsystem,
                status=Status.SKIPPED,
                short_message="Alle Aktionen bereits vorhanden — nichts zu tun.",
                duration_ms=_elapsed_ms(start),
            )

        target = HttpTarget(host=device.host, port=device.port, verify=device.tls_verify)
        try:
            key, secret = self.session.credentials_for(device.id)
        except OpnCockpitError as exc:
            return _make_failed(
                device=device,
                subsystem=adapter.subsystem,
                phase=Phase.WRITE,
                error_kind=exc.context.error_kind or "credentials",
                summary=exc.context.summary or "Credentials nicht verfügbar.",
                start=start,
            )
        ctx = RequestContext(target=target, key=key, secret=secret)
        try:
            settings = self.session.opened.data.settings
            vault_device = self._lookup_vault_device(device.id)
            # Pre-Apply-Backup ist die Grundlage fuer das Safety-Net
            # (Restore-Anker). Wenn Safety-Net aktiv ist, erzwingen wir
            # den Backup unabhaengig von ``auto_backup_before_apply`` -
            # ohne Backup kein Rollback-Anker, dann ist der Dead-Man
            # nutzlos.
            need_backup = settings.auto_backup_before_apply or (
                safety_net.active and self._device_has_ssh(vault_device)
            )
            pre_apply_record: BackupRecord | None = None
            if need_backup:
                backup_failure, pre_apply_record = self._take_pre_apply_backup(
                    client=client,
                    target=target,
                    key=key,
                    secret=secret,
                    device=device,
                    plan_id=plan_id,
                )
                if backup_failure is not None:
                    return _make_failed(
                        device=device,
                        subsystem=adapter.subsystem,
                        phase=Phase.WRITE,
                        error_kind="backup_blocked",
                        summary=(
                            "Pre-Apply-Backup fehlgeschlagen, Apply blockiert: "
                            + backup_failure
                        ),
                        start=start,
                    )

            # Safety-Net armen wenn aktiv und Geraet SSH-faehig.
            armed_jobid = ""
            if (
                safety_net.active
                and self._device_has_ssh(vault_device)
                and pre_apply_record is not None
                and vault_device is not None
            ):
                arm_failure = self._arm_safety_net(
                    device=device,
                    vault_device=vault_device,
                    pre_apply_record=pre_apply_record,
                    plan_id=plan_id,
                    window_s=safety_net.window_s,
                )
                if arm_failure:
                    return _make_failed(
                        device=device,
                        subsystem=adapter.subsystem,
                        phase=Phase.WRITE,
                        error_kind="safety_net_arm_failed",
                        summary=(
                            "Safety-Net konnte nicht aktiviert werden, Apply nicht "
                            "durchgefuehrt: " + arm_failure
                        ),
                        start=start,
                    )
                armed_jobid = ssh_safety_net.make_jobid(plan_id, device.id)

            try:
                result = self._write_activate_verify(
                    device=device,
                    adapter=adapter,
                    controller=controller,
                    client=client,
                    ctx=ctx,
                    to_write=to_write,
                    action_kind=action_kind,
                    plan_id=plan_id,
                    start=start,
                )
            except BaseException:
                # Wenn _write_activate_verify aus einem Programmierfehler
                # raus crashed, MUESSEN wir den daemon noch killen -
                # sonst rebootet die Box ohne Grund. Best-Effort, ohne
                # die Exception zu verschlucken.
                if armed_jobid and vault_device is not None:
                    self._best_effort_disarm(
                        device=device,
                        vault_device=vault_device,
                        jobid=armed_jobid,
                        plan_id=plan_id,
                    )
                raise

            # Disarm-Phase (nur wenn wir armed waren).
            if armed_jobid and vault_device is not None and pre_apply_record is not None:
                result = self._disarm_safety_net(
                    result=result,
                    device=device,
                    vault_device=vault_device,
                    jobid=armed_jobid,
                    plan_id=plan_id,
                    pre_apply_record=pre_apply_record,
                    window_s=safety_net.window_s,
                    watcher=safety_net.watcher,
                )
            return result
        finally:
            # Klartext-Credentials sollen die Funktion nicht überleben.
            del secret
            del key

    def _take_pre_apply_backup(
        self,
        *,
        client: HttpClient,
        target: HttpTarget,
        key: str,
        secret: str,
        device: Device,
        plan_id: str,
    ) -> tuple[str | None, BackupRecord | None]:
        """Zieht ein Backup vor dem WRITE-Phase.

        Liefert ``(None, record)`` bei Erfolg, sonst ``(reason, None)``.
        Der Record wird vom Safety-Net-Pfad als Restore-Anker
        weiterverwendet.

        Reihenfolge:
        1. ``download_backup`` (XML-Bytes von der OPNsense).
        2. ``append_backup`` (gzip-Persistenz + Index-Update).
        3. Audit-Eintrag PRE_APPLY_BACKUP (success oder failed).
        4. ``prune_backups`` als Best-Effort - bei Pruning-Fehlern wird
           der Apply NICHT blockiert; lieber ein paar Backups zuviel als
           ein verhinderter Rollout.
        """
        try:
            content = download_backup(client, target, key, secret)
        except OpnCockpitError as exc:
            reason = exc.context.summary or exc.context.error_kind or "unbekannt"
            self.audit.append(
                AuditEventKind.PRE_APPLY_BACKUP,
                action="pre_apply_backup",
                target_device_id=device.id,
                target_device_name=device.name,
                error_kind=exc.context.error_kind,
                summary=(
                    f"Pre-Apply-Backup FEHLGESCHLAGEN bei '{device.name}' "
                    f"(Plan {plan_id}): {reason}"
                ),
            )
            return reason, None

        try:
            record: BackupRecord = append_backup(
                device.id,
                content,
                trigger="pre-apply",
                related_plan_id=plan_id,
                device_name_at_creation=device.name,
                storage_root=self.backup_storage_root,
            )
        except BackupStoreError as exc:
            reason = str(exc)
            self.audit.append(
                AuditEventKind.PRE_APPLY_BACKUP,
                action="pre_apply_backup",
                target_device_id=device.id,
                target_device_name=device.name,
                error_kind="store_error",
                summary=(
                    f"Pre-Apply-Backup KONNTE NICHT GESPEICHERT werden bei "
                    f"'{device.name}' (Plan {plan_id}): {reason}"
                ),
            )
            return reason, None

        self.audit.append(
            AuditEventKind.PRE_APPLY_BACKUP,
            action="pre_apply_backup",
            target_device_id=device.id,
            target_device_name=device.name,
            summary=(
                f"Pre-Apply-Backup ok fuer '{device.name}' "
                f"(Plan {plan_id}, {record.size_bytes} Bytes -> "
                f"{record.size_compressed} Bytes gzip)."
            ),
        )

        # Pruning: Best-Effort. Fehler hier sollen den Apply nicht stoppen,
        # nur ins Log fuer Operations-Visibility.
        settings = self.session.opened.data.settings
        with contextlib.suppress(BackupStoreError):
            try:
                prune_backups(
                    device.id,
                    retention_pre_apply=settings.backup_retention_pre_apply,
                    retention_scheduled=settings.backup_retention_scheduled,
                    storage_root=self.backup_storage_root,
                )
            except Exception:
                _log.exception(
                    "Backup-Pruning fuer device_id=%s schlug fehl - Apply laeuft trotzdem.",
                    device.id,
                )
        return None, record

    def _write_activate_verify(
        self,
        *,
        device: Device,
        adapter: ObjectAdapter[Any, Any],
        controller: SubsystemController,
        client: HttpClient,
        ctx: RequestContext,
        to_write: list[PlannedDeviceAction],
        action_kind: ActionKind,
        plan_id: str,
        start: float,
    ) -> Result:
        add_outcomes: list[AddOutcome] = []
        # ----- WRITE -----
        # action_kind entscheidet welche Adapter-Method gerufen wird:
        # ADD -> add, UPDATE -> update, DELETE -> delete. Verify-Erwartung
        # invertiert sich bei DELETE (Eintrag MUSS nach Reconfigure weg sein).
        try:
            for action in to_write:
                if action_kind is ActionKind.DELETE:
                    add_outcome = adapter.delete(
                        client, ctx, adapter.identity(action.target_spec),
                    )
                elif action_kind is ActionKind.UPDATE:
                    add_outcome = adapter.update(client, ctx, action.target_spec)
                else:
                    add_outcome = adapter.add(client, ctx, action.target_spec)
                add_outcomes.append(add_outcome)
        except OpnCockpitError as exc:
            return _make_failed(
                device=device,
                subsystem=adapter.subsystem,
                phase=Phase.WRITE,
                error_kind=exc.context.error_kind or "write",
                summary=exc.context.summary or "Schreibvorgang fehlgeschlagen.",
                start=start,
            )

        # ----- ACTIVATE -----
        try:
            controller.reconfigure(client, ctx)
        except OpnCockpitError as exc:
            if not isinstance(exc, ReconfigureError):
                exc = ReconfigureError(
                    "reconfigure fehlgeschlagen.",
                    context=make_context(
                        error_kind=exc.context.error_kind or "reconfigure",
                        summary=exc.context.summary,
                        status_code=exc.context.status_code,
                    ),
                )
            return Result(
                device_id=device.id,
                subsystem=adapter.subsystem,
                status=Status.WRITTEN,
                short_message=exc.context.summary or "reconfigure fehlgeschlagen.",
                error_kind=exc.context.error_kind,
                failed_phase=Phase.ACTIVATE,
                duration_ms=_elapsed_ms(start),
                add_outcome=add_outcomes[0] if add_outcomes else None,
            )

        # ----- VERIFY -----
        # Bei DELETE wird "found=False" zum Erfolgs-Signal: der Eintrag soll
        # nach reconfigure weg sein. Bei ADD/UPDATE bleibt "found=True"
        # das Erfolgs-Signal.
        expected_found = action_kind is not ActionKind.DELETE
        last_verify: VerifyOutcome | None = None
        for action in to_write:
            try:
                verify_outcome = adapter.verify(
                    client, ctx, adapter.identity(action.target_spec)
                )
            except OpnCockpitError as exc:
                return Result(
                    device_id=device.id,
                    subsystem=adapter.subsystem,
                    status=Status.ACTIVATED,
                    short_message=exc.context.summary or "Verify-Aufruf scheiterte.",
                    error_kind=exc.context.error_kind or "verify",
                    failed_phase=Phase.VERIFY,
                    duration_ms=_elapsed_ms(start),
                    add_outcome=add_outcomes[0] if add_outcomes else None,
                )
            if verify_outcome.found is not expected_found:
                if expected_found:
                    err_msg = (
                        f"Read-back leer für {adapter.identity(action.target_spec)}."
                    )
                else:
                    err_msg = (
                        f"Eintrag '{adapter.identity(action.target_spec)}' "
                        "ist nach Loeschung noch sichtbar."
                    )
                err = VerificationError(err_msg)
                return Result(
                    device_id=device.id,
                    subsystem=adapter.subsystem,
                    status=Status.ACTIVATED,
                    short_message=err.context.summary or str(err),
                    error_kind="verification",
                    failed_phase=Phase.VERIFY,
                    duration_ms=_elapsed_ms(start),
                    add_outcome=add_outcomes[0] if add_outcomes else None,
                )
            last_verify = verify_outcome

        # ----- POST-APPLY-BACKUP -----
        # Nach erfolgreichem Apply einen neuen Drift-Baseline-Snapshot ziehen,
        # damit die Drift-Erkennung nicht den Pre-Apply-Backup als Baseline
        # nutzt (was zu False-Positive-Drift fuehrte). Symmetrisch zum
        # Pre-Apply-Backup: nur wenn auto_backup_before_apply an ist, holen
        # wir auch das Post-Apply-Backup. Fehler hier blockieren den Apply
        # NICHT - der Rollout ist bereits erfolgreich. Nur Audit, keine
        # Status-Aenderung.
        post_apply_settings = self.session.opened.data.settings
        if post_apply_settings.auto_backup_before_apply:
            with contextlib.suppress(Exception):
                self._take_post_apply_backup(
                    client=client,
                    ctx=ctx,
                    device=device,
                    plan_id=plan_id,
                )

        return Result(
            device_id=device.id,
            subsystem=adapter.subsystem,
            status=Status.VERIFIED,
            short_message=f"{len(to_write)} Eintrag/Einträge ok.",
            duration_ms=_elapsed_ms(start),
            add_outcome=add_outcomes[0] if add_outcomes else None,
            verify_outcome=last_verify,
        )

    def _take_post_apply_backup(
        self,
        *,
        client: HttpClient,
        ctx: RequestContext,
        device: Device,
        plan_id: str,
    ) -> None:
        """Zieht nach erfolgreichem Apply ein neues Backup als Drift-Baseline.

        Defensiv: jede Exception wird hier still geschluckt - der Apply
        gilt bereits als VERIFIED, ein nachgelagertes Backup-Problem
        darf den Erfolgsstatus nicht entwerten. Wir loggen das fuer
        Operations-Visibility und schreiben einen Audit-Eintrag - das
        reicht. Bei wiederholtem Fehlschlag bekommt der User die Drift-
        Warnung; das ist akzeptabel und weniger irrefuehrend als ein
        falsches FAILED.
        """
        try:
            content = download_backup(client, ctx.target, ctx.key, ctx.secret)
        except OpnCockpitError as exc:
            reason = exc.context.summary or exc.context.error_kind or "unbekannt"
            self.audit.append(
                AuditEventKind.PRE_APPLY_BACKUP,
                action="post_apply_backup",
                target_device_id=device.id,
                target_device_name=device.name,
                error_kind=exc.context.error_kind,
                summary=(
                    f"Post-Apply-Backup FEHLGESCHLAGEN bei '{device.name}' "
                    f"(Plan {plan_id}): {reason}. Drift-Erkennung kann "
                    f"False-Positive zeigen bis manuell ein Backup gezogen wird."
                ),
            )
            return

        try:
            record: BackupRecord = append_backup(
                device.id,
                content,
                trigger="post-apply",
                related_plan_id=plan_id,
                device_name_at_creation=device.name,
                storage_root=self.backup_storage_root,
            )
        except BackupStoreError as exc:
            self.audit.append(
                AuditEventKind.PRE_APPLY_BACKUP,
                action="post_apply_backup",
                target_device_id=device.id,
                target_device_name=device.name,
                error_kind="store_error",
                summary=(
                    f"Post-Apply-Backup KONNTE NICHT GESPEICHERT werden "
                    f"bei '{device.name}' (Plan {plan_id}): {exc}"
                ),
            )
            return

        self.audit.append(
            AuditEventKind.PRE_APPLY_BACKUP,
            action="post_apply_backup",
            target_device_id=device.id,
            target_device_name=device.name,
            summary=(
                f"Post-Apply-Backup ok fuer '{device.name}' "
                f"(Plan {plan_id}, {record.size_bytes} Bytes -> "
                f"{record.size_compressed} Bytes gzip). Neue Drift-Baseline."
            ),
        )

        settings = self.session.opened.data.settings
        with contextlib.suppress(BackupStoreError):
            prune_backups(
                device.id,
                retention_pre_apply=settings.backup_retention_pre_apply,
                retention_scheduled=settings.backup_retention_scheduled,
                storage_root=self.backup_storage_root,
            )

    # ----- Safety-Net (Dead-Man's-Switch) -----

    def _lookup_vault_device(self, device_id: str) -> VaultDevice | None:
        """Sucht das ``VaultDevice`` (mit SSH-Credentials) zum Device-ID.

        Liefert None wenn Session schon gesperrt ist oder die ID nicht
        passt. Aufrufer sollen None graceful behandeln.
        """
        try:
            for d in self.session.opened.data.devices:
                if d.id == device_id:
                    return d
        except Exception:  # noqa: BLE001
            return None
        return None

    @staticmethod
    def _device_has_ssh(vault_device: VaultDevice | None) -> bool:
        if vault_device is None:
            return False
        if not vault_device.ssh_enabled:
            return False
        if not vault_device.ssh_private_key_pem.strip():
            return False
        if not vault_device.ssh_user.strip():
            return False
        return True

    def _arm_safety_net(
        self,
        *,
        device: Device,
        vault_device: VaultDevice,
        pre_apply_record: BackupRecord,
        plan_id: str,
        window_s: int,
    ) -> str:
        """Pushd Pre-Apply-XML auf die Box, startet daemon-Timer.

        Liefert leeren String bei Erfolg, sonst eine Fehlermeldung. Bei
        Fehler hat der Aufrufer keinen armed Zustand zu disarmen -
        keine Aenderung blieb auf der Box.
        """
        try:
            xml_bytes = read_backup_content(
                device.id,
                pre_apply_record.id,
                storage_root=self.backup_storage_root,
            )
        except Exception as exc:  # noqa: BLE001
            return f"Pre-Apply-XML nicht lesbar: {exc}"

        jobid = ssh_safety_net.make_jobid(plan_id, device.id)
        arm_res = ssh_safety_net.arm(
            vault_device,
            jobid=jobid,
            pre_apply_xml=xml_bytes,
            window_s=window_s,
        )
        if not arm_res.success:
            self.audit.append(
                AuditEventKind.PRE_APPLY_BACKUP,
                action="safety_net_arm_failed",
                target_device_id=device.id,
                target_device_name=device.name,
                error_kind="safety_net_arm_failed",
                summary=(
                    f"Safety-Net konnte auf '{device.name}' nicht aktiviert "
                    f"werden (Plan {plan_id}): {arm_res.summary}"
                ),
            )
            return arm_res.summary
        self.audit.append(
            AuditEventKind.PRE_APPLY_BACKUP,
            action="safety_net_armed",
            target_device_id=device.id,
            target_device_name=device.name,
            summary=(
                f"Safety-Net armed auf '{device.name}' (Plan {plan_id}, "
                f"jobid={jobid}, window={window_s}s, pid={arm_res.pid})."
            ),
        )
        return ""

    def _disarm_safety_net(
        self,
        *,
        result: Result,
        device: Device,
        vault_device: VaultDevice,
        jobid: str,
        plan_id: str,
        pre_apply_record: BackupRecord,
        window_s: int,
        watcher: Any,
    ) -> Result:
        """3 Sofort-Retries fuer den Disarm; bei Failure an Watcher uebergeben.

        Mutiert ``result`` (via ``dataclasses.replace``) um den
        ``safety_net_status`` zu setzen.
        """
        import dataclasses as _dc  # noqa: PLC0415

        last_summary = ""
        for attempt in range(3):
            disarm_res = ssh_safety_net.disarm(vault_device, jobid=jobid)
            if disarm_res.success:
                self.audit.append(
                    AuditEventKind.PRE_APPLY_BACKUP,
                    action="safety_net_disarmed",
                    target_device_id=device.id,
                    target_device_name=device.name,
                    summary=(
                        f"Safety-Net disarmed auf '{device.name}' "
                        f"(Plan {plan_id}, jobid={jobid}, "
                        f"Versuch {attempt + 1}/3)."
                    ),
                )
                return _dc.replace(result, safety_net_status="disarmed")
            last_summary = disarm_res.summary
            if attempt < 2:
                time.sleep(2.0)

        # 3 Versuche gescheitert: dem Watcher uebergeben falls vorhanden.
        if watcher is not None:
            try:
                watcher.enqueue_pending_disarm(
                    plan_id=plan_id,
                    device_id=device.id,
                    device_name=device.name,
                    jobid=jobid,
                    pre_apply_backup_id=pre_apply_record.id,
                    window_s=window_s,
                )
            except Exception:  # noqa: BLE001
                _log.exception(
                    "SafetyNetWatcher konnte pending-disarm fuer device_id=%s nicht annehmen.",
                    device.id,
                )
        self.audit.append(
            AuditEventKind.PRE_APPLY_BACKUP,
            action="safety_net_disarm_pending",
            target_device_id=device.id,
            target_device_name=device.name,
            error_kind="safety_net_disarm_pending",
            summary=(
                f"Safety-Net Disarm auf '{device.name}' (Plan {plan_id}, "
                f"jobid={jobid}) nach 3 Versuchen fehlgeschlagen - "
                f"Watcher uebernimmt: {last_summary}"
            ),
        )
        return _dc.replace(result, safety_net_status="disarm_pending")

    def _best_effort_disarm(
        self,
        *,
        device: Device,
        vault_device: VaultDevice,
        jobid: str,
        plan_id: str,
    ) -> None:
        """Einmaliger Disarm-Versuch waehrend Exception-Cleanup."""
        try:
            disarm_res = ssh_safety_net.disarm(vault_device, jobid=jobid)
        except Exception:  # noqa: BLE001
            _log.exception(
                "Best-Effort-Disarm crashed fuer device_id=%s",
                device.id,
            )
            return
        action = "safety_net_disarmed" if disarm_res.success else "safety_net_disarm_failed"
        self.audit.append(
            AuditEventKind.PRE_APPLY_BACKUP,
            action=action,
            target_device_id=device.id,
            target_device_name=device.name,
            error_kind=None if disarm_res.success else "safety_net_disarm_failed",
            summary=(
                f"Best-Effort-Disarm waehrend Exception-Cleanup auf '{device.name}' "
                f"(Plan {plan_id}, jobid={jobid}): {disarm_res.summary}"
            ),
        )

    # ----- Audit-Eintrag pro Geräte-Ergebnis -----

    def _audit_device_result(self, plan: Plan, device: Device, result: Result) -> None:
        # Audit #13: TLS-Verify-Status sichtbar machen, damit Audit-Reviewer
        # sehen, gegen welche Geraete mit deaktivierter Zertifikats-Pruefung
        # gerollt wurde.
        tls_marker = "" if device.tls_verify else " [TLS-AUS]"
        self.audit.append(
            AuditEventKind.DEVICE_RESULT,
            action=plan.action,
            target_device_id=device.id,
            target_device_name=device.name,
            status=str(result.status),
            error_kind=result.error_kind,
            failed_phase=str(result.failed_phase) if result.failed_phase else None,
            duration_ms=result.duration_ms,
            parameters=mask_dict({
                "device_host": device.host,
                "tls_verify": device.tls_verify,
            }),
            summary=(
                f"{device.name}{tls_marker}: {result.status} — {result.short_message}"
            ),
        )


# ---------------------------------------------------------------------------
# Hilfen
# ---------------------------------------------------------------------------


def _elapsed_ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)


def _make_failed(
    *,
    device: Device,
    subsystem: str,
    phase: Phase,
    error_kind: str,
    summary: str,
    start: float,
) -> Result:
    return Result(
        device_id=device.id,
        subsystem=subsystem,
        status=Status.FAILED,
        short_message=summary,
        error_kind=error_kind,
        failed_phase=phase,
        duration_ms=_elapsed_ms(start),
    )
