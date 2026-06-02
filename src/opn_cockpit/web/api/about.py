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
from opn_cockpit.runtime_version import get_runtime_version

router = APIRouter(prefix="/api", tags=["about"])


@router.get("/about", include_in_schema=False)
def about() -> dict[str, str]:
    """Versionsstand + Entwickler-Kontakt + Repo-URL.

    ``version`` ist die *effektive* Version: Git-Tag wenn der Container
    von main gepullt hat (z.B. ``v0.6.4``), sonst die Source-Konstante
    ``__version__`` aus __init__.py (z.B. ``0.6.4`` beim Windows-
    Installer der vom Workflow gepatcht wurde).

    ``version_source`` ist die rohe ``__version__``-Konstante, fuer
    Debugging / Anzeige als sekundaere Info.
    """
    return {
        "name": "OPN-Cockpit",
        "version": get_runtime_version(),
        "version_source": __version__,
        "author": __author__,
        "author_email": __author_email__,
        "github_url": __github_url__,
        "license": __license_label__,
    }


__all__ = ["router"]
