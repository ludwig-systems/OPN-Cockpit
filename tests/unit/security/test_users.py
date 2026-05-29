"""Tests fuer das UserStore-Modul (SQLite + Argon2id)."""

from __future__ import annotations

from pathlib import Path

import pytest

from opn_cockpit.security.users import UserStore, UserStoreError

VALID_PASSWORD = "korrektes-pferd-batterie-heftklammer"


@pytest.fixture()
def store(tmp_path: Path) -> UserStore:
    return UserStore(path=tmp_path / "users.db")


class TestCreateUser:
    def test_creates_user(self, store: UserStore) -> None:
        user = store.create_user(
            username="admin",
            password=VALID_PASSWORD,
            role="admin",
        )
        assert user.username == "admin"
        assert user.role == "admin"
        assert user.allowed_tags == ()
        assert user.disabled is False
        assert user.id > 0

    def test_with_allowed_tags(self, store: UserStore) -> None:
        user = store.create_user(
            username="branch-admin",
            password=VALID_PASSWORD,
            role="operator",
            allowed_tags=("branches", "germany"),
        )
        assert user.allowed_tags == ("branches", "germany")

    def test_duplicate_username_raises(self, store: UserStore) -> None:
        store.create_user(username="a", password=VALID_PASSWORD, role="admin")
        with pytest.raises(UserStoreError, match="existiert bereits"):
            store.create_user(username="a", password=VALID_PASSWORD, role="admin")

    def test_short_password_raises(self, store: UserStore) -> None:
        with pytest.raises(UserStoreError, match="12 Zeichen"):
            store.create_user(username="a", password="kurz", role="admin")

    def test_invalid_role_raises(self, store: UserStore) -> None:
        with pytest.raises(UserStoreError, match="Ungueltige Rolle"):
            store.create_user(
                username="a", password=VALID_PASSWORD, role="god",  # type: ignore[arg-type]
            )


class TestAuthenticate:
    def test_correct_password(self, store: UserStore) -> None:
        store.create_user(username="alice", password=VALID_PASSWORD, role="operator")
        user = store.authenticate("alice", VALID_PASSWORD)
        assert user is not None
        assert user.username == "alice"
        assert user.last_login_at_iso is not None

    def test_wrong_password(self, store: UserStore) -> None:
        store.create_user(username="alice", password=VALID_PASSWORD, role="operator")
        assert store.authenticate("alice", "falsches-pw-12+") is None

    def test_unknown_user(self, store: UserStore) -> None:
        assert store.authenticate("ghost", VALID_PASSWORD) is None

    def test_disabled_user_cannot_log_in(self, store: UserStore) -> None:
        user = store.create_user(
            username="bob", password=VALID_PASSWORD, role="viewer",
        )
        store.update_user(user.id, disabled=True)
        assert store.authenticate("bob", VALID_PASSWORD) is None


class TestUpdateAndDelete:
    def test_update_role(self, store: UserStore) -> None:
        user = store.create_user(
            username="carol", password=VALID_PASSWORD, role="viewer",
        )
        updated = store.update_user(user.id, role="admin")
        assert updated.role == "admin"

    def test_update_allowed_tags(self, store: UserStore) -> None:
        user = store.create_user(
            username="dave", password=VALID_PASSWORD, role="operator",
        )
        updated = store.update_user(user.id, allowed_tags=("lab", "germany"))
        assert updated.allowed_tags == ("lab", "germany")

    def test_change_password(self, store: UserStore) -> None:
        user = store.create_user(
            username="erin", password=VALID_PASSWORD, role="operator",
        )
        new_pw = "neues-passwort-mit-genug-zeichen"
        store.change_password(user.id, new_pw)
        assert store.authenticate("erin", VALID_PASSWORD) is None
        assert store.authenticate("erin", new_pw) is not None

    def test_delete(self, store: UserStore) -> None:
        user = store.create_user(
            username="fred", password=VALID_PASSWORD, role="operator",
        )
        assert store.delete_user(user.id) is True
        assert store.get_user(user.id) is None
        assert store.delete_user(user.id) is False

    def test_update_unknown_user_raises(self, store: UserStore) -> None:
        with pytest.raises(UserStoreError, match="nicht gefunden"):
            store.update_user(99999, role="admin")

    def test_count_and_list(self, store: UserStore) -> None:
        assert store.count() == 0
        store.create_user(username="u1", password=VALID_PASSWORD, role="viewer")
        store.create_user(username="u2", password=VALID_PASSWORD, role="admin")
        assert store.count() == 2
        names = [u.username for u in store.list_users()]
        assert names == ["u1", "u2"]


class TestPersistence:
    def test_users_survive_reopen(self, tmp_path: Path) -> None:
        db = tmp_path / "users.db"
        s1 = UserStore(path=db)
        s1.create_user(username="alice", password=VALID_PASSWORD, role="admin")
        s1.close()
        s2 = UserStore(path=db)
        assert s2.get_user_by_name("alice") is not None
        s2.close()
