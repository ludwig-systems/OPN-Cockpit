"""Tests fuer die Vault-Discovery + Create-Routen."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from opn_cockpit.vault.discovery import VAULT_EXTENSION
from opn_cockpit.vault.store import create_vault, open_vault
from opn_cockpit.web.server import create_app

PASSWORD = "korrektes-pferd-batterie-heftklammer"


@pytest.fixture()
def client() -> TestClient:
    return TestClient(create_app())


@pytest.fixture()
def app_data_dir(tmp_path: Path) -> Path:
    return tmp_path / "appdata"


# ---------------------------------------------------------------------------
# GET /api/vaults
# ---------------------------------------------------------------------------


class TestListVaults:
    def test_empty_when_nothing_exists(
        self,
        client: TestClient,
        app_data_dir: Path,
    ) -> None:
        with patch.dict(os.environ, {"APPDATA": str(app_data_dir)}):
            response = client.get("/api/vaults")
        assert response.status_code == 200
        body = response.json()
        assert body["vaults"] == []
        assert body["suggested_new_path"].endswith(VAULT_EXTENSION)

    def test_lists_existing_vaults(
        self,
        client: TestClient,
        app_data_dir: Path,
    ) -> None:
        with patch.dict(os.environ, {"APPDATA": str(app_data_dir)}):
            vdir = app_data_dir / "OPN-Cockpit"
            vdir.mkdir(parents=True)
            create_vault(vdir / f"alpha{VAULT_EXTENSION}", PASSWORD)
            create_vault(vdir / f"beta{VAULT_EXTENSION}", PASSWORD)
            response = client.get("/api/vaults")
        assert response.status_code == 200
        names = {v["filename"] for v in response.json()["vaults"]}
        assert names == {f"alpha{VAULT_EXTENSION}", f"beta{VAULT_EXTENSION}"}


# ---------------------------------------------------------------------------
# POST /api/vaults
# ---------------------------------------------------------------------------


class TestCreateVault:
    def test_creates_and_returns_token(
        self,
        client: TestClient,
        tmp_path: Path,
    ) -> None:
        target = tmp_path / "neu.opnvault"
        with patch.dict(os.environ, {"APPDATA": str(tmp_path / "appdata")}):
            response = client.post(
                "/api/vaults",
                json={"path": str(target), "password": PASSWORD},
            )
        assert response.status_code == 201
        body = response.json()
        assert body["vault_filename"] == "neu.opnvault"
        assert body["token"]
        # Tresor auf Platte angelegt
        assert target.exists()
        # Mit selbem Passwort entsperrbar
        open_vault(target, PASSWORD)

    def test_auto_unlock_token_works(
        self,
        client: TestClient,
        tmp_path: Path,
    ) -> None:
        target = tmp_path / "neu.opnvault"
        with patch.dict(os.environ, {"APPDATA": str(tmp_path / "appdata")}):
            create_resp = client.post(
                "/api/vaults",
                json={"path": str(target), "password": PASSWORD},
            )
        token = create_resp.json()["token"]
        me = client.get(
            "/api/auth/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert me.status_code == 200
        assert me.json()["vault_filename"] == "neu.opnvault"

    def test_refuses_to_overwrite_existing(
        self,
        client: TestClient,
        tmp_path: Path,
    ) -> None:
        target = tmp_path / "exists.opnvault"
        create_vault(target, PASSWORD)
        with patch.dict(os.environ, {"APPDATA": str(tmp_path / "appdata")}):
            response = client.post(
                "/api/vaults",
                json={"path": str(target), "password": PASSWORD},
            )
        assert response.status_code == 409

    def test_weak_password_returns_400(
        self,
        client: TestClient,
        tmp_path: Path,
    ) -> None:
        target = tmp_path / "neu.opnvault"
        with patch.dict(os.environ, {"APPDATA": str(tmp_path / "appdata")}):
            response = client.post(
                "/api/vaults",
                json={"path": str(target), "password": "kurz"},
            )
        assert response.status_code == 400


class TestVaultSettingsAndChangePassword:
    """F5a + F5b + R3-Followup: Inaktivity-Timeout aenderbar, Vault-Master-PW
    aenderbar — und der frueher 500-werfende persist_session_vault-Aufruf
    bleibt verifiziert OK."""

    def _create_and_unlock(self, client: TestClient, tmp_path: Path) -> str:
        target = tmp_path / "v.opnvault"
        with patch.dict(os.environ, {"APPDATA": str(tmp_path / "appdata")}):
            r = client.post(
                "/api/vaults",
                json={"path": str(target), "password": PASSWORD},
            )
        return r.json()["token"]

    def test_get_settings_returns_current_inactivity_minutes(
        self, client: TestClient, tmp_path: Path,
    ) -> None:
        token = self._create_and_unlock(client, tmp_path)
        r = client.get(
            "/api/vaults/settings",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["inactivity_minutes"] == 10
        assert "max_workers" in body

    def test_update_settings_persists_and_returns_new_value(
        self, client: TestClient, tmp_path: Path,
    ) -> None:
        """Regression: vorher 500 wegen falscher persist_session_vault-Signatur."""
        token = self._create_and_unlock(client, tmp_path)
        r = client.post(
            "/api/vaults/settings",
            json={"inactivity_minutes": 45},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["inactivity_minutes"] == 45
        # Re-Read und verifiziere dass es persistent ist
        r2 = client.get(
            "/api/vaults/settings",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r2.json()["inactivity_minutes"] == 45

    def test_update_settings_validates_range(
        self, client: TestClient, tmp_path: Path,
    ) -> None:
        token = self._create_and_unlock(client, tmp_path)
        r = client.post(
            "/api/vaults/settings",
            json={"inactivity_minutes": 999},
            headers={"Authorization": f"Bearer {token}"},
        )
        # Pydantic-Validation -> 422
        assert r.status_code in {400, 422}

    def test_change_password_succeeds_and_session_keeps_working(
        self, client: TestClient, tmp_path: Path,
    ) -> None:
        token = self._create_and_unlock(client, tmp_path)
        new_pw = "neues-passwort-mindestens-12-zeichen"
        r = client.post(
            "/api/vaults/change-password",
            json={
                "current_password": PASSWORD,
                "new_password": new_pw,
                "new_password_repeat": new_pw,
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200, r.text
        # Token muss weiter funktionieren (session.unlock mit neuem PW lief)
        me = client.get(
            "/api/auth/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert me.status_code == 200

    def test_change_password_rejects_wrong_current(
        self, client: TestClient, tmp_path: Path,
    ) -> None:
        token = self._create_and_unlock(client, tmp_path)
        r = client.post(
            "/api/vaults/change-password",
            json={
                "current_password": "ist-bestimmt-nicht-richtig-mindestens-12",
                "new_password": "neues-passwort-mindestens-12-zeichen",
                "new_password_repeat": "neues-passwort-mindestens-12-zeichen",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 401

    def test_change_password_rejects_mismatch(
        self, client: TestClient, tmp_path: Path,
    ) -> None:
        token = self._create_and_unlock(client, tmp_path)
        r = client.post(
            "/api/vaults/change-password",
            json={
                "current_password": PASSWORD,
                "new_password": "neues-passwort-mindestens-12-zeichen",
                "new_password_repeat": "etwas-ganz-anderes-mindestens-12",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 400
