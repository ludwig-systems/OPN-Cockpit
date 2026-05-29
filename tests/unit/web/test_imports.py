"""Tests fuer Bulk-Import von Firewall-Geraeten (CSV + JSON)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from opn_cockpit.vault.model import VaultData, VaultDevice
from opn_cockpit.vault.store import create_vault, open_vault
from opn_cockpit.web.server import create_app

PASSWORD = "korrektes-pferd-batterie-heftklammer"

DEVICES_CSV = """name,host,port,tls_verify,tags,descr,api_key,api_secret
HQ Berlin,opn-berlin.lab,443,true,branches;germany,HQ,KEY-B,SECRET-B
Branch Munich,opn-munich.lab,443,false,branches;germany,,KEY-M,SECRET-M
"""

DEVICES_CSV_BROKEN = """name,host,port,tls_verify,tags,descr,api_key,api_secret
,no-name,443,true,,,KEY,SECRET
With Name,,443,true,,,KEY,SECRET
HQ Stuttgart,opn-stuttgart.lab,99999,true,,,KEY,SECRET
"""

DEVICES_JSON = """[
  {"name": "HQ Berlin", "host": "opn-berlin.lab", "port": 443, "tls_verify": true,
   "tags": ["branches", "germany"], "api_key": "KEY-B", "api_secret": "SECRET-B"},
  {"name": "Branch Munich", "host": "opn-munich.lab", "tls_verify": false,
   "tags": ["branches"], "api_key": "KEY-M", "api_secret": "SECRET-M"}
]
"""


def _make_vault(tmp_path: Path, existing: list[VaultDevice] | None = None) -> Path:
    path = tmp_path / "test.opnvault"
    create_vault(path, PASSWORD, VaultData(devices=existing or []))
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


class TestImportDevicesCsv:
    def test_requires_auth(self, client: TestClient) -> None:
        response = client.post(
            "/api/imports/devices",
            files={"file": ("a.csv", DEVICES_CSV, "text/csv")},
            data={"format": "csv"},
        )
        assert response.status_code == 401

    def test_valid_csv_adds_devices(
        self,
        client: TestClient,
        vault: Path,
    ) -> None:
        token = _unlock(client, vault)
        response = client.post(
            "/api/imports/devices",
            files={"file": ("devices.csv", DEVICES_CSV, "text/csv")},
            data={"format": "csv"},
            headers=_bearer(token),
        )
        assert response.status_code == 201
        body = response.json()
        assert body["parsed_count"] == 2
        assert len(body["added"]) == 2
        assert body["skipped_existing"] == []
        names = {d["name"] for d in body["added"]}
        assert names == {"HQ Berlin", "Branch Munich"}
        # Auf Platte persistiert
        opened = open_vault(vault, PASSWORD)
        on_disk_names = {d.name for d in opened.data.devices}
        assert on_disk_names == {"HQ Berlin", "Branch Munich"}
        # Secrets sind drin
        berlin = next(d for d in opened.data.devices if d.name == "HQ Berlin")
        assert berlin.api_key == "KEY-B"
        assert berlin.api_secret == "SECRET-B"
        assert berlin.tags == ["branches", "germany"]

    def test_broken_csv_returns_400_with_errors(
        self,
        client: TestClient,
        vault: Path,
    ) -> None:
        token = _unlock(client, vault)
        response = client.post(
            "/api/imports/devices",
            files={"file": ("bad.csv", DEVICES_CSV_BROKEN, "text/csv")},
            data={"format": "csv"},
            headers=_bearer(token),
        )
        assert response.status_code == 400
        detail = response.json()["detail"]
        assert "errors" in detail
        assert len(detail["errors"]) >= 1

    def test_skip_existing_by_name(
        self,
        client: TestClient,
        tmp_path: Path,
    ) -> None:
        existing = [VaultDevice(
            id="dev-001", name="HQ Berlin", host="other.host", port=443,
            tls_verify=True, tags=["existing"],
            api_key="OLD", api_secret="OLD", descr="",
        )]
        vault = _make_vault(tmp_path, existing=existing)
        token = _unlock(client, vault)
        response = client.post(
            "/api/imports/devices",
            files={"file": ("d.csv", DEVICES_CSV, "text/csv")},
            data={"format": "csv"},
            headers=_bearer(token),
        )
        assert response.status_code == 201
        body = response.json()
        assert "HQ Berlin" in body["skipped_existing"]
        added = {d["name"] for d in body["added"]}
        assert added == {"Branch Munich"}
        # Auf Platte: HQ Berlin unveraendert
        opened = open_vault(vault, PASSWORD)
        berlin = next(d for d in opened.data.devices if d.name == "HQ Berlin")
        assert berlin.host == "other.host"
        assert berlin.api_key == "OLD"


class TestImportDevicesJson:
    def test_valid_json_adds_devices(
        self,
        client: TestClient,
        vault: Path,
    ) -> None:
        token = _unlock(client, vault)
        response = client.post(
            "/api/imports/devices",
            files={"file": ("d.json", DEVICES_JSON, "application/json")},
            data={"format": "json"},
            headers=_bearer(token),
        )
        assert response.status_code == 201
        body = response.json()
        assert body["parsed_count"] == 2
        assert len(body["added"]) == 2

    def test_broken_json_returns_400(
        self,
        client: TestClient,
        vault: Path,
    ) -> None:
        token = _unlock(client, vault)
        response = client.post(
            "/api/imports/devices",
            files={"file": ("bad.json", "not json", "application/json")},
            data={"format": "json"},
            headers=_bearer(token),
        )
        assert response.status_code == 400


class TestImportDevicesValidation:
    def test_unknown_format_400(
        self,
        client: TestClient,
        vault: Path,
    ) -> None:
        token = _unlock(client, vault)
        response = client.post(
            "/api/imports/devices",
            files={"file": ("a.txt", "hi", "text/plain")},
            data={"format": "xml"},
            headers=_bearer(token),
        )
        assert response.status_code == 400
