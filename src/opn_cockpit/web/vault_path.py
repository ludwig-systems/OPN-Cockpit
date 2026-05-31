"""Vault-Pfad-Validierung gegen Path-Traversal (Audit #14).

Akzeptierte Pfade muessen:
* auf ``.opnvault`` enden (Endungs-Check ist soft — gegen Versehen,
  nicht gegen einen entschlossenen Angreifer)
* innerhalb einer erlaubten Basis liegen — heute ``get_app_data_dir()``
  und (optional) ein per ``OPNCOCKPIT_VAULT_DIR`` konfigurierter Pfad.

Pragmatik: Vor der Aenderung konnte ein authentifizierter User per
``vault_path=C:\\Windows\\System32\\config\\SAM`` versuchen, beliebige
Dateien zu lesen — open_vault liefert zwar nur 503 zurueck (Magic-
Mismatch), aber der Read findet trotzdem statt. Mit dieser Pruefung
sind solche Probe-Reads vorbei.

Symlinks werden via ``Path.resolve()`` aufgeloest, dann gegen die Basen
verglichen.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

from opn_cockpit.config import get_app_data_dir

VAULT_DIR_ENV = "OPNCOCKPIT_VAULT_DIR"
VAULT_SUFFIX = ".opnvault"


class VaultPathError(ValueError):
    """Pfad liegt ausserhalb der erlaubten Basen oder hat falsche Endung."""


def allowed_vault_bases() -> list[Path]:
    """Liefert die heute erlaubten Vault-Basen, aufgeloest.

    Reihenfolge:
    1. ``OPNCOCKPIT_VAULT_DIR`` falls gesetzt — Override fuer Custom-Pfade
    2. ``get_app_data_dir()`` — Default-Pfad (XDG / APPDATA)
    3. ``Path.home()`` — User-Profil. Im Single-User-Mode soll der Admin
       seinen Tresor selbst frei legen koennen (Documents, Desktop, eigene
       Unterordner). Im Multi-User-Server-Mode laeuft der Server als
       LocalService o.ae.; deren Home enthaelt keine User-Daten und ist
       fuer einen Angreifer uninteressant -- die Erweiterung schadet
       hier nicht.

    Pfade, die nicht existieren, werden weiterhin aufgenommen — die
    Validierung prueft Path-Prefixes, nicht Existenz. Das vermeidet
    "vault muss vor dem Anlegen schon liegen"-Endlosschleifen.
    """
    bases: list[Path] = []
    override = os.environ.get(VAULT_DIR_ENV, "").strip()
    if override:
        bases.append(Path(override).resolve())
    bases.append(get_app_data_dir().resolve())
    # Path.home() kann auf manchen Systemen scheitern -- best-effort.
    with contextlib.suppress(OSError, RuntimeError):
        bases.append(Path.home().resolve())
    # Deduplizieren bei gleichem resolve.
    seen: set[str] = set()
    unique: list[Path] = []
    for base in bases:
        key = str(base)
        if key not in seen:
            seen.add(key)
            unique.append(base)
    return unique


def resolve_safe_vault_path(raw: str) -> Path:
    """Pruefe einen vom User uebergebenen vault_path. Liefert den resolved Path.

    Wirft ``VaultPathError`` wenn:
    - Pfad leer ist
    - Endung nicht ``.opnvault``
    - resolved Pfad nicht unter einer der ``allowed_vault_bases()`` liegt
    """
    s = (raw or "").strip()
    if not s:
        raise VaultPathError("Tresor-Pfad fehlt.")
    candidate = Path(s)
    if candidate.suffix.lower() != VAULT_SUFFIX:
        raise VaultPathError(
            f"Tresor-Datei muss auf '{VAULT_SUFFIX}' enden.",
        )
    # resolve() loest Symlinks auf, falls vorhanden — sonst normalisiert
    # es immerhin ``..`` und macht den Pfad absolut.
    try:
        resolved = candidate.resolve()
    except OSError as exc:
        raise VaultPathError(f"Tresor-Pfad nicht aufloesbar: {exc}") from exc
    bases = allowed_vault_bases()
    for base in bases:
        try:
            resolved.relative_to(base)
        except ValueError:
            continue
        return resolved
    base_list = ", ".join(str(b) for b in bases)
    raise VaultPathError(
        f"Tresor-Pfad liegt ausserhalb der erlaubten Basen ({base_list}).",
    )


__all__ = [
    "VAULT_DIR_ENV",
    "VAULT_SUFFIX",
    "VaultPathError",
    "allowed_vault_bases",
    "resolve_safe_vault_path",
]
