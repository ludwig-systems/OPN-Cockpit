"""Tests für vault.crypto — KDF determinism, AES-GCM roundtrip, tampering."""

from __future__ import annotations

import pytest

from opn_cockpit.vault.crypto import (
    KEY_LEN,
    decrypt,
    derive_key,
    encrypt,
    generate_nonce,
    generate_salt,
)
from opn_cockpit.vault.errors import InvalidPasswordError
from opn_cockpit.vault.format import NONCE_SIZE, SALT_SIZE


class TestKdf:
    def test_derive_key_is_deterministic(self) -> None:
        salt = generate_salt()
        k1 = derive_key("password", salt)
        k2 = derive_key("password", salt)
        assert k1 == k2
        assert len(k1) == KEY_LEN

    def test_derive_key_differs_with_salt(self) -> None:
        k1 = derive_key("password", generate_salt())
        k2 = derive_key("password", generate_salt())
        assert k1 != k2

    def test_derive_key_differs_with_password(self) -> None:
        salt = generate_salt()
        k1 = derive_key("alpha", salt)
        k2 = derive_key("bravo", salt)
        assert k1 != k2


class TestRandomMaterial:
    def test_salt_size(self) -> None:
        assert len(generate_salt()) == SALT_SIZE

    def test_nonce_size(self) -> None:
        assert len(generate_nonce()) == NONCE_SIZE

    def test_fresh_values_differ(self) -> None:
        # statistisch praktisch sicher, dass zwei zufällige Bytes-Blöcke
        # unterschiedlich sind.
        assert generate_salt() != generate_salt()
        assert generate_nonce() != generate_nonce()


class TestAead:
    def test_roundtrip(self) -> None:
        key = derive_key("password", generate_salt())
        nonce = generate_nonce()
        aad = b"vault-header-bytes"
        plaintext = b"some secret payload"
        ciphertext = encrypt(plaintext, key, nonce, aad)
        recovered = decrypt(ciphertext, key, nonce, aad)
        assert recovered == plaintext

    def test_wrong_key_raises_invalid_password(self) -> None:
        salt = generate_salt()
        nonce = generate_nonce()
        aad = b"hdr"
        plaintext = b"secret"
        ciphertext = encrypt(plaintext, derive_key("right", salt), nonce, aad)
        wrong_key = derive_key("wrong", salt)
        with pytest.raises(InvalidPasswordError):
            decrypt(ciphertext, wrong_key, nonce, aad)

    def test_tampered_ciphertext_raises(self) -> None:
        key = derive_key("p", generate_salt())
        nonce = generate_nonce()
        aad = b"hdr"
        ciphertext = bytearray(encrypt(b"data", key, nonce, aad))
        ciphertext[0] ^= 0x01  # flip a bit
        with pytest.raises(InvalidPasswordError):
            decrypt(bytes(ciphertext), key, nonce, aad)

    def test_tampered_aad_raises(self) -> None:
        key = derive_key("p", generate_salt())
        nonce = generate_nonce()
        ciphertext = encrypt(b"data", key, nonce, b"original-header")
        with pytest.raises(InvalidPasswordError):
            decrypt(ciphertext, key, nonce, b"different-header")

    def test_wrong_nonce_raises(self) -> None:
        key = derive_key("p", generate_salt())
        nonce_enc = generate_nonce()
        nonce_dec = generate_nonce()
        ciphertext = encrypt(b"data", key, nonce_enc, b"hdr")
        with pytest.raises(InvalidPasswordError):
            decrypt(ciphertext, key, nonce_dec, b"hdr")
