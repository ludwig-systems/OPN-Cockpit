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

import ipaddress
import os
import string
import sys
import threading
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel

from opn_cockpit.vault.discovery import VAULT_EXTENSION

router = APIRouter(prefix="/api/files", tags=["files"])

# Native Folder-Picker (Windows): SHBrowseForFolderW darf nicht parallel
# aufgerufen werden — sonst kollidieren mehrere offene Dialoge.
_dialog_lock = threading.Lock()

# Audit-Finding G3: file-Picker-Endpoints sind un-auth (Setup-Wizard
# braucht sie vor Login). Damit niemand sie aus dem LAN nutzen kann
# wenn der Single-User-Mode versehentlich auf 0.0.0.0 gebunden wurde,
# erlauben wir den Picker nur von Loopback-Origins.
#
# ``request.client.host`` ist die echte Socket-Source-IP vom ASGI-Server
# (uvicorn) - die kann nicht ueber Header-Spoofing manipuliert werden,
# anders als X-Forwarded-For. Bei ``starlette.testclient.TestClient`` ist
# der Wert konventionell ``"testclient"``; wir lassen das durch, damit
# Tests laufen.
_LOOPBACK_LITERAL_HOSTS: frozenset[str] = frozenset({
    "localhost", "testclient",
})


def _is_loopback_origin(host: str) -> bool:
    if not host:
        return False
    if host in _LOOPBACK_LITERAL_HOSTS:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _require_loopback_origin(request: Request) -> None:
    """Wirft 403 wenn der Request nicht von einer Loopback-Adresse kommt.

    Schutz fuer File-Picker und Folder-Browser im Single-User-Mode, falls
    der Bind versehentlich auf 0.0.0.0 oder eine LAN-Adresse gestellt wurde.
    Im Multi-User-Mode greift schon vorher der ``is_single_user_mode``-Check.
    """
    client = request.client
    host = client.host if client else ""
    if not _is_loopback_origin(host):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "File-Picker ist nur von Loopback erreichbar (Single-User-PAW). "
                "Fuer Remote-Bedienung den Multi-User-Server-Mode mit "
                "manueller Pfad-Eingabe nutzen."
            ),
        )


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
    _require_loopback_origin(request)

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


class PickFolderResponse(BaseModel):
    path: str | None
    cancelled: bool


def _pick_folder_native_windows(title: str = "OPN-Cockpit — Ordner waehlen") -> str | None:
    """Oeffnet den Windows-Shell-Folder-Picker (Vista-Style).

    Blockiert den aufrufenden Thread bis der User waehlt oder abbricht.
    Liefert den absoluten Pfad als String oder ``None`` bei Abbruch.
    Nur unter Windows; auf anderen Plattformen ``OSError``.

    Nutzt SHBrowseForFolderW (immer verfuegbar, ab XP). Per
    ``BIF_USENEWUI`` bekommen wir den modernen Vista+-Look mit Resize
    und Edit-Feld. ``GetForegroundWindow()`` als ``hwndOwner`` sorgt
    dafuer dass der Dialog vor dem Browser auftaucht statt im
    Hintergrund zu verschwinden.
    """
    if sys.platform != "win32":
        raise OSError("Native folder picker only on Windows")

    import ctypes  # noqa: PLC0415 — Windows-only, vermeidet Import auf Linux
    from ctypes import wintypes  # noqa: PLC0415

    bif_returnonlyfsdirs = 0x00000001
    bif_usenewui = 0x00000040 | 0x00000010  # NEWDIALOGSTYLE | EDITBOX
    coinit_apartmentthreaded = 0x2

    class BROWSEINFOW(ctypes.Structure):
        _fields_ = (
            ("hwndOwner", wintypes.HWND),
            ("pidlRoot", ctypes.c_void_p),
            ("pszDisplayName", wintypes.LPWSTR),
            ("lpszTitle", wintypes.LPCWSTR),
            ("ulFlags", wintypes.UINT),
            ("lpfn", ctypes.c_void_p),
            ("lParam", wintypes.LPARAM),
            ("iImage", ctypes.c_int),
        )

    shell32 = ctypes.windll.shell32
    ole32 = ctypes.windll.ole32
    user32 = ctypes.windll.user32

    sh_browse = shell32.SHBrowseForFolderW
    sh_browse.argtypes = (ctypes.POINTER(BROWSEINFOW),)
    sh_browse.restype = ctypes.c_void_p

    sh_get_path = shell32.SHGetPathFromIDListW
    sh_get_path.argtypes = (ctypes.c_void_p, wintypes.LPWSTR)
    sh_get_path.restype = wintypes.BOOL

    co_task_mem_free = ole32.CoTaskMemFree
    co_task_mem_free.argtypes = (ctypes.c_void_p,)

    ole32.CoInitializeEx(None, coinit_apartmentthreaded)
    try:
        display_buf = ctypes.create_unicode_buffer(260)
        bi = BROWSEINFOW()
        bi.hwndOwner = user32.GetForegroundWindow()
        bi.pidlRoot = None
        bi.pszDisplayName = ctypes.cast(display_buf, wintypes.LPWSTR)
        bi.lpszTitle = title
        bi.ulFlags = bif_returnonlyfsdirs | bif_usenewui
        bi.lpfn = None
        bi.lParam = 0
        bi.iImage = 0

        pidl = sh_browse(ctypes.byref(bi))
        if not pidl:
            return None
        try:
            path_buf = ctypes.create_unicode_buffer(1024)
            if not sh_get_path(pidl, path_buf):
                return None
            return path_buf.value
        finally:
            co_task_mem_free(pidl)
    finally:
        ole32.CoUninitialize()


