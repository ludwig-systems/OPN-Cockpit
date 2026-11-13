"""Tests fuer die TOTP-Erweiterungen in ``UserStore``."""

from __future__ import annotations

from pathlib import Path

import pytest

from opn_cockpit.security.users import UserStore

VALID_PW = "korrektes-pferd-batterie-heftklammer"


@pytest.fixture()
def store(tmp_path: Path) -> UserStore:
    return UserStore(path=tmp_path / "users.db")


class TestTotpFields:
    def test_new_user_defaults_no_totp(self, store: UserStore) -> None:
        u = store.create_user(username="a", password=VALID_PW, role="admin")
        assert u.totp_enabled is False
        assert store.get_totp_secret(u.id) == ""
        assert store.get_backup_code_hashes(u.id) == []

    def test_set_secret_does_not_enable(self, store: UserStore) -> None:
        u = store.create_user(username="a", password=VALID_PW, role="admin")
        store.set_totp_secret(u.id, "ABCD" * 8)
        after = store.get_user(u.id)
        assert after is not None
        assert after.totp_enabled is False
        assert store.get_totp_secret(u.id) == "ABCD" * 8

    def test_enable_totp_sets_flag_and_backup_codes(self, store: UserStore) -> None:
        u = store.create_user(username="a", password=VALID_PW, role="admin")
        store.set_totp_secret(u.id, "S" * 32)
        hashes = ["h1", "h2", "h3"]
        store.enable_totp(u.id, hashes)
        after = store.get_user(u.id)
        assert after is not None
        assert after.totp_enabled is True
        assert store.get_backup_code_hashes(u.id) == hashes

    def test_disable_totp_clears_everything(self, store: UserStore) -> None:
        u = store.create_user(username="a", password=VALID_PW, role="admin")
        store.set_totp_secret(u.id, "S" * 32)
        store.enable_totp(u.id, ["x", "y"])
        store.disable_totp(u.id)
        after = store.get_user(u.id)
        assert after is not None
        assert after.totp_enabled is False
        assert store.get_totp_secret(u.id) == ""
        assert store.get_backup_code_hashes(u.id) == []

    def test_consume_backup_code(self, store: UserStore) -> None:
        u = store.create_user(username="a", password=VALID_PW, role="admin")
        store.enable_totp(u.id, ["a", "b", "c"])
        store.set_backup_code_hashes(u.id, ["b", "c"])
        assert store.get_backup_code_hashes(u.id) == ["b", "c"]
