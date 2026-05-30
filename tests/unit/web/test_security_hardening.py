"""Tests fuer die Security-Audit-Fixes (Rate-Limit, Bootstrap-Token,
Security-Headers, Path-Validierung).

Strategie: TestClient, dann konkrete Fehler-Pfade gegen die Endpunkte
schleudern und Status-Codes pruefen.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from opn_cockpit.config import AppSettings
from opn_cockpit.vault.model import VaultData, VaultDevice
from opn_cockpit.vault.store import create_vault
from opn_cockpit.web.rate_limit import RateLimiter
from opn_cockpit.web.server import create_app
from opn_cockpit.web.server_state import VAULT_PATH_ENV, ServerState

VAULT_PASSWORD = "korrektes-pferd-batterie-heftklammer"


def _make_vault(tmp_path: Path) -> Path:
    path = tmp_path / "shared.opnvault"
    create_vault(path, VAULT_PASSWORD, VaultData(devices=[
        VaultDevice(
            id="dev-001", name="HQ", host="opn.lab", port=443,
            tls_verify=True, tags=[], api_key="k", api_secret="s", descr="",
        ),
    ]))
    return path


# ---------------------------------------------------------------------------
# Security-Headers (Audit #6)
# ---------------------------------------------------------------------------


class TestSecurityHeaders:
    def test_headers_on_html_response(self, tmp_path: Path) -> None:
        client = TestClient(create_app())
        response = client.get("/")
        assert response.headers.get("X-Content-Type-Options") == "nosniff"
        assert response.headers.get("X-Frame-Options") == "DENY"
        assert response.headers.get("Referrer-Policy") == "same-origin"
        assert "default-src 'self'" in response.headers.get("Content-Security-Policy", "")

    def test_headers_on_api_response(self, tmp_path: Path) -> None:
        client = TestClient(create_app())
        response = client.get("/api/version")
        assert response.headers.get("X-Content-Type-Options") == "nosniff"
        assert response.headers.get("X-Frame-Options") == "DENY"


# ---------------------------------------------------------------------------
# Path-Validierung (Audit #14)
# ---------------------------------------------------------------------------


class TestVaultPathValidation:
    def test_invalid_suffix_rejected(self, tmp_path: Path) -> None:
        client = TestClient(create_app())
        bad_path = tmp_path / "not-a-vault.txt"
        bad_path.write_text("hi")
        response = client.post("/api/auth/unlock", json={
            "vault_path": str(bad_path),
            "password": VAULT_PASSWORD,
        })
        assert response.status_code == 400
        assert ".opnvault" in response.json()["detail"]

    def test_traversal_attempt_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Pfad ausserhalb der erlaubten Basen → 400."""
        # Sehr restriktive Basis: nur ein subdir
        monkeypatch.setenv(
            "OPNCOCKPIT_VAULT_DIR", str(tmp_path / "vaults-only"),
        )
        monkeypatch.delenv("OPNCOCKPIT_DATA_DIR", raising=False)
        (tmp_path / "vaults-only").mkdir()
        path = _make_vault(tmp_path)  # liegt eine Ebene zu hoch
        client = TestClient(create_app())
        response = client.post("/api/auth/unlock", json={
            "vault_path": str(path),
            "password": VAULT_PASSWORD,
        })
        assert response.status_code == 400
        assert "ausserhalb" in response.json()["detail"].lower()

    def test_allowed_path_accepted(self, tmp_path: Path) -> None:
        """Default-Pfad unter tmp ist via conftest erlaubt → 200."""
        client = TestClient(create_app())
        path = _make_vault(tmp_path)
        response = client.post("/api/auth/unlock", json={
            "vault_path": str(path),
            "password": VAULT_PASSWORD,
        })
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# Rate-Limit (Audit #4)
# ---------------------------------------------------------------------------


