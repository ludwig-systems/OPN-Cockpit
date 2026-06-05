"""Cert-Parsing-Helfer fuer den Custom-Trust-Store.

Liest PEM-Bloecke per ``cryptography`` (schon Runtime-Dep wegen
AES-Vault) und extrahiert die UI-relevanten Metadaten + den
Fingerprint, der als stabiler Identifier zum Loeschen dient.

Module ist DOMAIN-frei: keine FastAPI-, Pydantic- oder Vault-Imports
hier. Aufrufer (web/api/vaults.py) packt die Werte in Pydantic-Modelle.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.x509.oid import ExtensionOID, NameOID


_PEM_BLOCK_RE = re.compile(
    r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----",
    flags=re.DOTALL,
)


@dataclass(frozen=True, slots=True)
class CertMetadata:
    """UI-relevante Felder eines Trust-Eintrags."""

    pem: str
    fingerprint_sha256: str
    subject_cn: str
    issuer_cn: str
    not_before_iso: str
    not_after_iso: str
    days_until_expiry: int | None
    is_ca: bool
    self_signed: bool


def split_pem_blocks(raw: str) -> list[str]:
    """Splittet einen Multi-PEM-Eintrag in einzelne ``BEGIN/END``-Bloecke.

    Akzeptiert Whitespace + Leerzeilen drumherum. Wenn keine Bloecke
    gefunden werden, liefert die Funktion eine leere Liste — Aufrufer
    behandelt das als "kein gueltiges PEM".
    """
    if not raw:
        return []
    blocks = _PEM_BLOCK_RE.findall(raw)
    return [b.strip() for b in blocks]


def parse_cert(pem: str) -> CertMetadata:
    """Parst ein einzelnes PEM-Cert und liefert die Metadaten.

    Wirft ``ValueError``, wenn das PEM kein gueltiges X.509 ist.
    """
    if not pem or not pem.strip():
        raise ValueError("Leerer PEM-Block.")
    try:
        cert = x509.load_pem_x509_certificate(pem.encode("ascii"))
    except (ValueError, TypeError) as exc:
        raise ValueError(f"PEM nicht lesbar: {exc}") from exc

    fingerprint = cert.fingerprint(hashes.SHA256()).hex().upper()
    fingerprint_colon = ":".join(
        fingerprint[i : i + 2] for i in range(0, len(fingerprint), 2)
    )

    subject_cn = _common_name(cert.subject) or _stringify_name(cert.subject)
    issuer_cn = _common_name(cert.issuer) or _stringify_name(cert.issuer)

    # cryptography 42+ liefert tz-aware datetimes (Aenderung in 42.0). Wir
    # behandeln beide Formen damit alte Installationen weiter funktionieren.
    not_before = _as_utc(cert.not_valid_before_utc
                         if hasattr(cert, "not_valid_before_utc")
                         else cert.not_valid_before)
    not_after = _as_utc(cert.not_valid_after_utc
                        if hasattr(cert, "not_valid_after_utc")
                        else cert.not_valid_after)

    now = datetime.now(UTC)
    delta_days = (not_after - now).days

    is_ca = _is_ca_certificate(cert)
    self_signed = cert.subject == cert.issuer

    return CertMetadata(
        pem=pem.strip() + "\n",
        fingerprint_sha256=fingerprint_colon,
        subject_cn=subject_cn,
        issuer_cn=issuer_cn,
        not_before_iso=not_before.isoformat(),
        not_after_iso=not_after.isoformat(),
        days_until_expiry=delta_days,
        is_ca=is_ca,
        self_signed=self_signed,
    )


def parse_all(raw: str) -> tuple[list[CertMetadata], list[str]]:
    """Bequemer Wrapper: nimmt einen Multi-PEM-String + liefert
    (geparsed, fehler-strings). Aufrufer kann z. B. die Preview rendern
    ohne selbst zu splitten.
    """
    parsed: list[CertMetadata] = []
    errors: list[str] = []
    blocks = split_pem_blocks(raw)
    if not blocks:
        errors.append(
            "Kein '-----BEGIN CERTIFICATE-----'-Block gefunden. "
            "Erwartet ein PEM-codiertes X.509-Zertifikat.",
        )
        return parsed, errors
    for i, block in enumerate(blocks, start=1):
        try:
            parsed.append(parse_cert(block))
        except ValueError as exc:
            errors.append(f"PEM-Block #{i}: {exc}")
    return parsed, errors


def fingerprint_of_pem(pem: str) -> str | None:
    """Fingerprint-Lookup ohne weitere Metadaten - fuer den DELETE-Pfad."""
    try:
        meta = parse_cert(pem)
    except ValueError:
        return None
    return meta.fingerprint_sha256


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _common_name(name: x509.Name) -> str:
    for attr in name.get_attributes_for_oid(NameOID.COMMON_NAME):
        value = attr.value
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _stringify_name(name: x509.Name) -> str:
    return ", ".join(f"{a.oid._name}={a.value}" for a in name)  # noqa: SLF001


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _is_ca_certificate(cert: x509.Certificate) -> bool:
    try:
        bc = cert.extensions.get_extension_for_oid(
            ExtensionOID.BASIC_CONSTRAINTS,
        )
    except x509.ExtensionNotFound:
        return False
    return bool(bc.value.ca)


__all__ = [
    "CertMetadata",
    "fingerprint_of_pem",
    "parse_all",
    "parse_cert",
    "split_pem_blocks",
]
