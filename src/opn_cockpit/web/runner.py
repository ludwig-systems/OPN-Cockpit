"""Boot-Wrapper: uvicorn + optional Browser-Auto-Open.

``run()`` startet den FastAPI-Server in der aktuellen Thread und blockiert
bis zum Beenden (Ctrl+C oder OS-Signal). Der Browser-Start passiert in
einem Daemon-Thread mit kleinem Delay, damit der Server zur Begruessung
schon antwortet.

Vor dem Server-Start laeuft das Migrations-Framework: ist eine offene
Migration vorhanden, wird vorher ein Pre-Update-Backup erzeugt und die
Migration angewandt. Schlaegt das fehl, bricht der Boot ab — Datenintegritaet
geht vor.

Windowless-Mode (Single-User-Windows-Install): wenn der Server unter
``pythonw.exe`` / ``opn-cockpitw.exe`` laeuft, sind ``sys.stdout`` und
``sys.stderr`` None. Wir lenken die Ausgabe dann in eine Logdatei
(``<app_data>/logs/opn-cockpit.log``), damit Migrations-/Server-Logs
nicht verloren gehen und der Admin bei Problemen was zu lesen hat.
"""

from __future__ import annotations

import contextlib
import dataclasses
import logging
import os
import sys
import threading
import time
import webbrowser

import uvicorn

from opn_cockpit.migrations import MigrationError, run_pending_migrations
from opn_cockpit.web.server import create_app
from opn_cockpit.web.settings import (
    TLS_SOURCE_AUTO,
    TLS_SOURCE_CUSTOM,
    TLS_SOURCE_ENV,
    TLS_SOURCE_NONE,
    WebSettings,
)

_log = logging.getLogger(__name__)

_BROWSER_OPEN_DELAY_S = 0.7
_LOG_FILE_MAX_BYTES = 5 * 1024 * 1024  # 5 MiB Rotation-Schwelle
_LOG_FILENAME = "opn-cockpit.log"

# Wenn ein Cert im Boot ≤ dieser Schwelle vom Ablauf steht, regeneriert
# der Boot es sofort - der Reboot ist Gelegenheit ohnehin.
_BOOT_REGEN_THRESHOLD_DAYS = 14


def _redirect_stdio_if_windowless() -> None:
    """Wenn der Interpreter keine Konsole hat (pythonw / opn-cockpitw),
    leite stdout/stderr in eine Logdatei um.

    Pythonw setzt ``sys.stdout`` und ``sys.stderr`` auf None - jeder
    Write-Aufruf wirft AttributeError und der Server stirbt unsichtbar.
    Wir oeffnen stattdessen ``<app_data>/logs/opn-cockpit.log`` und
    leiten beide Streams dorthin. Mit append-mode + line-buffer kann
    der Admin die Datei live taillen.

    Best-Effort: Wenn das Anlegen des Log-Verzeichnisses scheitert
    (z. B. Berechtigungen), schweigt die Funktion - mehr koennen wir
    nicht tun, und der Server kann ohne stdout/stderr trotzdem laufen
    weil wir uvicorn's log_level auf warning haben.
    """
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        # Spaet import damit der Modul-Import in Tests nicht config.py triggert.
        from opn_cockpit.config import get_app_data_dir  # noqa: PLC0415

        log_dir = get_app_data_dir() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / _LOG_FILENAME
        # Naive Rotation: wenn die Datei groesser als das Limit ist, bumpen
        # wir sie auf .1 weg bevor wir neu oeffnen. Mehr braucht's hier
        # nicht - der Single-User-Use-Case generiert wenig Output.
        if log_path.exists() and log_path.stat().st_size > _LOG_FILE_MAX_BYTES:
            rotated = log_dir / f"{_LOG_FILENAME}.1"
            with contextlib.suppress(OSError):
                if rotated.exists():
                    rotated.unlink()
                log_path.rename(rotated)
        # buffering=1 = line-buffer; reicht fuer Tail-Anwendungen.
        handle = open(  # noqa: SIM115 - lifetime = Prozess-Lebenszeit
            log_path, "a", buffering=1, encoding="utf-8", errors="replace",
        )
        sys.stdout = handle
        sys.stderr = handle
        handle.write(
            f"\n[opn-cockpit] Windowless-Start (PID {os.getpid()}), "
            f"Logs in {log_path}\n",
        )
        handle.flush()
    except OSError:
        # Ohne Log lebt der Server trotzdem, nur halt blind.
        pass


