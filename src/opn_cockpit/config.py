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

# Deployment-Modes — heute ist nur 'single-local' implementiert. Die anderen
# sind Roadmap-Slots (siehe docs/ROADMAP.md), damit Config-Schema und
# Server-Bind sich spaeter ohne Breaking Changes erweitern lassen.
DEPLOYMENT_MODES = ("single-local", "single-network", "multi-server")
AUTH_BACKENDS = ("vault", "user-db")
STORAGE_BACKENDS = ("filesystem", "sqlite")

# Env-Variablen, die settings.json ueberschreiben. Nuetzlich fuer
# Docker/systemd, wo settings.json im persistenten Volume liegt, aber
# der Betreiber den Mode ohne JSON-Edit umstellen will.
AUTH_BACKEND_ENV = "OPNCOCKPIT_AUTH_BACKEND"
DEPLOYMENT_MODE_ENV = "OPNCOCKPIT_DEPLOYMENT_MODE"
STORAGE_BACKEND_ENV = "OPNCOCKPIT_STORAGE_BACKEND"
UPDATE_CHECK_ENABLED_ENV = "OPNCOCKPIT_UPDATE_CHECK_ENABLED"
UPDATE_CHECK_INTERVAL_ENV = "OPNCOCKPIT_UPDATE_CHECK_INTERVAL_HOURS"


