"""Folder-Browser-Endpoint fuer Single-User-Mode.

Der Browser kann keinen nativen Save-Dialog ausloesen, der dem Server
den vollen Pfad nennt. Stattdessen liefert dieser Endpoint dem Frontend
Verzeichnis-Auflistungen, mit denen es einen Picker rendern kann.

Sicherheits-Profil:

* Nur im **Single-User-Mode** verfuegbar (kein Multi-User-Server-Leak
  von Disk-Inhalten ueber das Netzwerk).
* Bewusst **ohne Auth** — der Server bindet sich per Default an
  ``127.0.0.1``, der einzige Akteur ist der lokale User selbst.
* Liefert nur **Unterverzeichnisse** + ggf. ``.opnvault``-Dateien
  (Existenz-Hinweis im Picker), keine sonstigen Dateien.
"""

from __future__ import annotations

import os
import string
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel

from opn_cockpit.vault.discovery import VAULT_EXTENSION

router = APIRouter(prefix="/api/files", tags=["files"])


class BrowseEntry(BaseModel):
    name: str
    path: str
    kind: str  # "drive" | "dir" | "vault"


class BrowseResponse(BaseModel):
    current: str           # absoluter Pfad des angezeigten Containers ("" wenn Drive-Liste)
    parent: str | None     # Eltern-Pfad, ``None`` bei Drive-Liste / Filesystem-Wurzel
    entries: list[BrowseEntry]
    error: str | None = None


def _list_drives_windows() -> list[BrowseEntry]:
    entries: list[BrowseEntry] = []
    for letter in string.ascii_uppercase:
        drive = f"{letter}:\\"
        try:
            exists = Path(drive).exists()
        except OSError:
            # Leerer Kartenleser, unmountete Volumes etc. werfen WinError 1005.
            continue
        if exists:
            entries.append(BrowseEntry(name=drive, path=drive, kind="drive"))
    return entries


def _list_dir(target: Path) -> list[BrowseEntry]:
    entries: list[BrowseEntry] = []
    try:
        children = sorted(target.iterdir(), key=lambda p: p.name.lower())
    except (OSError, PermissionError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Verzeichnis nicht lesbar: {exc}",
        ) from exc
    for child in children:
        try:
            is_dir = child.is_dir()
        except OSError:
            continue
        if is_dir:
            entries.append(BrowseEntry(
                name=child.name, path=str(child), kind="dir",
            ))
        elif child.suffix.lower() == VAULT_EXTENSION:
            entries.append(BrowseEntry(
                name=child.name, path=str(child), kind="vault",
            ))
    return entries


@router.get("/browse", response_model=BrowseResponse)
def browse(
    request: Request,
    path: str = Query(
        "",
        description=(
            "Absoluter Pfad. Leer = Home-Verzeichnis (Unix) bzw. Drive-Liste "
            "(Windows)."
        ),
    ),
) -> BrowseResponse:
    """Listet Inhalt eines Verzeichnisses (nur Unterordner + .opnvault-Dateien)."""
    server_state = request.app.state.server_state
    if not server_state.is_single_user_mode:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Folder-Picker ist nur im Single-User-Mode verfuegbar.",
        )

    if not path:
        # Erst-Aufruf ohne Pfad: Windows -> Drive-Liste, Unix -> Home.
        if os.name == "nt":
            return BrowseResponse(
                current="",
                parent=None,
                entries=_list_drives_windows(),
            )
        home = Path.home()
        return BrowseResponse(
            current=str(home),
            parent=None,
            entries=_list_dir(home),
        )

    candidate = Path(path)
    if not candidate.is_absolute():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Pfad muss absolut sein.",
        )
    try:
        resolved = candidate.resolve()
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Pfad nicht aufloesbar: {exc}",
        ) from exc

    if not resolved.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Verzeichnis existiert nicht: {resolved}",
        )
    if not resolved.is_dir():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Pfad ist kein Verzeichnis: {resolved}",
        )

    parent = resolved.parent
    # An der Filesystem-Wurzel (Drive-Root oder /): Windows -> Drive-Liste ("");
    # Unix -> kein hoeher (None).
    at_root = parent == resolved
    parent_str: str | None = (
        ("" if os.name == "nt" else None) if at_root else str(parent)
    )

    return BrowseResponse(
        current=str(resolved),
        parent=parent_str,
        entries=_list_dir(resolved),
    )


__all__ = ["router"]