def run(settings: WebSettings | None = None) -> int:
    """Startet den Server. Liefert Exit-Code.

    Bei ``settings.auto_open_browser=True`` wird nach kurzer Verzoegerung
    der Standard-Browser auf die Server-URL gerichtet. Die Verzoegerung
    verhindert, dass der Browser eine "Connection refused"-Seite sieht,
    falls der Server noch im Startup ist.

    **HTTPS-by-default**: wenn weder Env-Cert noch Custom-Cert noch
    ``OPNCOCKPIT_ALLOW_HTTP=1`` gesetzt sind, generiert der Boot ein
    Self-Signed-Cert und startet mit dem.
    """
    _redirect_stdio_if_windowless()
    settings = settings or WebSettings.from_env()

    try:
        result = run_pending_migrations()
    except MigrationError as exc:
        sys.stderr.write(
            "\n[opn-cockpit] Migration fehlgeschlagen — Boot abgebrochen.\n"
            f"  Fehler: {exc}\n"
            "  Backup liegt in <app_data>/backups/. Server NICHT gestartet.\n\n",
        )
        sys.stderr.flush()
        return 78  # EX_CONFIG
    if not result.skipped:
        sys.stderr.write(
            f"\n[opn-cockpit] {len(result.applied_ids)} Migration(en) angewandt: "
            f"{', '.join(result.applied_ids)}\n",
        )
        if result.backup is not None:
            sys.stderr.write(f"  Backup: {result.backup.path}\n")
        sys.stderr.flush()

    # HTTPS-Fallback-Kette: Env-Cert -> Custom-Cert -> Auto-Cert -> HTTP.
    settings = _ensure_tls_or_http(settings)

    # SECURITY-AUDIT-v0.11 D7: Boot-Audit-Event mit TLS-Modus, damit
    # ein spaeter versehentlich aktiviertes OPNCOCKPIT_ALLOW_HTTP=1 im
    # Audit-Log nachvollziehbar bleibt (nicht nur in stderr).
    _audit_boot_tls_mode(settings)

    app = create_app()
    app.state.web_settings = settings   # damit Endpoints das lesen koennen

    if settings.auto_open_browser:
        _schedule_browser_open(settings.base_url)

    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_level="warning",
        access_log=False,
        ssl_certfile=settings.tls_cert,
        ssl_keyfile=settings.tls_key,
    )
    return 0


