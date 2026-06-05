"""Inventory- und Action-ACL fuer den Multi-User-Mode (v3.0 Iter 4).

Zwei Achsen:

* **allowed_tags** (pro User in der User-DB): wenn nicht leer, sieht der
  User nur Geraete, deren Tags mindestens einen seiner Tags treffen
  (OR-Semantik). Leer = alle Geraete (Default fuer Admins / "no ACL").
* **Rolle** (viewer / operator / admin): bestimmt was der User darf:
    - viewer:  GET-Routen (Inventar lesen, Audit lesen, Plan-History)
    - operator: viewer + Plan/Apply + Inventar-Mutationen
    - admin:   alles (inkl. User-Mgmt — siehe ``require_admin``)

Im Single-User-Mode (session.user is None) sind beide Achsen inaktiv —
der Master-PW-Besitzer darf alles. Diese Konvention macht das ACL-
Modul gut testbar und vermeidet implizite Edge-Cases.

Die Funktionen werfen ``HTTPException`` mit passendem Status:
* 403 bei ACL-Verstoss (User hat das Geraet, darf es aber nicht aendern)
* 404 wenn ein Geraet-ID gar nicht im Tresor steht (gleiche Reaktion wie
  bei fehlendem Geraet — der ACL-Leak haette sonst die Existenz von
  Geraeten ausserhalb der Whitelist verraten)
"""

from __future__ import annotations

from collections.abc import Iterable

from fastapi import HTTPException, status

from opn_cockpit.security.session import Session
from opn_cockpit.vault.model import VaultDevice

# Welche Rollen welche Schreib-Endpunkte nutzen duerfen.
WRITE_ROLES: frozenset[str] = frozenset({"operator", "admin"})
PLAN_ROLES: frozenset[str] = frozenset({"operator", "admin"})


# ---------------------------------------------------------------------------
# Tag-Filter
# ---------------------------------------------------------------------------


def device_visible_to(device: VaultDevice, session: Session) -> bool:
    """True wenn das Geraet dem User per allowed_tags sichtbar ist.

    Single-Mode (``session.user is None``): immer True.
    Admin: True (Admins sehen alles, unabhaengig von allowed_tags).
    Sonst: True wenn die allowed_tags des Users leer sind ODER mindestens
    einer der allowed_tags in den Tags des Geraets vorkommt.
    """
    user = session.user
    if user is None:
        return True
    if user.role == "admin":
        return True
    if not user.allowed_tags:
        return True
    device_tags = set(device.tags)
    return any(t in device_tags for t in user.allowed_tags)


def filter_devices_for(
    devices: Iterable[VaultDevice], session: Session,
) -> list[VaultDevice]:
    """Filtert eine Geraete-Liste gemaess allowed_tags des Users."""
    return [d for d in devices if device_visible_to(d, session)]


def require_device_access(device: VaultDevice, session: Session) -> None:
    """Wirft 404, wenn der User dieses Geraet nicht sehen darf.

    Bewusst 404 statt 403 — sonst koennte ein User per probieren rausfinden,
    welche Geraete im Tresor existieren, die er nicht sehen darf.
    """
    if not device_visible_to(device, session):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Geraet mit ID '{device.id}' nicht im Tresor.",
        )


def require_device_ids_accessible(
    device_ids: Iterable[str],
    all_devices: Iterable[VaultDevice],
    session: Session,
) -> None:
    """Wirft 404, wenn auch nur eine der IDs ausserhalb des User-Scopes liegt.

    Praxis: Bei Plan/Apply mit Multi-Device-Targets. Eine einzige
    "verbotene" Geraete-ID im target_device_ids schiesst den ganzen
    Request ab (statt nur die verbotenen rauszufiltern) — damit
    versehentlich global gemeinte Plaene nicht heimlich kleiner werden.
    """
    by_id = {d.id: d for d in all_devices}
    for device_id in device_ids:
        device = by_id.get(device_id)
        if device is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Geraet mit ID '{device_id}' nicht im Tresor.",
            )
        if not device_visible_to(device, session):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Geraet mit ID '{device_id}' nicht im Tresor.",
            )


# ---------------------------------------------------------------------------
# Role-Gating
# ---------------------------------------------------------------------------


def require_write_role(session: Session) -> None:
    """Erlaubt nur operator + admin. viewer wird 403 abgewiesen.

    Im Single-Mode (kein User) durchgewunken — alle Aktionen sind erlaubt.
    """
    user = session.user
    if user is None:
        return
    if user.role not in WRITE_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Rolle '{user.role}' darf diese Aktion nicht ausfuehren — "
                "operator oder admin erforderlich."
            ),
        )


def require_plan_role(session: Session) -> None:
    """Erlaubt Plan/Apply nur fuer operator + admin."""
    user = session.user
    if user is None:
        return
    if user.role not in PLAN_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Rolle '{user.role}' darf keine Plaene erzeugen oder ausrollen — "
                "operator oder admin erforderlich."
            ),
        )


def require_admin_role(session: Session) -> None:
    """Strikter als ``require_plan_role``: nur Admin.

    Im Single-User-Mode (``session.user is None``) durchgewunken — der
    eingeloggte User ist implizit admin. Im Multi-User-Mode greift die
    Rollen-Pruefung: alles ausser ``admin`` wird mit 403 abgewiesen.

    Verwendet fuer Trust-Anker-Aenderungen (Custom-CAs, Cockpit-eigenes
    HTTPS) - das sind security-impacting Setting, die nicht jeder
    operator setzen darf.
    """
    user = session.user
    if user is None:
        return
    if user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Rolle '{user.role}' darf Trust-Anker nicht aendern — "
                "admin erforderlich."
            ),
        )


__all__ = [
    "PLAN_ROLES",
    "WRITE_ROLES",
    "require_admin_role",
    "device_visible_to",
    "filter_devices_for",
    "require_device_access",
    "require_device_ids_accessible",
    "require_plan_role",
    "require_write_role",
]
