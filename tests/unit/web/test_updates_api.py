"""Tests fuer den /api/updates/check-Endpunkt."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from opn_cockpit.web.server import create_app


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("OPNCOCKPIT_DATA_DIR", str(tmp_path))
    # Update-Check via Env deaktivieren -> Endpoint liefert "disabled".
    # Damit testen wir das Routing ohne echten GitHub-Aufruf.
    monkeypatch.setenv("OPNCOCKPIT_UPDATE_CHECK_ENABLED", "0")
    return TestClient(create_app())


class TestUpdateCheckEndpoint:
    def test_endpoint_is_anonymous(self, client: TestClient) -> None:
        response = client.get("/api/updates/check")
        assert response.status_code == 200

    def test_disabled_state_serialized(self, client: TestClient) -> None:
        response = client.get("/api/updates/check")
        body = response.json()
        assert body["status"] == "disabled"
        assert body["update_available"] is False
        assert body["current_version"]

    def test_force_param_accepted(self, client: TestClient) -> None:
        response = client.get("/api/updates/check?force=true")
        assert response.status_code == 200
