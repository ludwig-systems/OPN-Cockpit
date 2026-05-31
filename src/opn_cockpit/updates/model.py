"""Datentypen fuer den Update-Check."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

UpdateStatus = Literal[
    "available",   # Neue Version verfuegbar
    "up-to-date",  # Aktuelle Version ist die neueste
    "unknown",     # Konnte nicht ermittelt werden (Netzwerk, 404, ...)
    "disabled",    # Check ist per Settings/Env deaktiviert
]


@dataclass(frozen=True, slots=True)
class UpdateCheckResult:
    """Ergebnis eines Update-Check-Aufrufs.

    Wird vom API-Endpunkt direkt als JSON serialisiert.
    """

    status: UpdateStatus
    current_version: str
    latest_version: str | None
    html_url: str | None
    last_checked_iso: str | None
    source: Literal["github", "cache", "disabled", "error"]

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "current_version": self.current_version,
            "latest_version": self.latest_version,
            "html_url": self.html_url,
            "last_checked_iso": self.last_checked_iso,
            "source": self.source,
            "update_available": self.status == "available",
        }


__all__ = ["UpdateCheckResult", "UpdateStatus"]
