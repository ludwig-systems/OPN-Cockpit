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
import os
import sys
import threading
import time
import webbrowser

import uvicorn

from opn_cockpit.migrations import MigrationError, run_pending_migrations
from opn_cockpit.web.server import create_app
from opn_cockpit.web.settings import WebSettings

_BROWSER_OPEN_DELAY_S = 0.7
_LOG_FILE_MAX_BYTES = 5 * 1024 * 1024  # 5 MiB Rotation-Schwelle
_LOG_FILENAME = "opn-cockpit.log"


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

    app = create_app()

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


def _schedule_browser_open(url: str) -> None:
    def open_after_delay() -> None:
        time.sleep(_BROWSER_OPEN_DELAY_S)
        # Browser-Probleme sind nicht fatal — der Server laeuft trotzdem.
        with contextlib.suppress(Exception):  # pragma: no cover - OS-spezifisch
            webbrowser.open(url, new=2)

    thread = threading.Thread(target=open_after_delay, daemon=True)
    thread.start()
