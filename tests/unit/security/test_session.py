"""Tests für security.session — Lifecycle, Inaktivitäts-Timeout, Credentials."""

from __future__ import annotations

from pathlib import Path

import pytest

from opn_cockpit.security.session import Session
from opn_cockpit.vault.errors import (
    SessionLockedError,
    UnknownDeviceError,
)
from opn_cockpit.vault.format import VaultHeader
from opn_cockpit.vault.model import VaultData, VaultDevice, VaultSettings
from opn_cockpit.vault.store import OpenedVault

# ---------------------------------------------------------------------------
# Helfer
# ---------------------------------------------------------------------------


def _opened_vault(
    *,
    devices: list[VaultDevice] | None = None,
    inactivity_minutes: int = 10,
) -> OpenedVault:
    data = VaultData(
        devices=devices or [],
        settings=VaultSettings(inactivity_minutes=inactivity_minutes),
    )
    header = VaultHeader(
        version=1,
        kdf_salt=b"\x00" * 16,
        kdf_time_cost=1,
        kdf_memory_cost_kib=8,
        kdf_parallelism=1,
        nonce=b"\x00" * 12,
    )
    return OpenedVault(data=data, header=header)


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_fresh_session_is_locked(self) -> None:
        s = Session()
        assert s.is_locked
        assert not s.is_unlocked
        assert s.vault_path is None

    def test_unlock_sets_state(self, tmp_path: Path) -> None:
        s = Session()
        opened = _opened_vault()
        s.unlock(opened, tmp_path / "v.opnvault")
        assert s.is_unlocked
        assert s.vault_path is not None
        assert s.opened is opened

    def test_lock_clears_state(self, tmp_path: Path) -> None:
        s = Session()
        s.unlock(_opened_vault(), tmp_path / "v.opnvault")
        s.lock()
        assert s.is_locked
        assert s.vault_path is None

    def test_lock_is_idempotent(self) -> None:
        s = Session()
        s.lock()
        s.lock()

    def test_opened_on_locked_session_raises(self) -> None:
        s = Session()
        with pytest.raises(SessionLockedError):
            _ = s.opened


# ---------------------------------------------------------------------------
# Inaktivitäts-Timeout
# ---------------------------------------------------------------------------


class TestInactivity:
    def test_seconds_until_expiry_starts_at_full_timeout(
        self, tmp_path: Path
    ) -> None:
        clock = FakeClock()
        s = Session(_clock=clock)
        s.unlock(_opened_vault(inactivity_minutes=10), tmp_path / "v.opnvault")
        assert s.seconds_until_expiry() == pytest.approx(600.0)

    def test_check_inactivity_locks_after_timeout(
        self, tmp_path: Path
    ) -> None:
        clock = FakeClock()
        s = Session(_clock=clock)
        s.unlock(_opened_vault(inactivity_minutes=1), tmp_path / "v.opnvault")
        clock.advance(59)
        assert not s.check_inactivity()
        assert s.is_unlocked
        clock.advance(2)
        assert s.check_inactivity()
        assert s.is_locked

    def test_touch_resets_timer(self, tmp_path: Path) -> None:
        clock = FakeClock()
        s = Session(_clock=clock)
        s.unlock(_opened_vault(inactivity_minutes=1), tmp_path / "v.opnvault")
        clock.advance(50)
        s.touch()
        clock.advance(50)  # 100s total, aber Timer wurde bei 50 zurückgesetzt
        assert not s.check_inactivity()

    def test_timeout_from_vault_settings(self, tmp_path: Path) -> None:
        s = Session(_clock=FakeClock())
        s.unlock(_opened_vault(inactivity_minutes=25), tmp_path / "v.opnvault")
        assert s.inactivity_timeout_s == 25 * 60.0

    def test_check_inactivity_returns_false_on_locked(self) -> None:
        s = Session()
        assert s.check_inactivity() is False

    def test_seconds_until_expiry_on_locked_returns_zero(self) -> None:
        s = Session()
        assert s.seconds_until_expiry() == 0.0


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


class TestCredentials:
    def test_returns_credentials_for_known_device(self, tmp_path: Path) -> None:
        s = Session()
        s.unlock(
            _opened_vault(
                devices=[
                    VaultDevice(
                        id="d1", name="X", host="h",
                        api_key="K", api_secret="S",
                    )
                ]
            ),
            tmp_path / "v.opnvault",
        )
        key, secret = s.credentials_for("d1")
        assert (key, secret) == ("K", "S")

    def test_unknown_device_raises(self, tmp_path: Path) -> None:
        s = Session()
        s.unlock(_opened_vault(), tmp_path / "v.opnvault")
        with pytest.raises(UnknownDeviceError):
            s.credentials_for("nonexistent")

    def test_locked_session_raises(self) -> None:
        s = Session()
        with pytest.raises(SessionLockedError):
            s.credentials_for("d1")


# ---------------------------------------------------------------------------
# Replace-Opened
# ---------------------------------------------------------------------------


class TestReplaceOpened:
    def test_replaces_and_resets_timer(self, tmp_path: Path) -> None:
        clock = FakeClock()
        s = Session(_clock=clock)
        s.unlock(_opened_vault(inactivity_minutes=10), tmp_path / "v.opnvault")
        clock.advance(300)
        new_opened = _opened_vault(devices=[
            VaultDevice(id="x", name="X", host="h")
        ])
        s.replace_opened(new_opened)
        assert s.opened is new_opened
        assert s.seconds_until_expiry() == pytest.approx(600.0)

    def test_replace_on_locked_raises(self) -> None:
        s = Session()
        with pytest.raises(SessionLockedError):
            s.replace_opened(_opened_vault())