class TestLoginRateLimit:
    def test_brute_force_locks_after_n_attempts(self, tmp_path: Path) -> None:
        client = TestClient(create_app())
        # Limiter auf engen Modus setzen
        client.app.state.login_rate_limiter = RateLimiter(
            window_s=60.0, max_attempts=3, cooldown_s=30.0,
        )
        path = _make_vault(tmp_path)
        # Drei Fehlversuche → kein Lock noch
        for _ in range(3):
            response = client.post("/api/auth/unlock", json={
                "vault_path": str(path),
                "password": "falsches-pw-12+",
            })
            assert response.status_code == 401
        # Vierter Versuch sollte 429 sein
        response = client.post("/api/auth/unlock", json={
            "vault_path": str(path),
            "password": "falsches-pw-12+",
        })
        assert response.status_code == 429
        assert "Retry-After" in response.headers

    def test_success_resets_window(self, tmp_path: Path) -> None:
        client = TestClient(create_app())
        client.app.state.login_rate_limiter = RateLimiter(
            window_s=60.0, max_attempts=3, cooldown_s=30.0,
        )
        path = _make_vault(tmp_path)
        # 2 Fehlversuche
        for _ in range(2):
            client.post("/api/auth/unlock", json={
                "vault_path": str(path),
                "password": "falsch-pw-12+",
            })
        # Erfolgreicher Login
        ok = client.post("/api/auth/unlock", json={
            "vault_path": str(path),
            "password": VAULT_PASSWORD,
        })
        assert ok.status_code == 200
        # Jetzt darf wieder fehlversucht werden ohne Lock
        for _ in range(3):
            response = client.post("/api/auth/unlock", json={
                "vault_path": str(path),
                "password": "falsch-pw-12+",
            })
            assert response.status_code == 401


# ---------------------------------------------------------------------------
# Bootstrap-Token (Audit #5)
# ---------------------------------------------------------------------------


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
    yield TestClient(create_app())


class TestBootstrapToken:
    def test_admin_endpoint_requires_token(
        self, multi_client: TestClient,
    ) -> None:
        response = multi_client.post("/api/bootstrap/admin", json={
            "username": "alice", "password": "starkes-passwort-12+",
        })
        assert response.status_code == 403
        assert "token" in response.json()["detail"].lower()

    def test_wrong_token_rejected(self, multi_client: TestClient) -> None:
        response = multi_client.post(
            "/api/bootstrap/admin",
            headers={"X-Bootstrap-Token": "definitiv-nicht-der-token"},
            json={"username": "alice", "password": "starkes-passwort-12+"},
        )
        assert response.status_code == 403

    def test_correct_token_accepted(self, multi_client: TestClient) -> None:
        server: ServerState = multi_client.app.state.server_state
        token = server.bootstrap_token
        assert token is not None
        response = multi_client.post(
            "/api/bootstrap/admin",
            headers={"X-Bootstrap-Token": token},
            json={"username": "alice", "password": "starkes-passwort-12+"},
        )
        assert response.status_code == 201

    def test_token_rotates_after_admin_step(
        self, multi_client: TestClient,
    ) -> None:
        server: ServerState = multi_client.app.state.server_state
        first_token = server.bootstrap_token
        assert first_token is not None
        # Admin anlegen mit erstem Token
        multi_client.post(
            "/api/bootstrap/admin",
            headers={"X-Bootstrap-Token": first_token},
            json={"username": "alice", "password": "starkes-passwort-12+"},
        )
        new_token = server.bootstrap_token
        # Es gibt einen neuen Token (fuer vault-unlock-Schritt)
        assert new_token is not None
        assert new_token != first_token


# ---------------------------------------------------------------------------
# TLS-Verify im Audit (Audit #13) — indirekt: Executor-Test ist in
# tests/unit/orchestration; hier nur Verhaltens-Smoke ueber das Audit-Modul.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# vault_mutation_lock (Audit #9)
# ---------------------------------------------------------------------------


class TestVaultMutationLock:
    def test_lock_is_noop_in_single_mode(self, tmp_path: Path) -> None:
        client = TestClient(create_app())
        server: ServerState = client.app.state.server_state
        # Single-mode → context manager ist no-op
        with server.vault_mutation_lock():
            assert server.is_single_user_mode is True

    def test_lock_holds_in_multi_mode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        data = tmp_path / "data"
        data.mkdir()
        monkeypatch.setenv("OPNCOCKPIT_DATA_DIR", str(data))
        monkeypatch.delenv(VAULT_PATH_ENV, raising=False)
        AppSettings(auth_backend="user-db").save(data / "settings.json")
        client = TestClient(create_app())
        server: ServerState = client.app.state.server_state
        assert server.is_multi_user_mode is True
        # Lock kann genommen werden — kein Deadlock weil RLock
        with server.vault_mutation_lock(), server.vault_mutation_lock():
            pass
