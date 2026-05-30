"""Profile-Backend-Interface + Factory.

Analog zu :mod:`opn_cockpit.audit.backend` und :mod:`opn_cockpit.orchestration.backend`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from opn_cockpit.audit.backend import _shared_db
from opn_cockpit.config import AppSettings
from opn_cockpit.profiles.sqlite_store import SqliteProfileStore
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
    """Liefert das aktuell konfigurierte Profile-Store-Backend.

    File-Default oder SQLite je nach ``AppSettings.storage_backend``.
    """
    if AppSettings.load().storage_backend == "sqlite":
        return SqliteProfileStore(db=_shared_db())
    return ProfileStore(path=default_profiles_path())


__all__ = ["ProfileStoreBackend", "get_profile_store_backend"]
