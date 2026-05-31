"""Boot-Wrapper: uvicorn + optional Browser-Auto-Open.

``run()`` startet den FastAPI-Server in der aktuellen Thread und blockiert
bis zum Beenden (Ctrl+C oder OS-Signal). Der Browser-Start passiert in
einem Daemon-Thread mit kleinem Delay, damit der Server zur Begruessung
schon antwortet.

Vor dem Server-Start laeuft das Migrations-Framework: ist eine offene
Migration vorhanden, wird vorher ein Pre-Update-Backup erzeugt und die
Migration angewandt. Schlaegt das fehl, bricht der Boot ab — Datenintegritaet
geht vor.
"""

from __future__ import annotations

import contextlib
import sys
import threading
import time
import webbrowser

import uvicorn

from opn_cockpit.migrations import MigrationError, run_pending_migrations
from opn_cockpit.web.server import create_app
from opn_cockpit.web.settings import WebSettings

_BROWSER_OPEN_DELAY_S = 0.7


def run(settings: WebSettings | None = None) -> int:
    """Startet den Server. Liefert Exit-Code.

    Bei ``settings.auto_open_browser=True`` wird nach kurzer Verzoegerung
    der Standard-Browser auf die Server-URL gerichtet. Die Verzoegerung
    verhindert, dass der Browser eine "Connection refused"-Seite sieht,
    falls der Server noch im Startup ist.
    """
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
