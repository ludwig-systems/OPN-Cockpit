"""Tests fuer POST /api/inventory/unbound/import-multi.

Der Endpoint ruft die bestehende Single-Device-Logik pro Geraet auf. Wir
mocken :func:`_run_unbound_import` weg, damit die Tests nicht die
komplette OPNsense-HTTP-Stack durchgehen muessen — die
Multi-Device-Aggregation ist der zu testende Anteil.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from opn_cockpit.vault.model import VaultData, VaultDevice
from opn_cockpit.vault.store import create_vault
from opn_cockpit.web.api.schemas import (
    UnboundImportAction,
    UnboundImportResponse,
)
from opn_cockpit.web.server import create_app

PASSWORD = "korrektes-pferd-batterie-heftklammer"


def _make_vault(tmp_path: Path) -> Path:
    devices = [
        VaultDevice(
            id="dev-001",
            name="HQ Berlin",
            host="opn-berlin.lab",
            port=443,
            tls_verify=True,
            tags=["branches"],
            api_key="k1",
            api_secret="s1",
            descr="",
        ),
        VaultDevice(
            id="dev-002",
            name="Lab",
            host="opn-lab.intern",
            port=443,
            tls_verify=False,
            tags=["lab"],
            api_key="k2",
            api_secret="s2",
            descr="",
        ),
        VaultDevice(
            id="dev-003",
            name="Branch",
            host="opn-branch.lab",
            port=443,
            tls_verify=True,
            tags=["branches"],
            api_key="k3",
            api_secret="s3",
            descr="",
        ),
    ]
    path = tmp_path / "test.opnvault"
    create_vault(path, PASSWORD, VaultData(devices=devices))
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


@pytest.fixture()
def client() -> TestClient:
    return TestClient(create_app())


@pytest.fixture()
def vault(tmp_path: Path) -> Path:
    return _make_vault(tmp_path)


CSV_HOSTS = (
    "host,domain,server,description,enabled\n"
    "router,lab.local,10.0.0.1,,true\n"
    "switch,lab.local,10.0.0.2,,true\n"
)


def _fake_report(
    device_id: str,
    device_name: str,
    *,
    dry_run: bool,
    add: int = 2,
    applied: bool | None = None,
) -> UnboundImportResponse:
    actions = [
        UnboundImportAction(
            row_num=i + 2,
            action="add",
            identity=f"host{i}.lab.local",
            summary=f"Neu: host{i}",
        )
        for i in range(add)
    ]
    return UnboundImportResponse(
        device_id=device_id,
        device_name=device_name,
        subsystem="unbound_hosts",
        reconcile=False,
        dry_run=dry_run,
        applied=(not dry_run) if applied is None else applied,
        parse_errors=[],
        actions=actions,
        add_count=add,
        executed_at_iso="2026-07-20T20:00:00Z" if not dry_run else "",
    )


# ---------------------------------------------------------------------------
# Auth + Schema-Validierung
# ---------------------------------------------------------------------------


class TestAuthAndValidation:
    def test_requires_auth(self, client: TestClient) -> None:
        r = client.post(
            "/api/inventory/unbound/import-multi",
            json={
                "device_ids": ["dev-001"],
                "subsystem": "unbound_hosts",
                "csv_content": CSV_HOSTS,
                "dry_run": True,
            },
        )
        assert r.status_code == 401

    def test_rejects_empty_device_ids(
        self,
        client: TestClient,
        vault: Path,
    ) -> None:
        token = _unlock(client, vault)
        r = client.post(
            "/api/inventory/unbound/import-multi",
            headers=_bearer(token),
            json={
                "device_ids": [],
                "subsystem": "unbound_hosts",
                "csv_content": CSV_HOSTS,
            },
        )
        assert r.status_code == 422

    def test_rejects_unknown_subsystem(
        self,
        client: TestClient,
        vault: Path,
    ) -> None:
        token = _unlock(client, vault)
        r = client.post(
            "/api/inventory/unbound/import-multi",
            headers=_bearer(token),
            json={
                "device_ids": ["dev-001"],
                "subsystem": "firewall_rules",
                "csv_content": CSV_HOSTS,
            },
        )
        assert r.status_code == 422

    def test_dedupes_device_ids(
        self,
        client: TestClient,
        vault: Path,
    ) -> None:
        token = _unlock(client, vault)
        seen_ids: list[str] = []

        def fake(*, session, device, payload, subsystem):
            seen_ids.append(device.id)
            return _fake_report(device.id, device.name, dry_run=True)

        with patch(
            "opn_cockpit.web.api.inventory._run_unbound_import",
            side_effect=fake,
        ):
            r = client.post(
                "/api/inventory/unbound/import-multi",
                headers=_bearer(token),
                json={
                    "device_ids": ["dev-001", "dev-001", "dev-002"],
                    "subsystem": "unbound_hosts",
                    "csv_content": CSV_HOSTS,
                    "dry_run": True,
                },
            )
        assert r.status_code == 200
        data = r.json()
        assert data["total_devices"] == 2
        assert seen_ids == ["dev-001", "dev-002"]


# ---------------------------------------------------------------------------
# Happy Path
# ---------------------------------------------------------------------------


class TestFanOut:
    def test_dry_run_aggregates_counts(
        self,
        client: TestClient,
        vault: Path,
    ) -> None:
        token = _unlock(client, vault)

        def fake(*, session, device, payload, subsystem):
            return _fake_report(device.id, device.name, dry_run=True, add=5)

        with patch(
            "opn_cockpit.web.api.inventory._run_unbound_import",
            side_effect=fake,
        ):
            r = client.post(
                "/api/inventory/unbound/import-multi",
                headers=_bearer(token),
                json={
                    "device_ids": ["dev-001", "dev-002", "dev-003"],
                    "subsystem": "unbound_hosts",
                    "csv_content": CSV_HOSTS,
                    "dry_run": True,
                },
            )
        assert r.status_code == 200
        data = r.json()
        assert data["applied"] is False
        assert data["ok_device_count"] == 3
        assert data["failed_device_count"] == 0
        assert data["add_count"] == 15  # 5 * 3
        assert len(data["results"]) == 3
        assert all(r["ok"] for r in data["results"])

    def test_apply_marks_applied_and_stamps_iso(
        self,
        client: TestClient,
        vault: Path,
    ) -> None:
        token = _unlock(client, vault)

        def fake(*, session, device, payload, subsystem):
            return _fake_report(device.id, device.name, dry_run=False, add=1)

        with patch(
            "opn_cockpit.web.api.inventory._run_unbound_import",
            side_effect=fake,
        ):
            r = client.post(
                "/api/inventory/unbound/import-multi",
                headers=_bearer(token),
                json={
                    "device_ids": ["dev-001", "dev-002"],
                    "subsystem": "unbound_hosts",
                    "csv_content": CSV_HOSTS,
                    "dry_run": False,
                },
            )
        assert r.status_code == 200
        data = r.json()
        assert data["applied"] is True
        assert data["executed_at_iso"] == "2026-07-20T20:00:00Z"


# ---------------------------------------------------------------------------
# Error-Isolation pro Device
# ---------------------------------------------------------------------------


class TestErrorIsolation:
    def test_missing_device_id_marked_failed(
        self,
        client: TestClient,
        vault: Path,
    ) -> None:
        token = _unlock(client, vault)

        def fake(*, session, device, payload, subsystem):
            return _fake_report(device.id, device.name, dry_run=True)

        with patch(
            "opn_cockpit.web.api.inventory._run_unbound_import",
            side_effect=fake,
        ):
            r = client.post(
                "/api/inventory/unbound/import-multi",
                headers=_bearer(token),
                json={
                    "device_ids": ["dev-001", "does-not-exist"],
                    "subsystem": "unbound_hosts",
                    "csv_content": CSV_HOSTS,
                    "dry_run": True,
                },
            )
        assert r.status_code == 200
        data = r.json()
        assert data["ok_device_count"] == 1
        assert data["failed_device_count"] == 1
        failed = next(r for r in data["results"] if r["device_id"] == "does-not-exist")
        assert failed["ok"] is False
        assert "nicht im Tresor" in failed["error_summary"]

    def test_single_device_http_exception_does_not_kill_others(
        self,
        client: TestClient,
        vault: Path,
    ) -> None:
        from fastapi import HTTPException

        token = _unlock(client, vault)

        def fake(*, session, device, payload, subsystem):
            if device.id == "dev-002":
                raise HTTPException(
                    status_code=502,
                    detail="Pre-Apply-Backup gescheitert.",
                )
            return _fake_report(device.id, device.name, dry_run=False, add=3)

        with patch(
            "opn_cockpit.web.api.inventory._run_unbound_import",
            side_effect=fake,
        ):
            r = client.post(
                "/api/inventory/unbound/import-multi",
                headers=_bearer(token),
                json={
                    "device_ids": ["dev-001", "dev-002", "dev-003"],
                    "subsystem": "unbound_hosts",
                    "csv_content": CSV_HOSTS,
                    "dry_run": False,
                },
            )
        assert r.status_code == 200
        data = r.json()
        assert data["ok_device_count"] == 2
        assert data["failed_device_count"] == 1
        assert data["applied"] is True
        # dev-002 failed, aber die anderen zwei haben applied.
        failed = next(r for r in data["results"] if r["device_id"] == "dev-002")
        assert failed["ok"] is False
        assert "Pre-Apply-Backup" in failed["error_summary"]
        # Beide Erfolgreichen haben je add=3 -> 6 gesamt
        assert data["add_count"] == 6

    def test_generic_exception_becomes_error_result(
        self,
        client: TestClient,
        vault: Path,
    ) -> None:
        token = _unlock(client, vault)

        def fake(*, session, device, payload, subsystem):
            if device.id == "dev-001":
                raise RuntimeError("boom")
            return _fake_report(device.id, device.name, dry_run=True)

        with patch(
            "opn_cockpit.web.api.inventory._run_unbound_import",
            side_effect=fake,
        ):
            r = client.post(
                "/api/inventory/unbound/import-multi",
                headers=_bearer(token),
                json={
                    "device_ids": ["dev-001", "dev-002"],
                    "subsystem": "unbound_hosts",
                    "csv_content": CSV_HOSTS,
                    "dry_run": True,
                },
            )
        assert r.status_code == 200
        data = r.json()
        assert data["failed_device_count"] == 1
        failed = next(r for r in data["results"] if r["device_id"] == "dev-001")
        assert "boom" in failed["error_summary"]
