"""Selbstsignierte Server-Zertifikate fuer Cockpit-HTTPS.

Cockpit laeuft ab v0.10 per Default HTTPS. Ohne User-Config wird beim
Boot ein Cert generiert (EC P-256, 397 Tage, moderne Suite), gespeichert
unter ``<app_data>/server_tls/auto-cert.pem`` + ``auto-cert.key.pem``.
Der SHA-256-Fingerprint landet in ``auto-cert.fingerprint`` zum
schnellen Lesen ohne PEM-Parse.

Wenn der User spaeter ein echtes Cert (Let's Encrypt, interne CA)
hochlaedt, greift der Custom-Cert-Pfad vor dem Auto-Cert - das Auto-
File bleibt liegen als Fallback bei Custom-DELETE.

Modul ist DOMAIN-frei: keine FastAPI-/Vault-/HTTPX-Imports.
"""

from __future__ import annotations

import contextlib
import ipaddress
import logging
import os
import secrets
import socket
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

_log = logging.getLogger(__name__)

# Cert-Parameter: EC P-256, 397 Tage. Beides bewusst gewaehlt.
# * EC P-256 statt RSA-2048: kleiner, schneller, moderne Suite. Kein
#   uralt-Client soll gegen Cockpit sprechen muessen; das laeuft im
#   Admin-LAN gegen moderne Browser.
# * 397 Tage: Safari/Chrome akzeptieren max. 398 Tage fuer neue Certs
#   (BR 1.5.1). Etwas Reserve. Force-Rotation ~alle 12 Monate.
DEFAULT_VALID_DAYS = 397
COMMON_NAME = "opn-cockpit"

# Datei-Namen unter ``<app_data>/server_tls/`` - unterschieden vom
# Custom-Upload (``cert.pem``/``key.pem``) damit beide gleichzeitig
# liegen koennen.
AUTO_CERT_FILENAME = "auto-cert.pem"
AUTO_KEY_FILENAME = "auto-cert.key.pem"
AUTO_FINGERPRINT_FILENAME = "auto-cert.fingerprint"


@dataclass(frozen=True, slots=True)
class AutoCertPaths:
    """Dateipfade fuer das Auto-Cert-Set."""

    cert: Path
    key: Path
    fingerprint: Path

    @classmethod
    def from_dir(cls, directory: Path) -> AutoCertPaths:
        return cls(
            cert=directory / AUTO_CERT_FILENAME,
            key=directory / AUTO_KEY_FILENAME,
            fingerprint=directory / AUTO_FINGERPRINT_FILENAME,
        )


@dataclass(frozen=True, slots=True)
class GeneratedCert:
    """Ergebnis von :func:`generate_self_signed` - fuer Log + Audit."""

    cert_path: Path
    key_path: Path
    fingerprint_sha256: str          # AB:CD:EF:...
    fingerprint_sha256_hex: str      # ABCDEF... (ohne Trennzeichen)
    subject_cn: str
    not_before_iso: str
    not_after_iso: str
    valid_days: int
    san_entries: tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def generate_self_signed(
    paths: AutoCertPaths,
    *,
    hostname: str | None = None,
    valid_days: int = DEFAULT_VALID_DAYS,
) -> GeneratedCert:
    """Erzeugt Auto-Cert + Auto-Key und schreibt sie mit 0600 / 0644.

    ``hostname`` steuert die SAN-DNS-Eintraege; wenn None, wird
    ``socket.gethostname()`` verwendet. IP-SANs werden aus den
    lokalen Interfaces (via ``socket.getaddrinfo``) gesammelt, plus
    Loopback als Sicherheitsnetz.

    Ueberschreibt bestehende Dateien atomar via ``<name>.tmp`` +
    ``os.replace``. Bei Fehlern raised eine :class:`OSError`.
    """
    if hostname is None:
        hostname = socket.gethostname() or "opn-cockpit-host"

    private_key = ec.generate_private_key(ec.SECP256R1())
    subject = issuer = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, COMMON_NAME)]
    )

    now = datetime.now(UTC)
    not_before = now - timedelta(minutes=5)   # Clock-Skew-Puffer
    not_after = now + timedelta(days=int(valid_days))

    san_entries = _collect_san(hostname)
    san_display = _san_to_strings(san_entries)

    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(int.from_bytes(secrets.token_bytes(16), "big"))
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(x509.SubjectAlternativeName(san_entries), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,   # bei EC nicht nutzbar
                data_encipherment=False,
                key_agreement=True,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(private_key, hashes.SHA256())
    )

    fingerprint_bytes = cert.fingerprint(hashes.SHA256())
    fingerprint_hex = fingerprint_bytes.hex().upper()
    fingerprint_colon = ":".join(
        fingerprint_hex[i : i + 2] for i in range(0, len(fingerprint_hex), 2)
    )

    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )

    paths.cert.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_bytes(paths.cert, cert_pem, mode=0o644)
    _atomic_write_bytes(paths.key, key_pem, mode=0o600)
    _atomic_write_bytes(
        paths.fingerprint,
        (fingerprint_colon + "\n").encode("ascii"),
        mode=0o644,
    )

    return GeneratedCert(
        cert_path=paths.cert,
        key_path=paths.key,
        fingerprint_sha256=fingerprint_colon,
        fingerprint_sha256_hex=fingerprint_hex,
        subject_cn=COMMON_NAME,
        not_before_iso=not_before.isoformat(),
        not_after_iso=not_after.isoformat(),
        valid_days=int(valid_days),
        san_entries=tuple(san_display),
    )


