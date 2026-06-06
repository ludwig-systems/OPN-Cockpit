"""TOTP (RFC 6238) als optionale 2FA fuer den Multi-User-Server-Modus.

Pro User:

* ``totp_secret`` — Base32-encoded HMAC-Secret (160 bit / 32 base32 chars).
  Gespeichert in der User-DB. Wer Lese-Zugriff auf die DB hat, kann
  Codes errechnen — das ist ein bewusst akzeptiertes Risiko (vergleichbar
  mit Argon2-Hashes: stark genug fuer Server-Compromise-Forensik, aber
  kein Geheimnis ggue. dem Service-User selbst).
* ``totp_enabled`` — Schutz-Flag. Setup-Flow: Secret generieren ->
  User bestaetigt mit aktuellem Code -> Flag wird gesetzt.
* ``totp_backup_codes`` — 8 einmalige Recovery-Codes als Argon2-Hashes.
  Beim Verbrauch werden sie geloescht.

Verifikation:

* Code-Pruefung mit ``valid_window=1`` -> akzeptiert das jetzige UND das
  unmittelbar vergangene 30-s-Fenster. Schutz gegen leichte Clock-Skew
  zwischen Server und Authenticator-App.
* Backup-Code: 10-stellig alphanumerisch, mit Bindestrich nach 5 Zeichen
  zur Lesbarkeit.

Login-Flow (auth.py):

1. ``POST /api/auth/login`` mit Username+Passwort -> wenn TOTP enabled,
   antwortet der Server mit ``totp_required=true`` + ``totp_challenge``
   (ein kurzlebiges signiertes Token, das nur fuer Schritt 2 gilt).
2. ``POST /api/auth/login/totp`` mit ``challenge`` + ``code`` -> echte
   Session-Token-Antwort.

Im Single-User-Modus ist TOTP nicht aktiv — dort gibt's keinen User,
gegen den 2FA verankert wuerde. Master-PW + Inaktivitaets-Lock + lokaler
PAW reichen.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import pyotp

from opn_cockpit.config import get_app_data_dir

ISSUER = "OPN-Cockpit"
TOTP_DIGITS = 6
TOTP_INTERVAL_S = 30
TOTP_VALID_WINDOW = 1  # +/- 1 Schritt = +/-30s Clock-Skew toleriert
BACKUP_CODE_COUNT = 8
BACKUP_CODE_LEN = 10  # 10-stellig alphanum
BACKUP_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # ohne I/O/0/1
CHALLENGE_TTL_S = 5 * 60  # 5 min Frist zwischen Schritt 1 und 2
CHALLENGE_VERSION: Final[bytes] = b"v1"


# ---------------------------------------------------------------------------
# Secret-Generation + Provisioning-URI
# ---------------------------------------------------------------------------


def generate_secret() -> str:
    """Erzeugt ein frisches Base32-Secret (160 bit Entropy)."""
    return pyotp.random_base32()


def provisioning_uri(secret: str, username: str) -> str:
    """Baut die ``otpauth://``-URI fuer Authenticator-Apps.

    Format: ``otpauth://totp/<Issuer>:<User>?secret=<...>&issuer=<Issuer>``.
    Authenticator-Apps (Google, Microsoft, Aegis, Bitwarden) rendern daraus
    den QR-Code; Frontends koennen das URI per JS-Library lokal in einen
    QR umsetzen — kein Server-Side-QR-Rendering noetig.
    """
    return pyotp.TOTP(secret, digits=TOTP_DIGITS, interval=TOTP_INTERVAL_S).provisioning_uri(
        name=username,
        issuer_name=ISSUER,
    )


def verify_code(secret: str, code: str) -> bool:
    """Verifiziert einen TOTP-Code mit Tolerance fuer +/- 1 Schritt Clock-Skew.

    Returns:
        ``True`` bei gueltigem Code, ``False`` sonst. Whitespace im Code
        wird entfernt, damit Authenticator-Layouts mit Leerzeichen
        (``123 456``) trotzdem matchen.
    """
    if not secret or not code:
        return False
    cleaned = "".join(c for c in code if not c.isspace())
    if not cleaned.isdigit() or len(cleaned) != TOTP_DIGITS:
        return False
    return pyotp.TOTP(secret, digits=TOTP_DIGITS, interval=TOTP_INTERVAL_S).verify(
        cleaned, valid_window=TOTP_VALID_WINDOW,
    )


# ---------------------------------------------------------------------------
# Backup-Codes
# ---------------------------------------------------------------------------


def generate_backup_codes() -> list[str]:
    """Erzeugt 8 Klartext-Backup-Codes (einmalig anzuzeigen).

    Format: ``ABCDE-FGHIJ`` (10 Zeichen + Bindestrich nach 5). Bindestrich
    ist optisch — beim Verifizieren wird er entfernt.
    """
    codes: list[str] = []
    for _ in range(BACKUP_CODE_COUNT):
        raw = "".join(secrets.choice(BACKUP_CODE_ALPHABET) for _ in range(BACKUP_CODE_LEN))
        codes.append(f"{raw[:5]}-{raw[5:]}")
    return codes


def hash_backup_code(code: str) -> str:
    """SHA-256 fuer Backup-Codes — kein Argon2.

    Begruendung: Codes haben 50 bit Entropy (32**10) und werden einmalig
    benutzt + bei Konsum geloescht. Argon2 waere overkill und bricht
    den Login-Pfad (Verifikation muss schnell sein). SHA-256 reicht
    gegen Hash-Cracking, weil ein Angreifer pro Code nur einen Versuch
    hat (das gehashte Set ist klein, Brute-Force gegen 32**10 ist
    teuer genug — ~10^15 Hashes).
    """
    norm = code.replace("-", "").replace(" ", "").upper()
    return hashlib.sha256(norm.encode("ascii")).hexdigest()


def verify_backup_code(code: str, hashes: list[str]) -> tuple[bool, list[str]]:
    """Prueft ``code`` gegen die Hash-Liste. Bei Treffer wird der Hash entfernt.

    Returns:
        ``(consumed, remaining_hashes)``. ``consumed=True`` heisst: Code war
        gueltig + die Liste enthaelt diesen Hash nicht mehr (vom Aufrufer
        zu persistieren). ``consumed=False`` heisst: Code unbekannt,
        Hash-Liste unveraendert.
    """
    target = hash_backup_code(code)
    if target in hashes:
        remaining = [h for h in hashes if h != target]
        return True, remaining
    return False, hashes


# ---------------------------------------------------------------------------
# Challenge-Token fuer den 2-Schritt-Login
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TotpChallenge:
    """Kurzlebiger Token zwischen Login-Schritt 1 (Passwort) und 2 (TOTP).

    Inhalt:

    * ``user_id`` — wer ist gemeint
    * ``issued_at_unix`` — Ablaufpruefung (CHALLENGE_TTL_S)
    * ``signature`` — HMAC-SHA256 ueber (user_id, issued_at, version) mit
      einem App-weiten Secret. Damit kann der Server beim 2. Schritt
      verifizieren, dass die Challenge wirklich von ihm stammt und nicht
      vom Client gefakt ist.

    Die Challenge ersetzt **nicht** das Passwort — sie zertifiziert nur,
    dass Schritt 1 (Username+Passwort) erfolgreich war. Wer eine
    Challenge stiehlt, braucht zusaetzlich einen gueltigen TOTP-Code,
    um Schritt 2 zu bestehen.
    """

    user_id: int
    issued_at_unix: int
    signature: str

    def to_token(self) -> str:
        """Serialisiert die Challenge als kompakten String fuer den HTTP-Body."""
        return f"{self.user_id}:{self.issued_at_unix}:{self.signature}"

    @classmethod
    def from_token(cls, token: str) -> TotpChallenge | None:
        parts = token.split(":", 2)
        _EXPECTED_FIELDS = 3
        if len(parts) != _EXPECTED_FIELDS:
            return None
        try:
            user_id = int(parts[0])
            issued_at = int(parts[1])
        except ValueError:
            return None
        return cls(user_id=user_id, issued_at_unix=issued_at, signature=parts[2])


def issue_challenge(user_id: int, secret: bytes) -> TotpChallenge:
    """Erzeugt eine frische TOTP-Challenge nach erfolgreichem Schritt-1."""
    now = int(time.time())
    sig = _challenge_signature(user_id, now, secret)
    return TotpChallenge(user_id=user_id, issued_at_unix=now, signature=sig)


def verify_challenge(
    challenge: TotpChallenge,
    secret: bytes,
    *,
    now_unix: int | None = None,
) -> bool:
    """Verifiziert Signatur und Frist einer Challenge.

    Konstant-Zeit-Vergleich der Signatur ueber ``hmac.compare_digest``.
    """
    t = now_unix if now_unix is not None else int(time.time())
    if t - challenge.issued_at_unix < 0 or t - challenge.issued_at_unix > CHALLENGE_TTL_S:
        return False
    expected = _challenge_signature(challenge.user_id, challenge.issued_at_unix, secret)
    return hmac.compare_digest(expected, challenge.signature)


def _challenge_signature(user_id: int, issued_at: int, secret: bytes) -> str:
    mac = hmac.new(secret, digestmod=hashlib.sha256)
    mac.update(CHALLENGE_VERSION)
    mac.update(b":")
    mac.update(str(user_id).encode("ascii"))
    mac.update(b":")
    mac.update(str(issued_at).encode("ascii"))
    return mac.hexdigest()


def load_or_generate_challenge_secret() -> bytes:
    """Liest oder erzeugt das Challenge-HMAC-Secret.

    Reihenfolge:

    1. ``OPNCOCKPIT_TOTP_CHALLENGE_SECRET`` als Hex-String
    2. ``<app_data>/totp-challenge-secret`` (Bytes, 0o600)
    3. neu generieren + persistieren

    Wer das File hat, kann TOTP-Challenges signieren — entsprechend
    sollte es nur dem Service-User gehoeren. Auf Multi-Worker-Setups
    teilen sich die Worker das File; auf Single-Worker ohnehin egal.
    """
    env = os.environ.get("OPNCOCKPIT_TOTP_CHALLENGE_SECRET", "").strip()
    if env:
        try:
            return bytes.fromhex(env)
        except ValueError:
            return env.encode("utf-8")
    path: Path = get_app_data_dir() / "totp-challenge-secret"
    if path.exists():
        return path.read_bytes()
    path.parent.mkdir(parents=True, exist_ok=True)
    secret = secrets.token_bytes(32)
    path.write_bytes(secret)
    with contextlib.suppress(OSError):
        path.chmod(0o600)
    return secret


__all__ = [
    "BACKUP_CODE_COUNT",
    "CHALLENGE_TTL_S",
    "ISSUER",
    "TOTP_DIGITS",
    "TOTP_INTERVAL_S",
    "TotpChallenge",
    "generate_backup_codes",
    "generate_secret",
    "hash_backup_code",
    "issue_challenge",
    "load_or_generate_challenge_secret",
    "provisioning_uri",
    "verify_backup_code",
    "verify_challenge",
    "verify_code",
]
