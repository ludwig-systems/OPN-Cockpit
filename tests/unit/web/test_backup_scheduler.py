"""Tests fuer den Scheduled-Auto-Backup-Hintergrund-Scheduler.

Wir testen die Tick-Logik direkt (``_tick_once``), nicht den Daemon-
Thread. Der Thread ist nur das Timing-Wrapping um die Tick-Methode.
"""

from __future__ import annotations

import gzip
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from opn_cockpit.audit.log import AuditEventKind, AuditRecord
from opn_cockpit.backups import append_backup, list_backups
from opn_cockpit.security.session import Session
from opn_cockpit.vault.model import VaultData, VaultDevice, VaultSettings
from opn_cockpit.vault.store import create_vault, open_vault
from opn_cockpit.web.auth.manager import SessionManager
from opn_cockpit.web.backup_scheduler import BackupScheduler

PASSWORD = "korrektes-pferd-batterie-heftklammer"


class _RecordingAudit:
    """In-Memory AuditBackend-Stand-In, das ``append``-Calls sammelt."""

    def __init__(self) -> None:
        self.records: list[tuple[AuditEventKind, dict[str, object]]] = []

    def append(self, event: AuditEventKind, **kwargs: object) -> AuditRecord:
        self.records.append((event, dict(kwargs)))
        return AuditRecord(
            timestamp_utc="2026-06-03T12:00:00.000000Z",
            actor="test",
            event=event,
            summary=str(kwargs.get("summary", "")),
        )

    def stream(self, *_args: object, **_kwargs: object) -> list[AuditRecord]:
        return []

    def reset(self) -> None:
        self.records.clear()


@pytest.fixture()
def storage_root(tmp_path: Path) -> Path:
    return tmp_path / "backups"


@pytest.fixture()
def vault_settings_on() -> VaultSettings:
    return VaultSettings(
        scheduled_backup_enabled=True,
        scheduled_backup_interval_hours=24,
        backup_retention_pre_apply=30,
        backup_retention_scheduled=90,
    )


def _open_session(tmp_path: Path, settings: VaultSettings) -> tuple[Session, Path]:
    vault_path = tmp_path / "test.opnvault"
    data = VaultData(
        devices=[
            VaultDevice(
                id="dev-001",
                name="HQ Berlin",
                host="opn-1.lab",
                port=443,
                tls_verify=True,
                tags=[],
                api_key="k",
                api_secret="s",
                descr="",
            ),
            VaultDevice(
                id="dev-002",
                name="Lab 2",
                host="opn-2.lab",
                port=443,
                tls_verify=True,
                tags=[],
                api_key="k",
                api_secret="s",
                descr="",
            ),
        ],
        settings=settings,
    )
    create_vault(vault_path, PASSWORD, data)
    opened = open_vault(vault_path, PASSWORD)
    session = Session()
    session.unlock(opened, vault_path, PASSWORD)
    return session, vault_path


def _seed_manager(session: Session, vault_path: Path) -> SessionManager:
    manager = SessionManager()
    # Direkt in die Registry schreiben - der Test bypassed create_vault/unlock.
    from opn_cockpit.web.auth.manager import _SessionEntry

    manager._sessions["tok"] = _SessionEntry(session=session, vault_path=vault_path)
    return manager


class _FakeServerState:
    """Minimaler ServerState-Stand-In fuer den Multi-User-Server-Test."""

    def __init__(self, opened: object, vault_path: Path) -> None:
        self.opened_vault = opened
        self.vault_path = vault_path


class TestDueDetection:
    def test_no_backups_yet_is_due(
        self,
        tmp_path: Path,
        storage_root: Path,
        vault_settings_on: VaultSettings,
    ) -> None:
        session, vault_path = _open_session(tmp_path, vault_settings_on)
        manager = _seed_manager(session, vault_path)
        sched = BackupScheduler(
            manager,
            backup_storage_root=storage_root,
            audit_backend=_RecordingAudit(),
        )
        device = session.opened.data.devices[0]
        assert sched._is_due(device, interval_hours=24)

    def test_recent_backup_not_due(
        self,
        tmp_path: Path,
        storage_root: Path,
        vault_settings_on: VaultSettings,
    ) -> None:
        session, vault_path = _open_session(tmp_path, vault_settings_on)
        manager = _seed_manager(session, vault_path)
        sched = BackupScheduler(
            manager,
            backup_storage_root=storage_root,
            audit_backend=_RecordingAudit(),
        )
        device = session.opened.data.devices[0]
        append_backup(
            device.id,
            b"<opnsense/>",
            trigger="scheduled",
            storage_root=storage_root,
        )
        assert not sched._is_due(device, interval_hours=24)

    def test_old_backup_is_due(
        self,
        tmp_path: Path,
        storage_root: Path,
        vault_settings_on: VaultSettings,
    ) -> None:
        session, vault_path = _open_session(tmp_path, vault_settings_on)
        manager = _seed_manager(session, vault_path)
        sched = BackupScheduler(
            manager,
            backup_storage_root=storage_root,
            audit_backend=_RecordingAudit(),
        )
        device = session.opened.data.devices[0]
        # Echtes Backup anlegen, dann Index-Timestamp manuell zurueckdatieren.
        append_backup(
            device.id,
            b"<opnsense/>",
            trigger="scheduled",
            storage_root=storage_root,
        )
        import json

        idx_path = storage_root / device.id / "index.json"
        records = json.loads(idx_path.read_text())
        old = datetime.now(UTC) - timedelta(hours=48)
        records[0]["timestamp_utc"] = old.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        idx_path.write_text(json.dumps(records))
        assert sched._is_due(device, interval_hours=24)


