"""FastAPI App-Factory.

Erzeugt die App ohne Seiteneffekte (kein Server-Start). Tests instanziieren
die App via ``create_app()`` und fahren sie ueber ``TestClient`` ab.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.types import Message, Receive, Scope, Send

from opn_cockpit import __version__
from opn_cockpit.web.api import register_api_routes
from opn_cockpit.web.auth.manager import SessionManager

WEB_DIR = Path(__file__).parent
STATIC_DIR = WEB_DIR / "static"
TEMPLATES_DIR = WEB_DIR / "templates"


class NoCacheStaticFiles(StaticFiles):
    """StaticFiles, das ``Cache-Control: no-cache`` mitsendet.

    Verhindert, dass Browser zwischen Server-Restarts oder Iterationen
    eine alte ``app.js``/``styles.css`` zaeh halten. ``no-cache`` heisst
    "darf im Cache liegen, aber muss vor jedem Use revalidiert werden" —
    via etag/last-modified bleibt der Traffic minimal, aber stale
    Versionen sind ausgeschlossen.
    """

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def send_with_no_cache(message: Message) -> None:
            if message.get("type") == "http.response.start":
                headers = list(message.get("headers") or [])
                headers.append((b"cache-control", b"no-cache, must-revalidate"))
                message["headers"] = headers
            await send(message)

        await super().__call__(scope, receive, send_with_no_cache)


def create_app() -> FastAPI:
    """Konstruiert die FastAPI-Anwendung.

    Disabled Swagger/Redoc (``docs_url=None``), weil das Tool kein
    oeffentliches API ist — die internen Routen sind ueber die Code-Suche
    auffindbar. Bei spaeterer Multi-User-Variante kann ``docs_url`` per
    Setting wieder aktiviert werden.
    """
    app = FastAPI(
        title="OPN-Cockpit",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    app.state.templates = templates
    app.state.session_manager = SessionManager()

    if STATIC_DIR.exists():
        app.mount(
            "/static",
            NoCacheStaticFiles(directory=str(STATIC_DIR)),
            name="static",
        )

    @app.get("/health", response_class=HTMLResponse, include_in_schema=False)
    def health() -> str:
        """Trivialer Liveness-Check fuer den Boot-Wrapper."""
        return "ok"

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def index(request: Request) -> HTMLResponse:
        """Single-Page-Entry. JS uebernimmt Login-State und View-Switching."""
        return templates.TemplateResponse(
            request, "index.html", {"version": __version__}
        )

    # API-Routen einhaengen (in Iteration 2+ befuellt).
    register_api_routes(app)

    return app
