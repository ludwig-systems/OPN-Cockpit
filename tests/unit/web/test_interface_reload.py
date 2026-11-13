"""Tests fuer POST /api/inventory/devices/{id}/interfaces/{identifier}/reload.

Der Endpoint ruft OPNsense's reloadInterface-Endpoint. Wir mocken den
HttpClient in inventory.py auf Ebene der class call() — damit umgehen
wir den ganzen httpx-Stack und testen die Aggregations-/Fehlerbehandlungs-
Logik direkt.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from opn_cockpit.core.errors import ErrorContext, OpnCockpitError
from opn_cockpit.vault.model import VaultData, VaultDevice
from opn_cockpit.vault.store import create_vault
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


class _FakeClientContext:
    """Mimikt HttpClient als Context-Manager. call() wird pro Test gesetzt."""

    def __init__(self, call_fn):
        self.call = call_fn

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class TestReloadEndpoint:
    def test_requires_auth(self, client: TestClient) -> None:
        r = client.post(
            "/api/inventory/devices/dev-001/interfaces/wan/reload",
        )
        assert r.status_code == 401

    def test_missing_device_returns_404(
        self,
        client: TestClient,
        vault: Path,
    ) -> None:
        token = _unlock(client, vault)
        r = client.post(
            "/api/inventory/devices/does-not-exist/interfaces/wan/reload",
            headers=_bearer(token),
        )
        assert r.status_code == 404

    @pytest.mark.parametrize(
        "bad_id",
        [
            "wan/../etc/passwd",
            "wan;whoami",
            "wan wan",
            "",
        ],
    )
    def test_path_injection_blocked(
        self,
        client: TestClient,
        vault: Path,
        bad_id: str,
    ) -> None:
        token = _unlock(client, vault)
        r = client.post(
            f"/api/inventory/devices/dev-001/interfaces/{bad_id}/reload",
            headers=_bearer(token),
        )
        # 400 vom Regex-Filter ODER 404 vom Router (leerer Path).
        assert r.status_code in (400, 404, 405)

    def test_successful_reload(
        self,
        client: TestClient,
        vault: Path,
    ) -> None:
        token = _unlock(client, vault)

        called_endpoints: list[str] = []

        def fake_call(target, key, secret, method, endpoint, **kwargs):
            called_endpoints.append(endpoint)
            resp = MagicMock()
            resp.json.return_value = {"message": "Interface WAN reloaded."}
            return resp

        with patch(
            "opn_cockpit.web.api.inventory.HttpClient",
            return_value=_FakeClientContext(fake_call),
        ):
            r = client.post(
                "/api/inventory/devices/dev-001/interfaces/wan/reload",
                headers=_bearer(token),
            )
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert data["identifier"] == "wan"
        assert "reloaded" in data["message"].lower()
        assert called_endpoints == [
            "/api/interfaces/overview/reloadInterface/wan",
        ]

    def test_opnsense_error_returns_ok_false(
        self,
        client: TestClient,
        vault: Path,
    ) -> None:
        token = _unlock(client, vault)

        def fake_call(*args, **kwargs):
            ctx = ErrorContext(
                error_kind="opnsense_error",
                summary="Interface nicht gefunden",
            )
            raise OpnCockpitError("boom", context=ctx)

        with patch(
            "opn_cockpit.web.api.inventory.HttpClient",
            return_value=_FakeClientContext(fake_call),
        ):
            r = client.post(
                "/api/inventory/devices/dev-001/interfaces/opt99/reload",
                headers=_bearer(token),
            )
        # HTTP 200, aber ok=False im Body — der Endpoint dreht Failures
        # bewusst nicht in HTTP-Errors um, damit das UI eine klare
        # per-Interface-Meldung hat.
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is False
        assert "nicht gefunden" in data["message"]
