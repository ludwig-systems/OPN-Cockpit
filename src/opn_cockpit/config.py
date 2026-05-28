"""App-weite Einstellungen außerhalb des Tresors.

Diese Datei lebt in ``%APPDATA%\\OPN-Cockpit\\settings.json`` (oder einem
Fallback im Home-Verzeichnis) und enthält **keine** Secrets — nur
Komfort-Konfiguration:

* Liste der zuletzt geöffneten Tresor-Dateien (max 5)
* Pfad des Default-Tresors, der beim Tool-Start automatisch zum Entsperren
  vorgeschlagen wird
* UI-Präferenzen (Theme etc.) — kommen bei Schritt 8 dazu

Alles, was ein Geheimnis sein könnte, gehört in den Tresor.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

APP_NAME = "OPN-Cockpit"
SETTINGS_FILENAME = "settings.json"
DEFAULT_RECENT_LIMIT = 5


def get_app_data_dir() -> Path:
    """Ermittelt das App-Daten-Verzeichnis.

    Bevorzugt ``%APPDATA%``; fällt auf ``~/.opn-cockpit`` zurück, wenn die
    Umgebungsvariable nicht gesetzt ist (z. B. Headless-CLI in einem
    Service-Account).
    """
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / APP_NAME
    return Path.home() / f".{APP_NAME.lower()}"


def get_settings_path() -> Path:
    return get_app_data_dir() / SETTINGS_FILENAME


@dataclass(slots=True)
class AppSettings:
    """Persistente, nicht-sensitive App-Einstellungen.

    Persistenz: explizit per :meth:`save`. Default-Defaults (leere Listen,
    keine Default-Tresor-Datei) verhindern, dass eine fehlende Datei den
    Tool-Start blockiert.
    """

    recent_vaults: list[str] = field(default_factory=list)
    default_vault: str | None = None
    recent_limit: int = DEFAULT_RECENT_LIMIT

    # ----- Persistenz -----

    @classmethod
    def load(cls, path: Path | None = None) -> AppSettings:
        """Lädt Settings aus ``path`` oder Default-Pfad; tolerant bei Fehlern."""
        resolved = path or get_settings_path()
        if not resolved.exists():
            return cls()
        try:
            raw = json.loads(resolved.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # Bewusst tolerant: kaputte settings.json soll das Tool nicht
            # blockieren, wir starten mit Defaults.
            return cls()
        if not isinstance(raw, dict):
            return cls()
        return cls._from_dict(raw)

    def save(self, path: Path | None = None) -> None:
        """Schreibt Settings nach ``path`` oder Default-Pfad (atomar)."""
        resolved = path or get_settings_path()
        resolved.parent.mkdir(parents=True, exist_ok=True)
        tmp = resolved.with_suffix(resolved.suffix + ".tmp")
        tmp.write_text(
            json.dumps(asdict(self), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, resolved)

    # ----- Recent-Vaults-Liste -----

    def remember_vault(self, vault_path: str | Path) -> None:
        """Schiebt ``vault_path`` an die Spitze der Recent-Liste.

        Duplikate werden entfernt, danach auf ``recent_limit`` gekürzt. Der
        Aufrufer ruft anschließend :meth:`save`.
        """
        value = str(vault_path)
        deduped = [value] + [p for p in self.recent_vaults if p != value]
        self.recent_vaults = deduped[: self.recent_limit]

    def forget_vault(self, vault_path: str | Path) -> None:
        """Entfernt ``vault_path`` aus der Recent-Liste (falls vorhanden)."""
        value = str(vault_path)
        self.recent_vaults = [p for p in self.recent_vaults if p != value]
        if self.default_vault == value:
            self.default_vault = None

    # ----- Internals -----

    @classmethod
    def _from_dict(cls, raw: dict[str, Any]) -> AppSettings:
        recent_raw = raw.get("recent_vaults", [])
        recent = [str(p) for p in recent_raw] if isinstance(recent_raw, list) else []
        default = raw.get("default_vault")
        default_str = str(default) if isinstance(default, str) and default else None
        limit_raw = raw.get("recent_limit", DEFAULT_RECENT_LIMIT)
        try:
            limit = int(limit_raw)
        except (TypeError, ValueError):
            limit = DEFAULT_RECENT_LIMIT
        return cls(
            recent_vaults=recent[:limit],
            default_vault=default_str,
            recent_limit=limit,
        )
