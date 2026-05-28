"""Tests für vault.model — VaultData / VaultDevice / VaultSettings."""

from __future__ import annotations

import json

import pytest

from opn_cockpit.vault.errors import CorruptVaultError
from opn_cockpit.vault.model import (
    SCHEMA_VERSION_CURRENT,
    VaultData,
    VaultDevice,
    VaultSettings,
)


class TestVaultDevice:
    def test_new_id_returns_unique_uuids(self) -> None:
        a = VaultDevice.new_id()
        b = VaultDevice.new_id()
        assert a != b
        assert len(a) == 36  # 8-4-4-4-12

    def test_defaults(self) -> None:
        d = VaultDevice(id="x", name="Berlin", host="opn-berlin.lab")
        assert d.port == 443
        assert d.tls_verify is True
        assert d.tags == []
        assert d.api_key == ""
        assert d.api_secret == ""


class TestVaultSettings:
    def test_defaults_match_plan(self) -> None:
        s = VaultSettings()
        assert s.inactivity_minutes == 10
        assert s.max_workers == 8
        assert s.connect_timeout_s == 5.0
        assert s.read_timeout_s == 30.0
        assert s.reconfigure_timeout_s == 60.0
        assert s.retry_count == 2


class TestRoundtrip:
    def test_default_vault_roundtrip(self) -> None:
        data = VaultData()
        recovered = VaultData.from_json_bytes(data.to_json_bytes())
        assert recovered.schema_version == data.schema_version
        assert recovered.devices == []
        assert recovered.settings == data.settings

    def test_devices_and_settings_roundtrip(self) -> None:
        data = VaultData(
            devices=[
                VaultDevice(
                    id="id-1",
                    name="Berlin",
                    host="opn-berlin.lab",
                    port=443,
                    tls_verify=False,
                    tags=["branches", "germany"],
                    api_key="abc",
                    api_secret="secret",
                    descr="HQ",
                ),
                VaultDevice(
                    id="id-2",
                    name="München",
                    host="opn-muc.lab",
                    tags=["branches"],
                ),
            ],
            settings=VaultSettings(
                inactivity_minutes=15,
                max_workers=4,
            ),
        )
        recovered = VaultData.from_json_bytes(data.to_json_bytes())
        assert len(recovered.devices) == 2
        assert recovered.devices[0].host == "opn-berlin.lab"
        assert recovered.devices[0].tags == ["branches", "germany"]
        assert recovered.devices[0].api_secret == "secret"
        assert recovered.settings.inactivity_minutes == 15
        assert recovered.settings.max_workers == 4


class TestDefensiveReader:
    def test_extra_unknown_fields_are_ignored(self) -> None:
        payload = json.dumps(
            {
                "schema_version": SCHEMA_VERSION_CURRENT,
                "future_feature": "we will add this later",
                "devices": [],
                "settings": {},
            }
        ).encode("utf-8")
        recovered = VaultData.from_json_bytes(payload)
        assert recovered.devices == []

    def test_missing_settings_uses_defaults(self) -> None:
        payload = json.dumps({"devices": []}).encode("utf-8")
        recovered = VaultData.from_json_bytes(payload)
        assert recovered.settings.inactivity_minutes == 10

    def test_partial_settings_filled_with_defaults(self) -> None:
        payload = json.dumps(
            {"devices": [], "settings": {"max_workers": 16}}
        ).encode("utf-8")
        recovered = VaultData.from_json_bytes(payload)
        assert recovered.settings.max_workers == 16
        assert recovered.settings.inactivity_minutes == 10  # default kept

    def test_non_dict_payload_raises_corrupt(self) -> None:
        with pytest.raises(CorruptVaultError):
            VaultData.from_json_bytes(b'["not", "a", "dict"]')

    def test_garbage_raises_corrupt(self) -> None:
        with pytest.raises(CorruptVaultError):
            VaultData.from_json_bytes(b"\xff\xfe\xfd not-json")

    def test_invalid_utf8_raises_corrupt(self) -> None:
        with pytest.raises(CorruptVaultError):
            VaultData.from_json_bytes(b"\xff\xfeinvalid utf8")

    def test_device_without_id_gets_generated_id(self) -> None:
        payload = json.dumps(
            {"devices": [{"name": "X", "host": "h"}]}
        ).encode("utf-8")
        recovered = VaultData.from_json_bytes(payload)
        assert recovered.devices[0].id
        assert len(recovered.devices[0].id) == 36
