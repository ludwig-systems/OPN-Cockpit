"""Tests für vault.format — Header-Pack/Unpack-Roundtrip, Magic, Version."""

from __future__ import annotations

import pytest

from opn_cockpit.vault.errors import CorruptVaultError, VaultVersionError
from opn_cockpit.vault.format import (
    HEADER_MAGIC,
    HEADER_SIZE,
    HEADER_VERSION,
    NONCE_SIZE,
    SALT_SIZE,
    VaultHeader,
)


def _make_header(version: int = HEADER_VERSION) -> VaultHeader:
    return VaultHeader(
        version=version,
        kdf_salt=b"\x11" * SALT_SIZE,
        kdf_time_cost=4,
        kdf_memory_cost_kib=262144,
        kdf_parallelism=2,
        nonce=b"\x22" * NONCE_SIZE,
    )


class TestPackUnpackRoundtrip:
    def test_pack_size_matches_constant(self) -> None:
        packed = _make_header().pack()
        assert len(packed) == HEADER_SIZE

    def test_pack_starts_with_magic(self) -> None:
        packed = _make_header().pack()
        assert packed[: len(HEADER_MAGIC)] == HEADER_MAGIC

    def test_roundtrip_preserves_fields(self) -> None:
        original = _make_header()
        recovered = VaultHeader.unpack(original.pack())
        assert recovered == original


class TestUnpackErrors:
    def test_too_short_raises_corrupt(self) -> None:
        with pytest.raises(CorruptVaultError):
            VaultHeader.unpack(b"short")

    def test_wrong_magic_raises_corrupt(self) -> None:
        bogus = b"XXXXXXXX" + b"\x00" * (HEADER_SIZE - 8)
        with pytest.raises(CorruptVaultError) as exc:
            VaultHeader.unpack(bogus)
        assert "Magic" in str(exc.value)

    def test_unknown_version_raises_version(self) -> None:
        fake = _make_header(version=99)
        with pytest.raises(VaultVersionError):
            VaultHeader.unpack(fake.pack())


class TestHeaderValidation:
    def test_short_salt_raises_corrupt(self) -> None:
        with pytest.raises(CorruptVaultError):
            VaultHeader(
                version=1,
                kdf_salt=b"\x00" * (SALT_SIZE - 1),
                kdf_time_cost=4,
                kdf_memory_cost_kib=262144,
                kdf_parallelism=2,
                nonce=b"\x00" * NONCE_SIZE,
            )

    def test_short_nonce_raises_corrupt(self) -> None:
        with pytest.raises(CorruptVaultError):
            VaultHeader(
                version=1,
                kdf_salt=b"\x00" * SALT_SIZE,
                kdf_time_cost=4,
                kdf_memory_cost_kib=262144,
                kdf_parallelism=2,
                nonce=b"\x00" * (NONCE_SIZE - 1),
            )
