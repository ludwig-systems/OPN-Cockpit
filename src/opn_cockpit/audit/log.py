"""Append-only Audit-Log in JSON-Lines-Format.

Erfüllt Spec R-LOG-1..3:

* **R-LOG-1** — jeder Eintrag trägt Zeitstempel (UTC, ISO 8601 mit ``Z``),
  Akteur, Ereignistyp, Aktion, optionale Geräte-Zuordnung, Status,
  Fehlerart und eine kurze Zusammenfassung.
* **R-LOG-2** — append-only, JSON Lines, parsbar zeilenweise; defensiver
  Reader für die GUI-Audit-Ansicht.
* **R-LOG-3** — Whitelist + Maskierung verhindern Klartext-Secrets:
  ``append`` akzeptiert NUR die in :class:`AuditRecord` definierten Felder
  (kein offener ``extra``-Dict), und das einzige Dict-Feld
  (``parameters``) wird vor dem Schreiben durch ``security.masking.mask_dict``
  geschickt.

Speicher-Ort: standardmäßig ``%APPDATA%/OPN-Cockpit/audit.jsonl``.
Bewusst **außerhalb** der Tresor-Datei, damit das Log auch ohne Master-
Passwort lesbar bleibt (post-mortem, anderer Admin).
"""

from __future__ import annotations

import getpass
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, fields
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from opn_cockpit.config import get_app_data_dir
from opn_cockpit.security.masking import mask_dict

# ---------------------------------------------------------------------------
# Konstanten
# ---------------------------------------------------------------------------

AUDIT_FILENAME = "audit.jsonl"
SUMMARY_MAX_LEN = 300


class AuditEventKind(StrEnum):
    """Ereignistyp eines Audit-Eintrags.

    Trennt **was passiert ist** (z. B. PLAN_GENERATED) von **welcher Aktion**
    (z. B. ``add_route``). ``action`` ist ein freier String, ``event`` ist
    diese Enum.
    """

    PLAN_GENERATED = "plan_generated"
    APPLY_STARTED = "apply_started"
    DEVICE_RESULT = "device_result"
    APPLY_COMPLETED = "apply_completed"
    VAULT_CREATED = "vault_created"
    VAULT_OPENED = "vault_opened"
    VAULT_LOCKED = "vault_locked"
    VAULT_PASSWORD_CHANGED = "vault_password_changed"
    TEMPLATE_EXPORTED = "template_exported"
    SESSION_AUTO_LOCKED = "session_auto_locked"
    LOGIN_FAILED = "login_failed"
    USER_CREATED = "user_created"
    USER_UPDATED = "user_updated"
    USER_DELETED = "user_deleted"
    BACKUP_DOWNLOADED = "backup_downloaded"
    API_KEY_REVEALED = "api_key_revealed"
    PRE_APPLY_BACKUP = "pre_apply_backup"
    SCHEDULED_BACKUP = "scheduled_backup"


# ---------------------------------------------------------------------------
# Datentyp
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AuditRecord:
    """Ein Audit-Log-Eintrag.

    Pflichtfelder ``timestamp_utc``, ``actor``, ``event``, ``summary``.
    Alle anderen sind optional und werden bei ``None`` als ``null`` in der
    JSON-Zeile mitgeschrieben — so bleiben Filter-Konsumenten einheitlich.
    """

    timestamp_utc: str
    actor: str
    event: AuditEventKind
    summary: str

    action: str | None = None
    target_device_id: str | None = None
    target_device_name: str | None = None
    target_count: int | None = None
    parameters: dict[str, Any] | None = None
    status: str | None = None
    error_kind: str | None = None
    failed_phase: str | None = None
    duration_ms: int | None = None
    vault_path: str | None = None

    # ----- Serialisierung -----

    def to_json_line(self) -> str:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
        ) + "\n"

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp_utc": self.timestamp_utc,
            "actor": self.actor,
            "event": str(self.event),
            "summary": self.summary,
            "action": self.action,
            "target_device_id": self.target_device_id,
            "target_device_name": self.target_device_name,
            "target_count": self.target_count,
            "parameters": self.parameters,
            "status": self.status,
            "error_kind": self.error_kind,
            "failed_phase": self.failed_phase,
            "duration_ms": self.duration_ms,
            "vault_path": self.vault_path,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> AuditRecord:
        """Defensiver Reader: unbekannte Felder werden ignoriert, fehlende
        Pflichtfelder mit sicheren Defaults aufgefüllt."""
        event_raw = raw.get("event", "")
        try:
            event = AuditEventKind(event_raw)
        except ValueError as exc:
            raise _AuditLineError(f"Unbekannter event-Wert: {event_raw!r}") from exc
        return cls(
            timestamp_utc=str(raw.get("timestamp_utc", "")),
            actor=str(raw.get("actor", "")),
            event=event,
            summary=str(raw.get("summary", "")),
            action=_optional_str(raw.get("action")),
            target_device_id=_optional_str(raw.get("target_device_id")),
            target_device_name=_optional_str(raw.get("target_device_name")),
            target_count=_optional_int(raw.get("target_count")),
            parameters=raw.get("parameters") if isinstance(raw.get("parameters"), dict) else None,
            status=_optional_str(raw.get("status")),
            error_kind=_optional_str(raw.get("error_kind")),
            failed_phase=_optional_str(raw.get("failed_phase")),
            duration_ms=_optional_int(raw.get("duration_ms")),
            vault_path=_optional_str(raw.get("vault_path")),
        )


