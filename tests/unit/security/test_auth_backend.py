"""Tests fuer Auth-Backends (VaultAuthBackend + UserDbAuthBackend)."""

from __future__ import annotations

from pathlib import Path

import pytest

from opn_cockpit.security.auth_backend import (
    UserDbAuthBackend,
    VaultAuthBackend,
    make_session,
)
from opn_cockpit.security.users import UserStore
from opn_cockpit.vault.model import VaultData, VaultDevice
from opn_cockpit.vault.store import create_vault, open_vault

PASSWORD = "korrektes-pferd-batterie-heftklammer"
USER_PASSWORD = "user-passwort-mit-genug-zeichen"


def _make_vault(tmp_path: Path) -> Path:
    path = tmp_path / "test.opnvault"
    devices = [
        VaultDevice(
            id="dev-001", name="HQ Berlin", host="opn-1.lab", port=443,
            tls_verify=True, tags=[], api_key="k", api_secret="s", descr="",
        ),
    ]
    create_vault(path, PASSWORD, VaultData(devices=devices))
    return path


class TestVaultAuthBackend:
    def test_correct_password_returns_result(self, tmp_path: Path) -> None:
        path = _make_vault(tmp_path)
        backend = VaultAuthBackend()
        result = backend.authenticate({
            "vault_path": str(path), "password": PASSWORD,
        })
        assert result is not None
        assert result.user is None
        assert result.vault_path == path
        assert result.master_password == PASSWORD
        assert len(result.opened_vault.data.devices) == 1

    def test_wrong_password_returns_none(self, tmp_path: Path) -> None:
        path = _make_vault(tmp_path)
        backend = VaultAuthBackend()
        result = backend.authenticate({
            "vault_path": str(path), "password": "wrong-but-12-zeichen",
        })
        assert result is None

    def test_missing_vault_returns_none(self, tmp_path: Path) -> None:
        backend = VaultAuthBackend()
        result = backend.authenticate({
            "vault_path": str(tmp_path / "no-such.opnvault"),
            "password": PASSWORD,
        })
        assert result is None

    def test_empty_credentials_returns_none(self) -> None:
        backend = VaultAuthBackend()
        assert backend.authenticate({}) is None
        assert backend.authenticate({"vault_path": "", "password": ""}) is None


class TestUserDbAuthBackend:
    @pytest.fixture()
    def backend(self, tmp_path: Path) -> UserDbAuthBackend:
        # Vault einmal entsperren (server-bootstrap-simulation).
        path = _make_vault(tmp_path)
        opened = open_vault(path, PASSWORD)
        user_store = UserStore(path=tmp_path / "users.db")
        user_store.create_user(
            username="alice", password=USER_PASSWORD, role="operator",
        )
        return UserDbAuthBackend(
            user_store=user_store,
            opened_vault=opened,
            vault_path=path,
            master_password=PASSWORD,
        )

    def test_correct_login(self, backend: UserDbAuthBackend) -> None:
        result = backend.authenticate({
            "username": "alice", "password": USER_PASSWORD,
        })
        assert result is not None
        assert result.user is not None
        assert result.user.username == "alice"
        assert result.master_password == PASSWORD

    def test_wrong_password(self, backend: UserDbAuthBackend) -> None:
        result = backend.authenticate({
            "username": "alice", "password": "falsch-pw-zwoelf",
        })
        assert result is None

    def test_unknown_user(self, backend: UserDbAuthBackend) -> None:
        result = backend.authenticate({
            "username": "ghost", "password": USER_PASSWORD,
        })
        assert result is None

    def test_disabled_user_cannot_log_in(self, backend: UserDbAuthBackend) -> None:
        user = backend.user_store.get_user_by_name("alice")
        assert user is not None
        backend.user_store.update_user(user.id, disabled=True)
        result = backend.authenticate({
            "username": "alice", "password": USER_PASSWORD,
        })
        assert result is None

    def test_all_sessions_share_same_opened_vault(
        self,
        backend: UserDbAuthBackend,
    ) -> None:
        """Wichtige Multi-User-Invariante: alle Sessions zeigen auf denselben Vault."""
        backend.user_store.create_user(
            username="bob", password=USER_PASSWORD, role="viewer",
        )
        a = backend.authenticate({"username": "alice", "password": USER_PASSWORD})
        b = backend.authenticate({"username": "bob", "password": USER_PASSWORD})
        assert a is not None and b is not None
        assert a.opened_vault is b.opened_vault


class TestMakeSession:
    def test_creates_unlocked_session(self, tmp_path: Path) -> None:
        path = _make_vault(tmp_path)
        backend = VaultAuthBackend()
        result = backend.authenticate({
            "vault_path": str(path), "password": PASSWORD,
        })
        assert result is not None
        session = make_session(result)
        assert session.is_unlocked
        assert session.master_password == PASSWORD
        assert session.vault_path == path
