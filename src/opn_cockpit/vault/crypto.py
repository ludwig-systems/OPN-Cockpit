"""Schlüsselableitung und Authenticated Encryption für den Tresor.

* **KDF:** Argon2id (über ``argon2-cffi``) mit RFC-9106-Empfehlung als
  Default — robust gegen Offline-Brute-Force, schmerzfrei auf einer PAW.
* **AEAD:** AES-256-GCM (über ``cryptography``). Authentifiziert sowohl
  Ciphertext als auch den Tresor-Header als AAD — jede Manipulation
  (Magic, KDF-Parameter, Nonce, Body) lässt die Entschlüsselung scheitern.

Niemand außerhalb dieses Moduls ruft die Crypto direkt auf; ``vault.store``
ist die Fassade.
"""

from __future__ import annotations

import os

from argon2.low_level import Type, hash_secret_raw
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from opn_cockpit.vault.errors import InvalidPasswordError
from opn_cockpit.vault.format import NONCE_SIZE, SALT_SIZE

# ---------------------------------------------------------------------------
# Defaults (RFC 9106 erste Empfehlung)
# ---------------------------------------------------------------------------

DEFAULT_TIME_COST: int = 4
DEFAULT_MEMORY_COST_KIB: int = 262_144  # 256 MiB
DEFAULT_PARALLELISM: int = 2
KEY_LEN: int = 32  # 256-bit für AES-256-GCM


def generate_salt() -> bytes:
    """Erzeugt einen frischen ``SALT_SIZE``-Byte-Salt aus dem System-CSPRNG."""
    return os.urandom(SALT_SIZE)


def generate_nonce() -> bytes:
    """Erzeugt eine frische ``NONCE_SIZE``-Byte-Nonce aus dem System-CSPRNG.

    GCM verlangt absolute Eindeutigkeit der Nonce pro Schlüssel — daher
    erzeugen wir bei JEDEM Save einen frischen Wert, auch wenn das Passwort
    unverändert bleibt.
    """
    return os.urandom(NONCE_SIZE)


def derive_key(
    password: str,
    salt: bytes,
    *,
    time_cost: int = DEFAULT_TIME_COST,
    memory_cost_kib: int = DEFAULT_MEMORY_COST_KIB,
    parallelism: int = DEFAULT_PARALLELISM,
) -> bytes:
    """Leitet einen 256-bit-Schlüssel aus ``password`` und ``salt`` ab.

    Deterministisch: gleiche Eingaben → gleicher Schlüssel.
    """
    return hash_secret_raw(
        secret=password.encode("utf-8"),
        salt=salt,
        time_cost=time_cost,
        memory_cost=memory_cost_kib,
        parallelism=parallelism,
        hash_len=KEY_LEN,
        type=Type.ID,
    )


def encrypt(plaintext: bytes, key: bytes, nonce: bytes, aad: bytes) -> bytes:
    """Verschlüsselt + authentifiziert mit AES-256-GCM.

    Liefert ``ciphertext || tag`` (16-Byte-Tag am Ende, GCM-Standardlayout
    von ``cryptography.AESGCM``).
    """
    return AESGCM(key).encrypt(nonce, plaintext, aad)


def decrypt(ciphertext_and_tag: bytes, key: bytes, nonce: bytes, aad: bytes) -> bytes:
    """Entschlüsselt + verifiziert AES-256-GCM.

    Wirft ``InvalidPasswordError`` bei falschem Schlüssel ODER manipuliertem
    Ciphertext/AAD — beide Fälle sind nicht unterscheidbar (und sollen es
    auch nicht sein).
    """
    try:
        return AESGCM(key).decrypt(nonce, ciphertext_and_tag, aad)
    except InvalidTag as exc:
        raise InvalidPasswordError(
            "Master-Passwort falsch oder Tresor-Datei manipuliert."
        ) from exc
