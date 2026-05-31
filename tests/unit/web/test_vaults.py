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
