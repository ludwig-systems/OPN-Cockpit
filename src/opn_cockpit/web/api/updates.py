"""Update-Check-Endpunkt.

Anonym aufrufbar — die Versionsnummer steht ohnehin schon im About-
Modal und auf der Login-Seite. Der Endpunkt nutzt den lokalen Cache,
damit GitHub-Rate-Limits nicht durch ueberbordendes Polling beruehrt
werden.

Heute wird der Check **synchron** im Handler ausgefuehrt; mit dem
Cache-Default (24 h) und einem ~5 s-Timeout ist das unproblematisch.
Sollte die Server-Last spaeter steigen, kann der Aufruf in einen
Hintergrund-Worker (analog ``retry_watcher``) ausgelagert werden.
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request

from opn_cockpit.updates import default_checker

router = APIRouter(prefix="/api/updates", tags=["updates"])


@router.get("/check", include_in_schema=False)
def check_for_update(
    request: Request,
    force: bool = Query(False, description="Cache umgehen und frisch abfragen"),
) -> dict[str, object]:
    """Liefert Update-Status (siehe ``UpdateCheckResult.to_dict``).

    ``force=true`` ist fuer manuelle Refresh-Buttons im UI — normale
    Banner-Loads sollten ohne ``force`` aufrufen und den Cache nutzen.
    """
    server_state = request.app.state.server_state
    settings = server_state.settings
    checker = default_checker()
    result = checker.check(settings, force=force)
    return result.to_dict()


__all__ = ["router"]
