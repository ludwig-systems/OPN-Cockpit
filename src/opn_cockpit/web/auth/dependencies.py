"""FastAPI-Depends-Helfer: Session aus Bearer-Token aufloesen."""

from __future__ import annotations

from fastapi import Depends, Header, HTTPException, Request, status

from opn_cockpit.security.session import Session
from opn_cockpit.web.auth.manager import SessionManager

BEARER_PREFIX = "Bearer "


def get_session_manager(request: Request) -> SessionManager:
    """Liefert den serverseitigen SessionManager.

    Wird beim ``create_app`` als ``app.state.session_manager`` gesetzt.
    """
    manager = getattr(request.app.state, "session_manager", None)
    if not isinstance(manager, SessionManager):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="SessionManager not initialised",
        )
    return manager


def require_session_with_token(
    authorization: str | None = Header(None),
    manager: SessionManager = Depends(get_session_manager),
) -> tuple[Session, str]:
    """Akzeptiert das Bearer-Token und liefert (Session, Token).

    Wird intern von ``require_session`` benutzt und in der Logout-Route,
    die das Token zur Revoke-Logik braucht.
    """
    # Audit-Finding G4: RFC 6750 schreibt den Scheme-Namen "Bearer" als
    # case-insensitive vor. Vorher wurde nur "Bearer " (exakt) akzeptiert;
    # ein Browser/Client mit kleingeschriebenem "bearer" hatte das Token
    # also nicht durchgereicht. Wir splitten am ersten Whitespace und
    # vergleichen scheme-case-insensitive.
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = parts[1].strip()
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Empty token.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    session = manager.get(token)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session expired or invalid.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return session, token


def require_session(
    pair: tuple[Session, str] = Depends(require_session_with_token),
) -> Session:
    """Kurzform fuer Routen, die nur die Session brauchen — nicht das Token."""
    return pair[0]


def require_admin(
    session: Session = Depends(require_session),
) -> Session:
    """Routen-Guard fuer Admin-only-Endpunkte (Multi-User-Mode).

    Verlangt eine Multi-User-Session (``session.user`` ist gesetzt) mit
    Rolle ``admin``. Schlaegt sonst mit 403 fehl. Im Single-User-Mode ist
    ``session.user`` immer None — User-Verwaltungs-Endpunkte sind dort
    nicht aufrufbar.
    """
    user = session.user
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "User-Verwaltung ist nur im Multi-User-Mode verfuegbar. "
                "Aktiviere OPNCOCKPIT_AUTH_BACKEND=user-db im Server."
            ),
        )
    if user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Nur Admin-User duerfen diese Aktion ausfuehren.",
        )
    return session