@router.get("/pick-folder", response_model=PickFolderResponse)
def pick_folder_native(request: Request) -> PickFolderResponse:
    """Oeffnet den nativen OS-Folder-Picker auf dem Server.

    Nur sinnvoll wenn Server und Browser auf demselben Geraet laufen
    (Single-User-Mode lokal). Im Multi-User-Server-Mode wird der Web-
    Browser-Picker (``/api/files/browse``) verwendet.

    Auf Nicht-Windows-Systemen wird ``501 Not Implemented`` geliefert —
    das Frontend soll dann auf den Web-Picker fallback machen.
    """
    server_state = request.app.state.server_state
    if not server_state.is_single_user_mode:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Native Picker ist nur im Single-User-Mode verfuegbar.",
        )
    _require_loopback_origin(request)
    if sys.platform != "win32":
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Native Picker derzeit nur unter Windows verfuegbar.",
        )

    with _dialog_lock:
        try:
            picked = _pick_folder_native_windows()
        except OSError as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Folder-Picker fehlgeschlagen: {exc}",
            ) from exc

    if picked is None:
        return PickFolderResponse(path=None, cancelled=True)
    return PickFolderResponse(path=picked, cancelled=False)


class PickFileResponse(BaseModel):
    path: str | None
    cancelled: bool


