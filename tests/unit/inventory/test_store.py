"""Tests für inventory.store — Fassade über Session."""

from __future__ import annotations

from pathlib import Path

import pytest

from opn_cockpit.inventory.store import InventoryStore
from opn_cockpit.security.session import Session
from opn_cockpit.vault.errors import UnknownDeviceError
from opn_cockpit.vault.format import VaultHeader
from opn_cockpit.vault.model import VaultData, VaultDevice
from opn_cockpit.vault.store import OpenedVault


def _opened(devices: list[VaultDevice]) -> OpenedVault:
    data = VaultData(devices=devices)
    header = VaultHeader(
        version=1,
        kdf_salt=b"\x00" * 16,
        kdf_time_cost=1,
        kdf_memory_cost_kib=8,
        kdf_parallelism=1,
        nonce=b"\x00" * 12,
    )
    return OpenedVault(data=data, header=header)


def _session_with(devices: list[VaultDevice]) -> Session:
    s = Session()
    s.unlock(_opened(devices), Path("dummy.opnvault"))
    return s


class TestListDevices:
    def test_locked_session_returns_empty(self) -> None:
        store = InventoryStore(session=Session())
        assert store.list_devices() == []

    def test_returns_devices_without_secrets(self) -> None:
        s = _session_with(
            [
                VaultDevice(
                    id="i", name="Berlin", host="h",
                    api_key="K", api_secret="S",
                )
            ]
        )
        store = InventoryStore(session=s)
        devices = store.list_devices()
        assert len(devices) == 1
        # Device hat per Vertrag KEINE api_key/api_secret-Felder
        assert not hasattr(devices[0], "api_key")
        assert not hasattr(devices[0], "api_secret")


class TestGetDevice:
    def test_returns_device_by_id(self) -> None:
        s = _session_with(
            [
                VaultDevice(id="a", name="X", host="h1"),
                VaultDevice(id="b", name="Y", host="h2"),
            ]
        )
        store = InventoryStore(session=s)
        device = store.get_device("b")
        assert device.name == "Y"

    def test_unknown_id_raises(self) -> None:
        s = _session_with([VaultDevice(id="a", name="X", host="h")])
        store = InventoryStore(session=s)
        with pytest.raises(UnknownDeviceError):
            store.get_device("does-not-exist")


class TestSelect:
    def test_propagates_selector_to_apply_selector(self) -> None:
        s = _session_with(
            [
                VaultDevice(id="a", name="Berlin", host="h", tags=["core"]),
                VaultDevice(id="b", name="München", host="h", tags=["branches"]),
            ]
        )
        store = InventoryStore(session=s)
        result = store.select("tag:branches")
        assert len(result) == 1
        assert result[0].name == "München"


class TestTags:
    def test_returns_sorted_unique_tags(self) -> None:
        s = _session_with(
            [
                VaultDevice(id="a", name="X", host="h", tags=["core", "branches"]),
                VaultDevice(id="b", name="Y", host="h", tags=["branches", "germany"]),
            ]
        )
        store = InventoryStore(session=s)
        assert store.tags == ["branches", "core", "germany"]

    def test_locked_session_returns_empty(self) -> None:
        store = InventoryStore(session=Session())
        assert store.tags == []
