"""User-Verwaltungs-Routen (Multi-User-Mode, admin-only).

Schnittstelle fuer die Admin-UI:

* ``GET /api/users`` — Liste aller User (admin).
* ``POST /api/users`` — neuen User anlegen (admin).
* ``PATCH /api/users/{id}`` — Rolle / Tag-ACL / disabled-Flag aendern (admin).
* ``DELETE /api/users/{id}`` — User loeschen (admin).
* ``POST /api/users/{id}/password`` — Admin setzt das Passwort eines
  anderen Users zurueck (admin).
* ``POST /api/users/me/password`` — Self-Service-Passwortwechsel
  (jeder eingeloggte User).

Im Single-User-Mode sind diese Endpunkte nicht erreichbar — ``require_admin``
schlaegt mit 403 fehl, weil ``session.user is None``.

Sicherheitsmassnahmen:
* Admin kann sich nicht selbst loeschen oder deaktivieren (sonst lockt
  er das System aus).
* Der letzte aktive Admin kann nicht zur viewer-Rolle degradiert oder
  deaktiviert werden.
* Alle Mutationen landen im Audit-Log mit dem Admin-Username als actor.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from opn_cockpit.audit.backend import get_audit_backend
from opn_cockpit.audit.log import AuditEventKind
from opn_cockpit.security.session import Session
from opn_cockpit.security.users import Role, User, UserStore, UserStoreError
from opn_cockpit.web.api.bootstrap import get_server_state
from opn_cockpit.web.api.schemas import (
    AdminPasswordResetRequest,
    PasswordChangeRequest,
    UserCreateRequest,
    UserListResponse,
    UserResponse,
    UserUpdateRequest,
)
from opn_cockpit.web.auth.dependencies import require_admin, require_session
from opn_cockpit.web.server_state import ServerState

router = APIRouter(prefix="/api/users", tags=["users"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _user_store(server: ServerState) -> UserStore:
    """Liefert den UserStore — wird vom require_admin-Pfad implizit garantiert."""
    store = server.user_store
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="User-Store nicht initialisiert.",
        )
    return store


def _to_response(user: User) -> UserResponse:
    return UserResponse(
        id=user.id,
        username=user.username,
        role=user.role,
        allowed_tags=list(user.allowed_tags),
        created_at_iso=user.created_at_iso,
        last_login_at_iso=user.last_login_at_iso,
        disabled=user.disabled,
    )


def _count_active_admins(store: UserStore) -> int:
    return sum(
        1
        for u in store.list_users()
        if u.role == "admin" and not u.disabled
    )


def _audit(event: AuditEventKind, actor: str, summary: str) -> None:
    """Schreibt einen Audit-Eintrag mit dem eingeloggten Username als actor."""
    get_audit_backend().append(event, actor=actor, summary=summary)


# ---------------------------------------------------------------------------
# Routen
# ---------------------------------------------------------------------------


@router.get("", response_model=UserListResponse)
def list_users(
    server: ServerState = Depends(get_server_state),
    _admin: Session = Depends(require_admin),
) -> UserListResponse:
    """Liefert alle User. Nur fuer Admins."""
    store = _user_store(server)
    return UserListResponse(users=[_to_response(u) for u in store.list_users()])


@router.post(
    "",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_user(
    payload: UserCreateRequest,
    server: ServerState = Depends(get_server_state),
    admin: Session = Depends(require_admin),
) -> UserResponse:
    """Legt einen neuen User an. Nur fuer Admins."""
    store = _user_store(server)
    try:
        user = store.create_user(
            username=payload.username,
            password=payload.password,
            role=_role_or_400(payload.role),
            allowed_tags=tuple(payload.allowed_tags),
        )
    except UserStoreError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    assert admin.user is not None
    _audit(
        AuditEventKind.USER_CREATED,
        actor=admin.user.username,
        summary=f"User '{payload.username}' (Rolle: {payload.role}) angelegt.",
    )
    return _to_response(user)


@router.patch("/{user_id}", response_model=UserResponse)
def update_user(
    user_id: int,
    payload: UserUpdateRequest,
    server: ServerState = Depends(get_server_state),
    admin: Session = Depends(require_admin),
) -> UserResponse:
    """Aktualisiert Rolle / Tag-ACL / disabled-Flag. Nur fuer Admins."""
    store = _user_store(server)
    target = store.get_user(user_id)
    if target is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User-ID {user_id} nicht gefunden.",
        )
    assert admin.user is not None

    # Selbst-Schutz: Admin darf sich nicht selbst deaktivieren.
    if target.id == admin.user.id:
        if payload.disabled is True:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Du kannst dich nicht selbst deaktivieren.",
            )
        if payload.role is not None and payload.role != "admin":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Du kannst deine eigene Admin-Rolle nicht entfernen.",
            )

    # Last-Admin-Schutz: nicht den letzten aktiven Admin entfernen.
    if target.role == "admin" and not target.disabled:
        admin_change = (
            (payload.role is not None and payload.role != "admin")
            or payload.disabled is True
        )
        if admin_change and _count_active_admins(store) <= 1:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "Letzter aktiver Admin kann nicht degradiert oder "
                    "deaktiviert werden — sonst sperrst du dich aus."
                ),
            )

    try:
        updated = store.update_user(
            user_id,
            role=_role_or_none(payload.role),
            allowed_tags=(
                tuple(payload.allowed_tags) if payload.allowed_tags is not None else None
            ),
            disabled=payload.disabled,
        )
    except UserStoreError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    _audit(
        AuditEventKind.USER_UPDATED,
        actor=admin.user.username,
        summary=(
            f"User '{updated.username}' aktualisiert "
            f"(Rolle: {updated.role}, disabled: {updated.disabled})."
        ),
    )
    return _to_response(updated)


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_user(
    user_id: int,
    server: ServerState = Depends(get_server_state),
    admin: Session = Depends(require_admin),
) -> None:
    """Loescht einen User. Nur fuer Admins. Selbst-Loeschung untersagt."""
    store = _user_store(server)
    target = store.get_user(user_id)
    if target is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User-ID {user_id} nicht gefunden.",
        )
    assert admin.user is not None
    if target.id == admin.user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Du kannst dich nicht selbst loeschen.",
        )
    if target.role == "admin" and not target.disabled and _count_active_admins(store) <= 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Letzter aktiver Admin kann nicht geloescht werden.",
        )
    store.delete_user(user_id)
    _audit(
        AuditEventKind.USER_DELETED,
        actor=admin.user.username,
        summary=f"User '{target.username}' geloescht.",
    )


@router.post("/me/password", status_code=status.HTTP_204_NO_CONTENT)
def change_own_password(
    payload: PasswordChangeRequest,
    server: ServerState = Depends(get_server_state),
    session: Session = Depends(require_session),
) -> None:
    """Self-Service: eingeloggter User aendert sein eigenes Passwort.

    Nur im Multi-User-Mode sinnvoll — im Single-Mode gibt es kein
    User-Konzept. Erfordert Verifikation des aktuellen Passworts.
    """
    if session.user is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Self-Service-Passwortwechsel ist nur im Multi-User-Mode "
                "verfuegbar."
            ),
        )
    store = _user_store(server)
    # Aktuelles Passwort verifizieren — sonst koennte ein gestohlenes Token
    # zur Passwort-Uebernahme reichen.
    verified = store.authenticate(session.user.username, payload.current_password)
    if verified is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Aktuelles Passwort falsch.",
        )
    try:
        store.change_password(session.user.id, payload.new_password)
    except UserStoreError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    _audit(
        AuditEventKind.USER_UPDATED,
        actor=session.user.username,
        summary=f"Passwort von '{session.user.username}' geaendert (Self-Service).",
    )


@router.post("/{user_id}/password", status_code=status.HTTP_204_NO_CONTENT)
def admin_reset_password(
    user_id: int,
    payload: AdminPasswordResetRequest,
    server: ServerState = Depends(get_server_state),
    admin: Session = Depends(require_admin),
) -> None:
    """Admin setzt das Passwort eines anderen Users zurueck."""
    store = _user_store(server)
    target = store.get_user(user_id)
    if target is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User-ID {user_id} nicht gefunden.",
        )
    try:
        store.change_password(user_id, payload.new_password)
    except UserStoreError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    assert admin.user is not None
    _audit(
        AuditEventKind.USER_UPDATED,
        actor=admin.user.username,
        summary=f"Passwort von '{target.username}' durch Admin zurueckgesetzt.",
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _role_or_400(role: str) -> Role:
    # Pydantic-Pattern schuetzt schon — Cast zur Typkonsistenz.
    if role not in ("viewer", "operator", "admin"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Ungueltige Rolle: {role}",
        )
    return role  # type: ignore[return-value]


def _role_or_none(role: str | None) -> Role | None:
    if role is None:
        return None
    return _role_or_400(role)
