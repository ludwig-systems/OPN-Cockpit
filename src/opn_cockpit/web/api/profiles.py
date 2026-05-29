"""Profile-Routen: Aktions-Templates persistent speichern und abrufen.

Wraps ``profiles.store.ProfileStore``. Speicherung im selben Pfad wie CLI
(``%APPDATA%/OPN-Cockpit/profiles.json``) — Web und CLI teilen sich die
Vorlagen.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from opn_cockpit.profiles.store import (
    Profile,
    ProfileStore,
    ProfileStoreError,
    default_profiles_path,
)
from opn_cockpit.security.session import Session
from opn_cockpit.web.api.schemas import (
    ProfileCreateRequest,
    ProfileListResponse,
    ProfileResponse,
)
from opn_cockpit.web.auth.dependencies import require_session

router = APIRouter(prefix="/api/profiles", tags=["profiles"])


def _store() -> ProfileStore:
    return ProfileStore(path=default_profiles_path())


def _to_response(profile: Profile) -> ProfileResponse:
    return ProfileResponse(
        id=profile.id,
        name=profile.name,
        action=profile.action,
        subsystem=profile.subsystem,
        default_selector=profile.default_selector,
        spec=profile.spec,
    )


@router.get("", response_model=ProfileListResponse)
def list_profiles(session: Session = Depends(require_session)) -> ProfileListResponse:
    """Liefert alle gespeicherten Aktions-Templates."""
    session.touch()
    return ProfileListResponse(
        profiles=[_to_response(p) for p in _store().list_profiles()],
    )


@router.post(
    "",
    response_model=ProfileResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_profile(
    payload: ProfileCreateRequest,
    session: Session = Depends(require_session),
) -> ProfileResponse:
    """Legt ein neues Template an."""
    session.touch()
    try:
        profile = _store().save_new(
            name=payload.name,
            action=payload.action,
            subsystem=payload.subsystem,
            default_selector=payload.default_selector,
            spec=dict(payload.spec),
        )
    except ProfileStoreError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    return _to_response(profile)


@router.get("/{profile_id}", response_model=ProfileResponse)
def get_profile(
    profile_id: str,
    session: Session = Depends(require_session),
) -> ProfileResponse:
    """Liefert ein einzelnes Profil."""
    session.touch()
    try:
        return _to_response(_store().get(profile_id))
    except ProfileStoreError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc


@router.delete("/{profile_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_profile(
    profile_id: str,
    session: Session = Depends(require_session),
) -> None:
    """Entfernt ein Profil aus dem Store."""
    session.touch()
    removed = _store().delete(profile_id)
    if not removed:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Profil '{profile_id}' nicht gefunden.",
        )


__all__ = ["router"]
