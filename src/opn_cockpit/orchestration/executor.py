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

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any

from opn_cockpit.audit.backend import AuditBackend
from opn_cockpit.audit.log import AuditEventKind
from opn_cockpit.core.errors import (
    OpnCockpitError,
    ReconfigureError,
    VerificationError,
    make_context,
)
from opn_cockpit.core.http_client import HttpClient, HttpTarget
from opn_cockpit.core.objects.base import (
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
class Executor:
    """Führt einen :class:`Plan` aus.

    Bekommt Session (für Credentials), Audit-Log und HttpClient bei der
    Anwendung — keine versteckten Mitglieder, die Tests umgehen müssen.
    """

    session: Session
    audit: AuditBackend
    max_workers: int = 8

    def apply(
        self,
        plan: Plan,
        *,
        adapter: ObjectAdapter[Any, Any],
        controller: SubsystemController,
        client: HttpClient,
    ) -> RolloutReport:
        """Rollt ``plan`` aus und liefert einen aggregierten Report."""
        pipelines = group_by_device(plan)
        self.audit.append(
            AuditEventKind.APPLY_STARTED,
            action=plan.action,
            target_count=len(pipelines),
            summary=f"Plan {plan.plan_id} wird auf {len(pipelines)} Gerät(e) ausgerollt.",
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
                start=start,
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
        start: float,
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
            return self._write_activate_verify(
                device=device,
                adapter=adapter,
                controller=controller,
                client=client,
                ctx=ctx,
                to_write=to_write,
                start=start,
            )
        finally:
            # Klartext-Credentials sollen die Funktion nicht überleben.
            del secret
            del key

    def _write_activate_verify(
        self,
        *,
        device: Device,
        adapter: ObjectAdapter[Any, Any],
        controller: SubsystemController,
        client: HttpClient,
        ctx: RequestContext,
        to_write: list[PlannedDeviceAction],
        start: float,
    ) -> Result:
        add_outcomes: list[AddOutcome] = []
        # ----- WRITE -----
        try:
            for action in to_write:
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
            if not verify_outcome.found:
                err = VerificationError(
                    f"Read-back leer für {adapter.identity(action.target_spec)}."
                )
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

        return Result(
            device_id=device.id,
            subsystem=adapter.subsystem,
            status=Status.VERIFIED,
            short_message=f"{len(to_write)} Eintrag/Einträge ok.",
            duration_ms=_elapsed_ms(start),
            add_outcome=add_outcomes[0] if add_outcomes else None,
            verify_outcome=last_verify,
        )

    # ----- Audit-Eintrag pro Geräte-Ergebnis -----

    def _audit_device_result(self, plan: Plan, device: Device, result: Result) -> None:
        self.audit.append(
            AuditEventKind.DEVICE_RESULT,
            action=plan.action,
            target_device_id=device.id,
            target_device_name=device.name,
            status=str(result.status),
            error_kind=result.error_kind,
            failed_phase=str(result.failed_phase) if result.failed_phase else None,
            duration_ms=result.duration_ms,
            parameters=mask_dict({"device_host": device.host}),
            summary=f"{device.name}: {result.status} — {result.short_message}",
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
