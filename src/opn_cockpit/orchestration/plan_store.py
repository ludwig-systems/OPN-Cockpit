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
            if PLAN_ID_PATTERN.match(stem):
                ids.append(stem)
        return sorted(ids)

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
