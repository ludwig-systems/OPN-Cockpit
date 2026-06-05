"""Web-Server-Konfiguration.

Wird beim Start aus Umgebungsvariablen gelesen, danach immutable. Mit
Blick auf die spaetere Server-Variante (Multi-User) sind alle Werte
ueberschreibbar — der Standard ist Loopback-Single-User, aber ein
zentraler Server kann ueber ``OPNCOCKPIT_HOST=0.0.0.0`` betrieben werden,
sobald die Auth-Schicht Multi-User-faehig ist.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9876


@dataclass(frozen=True, slots=True)
class WebSettings:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    auto_open_browser: bool = True
    # Spaetere Server-Variante: Pfad zu TLS-Zertifikat + Key. Aktuell ungenutzt.
    tls_cert: str | None = None
    tls_key: str | None = None

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> WebSettings:
        """Liest die Web-Konfiguration aus Env + AppSettings.

        Reihenfolge:
        1. ``OPNCOCKPIT_TLS_CERT`` / ``OPNCOCKPIT_TLS_KEY`` aus dem
           Environment (klassischer Service-Style, Override).
        2. ``server_tls_cert_path`` / ``server_tls_key_path`` aus
           ``%APPDATA%/OPN-Cockpit/settings.json`` (UI-Setup-Pfad).
        3. Sonst HTTP wie bisher.

        AppSettings werden defensiv gelesen - wenn ``settings.json``
        nicht existiert oder kaputt ist, faellt der Boot auf reines
        HTTP zurueck. Cockpit darf nie wegen eines kaputten Cert-
        Eintrags ungestartet bleiben.
        """
        env = env if env is not None else dict(os.environ)
        try:
            port = int(env.get("OPNCOCKPIT_PORT", str(DEFAULT_PORT)))
        except ValueError:
            port = DEFAULT_PORT

        tls_cert = env.get("OPNCOCKPIT_TLS_CERT") or None
        tls_key = env.get("OPNCOCKPIT_TLS_KEY") or None
        if not tls_cert or not tls_key:
            # AppSettings-Fallback - Spaeter-Import damit Tests den Pfad
            # via OPNCOCKPIT_DATA_DIR injizieren koennen ohne dass die
            # Settings beim Modul-Import schon eingelesen sind.
            try:
                from opn_cockpit.config import AppSettings  # noqa: PLC0415
                resolved = AppSettings.load().resolved_tls_paths()
            except Exception:  # noqa: BLE001
                resolved = None
            if resolved is not None:
                tls_cert = str(resolved[0])
                tls_key = str(resolved[1])

        return cls(
            host=env.get("OPNCOCKPIT_HOST", DEFAULT_HOST),
            port=port,
            auto_open_browser=env.get("OPNCOCKPIT_NO_BROWSER", "0") != "1",
            tls_cert=tls_cert,
            tls_key=tls_key,
        )

    @property
    def base_url(self) -> str:
        scheme = "https" if (self.tls_cert and self.tls_key) else "http"
        return f"{scheme}://{self.host}:{self.port}"

    @property
    def is_loopback_only(self) -> bool:
        return self.host in ("127.0.0.1", "::1", "localhost")