def get_app_data_dir() -> Path:
    """Ermittelt das App-Daten-Verzeichnis plattformabhaengig.

    Priorität:
    1. ``OPNCOCKPIT_DATA_DIR`` — explizite Override (Container, Service,
       Tests). Erlaubt es, alle Daten in ein einzelnes Volume zu legen.
    2. Windows: ``%APPDATA%\\OPN-Cockpit\\``.
    3. Linux/Mac XDG: ``$XDG_DATA_HOME/opn-cockpit`` falls gesetzt,
       sonst ``~/.local/share/opn-cockpit``.
    4. Letzter Fallback: ``~/.opn-cockpit`` (Headless ohne XDG).

    Die XDG-Variante macht den Linux-Container-Modus moeglich (Roadmap
    v3.0) ohne dass wir das Pfadschema spaeter aendern muessen.
    """
    explicit = os.environ.get("OPNCOCKPIT_DATA_DIR")
    if explicit:
        return Path(explicit)
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / APP_NAME
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / APP_NAME.lower()
    home = Path.home()
    xdg_default = home / ".local" / "share" / APP_NAME.lower()
    if (home / ".local").exists():
        return xdg_default
    return home / f".{APP_NAME.lower()}"


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

    # Roadmap-Slots fuer v3.x (Multi-User / Linux-Container). Heute auf
    # 'single-local' / 'vault' / 'filesystem' fixiert - der Server prueft
    # diese Werte noch nicht. Aber Schema bleibt forward-kompatibel.
    deployment_mode: str = "single-local"
    auth_backend: str = "vault"
    storage_backend: str = "filesystem"

    # v6-Pass 3: Update-Check via GitHub-Releases-API. Default an, kann
    # fuer Air-gapped-Installationen per Env oder JSON deaktiviert werden.
    update_check_enabled: bool = True
    update_check_interval_hours: int = 24

    # v0.8 #12: Eigenes Server-Zertifikat fuer den Cockpit-eigenen HTTPS-
    # Port. Pfade zu PEM-codiertem Cert (Fullchain) und Private-Key auf
    # der Cockpit-Maschine. Wenn beide gesetzt sind UND beide existieren,
    # startet uvicorn auf HTTPS statt HTTP. Andernfalls bleibt das alte
    # HTTP-Verhalten (Loopback Single-User; Reverse-Proxy Multi-User).
    # Schluesselsicherheit: die Dateien gehoeren mit 0600 dem Service-
    # User; Cockpit liest sie nur lesend.
    server_tls_cert_path: str | None = None
    server_tls_key_path: str | None = None

    # ----- Persistenz -----

    @classmethod
    def load(cls, path: Path | None = None) -> AppSettings:
        """Lädt Settings aus ``path`` oder Default-Pfad; tolerant bei Fehlern.

        Env-Variablen ``OPNCOCKPIT_AUTH_BACKEND``,
        ``OPNCOCKPIT_DEPLOYMENT_MODE``, ``OPNCOCKPIT_STORAGE_BACKEND``
        ueberschreiben die JSON-Werte, falls gesetzt. So kann Docker
        oder systemd den Mode ohne JSON-Edit umstellen.
        """
        resolved = path or get_settings_path()
        if not resolved.exists():
            settings = cls()
        else:
            try:
                raw = json.loads(resolved.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                # Bewusst tolerant: kaputte settings.json soll das Tool nicht
                # blockieren, wir starten mit Defaults.
                settings = cls()
            else:
                settings = cls() if not isinstance(raw, dict) else cls._from_dict(raw)
        settings._apply_env_overrides()
        return settings

    def _apply_env_overrides(self) -> None:
        """Wendet Env-Variablen-Overrides an. Unbekannte Werte werden ignoriert."""
        auth = os.environ.get(AUTH_BACKEND_ENV, "").strip()
        if auth and auth in AUTH_BACKENDS:
            self.auth_backend = auth
        mode = os.environ.get(DEPLOYMENT_MODE_ENV, "").strip()
        if mode and mode in DEPLOYMENT_MODES:
            self.deployment_mode = mode
        storage = os.environ.get(STORAGE_BACKEND_ENV, "").strip()
        if storage and storage in STORAGE_BACKENDS:
            self.storage_backend = storage
        update_enabled = os.environ.get(UPDATE_CHECK_ENABLED_ENV, "").strip().lower()
        if update_enabled in {"0", "false", "no", "off"}:
            self.update_check_enabled = False
        elif update_enabled in {"1", "true", "yes", "on"}:
            self.update_check_enabled = True
        interval_raw = os.environ.get(UPDATE_CHECK_INTERVAL_ENV, "").strip()
        if interval_raw:
            try:
                interval = int(interval_raw)
            except ValueError:
                interval = -1
            if interval > 0:
                self.update_check_interval_hours = interval

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

    def resolved_tls_paths(self) -> tuple[Path, Path] | None:
        """Liefert ``(cert_path, key_path)`` wenn beide Dateien existieren,
        sonst ``None``. Aufrufer (Server-Bootstrap) interpretiert das als
        "HTTPS einschalten" / "HTTP weiter".
        """
        if not self.server_tls_cert_path or not self.server_tls_key_path:
            return None
        cert = Path(self.server_tls_cert_path).expanduser()
        key = Path(self.server_tls_key_path).expanduser()
        if not cert.is_file() or not key.is_file():
            return None
        return cert, key

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
        deployment_mode = str(raw.get("deployment_mode", "single-local"))
        if deployment_mode not in DEPLOYMENT_MODES:
            deployment_mode = "single-local"
        auth_backend = str(raw.get("auth_backend", "vault"))
        if auth_backend not in AUTH_BACKENDS:
            auth_backend = "vault"
        storage_backend = str(raw.get("storage_backend", "filesystem"))
        if storage_backend not in STORAGE_BACKENDS:
            storage_backend = "filesystem"
        update_enabled_raw = raw.get("update_check_enabled", True)
        update_enabled = bool(update_enabled_raw) if isinstance(
            update_enabled_raw, bool,
        ) else True
        try:
            interval = int(raw.get("update_check_interval_hours", 24))
        except (TypeError, ValueError):
            interval = 24
        if interval <= 0:
            interval = 24
        cert_raw = raw.get("server_tls_cert_path")
        cert_path = str(cert_raw) if isinstance(cert_raw, str) and cert_raw.strip() else None
        key_raw = raw.get("server_tls_key_path")
        key_path = str(key_raw) if isinstance(key_raw, str) and key_raw.strip() else None
        return cls(
            recent_vaults=recent[:limit],
            default_vault=default_str,
            recent_limit=limit,
            deployment_mode=deployment_mode,
            auth_backend=auth_backend,
            storage_backend=storage_backend,
            update_check_enabled=update_enabled,
            update_check_interval_hours=interval,
            server_tls_cert_path=cert_path,
            server_tls_key_path=key_path,
        )
