"""Tests fuer SqliteAuditBackend (v3.1).

Reuse-Spirit: dieselben Verhaltens-Erwartungen wie ``AuditLog``-Tests —
nur das Storage-Layer wechselt. Beide Backends muessen Aufrufer
identisch bedienen.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from opn_cockpit.audit.log import AuditEventKind, AuditFieldError
from opn_cockpit.audit.sqlite_backend import SqliteAuditBackend
from opn_cockpit.storage.sqlite_db import SqliteDb


@pytest.fixture()
def backend(tmp_path: Path) -> SqliteAuditBackend:
    db = SqliteDb(path=tmp_path / "audit.db")
    return SqliteAuditBackend(db=db, actor="alice")


class TestAppend:
    def test_inserts_minimal_record(self, backend: SqliteAuditBackend) -> None:
        record = backend.append(
            AuditEventKind.VAULT_OPENED,
            summary="Vault entsperrt.",
        )
        assert record.summary == "Vault entsperrt."
        assert record.actor == "alice"

    def test_inserts_with_parameters_masked(
        self, backend: SqliteAuditBackend,
    ) -> None:
        backend.append(
            AuditEventKind.DEVICE_RESULT,
            summary="result",
            target_device_id="dev-001",
            parameters={"api_secret": "geheim", "host": "lab"},
        )
        rec = backend.read_all()[0]
        assert rec.parameters is not None
        assert rec.parameters["api_secret"] == "***"
        assert rec.parameters["host"] == "lab"

    def test_unknown_field_rejected(self, backend: SqliteAuditBackend) -> None:
        with pytest.raises(AuditFieldError):
            backend.append(
                AuditEventKind.VAULT_OPENED,
                summary="x",
                weird_field="should fail",
            )

    def test_summary_truncated(self, backend: SqliteAuditBackend) -> None:
        long = "x" * 500
        backend.append(AuditEventKind.VAULT_OPENED, summary=long)
        rec = backend.read_all()[0]
        assert len(rec.summary) < 500
        assert rec.summary.endswith("…")


class TestReadAndFilter:
    def test_read_all_preserves_order(self, backend: SqliteAuditBackend) -> None:
        for i in range(3):
            backend.append(
                AuditEventKind.VAULT_OPENED, summary=f"event-{i}",
            )
        records = backend.read_all()
        assert [r.summary for r in records] == ["event-0", "event-1", "event-2"]

    def test_filter_by_event(self, backend: SqliteAuditBackend) -> None:
        backend.append(AuditEventKind.VAULT_OPENED, summary="open")
        backend.append(AuditEventKind.VAULT_LOCKED, summary="lock")
        backend.append(AuditEventKind.VAULT_OPENED, summary="open2")
        opened = backend.filter(event=AuditEventKind.VAULT_OPENED)
        assert [r.summary for r in opened] == ["open", "open2"]

    def test_filter_by_device(self, backend: SqliteAuditBackend) -> None:
        backend.append(
            AuditEventKind.DEVICE_RESULT, summary="d1",
            target_device_id="dev-001",
        )
        backend.append(
            AuditEventKind.DEVICE_RESULT, summary="d2",
            target_device_id="dev-002",
        )
        results = backend.filter(target_device_id="dev-001")
        assert len(results) == 1
        assert results[0].summary == "d1"

    def test_filter_by_action(self, backend: SqliteAuditBackend) -> None:
        backend.append(
            AuditEventKind.DEVICE_RESULT, summary="add-route", action="add_route",
            target_device_id="dev-001",
        )
        backend.append(
            AuditEventKind.DEVICE_RESULT, summary="add-alias", action="add_alias",
            target_device_id="dev-001",
        )
        results = backend.filter(action="add_route")
        assert len(results) == 1
        assert results[0].action == "add_route"

    def test_filter_by_time_range(self, backend: SqliteAuditBackend) -> None:
        # Klammer den Timestamp explizit durch Monkey-Patch des clock.
        ts = ["2026-01-01T00:00:00.000Z", "2026-06-01T00:00:00.000Z", "2026-12-01T00:00:00.000Z"]
        idx = [0]

        def fake_clock() -> str:
            v = ts[idx[0]]
            idx[0] += 1
            return v

        backend.clock = fake_clock
        for s in ("a", "b", "c"):
            backend.append(AuditEventKind.VAULT_OPENED, summary=s)
        mid = backend.filter(
            since_iso="2026-03-01T00:00:00.000Z",
            until_iso="2026-09-01T00:00:00.000Z",
        )
        assert [r.summary for r in mid] == ["b"]


class TestPersistence:
    def test_records_survive_reopen(self, tmp_path: Path) -> None:
        db_path = tmp_path / "audit.db"
        db1 = SqliteDb(path=db_path)
        backend1 = SqliteAuditBackend(db=db1, actor="alice")
        backend1.append(AuditEventKind.VAULT_OPENED, summary="persist-me")
        db1.close()

        db2 = SqliteDb(path=db_path)
        backend2 = SqliteAuditBackend(db=db2, actor="alice")
        records = backend2.read_all()
        assert len(records) == 1
        assert records[0].summary == "persist-me"
