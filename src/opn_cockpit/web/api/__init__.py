"""API-Routen-Aggregation.

In Iteration 1 ist nur eine Smoke-Route registriert. Spaetere Iterationen
fuegen ``auth``, ``inventory``, ``plan``, ``apply``, ``audit``,
``discover``, ``vaults`` hinzu.
"""

from __future__ import annotations

from fastapi import APIRouter, FastAPI

from opn_cockpit import __version__
from opn_cockpit.web.api import about as about_routes
from opn_cockpit.web.api import audit as audit_routes
from opn_cockpit.web.api import auth as auth_routes
from opn_cockpit.web.api import bootstrap as bootstrap_routes
from opn_cockpit.web.api import discover as discover_routes
from opn_cockpit.web.api import imports as imports_routes
from opn_cockpit.web.api import inventory as inventory_routes
from opn_cockpit.web.api import plans as plans_routes
from opn_cockpit.web.api import profiles as profiles_routes
from opn_cockpit.web.api import retry as retry_routes
from opn_cockpit.web.api import users as users_routes
from opn_cockpit.web.api import vaults as vaults_routes

api_router = APIRouter(prefix="/api", tags=["api"])


@api_router.get("/version", include_in_schema=False)
def version() -> dict[str, str]:
    """Frontend nutzt das, um die Backend-Version anzuzeigen + zu validieren."""
    return {"version": __version__}


def register_api_routes(app: FastAPI) -> None:
    app.include_router(api_router)
    app.include_router(about_routes.router)
    app.include_router(bootstrap_routes.router)
    app.include_router(auth_routes.router)
    app.include_router(vaults_routes.router)
    app.include_router(inventory_routes.router)
    app.include_router(plans_routes.router)
    app.include_router(audit_routes.router)
    app.include_router(discover_routes.router)
    app.include_router(profiles_routes.router)
    app.include_router(imports_routes.router)
    app.include_router(retry_routes.router)
    app.include_router(users_routes.router)
