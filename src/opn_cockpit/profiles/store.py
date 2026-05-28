"""Aktions-Templates (Profile) — Persistenz und CRUD.

Spec R-TPL-1/2: wiederverwendbare Aktions-Vorlagen, die als Ausgangspunkt
geladen werden können, **ohne** Credentials. Persistenz: JSON-Datei unter
``%APPDATA%/OPN-Cockpit/profiles.json``.

Struktur:

```json
{
  "schema_version": 1,
  "profiles": [
    {
      "id": "prof-XXXXXXXX",
      "name": "Standard-Routen-Set für Außenstandorte",
      "action": "add_route",
      "subsystem": "routes",
      "default_selector": "tag:branches",
      "spec": {"network": "10.99.0.0/24", "gateway": "WAN_GW", "descr": "", "disabled": false}
    }
  ]
}
```
"""

from __future__ import annotations

import json
import os
import secrets
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from opn_cockpit.config import get_app_data_dir

PROFILES_FILENAME = "profiles.json"
PROFILES_SCHEMA_VERSION = 1


def generate_profile_id() -> str:
    return f"prof-{secrets.token_hex(4).upper()}"


@dataclass(frozen=True, slots=True)
class Profile:
    """Ein gespeichertes Aktions-Template.

    Enthält ausschließlich Aktions-Parameter — Selektor, Spec-Dict und Metadaten.
    KEINE Credentials, KEINE Gerätelisten (die kommen aus dem Inventar des
    Tresors zur Lade-Zeit).
    """

    id: str
    name: str
    action: str
    subsystem: str
    default_selector: str
    spec: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ProfileStoreError(ValueError):
    """Profil-Datei nicht lesbar/parsbar oder Profil-ID/Name ungültig."""


@dataclass(slots=True)
class ProfileStore:
    """JSON-File-basierter Profile-Store mit CRUD-Operationen.

    Mehrfach-Instanzen auf derselben Datei sind harmlos — jede Mutation
    liest, modifiziert und schreibt atomar zurück.
    """

    path: Path
    _cache: list[Profile] = field(default_factory=list)
    _loaded: bool = False

    # ----- Public API -----

    def list_profiles(self) -> list[Profile]:
        self._ensure_loaded()
        return list(self._cache)

    def get(self, profile_id: str) -> Profile:
        self._ensure_loaded()
        for p in self._cache:
            if p.id == profile_id:
                return p
        raise ProfileStoreError(f"Profil-ID nicht gefunden: {profile_id}")

    def find_by_name(self, name: str) -> Profile | None:
        self._ensure_loaded()
        for p in self._cache:
            if p.name == name:
                return p
        return None

    def save_new(
        self,
        *,
        name: str,
        action: str,
        subsystem: str,
        default_selector: str,
        spec: dict[str, Any],
    ) -> Profile:
        self._ensure_loaded()
        if not name.strip():
            raise ProfileStoreError("Profil-Name darf nicht leer sein.")
        if any(p.name == name for p in self._cache):
            raise ProfileStoreError(f"Profil-Name existiert bereits: {name!r}")
        profile = Profile(
            id=generate_profile_id(),
            name=name.strip(),
            action=action,
            subsystem=subsystem,
            default_selector=default_selector,
            spec=_sanitize_spec(spec),
        )
        self._cache.append(profile)
        self._persist()
        return profile

    def delete(self, profile_id: str) -> bool:
        self._ensure_loaded()
        before = len(self._cache)
        self._cache = [p for p in self._cache if p.id != profile_id]
        if len(self._cache) == before:
            return False
        self._persist()
        return True

    # ----- Internals -----

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._cache = _load_from_disk(self.path)
        self._loaded = True

    def _persist(self) -> None:
        _save_to_disk(self.path, self._cache)


# ---------------------------------------------------------------------------
# Serialisierung
# ---------------------------------------------------------------------------


_FORBIDDEN_SPEC_KEYS = frozenset(
    {"api_key", "api_secret", "password", "token", "authorization"}
)


def _sanitize_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """Stellt sicher, dass kein Secret-ähnliches Feld in einem Profil landet.

    Defensiv: ein Adapter könnte versehentlich ein Secret in den
    ``spec_to_dict``-Output rutschen lassen. Profile sollen unbedenklich
    weitergegeben werden — also Whitelist-Säuberung beim Speichern.
    """
    cleaned: dict[str, Any] = {}
    for k, v in spec.items():
        if isinstance(k, str) and k.lower() in _FORBIDDEN_SPEC_KEYS:
            continue
        cleaned[k] = v
    return cleaned


def _load_from_disk(path: Path) -> list[Profile]:
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProfileStoreError(f"Profil-Datei nicht lesbar: {path} ({exc})") from exc
    if not isinstance(raw, dict):
        return []
    items_raw = raw.get("profiles", [])
    if not isinstance(items_raw, list):
        return []
    result: list[Profile] = []
    for item in items_raw:
        if not isinstance(item, dict):
            continue
        try:
            result.append(_profile_from_dict(item))
        except ProfileStoreError:
            continue
    return result


def _profile_from_dict(raw: dict[str, Any]) -> Profile:
    pid = str(raw.get("id") or generate_profile_id())
    name = str(raw.get("name", "")).strip()
    if not name:
        raise ProfileStoreError("Profil ohne Namen — übersprungen.")
    spec = raw.get("spec")
    if not isinstance(spec, dict):
        spec = {}
    return Profile(
        id=pid,
        name=name,
        action=str(raw.get("action", "")),
        subsystem=str(raw.get("subsystem", "")),
        default_selector=str(raw.get("default_selector", "all")),
        spec=_sanitize_spec(spec),
    )


def _save_to_disk(path: Path, profiles: list[Profile]) -> None:
    payload = {
        "schema_version": PROFILES_SCHEMA_VERSION,
        "profiles": [p.to_dict() for p in profiles],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(tmp, path)


def default_profiles_path() -> Path:
    """Standard-Pfad: ``%APPDATA%/OPN-Cockpit/profiles.json``."""
    return get_app_data_dir() / PROFILES_FILENAME
