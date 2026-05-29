"""Web-Auth-Schicht: SessionManager + FastAPI-Dependencies."""

from opn_cockpit.web.auth.dependencies import (
    get_session_manager,
    require_session,
    require_session_with_token,
)
from opn_cockpit.web.auth.manager import SessionManager

__all__ = [
    "SessionManager",
    "get_session_manager",
    "require_session",
    "require_session_with_token",
]
