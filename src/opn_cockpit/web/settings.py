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
        env = env if env is not None else dict(os.environ)
        try:
            port = int(env.get("OPNCOCKPIT_PORT", str(DEFAULT_PORT)))
        except ValueError:
            port = DEFAULT_PORT
        return cls(
            host=env.get("OPNCOCKPIT_HOST", DEFAULT_HOST),
            port=port,
            auto_open_browser=env.get("OPNCOCKPIT_NO_BROWSER", "0") != "1",
            tls_cert=env.get("OPNCOCKPIT_TLS_CERT") or None,
            tls_key=env.get("OPNCOCKPIT_TLS_KEY") or None,
        )

    @property
    def base_url(self) -> str:
        scheme = "https" if (self.tls_cert and self.tls_key) else "http"
        return f"{scheme}://{self.host}:{self.port}"

    @property
    def is_loopback_only(self) -> bool:
        return self.host in ("127.0.0.1", "::1", "localhost")
