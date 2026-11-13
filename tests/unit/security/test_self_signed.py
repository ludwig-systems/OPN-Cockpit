"""Tests fuer security.self_signed - Cert-Generierung + Regeneration-Check."""

from __future__ import annotations

from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from opn_cockpit.security.self_signed import (
    AUTO_CERT_FILENAME,
    AUTO_FINGERPRINT_FILENAME,
    AUTO_KEY_FILENAME,
    DEFAULT_VALID_DAYS,
    AutoCertPaths,
    generate_self_signed,
    needs_regeneration,
    read_fingerprint,
)


class TestGenerateSelfSigned:
    def test_writes_all_three_files(self, tmp_path: Path) -> None:
        paths = AutoCertPaths.from_dir(tmp_path)
        result = generate_self_signed(paths, hostname="test-host")
        assert paths.cert.exists()
        assert paths.key.exists()
        assert paths.fingerprint.exists()
        assert result.cert_path == paths.cert
        assert result.key_path == paths.key

    def test_uses_default_validity(self, tmp_path: Path) -> None:
        paths = AutoCertPaths.from_dir(tmp_path)
        result = generate_self_signed(paths, hostname="test-host")
        assert result.valid_days == DEFAULT_VALID_DAYS

    def test_cert_is_parseable_x509(self, tmp_path: Path) -> None:
        paths = AutoCertPaths.from_dir(tmp_path)
        generate_self_signed(paths, hostname="test-host")
        cert = x509.load_pem_x509_certificate(paths.cert.read_bytes())
        # Fingerprint aus dem File matcht das echt geladene Cert
        fp = cert.fingerprint(_sha256_algo()).hex().upper()
        fp_colon = ":".join(fp[i : i + 2] for i in range(0, len(fp), 2))
        assert paths.fingerprint.read_text(encoding="ascii").strip() == fp_colon

    def test_key_is_ec_p256(self, tmp_path: Path) -> None:
        paths = AutoCertPaths.from_dir(tmp_path)
        generate_self_signed(paths, hostname="test-host")
        key = serialization.load_pem_private_key(paths.key.read_bytes(), password=None)
        assert isinstance(key, ec.EllipticCurvePrivateKey)
        assert isinstance(key.curve, ec.SECP256R1)

    def test_san_includes_localhost(self, tmp_path: Path) -> None:
        paths = AutoCertPaths.from_dir(tmp_path)
        generate_self_signed(paths, hostname="test-host")
        cert = x509.load_pem_x509_certificate(paths.cert.read_bytes())
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        dns_names = san.value.get_values_for_type(x509.DNSName)
        assert "localhost" in dns_names
        assert "test-host" in dns_names

    def test_san_includes_hostname_local_when_bare(self, tmp_path: Path) -> None:
        paths = AutoCertPaths.from_dir(tmp_path)
        generate_self_signed(paths, hostname="myhost")
        cert = x509.load_pem_x509_certificate(paths.cert.read_bytes())
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        dns_names = san.value.get_values_for_type(x509.DNSName)
        assert "myhost.local" in dns_names

    def test_san_no_local_suffix_for_fqdn(self, tmp_path: Path) -> None:
        paths = AutoCertPaths.from_dir(tmp_path)
        generate_self_signed(paths, hostname="host.example.com")
        cert = x509.load_pem_x509_certificate(paths.cert.read_bytes())
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        dns_names = san.value.get_values_for_type(x509.DNSName)
        assert "host.example.com" in dns_names
        assert "host.example.com.local" not in dns_names

    def test_san_includes_loopback_ips(self, tmp_path: Path) -> None:
        paths = AutoCertPaths.from_dir(tmp_path)
        generate_self_signed(paths, hostname="test-host")
        cert = x509.load_pem_x509_certificate(paths.cert.read_bytes())
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        ips = {str(ip) for ip in san.value.get_values_for_type(x509.IPAddress)}
        assert "127.0.0.1" in ips
        assert "::1" in ips

    def test_returned_fingerprint_format(self, tmp_path: Path) -> None:
        paths = AutoCertPaths.from_dir(tmp_path)
        result = generate_self_signed(paths, hostname="test-host")
        # Format: 32 Doppelpunkte-getrennte Byte-Paare
        parts = result.fingerprint_sha256.split(":")
        assert len(parts) == 32
        for p in parts:
            assert len(p) == 2
            int(p, 16)  # jedes Byte ist hex

    def test_short_validity_works(self, tmp_path: Path) -> None:
        paths = AutoCertPaths.from_dir(tmp_path)
        result = generate_self_signed(paths, hostname="test-host", valid_days=5)
        assert result.valid_days == 5

    def test_default_hostname_when_none(self, tmp_path: Path) -> None:
        paths = AutoCertPaths.from_dir(tmp_path)
        # Sollte nicht crashen wenn hostname=None
        result = generate_self_signed(paths, hostname=None)
        assert result.subject_cn == "opn-cockpit"


class TestNeedsRegeneration:
    def test_missing_cert_true(self, tmp_path: Path) -> None:
        paths = AutoCertPaths.from_dir(tmp_path)
        assert needs_regeneration(paths.cert) is True

    def test_fresh_cert_false(self, tmp_path: Path) -> None:
        paths = AutoCertPaths.from_dir(tmp_path)
        generate_self_signed(paths, hostname="test-host")
        # Frisch generiertes Cert hat 397 Tage - Threshold 30 -> False
        assert needs_regeneration(paths.cert, threshold_days=30) is False

    def test_high_threshold_true(self, tmp_path: Path) -> None:
        paths = AutoCertPaths.from_dir(tmp_path)
        generate_self_signed(paths, hostname="test-host")
        # Threshold 500 > 397 Tage -> True
        assert needs_regeneration(paths.cert, threshold_days=500) is True

    def test_corrupt_cert_true(self, tmp_path: Path) -> None:
        paths = AutoCertPaths.from_dir(tmp_path)
        paths.cert.write_bytes(b"not a real PEM")
        assert needs_regeneration(paths.cert) is True


class TestReadFingerprint:
    def test_returns_cached_value(self, tmp_path: Path) -> None:
        paths = AutoCertPaths.from_dir(tmp_path)
        result = generate_self_signed(paths, hostname="test-host")
        assert read_fingerprint(paths) == result.fingerprint_sha256

    def test_fallback_to_cert_parse(self, tmp_path: Path) -> None:
        paths = AutoCertPaths.from_dir(tmp_path)
        result = generate_self_signed(paths, hostname="test-host")
        # Fingerprint-Datei loeschen
        paths.fingerprint.unlink()
        # read_fingerprint sollte trotzdem funktionieren
        assert read_fingerprint(paths) == result.fingerprint_sha256

    def test_returns_empty_when_no_cert(self, tmp_path: Path) -> None:
        paths = AutoCertPaths.from_dir(tmp_path)
        assert read_fingerprint(paths) == ""


class TestAutoCertPaths:
    def test_uses_expected_filenames(self, tmp_path: Path) -> None:
        paths = AutoCertPaths.from_dir(tmp_path)
        assert paths.cert.name == AUTO_CERT_FILENAME
        assert paths.key.name == AUTO_KEY_FILENAME
        assert paths.fingerprint.name == AUTO_FINGERPRINT_FILENAME


def _sha256_algo():
    from cryptography.hazmat.primitives import hashes

    return hashes.SHA256()
