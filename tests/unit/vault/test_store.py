"""Tests für vault.store — create / open / save / change_password / export_template."""

from __future__ import annotations

from pathlib import Path

import pytest

from opn_cockpit.vault.errors import (
    CorruptVaultError,
    InvalidPasswordError,
    VaultIOError,
    WeakPasswordError,
)
from opn_cockpit.vault.format import HEADER_SIZE
from opn_cockpit.vault.model import VaultData, VaultDevice, VaultSettings
from opn_cockpit.vault.store import (
    MIN_PASSWORD_LENGTH,
    change_password,
    create_vault,
    export_template,
    open_vault,
    save_vault,
    validate_password,
)

# ---------------------------------------------------------------------------
# validate_password
# ---------------------------------------------------------------------------


class TestValidatePassword:
    def test_accepts_min_length(self) -> None:
        validate_password("x" * MIN_PASSWORD_LENGTH)

    def test_rejects_short_password(self) -> None:
        with pytest.raises(WeakPasswordError):
            validate_password("x" * (MIN_PASSWORD_LENGTH - 1))


# ---------------------------------------------------------------------------
# Create + Open Roundtrip
# ---------------------------------------------------------------------------


class TestCreateAndOpen:
    def test_create_then_open_roundtrip(
        self, tmp_path: Path, valid_password: str
    ) -> None:
        path = tmp_path / "v.opnvault"
        data = VaultData(
            devices=[
                VaultDevice(
                    id="i", name="Berlin", host="opn.lab",
                    api_key="k", api_secret="s",
                )
            ]
        )
        create_vault(path, valid_password, data)
        opened = open_vault(path, valid_password)
        assert len(opened.data.devices) == 1
        assert opened.data.devices[0].api_secret == "s"

    def test_empty_default_vault(
        self, tmp_path: Path, valid_password: str
    ) -> None:
        path = tmp_path / "v.opnvault"
        create_vault(path, valid_password)
        opened = open_vault(path, valid_password)
        assert opened.data.devices == []

    def test_refuses_to_overwrite_existing(
        self, tmp_path: Path, valid_password: str
    ) -> None:
        path = tmp_path / "v.opnvault"
        create_vault(path, valid_password)
        with pytest.raises(VaultIOError):
            create_vault(path, valid_password)

    def test_overwrite_flag_works(
        self, tmp_path: Path, valid_password: str
    ) -> None:
        path = tmp_path / "v.opnvault"
        create_vault(path, valid_password)
        create_vault(path, valid_password, overwrite=True)

    def test_short_password_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "v.opnvault"
        with pytest.raises(WeakPasswordError):
            create_vault(path, "kurz")

    def test_creates_missing_parent_directory(
        self, tmp_path: Path, valid_password: str
    ) -> None:
        # User picks a target like Desktop\OPN-Cockpit\TEST.opnvault where the
        # OPN-Cockpit subfolder does not exist yet — _atomic_write must mkdir.
        path = tmp_path / "neuer-ordner" / "weiter-tief" / "v.opnvault"
        create_vault(path, valid_password)
        assert path.exists()
        assert path.parent.is_dir()


# ---------------------------------------------------------------------------
# Open-Fehlerfälle
# ---------------------------------------------------------------------------


class TestOpenErrors:
    def test_wrong_password_raises_invalid_password(
        self, tmp_path: Path, valid_password: str
    ) -> None:
        path = tmp_path / "v.opnvault"
        create_vault(path, valid_password)
        with pytest.raises(InvalidPasswordError):
            open_vault(path, "Falscher-Master-Passwort-Wert-X")

    def test_missing_file_raises_io(self, tmp_path: Path) -> None:
        path = tmp_path / "no-such-vault.opnvault"
        with pytest.raises(VaultIOError):
            open_vault(path, "irrelevant-password")

    def test_too_short_file_raises_corrupt(
        self, tmp_path: Path, valid_password: str
    ) -> None:
        path = tmp_path / "v.opnvault"
        path.write_bytes(b"short")
        with pytest.raises(CorruptVaultError):
            open_vault(path, valid_password)

    def test_tampered_header_raises(
        self, tmp_path: Path, valid_password: str
    ) -> None:
        path = tmp_path / "v.opnvault"
        create_vault(path, valid_password)
        raw = bytearray(path.read_bytes())
        # Flip a byte in the AAD-protected header (e.g. KDF time_cost)
        raw[HEADER_SIZE - 1] ^= 0xFF
        path.write_bytes(bytes(raw))
        # Header-Parse läuft durch, GCM-Verifikation schlägt fehl.
        with pytest.raises(InvalidPasswordError):
            open_vault(path, valid_password)

    def test_tampered_ciphertext_raises(
        self, tmp_path: Path, valid_password: str
    ) -> None:
        path = tmp_path / "v.opnvault"
        create_vault(path, valid_password)
        raw = bytearray(path.read_bytes())
        # Bit-Flip im Ciphertext
        raw[HEADER_SIZE] ^= 0x01
        path.write_bytes(bytes(raw))
        with pytest.raises(InvalidPasswordError):
            open_vault(path, valid_password)


