"""Tests fuer Vault-Export-Endpunkte (Backup + Template)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from opn_cockpit.vault.model import VaultData, VaultDevice
from opn_cockpit.vault.store import create_vault, open_vault
from opn_cockpit.web.server import create_app

VAULT_PASSWORD = "korrektes-pferd-batterie-heftklammer"
TEMPLATE_PASSWORD = "template-passwort-mit-zeichen"


def _make_vault(tmp_path: Path) -> Path:
    path = tmp_path / "active.opnvault"
    create_vault(path, VAULT_PASSWORD, VaultData(devices=[
        VaultDevice(
            id="dev-001", name="HQ Berlin", host="opn.lab", port=443,
            tls_verify=True, tags=["core"],
            api_key="secret-key", api_secret="secret-secret",
            descr="primary",
        ),
        VaultDevice(
            id="dev-002", name="Branch Munich", host="opn2.lab", port=443,
            tls_verify=False, tags=["branches"],
            api_key="k2", api_secret="s2",
            descr="",
        ),
    ]))
    return path


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("OPNCOCKPIT_DATA_DIR", str(tmp_path))
    return TestClient(create_app())


@pytest.fixture()
def token(client: TestClient, tmp_path: Path) -> str:
    path = _make_vault(tmp_path)
    response = client.post("/api/auth/unlock", json={
        "vault_path": str(path), "password": VAULT_PASSWORD,
    })
    assert response.status_code == 200
    return str(response.json()["token"])


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class TestBackupExport:
    def test_returns_active_vault_bytes(
        self, client: TestClient, token: str, tmp_path: Path,
    ) -> None:
        response = client.get("/api/vaults/export/backup", headers=_h(token))
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/octet-stream"
        assert "attachment" in response.headers.get("content-disposition", "").lower()
        # Inhalt entspricht der Vault-Datei
        original_bytes = (tmp_path / "active.opnvault").read_bytes()
        assert response.content == original_bytes

    def test_backup_still_openable_with_master_password(
        self, client: TestClient, token: str, tmp_path: Path,
    ) -> None:
        """Backup ist als-ist verschluesselt — mit Master-PW oeffenbar."""
        response = client.get("/api/vaults/export/backup", headers=_h(token))
        assert response.status_code == 200
        backup_path = tmp_path / "downloaded-backup.opnvault"
        backup_path.write_bytes(response.content)
        opened = open_vault(backup_path, VAULT_PASSWORD)
        names = [d.name for d in opened.data.devices]
        assert "HQ Berlin" in names

    def test_unauthorized_returns_401(self, client: TestClient) -> None:
        assert client.get("/api/vaults/export/backup").status_code == 401


class TestTemplateExport:
    def test_template_has_blank_credentials(
        self, client: TestClient, token: str, tmp_path: Path,
    ) -> None:
        response = client.post(
            "/api/vaults/export/template",
            headers=_h(token),
            json={"template_password": TEMPLATE_PASSWORD},
        )
        assert response.status_code == 200
        # Datei runterspeichern + entschluesseln
        tpl_path = tmp_path / "template.opnvault"
        tpl_path.write_bytes(response.content)
        opened = open_vault(tpl_path, TEMPLATE_PASSWORD)
        # Geraete-Stammdaten erhalten, Credentials leer
        assert len(opened.data.devices) == 2
        for d in opened.data.devices:
            assert d.api_key == ""
            assert d.api_secret == ""
            # Aber Name/Host/Tags bleiben
            assert d.name
            assert d.host
        # Quelle bleibt unveraendert
        src = open_vault(tmp_path / "active.opnvault", VAULT_PASSWORD)
        assert src.data.devices[0].api_key == "secret-key"

    def test_template_filename_suffix(
        self, client: TestClient, token: str,
    ) -> None:
        response = client.post(
            "/api/vaults/export/template",
            headers=_h(token),
            json={"template_password": TEMPLATE_PASSWORD},
        )
        cd = response.headers.get("content-disposition", "")
        assert "template" in cd.lower()
        assert ".opnvault" in cd.lower()

    def test_short_password_rejected(
        self, client: TestClient, token: str,
    ) -> None:
        response = client.post(
            "/api/vaults/export/template",
            headers=_h(token),
            json={"template_password": "zu-kurz"},
        )
        assert response.status_code == 422

    def test_unauthorized_returns_401(self, client: TestClient) -> None:
        response = client.post(
            "/api/vaults/export/template",
            json={"template_password": TEMPLATE_PASSWORD},
        )
        assert response.status_code == 401
