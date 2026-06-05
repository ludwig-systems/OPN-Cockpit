"""FastAPI App-Factory.

Erzeugt die App ohne Seiteneffekte (kein Server-Start). Tests instanziieren
die App via ``create_app()`` und fahren sie ueber ``TestClient`` ab.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.types import Message, Receive, Scope, Send

from opn_cockpit import __version__
from opn_cockpit.config import get_app_data_dir
from opn_cockpit.runtime_version import get_runtime_version
from opn_cockpit.web.api import register_api_routes
from opn_cockpit.web.auth.manager import SessionManager
from opn_cockpit.web.backup_scheduler import BackupScheduler
from opn_cockpit.web.rate_limit import RateLimiter
from opn_cockpit.web.retry_watcher import QUEUE_FILE_NAME, RetryWatcher
from opn_cockpit.web.safety_net_watcher import SafetyNetWatcher
from opn_cockpit.web.server_state import ServerState

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
    # Effective Version: bevorzugt das Git-Tag wenn das Repo eines hat,
    # sonst Fallback auf das hardcoded __version__ aus __init__.py.
    # Damit zeigen Login-Maske/Topbar/About im Linux-Container die echte
    # Release-Version statt der dev-Bezeichnung aus main.
    effective_version = get_runtime_version()
    app = FastAPI(
        title="OPN-Cockpit",
        version=effective_version,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    app.state.templates = templates
    session_manager = SessionManager()
    app.state.session_manager = session_manager
    # Retry-Queue persistiert in <app_data>/state/retry-queue.json damit
    # Jobs Container-Restarts ueberleben (Orphan-Adoption beim naechsten
    # Vault-Unlock). Pfad-Resolution via OPNCOCKPIT_DATA_DIR-Override
    # damit Tests einen tmp_path injizieren koennen.
    retry_queue_path = get_app_data_dir() / "state" / QUEUE_FILE_NAME
    app.state.retry_watcher = RetryWatcher(
        session_manager, queue_path=retry_queue_path,
    )
    app.state.server_state = ServerState.from_settings()
    # Scheduler kennt jetzt sowohl SessionManager (Single-User) als auch
    # ServerState (Multi-User-Server-Mode mit zentralem Vault). Bug aus
    # User-Test 2026-06-04: Tresor war 20h nach Browser-Logout still,
    # weil nur Sessions iteriert wurden. Mit server_state laeuft der
    # Scheduler durchgehend solange der Container an ist.
    app.state.backup_scheduler = BackupScheduler(
        session_manager, server_state=app.state.server_state,
    )
    app.state.backup_scheduler.start()
    # SafetyNetWatcher: bewusst in-memory (kein Persistenz), weil ein
    # armed Apply nur waehrend der User-Session sinnvoll laeuft.
    # AuditBackend wird im Watcher gebraucht damit Auto-Rollback im
    # Hintergrund-Thread auditbar bleibt.
    from opn_cockpit.audit.backend import get_audit_backend  # noqa: PLC0415
    app.state.safety_net_watcher = SafetyNetWatcher(get_audit_backend())
    app.state.safety_net_watcher.start()
    app.state.login_rate_limiter = RateLimiter()
    app.state.bootstrap_rate_limiter = RateLimiter(
        # Bootstrap ist seltener als Login — strenger limitieren.
        max_attempts=5, window_s=60 * 60.0, cooldown_s=10 * 60.0,
    )

    _install_security_middleware(app)

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
        """Single-Page-Entry. JS uebernimmt Login-State und View-Switching.

        Response erhaelt ``Cache-Control: no-cache, must-revalidate`` —
        sonst behaelt der Browser die HTML zaeh und kommt nicht an neue
        Asset-URLs (die wir per mtime-Hash invalidieren).
        """
        response = templates.TemplateResponse(
            request,
            "index.html",
            {
                "version": effective_version,
                "asset_version": _asset_version(),
            },
        )
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response

    # API-Routen einhaengen (in Iteration 2+ befuellt).
    register_api_routes(app)

    return app


def _asset_version() -> str:
    """Cache-Buster fuer ``/static/*``-URLs (mtime der Frontend-Assets).

    Jede Patch-Aenderung an ``app.js`` oder ``styles.css`` verschiebt
    den Hash automatisch — der Browser muss neu laden, ohne dass der
    User Strg+Shift+R druecken oder Cookies leeren muss. Bei
    Build-Artefakten (Bundle/Installer) bleibt der Hash stabil solange
    die Asset-Dateien nicht angefasst werden.
    """
    parts: list[str] = [__version__]
    for asset in ("static/app.js", "static/styles.css", "static/favicon.svg"):
        path = WEB_DIR / asset
        try:
            parts.append(str(path.stat().st_mtime_ns))
        except OSError:
            parts.append("0")
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:10]
    return f"{__version__}-{digest}"


def _install_security_middleware(app: FastAPI) -> None:
    """Setzt Security-Header auf alle Responses (Audit #6 + v4-Pass 4).

    * ``X-Content-Type-Options: nosniff`` — verhindert MIME-Sniffing
    * ``X-Frame-Options: DENY`` — Clickjacking-Schutz
    * ``Referrer-Policy: same-origin`` — kein Referrer-Leak nach extern
    * ``Content-Security-Policy`` — XSS-Defense-in-Depth (eng gesetzt,
      eigene Scripts + Inline-Styles erlaubt — alles andere blockiert)
    * ``Strict-Transport-Security`` — nur aktiv wenn
      ``OPNCOCKPIT_HSTS_ENABLED=1`` (oder beim TLS-Bind). Bricht sonst
      lokale http-Setups, wenn der Browser einmal HSTS gecacht hat.
    """
    csp = (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'"
    )
    hsts_enabled = os.environ.get("OPNCOCKPIT_HSTS_ENABLED", "").strip() in {"1", "true", "yes"}
    hsts_max_age = os.environ.get("OPNCOCKPIT_HSTS_MAX_AGE", "31536000").strip()
    hsts_header = f"max-age={hsts_max_age}; includeSubDomains"

    @app.middleware("http")
    async def _add_security_headers(request: Request, call_next):  # type: ignore[no-untyped-def]
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("Content-Security-Policy", csp)
        if hsts_enabled:
            response.headers.setdefault("Strict-Transport-Security", hsts_header)
        return response
