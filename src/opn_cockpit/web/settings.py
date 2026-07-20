"""Web-Server-Konfiguration.

Wird beim Start aus Umgebungsvariablen gelesen, danach immutable. Mit
Blick auf die spaetere Server-Variante (Multi-User) sind alle Werte
ueberschreibbar — der Standard ist Loopback-Single-User, aber ein
zentraler Server kann ueber ``OPNCOCKPIT_HOST=0.0.0.0`` betrieben werden,
sobald die Auth-Schicht Multi-User-faehig ist.

**HTTPS by default (v0.10):** Wenn weder Env-Cert noch Custom-Cert
noch Env-``OPNCOCKPIT_ALLOW_HTTP=1`` gesetzt sind, generiert Cockpit
beim Boot ein Self-Signed und laeuft auf HTTPS. Custom-Cert-Import
bleibt der empfohlene Endzustand.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import sys
from dataclasses import dataclass

_log = logging.getLogger(__name__)

# Whitelist fuer Service-Namen die in Restart-Subprozess-Shell-Zeilen
# interpoliert werden (Windows nssm, Linux systemctl). Nur ASCII-Buchstaben,
# Ziffern, Punkt, Bindestrich, Unterstrich - alles was Windows/Linux fuer
# Service-Namen ohnehin erlauben.
# Siehe SECURITY-AUDIT-v0.11.local.md Finding D2.
_ALLOWED_SERVICE_NAME = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

DEFAULT_HOST = "127.0.0.1"
# 443 = HTTPS-Standardport. Zusammen mit HTTPS-by-default (v0.10) heisst
# das: Cockpit-URL ist einfach ``https://<host>`` ohne :port-Suffix. Auf
# Linux erlaubt CAP_NET_BIND_SERVICE (systemd-Unit) dem non-root Service-
# User das Binden; Windows / Dev-Mode braucht Admin- bzw. keine
# Restrictions. Override via ``OPNCOCKPIT_PORT=<n>``.
DEFAULT_PORT = 443

# TLS-Source-Marker fuers Boot-Log (nicht endpoint-relevant, aber
# hilfreich in stderr).
TLS_SOURCE_ENV = "env"                 # Env-Override
TLS_SOURCE_CUSTOM = "custom"           # settings.json (User-Upload)
TLS_SOURCE_AUTO = "auto"               # Self-Signed vom Cockpit
TLS_SOURCE_NONE = "none"               # HTTP-Fallback aktiv

# Restart-Modes (siehe :func:`detect_service_mode`).
RESTART_MODE_DEV = "dev"
RESTART_MODE_NSSM = "nssm"
RESTART_MODE_SYSTEMD = "systemd"


@dataclass(frozen=True, slots=True)
class WebSettings:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    auto_open_browser: bool = True
    tls_cert: str | None = None
    tls_key: str | None = None
    tls_source: str = TLS_SOURCE_NONE
    allow_http_fallback: bool = False

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> WebSettings:
        """Liest die Web-Konfiguration aus Env + AppSettings.

        Fallback-Kette fuer TLS:

        1. ``OPNCOCKPIT_TLS_CERT`` / ``OPNCOCKPIT_TLS_KEY`` aus dem
           Environment (klassischer Service-Style, hoechste Prio).
        2. ``server_tls_cert_path`` / ``server_tls_key_path`` aus
           ``settings.json`` (Custom-Upload per UI).
        3. Kein TLS gesetzt: HTTPS soll trotzdem laufen. Aufrufer
           (:func:`runner.run`) prueft den Auto-Cert-Pfad und
           generiert das Self-Signed bei Bedarf.

        ``OPNCOCKPIT_ALLOW_HTTP=1`` schaltet HTTP als Notausgang frei -
        ``tls_cert``/``tls_key`` bleiben None und der Aufrufer soll die
        Auto-Cert-Generation ueberspringen.

        AppSettings werden defensiv gelesen; kaputte ``settings.json``
        blockiert den Boot nicht - dann wird beim naechsten Schritt
        (Auto-Cert) trotzdem HTTPS gestartet.
        """
        env = env if env is not None else dict(os.environ)
        try:
            port = int(env.get("OPNCOCKPIT_PORT", str(DEFAULT_PORT)))
        except ValueError:
            port = DEFAULT_PORT

        allow_http_env = env.get("OPNCOCKPIT_ALLOW_HTTP", "").strip().lower()
        allow_http = allow_http_env in {"1", "true", "yes", "on"}

        tls_cert = env.get("OPNCOCKPIT_TLS_CERT") or None
        tls_key = env.get("OPNCOCKPIT_TLS_KEY") or None
        source = TLS_SOURCE_NONE
        if tls_cert and tls_key:
            source = TLS_SOURCE_ENV
        else:
            # AppSettings-Fallback fuer Custom-Cert.
            try:
                from opn_cockpit.config import AppSettings  # noqa: PLC0415
                app_settings = AppSettings.load()
            except Exception:  # noqa: BLE001
                app_settings = None
            if app_settings is not None:
                resolved = app_settings.resolved_tls_paths()
                if resolved is not None:
                    tls_cert = str(resolved[0])
                    tls_key = str(resolved[1])
                    source = TLS_SOURCE_CUSTOM
                # settings.json kann allow_http_fallback ueberschreiben,
                # aber Env hat weiterhin Vorrang.
                if not allow_http and app_settings.allow_http_fallback:
                    allow_http = True

        return cls(
            host=env.get("OPNCOCKPIT_HOST", DEFAULT_HOST),
            port=port,
            auto_open_browser=env.get("OPNCOCKPIT_NO_BROWSER", "0") != "1",
            tls_cert=tls_cert,
            tls_key=tls_key,
            tls_source=source,
            allow_http_fallback=allow_http,
        )

    @property
    def base_url(self) -> str:
        # Wenn TLS gesetzt oder wir HTTPS-by-default gehen (allow_http_fallback
        # ausgeschaltet), ist der User-facing Scheme https. Sonst http.
        # Standard-Ports (443/HTTPS, 80/HTTP) werden weggelassen — das ist
        # URL-Convention und macht Bookmarks lesbarer.
        use_https = bool(self.tls_cert and self.tls_key) or not self.allow_http_fallback
        if use_https:
            suffix = "" if self.port == 443 else f":{self.port}"
            return f"https://{self.host}{suffix}"
        suffix = "" if self.port == 80 else f":{self.port}"
        return f"http://{self.host}{suffix}"

    @property
    def is_loopback_only(self) -> bool:
        return self.host in ("127.0.0.1", "::1", "localhost")


def detect_service_mode(env: dict[str, str] | None = None) -> str:
    """Erkennt in welchem Service-Kontext Cockpit laeuft.

    Priorisierung:

    1. Explizite Env ``OPNCOCKPIT_RESTART_MODE`` (dev/nssm/systemd) -
       fuer manuelles Override und Tests.
    2. Windows + NSSM: ``OPNCOCKPIT_SERVICE_NAME`` gesetzt ODER
       ``nssm.exe`` im PATH auffindbar.
    3. Linux: ``systemctl`` verfuegbar UND ``opn-cockpit.service``
       Kontext (per ``INVOCATION_ID`` env vom systemd-launched Prozess).
    4. Sonst: Dev-Mode.
    """
    env = env if env is not None else dict(os.environ)
    override = env.get("OPNCOCKPIT_RESTART_MODE", "").strip().lower()
    if override in {RESTART_MODE_DEV, RESTART_MODE_NSSM, RESTART_MODE_SYSTEMD}:
        return override

    if sys.platform == "win32":
        if env.get("OPNCOCKPIT_SERVICE_NAME"):
            return RESTART_MODE_NSSM
        if shutil.which("nssm.exe") or shutil.which("nssm"):
            # Nicht 100% sicher dass wir als Service laufen, aber NSSM
            # da => Wahrscheinlichkeit hoch, dass der Admin es benutzen will.
            # User kann via OPNCOCKPIT_RESTART_MODE=dev sperren.
            return RESTART_MODE_NSSM
        return RESTART_MODE_DEV

    # Linux/macOS: systemd nur wenn wir von systemd gestartet wurden.
    if env.get("INVOCATION_ID") and shutil.which("systemctl"):
        return RESTART_MODE_SYSTEMD
    return RESTART_MODE_DEV


def service_name(env: dict[str, str] | None = None) -> str:
    """Name der Service-Unit fuer Restart-Aufrufe.

    Windows: ``OPNCOCKPIT_SERVICE_NAME`` oder Default ``OPN-Cockpit``.
    Linux: ``opn-cockpit.service`` (fix).

    Der Name wird in ``server_control._spawn_restart`` in eine
    Shell-Zeile interpoliert (``cmd /c "... nssm restart <name>"``).
    Um Command-Injection ueber eine manipulierte Env-Var zu verhindern
    (Defense-in-Depth — der Env-Var-Wert kommt vom Deploy-Betreiber, ist
    aber trotzdem eine Whitelist wert), erzwingt diese Funktion ein
    striktes ``[A-Za-z0-9._-]{1,64}``-Muster. Nicht-passende Werte
    werden verworfen und der Default zurueckgegeben.
    """
    env = env if env is not None else dict(os.environ)
    if sys.platform != "win32":
        return "opn-cockpit.service"
    raw = env.get("OPNCOCKPIT_SERVICE_NAME") or "OPN-Cockpit"
    if not _ALLOWED_SERVICE_NAME.fullmatch(raw):
        _log.warning(
            "OPNCOCKPIT_SERVICE_NAME=%r matcht nicht [A-Za-z0-9._-]{1,64} - "
            "fallback auf 'OPN-Cockpit'. (SECURITY-AUDIT-v0.11 D2)",
            raw,
        )
        return "OPN-Cockpit"
    return raw
