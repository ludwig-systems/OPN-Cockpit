"""Persistente Plan-Datei zwischen ``plan`` und ``apply`` (CLI).

Pläne landen als JSON unter ``%APPDATA%/OPN-Cockpit/plans/<plan-id>.json``.
Das ist die einfachste Form, Plan/Apply in zwei CLI-Aufrufen sauber zu
trennen — und gleichzeitig der Mechanismus, mit dem du einen Plan
mehrfach prüfen kannst, bevor du ihn ausführst.

Auf Platte sind absichtlich nur:
* Plan-Metadaten (ID, Aktion, Subsystem, Zeitstempel)
* Pro Geräte-Aktion: Geräte-Stammdaten (kein API-Secret!), die typ-spezifische
  Spec als Dict (über den Adapter rekonstituierbar) und der **maskierte**
  Payload für die Vorschau.

Klartext-Credentials erreichen die Plan-Datei nie — die kommen erst beim
``apply`` aus dem entsperrten Tresor und werden über die ``Session`` pro
Aufruf bereitgestellt.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from opn_cockpit.core.objects.base import Diff, DiffKind
from opn_cockpit.core.result import (
    Phase,
    Result,
    RolloutReport,
    Status,
)
from opn_cockpit.inventory.model import Device
from opn_cockpit.orchestration.planner import Plan, PlannedDeviceAction
from opn_cockpit.orchestration.registry import get_binding

PLAN_ID_PATTERN = re.compile(r"^pl-[0-9A-Fa-f]{4,16}$")


class PlanStoreError(ValueError):
    """Plan-Datei nicht lesbar/parsbar oder Plan-ID ungültig."""


@dataclass(frozen=True, slots=True)
class PlanStore:
    """Schlanker Datei-Store für Pläne.

    ``base_dir`` ist üblicherweise ``%APPDATA%/OPN-Cockpit/plans/``. Wird
    bei Bedarf angelegt.
    """

    base_dir: Path

    # ----- Schreiben -----

    def save(self, plan: Plan) -> Path:
        self.base_dir.mkdir(parents=True, exist_ok=True)
        path = self.base_dir / f"{plan.plan_id}.json"
        payload = json.dumps(_plan_to_dict(plan), ensure_ascii=False, indent=2)
        path.write_text(payload, encoding="utf-8")
        return path

    # ----- Lesen -----

    def load(self, plan_id_or_path: str) -> Plan:
        """Lädt einen Plan anhand der ID oder eines expliziten Pfads."""
        path = self._resolve_path(plan_id_or_path)
        if not path.exists():
            raise PlanStoreError(f"Plan-Datei nicht gefunden: {path}")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PlanStoreError(f"Plan-Datei nicht lesbar: {path} ({exc})") from exc
        if not isinstance(raw, dict):
            raise PlanStoreError(f"Plan-Datei hat kein Wurzel-Objekt: {path}")
        return _plan_from_dict(raw)

    def list_ids(self) -> list[str]:
        """Liefert alle aktuell gespeicherten Plan-IDs (alphabetisch sortiert)."""
        if not self.base_dir.exists():
            return []
        ids: list[str] = []
        for p in self.base_dir.glob("pl-*.json"):
            stem = p.stem
            if PLAN_ID_PATTERN.match(stem) and not stem.endswith(".report"):
                ids.append(stem)
        return sorted(ids)

    # ----- Loeschen -----

    def delete(self, plan_id: str) -> bool:
        """Loescht den Plan + den ggf. existierenden Apply-Report.

        Liefert ``True`` wenn der Plan existierte, ``False`` sonst.
        """
        if not PLAN_ID_PATTERN.match(plan_id):
            raise PlanStoreError(f"Ungueltige Plan-ID: {plan_id!r}")
        plan_path = self.base_dir / f"{plan_id}.json"
        report_path = self.base_dir / f"{plan_id}.report.json"
        existed = plan_path.exists()
        if existed:
            plan_path.unlink()
        if report_path.exists():
            report_path.unlink()
        return existed

    # ----- Apply-Reports -----

    def save_report(self, plan_id: str, report: RolloutReport) -> Path:
        """Persistiert das Apply-Resultat neben dem Plan.

        Dateiname: ``{plan_id}.report.json``. Ueberschreibt frueherere
        Reports - bei mehreren Apply-Versuchen (Retry) gilt der letzte.
        """
        if not PLAN_ID_PATTERN.match(plan_id):
            raise PlanStoreError(f"Ungueltige Plan-ID: {plan_id!r}")
        self.base_dir.mkdir(parents=True, exist_ok=True)
        path = self.base_dir / f"{plan_id}.report.json"
        path.write_text(
            json.dumps(_report_to_dict(report), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return path

    def load_report(self, plan_id: str) -> RolloutReport | None:
        """Laedt den letzten Apply-Report fuer einen Plan, falls vorhanden."""
        if not PLAN_ID_PATTERN.match(plan_id):
            raise PlanStoreError(f"Ungueltige Plan-ID: {plan_id!r}")
        path = self.base_dir / f"{plan_id}.report.json"
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PlanStoreError(f"Report-Datei nicht lesbar: {path} ({exc})") from exc
        return _report_from_dict(raw)

    def _resolve_path(self, plan_id_or_path: str) -> Path:
        candidate = Path(plan_id_or_path)
        if candidate.suffix == ".json" and (candidate.is_absolute() or candidate.exists()):
            return candidate
        if PLAN_ID_PATTERN.match(plan_id_or_path):
            return self.base_dir / f"{plan_id_or_path}.json"
        raise PlanStoreError(
            f"'{plan_id_or_path}' ist weder eine Plan-ID (pl-XXXX) noch ein .json-Pfad."
        )


# ---------------------------------------------------------------------------
# Serialisierung
# ---------------------------------------------------------------------------


def _plan_to_dict(plan: Plan) -> dict[str, Any]:
    binding = get_binding(plan.subsystem)
    return {
        "plan_id": plan.plan_id,
        "action": plan.action,
        "subsystem": plan.subsystem,
        "created_at_utc": plan.created_at_utc,
        "actions": [_action_to_dict(a, binding.adapter) for a in plan.actions],
    }


def _action_to_dict(action: PlannedDeviceAction, adapter: Any) -> dict[str, Any]:
    return {
        "device": _device_to_dict(action.device),
        "target_spec": adapter.spec_to_dict(action.target_spec),
        "current_state": (
            adapter.spec_to_dict(action.current_state)
            if action.current_state is not None
            else None
        ),
        "diff": {"kind": str(action.diff.kind), "summary": action.diff.summary},
        "payload_masked": action.payload_masked,
    }


def _device_to_dict(device: Device) -> dict[str, Any]:
    return {
        "id": device.id,
        "name": device.name,
        "host": device.host,
        "port": device.port,
        "tls_verify": device.tls_verify,
        "tags": list(device.tags),
        "descr": device.descr,
    }


def _plan_from_dict(raw: dict[str, Any]) -> Plan:
    subsystem = str(raw.get("subsystem", ""))
    if not subsystem:
        raise PlanStoreError("Plan-Datei ohne 'subsystem'.")
    try:
        binding = get_binding(subsystem)
    except KeyError as exc:
        raise PlanStoreError(f"Unbekanntes Subsystem im Plan: {subsystem!r}") from exc

    actions_raw = raw.get("actions", [])
    if not isinstance(actions_raw, list):
        raise PlanStoreError("'actions' ist keine Liste.")

    actions: list[PlannedDeviceAction] = []
    for a in actions_raw:
        if not isinstance(a, dict):
            continue
        actions.append(_action_from_dict(a, binding.adapter))

    return Plan(
        plan_id=str(raw.get("plan_id", "")),
        action=str(raw.get("action", "")),
        subsystem=subsystem,
        created_at_utc=str(raw.get("created_at_utc", "")),
        actions=tuple(actions),
    )


def _action_from_dict(raw: dict[str, Any], adapter: Any) -> PlannedDeviceAction:
    device = _device_from_dict(raw.get("device") or {})
    spec_raw = raw.get("target_spec") or {}
    if not isinstance(spec_raw, dict):
        spec_raw = {}
    spec = adapter.spec_from_dict(spec_raw)

    cs_raw = raw.get("current_state")
    current_state = adapter.spec_from_dict(cs_raw) if isinstance(cs_raw, dict) else None

    diff_raw = raw.get("diff") or {}
    try:
        kind = DiffKind(str(diff_raw.get("kind", "new")))
    except ValueError:
        kind = DiffKind.NEW
    diff = Diff(kind=kind, summary=str(diff_raw.get("summary", "")))

    payload = raw.get("payload_masked")
    payload_dict: dict[str, Any] = payload if isinstance(payload, dict) else {}

    return PlannedDeviceAction(
        device=device,
        target_spec=spec,
        current_state=current_state,
        diff=diff,
        payload_masked=payload_dict,
    )


# ---------------------------------------------------------------------------
# Report-Serialisierung
# ---------------------------------------------------------------------------


def _report_to_dict(report: RolloutReport) -> dict[str, Any]:
    return {
        "results": [
            {
                "device_id": r.device_id,
                "subsystem": r.subsystem,
                "status": str(r.status),
                "short_message": r.short_message,
                "error_kind": r.error_kind,
                "failed_phase": str(r.failed_phase) if r.failed_phase else None,
                "duration_ms": r.duration_ms,
            }
            for r in report.results
        ],
    }


def _report_from_dict(raw: dict[str, Any]) -> RolloutReport:
    if not isinstance(raw, dict):
        return RolloutReport(results=())
    results_raw = raw.get("results") or []
    if not isinstance(results_raw, list):
        return RolloutReport(results=())
    results: list[Result] = []
    for r in results_raw:
        if not isinstance(r, dict):
            continue
        try:
            status_val = Status(str(r.get("status", "")))
        except ValueError:
            continue
        failed_phase: Phase | None
        phase_raw = r.get("failed_phase")
        if isinstance(phase_raw, str) and phase_raw:
            try:
                failed_phase = Phase(phase_raw)
            except ValueError:
                failed_phase = None
        else:
            failed_phase = None
        results.append(
            Result(
                device_id=str(r.get("device_id", "")),
                subsystem=str(r.get("subsystem", "")),
                status=status_val,
                short_message=str(r.get("short_message", "")),
                error_kind=r.get("error_kind") if isinstance(r.get("error_kind"), str) else None,
                failed_phase=failed_phase,
                duration_ms=int(r.get("duration_ms", 0) or 0),
            )
        )
    return RolloutReport(results=tuple(results))


def _device_from_dict(raw: dict[str, Any]) -> Device:
    tags_raw = raw.get("tags", [])
    tags = tuple(str(t) for t in tags_raw) if isinstance(tags_raw, list) else ()
    return Device(
        id=str(raw.get("id", "")),
        name=str(raw.get("name", "")),
        host=str(raw.get("host", "")),
        port=int(raw.get("port", 443)),
        tls_verify=bool(raw.get("tls_verify", True)),
        tags=tags,
        descr=str(raw.get("descr", "")),
    )
