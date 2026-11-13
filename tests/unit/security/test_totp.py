"""Tests fuer ``opn_cockpit.security.totp``."""

from __future__ import annotations

import time

import pytest

from opn_cockpit.security import totp as totp_mod


class TestSecretGeneration:
    def test_secret_is_base32_and_unique(self) -> None:
        s1 = totp_mod.generate_secret()
        s2 = totp_mod.generate_secret()
        # pyotp default ist 32 base32 chars (160 bit)
        assert len(s1) == 32
        assert s1 != s2
        # Base32-Alphabet
        assert all(c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567=" for c in s1)


class TestProvisioningUri:
    def test_uri_contains_user_and_issuer(self) -> None:
        secret = totp_mod.generate_secret()
        uri = totp_mod.provisioning_uri(secret, "alice")
        assert uri.startswith("otpauth://totp/")
        assert "alice" in uri
        assert "OPN-Cockpit" in uri
        assert secret in uri


class TestVerifyCode:
    def test_current_code_passes(self) -> None:
        import pyotp

        secret = totp_mod.generate_secret()
        code = pyotp.TOTP(secret).now()
        assert totp_mod.verify_code(secret, code) is True

    def test_code_with_spaces_passes(self) -> None:
        import pyotp

        secret = totp_mod.generate_secret()
        code = pyotp.TOTP(secret).now()
        # Authenticator-Layout "123 456"
        spaced = code[:3] + " " + code[3:]
        assert totp_mod.verify_code(secret, spaced) is True

    def test_wrong_code_fails(self) -> None:
        secret = totp_mod.generate_secret()
        assert totp_mod.verify_code(secret, "000000") is False

    def test_short_code_fails(self) -> None:
        secret = totp_mod.generate_secret()
        assert totp_mod.verify_code(secret, "12345") is False

    def test_non_digit_code_fails(self) -> None:
        secret = totp_mod.generate_secret()
        assert totp_mod.verify_code(secret, "abcdef") is False

    def test_empty_secret_fails(self) -> None:
        assert totp_mod.verify_code("", "123456") is False


class TestBackupCodes:
    def test_generates_eight_unique_codes(self) -> None:
        codes = totp_mod.generate_backup_codes()
        assert len(codes) == totp_mod.BACKUP_CODE_COUNT == 8
        assert len(set(codes)) == 8  # alle unterschiedlich
        # Format ABCDE-FGHIJ
        for c in codes:
            assert len(c) == 11  # 10 chars + Bindestrich
            assert c[5] == "-"

    def test_hash_is_deterministic_and_normalisiert(self) -> None:
        h1 = totp_mod.hash_backup_code("ABCDE-FGHIJ")
        h2 = totp_mod.hash_backup_code("abcde fghij")
        # Bindestrich + Whitespace ignorieren, Case-up
        assert h1 == h2

    def test_verify_consumes_used_code(self) -> None:
        codes = totp_mod.generate_backup_codes()
        hashes = [totp_mod.hash_backup_code(c) for c in codes]
        consumed, remaining = totp_mod.verify_backup_code(codes[3], hashes)
        assert consumed is True
        assert len(remaining) == 7
        assert totp_mod.hash_backup_code(codes[3]) not in remaining

    def test_verify_rejects_unknown_code(self) -> None:
        codes = totp_mod.generate_backup_codes()
        hashes = [totp_mod.hash_backup_code(c) for c in codes]
        consumed, remaining = totp_mod.verify_backup_code("ZZZZZ-ZZZZZ", hashes)
        assert consumed is False
        assert remaining == hashes


class TestChallenge:
    def test_roundtrip(self) -> None:
        secret = b"secret-bytes-32-bytes-long-abcdef"
        ch = totp_mod.issue_challenge(42, secret)
        token = ch.to_token()
        parsed = totp_mod.TotpChallenge.from_token(token)
        assert parsed is not None
        assert parsed.user_id == 42
        assert totp_mod.verify_challenge(parsed, secret) is True

    def test_wrong_secret_fails(self) -> None:
        secret_a = b"a" * 32
        secret_b = b"b" * 32
        ch = totp_mod.issue_challenge(1, secret_a)
        parsed = totp_mod.TotpChallenge.from_token(ch.to_token())
        assert parsed is not None
        assert totp_mod.verify_challenge(parsed, secret_b) is False

    def test_expired_fails(self) -> None:
        secret = b"x" * 32
        ch = totp_mod.issue_challenge(7, secret)
        parsed = totp_mod.TotpChallenge.from_token(ch.to_token())
        assert parsed is not None
        future = ch.issued_at_unix + totp_mod.CHALLENGE_TTL_S + 1
        assert totp_mod.verify_challenge(parsed, secret, now_unix=future) is False

    def test_future_issued_fails(self) -> None:
        # User-Clock vorgestellt — issued_at in der Server-Zukunft
        secret = b"x" * 32
        ch = totp_mod.TotpChallenge(
            user_id=1,
            issued_at_unix=int(time.time()) + 600,
            signature="deadbeef",
        )
        # Wir benutzen issue_challenge nicht, weil wir explizit issued_at
        # in der Zukunft setzen wollen. Signatur ist auch falsch — beide
        # Conditions → False.
        assert totp_mod.verify_challenge(ch, secret) is False

    def test_token_format_robust(self) -> None:
        assert totp_mod.TotpChallenge.from_token("") is None
        assert totp_mod.TotpChallenge.from_token("garbage") is None
        assert totp_mod.TotpChallenge.from_token("1:two:three") is None
        assert totp_mod.TotpChallenge.from_token("1:2") is None


class TestChallengeSecretLoader:
    def test_env_hex_secret(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(
            "OPNCOCKPIT_TOTP_CHALLENGE_SECRET",
            "00" * 32,
        )
        s = totp_mod.load_or_generate_challenge_secret()
        assert s == b"\x00" * 32

    def test_env_utf8_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(
            "OPNCOCKPIT_TOTP_CHALLENGE_SECRET",
            "nicht-hex-aber-string",
        )
        s = totp_mod.load_or_generate_challenge_secret()
        assert s == b"nicht-hex-aber-string"
