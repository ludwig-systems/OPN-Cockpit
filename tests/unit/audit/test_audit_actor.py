"""Tests fuer per-Aufruf-Actor-Override in beiden Audit-Backends (v4)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from opn_cockpit.audit.backend import audit_actor
from opn_cockpit.audit.log import AuditEventKind, AuditLog
from opn_cockpit.audit.sqlite_backend import SqliteAuditBackend
from opn_cockpit.storage.sqlite_db import SqliteDb


class TestActorOverride:
    def test_file_backend_uses_default_actor(self, tmp_path: Path) -> None:
        log = AuditLog(path=tmp_path / "audit.jsonl", actor="default-actor")
        log.append(AuditEventKind.VAULT_OPENED, summary="x")
        record = log.read_all()[0]
        assert record.actor == "default-actor"

    def test_file_backend_actor_override(self, tmp_path: Path) -> None:
        log = AuditLog(path=tmp_path / "audit.jsonl", actor="default-actor")
        log.append(
            AuditEventKind.VAULT_OPENED, summary="x", actor="alice",
        )
        record = log.read_all()[0]
        assert record.actor == "alice"

    def test_sqlite_backend_uses_default_actor(self, tmp_path: Path) -> None:
        backend = SqliteAuditBackend(
            db=SqliteDb(path=tmp_path / "audit.db"),
            actor="os-user",
        )
        backend.append(AuditEventKind.VAULT_OPENED, summary="x")
        assert backend.read_all()[0].actor == "os-user"

    def test_sqlite_backend_actor_override(self, tmp_path: Path) -> None:
        backend = SqliteAuditBackend(
            db=SqliteDb(path=tmp_path / "audit.db"),
            actor="os-user",
        )
        backend.append(
            AuditEventKind.VAULT_OPENED, summary="x", actor="alice",
        )
        assert backend.read_all()[0].actor == "alice"

    def test_empty_actor_falls_back_to_default(self, tmp_path: Path) -> None:
        log = AuditLog(path=tmp_path / "audit.jsonl", actor="default-actor")
        log.append(AuditEventKind.VAULT_OPENED, summary="x", actor=None)
        assert log.read_all()[0].actor == "default-actor"


@dataclass
class _User:
    username: str


@dataclass
class _Session:
    user: _User | None


class TestAuditActorHelper:
    def test_session_none_returns_none(self) -> None:
        assert audit_actor(None) is None

    def test_session_without_user_returns_none(self) -> None:
        assert audit_actor(_Session(user=None)) is None

    def test_session_with_user_returns_username(self) -> None:
        assert audit_actor(_Session(user=_User(username="alice"))) == "alice"

    def test_empty_username_returns_none(self) -> None:
        assert audit_actor(_Session(user=_User(username=""))) is None


class TestUnknownKeywordStillRejected:
    """Whitelisting darf nicht zerstoert sein durch das actor-Pop."""

    def test_file_backend_rejects_unknown_field(self, tmp_path: Path) -> None:
        log = AuditLog(path=tmp_path / "audit.jsonl", actor="x")
        with pytest.raises(Exception, match="Unzul"):
            log.append(
                AuditEventKind.VAULT_OPENED, summary="x", weird_field="bad",
            )

    def test_sqlite_backend_rejects_unknown_field(self, tmp_path: Path) -> None:
        backend = SqliteAuditBackend(
            db=SqliteDb(path=tmp_path / "audit.db"), actor="x",
        )
        with pytest.raises(Exception, match="Unzul"):
            backend.append(
                AuditEventKind.VAULT_OPENED, summary="x", weird_field="bad",
            )
