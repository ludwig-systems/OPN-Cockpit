"""Hochlevel-API für Tresor-Operationen.

Kapselt das Zusammenspiel von Format, KDF, AEAD und Filesystem in einer
schmalen Funktionsmenge:

* :func:`create_vault` — neue Datei mit Passwort anlegen
* :func:`open_vault` — Datei + Passwort → entsperrter ``OpenedVault``
* :func:`save_vault` — Änderungen unter demselben Passwort persistieren
* :func:`change_password` — Datei mit neuem Master-Passwort neu verschlüsseln
* :func:`export_template` — neue Datei mit identischem Inventar, aber
  geleerten Secret-Feldern (für das Verschicken als Vorlage)

Alle Schreibvorgänge sind atomar: erst in eine ``*.tmp``-Datei daneben,
dann ``os.replace`` — kein halb geschriebener Tresor bei Crash.

Bei jedem Save werden **frische** Nonce und (bei ``create_vault``,
``change_password``, ``export_template``) ein frisches Salt erzeugt.
``save_vault`` darf ausnahmsweise das Salt aus dem Header beibehalten,
damit dasselbe Passwort denselben Key liefert und der KDF-Schritt nicht
bei jedem trivialen Speichern erneut Sekunden brennt.
"""

from __future__ import annotations

import contextlib
import os
from dataclasses import dataclass, replace
from pathlib import Path

from opn_cockpit.vault.crypto import (
    DEFAULT_MEMORY_COST_KIB,
    DEFAULT_PARALLELISM,
    DEFAULT_TIME_COST,
    decrypt,
    derive_key,
    encrypt,
    generate_nonce,
    generate_salt,
)
from opn_cockpit.vault.errors import (
    CorruptVaultError,
    VaultIOError,
    WeakPasswordError,
)
from opn_cockpit.vault.format import HEADER_SIZE, HEADER_VERSION, VaultHeader
from opn_cockpit.vault.model import VaultData

MIN_PASSWORD_LENGTH: int = 12


# ---------------------------------------------------------------------------
# Hilfen
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OpenedVault:
    """Ergebnis eines erfolgreichen ``open_vault``: Klartext + Header.

    Den Header tragen wir mit, damit ``save_vault`` mit denselben
    KDF-Parametern + Salt arbeitet (sonst müsste der User bei jedem
    Speichern erneut den KDF-Aufwand zahlen).
    """

    data: VaultData
    header: VaultHeader


def validate_password(password: str) -> None:
    """Erzwingt die Mindest-Länge des Master-Passworts.

    Andere Regeln (Sonderzeichen, Mischung) bewusst nicht — siehe Memory:
    User-PAW-Kontext, der Admin entscheidet selbst.
    """
    if len(password) < MIN_PASSWORD_LENGTH:
        raise WeakPasswordError(
            f"Master-Passwort muss mindestens {MIN_PASSWORD_LENGTH} Zeichen lang sein."
        )


# ---------------------------------------------------------------------------
# Öffnen
# ---------------------------------------------------------------------------


def open_vault(path: Path, password: str) -> OpenedVault:
    """Lädt ``path``, leitet den Schlüssel aus ``password`` ab, entschlüsselt.

    Fehlerquellen:

    * ``VaultIOError`` — Datei nicht lesbar
    * ``CorruptVaultError`` — Magic falsch, Datei zu kurz, Header unparsbar
    * ``VaultVersionError`` — Datei in inkompatibler Version
    * ``InvalidPasswordError`` — Passwort falsch oder Inhalt manipuliert
    """
    raw = _read_file(path)
    return open_vault_bytes(raw, password)


def open_vault_bytes(raw: bytes, password: str) -> OpenedVault:
    """Wie :func:`open_vault`, aber direkt aus den Bytes — fuer File-Uploads
    aus dem Multi-User-Server-Mode, bei dem der Server keine User-Pfade
    sehen darf (LocalService-Account)."""
    if len(raw) < HEADER_SIZE:
        raise CorruptVaultError(
            f"Tresor-Datei zu kurz ({len(raw)} < {HEADER_SIZE} Bytes)."
        )
    header_bytes = raw[:HEADER_SIZE]
    header = VaultHeader.unpack(header_bytes)
    ciphertext = raw[HEADER_SIZE:]
    key = derive_key(
        password,
        header.kdf_salt,
        time_cost=header.kdf_time_cost,
        memory_cost_kib=header.kdf_memory_cost_kib,
        parallelism=header.kdf_parallelism,
    )
    plaintext = decrypt(ciphertext, key, header.nonce, aad=header_bytes)
    data = VaultData.from_json_bytes(plaintext)
    return OpenedVault(data=data, header=header)


# ---------------------------------------------------------------------------
# Anlegen / Speichern
# ---------------------------------------------------------------------------


