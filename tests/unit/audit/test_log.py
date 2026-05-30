"""Tests für audit.log — Whitelist, Masking, JSON-Lines, Filter, defensive Reader."""

from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from opn_cockpit.audit.log import (
    AUDIT_FILENAME,
    SUMMARY_MAX_LEN,
    AuditEventKind,
    AuditFieldError,
    AuditLog,
    AuditRecord,
    default_audit_path,
)

# ---------------------------------------------------------------------------
# Hilfen
# ---------------------------------------------------------------------------


class FakeClock:
    def __init__(self, start: str = "2026-05-28T10:00:00.000Z") -> None:
        self._values = [start]

    def __call__(self) -> str:
        return self._values[-1]

    def set(self, ts: str) -> None:
        self._values.append(ts)


def _log(path: Path, actor: str = "alice", clock: FakeClock | None = None) -> AuditLog:
    return AuditLog(path=path, actor=actor, clock=clock or FakeClock())


# ---------------------------------------------------------------------------
# Append + Roundtrip
# ---------------------------------------------------------------------------


class TestAppendRoundtrip:
    def test_writes_a_single_json_line(self, tmp_path: Path) -> None:
        path = tmp_path / "audit.jsonl"
        log = _log(path)
        log.append(AuditEventKind.VAULT_OPENED, summary="Berlin-Tresor geöffnet")
        content = path.read_text(encoding="utf-8")
        assert content.endswith("\n")
        lines = content.strip().splitlines()
        assert len(lines) == 1
        parsed = json.loads(lines[0])
        assert parsed["event"] == "vault_opened"
        assert parsed["actor"] == "alice"
        assert parsed["summary"] == "Berlin-Tresor geöffnet"

    def test_multiple_appends_are_separate_lines(self, tmp_path: Path) -> None:
        path = tmp_path / "audit.jsonl"
        log = _log(path)
        log.append(AuditEventKind.VAULT_OPENED, summary="open")
        log.append(AuditEventKind.VAULT_LOCKED, summary="lock")
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2

    def test_creates_parent_directory(self, tmp_path: Path) -> None:
        path = tmp_path / "sub" / "dir" / "audit.jsonl"
        log = _log(path)
        log.append(AuditEventKind.VAULT_OPENED, summary="ok")
        assert path.exists()

    def test_record_returned_matches_what_was_written(self, tmp_path: Path) -> None:
        path = tmp_path / "audit.jsonl"
        log = _log(path)
        rec = log.append(
            AuditEventKind.DEVICE_RESULT,
            action="add_route",
            target_device_id="d1",
            target_device_name="Berlin",
            status="Verifiziert",
            summary="Route 10/24 ok",
            duration_ms=1234,
        )
        all_read = log.read_all()
        assert len(all_read) == 1
        assert all_read[0] == rec


# ---------------------------------------------------------------------------
# Whitelist
# ---------------------------------------------------------------------------


class TestWhitelist:
    def test_unknown_field_raises(self, tmp_path: Path) -> None:
        log = _log(tmp_path / "audit.jsonl")
        with pytest.raises(AuditFieldError) as exc:
            log.append(
                AuditEventKind.DEVICE_RESULT,
                summary="x",
                raw_response_body="LEAK",  # nicht erlaubt
            )
        assert "raw_response_body" in str(exc.value)

    def test_multiple_unknown_fields_reported(self, tmp_path: Path) -> None:
        log = _log(tmp_path / "audit.jsonl")
        with pytest.raises(AuditFieldError) as exc:
            log.append(
                AuditEventKind.DEVICE_RESULT,
                summary="x",
                http_response_body="L1",
                stack_trace="L2",
            )
        msg = str(exc.value)
        assert "http_response_body" in msg
        assert "stack_trace" in msg

    def test_timestamp_event_not_allowed_as_kwargs(self, tmp_path: Path) -> None:
        # timestamp_utc und event werden vom Logger selbst gesetzt — der
        # Aufrufer darf sie nicht ueberschreiben.
        log = _log(tmp_path / "audit.jsonl")
        with pytest.raises(AuditFieldError):
            log.append(
                AuditEventKind.VAULT_OPENED,
                summary="x",
                timestamp_utc="1970-01-01T00:00:00.000Z",
            )

    def test_actor_override_is_explicit_server_decision(
        self, tmp_path: Path,
    ) -> None:
        # v4-Pass 1: actor-Override ist eine erlaubte, explizite Server-
        # Funktion (Multi-User-Mode setzt eingeloggten Username statt
        # OS-User). Kein Spoofing-Vektor, weil der Aufrufer der
        # vertrauenswuerdige Endpoint ist.
        log = _log(tmp_path / "audit.jsonl")
        log.append(
            AuditEventKind.VAULT_OPENED,
            summary="x",
            actor="alice",
        )
        records = log.read_all()
        assert records[0].actor == "alice"


# ---------------------------------------------------------------------------
# Masking
# ---------------------------------------------------------------------------


