"""API-Routen-Aggregation.

In Iteration 1 ist nur eine Smoke-Route registriert. Spaetere Iterationen
fuegen ``auth``, ``inventory``, ``plan``, ``apply``, ``audit``,
``discover``, ``vaults`` hinzu.
"""

from __future__ import annotations

from fastapi import APIRouter, FastAPI

from opn_cockpit import __version__

api_router = APIRouter(prefix="/api", tags=["api"])


@api_router.get("/version", include_in_schema=False)
def version() -> dict[str, str]:
    """Frontend nutzt das, um die Backend-Version anzuzeigen + zu validieren."""
    return {"version": __version__}


def register_api_routes(app: FastAPI) -> None:
    app.include_router(api_router)
