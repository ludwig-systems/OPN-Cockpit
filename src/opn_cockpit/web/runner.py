"""Boot-Wrapper: uvicorn + optional Browser-Auto-Open.

``run()`` startet den FastAPI-Server in der aktuellen Thread und blockiert
bis zum Beenden (Ctrl+C oder OS-Signal). Der Browser-Start passiert in
einem Daemon-Thread mit kleinem Delay, damit der Server zur Begruessung
schon antwortet.
"""

from __future__ import annotations

import contextlib
import threading
import time
import webbrowser

import uvicorn

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
