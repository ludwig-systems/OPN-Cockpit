"""Tests fuer Inventory-ACL (allowed_tags) + Role-Gating (v3.0 Iter 4).

Strategie: Multi-User-Setup mit drei Usern (admin, operator, viewer) und
einem operator mit allowed_tags=["germany"]. Tests pruefen, dass die
ACL-Regeln in jeder relevanten Route durchschlagen.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from opn_cockpit.config import AppSettings
from opn_cockpit.vault.model import VaultData, VaultDevice
from opn_cockpit.vault.store import create_vault
from opn_cockpit.web.server import create_app
from opn_cockpit.web.server_state import VAULT_PATH_ENV, ServerState

VAULT_PASSWORD = "korrektes-pferd-batterie-heftklammer"
ADMIN_PASSWORD = "admin-passwort-mit-genug-zeichen"
USER_PASSWORD = "user-passwort-mit-genug-zeichen"


def _make_vault(tmp_path: Path) -> Path:
    path = tmp_path / "shared.opnvault"
    # 3 Devices mit unterschiedlichen Tags
    devices = [
        VaultDevice(
            id="dev-de-1", name="HQ Berlin", host="opn-de.lab", port=443,
            tls_verify=True, tags=["germany", "core"],
            api_key="k", api_secret="s", descr="",
        ),
        VaultDevice(
            id="dev-at-1", name="Wien Office", host="opn-at.lab", port=443,
            tls_verify=True, tags=["austria", "branches"],
            api_key="k", api_secret="s", descr="",
        ),
        VaultDevice(
            id="dev-de-2", name="Branch Munich", host="opn-de2.lab", port=443,
            tls_verify=True, tags=["germany", "branches"],
            api_key="k", api_secret="s", descr="",
        ),
    ]
    create_vault(path, VAULT_PASSWORD, VaultData(devices=devices))
    return path


@pytest.fixture()
def multi_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("OPNCOCKPIT_DATA_DIR", str(data))
    monkeypatch.delenv(VAULT_PATH_ENV, raising=False)
    vault_path = _make_vault(tmp_path)
    AppSettings(
        auth_backend="user-db",
        deployment_mode="multi-server",
        default_vault=str(vault_path),
    ).save(data / "settings.json")
    client = TestClient(create_app())
    server: ServerState = client.app.state.server_state
    server.bootstrap_create_admin("alice", ADMIN_PASSWORD)
    server.bootstrap_unlock_vault(vault_path, VAULT_PASSWORD)
    # 3 Test-User: bob (operator, alle Tags), carla (operator, nur germany),
    # dave (viewer, alle Tags)
    assert server.user_store is not None
    server.user_store.create_user(
        username="bob", password=USER_PASSWORD, role="operator",
    )
    server.user_store.create_user(
        username="carla", password=USER_PASSWORD, role="operator",
        allowed_tags=("germany",),
    )
    server.user_store.create_user(
        username="dave", password=USER_PASSWORD, role="viewer",
    )
    yield client


def _login(client: TestClient, username: str, password: str) -> str:
    response = client.post("/api/auth/login", json={
        "username": username, "password": password,
    })
    assert response.status_code == 200, response.text
    return str(response.json()["token"])


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Inventar-Sichtbarkeit
# ---------------------------------------------------------------------------


class TestInventoryVisibility:
    def test_admin_sees_all(self, multi_client: TestClient) -> None:
        t = _login(multi_client, "alice", ADMIN_PASSWORD)
        body = multi_client.get("/api/inventory", headers=_h(t)).json()
        names = sorted(d["name"] for d in body["devices"])
        assert names == ["Branch Munich", "HQ Berlin", "Wien Office"]

    def test_operator_without_tags_sees_all(
        self, multi_client: TestClient,
    ) -> None:
        t = _login(multi_client, "bob", USER_PASSWORD)
        body = multi_client.get("/api/inventory", headers=_h(t)).json()
        assert len(body["devices"]) == 3

    def test_operator_with_tag_filter(
        self, multi_client: TestClient,
    ) -> None:
        t = _login(multi_client, "carla", USER_PASSWORD)
        body = multi_client.get("/api/inventory", headers=_h(t)).json()
        names = sorted(d["name"] for d in body["devices"])
        # Nur germany-Tag → HQ Berlin + Branch Munich
        assert names == ["Branch Munich", "HQ Berlin"]
        # Tag-Summary auch nur fuer sichtbare Geraete
        tag_names = {t["name"] for t in body["tags"]}
        assert "austria" not in tag_names

    def test_viewer_sees_all_devices_but_no_writes(
        self, multi_client: TestClient,
    ) -> None:
        t = _login(multi_client, "dave", USER_PASSWORD)
        body = multi_client.get("/api/inventory", headers=_h(t)).json()
        assert len(body["devices"]) == 3


# ---------------------------------------------------------------------------
# Schreib-Routen: Role-Gating
# ---------------------------------------------------------------------------


class TestWriteGating:
    def test_viewer_cannot_create_device(self, multi_client: TestClient) -> None:
        t = _login(multi_client, "dave", USER_PASSWORD)
        response = multi_client.post(
            "/api/inventory/devices", headers=_h(t),
            json={
                "name": "X", "host": "x.lab", "port": 443,
                "tls_verify": True, "tags": [], "descr": "",
                "api_key": "k", "api_secret": "s",
            },
        )
        assert response.status_code == 403

    def test_operator_can_create_device(self, multi_client: TestClient) -> None:
        t = _login(multi_client, "bob", USER_PASSWORD)
        response = multi_client.post(
            "/api/inventory/devices", headers=_h(t),
            json={
                "name": "X", "host": "x.lab", "port": 443,
                "tls_verify": True, "tags": [], "descr": "",
                "api_key": "k", "api_secret": "s",
            },
        )
        assert response.status_code == 201

    def test_carla_cannot_modify_austria_device(
        self, multi_client: TestClient,
    ) -> None:
        t = _login(multi_client, "carla", USER_PASSWORD)
        response = multi_client.patch(
            "/api/inventory/devices/dev-at-1",
            headers=_h(t),
            json={"descr": "noch nicht freigegeben"},
        )
        assert response.status_code == 404

    def test_carla_can_modify_germany_device(
        self, multi_client: TestClient,
    ) -> None:
        t = _login(multi_client, "carla", USER_PASSWORD)
        response = multi_client.patch(
            "/api/inventory/devices/dev-de-1",
            headers=_h(t),
            json={"descr": "ok"},
        )
        assert response.status_code == 200

    def test_carla_cannot_delete_austria_device(
        self, multi_client: TestClient,
    ) -> None:
        t = _login(multi_client, "carla", USER_PASSWORD)
        response = multi_client.delete(
            "/api/inventory/devices/dev-at-1", headers=_h(t),
        )
        assert response.status_code == 404

    def test_viewer_cannot_delete_device(self, multi_client: TestClient) -> None:
        t = _login(multi_client, "dave", USER_PASSWORD)
        response = multi_client.delete(
            "/api/inventory/devices/dev-de-1", headers=_h(t),
        )
        assert response.status_code == 403


# ---------------------------------------------------------------------------
# Plans: Role + Tag-Whitelist
# ---------------------------------------------------------------------------


class TestPlanAcl:
    def test_viewer_cannot_create_plan(self, multi_client: TestClient) -> None:
        t = _login(multi_client, "dave", USER_PASSWORD)
        response = multi_client.post(
            "/api/plans/route", headers=_h(t),
            json={
                "network": "10.99.0.0/24", "gateway": "WAN_GW",
                "descr": "x", "disabled": False,
                "target_device_ids": ["dev-de-1"],
            },
        )
        assert response.status_code == 403

    def test_carla_cannot_target_austria_device(
        self, multi_client: TestClient,
    ) -> None:
        t = _login(multi_client, "carla", USER_PASSWORD)
        response = multi_client.post(
            "/api/plans/route", headers=_h(t),
            json={
                "network": "10.99.0.0/24", "gateway": "WAN_GW",
                "descr": "x", "disabled": False,
                "target_device_ids": ["dev-de-1", "dev-at-1"],
            },
        )
        assert response.status_code == 404

    def test_carla_can_target_germany_only(
        self, multi_client: TestClient,
    ) -> None:
        t = _login(multi_client, "carla", USER_PASSWORD)
        response = multi_client.post(
            "/api/plans/route", headers=_h(t),
            json={
                "network": "10.99.0.0/24", "gateway": "WAN_GW",
                "descr": "x", "disabled": False,
                "target_device_ids": ["dev-de-1", "dev-de-2"],
            },
        )
        assert response.status_code == 201

    def test_plan_list_filtered(self, multi_client: TestClient) -> None:
        """Bob legt Plan mit austria-Device an, carla sieht ihn nicht."""
        bob = _login(multi_client, "bob", USER_PASSWORD)
        plan = multi_client.post(
            "/api/plans/route", headers=_h(bob),
            json={
                "network": "10.99.0.0/24", "gateway": "WAN_GW",
                "descr": "x", "disabled": False,
                "target_device_ids": ["dev-at-1"],
            },
        ).json()
        plan_id = plan["plan_id"]

        carla = _login(multi_client, "carla", USER_PASSWORD)
        body = multi_client.get("/api/plans", headers=_h(carla)).json()
        plan_ids = [p["plan_id"] for p in body["plans"]]
        assert plan_id not in plan_ids

        # get_plan auch verboten
        response = multi_client.get(f"/api/plans/{plan_id}", headers=_h(carla))
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# Discover / Test-Connection / Retry
# ---------------------------------------------------------------------------


class TestDiscoverAcl:
    def test_carla_cannot_discover_austria_device(
        self, multi_client: TestClient,
    ) -> None:
        t = _login(multi_client, "carla", USER_PASSWORD)
        response = multi_client.get(
            "/api/discover/devices/dev-at-1/gateways", headers=_h(t),
        )
        assert response.status_code == 404

    def test_carla_cannot_test_connection_on_austria(
        self, multi_client: TestClient,
    ) -> None:
        t = _login(multi_client, "carla", USER_PASSWORD)
        response = multi_client.post(
            "/api/inventory/devices/dev-at-1/test-connection", headers=_h(t),
        )
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# Single-User-Mode: ACL inaktiv
# ---------------------------------------------------------------------------


class TestSingleUserUnaffected:
    def test_single_user_can_do_everything(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        data = tmp_path / "data"
        data.mkdir()
        monkeypatch.setenv("OPNCOCKPIT_DATA_DIR", str(data))
        vault_path = _make_vault(tmp_path)
        client = TestClient(create_app())
        unlock = client.post("/api/auth/unlock", json={
            "vault_path": str(vault_path), "password": VAULT_PASSWORD,
        })
        token = unlock.json()["token"]
        # alles erlaubt — keine ACL
        inv = client.get("/api/inventory", headers=_h(token)).json()
        assert len(inv["devices"]) == 3
        add = client.post(
            "/api/inventory/devices", headers=_h(token),
            json={
                "name": "Solo", "host": "s.lab", "port": 443,
                "tls_verify": True, "tags": [], "descr": "",
                "api_key": "k", "api_secret": "s",
            },
        )
        assert add.status_code == 201
