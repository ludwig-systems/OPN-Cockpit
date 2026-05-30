"""HMAC-Hash-Chain fuer Audit-Log-Tamper-Evidence (v4-Pass 3, Security-Audit #11).

Jeder neue Audit-Eintrag enthaelt:

* ``prev_hash``: Hash des unmittelbar vorherigen Eintrags (oder ein
  Anker-Wert beim ersten Eintrag)
* ``this_hash``: HMAC(server_secret, prev_hash || canonical(record))

So baut sich eine Kette auf, die rueckwirkend nachpruefbar ist: wer
einen Eintrag aendert oder loescht, zerbricht alle nachfolgenden
Hashes. Das schuetzt nicht gegen einen Angreifer, der das Server-
Secret kennt — aber gegen Filesystem-Tampering durch einen User mit
Lese/Schreib-Zugriff auf die Audit-Datei oder DB.

Server-Secret kommt aus ``OPNCOCKPIT_AUDIT_SECRET`` oder wird beim
ersten Start in ``$OPNCOCKPIT_DATA_DIR/audit-secret`` generiert
(0o600 Permissions). Wer das File hat, hat den Schluessel — entsprechend
sollte das nur dem Server-User gehoeren.

Verifikation: ``verify_chain(records, secret)`` laeuft chronologisch
durch alle Eintraege und prueft, dass jeder Hash zur Kette passt.
Liefert eine Liste der gebrochenen Indices.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

from opn_cockpit.audit.log import AuditRecord
from opn_cockpit.config import get_app_data_dir

AUDIT_SECRET_ENV = "OPNCOCKPIT_AUDIT_SECRET"
AUDIT_SECRET_FILENAME = "audit-secret"
GENESIS_HASH = "00" * 32  # 256 Nullbits als Anker fuer den ersten Eintrag


def default_secret_path() -> Path:
    return get_app_data_dir() / AUDIT_SECRET_FILENAME


def load_or_generate_secret() -> bytes:
    """Liest oder erzeugt das HMAC-Secret.

    Reihenfolge:
    1. ``OPNCOCKPIT_AUDIT_SECRET`` als Hex-String
    2. ``$OPNCOCKPIT_DATA_DIR/audit-secret`` (Bytes)
    3. Wird neu generiert + gespeichert (32 Bytes Random)

    Permission: bei neu angelegtem File 0o600 (nur Owner). Existierende
    Files bleiben unberuehrt — der Server-Admin entscheidet.
    """
    env = os.environ.get(AUDIT_SECRET_ENV, "").strip()
    if env:
        try:
            return bytes.fromhex(env)
        except ValueError:
            # Nicht-Hex Env-Wert: als UTF-8 nehmen
            return env.encode("utf-8")
    path = default_secret_path()
    if path.exists():
        return path.read_bytes()
    path.parent.mkdir(parents=True, exist_ok=True)
    secret = secrets.token_bytes(32)
    path.write_bytes(secret)
    # Best-effort Permission-Setzung (Windows-fs ignoriert das stillschweigend)
    with contextlib.suppress(OSError):
        path.chmod(0o600)
    return secret


@dataclass(frozen=True, slots=True)
class ChainedRecord:
    """Audit-Record + Hash-Chain-Felder."""

    record: AuditRecord
    prev_hash: str
    this_hash: str


def canonical_record(record: AuditRecord) -> bytes:
    """JSON-Canonicalisierung — Reihenfolge der Keys ist garantiert.

    Wird zum HMAC-Input fuer ``this_hash``. Felder, die nicht im
    Record stehen, kommen explizit als ``null`` rein — sonst koennte
    ein Angreifer das gleiche Hash erzeugen indem er Felder weglaesst.
    """
    payload = record.to_dict()
    # Event-Enum als String
    if "event" in payload:
        payload["event"] = str(payload["event"])
    return json.dumps(
        payload, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def compute_this_hash(
    secret: bytes, prev_hash: str, record: AuditRecord,
) -> str:
    """HMAC-SHA256 ueber prev_hash + canonical_record."""
    mac = hmac.new(secret, digestmod=hashlib.sha256)
    mac.update(prev_hash.encode("ascii"))
    mac.update(canonical_record(record))
    return mac.hexdigest()


def verify_chain(
    chained: list[ChainedRecord], secret: bytes,
) -> list[int]:
    """Prueft die Hash-Chain. Liefert Liste der gebrochenen Index-Positionen.

    Leere Liste = alles OK. Bei einem Bruch bei Index i sind in der
    Praxis auch alle nachfolgenden Indices "broken", weil prev_hash
    nicht mehr stimmt. Wir geben trotzdem alle zurueck — das macht
    Forensik einfacher.
    """
    broken: list[int] = []
    expected_prev = GENESIS_HASH
    for i, entry in enumerate(chained):
        if entry.prev_hash != expected_prev:
            broken.append(i)
        recomputed = compute_this_hash(secret, entry.prev_hash, entry.record)
        if recomputed != entry.this_hash and i not in broken:
            broken.append(i)
        expected_prev = entry.this_hash
    return broken


__all__ = [
    "AUDIT_SECRET_ENV",
    "AUDIT_SECRET_FILENAME",
    "GENESIS_HASH",
    "ChainedRecord",
    "canonical_record",
    "compute_this_hash",
    "default_secret_path",
    "load_or_generate_secret",
    "verify_chain",
]
