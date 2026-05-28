"""Plan-Erzeugung mit Pre-Check, Diff und maskierten Payloads.

Der Planner ist die "Plan"-Hälfte des Plan/Apply-Musters (Terraform-Stil).
Er bekommt eine Aktion + eine Spec + eine Geräte-Liste und produziert ein
:class:`Plan`-Objekt, das die GUI/CLI als Vorschau zeigt und der
:class:`Executor` später ausrollt.

Wichtig:
* Pre-Check (``exists``) läuft **parallel** über alle Geräte (ThreadPool).
* Maskierung passiert **hier** — der Payload, der im Plan landet, ist
  bereits durch ``security.masking.mask_dict`` gegangen.
* Plan-IDs sind kurz und random (``pl-XXXXXXXX``) — kollisionsfest genug
  für die handvoll parallel offener Pläne, die ein Admin hat.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from opn_cockpit.audit.log import AuditEventKind, AuditLog
from opn_cockpit.core.errors import OpnCockpitError
from opn_cockpit.core.http_client import HttpClient, HttpTarget
from opn_cockpit.core.objects.base import (
    Diff,
    DiffKind,
    ObjectAdapter,
    RequestContext,
)
from opn_cockpit.inventory.model import Device
from opn_cockpit.security.masking import mask_dict
from opn_cockpit.security.session import Session

# ---------------------------------------------------------------------------
# Plan-Datentypen
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlannedDeviceAction:
    """Was für ein konkretes Gerät getan werden soll.

    Ein Plan ist eine Sammlung davon. ``payload_masked`` ist die einzige
    Repräsentation des Soll-Zustands, die jemals in GUI/Log erscheint —
    Klartext-Payloads bleiben im Adapter.
    """

    device: Device
    target_spec: Any
    current_state: Any | None
    diff: Diff
    payload_masked: dict[str, Any]


@dataclass(frozen=True, slots=True)
class Plan:
    """Ergebnis einer Plan-Erzeugung.

    Enthält die Plan-ID (für ``apply <id>``), den Aktionsnamen, das
    Subsystem und die per-Gerät-Aktionen. ``created_at_utc`` dient nur
    der Anzeige und Audit-Korrelation.
    """

    plan_id: str
    action: str
    subsystem: str
    created_at_utc: str
    actions: tuple[PlannedDeviceAction, ...] = field(default_factory=tuple)

    @property
    def target_count(self) -> int:
        return len(self.actions)

    @property
    def to_apply_count(self) -> int:
        """Anzahl Geräte, die nicht SKIP sind (also tatsächlich geschrieben werden)."""
        return sum(1 for a in self.actions if a.diff.kind is not DiffKind.SKIP)

    @property
    def skip_count(self) -> int:
        return sum(1 for a in self.actions if a.diff.kind is DiffKind.SKIP)


# ---------------------------------------------------------------------------
# Plan-ID-Erzeugung
# ---------------------------------------------------------------------------


def generate_plan_id() -> str:
    """Liefert eine kurze, kollisionsfeste Plan-ID.

    Format: ``pl-XXXXXXXX`` (8 Hex-Zeichen, ~10^9 Möglichkeiten — reicht
    für die handvoll Pläne, die parallel offen sein können).
    """
    return f"pl-{secrets.token_hex(4).upper()}"


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------


class PlannerError(OpnCockpitError):
    """Plan-Erzeugung konnte nicht abgeschlossen werden."""

    default_kind = "planner"


@dataclass(slots=True)
class Planner:
    """Erzeugt :class:`Plan`-Objekte.

    Stateless im Sinne der Architektur: bekommt Client, Audit und Session
    bei jedem Aufruf — keine versteckten Member, die Tests umgehen müssten.
    """

    audit: AuditLog
    session: Session
    max_workers: int = 8

    def create_plan(
        self,
        *,
        action: str,
        spec: Any,
        devices: Iterable[Device],
        adapter: ObjectAdapter[Any, Any],
        client: HttpClient,
    ) -> Plan:
        """Erzeugt einen Plan für ``action`` über ``devices`` (eine Spec).

        Convenience-Wrapper um :meth:`create_bulk_plan` mit ``specs=[spec]``.
        """
        return self.create_bulk_plan(
            action=action, specs=[spec], devices=devices,
            adapter=adapter, client=client,
        )

    def create_bulk_plan(
        self,
        *,
        action: str,
        specs: list[Any],
        devices: Iterable[Device],
        adapter: ObjectAdapter[Any, Any],
        client: HttpClient,
    ) -> Plan:
        """Erzeugt einen Plan über N Specs x M Geräte.

        Pro Gerät werden parallel alle Specs durch den Pre-Check geschickt.
        Das Resultat ist ein Plan mit N x M Aktionen, gruppiert pro Gerät
        durch den Executor. Best-Effort: Pre-Check-Fehler pro Spec werden
        als NEW mit Hinweis im Diff durchgereicht.
        """
        devices_list = list(devices)
        plan_id = generate_plan_id()
        created_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

        all_planned: list[PlannedDeviceAction] = []
        for spec in specs:
            all_planned.extend(
                self._plan_per_device(devices_list, spec, adapter, client)
            )

        plan = Plan(
            plan_id=plan_id,
            action=action,
            subsystem=adapter.subsystem,
            created_at_utc=created_at,
            actions=tuple(all_planned),
        )

        masked_specs = [adapter.spec_to_dict(spec) for spec in specs]
        self.audit.append(
            AuditEventKind.PLAN_GENERATED,
            action=action,
            target_count=plan.target_count,
            parameters=mask_dict({"specs": masked_specs}),
            summary=(
                f"Plan {plan_id} für {action}: {plan.target_count} Aktionen "
                f"({len(specs)} Spec(s) x {len(devices_list)} Geräte), "
                f"{plan.to_apply_count} schreiben, {plan.skip_count} überspringen."
            ),
        )
        return plan

    # ----- Internals -----

    def _plan_per_device(
        self,
        devices: list[Device],
        spec: Any,
        adapter: ObjectAdapter[Any, Any],
        client: HttpClient,
    ) -> list[PlannedDeviceAction]:
        if not devices:
            return []
        workers = min(self.max_workers, len(devices))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(self._plan_one, device, spec, adapter, client)
                for device in devices
            ]
            return [future.result() for future in futures]

    def _plan_one(
        self,
        device: Device,
        spec: Any,
        adapter: ObjectAdapter[Any, Any],
        client: HttpClient,
    ) -> PlannedDeviceAction:
        target = HttpTarget(
            host=device.host,
            port=device.port,
            verify=device.tls_verify,
        )
        try:
            key, secret = self.session.credentials_for(device.id)
        except OpnCockpitError as exc:
            # Credentials nicht abrufbar (Session-Lock o. ä.) — Geräte-Aktion
            # als NEW mit Hinweis durchreichen; Executor wird das sauber
            # auflösen oder als Fehler melden.
            return _failed_plan_action(
                device, spec, adapter,
                summary=f"Pre-Check übersprungen: {exc.context.summary or exc.context.error_kind}",
            )
        ctx = RequestContext(target=target, key=key, secret=secret)
        try:
            current_state = adapter.exists(client, ctx, adapter.identity(spec))
        except OpnCockpitError as exc:
            reason = exc.context.summary or exc.context.error_kind
            return _failed_plan_action(
                device, spec, adapter,
                summary=f"Pre-Check fehlgeschlagen: {reason}",
            )
        diff = adapter.diff(current_state, spec)
        payload_masked = mask_dict(adapter.to_payload(spec))
        return PlannedDeviceAction(
            device=device,
            target_spec=spec,
            current_state=current_state,
            diff=diff,
            payload_masked=payload_masked,
        )


# ---------------------------------------------------------------------------
# Helfer
# ---------------------------------------------------------------------------


def _failed_plan_action(
    device: Device,
    spec: Any,
    adapter: ObjectAdapter[Any, Any],
    *,
    summary: str,
) -> PlannedDeviceAction:
    """Fallback-Action: behandelt das Gerät als "neu" und markiert das im Diff.

    Wird verwendet, wenn der Pre-Check fehlschlägt — z. B. weil das Gerät
    gerade nicht erreichbar ist. Wir verhindern damit nicht den Plan;
    der Executor versucht später und meldet den realen Fehler in der
    Result-Matrix.
    """
    return PlannedDeviceAction(
        device=device,
        target_spec=spec,
        current_state=None,
        diff=Diff(kind=DiffKind.NEW, summary=summary),
        payload_masked=mask_dict(adapter.to_payload(spec)),
    )