class TestMasking:
    def test_parameters_dict_is_masked(self, tmp_path: Path) -> None:
        log = _log(tmp_path / "audit.jsonl")
        log.append(
            AuditEventKind.DEVICE_RESULT,
            action="add_route",
            target_device_id="d1",
            parameters={
                "network": "10.0.0.0/24",
                "api_key": "leaked-key",
                "api_secret": "leaked-secret",
                "gateway": "WAN_GW",
            },
            summary="ok",
        )
        on_disk = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
        assert "leaked-key" not in on_disk
        assert "leaked-secret" not in on_disk
        assert "10.0.0.0/24" in on_disk  # nicht sensitiv, bleibt sichtbar
        assert "WAN_GW" in on_disk

    def test_masking_works_recursively(self, tmp_path: Path) -> None:
        log = _log(tmp_path / "audit.jsonl")
        log.append(
            AuditEventKind.PLAN_GENERATED,
            action="add_route",
            parameters={
                "device": {
                    "id": "d1",
                    "api_secret": "deep-secret",
                }
            },
            summary="plan",
        )
        on_disk = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
        assert "deep-secret" not in on_disk

    def test_non_dict_parameters_rejected(self, tmp_path: Path) -> None:
        log = _log(tmp_path / "audit.jsonl")
        with pytest.raises(AuditFieldError):
            log.append(
                AuditEventKind.PLAN_GENERATED,
                summary="x",
                parameters="not-a-dict",  # type: ignore[arg-type]
            )


# ---------------------------------------------------------------------------
# Summary-Längenbegrenzung
# ---------------------------------------------------------------------------


class TestSummaryTruncation:
    def test_long_summary_truncated(self, tmp_path: Path) -> None:
        log = _log(tmp_path / "audit.jsonl")
        rec = log.append(
            AuditEventKind.DEVICE_RESULT,
            summary="x" * (SUMMARY_MAX_LEN + 200),
        )
        assert len(rec.summary) <= SUMMARY_MAX_LEN
        assert rec.summary.endswith("…")

    def test_short_summary_unchanged(self, tmp_path: Path) -> None:
        log = _log(tmp_path / "audit.jsonl")
        rec = log.append(AuditEventKind.DEVICE_RESULT, summary="kurz")
        assert rec.summary == "kurz"


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------


class TestReader:
    def test_returns_empty_list_when_file_missing(self, tmp_path: Path) -> None:
        log = _log(tmp_path / "no-such-file.jsonl")
        assert log.read_all() == []

    def test_chronological_order_preserved(self, tmp_path: Path) -> None:
        clock = FakeClock("2026-01-01T00:00:00.000Z")
        log = _log(tmp_path / "audit.jsonl", clock=clock)
        log.append(AuditEventKind.VAULT_OPENED, summary="first")
        clock.set("2026-01-02T00:00:00.000Z")
        log.append(AuditEventKind.VAULT_LOCKED, summary="second")
        records = log.read_all()
        assert [r.summary for r in records] == ["first", "second"]

    def test_skips_blank_lines(self, tmp_path: Path) -> None:
        path = tmp_path / "audit.jsonl"
        path.write_text(
            '\n   \n{"timestamp_utc":"t","actor":"a","event":"vault_opened","summary":"x"}\n\n',
            encoding="utf-8",
        )
        log = _log(path)
        assert len(log.read_all()) == 1

    def test_skips_malformed_json_lines(self, tmp_path: Path) -> None:
        path = tmp_path / "audit.jsonl"
        path.write_text(
            'not-json\n'
            '{"timestamp_utc":"t","actor":"a","event":"vault_opened","summary":"ok"}\n'
            '{broken json\n',
            encoding="utf-8",
        )
        log = _log(path)
        recs = log.read_all()
        assert len(recs) == 1
        assert recs[0].summary == "ok"

    def test_skips_lines_with_unknown_event(self, tmp_path: Path) -> None:
        path = tmp_path / "audit.jsonl"
        path.write_text(
            '{"timestamp_utc":"t","actor":"a","event":"made_up","summary":"x"}\n'
            '{"timestamp_utc":"t","actor":"a","event":"vault_opened","summary":"real"}\n',
            encoding="utf-8",
        )
        log = _log(path)
        recs = log.read_all()
        assert len(recs) == 1
        assert recs[0].summary == "real"


# ---------------------------------------------------------------------------
# Filter
# ---------------------------------------------------------------------------