def needs_regeneration(cert_path: Path, *, threshold_days: int = 30) -> bool:
    """True wenn Cert fehlt, unlesbar oder in ``<= threshold_days`` ablaeuft.

    Fehler beim Parsen fuehren zu True - ein kaputtes Cert wird sicher
    ersetzt statt einen Boot-Loop zu erzeugen.
    """
    if not cert_path.exists():
        return True
    try:
        pem_bytes = cert_path.read_bytes()
        cert = x509.load_pem_x509_certificate(pem_bytes)
    except (OSError, ValueError):
        return True
    not_after = _get_not_after(cert)
    days_left = (not_after - datetime.now(UTC)).days
    return days_left <= int(threshold_days)


def read_fingerprint(paths: AutoCertPaths) -> str:
    """Liest den zwischengespeicherten Fingerprint. Fallback: parse Cert.

    Liefert leeren String wenn beides fehlschlaegt.
    """
    if paths.fingerprint.exists():
        try:
            content = paths.fingerprint.read_text(encoding="ascii").strip()
            if content:
                return content
        except OSError:
            pass
    if paths.cert.exists():
        try:
            cert = x509.load_pem_x509_certificate(paths.cert.read_bytes())
            fp = cert.fingerprint(hashes.SHA256()).hex().upper()
            return ":".join(fp[i : i + 2] for i in range(0, len(fp), 2))
        except (OSError, ValueError):
            pass
    return ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _collect_san(hostname: str) -> list[x509.GeneralName]:
    """Sammelt SAN-Eintraege fuer das Auto-Cert.

    Immer dabei: ``localhost``, ``127.0.0.1``, ``::1``. Wenn hostname
    gesetzt und != localhost: ``<hostname>`` + ``<hostname>.local``
    (mDNS-Convention). Zusaetzlich alle nicht-Loopback-IPs des Hosts
    (bestmoeglich via ``getaddrinfo``).
    """
    dns_names: set[str] = {"localhost"}
    if hostname and hostname.lower() != "localhost":
        dns_names.add(hostname)
        if "." not in hostname:
            dns_names.add(f"{hostname}.local")

    ip_addrs: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
    for lo in ("127.0.0.1", "::1"):
        with contextlib.suppress(ValueError):
            ip_addrs.add(ipaddress.ip_address(lo))

    try:
        infos = socket.getaddrinfo(hostname, None)
    except (socket.gaierror, OSError, UnicodeError):
        infos = []
    for _fam, _typ, _proto, _canon, sock_addr in infos:
        raw_addr = sock_addr[0]
        # IPv6 Scoped-Suffix strippen ("fe80::1%eth0")
        raw_addr = raw_addr.split("%", 1)[0]
        try:
            ip = ipaddress.ip_address(raw_addr)
        except ValueError:
            continue
        if ip.is_link_local:
            continue
        ip_addrs.add(ip)

    entries: list[x509.GeneralName] = [x509.DNSName(name) for name in sorted(dns_names)]
    entries.extend(x509.IPAddress(ip) for ip in sorted(ip_addrs, key=str))
    return entries


def _san_to_strings(entries: list[x509.GeneralName]) -> list[str]:
    out: list[str] = []
    for entry in entries:
        if isinstance(entry, x509.DNSName):
            out.append(f"DNS:{entry.value}")
        elif isinstance(entry, x509.IPAddress):
            out.append(f"IP:{entry.value}")
        else:
            out.append(str(entry))
    return out


def _get_not_after(cert: x509.Certificate) -> datetime:
    """cryptography 42+ liefert tz-aware; Fallback fuer altere Versionen."""
    raw = getattr(cert, "not_valid_after_utc", None) or cert.not_valid_after
    if raw.tzinfo is None:
        return raw.replace(tzinfo=UTC)
    return raw.astimezone(UTC)


def _atomic_write_bytes(path: Path, data: bytes, *, mode: int) -> None:
    """Schreibt ``data`` atomar nach ``path`` mit den gewuenschten Rechten.

    Auf Windows ist ``os.chmod`` weitgehend ein No-Op fuer 0600 - aber
    beim Deploy in Linux-Container schuetzt es den Key vor unberechtigten
    Lesern. Wir setzen es also immer.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("wb") as fh:
        fh.write(data)
    try:
        os.chmod(tmp, mode)
    except OSError:
        # Rechte-Setzen scheitert auf manchen exotischen FS - kein Blocker.
        _log.debug("chmod %s auf %o fehlgeschlagen", tmp, mode)
    os.replace(tmp, path)


__all__ = [
    "AUTO_CERT_FILENAME",
    "AUTO_FINGERPRINT_FILENAME",
    "AUTO_KEY_FILENAME",
    "AutoCertPaths",
    "DEFAULT_VALID_DAYS",
    "GeneratedCert",
    "generate_self_signed",
    "needs_regeneration",
    "read_fingerprint",
]
