"""Tests fuer den Auto-Retry-Watcher.

Wir testen den Watcher-State + die Tick-Logik direkt, nicht den Daemon-
Thread - der ist nur das Timing-Wrapping.
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from opn_cockpit.config import get_app_data_dir
from opn_cockpit.core.objects.base import Diff, DiffKind
from opn_cockpit.core.objects.routes import RouteSpec
from opn_cockpit.core.result import (
    Phase,
    Result,
    RolloutReport,
    Status,
)
from opn_cockpit.inventory.model import Device
from opn_cockpit.orchestration.plan_store import PlanStore
from opn_cockpit.orchestration.planner import Plan, PlannedDeviceAction
from opn_cockpit.security.session import Session
from opn_cockpit.vault.format import VaultHeader
from opn_cockpit.vault.model import VaultData, VaultDevice
from opn_cockpit.vault.store import OpenedVault, create_vault
from opn_cockpit.web.auth.manager import SessionManager
from opn_cockpit.web.retry_watcher import RetryWatcher
from opn_cockpit.web.server import create_app

PASSWORD = "korrektes-pferd-batterie-heftklammer"


def _vault(tmp_path: Path) -> Path:
    path = tmp_path / "test.opnvault"
    create_vault(
        path,
        PASSWORD,
        VaultData(
            devices=[
                VaultDevice(
                    id="dev-001",
                    name="Box 1",
                    host="opn-1.lab",
                    port=443,
                    tls_verify=True,
                    tags=[],
                    api_key="k",
                    api_secret="s",
                    descr="",
                ),
            ]
        ),
    )
    return path


def _unlock(client: TestClient, vault_path: Path) -> str:
    response = client.post(
        "/api/auth/unlock",
        json={"vault_path": str(vault_path), "password": PASSWORD},
    )
    assert response.status_code == 200
    return str(response.json()["token"])


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _seed_plan(plan_id: str = "pl-12345678") -> None:
    spec = RouteSpec(network="10.0.0.0/24", gateway="WAN_GW")
    action = PlannedDeviceAction(
        device=Device(
            id="dev-001",
            name="Box 1",
            host="opn-1.lab",
            port=443,
            tls_verify=True,
            tags=(),
            descr="",
        ),
        target_spec=spec,
        current_state=None,
        diff=Diff(kind=DiffKind.NEW, summary="NEW"),
        payload_masked={},
    )
    plan = Plan(
        plan_id=plan_id,
        action="add_route",
        subsystem="routes",
        created_at_utc="2026-05-29T12:00:00.000Z",
        actions=(action,),
    )
    PlanStore(base_dir=get_app_data_dir() / "plans").save(plan)


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    return TestClient(create_app())


@pytest.fixture()
def vault(tmp_path: Path) -> Path:
    return _vault(tmp_path)


@pytest.fixture()
def token(client: TestClient, vault: Path) -> str:
    return _unlock(client, vault)


# ---------------------------------------------------------------------------
# HTTP-Endpoints
# ---------------------------------------------------------------------------


class TestRetryEndpoints:
    def test_status_requires_auth(self, client: TestClient) -> None:
        assert client.get("/api/retry/status").status_code == 401

    def test_status_empty_initially(self, client: TestClient, token: str) -> None:
        response = client.get("/api/retry/status", headers=_bearer(token))
        assert response.status_code == 200
        assert response.json()["jobs"] == []

    def test_schedule_creates_job(self, client: TestClient, token: str) -> None:
        _seed_plan()
        response = client.post(
            "/api/retry/schedule",
            json={
                "plan_id": "pl-12345678",
                "device_ids": ["dev-001"],
                "interval_s": 60,
                "max_duration_s": 600,
            },
            headers=_bearer(token),
        )
        assert response.status_code == 201
        body = response.json()
        assert body["plan_id"] == "pl-12345678"
        assert body["device_ids"] == ["dev-001"]
        # In Status sichtbar
        status_resp = client.get("/api/retry/status", headers=_bearer(token))
        assert len(status_resp.json()["jobs"]) == 1

    def test_cancel_removes_job(self, client: TestClient, token: str) -> None:
        _seed_plan()
        client.post(
            "/api/retry/schedule",
            json={"plan_id": "pl-12345678", "device_ids": ["dev-001"]},
            headers=_bearer(token),
        )
        d = client.delete("/api/retry/jobs/pl-12345678", headers=_bearer(token))
        assert d.status_code == 204
        status_resp = client.get("/api/retry/status", headers=_bearer(token))
        assert status_resp.json()["jobs"] == []

    def test_cancel_unknown_job_404(self, client: TestClient, token: str) -> None:
        response = client.delete(
            "/api/retry/jobs/pl-12345678",
            headers=_bearer(token),
        )
        assert response.status_code == 404

    def test_lock_cancels_all_jobs_of_token(
        self,
        client: TestClient,
        token: str,
    ) -> None:
        _seed_plan()
        client.post(
            "/api/retry/schedule",
            json={"plan_id": "pl-12345678", "device_ids": ["dev-001"]},
            headers=_bearer(token),
        )
        # Lock killt das Token + die zugehoerigen Jobs.
        client.post("/api/auth/lock", headers=_bearer(token))
        # Mit dem alten Token: 401
        s = client.get("/api/retry/status", headers=_bearer(token))
        assert s.status_code == 401


# ---------------------------------------------------------------------------
# Tick-Verhalten direkt (kein Thread)
# ---------------------------------------------------------------------------


def _make_session() -> Session:
    data = VaultData(
        devices=[
            VaultDevice(
                id="dev-001",
                name="Box 1",
                host="opn-1.lab",
                port=443,
                tls_verify=True,
                tags=[],
                api_key="k",
                api_secret="s",
                descr="",
            ),
        ]
    )
    header = VaultHeader(
        version=1,
        kdf_salt=b"\x00" * 16,
        kdf_time_cost=1,
        kdf_memory_cost_kib=8,
        kdf_parallelism=1,
        nonce=b"\x00" * 12,
    )
    session = Session()
    session.unlock(OpenedVault(data=data, header=header), Path("dummy"), PASSWORD)
    return session


class TestWatcherTick:
    def test_success_removes_job(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
        manager = SessionManager()
        session = _make_session()
        token, _ = manager.create(
            session.opened,
            Path("dummy"),
            PASSWORD,
        )
        watcher = RetryWatcher(manager)
        # vault_path benoetigt fuer Orphan-Adoption nach Session-Lock /
        # Server-Restart; tests benutzen Path("dummy") konsistent mit der
        # _make_session-Fixture, damit Adoption greift wenn der Token
        # spaeter revoked wird.
        watcher.schedule(
            vault_path="dummy",
            plan_id="pl-AAAAAAAA",
            session_token=token,
            device_ids=["dev-001"],
            interval_s=1,
            max_duration_s=600,
        )
        ok_report = RolloutReport(
            results=(
                Result(
                    device_id="dev-001",
                    subsystem="routes",
                    status=Status.VERIFIED,
                    short_message="ok",
                    duration_ms=300,
                ),
            )
        )
        with patch(
            "opn_cockpit.web.api.plans.run_apply",
            return_value=(
                Plan(
                    plan_id="pl-AAAAAAAA",
                    action="x",
                    subsystem="routes",
                    created_at_utc="",
                    actions=(),
                ),
                ok_report,
            ),
        ):
            # Tick weit in der Zukunft, damit is_due True ist.
            watcher._tick(_due_soon_ms())
        # Job ist weg, weil Erfolg.
        assert watcher.stats() == []

    def test_failure_increments_attempts_and_reschedules(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
        manager = SessionManager()
        session = _make_session()
        token, _ = manager.create(session.opened, Path("dummy"), PASSWORD)
        watcher = RetryWatcher(manager)
        # vault_path benoetigt fuer Orphan-Adoption nach Session-Lock /
        # Server-Restart; tests benutzen Path("dummy") konsistent mit der
        # _make_session-Fixture, damit Adoption greift wenn der Token
        # spaeter revoked wird.
        watcher.schedule(
            vault_path="dummy",
            plan_id="pl-BBBBBBBB",
            session_token=token,
            device_ids=["dev-001"],
            interval_s=1,
            max_duration_s=600,
        )
        fail_report = RolloutReport(
            results=(
                Result(
                    device_id="dev-001",
                    subsystem="routes",
                    status=Status.FAILED,
                    short_message="timeout",
                    error_kind="network",
                    failed_phase=Phase.WRITE,
                    duration_ms=3000,
                ),
            )
        )
        with patch(
            "opn_cockpit.web.api.plans.run_apply",
            return_value=(
                Plan(
                    plan_id="pl-BBBBBBBB",
                    action="x",
                    subsystem="routes",
                    created_at_utc="",
                    actions=(),
                ),
                fail_report,
            ),
        ):
            watcher._tick(_due_soon_ms())
        jobs = watcher.stats()
        assert len(jobs) == 1
        assert jobs[0].attempts == 1
        assert jobs[0].last_failure_count == 1

    def test_expired_job_is_canceled(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
        manager = SessionManager()
        session = _make_session()
        token, _ = manager.create(session.opened, Path("dummy"), PASSWORD)
        watcher = RetryWatcher(manager)
        # vault_path benoetigt fuer Orphan-Adoption nach Session-Lock /
        # Server-Restart; tests benutzen Path("dummy") konsistent mit der
        # _make_session-Fixture, damit Adoption greift wenn der Token
        # spaeter revoked wird.
        watcher.schedule(
            vault_path="dummy",
            plan_id="pl-CCCCCCCC",
            session_token=token,
            device_ids=["dev-001"],
            interval_s=1,
            max_duration_s=60,
        )
        # Tick weit in der Zukunft -> Job ist expired (max_duration ueberschritten)
        watcher._tick(_far_future_ms())
        assert watcher.stats() == []

    def test_session_lost_keeps_job_as_orphan(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Token-Verlust loescht den Job NICHT mehr - er bleibt als Orphan
        in der Queue und wird beim naechsten Vault-Unlock adoptiert.
        Nur Ablauf der max_duration entfernt den Job endgueltig.
        """
        monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
        manager = SessionManager()
        session = _make_session()
        token, _ = manager.create(session.opened, Path("dummy"), PASSWORD)
        watcher = RetryWatcher(manager)
        watcher.schedule(
            vault_path="dummy",
            plan_id="pl-DDDDDDDD",
            session_token=token,
            device_ids=["dev-001"],
            interval_s=1,
            max_duration_s=3600,
        )
        manager.revoke(token)
        # Tick mit "due aber nicht expired" - Session ist weg, keine
        # adoptierbare Session vorhanden -> Orphan bleibt in der Queue.
        watcher._tick(_due_soon_ms())
        jobs = watcher.stats()
        assert len(jobs) == 1

    def test_orphan_job_is_adopted_via_vault_path(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Orphan-Adoption: Session sperren, neue Session zum gleichen
        Vault, naechster Tick adoptiert den Token automatisch.
        """
        monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
        manager = SessionManager()
        session = _make_session()
        token, _ = manager.create(session.opened, Path("dummy"), PASSWORD)
        watcher = RetryWatcher(manager)
        watcher.schedule(
            vault_path="dummy",
            plan_id="pl-EEEEEEEE",
            session_token=token,
            device_ids=["dev-001"],
            interval_s=1,
            max_duration_s=3600,
        )
        manager.revoke(token)
        # Neue Session zum gleichen Vault-Pfad - Adoption-Kandidat
        _new_token, _ = manager.create(session.opened, Path("dummy"), PASSWORD)
        ok_report = RolloutReport(
            results=(
                Result(
                    device_id="dev-001",
                    subsystem="routes",
                    status=Status.VERIFIED,
                    short_message="ok",
                    duration_ms=300,
                ),
            )
        )
        with patch(
            "opn_cockpit.web.api.plans.run_apply",
            return_value=(
                Plan(
                    plan_id="pl-EEEEEEEE",
                    action="x",
                    subsystem="routes",
                    created_at_utc="",
                    actions=(),
                ),
                ok_report,
            ),
        ):
            watcher._tick(_due_soon_ms())
        # Erfolg -> Job ist weg
        assert watcher.stats() == []

    def test_queue_persists_across_watcher_instances(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Persistierte Jobs ueberleben Server-Restart als Orphan."""
        monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
        queue_path = tmp_path / "state" / "retry-queue.json"
        manager = SessionManager()
        session = _make_session()
        token, _ = manager.create(session.opened, Path("dummy"), PASSWORD)
        watcher1 = RetryWatcher(manager, queue_path=queue_path)
        watcher1.schedule(
            vault_path="dummy",
            plan_id="pl-FFFFFFFF",
            session_token=token,
            device_ids=["dev-001"],
            interval_s=60,
            max_duration_s=3600,
        )
        assert queue_path.exists()
        # Neuer Watcher (Restart simuliert) liest die Queue
        manager2 = SessionManager()
        watcher2 = RetryWatcher(manager2, queue_path=queue_path)
        jobs = watcher2.stats()
        assert len(jobs) == 1
        assert jobs[0].plan_id == "pl-FFFFFFFF"
        assert jobs[0].vault_path == "dummy"
        # Token ist Orphan - alle Tokens sind beim Restart stale
        assert jobs[0].session_token == ""


def _due_soon_ms() -> int:
    """Now + 2 s — triggert is_due (interval_s=1) ohne is_expired zu treffen."""
    return int(time.time() * 1000) + 2000


def _far_future_ms() -> int:
    # Sehr grosser Wert > started_at_ms + max_duration_s, sorgt dafuer
    # dass is_due immer True ist (und is_expired bei kurzer max_duration).
    return int(time.time() * 1000) + 10**9