class TestFilter:
    @pytest.fixture()
    def populated_log(self, tmp_path: Path) -> AuditLog:
        clock = FakeClock("2026-01-01T00:00:00.000Z")
        log = _log(tmp_path / "audit.jsonl", actor="alice", clock=clock)
        log.append(
            AuditEventKind.DEVICE_RESULT,
            action="add_route", target_device_id="d1",
            status="Verifiziert", summary="r1",
        )
        clock.set("2026-01-02T00:00:00.000Z")
        log.append(
            AuditEventKind.DEVICE_RESULT,
            action="add_route", target_device_id="d2",
            status="Fehlgeschlagen", summary="r2",
        )
        clock.set("2026-01-03T00:00:00.000Z")
        log.append(
            AuditEventKind.DEVICE_RESULT,
            action="add_alias", target_device_id="d1",
            status="Verifiziert", summary="r3",
        )
        clock.set("2026-01-04T00:00:00.000Z")
        log.append(AuditEventKind.VAULT_LOCKED, summary="lock")
        return log

    def test_filter_by_event(self, populated_log: AuditLog) -> None:
        recs = populated_log.filter(event=AuditEventKind.VAULT_LOCKED)
        assert len(recs) == 1

    def test_filter_by_action(self, populated_log: AuditLog) -> None:
        recs = populated_log.filter(action="add_route")
        assert len(recs) == 2

    def test_filter_by_device(self, populated_log: AuditLog) -> None:
        recs = populated_log.filter(target_device_id="d1")
        assert {r.summary for r in recs} == {"r1", "r3"}

    def test_filter_by_time_range(self, populated_log: AuditLog) -> None:
        recs = populated_log.filter(
            since_iso="2026-01-02T00:00:00.000Z",
            until_iso="2026-01-03T23:59:59.999Z",
        )
        assert {r.summary for r in recs} == {"r2", "r3"}

    def test_filter_combines_criteria(self, populated_log: AuditLog) -> None:
        recs = populated_log.filter(action="add_route", target_device_id="d1")
        assert len(recs) == 1
        assert recs[0].summary == "r1"


# ---------------------------------------------------------------------------
# Vertragstest: kein Schlüssel namens api_key/api_secret/password/token
# ---------------------------------------------------------------------------


class TestNoRawSecretFields:
    def test_audit_record_has_no_secret_named_fields(self) -> None:
        forbidden = {"api_key", "api_secret", "password", "token", "secret"}
        field_names = {f.name for f in dataclasses.fields(AuditRecord)}
        assert not (field_names & forbidden), (
            "AuditRecord darf keine Felder mit Secret-Namen tragen — "
            "Secrets gehen nur durch mask_dict()."
        )


# ---------------------------------------------------------------------------
# from_dict / to_dict Roundtrip + Reader-Helfer
# ---------------------------------------------------------------------------


class TestRecordDictRoundtrip:
    def test_to_dict_from_dict(self) -> None:
        rec = AuditRecord(
            timestamp_utc="2026-01-01T00:00:00.000Z",
            actor="alice",
            event=AuditEventKind.DEVICE_RESULT,
            summary="ok",
            action="add_route",
            target_device_id="d1",
            target_device_name="Berlin",
            status="Verifiziert",
            duration_ms=42,
        )
        recovered = AuditRecord.from_dict(rec.to_dict())
        assert recovered == rec

    def test_from_dict_handles_int_strings_for_duration(self) -> None:
        rec = AuditRecord.from_dict(
            {
                "timestamp_utc": "t",
                "actor": "a",
                "event": "device_result",
                "summary": "x",
                "duration_ms": "1234",
            }
        )
        assert rec.duration_ms == 1234

    def test_from_dict_handles_garbage_duration(self) -> None:
        rec = AuditRecord.from_dict(
            {
                "timestamp_utc": "t",
                "actor": "a",
                "event": "device_result",
                "summary": "x",
                "duration_ms": "kein-int",
            }
        )
        assert rec.duration_ms is None

    def test_from_dict_handles_bool_duration(self) -> None:
        rec = AuditRecord.from_dict(
            {
                "timestamp_utc": "t",
                "actor": "a",
                "event": "device_result",
                "summary": "x",
                "duration_ms": True,
            }
        )
        # bool gilt als int — wir akzeptieren das defensiv und konvertieren.
        assert rec.duration_ms == 1

    def test_from_dict_ignores_invalid_parameters_type(self) -> None:
        rec = AuditRecord.from_dict(
            {
                "timestamp_utc": "t",
                "actor": "a",
                "event": "device_result",
                "summary": "x",
                "parameters": "not-a-dict",
            }
        )
        assert rec.parameters is None

    def test_from_dict_handles_empty_optional_strings(self) -> None:
        rec = AuditRecord.from_dict(
            {
                "timestamp_utc": "t",
                "actor": "a",
                "event": "device_result",
                "summary": "x",
                "action": "",  # leer → None
                "target_device_id": None,
            }
        )
        assert rec.action is None
        assert rec.target_device_id is None


# ---------------------------------------------------------------------------
# Default-Pfad
# ---------------------------------------------------------------------------


class TestDefaultAuditPath:
    def test_uses_appdata_dir(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"APPDATA": str(tmp_path)}):
            result = default_audit_path()
        assert result.name == AUDIT_FILENAME
        assert str(tmp_path) in str(result)


# ---------------------------------------------------------------------------
# Default-Actor (Fallback)
# ---------------------------------------------------------------------------


class TestDefaultActor:
    def test_uses_os_user_when_no_actor_passed(self, tmp_path: Path) -> None:
        # Wir prüfen nur, dass irgendein nicht-leerer String reinkommt — der
        # konkrete Wert hängt vom OS-Login der CI/Dev-Maschine ab.
        log = AuditLog(path=tmp_path / "audit.jsonl")
        assert log.actor
        assert isinstance(log.actor, str)
