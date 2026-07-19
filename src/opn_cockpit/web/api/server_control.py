"""Server-Restart-Endpoint.

Admin-only. Triggert einen sauberen Prozess-Restart, damit ein neu
hinterlegtes TLS-Cert (oder ein rotiertes Auto-Cert) aktiv wird.

Plattform-Detection:

* **Windows Service-Mode** (NSSM erkannt): ``nssm restart <name>``
  wird verzoegert per detached ``cmd.exe`` gestartet. Damit die
  HTTP-Antwort noch durchgeht, wartet der Wrapper 2 Sekunden.
* **Linux systemd**: ``systemctl restart <name>`` per detached shell.
  Braucht polkit-Regel (siehe ``installer/linux/``).
* **Dev-Mode** (kein Service erkannt): HTTP 501 mit klarer Meldung.
  Wer im Dev-Loop testen will, kann ``OPNCOCKPIT_RESTART_MODE=nssm``
  setzen, aber das ist Selbstverantwortung.

Modul stellt zusaetzlich :func:`get_restart_callback` bereit — der
:class:`TlsWatcher` benutzt das, um bei abgelaufenem Auto-Cert einen
automatischen Restart auszuloesen (ohne Admin-Klick).
"""

from __future__ import annotations

import logging
import subprocess
import sys
from typing import Callable

from fastapi import APIRouter, Depends, HTTPException, status

from opn_cockpit.audit.backend import audit_actor, get_audit_backend
from opn_cockpit.audit.log import AuditEventKind
from opn_cockpit.security.session import Session
from opn_cockpit.web.acl import require_admin_role
from opn_cockpit.web.auth.dependencies import require_session
from opn_cockpit.web.settings import (
    RESTART_MODE_DEV,
    RESTART_MODE_NSSM,
    RESTART_MODE_SYSTEMD,
    detect_service_mode,
    service_name,
)

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/server", tags=["server-control"])


@router.post("/restart", status_code=status.HTTP_202_ACCEPTED)
def restart_server(
    session: Session = Depends(require_session),
) -> dict[str, str]:
    """Loest einen Prozess-Restart aus.

    Antwortet mit HTTP 202 sobald der detached Sub-Prozess gestartet
    ist (die eigentliche Restart-Aktion folgt nach ~2 s Delay). Der
    Browser sollte danach nach ~5 s automatisch reload versuchen.

    Im Dev-Mode liefert der Endpoint HTTP 501 mit klarer Meldung.
    """
    session.touch()
    require_admin_role(session)

    mode = detect_service_mode()
    if mode == RESTART_MODE_DEV:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=(
                "Restart-Endpoint nur im Service-Mode verfuegbar. "
                "Bitte den opn-cockpit-Prozess manuell neu starten "
                "oder OPNCOCKPIT_RESTART_MODE=nssm|systemd setzen wenn "
                "du weisst was du tust."
            ),
        )

    actor = audit_actor(session)
    try:
        _spawn_restart(mode)
    except OSError as exc:
        _log.exception("Restart-Trigger fehlgeschlagen")
        get_audit_backend().append(
            AuditEventKind.SERVER_RESTARTED,
            actor=actor,
            action="server_restart_failed",
            error_kind="restart_failed",
            summary=f"Server-Restart konnte nicht ausgeloest werden ({mode}): {exc}",
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Restart-Trigger fehlgeschlagen: {exc}",
        ) from exc

    get_audit_backend().append(
        AuditEventKind.SERVER_RESTARTED,
        actor=actor,
        action="server_restart_requested",
        summary=(
            f"Server-Restart ausgeloest ({mode}). Prozess kommt in ~5 s wieder hoch."
        ),
    )
    return {
        "status": "scheduled",
        "mode": mode,
        "message": (
            "Server-Restart eingeleitet. Bitte in ~5 Sekunden neu laden."
        ),
    }


# ---------------------------------------------------------------------------
# Public helper fuer TlsWatcher
# ---------------------------------------------------------------------------


def get_restart_callback() -> Callable[[], None] | None:
    """Liefert eine 0-arg Callable die den Prozess-Restart triggert.

    ``None`` im Dev-Mode - der Watcher soll dann NICHT versuchen zu
    restarten. Der Aufrufer (``create_app``) reicht das an den
    :class:`TlsWatcher` durch.
    """
    mode = detect_service_mode()
    if mode == RESTART_MODE_DEV:
        return None

    def _cb() -> None:
        _spawn_restart(mode)

    return _cb


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _spawn_restart(mode: str) -> None:
    """Startet den Restart-Subprozess detached und returned sofort."""
    if mode == RESTART_MODE_NSSM:
        name = service_name()
        # cmd /c "timeout /t 2 /nobreak >nul & nssm restart <name>"
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP damit der Prozess
        # nicht mit dem uvicorn stirbt.
        creationflags = 0
        if sys.platform == "win32":
            # Kein direkter Import von subprocess.DETACHED_PROCESS damit
            # der Modul-Import auf Linux nicht crashed.
            creationflags = 0x00000008 | 0x00000200  # DETACHED | NEW_GROUP
        subprocess.Popen(  # noqa: S603
            [
                "cmd",
                "/c",
                f'timeout /t 2 /nobreak >nul & nssm restart "{name}"',
            ],
            creationflags=creationflags,
            close_fds=True,
        )
        return

    if mode == RESTART_MODE_SYSTEMD:
        name = service_name()
        # sh -c "sleep 2 && systemctl restart <name>"
        # start_new_session damit der Prozess unabhaengig vom uvicorn
        # weiterlaeuft, auch wenn der uvicorn im Zuge des Restarts stirbt.
        subprocess.Popen(  # noqa: S603
            ["sh", "-c", f"sleep 2 && systemctl restart {name}"],
            start_new_session=True,
            close_fds=True,
        )
        return

    # Kein bekannter Mode - fallback, wir werfen bewusst.
    msg = f"Unbekannter Restart-Mode: {mode}"
    raise OSError(msg)


__all__ = ["get_restart_callback", "restart_server", "router"]