# Felder, die in :meth:`AuditLog.append` als Keyword-Argumente erlaubt sind.
_APPEND_WHITELIST: frozenset[str] = frozenset(
    f.name
    for f in fields(AuditRecord)
    if f.name not in {"timestamp_utc", "actor", "event"}
)


# ---------------------------------------------------------------------------
# Fehler
# ---------------------------------------------------------------------------


class AuditFieldError(ValueError):
    """``append`` wurde mit einem nicht zugelassenen Feld aufgerufen.

    Schützt gegen ungewollte Erweiterung der Log-Felder durch versehentliche
    ``extra={...}``-Lecks (z. B. wenn jemand eine API-Antwort komplett
    ins Audit pumpen will).
    """


class _AuditLineError(ValueError):
    """Interne Markierung beschädigter Zeilen beim Lesen — nur defensiv."""


# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------


def _now_utc_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _default_actor() -> str:
    try:
        return getpass.getuser() or "unknown"
    except OSError:
        return "unknown"


@dataclass(slots=True)
class AuditLog:
    """Append-only Audit-Log-Writer + Reader.

    Mehrere ``AuditLog``-Instanzen auf derselben Datei sind erlaubt — jedes
    ``append`` öffnet den File-Handle für die einzelne Zeile und schließt
    ihn wieder (kleine Latenz pro Eintrag, dafür kein Lock-Stress in
    Multi-Worker-Settings wie dem Executor mit ThreadPool).
    """

    path: Path
    actor: str = field(default_factory=_default_actor)
    clock: Callable[[], str] = field(default=_now_utc_iso)

    # ----- Schreiben -----

    def append(self, event: AuditEventKind, /, **fields_in: Any) -> AuditRecord:
        """Schreibt einen Eintrag.

        Keyword-Argumente werden gegen die ``AuditRecord``-Felder geprüft.
        Unbekannte Felder werfen ``AuditFieldError``. Das einzige Dict-Feld
        ``parameters`` wird vor dem Persistieren rekursiv maskiert. Das
        ``summary``-Feld wird defensiv auf ``SUMMARY_MAX_LEN`` Zeichen
        gekürzt.

        ``actor`` kann pro-Aufruf ueberschrieben werden (Multi-User-Mode:
        eingeloggter Username statt OS-User). Ohne Override gilt der
        Default-Actor aus dem Konstruktor.
        """
        actor_override = fields_in.pop("actor", None)
        unknown = set(fields_in.keys()) - _APPEND_WHITELIST
        if unknown:
            raise AuditFieldError(
                "Unzulässige Audit-Felder: " + ", ".join(sorted(unknown))
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
            actor=str(actor_override) if actor_override else self.actor,
            event=event,
            summary=summary,
            parameters=parameters,
            **fields_in,
        )
        self._write_line(record.to_json_line())
        return record

    def _write_line(self, line: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # "a" + utf-8: append-only, line-orientiert, kein lock-Stress.
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line)

    # ----- Lesen -----

    def read_all(self) -> list[AuditRecord]:
        """Liefert alle Einträge in chronologischer Reihenfolge.

        Defekte/unvollständige Zeilen werden übersprungen (defensiv) — eine
        einzelne korrupte Zeile soll die UI-Anzeige nicht blockieren.
        """
        if not self.path.exists():
            return []
        records: list[AuditRecord] = []
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    raw = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if not isinstance(raw, dict):
                    continue
                try:
                    records.append(AuditRecord.from_dict(raw))
                except _AuditLineError:
                    continue
        return records

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
        """Filtert Einträge für die UI-Audit-Ansicht.

        Alle Filter sind AND-verknüpft, jeweils gegen einen Substring-/
        Exakt-Match. Zeit-Filter vergleichen lexikografisch — das ist bei
        ISO-8601-mit-Z-Format identisch zu chronologischem Vergleich.
        """
        records: Iterable[AuditRecord] = self.read_all()
        if event is not None:
            records = (r for r in records if r.event is event)
        if action is not None:
            records = (r for r in records if r.action == action)
        if target_device_id is not None:
            records = (r for r in records if r.target_device_id == target_device_id)
        if actor is not None:
            records = (r for r in records if r.actor == actor)
        if since_iso is not None:
            records = (r for r in records if r.timestamp_utc >= since_iso)
        if until_iso is not None:
            records = (r for r in records if r.timestamp_utc <= until_iso)
        return list(records)


# ---------------------------------------------------------------------------
# Standardpfad
# ---------------------------------------------------------------------------


def default_audit_path() -> Path:
    """Standard-Pfad: ``%APPDATA%/OPN-Cockpit/audit.jsonl`` (oder Fallback)."""
    return get_app_data_dir() / AUDIT_FILENAME


# ---------------------------------------------------------------------------
# Reader-Helfer
# ---------------------------------------------------------------------------


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value or None
    return str(value)


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


