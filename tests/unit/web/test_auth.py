"""End-to-End-Tests fuer die Auth-Routen (Unlock + Lock + Me)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from opn_cockpit.vault.model import VaultData, VaultDevice
from opn_cockpit.vault.store import create_vault
from opn_cockpit.web.server import create_app

PASSWORD = "korrektes-pferd-batterie-heftklammer"


def _make_vault(tmp_path: Path, devices: list[VaultDevice] | None = None) -> Path:
    path = tmp_path / "test.opnvault"
    create_vault(path, PASSWORD, VaultData(devices=devices or []))
    return path


@pytest.fixture()
def client() -> TestClient:
    return TestClient(create_app())


@pytest.fixture()
def vault_path(tmp_path: Path) -> Path:
    return _make_vault(tmp_path)


# ---------------------------------------------------------------------------
# Unlock
# ---------------------------------------------------------------------------


class TestUnlock:
    def test_returns_token_on_correct_password(
        self,
        client: TestClient,
        vault_path: Path,
    ) -> None:
        response = client.post(
            "/api/auth/unlock",
            json={"vault_path": str(vault_path), "password": PASSWORD},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["token"]
        assert body["vault_path"] == str(vault_path)
        assert body["vault_filename"] == "test.opnvault"
        assert body["inactivity_timeout_s"] > 0
        assert body["seconds_until_expiry"] > 0

    def test_wrong_password_returns_401(
        self,
        client: TestClient,
        vault_path: Path,
    ) -> None:
        response = client.post(
            "/api/auth/unlock",
            json={"vault_path": str(vault_path), "password": "falsches-passwort-12+"},
        )
        assert response.status_code == 401
        assert "Master-Passwort" in response.json()["detail"]

    def test_missing_vault_returns_404(self, client: TestClient, tmp_path: Path) -> None:
        response = client.post(
            "/api/auth/unlock",
            json={
                "vault_path": str(tmp_path / "no-such.opnvault"),
                "password": PASSWORD,
            },
        )
        assert response.status_code == 404

    def test_empty_body_returns_422(self, client: TestClient) -> None:
        response = client.post("/api/auth/unlock", json={})
        assert response.status_code == 422


# ---------------------------------------------------------------------------
# Me + Lock
# ---------------------------------------------------------------------------


def _unlock(client: TestClient, vault_path: Path) -> str:
    response = client.post(
        "/api/auth/unlock",
        json={"vault_path": str(vault_path), "password": PASSWORD},
    )
    assert response.status_code == 200
    return str(response.json()["token"])


class TestMe:
    def test_returns_current_session(
        self,
        client: TestClient,
        vault_path: Path,
    ) -> None:
        token = _unlock(client, vault_path)
        response = client.get(
            "/api/auth/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["vault_filename"] == "test.opnvault"

    def test_missing_token_returns_401(self, client: TestClient) -> None:
        response = client.get("/api/auth/me")
        assert response.status_code == 401
        assert response.headers.get("WWW-Authenticate") == "Bearer"

    def test_invalid_scheme_returns_401(self, client: TestClient) -> None:
        response = client.get(
            "/api/auth/me",
            headers={"Authorization": "Basic abc"},
        )
        assert response.status_code == 401

    def test_unknown_token_returns_401(self, client: TestClient) -> None:
        response = client.get(
            "/api/auth/me",
            headers={"Authorization": "Bearer not-a-real-token"},
        )
        assert response.status_code == 401


class TestLock:
    def test_locks_session_and_revokes_token(
        self,
        client: TestClient,
        vault_path: Path,
    ) -> None:
        token = _unlock(client, vault_path)
        # Token funktioniert
        assert client.get(
            "/api/auth/me", headers={"Authorization": f"Bearer {token}"}
        ).status_code == 200
        # Lock
        response = client.post(
            "/api/auth/lock",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 204
        # Danach 401
        assert client.get(
            "/api/auth/me", headers={"Authorization": f"Bearer {token}"}
        ).status_code == 401

    def test_lock_without_token_is_401(self, client: TestClient) -> None:
        assert client.post("/api/auth/lock").status_code == 401


class TestTokenIsolation:
    def test_each_unlock_gets_unique_token(
        self,
        client: TestClient,
        vault_path: Path,
    ) -> None:
        token_a = _unlock(client, vault_path)
        token_b = _unlock(client, vault_path)
        assert token_a != token_b
        # Beide funktionieren
        for t in (token_a, token_b):
            assert client.get(
                "/api/auth/me", headers={"Authorization": f"Bearer {t}"}
            ).status_code == 200

    def test_revoking_one_does_not_affect_other(
        self,
        client: TestClient,
        vault_path: Path,
    ) -> None:
        token_a = _unlock(client, vault_path)
        token_b = _unlock(client, vault_path)
        client.post(
            "/api/auth/lock",
            headers={"Authorization": f"Bearer {token_a}"},
        )
        assert client.get(
            "/api/auth/me", headers={"Authorization": f"Bearer {token_b}"}
        ).status_code == 200
