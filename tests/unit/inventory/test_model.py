"""Tests für inventory.model — Device als Sicht ohne Secrets."""

from __future__ import annotations

import dataclasses

import pytest

from opn_cockpit.inventory.model import Device
from opn_cockpit.vault.model import VaultDevice


class TestFromVaultDevice:
    def test_copies_non_secret_fields(self) -> None:
        vd = VaultDevice(
            id="i1",
            name="Berlin",
            host="opn.lab",
            port=8443,
            tls_verify=False,
            tags=["a", "b"],
            api_key="K",
            api_secret="S",
            descr="HQ",
        )
        d = Device.from_vault_device(vd)
        assert d.id == "i1"
        assert d.name == "Berlin"
        assert d.host == "opn.lab"
        assert d.port == 8443
        assert d.tls_verify is False
        assert d.tags == ("a", "b")
        assert d.descr == "HQ"

    def test_device_has_no_secret_fields(self) -> None:
        # Vertragstest: Device darf KEINE api_key/api_secret-Felder haben.
        field_names = {f.name for f in dataclasses.fields(Device)}
        forbidden = {"api_key", "api_secret", "secret", "password"}
        assert not (field_names & forbidden), (
            "Device-Datentyp darf keine Secret-Felder haben — "
            "Klartext-Credentials bleiben im VaultDevice."
        )

    def test_tags_are_immutable_tuple(self) -> None:
        vd = VaultDevice(id="i", name="X", host="h", tags=["a"])
        d = Device.from_vault_device(vd)
        assert isinstance(d.tags, tuple)

    def test_is_frozen(self) -> None:
        vd = VaultDevice(id="i", name="X", host="h")
        d = Device.from_vault_device(vd)
        with pytest.raises(dataclasses.FrozenInstanceError):
            d.name = "Y"  # type: ignore[misc]


class TestDisplayLabel:
    def test_includes_name_and_host(self) -> None:
        vd = VaultDevice(id="i", name="Berlin", host="opn.lab", port=443)
        d = Device.from_vault_device(vd)
        assert "Berlin" in d.display_label
        assert "opn.lab" in d.display_label
