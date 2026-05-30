"""SQLite-Implementierung des PlanStoreBackend (v3.1).

Plaene und Reports liegen als JSON-Blob in einer einzigen Datei. Vorteil
gegenueber der File-Variante: atomare Multi-Tabellen-Updates, Backups
sind eine einzelne ``.db``, und Plan-Listing ist ein ``SELECT id`` ohne
Glob-Scan.

Reaper- und Garbage-Collection-Strategien (z. B. "Plaene >30 Tage alt
automatisch loeschen") sind heute noch nicht implementiert — der
PlanStore ist API-kompatibel mit der File-Variante und nichts mehr.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from opn_cockpit.core.result import RolloutReport
from opn_cockpit.orchestration.plan_store import (
    PLAN_ID_PATTERN,
    PlanStoreError,
    _plan_from_dict,
    _plan_to_dict,
    _report_from_dict,
    _report_to_dict,
)
from opn_cockpit.orchestration.planner import Plan
from opn_cockpit.storage.sqlite_db import SqliteDb

_SCHEMA = """
CREATE TABLE IF NOT EXISTS plans (
    plan_id TEXT PRIMARY KEY,
    created_at_utc TEXT NOT NULL,
    action TEXT NOT NULL,
    subsystem TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    report_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_plans_created ON plans(created_at_utc);
"""


@dataclass(slots=True)
class SqlitePlanStore:
    """SQLite-Plan-Store. API-kompatibel zu ``PlanStore``."""

    db: SqliteDb

    def __post_init__(self) -> None:
        self.db.executescript(_SCHEMA)

    # ----- Schreiben -----

    def save(self, plan: Plan) -> str:
        payload = json.dumps(_plan_to_dict(plan), ensure_ascii=False)
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO plans "
                "(plan_id, created_at_utc, action, subsystem, plan_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    plan.plan_id,
                    plan.created_at_utc,
                    plan.action,
                    plan.subsystem,
                    payload,
                ),
            )
        return plan.plan_id

    # ----- Lesen -----

    def load(self, plan_id_or_path: str) -> Plan:
        # SQLite kennt keine Pfade — wir akzeptieren nur eine ID, nicht
        # einen .json-Pfad wie der File-Store.
        if not PLAN_ID_PATTERN.match(plan_id_or_path):
            raise PlanStoreError(
                f"'{plan_id_or_path}' ist keine gueltige Plan-ID (pl-XXXX).",
            )
        with self.db.cursor() as cur:
            row = cur.execute(
                "SELECT plan_json FROM plans WHERE plan_id = ?",
                (plan_id_or_path,),
            ).fetchone()
        if row is None:
            raise PlanStoreError(f"Plan-Datei nicht gefunden: {plan_id_or_path}")
        try:
            raw = json.loads(row["plan_json"])
        except json.JSONDecodeError as exc:
            raise PlanStoreError(
                f"Plan-Datei nicht lesbar: {plan_id_or_path} ({exc})",
            ) from exc
        if not isinstance(raw, dict):
            raise PlanStoreError(
                f"Plan-Datei hat kein Wurzel-Objekt: {plan_id_or_path}",
            )
        return _plan_from_dict(raw)

    def list_ids(self) -> list[str]:
        with self.db.cursor() as cur:
            rows = cur.execute(
                "SELECT plan_id FROM plans ORDER BY plan_id ASC",
            ).fetchall()
        return [str(r["plan_id"]) for r in rows]

    # ----- Loeschen -----

    def delete(self, plan_id: str) -> bool:
        if not PLAN_ID_PATTERN.match(plan_id):
            raise PlanStoreError(f"Ungueltige Plan-ID: {plan_id!r}")
        with self.db.transaction() as conn:
            cur = conn.execute(
                "DELETE FROM plans WHERE plan_id = ?", (plan_id,),
            )
        return cur.rowcount > 0

    # ----- Apply-Reports -----

    def save_report(self, plan_id: str, report: RolloutReport) -> str:
        if not PLAN_ID_PATTERN.match(plan_id):
            raise PlanStoreError(f"Ungueltige Plan-ID: {plan_id!r}")
        payload = json.dumps(_report_to_dict(report), ensure_ascii=False)
        with self.db.transaction() as conn:
            cur = conn.execute(
                "UPDATE plans SET report_json = ? WHERE plan_id = ?",
                (payload, plan_id),
            )
        if cur.rowcount == 0:
            raise PlanStoreError(f"Plan '{plan_id}' nicht gefunden — kein Report-Save.")
        return plan_id

    def load_report(self, plan_id: str) -> RolloutReport | None:
        if not PLAN_ID_PATTERN.match(plan_id):
            raise PlanStoreError(f"Ungueltige Plan-ID: {plan_id!r}")
        with self.db.cursor() as cur:
            row = cur.execute(
                "SELECT report_json FROM plans WHERE plan_id = ?", (plan_id,),
            ).fetchone()
        if row is None or not row["report_json"]:
            return None
        try:
            raw = json.loads(row["report_json"])
        except json.JSONDecodeError as exc:
            raise PlanStoreError(
                f"Report-Datei nicht lesbar: {plan_id} ({exc})",
            ) from exc
        return _report_from_dict(raw)


__all__ = ["SqlitePlanStore"]
