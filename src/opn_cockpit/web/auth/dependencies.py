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
    if not authorization or not authorization.startswith(BEARER_PREFIX):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = authorization[len(BEARER_PREFIX) :].strip()
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
