"""Tests fuer die Audit-Log-HMAC-Hash-Chain (v4-Pass 3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from opn_cockpit.audit.chain import (
    GENESIS_HASH,
    ChainedRecord,
    canonical_record,
    compute_this_hash,
    load_or_generate_secret,
    verify_chain,
)
from opn_cockpit.audit.log import AuditEventKind, AuditRecord
from opn_cockpit.audit.sqlite_backend import SqliteAuditBackend
from opn_cockpit.storage.sqlite_db import SqliteDb

SECRET = b"test-server-secret-32-bytes-long!"


def _record(event: AuditEventKind, summary: str, actor: str = "alice") -> AuditRecord:
    return AuditRecord(
        timestamp_utc="2026-01-01T00:00:00.000Z",
        actor=actor,
        event=event,
        summary=summary,
    )


class TestCanonicalRecord:
    def test_stable_serialization(self) -> None:
        r = _record(AuditEventKind.VAULT_OPENED, "open")
        out = canonical_record(r)
        # Sorted keys + UTF-8
        assert b'"event":' in out
        assert b'"summary":"open"' in out

    def test_same_record_same_bytes(self) -> None:
        r1 = _record(AuditEventKind.VAULT_OPENED, "open")
        r2 = _record(AuditEventKind.VAULT_OPENED, "open")
        assert canonical_record(r1) == canonical_record(r2)

    def test_different_record_different_bytes(self) -> None:
        r1 = _record(AuditEventKind.VAULT_OPENED, "open")
        r2 = _record(AuditEventKind.VAULT_OPENED, "different")
        assert canonical_record(r1) != canonical_record(r2)


class TestComputeHash:
    def test_deterministic(self) -> None:
        r = _record(AuditEventKind.VAULT_OPENED, "x")
        h1 = compute_this_hash(SECRET, GENESIS_HASH, r)
        h2 = compute_this_hash(SECRET, GENESIS_HASH, r)
        assert h1 == h2
        assert len(h1) == 64  # SHA256 hex

    def test_changes_with_prev_hash(self) -> None:
        r = _record(AuditEventKind.VAULT_OPENED, "x")
        h1 = compute_this_hash(SECRET, GENESIS_HASH, r)
        h2 = compute_this_hash(SECRET, "ab" * 32, r)
        assert h1 != h2

    def test_changes_with_secret(self) -> None:
        r = _record(AuditEventKind.VAULT_OPENED, "x")
        h1 = compute_this_hash(SECRET, GENESIS_HASH, r)
        h2 = compute_this_hash(b"different-secret", GENESIS_HASH, r)
        assert h1 != h2


class TestVerifyChain:
    def test_empty_chain_valid(self) -> None:
        assert verify_chain([], SECRET) == []

    def test_correct_chain_valid(self) -> None:
        r1 = _record(AuditEventKind.VAULT_OPENED, "a")
        h1 = compute_this_hash(SECRET, GENESIS_HASH, r1)
        r2 = _record(AuditEventKind.VAULT_LOCKED, "b")
        h2 = compute_this_hash(SECRET, h1, r2)
        chained = [
            ChainedRecord(record=r1, prev_hash=GENESIS_HASH, this_hash=h1),
            ChainedRecord(record=r2, prev_hash=h1, this_hash=h2),
        ]
        assert verify_chain(chained, SECRET) == []

    def test_tampered_record_breaks_chain(self) -> None:
        """Ein nachtraeglich geaenderter Eintrag wird erkannt."""
        r1 = _record(AuditEventKind.VAULT_OPENED, "original")
        h1 = compute_this_hash(SECRET, GENESIS_HASH, r1)
        r2 = _record(AuditEventKind.VAULT_LOCKED, "b")
        h2 = compute_this_hash(SECRET, h1, r2)
        # Eintrag 0 nachtraeglich aendern, aber Hash bleibt der alte
        tampered = _record(AuditEventKind.VAULT_OPENED, "TAMPERED")
        chained = [
            ChainedRecord(record=tampered, prev_hash=GENESIS_HASH, this_hash=h1),
            ChainedRecord(record=r2, prev_hash=h1, this_hash=h2),
        ]
        broken = verify_chain(chained, SECRET)
        assert 0 in broken

    def test_deleted_record_breaks_chain(self) -> None:
        """Ein geloeschter Eintrag bricht prev_hash der nachfolgenden."""
        r1 = _record(AuditEventKind.VAULT_OPENED, "a")
        h1 = compute_this_hash(SECRET, GENESIS_HASH, r1)
        r2 = _record(AuditEventKind.VAULT_LOCKED, "b")
        h2 = compute_this_hash(SECRET, h1, r2)
        # Eintrag 0 fehlt — r2 hat noch prev_hash=h1, der gehoert aber zu r1
        chained = [
            ChainedRecord(record=r2, prev_hash=h1, this_hash=h2),
        ]
        broken = verify_chain(chained, SECRET)
        assert 0 in broken


# ---------------------------------------------------------------------------
# Integration: SqliteAuditBackend mit Hash-Chain
# ---------------------------------------------------------------------------


@pytest.fixture()
def backend(tmp_path: Path) -> SqliteAuditBackend:
    return SqliteAuditBackend(
        db=SqliteDb(path=tmp_path / "audit.db"),
        actor="alice",
        chain_secret=SECRET,
    )


class TestSqliteChainIntegration:
    def test_inserts_with_hash_fields(self, backend: SqliteAuditBackend) -> None:
        backend.append(AuditEventKind.VAULT_OPENED, summary="open")
        chained = backend.read_chain()
        assert len(chained) == 1
        assert chained[0].prev_hash == GENESIS_HASH
        assert len(chained[0].this_hash) == 64

    def test_chain_links_correctly(self, backend: SqliteAuditBackend) -> None:
        backend.append(AuditEventKind.VAULT_OPENED, summary="a")
        backend.append(AuditEventKind.VAULT_LOCKED, summary="b")
        backend.append(AuditEventKind.VAULT_OPENED, summary="c")
        chained = backend.read_chain()
        assert len(chained) == 3
        # Verifikation laeuft durch
        assert verify_chain(chained, SECRET) == []

    def test_tampering_via_db_detected(
        self, backend: SqliteAuditBackend, tmp_path: Path,
    ) -> None:
        """Manuelle DB-Manipulation wird vom verify_chain erkannt."""
        backend.append(AuditEventKind.VAULT_OPENED, summary="original")
        backend.append(AuditEventKind.VAULT_LOCKED, summary="x")
        # Eintrag 1 direkt in der DB faelschen
        with backend.db.transaction() as conn:
            conn.execute(
                "UPDATE audit SET summary = ? WHERE id = 1",
                ("MALICIOUS",),
            )
        chained = backend.read_chain()
        broken = verify_chain(chained, SECRET)
        assert broken  # mindestens ein Eintrag ist gebrochen

    def test_no_chain_when_secret_is_none(self, tmp_path: Path) -> None:
        """Backward-Compat: ohne Secret werden keine Hashes geschrieben."""
        backend = SqliteAuditBackend(
            db=SqliteDb(path=tmp_path / "audit.db"),
            actor="alice",
            chain_secret=None,
        )
        backend.append(AuditEventKind.VAULT_OPENED, summary="x")
        # read_chain liefert nur Eintraege mit gesetztem this_hash
        assert backend.read_chain() == []
        # Normaler read_all funktioniert weiterhin
        assert len(backend.read_all()) == 1


# ---------------------------------------------------------------------------
# Secret-Persistenz
# ---------------------------------------------------------------------------


class TestSecretLifecycle:
    def test_generates_and_persists(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("OPNCOCKPIT_DATA_DIR", str(tmp_path))
        monkeypatch.delenv("OPNCOCKPIT_AUDIT_SECRET", raising=False)
        s1 = load_or_generate_secret()
        assert len(s1) == 32
        s2 = load_or_generate_secret()
        assert s1 == s2

    def test_env_override_hex(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("OPNCOCKPIT_DATA_DIR", str(tmp_path))
        hex_value = "ab" * 16
        monkeypatch.setenv("OPNCOCKPIT_AUDIT_SECRET", hex_value)
        secret = load_or_generate_secret()
        assert secret == bytes.fromhex(hex_value)

    def test_env_override_non_hex_taken_as_utf8(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("OPNCOCKPIT_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("OPNCOCKPIT_AUDIT_SECRET", "not-hex-but-still-a-secret")
        secret = load_or_generate_secret()
        assert secret == b"not-hex-but-still-a-secret"