class TestTickOnce:
    def test_disabled_does_nothing(
        self,
        tmp_path: Path,
        storage_root: Path,
    ) -> None:
        session, vault_path = _open_session(
            tmp_path,
            VaultSettings(
                scheduled_backup_enabled=False,
            ),
        )
        manager = _seed_manager(session, vault_path)
        audit = _RecordingAudit()
        sched = BackupScheduler(
            manager,
            backup_storage_root=storage_root,
            audit_backend=audit,
        )
        tick = sched._tick_once()
        assert tick.backups_taken == 0
        assert tick.failures == 0
        assert audit.records == []

    def test_takes_backup_on_first_tick(
        self,
        tmp_path: Path,
        storage_root: Path,
        vault_settings_on: VaultSettings,
    ) -> None:
        session, vault_path = _open_session(tmp_path, vault_settings_on)
        manager = _seed_manager(session, vault_path)
        audit = _RecordingAudit()
        sched = BackupScheduler(
            manager,
            backup_storage_root=storage_root,
            audit_backend=audit,
        )
        with patch(
            "opn_cockpit.web.backup_scheduler.download_backup",
            return_value=b"<opnsense><dummy/></opnsense>",
        ):
            tick = sched._tick_once()
        assert tick.devices_checked == 2
        assert tick.backups_taken == 2
        assert tick.failures == 0
        # Beide Geraete haben jetzt einen scheduled-Eintrag im Index
        for device in session.opened.data.devices:
            records = list_backups(device.id, storage_root=storage_root)
            assert len(records) == 1
            assert records[0].trigger == "scheduled"
            # Inhalt ist gzip + identisch zum Original
            blob = storage_root / device.id / f"{records[0].id}.xml.gz"
            assert gzip.decompress(blob.read_bytes()).startswith(b"<opnsense>")
        # Audit hat zwei Erfolgs-Eintraege
        success = [r for r in audit.records if r[0] == AuditEventKind.SCHEDULED_BACKUP]
        assert len(success) == 2

    def test_failed_backup_logs_audit_no_persist(
        self,
        tmp_path: Path,
        storage_root: Path,
        vault_settings_on: VaultSettings,
    ) -> None:
        from opn_cockpit.core.errors import UnreachableError, make_context

        session, vault_path = _open_session(tmp_path, vault_settings_on)
        manager = _seed_manager(session, vault_path)
        audit = _RecordingAudit()
        sched = BackupScheduler(
            manager,
            backup_storage_root=storage_root,
            audit_backend=audit,
        )

        def boom(*_a: object, **_k: object) -> bytes:
            raise UnreachableError(
                "timeout",
                context=make_context(
                    host="opn-1.lab",
                    port=443,
                    error_kind="connect_timeout",
                    summary="TCP-Connect-Timeout",
                ),
            )

        with patch(
            "opn_cockpit.web.backup_scheduler.download_backup",
            side_effect=boom,
        ):
            tick = sched._tick_once()
        assert tick.backups_taken == 0
        assert tick.failures == 2
        # Keine Backups auf Disk
        for device in session.opened.data.devices:
            assert list_backups(device.id, storage_root=storage_root) == []
        # Audit hat zwei FEHLGESCHLAGEN-Eintraege
        failed = [
            r
            for r in audit.records
            if r[0] == AuditEventKind.SCHEDULED_BACKUP
            and "FEHLGESCHLAGEN" in str(r[1].get("summary", ""))
        ]
        assert len(failed) == 2

    def test_central_vault_runs_without_sessions(
        self,
        tmp_path: Path,
        storage_root: Path,
        vault_settings_on: VaultSettings,
    ) -> None:
        """Multi-User-Server-Mode-Bug: Scheduler war nach Logout still.

        Sessions sind leer, aber server_state hat den Vault entsperrt.
        Scheduler muss trotzdem die Geraete-Backup-Schleife durchlaufen.
        """
        session, vault_path = _open_session(tmp_path, vault_settings_on)
        # Manager bewusst LEER lassen (kein _seed_manager) - das simuliert
        # den Zustand nach Browser-Logout im Multi-User-Server-Mode.
        manager = SessionManager()
        fake_state = _FakeServerState(session.opened, vault_path)
        audit = _RecordingAudit()
        sched = BackupScheduler(
            manager,
            server_state=fake_state,  # type: ignore[arg-type]
            backup_storage_root=storage_root,
            audit_backend=audit,
        )
        with patch(
            "opn_cockpit.web.backup_scheduler.download_backup",
            return_value=b"<opnsense><dummy/></opnsense>",
        ):
            tick = sched._tick_once()
        assert tick.devices_checked == 2
        assert tick.backups_taken == 2
        for device in session.opened.data.devices:
            assert len(list_backups(device.id, storage_root=storage_root)) == 1

    def test_already_fresh_skips_run(
        self,
        tmp_path: Path,
        storage_root: Path,
        vault_settings_on: VaultSettings,
    ) -> None:
        session, vault_path = _open_session(tmp_path, vault_settings_on)
        manager = _seed_manager(session, vault_path)
        audit = _RecordingAudit()
        # Beide Geraete haben schon einen scheduled-Eintrag
        for device in session.opened.data.devices:
            append_backup(
                device.id,
                b"<opnsense/>",
                trigger="scheduled",
                storage_root=storage_root,
            )
        sched = BackupScheduler(
            manager,
            backup_storage_root=storage_root,
            audit_backend=audit,
        )
        with patch(
            "opn_cockpit.web.backup_scheduler.download_backup",
            return_value=b"<opnsense/>",
        ) as dl:
            tick = sched._tick_once()
        assert tick.backups_taken == 0
        assert tick.failures == 0
        # download_backup wurde gar nicht erst aufgerufen
        assert dl.call_count == 0
