"""Tests fuer die Audit-Routen (Web-Sicht auf das append-only JSON-Lines-Log)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from opn_cockpit.vault.model import VaultData, VaultDevice
from opn_cockpit.vault.store import create_vault
from opn_cockpit.web.server import create_app

PASSWORD = "korrektes-pferd-batterie-heftklammer"


def _make_vault(tmp_path: Path) -> Path:
    path = tmp_path / "test.opnvault"
    create_vault(path, PASSWORD, VaultData(devices=[
        VaultDevice(
            id="dev-001", name="Box 1", host="opn-1.lab", port=443,
            tls_verify=True, tags=["test"], api_key="k", api_secret="s", descr="",
        ),
    ]))
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
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    return TestClient(create_app())


@pytest.fixture()
def vault(tmp_path: Path) -> Path:
    return _make_vault(tmp_path)


# ---------------------------------------------------------------------------
# GET /api/audit/events
# ---------------------------------------------------------------------------


class TestAuditEvents:
    def test_requires_auth(self, client: TestClient) -> None:
        assert client.get("/api/audit/events").status_code == 401

    def test_returns_event_kinds(
        self,
        client: TestClient,
        vault: Path,
    ) -> None:
        token = _unlock(client, vault)
        response = client.get("/api/audit/events", headers=_bearer(token))
        assert response.status_code == 200
        events = response.json()
        assert "plan_generated" in events
        assert "vault_opened" in events


# ---------------------------------------------------------------------------
# GET /api/audit
# ---------------------------------------------------------------------------


class TestAuditList:
    def test_requires_auth(self, client: TestClient) -> None:
        assert client.get("/api/audit").status_code == 401

    def test_returns_at_least_unlock_event(
        self,
        client: TestClient,
        vault: Path,
    ) -> None:
        token = _unlock(client, vault)
        response = client.get("/api/audit", headers=_bearer(token))
        assert response.status_code == 200
        body = response.json()
        # Mindestens der Unlock-Event ist drin
        events = {e["event"] for e in body["entries"]}
        assert "vault_opened" in events

    def test_filter_by_event_kind(
        self,
        client: TestClient,
        vault: Path,
    ) -> None:
        token = _unlock(client, vault)
        response = client.get(
            "/api/audit?event=vault_opened",
            headers=_bearer(token),
        )
        assert response.status_code == 200
        for entry in response.json()["entries"]:
            assert entry["event"] == "vault_opened"

    def test_unknown_event_returns_400(
        self,
        client: TestClient,
        vault: Path,
    ) -> None:
        token = _unlock(client, vault)
        response = client.get(
            "/api/audit?event=not_a_real_event",
            headers=_bearer(token),
        )
        assert response.status_code == 400

    def test_limit_caps_results(
        self,
        client: TestClient,
        vault: Path,
    ) -> None:
        token = _unlock(client, vault)
        # Mehrere Unlocks erzeugen mehr Events
        for _ in range(3):
            _unlock(client, vault)
        response = client.get("/api/audit?limit=2", headers=_bearer(token))
        assert response.status_code == 200
        body = response.json()
        assert len(body["entries"]) <= 2
        assert body["total"] >= 2

    def test_invalid_limit_422(
        self,
        client: TestClient,
        vault: Path,
    ) -> None:
        token = _unlock(client, vault)
        response = client.get("/api/audit?limit=0", headers=_bearer(token))
        assert response.status_code == 422

    def test_newest_first_ordering(
        self,
        client: TestClient,
        vault: Path,
    ) -> None:
        token = _unlock(client, vault)
        for _ in range(2):
            _unlock(client, vault)
        body = client.get("/api/audit", headers=_bearer(token)).json()
        # Mindestens 3 Events, neueste zuerst -> erster timestamp >= letzter
        if len(body["entries"]) >= 2:
            first_ts = body["entries"][0]["timestamp_utc"]
            last_ts = body["entries"][-1]["timestamp_utc"]
            assert first_ts >= last_ts
