"""Tests fuer Audit-CSV-Export + Verify-Endpoint (v5-Pass 1)."""

from __future__ import annotations

import csv
import io
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from opn_cockpit.audit.backend import reset_db_cache
from opn_cockpit.vault.model import VaultData, VaultDevice
from opn_cockpit.vault.store import create_vault
from opn_cockpit.web.server import create_app

VAULT_PASSWORD = "korrektes-pferd-batterie-heftklammer"


def _make_vault(tmp_path: Path) -> Path:
    path = tmp_path / "active.opnvault"
    create_vault(path, VAULT_PASSWORD, VaultData(devices=[
        VaultDevice(
            id="dev-001", name="HQ", host="opn.lab", port=443,
            tls_verify=True, tags=[], api_key="k", api_secret="s", descr="",
        ),
    ]))
    return path


@pytest.fixture()
def filesystem_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[TestClient, str]]:
    reset_db_cache()
    monkeypatch.setenv("OPNCOCKPIT_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("OPNCOCKPIT_STORAGE_BACKEND", raising=False)
    path = _make_vault(tmp_path)
    client = TestClient(create_app())
    unlock = client.post("/api/auth/unlock", json={
        "vault_path": str(path), "password": VAULT_PASSWORD,
    })
    token = unlock.json()["token"]
    yield client, token
    reset_db_cache()


@pytest.fixture()
def sqlite_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[TestClient, str]]:
    reset_db_cache()
    monkeypatch.setenv("OPNCOCKPIT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("OPNCOCKPIT_STORAGE_BACKEND", "sqlite")
    monkeypatch.delenv("OPNCOCKPIT_AUDIT_SECRET", raising=False)
    path = _make_vault(tmp_path)
    client = TestClient(create_app())
    unlock = client.post("/api/auth/unlock", json={
        "vault_path": str(path), "password": VAULT_PASSWORD,
    })
    token = unlock.json()["token"]
    yield client, token
    reset_db_cache()


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class TestCsvExport:
    def test_csv_has_header_and_rows(
        self, filesystem_client: tuple[TestClient, str],
    ) -> None:
        client, token = filesystem_client
        # Wenigstens einen Eintrag erzeugt durch unlock
        response = client.get("/api/audit/export.csv", headers=_h(token))
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/csv")
        assert "filename=\"opn-cockpit-audit.csv\"" in response.headers.get(
            "content-disposition", "",
        )
        reader = csv_reader(response.text)
        headers = next(reader)
        assert headers == [
            "timestamp_utc", "actor", "event", "summary", "action",
            "target_device_id", "target_device_name", "target_count",
            "status", "error_kind", "failed_phase", "duration_ms",
            "vault_path",
        ]
        rows = list(reader)
        assert any("vault_opened" in r for r in rows)

    def test_csv_filter_by_event(
        self, filesystem_client: tuple[TestClient, str], tmp_path: Path,
    ) -> None:
        client, token = filesystem_client
        # Generiere mehrere Events: erst Lock, dann Unlock erneut
        client.post("/api/auth/lock", headers=_h(token))
        unlock = client.post("/api/auth/unlock", json={
            "vault_path": str(tmp_path / "active.opnvault"),
            "password": VAULT_PASSWORD,
        })
        new_token = unlock.json()["token"]
        response = client.get(
            "/api/audit/export.csv?event=vault_locked",
            headers=_h(new_token),
        )
        assert response.status_code == 200
        rows = list(csv_reader(response.text))[1:]
        # Alle Zeilen muessen vault_locked sein
        for row in rows:
            assert row[2] == "vault_locked"

    def test_unauthorized_returns_401(
        self, filesystem_client: tuple[TestClient, str],
    ) -> None:
        client, _ = filesystem_client
        assert client.get("/api/audit/export.csv").status_code == 401


class TestVerifyChain:
    def test_filesystem_backend_returns_not_available(
        self, filesystem_client: tuple[TestClient, str],
    ) -> None:
        client, token = filesystem_client
        response = client.get("/api/audit/verify", headers=_h(token))
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "not-available"

    def test_sqlite_backend_returns_ok(
        self, sqlite_client: tuple[TestClient, str],
    ) -> None:
        client, token = sqlite_client
        # Mehrere Events erzeugen
        client.get("/api/inventory", headers=_h(token))
        response = client.get("/api/audit/verify", headers=_h(token))
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["total"] >= 1
        assert body["broken"] == []

    def test_sqlite_backend_detects_tampering(
        self, sqlite_client: tuple[TestClient, str], tmp_path: Path,
    ) -> None:
        client, token = sqlite_client
        # Eintraege erzeugen
        client.get("/api/inventory", headers=_h(token))
        # Direkt in der DB manipulieren
        conn = sqlite3.connect(tmp_path / "opn-cockpit.db")
        try:
            conn.execute("UPDATE audit SET summary = 'HACKED' WHERE id = 1")
            conn.commit()
        finally:
            conn.close()
        response = client.get("/api/audit/verify", headers=_h(token))
        body = response.json()
        assert body["status"] == "broken"
        assert body["broken"]


def csv_reader(text: str):
    return csv.reader(io.StringIO(text))