# ---------------------------------------------------------------------------
# Save
# ---------------------------------------------------------------------------


class TestSave:
    def test_modifications_persist(
        self, tmp_path: Path, valid_password: str
    ) -> None:
        path = tmp_path / "v.opnvault"
        create_vault(path, valid_password)
        opened = open_vault(path, valid_password)
        opened.data.devices.append(
            VaultDevice(id="i1", name="X", host="h", api_key="k", api_secret="s")
        )
        save_vault(path, opened, valid_password)

        reopened = open_vault(path, valid_password)
        assert len(reopened.data.devices) == 1
        assert reopened.data.devices[0].name == "X"

    def test_save_rotates_nonce_but_keeps_salt(
        self, tmp_path: Path, valid_password: str
    ) -> None:
        path = tmp_path / "v.opnvault"
        create_vault(path, valid_password)
        first_raw = path.read_bytes()
        opened = open_vault(path, valid_password)
        save_vault(path, opened, valid_password)
        second_raw = path.read_bytes()
        # Salt-Position: 12..28
        assert first_raw[12:28] == second_raw[12:28]
        # Nonce-Position: 40..52
        assert first_raw[40:52] != second_raw[40:52]


# ---------------------------------------------------------------------------
# Change-Password
# ---------------------------------------------------------------------------


class TestChangePassword:
    def test_old_password_no_longer_works(
        self, tmp_path: Path, valid_password: str
    ) -> None:
        path = tmp_path / "v.opnvault"
        new = "Mein-Neues-Master-Passwort-1234"
        create_vault(path, valid_password)
        change_password(path, valid_password, new)
        with pytest.raises(InvalidPasswordError):
            open_vault(path, valid_password)
        # neues Passwort funktioniert
        open_vault(path, new)

    def test_change_password_rotates_salt(
        self, tmp_path: Path, valid_password: str
    ) -> None:
        path = tmp_path / "v.opnvault"
        new = "Mein-Neues-Master-Passwort-1234"
        create_vault(path, valid_password)
        first_salt = path.read_bytes()[12:28]
        change_password(path, valid_password, new)
        second_salt = path.read_bytes()[12:28]
        assert first_salt != second_salt

    def test_rejects_weak_new_password(
        self, tmp_path: Path, valid_password: str
    ) -> None:
        path = tmp_path / "v.opnvault"
        create_vault(path, valid_password)
        with pytest.raises(WeakPasswordError):
            change_password(path, valid_password, "kurz")

    def test_wrong_old_password_fails_first(
        self, tmp_path: Path, valid_password: str
    ) -> None:
        path = tmp_path / "v.opnvault"
        create_vault(path, valid_password)
        with pytest.raises(InvalidPasswordError):
            change_password(path, "Falsches-Passwort-Hier-123", "neues-passwort-12X")


# ---------------------------------------------------------------------------
# Export Template
# ---------------------------------------------------------------------------


class TestExportTemplate:
    def test_template_keeps_inventory_but_blanks_secrets(
        self, tmp_path: Path, valid_password: str
    ) -> None:
        source = tmp_path / "v.opnvault"
        dest = tmp_path / "template.opnvault"
        create_vault(
            source,
            valid_password,
            VaultData(
                devices=[
                    VaultDevice(
                        id="i", name="Berlin", host="h",
                        tags=["a"], api_key="K", api_secret="S",
                    )
                ],
                settings=VaultSettings(inactivity_minutes=15),
            ),
        )
        export_template(source, dest, valid_password)
        template = open_vault(dest, valid_password)
        assert len(template.data.devices) == 1
        assert template.data.devices[0].api_key == ""
        assert template.data.devices[0].api_secret == ""
        # Nicht-Secret-Felder bleiben
        assert template.data.devices[0].name == "Berlin"
        assert template.data.devices[0].tags == ["a"]
        # Per-Vault-Settings ebenfalls.
        assert template.data.settings.inactivity_minutes == 15

    def test_template_does_not_modify_source(
        self, tmp_path: Path, valid_password: str
    ) -> None:
        source = tmp_path / "v.opnvault"
        dest = tmp_path / "template.opnvault"
        create_vault(
            source,
            valid_password,
            VaultData(devices=[
                VaultDevice(id="i", name="X", host="h", api_secret="SECRET")
            ]),
        )
        export_template(source, dest, valid_password)
        source_reopened = open_vault(source, valid_password)
        assert source_reopened.data.devices[0].api_secret == "SECRET"

    def test_refuses_to_overwrite_dest(
        self, tmp_path: Path, valid_password: str
    ) -> None:
        source = tmp_path / "v.opnvault"
        dest = tmp_path / "template.opnvault"
        create_vault(source, valid_password)
        dest.write_bytes(b"existing")
        with pytest.raises(VaultIOError):
            export_template(source, dest, valid_password)