def _pick_file_native_windows(
    title: str = "OPN-Cockpit — Tresor-Datei waehlen",
    filter_label: str = "OPN-Cockpit Tresor",
    filter_pattern: str = "*.opnvault",
) -> str | None:
    """Oeffnet den Windows-Datei-Open-Dialog (GetOpenFileNameW).

    Liefert den absoluten Pfad oder ``None`` bei Abbruch. Nur unter
    Windows. Filter ist auf ``.opnvault`` voreingestellt; der User kann
    im Dialog auf "Alle Dateien" umschalten.
    """
    if sys.platform != "win32":
        raise OSError("Native file picker only on Windows")

    import ctypes  # noqa: PLC0415 — Windows-only
    from ctypes import wintypes  # noqa: PLC0415

    ofn_explorer = 0x00080000
    ofn_filemustexist = 0x00001000
    ofn_pathmustexist = 0x00000800
    ofn_hidereadonly = 0x00000004
    ofn_nochangedir = 0x00000008

    class OPENFILENAMEW(ctypes.Structure):
        _fields_ = (
            ("lStructSize", wintypes.DWORD),
            ("hwndOwner", wintypes.HWND),
            ("hInstance", wintypes.HINSTANCE),
            ("lpstrFilter", wintypes.LPCWSTR),
            ("lpstrCustomFilter", wintypes.LPWSTR),
            ("nMaxCustFilter", wintypes.DWORD),
            ("nFilterIndex", wintypes.DWORD),
            ("lpstrFile", wintypes.LPWSTR),
            ("nMaxFile", wintypes.DWORD),
            ("lpstrFileTitle", wintypes.LPWSTR),
            ("nMaxFileTitle", wintypes.DWORD),
            ("lpstrInitialDir", wintypes.LPCWSTR),
            ("lpstrTitle", wintypes.LPCWSTR),
            ("Flags", wintypes.DWORD),
            ("nFileOffset", wintypes.WORD),
            ("nFileExtension", wintypes.WORD),
            ("lpstrDefExt", wintypes.LPCWSTR),
            ("lCustData", wintypes.LPARAM),
            ("lpfnHook", ctypes.c_void_p),
            ("lpTemplateName", wintypes.LPCWSTR),
            ("pvReserved", ctypes.c_void_p),
            ("dwReserved", wintypes.DWORD),
            ("FlagsEx", wintypes.DWORD),
        )

    comdlg32 = ctypes.windll.comdlg32
    user32 = ctypes.windll.user32

    get_open_file_name = comdlg32.GetOpenFileNameW
    get_open_file_name.argtypes = (ctypes.POINTER(OPENFILENAMEW),)
    get_open_file_name.restype = wintypes.BOOL

    # Filter-String: paerchenweise label\0pattern\0, terminiert mit \0\0
    filter_str = f"{filter_label} ({filter_pattern})\0{filter_pattern}\0Alle Dateien (*.*)\0*.*\0\0"

    path_buf = ctypes.create_unicode_buffer(1024)
    ofn = OPENFILENAMEW()
    ofn.lStructSize = ctypes.sizeof(OPENFILENAMEW)
    ofn.hwndOwner = user32.GetForegroundWindow()
    ofn.lpstrFilter = filter_str
    ofn.nFilterIndex = 1
    ofn.lpstrFile = ctypes.cast(path_buf, wintypes.LPWSTR)
    ofn.nMaxFile = len(path_buf)
    ofn.lpstrTitle = title
    ofn.Flags = (
        ofn_explorer | ofn_filemustexist | ofn_pathmustexist
        | ofn_hidereadonly | ofn_nochangedir
    )
    ofn.lpstrDefExt = "opnvault"

    if not get_open_file_name(ctypes.byref(ofn)):
        return None
    return path_buf.value


@router.get("/pick-file", response_model=PickFileResponse)
def pick_file_native(
    request: Request,
    title: str = Query(
        "OPN-Cockpit — Tresor-Datei waehlen",
        description="Fenstertitel des Datei-Dialogs.",
    ),
) -> PickFileResponse:
    """Oeffnet den nativen OS-Datei-Picker auf dem Server.

    Filter ist fix auf ``.opnvault`` (kann im Dialog auf "Alle Dateien"
    geschaltet werden). Nur sinnvoll wenn Server und Browser auf
    derselben Maschine laufen - Single-User-Local-Setup.

    Nicht-Windows: 501. Frontend macht Fallback auf Pfad-Eingabe.
    """
    server_state = request.app.state.server_state
    if not server_state.is_single_user_mode:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Native Picker ist nur im Single-User-Mode verfuegbar.",
        )
    _require_loopback_origin(request)
    if sys.platform != "win32":
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Native Picker derzeit nur unter Windows verfuegbar.",
        )

    with _dialog_lock:
        try:
            picked = _pick_file_native_windows(title=title)
        except OSError as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"File-Picker fehlgeschlagen: {exc}",
            ) from exc

    if picked is None:
        return PickFileResponse(path=None, cancelled=True)
    return PickFileResponse(path=picked, cancelled=False)


__all__ = ["router"]
