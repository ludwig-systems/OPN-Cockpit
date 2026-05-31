"""About-Endpoint: liefert Versions- und Entwickler-Metadaten.

Bewusst ohne Auth — die Versionsnummer steht ohnehin schon auf der
Login-Seite (Footer + Boot-Splash). Der Endpoint ist die strukturierte
Quelle fuer das About-Modal im Frontend und kann spaeter auch vom
Update-Check ausgewertet werden.
"""

from __future__ import annotations

from fastapi import APIRouter

from opn_cockpit import (
    __author__,
    __author_email__,
    __github_url__,
    __license_label__,
    __version__,
)

router = APIRouter(prefix="/api", tags=["about"])


@router.get("/about", include_in_schema=False)
def about() -> dict[str, str]:
    """Versionsstand + Entwickler-Kontakt + Repo-URL."""
    return {
        "name": "OPN-Cockpit",
        "version": __version__,
        "author": __author__,
        "author_email": __author_email__,
        "github_url": __github_url__,
        "license": __license_label__,
    }


__all__ = ["router"]