def create_vault(
    path: Path,
    password: str,
    data: VaultData | None = None,
    *,
    overwrite: bool = False,
) -> None:
    """Erzeugt einen frischen Tresor unter ``path``.

    Wirft ``VaultIOError`` wenn die Datei bereits existiert und
    ``overwrite=False`` ist (Default — Schutz vor versehentlichem Überschreiben).
    """
    validate_password(password)
    if path.exists() and not overwrite:
        raise VaultIOError(f"Tresor-Datei existiert bereits: {path}")
    payload = data or VaultData()
    _write_with_fresh_key_material(path, payload, password)


def save_vault(path: Path, opened: OpenedVault, password: str) -> OpenedVault:
    """Persistiert Änderungen unter dem aktuellen Passwort.

    Übernimmt Salt + KDF-Parameter aus ``opened.header``, erzeugt aber eine
    frische Nonce (GCM-Pflicht: nie wiederverwenden). Liefert einen neuen
    ``OpenedVault`` mit aktualisiertem Header zurück — der Aufrufer
    überschreibt damit seine Referenz.
    """
    validate_password(password)
    new_nonce = generate_nonce()
    new_header = VaultHeader(
        version=HEADER_VERSION,
        kdf_salt=opened.header.kdf_salt,
        kdf_time_cost=opened.header.kdf_time_cost,
        kdf_memory_cost_kib=opened.header.kdf_memory_cost_kib,
        kdf_parallelism=opened.header.kdf_parallelism,
        nonce=new_nonce,
    )
    _write_with_header(path, opened.data, password, new_header)
    return OpenedVault(data=opened.data, header=new_header)


# ---------------------------------------------------------------------------
# Passwort ändern
# ---------------------------------------------------------------------------


def change_password(
    path: Path,
    old_password: str,
    new_password: str,
) -> OpenedVault:
    """Öffnet ``path`` mit ``old_password`` und schreibt mit ``new_password`` zurück.

    Erzwingt frisches Salt + frische Nonce für den neuen Schlüssel.
    """
    opened = open_vault(path, old_password)
    validate_password(new_password)
    return _write_with_fresh_key_material_and_return(path, opened.data, new_password)


# ---------------------------------------------------------------------------
# Template-Export
# ---------------------------------------------------------------------------


def export_template(
    source_path: Path,
    dest_path: Path,
    password: str,
    *,
    overwrite: bool = False,
) -> None:
    """Erzeugt unter ``dest_path`` eine Kopie mit leeren Secret-Feldern.

    Identisches Master-Passwort, identische Geräte-Liste und -Settings, aber
    ``api_key`` und ``api_secret`` jedes Geräts sind leere Strings. Der
    Empfänger füllt diese aus und kann das Master-Passwort ändern.
    """
    if dest_path.exists() and not overwrite:
        raise VaultIOError(f"Ziel-Datei existiert bereits: {dest_path}")
    opened = open_vault(source_path, password)
    blanked_devices = [replace(d, api_key="", api_secret="") for d in opened.data.devices]
    template_data = VaultData(
        schema_version=opened.data.schema_version,
        devices=blanked_devices,
        settings=opened.data.settings,
    )
    _write_with_fresh_key_material(dest_path, template_data, password)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _read_file(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise VaultIOError(f"Tresor-Datei nicht lesbar: {path} ({exc})") from exc


def _atomic_write(path: Path, payload: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        # Parent-Verzeichnis on-demand anlegen — der Pfad ist zu diesem Zeitpunkt
        # bereits durch web/vault_path.py oder den CLI-Caller validiert.
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(payload)
        os.replace(tmp, path)
    except OSError as exc:
        if tmp.exists():
            with contextlib.suppress(OSError):
                tmp.unlink()
        raise VaultIOError(f"Tresor-Datei nicht schreibbar: {path} ({exc})") from exc


def _write_with_fresh_key_material(
    path: Path,
    data: VaultData,
    password: str,
) -> None:
    """Schreibt mit frischem Salt + frischer Nonce.

    Wird von ``create_vault``, ``change_password`` und ``export_template``
    aufgerufen.
    """
    _write_with_fresh_key_material_and_return(path, data, password)


def _write_with_fresh_key_material_and_return(
    path: Path,
    data: VaultData,
    password: str,
) -> OpenedVault:
    header = VaultHeader(
        version=HEADER_VERSION,
        kdf_salt=generate_salt(),
        kdf_time_cost=DEFAULT_TIME_COST,
        kdf_memory_cost_kib=DEFAULT_MEMORY_COST_KIB,
        kdf_parallelism=DEFAULT_PARALLELISM,
        nonce=generate_nonce(),
    )
    _write_with_header(path, data, password, header)
    return OpenedVault(data=data, header=header)


def _write_with_header(
    path: Path,
    data: VaultData,
    password: str,
    header: VaultHeader,
) -> None:
    header_bytes = header.pack()
    key = derive_key(
        password,
        header.kdf_salt,
        time_cost=header.kdf_time_cost,
        memory_cost_kib=header.kdf_memory_cost_kib,
        parallelism=header.kdf_parallelism,
    )
    plaintext = data.to_json_bytes()
    ciphertext = encrypt(plaintext, key, header.nonce, aad=header_bytes)
    _atomic_write(path, header_bytes + ciphertext)