def _ensure_tls_or_http(settings: WebSettings) -> WebSettings:
    """Aktiviert HTTPS by default und generiert Auto-Cert bei Bedarf.

    Vier Endzustaende:

    * ``tls_source=env`` oder ``tls_source=custom``: WebSettings hat
      schon Cert+Key gesetzt (durch ``from_env``). Wir loggen die Wahl
      und lassen alles wie es ist.
    * ``allow_http_fallback=True``: kein TLS aktivieren. Sichtbare
      Warnung in stderr.
    * Auto-Cert existiert und ist frisch: benutzen.
    * Auto-Cert fehlt oder laeuft in <= ``_BOOT_REGEN_THRESHOLD_DAYS``
      ab: generieren + benutzen. Fingerprint prominent nach stderr.
    """
    if settings.tls_cert and settings.tls_key:
        sys.stderr.write(
            f"[opn-cockpit] HTTPS aktiv (Cert-Quelle: {settings.tls_source})\n"
            f"  Cert: {settings.tls_cert}\n"
        )
        sys.stderr.flush()
        return settings

    if settings.allow_http_fallback:
        sys.stderr.write(
            "[opn-cockpit] ACHTUNG: HTTP-Fallback aktiv (OPNCOCKPIT_ALLOW_HTTP=1).\n"
            "  Cockpit laeuft ohne TLS. Nur nutzen wenn ein Reverse-Proxy\n"
            "  vor Cockpit TLS terminiert.\n",
        )
        sys.stderr.flush()
        return settings

    # Auto-Cert-Pfad: prueft, generiert bei Bedarf.
    try:
        from opn_cockpit.config import AppSettings  # noqa: PLC0415
        from opn_cockpit.security.self_signed import (  # noqa: PLC0415
            AutoCertPaths,
            generate_self_signed,
            needs_regeneration,
            read_fingerprint,
        )
    except ImportError as exc:
        sys.stderr.write(
            f"[opn-cockpit] Auto-Cert nicht verfuegbar (Import: {exc}).\n"
            "  Cockpit faellt auf HTTP zurueck. Setze OPNCOCKPIT_ALLOW_HTTP=1\n"
            "  um das explizit zu machen, oder installier 'cryptography'.\n",
        )
        sys.stderr.flush()
        return settings

    app_settings = AppSettings.load()
    auto_paths = AutoCertPaths.from_dir(app_settings.auto_cert_directory())
    regenerated = False
    if needs_regeneration(auto_paths.cert, threshold_days=_BOOT_REGEN_THRESHOLD_DAYS):
        try:
            gc = generate_self_signed(auto_paths)
            regenerated = True
            sys.stderr.write(
                "\n[opn-cockpit] Neues Self-Signed-Cert generiert (HTTPS-Default):\n"
                f"  Subject:      CN={gc.subject_cn}\n"
                f"  Gueltig bis:  {gc.not_after_iso}\n"
                f"  Fingerprint:  SHA-256:{gc.fingerprint_sha256}\n"
                f"  Cert-Datei:   {gc.cert_path}\n"
                f"  Key-Datei:    {gc.key_path}\n"
                "  Beim ersten Browser-Aufruf akzeptierst du die Warnung und\n"
                "  vergleichst den Fingerprint aus diesem Log mit dem im Browser.\n"
                "  Fuer ein produktives Cert bitte im Server-TLS-Modal hochladen.\n\n",
            )
            sys.stderr.flush()
        except Exception as exc:  # noqa: BLE001
            _log.exception("Auto-Cert-Generation crashed")
            sys.stderr.write(
                f"[opn-cockpit] Auto-Cert-Generation FEHLGESCHLAGEN: {exc}\n"
                "  Cockpit faellt auf HTTP zurueck. Bitte manuell TLS aufsetzen.\n",
            )
            sys.stderr.flush()
            return settings

    if not regenerated:
        # Kein Regen noetig - Fingerprint aus dem Cache nachlogen.
        fp = read_fingerprint(auto_paths)
        if fp:
            sys.stderr.write(
                f"[opn-cockpit] HTTPS aktiv (Cert-Quelle: auto)\n"
                f"  Fingerprint: SHA-256:{fp}\n",
            )
            sys.stderr.flush()

    return dataclasses.replace(
        settings,
        tls_cert=str(auto_paths.cert),
        tls_key=str(auto_paths.key),
        tls_source=TLS_SOURCE_AUTO,
    )


def _audit_boot_tls_mode(settings: WebSettings) -> None:
    """Schreibt beim Server-Boot einen einmaligen Audit-Event mit TLS-Modus.

    Verwendet ``SERVER_RESTARTED``-Event-Kind (existiert bereits fuer den
    Restart-Endpoint) mit ``action="server_boot"``. So bleibt ein
    versehentlich aktiviertes ``OPNCOCKPIT_ALLOW_HTTP=1`` im Audit-Log
    nachvollziehbar — nicht nur in stderr das nach Log-Rotation weg ist.

    Fehler beim Audit-Write duerfen den Server-Start nicht blockieren.
    """
    try:
        from opn_cockpit.audit.backend import get_audit_backend  # noqa: PLC0415
        from opn_cockpit.audit.log import AuditEventKind  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return
    tls_summary = {
        "env": "TLS aktiv (Env-Cert)",
        "custom": "TLS aktiv (Custom-Cert)",
        "auto": "TLS aktiv (Auto-generiertes Self-Signed)",
        "none": "HTTP-only (OPNCOCKPIT_ALLOW_HTTP=1)",
    }.get(settings.tls_source, f"TLS-Modus: {settings.tls_source}")
    try:
        get_audit_backend().append(
            AuditEventKind.SERVER_RESTARTED,
            actor="system",
            action="server_boot",
            summary=(
                f"Server-Boot: {tls_summary}, bind {settings.host}:{settings.port}"
            ),
        )
    except Exception:  # noqa: BLE001
        # Audit-Backend nicht ansprechbar (Filesystem-Rechte, SQLite-Lock,
        # o. ae.) -> nicht fatal, Server startet trotzdem.
        pass


def _schedule_browser_open(url: str) -> None:
    def open_after_delay() -> None:
        time.sleep(_BROWSER_OPEN_DELAY_S)
        # Browser-Probleme sind nicht fatal — der Server laeuft trotzdem.
        with contextlib.suppress(Exception):  # pragma: no cover - OS-spezifisch
            webbrowser.open(url, new=2)

    thread = threading.Thread(target=open_after_delay, daemon=True)
    thread.start()
