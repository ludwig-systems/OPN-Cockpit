"""Profile-Backend-Interface + Factory.

Analog zu :mod:`opn_cockpit.audit.backend` und :mod:`opn_cockpit.orchestration.backend`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from opn_cockpit.profiles.store import ProfileStore, default_profiles_path

if TYPE_CHECKING:
    from opn_cockpit.profiles.store import Profile


@runtime_checkable
class ProfileStoreBackend(Protocol):
    """Pflichtschnittstelle aller Profile-Store-Backends."""

    def list_profiles(self) -> list[Profile]:
        ...

    def get(self, profile_id: str) -> Profile:
        ...

    def find_by_name(self, name: str) -> Profile | None:
        ...

    def save_new(
        self,
        *,
        name: str,
        action: str,
        subsystem: str,
        default_selector: str,
        spec: dict[str, Any],
    ) -> Profile:
        ...

    def delete(self, profile_id: str) -> bool:
        ...


def get_profile_store_backend() -> ProfileStoreBackend:
    """Liefert das aktuell konfigurierte Profile-Store-Backend."""
    return ProfileStore(path=default_profiles_path())


__all__ = ["ProfileStoreBackend", "get_profile_store_backend"]
