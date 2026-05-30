"""SQLite-Implementierung des AuditBackend (v3.1).

Schreibt jede AuditEvent-Zeile in eine ``audit``-Tabelle. Filter laufen
mit echtem SQL-WHERE statt In-Memory — bei einigen tausend Eintraegen
spuerbar schneller als der File-Reader.

Schema:

    CREATE TABLE audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp_utc TEXT NOT NULL,
        actor TEXT NOT NULL,
        event TEXT NOT NULL,
        summary TEXT NOT NULL,
        action TEXT,
        target_device_id TEXT,
        target_device_name TEXT,
        target_count INTEGER,
        parameters_json TEXT,
        status TEXT,
        error_kind TEXT,
        failed_phase TEXT,
        duration_ms INTEGER,
        vault_path TEXT
    );

Indexe auf ``timestamp_utc``, ``event``, ``target_device_id``.

Whitelist + Masking laeuft wie bei ``AuditLog`` — die zentrale Logik
sitzt in einer geteilten Helper-Funktion in ``audit.log``.
"""

from __future__ import annotations

import getpass
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, fields
from datetime import UTC, datetime
from typing import Any

from opn_cockpit.audit.log import (
    SUMMARY_MAX_LEN,
    AuditEventKind,
    AuditFieldError,
    AuditRecord,
)
from opn_cockpit.security.masking import mask_dict
from opn_cockpit.storage.sqlite_db import SqliteDb

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp_utc TEXT NOT NULL,
    actor TEXT NOT NULL,
    event TEXT NOT NULL,
    summary TEXT NOT NULL,
    action TEXT,
    target_device_id TEXT,
    target_device_name TEXT,
    target_count INTEGER,
    parameters_json TEXT,
    status TEXT,
    error_kind TEXT,
    failed_phase TEXT,
    duration_ms INTEGER,
    vault_path TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit(timestamp_utc);
CREATE INDEX IF NOT EXISTS idx_audit_event ON audit(event);
CREATE INDEX IF NOT EXISTS idx_audit_device ON audit(target_device_id);
"""

_APPEND_WHITELIST: frozenset[str] = frozenset(
    f.name
    for f in fields(AuditRecord)
    if f.name not in {"timestamp_utc", "actor", "event"}
)


def _now_utc_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _default_actor() -> str:
    try:
        return getpass.getuser() or "unknown"
    except OSError:
        return "unknown"


@dataclass(slots=True)
class SqliteAuditBackend:
    """SQLite-basiertes Audit-Backend. API-kompatibel zu ``AuditLog``."""

    db: SqliteDb
    actor: str = field(default_factory=_default_actor)
    clock: Callable[[], str] = field(default=_now_utc_iso)

    def __post_init__(self) -> None:
        self.db.executescript(_SCHEMA)

    # ----- Schreiben -----

    def append(self, event: AuditEventKind, /, **fields_in: Any) -> AuditRecord:
        unknown = set(fields_in.keys()) - _APPEND_WHITELIST
        if unknown:
            raise AuditFieldError(
                "Unzulaessige Audit-Felder: " + ", ".join(sorted(unknown)),
            )
        summary = str(fields_in.pop("summary", ""))
        if len(summary) > SUMMARY_MAX_LEN:
            summary = summary[: SUMMARY_MAX_LEN - 1] + "…"
        parameters = fields_in.pop("parameters", None)
        if parameters is not None:
            if not isinstance(parameters, dict):
                raise AuditFieldError("Feld 'parameters' muss ein dict oder None sein.")
            parameters = mask_dict(parameters)
        record = AuditRecord(
            timestamp_utc=self.clock(),
            actor=self.actor,
            event=event,
            summary=summary,
            parameters=parameters,
            **fields_in,
        )
        params_json = (
            json.dumps(record.parameters, ensure_ascii=False)
            if record.parameters is not None
            else None
        )
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO audit (timestamp_utc, actor, event, summary, action, "
                "target_device_id, target_device_name, target_count, parameters_json, "
                "status, error_kind, failed_phase, duration_ms, vault_path) VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.timestamp_utc,
                    record.actor,
                    str(record.event),
                    record.summary,
                    record.action,
                    record.target_device_id,
                    record.target_device_name,
                    record.target_count,
                    params_json,
                    record.status,
                    record.error_kind,
                    record.failed_phase,
                    record.duration_ms,
                    record.vault_path,
                ),
            )
        return record

    # ----- Lesen -----

    def read_all(self) -> list[AuditRecord]:
        with self.db.cursor() as cur:
            rows = cur.execute(
                "SELECT * FROM audit ORDER BY timestamp_utc ASC, id ASC",
            ).fetchall()
        return [_row_to_record(r) for r in rows]

    def filter(
        self,
        *,
        event: AuditEventKind | None = None,
        action: str | None = None,
        target_device_id: str | None = None,
        actor: str | None = None,
        since_iso: str | None = None,
        until_iso: str | None = None,
    ) -> list[AuditRecord]:
        clauses: list[str] = []
        values: list[Any] = []
        if event is not None:
            clauses.append("event = ?")
            values.append(str(event))
        if action is not None:
            clauses.append("action = ?")
            values.append(action)
        if target_device_id is not None:
            clauses.append("target_device_id = ?")
            values.append(target_device_id)
        if actor is not None:
            clauses.append("actor = ?")
            values.append(actor)
        if since_iso is not None:
            clauses.append("timestamp_utc >= ?")
            values.append(since_iso)
        if until_iso is not None:
            clauses.append("timestamp_utc <= ?")
            values.append(until_iso)
        sql = "SELECT * FROM audit"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY timestamp_utc ASC, id ASC"
        with self.db.cursor() as cur:
            rows = cur.execute(sql, values).fetchall()
        records: Iterable[AuditRecord] = (_row_to_record(r) for r in rows)
        return list(records)


def _row_to_record(row: Any) -> AuditRecord:
    params_json = row["parameters_json"]
    parameters = None
    if params_json:
        try:
            decoded = json.loads(params_json)
        except json.JSONDecodeError:
            decoded = None
        if isinstance(decoded, dict):
            parameters = decoded
    try:
        event = AuditEventKind(row["event"])
    except ValueError:
        # Defensiv: alte/unbekannte Events ueberspringen wir nicht, sondern
        # nehmen einen Default — sonst wird ein einzelner kaputter Eintrag
        # zum DoS-Vektor fuer die UI.
        event = AuditEventKind.DEVICE_RESULT
    return AuditRecord(
        timestamp_utc=str(row["timestamp_utc"]),
        actor=str(row["actor"]),
        event=event,
        summary=str(row["summary"]),
        action=row["action"],
        target_device_id=row["target_device_id"],
        target_device_name=row["target_device_name"],
        target_count=row["target_count"],
        parameters=parameters,
        status=row["status"],
        error_kind=row["error_kind"],
        failed_phase=row["failed_phase"],
        duration_ms=row["duration_ms"],
        vault_path=row["vault_path"],
    )


__all__ = ["SqliteAuditBackend"]
