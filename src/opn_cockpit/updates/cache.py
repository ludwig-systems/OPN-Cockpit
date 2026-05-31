"""Persistenter Cache fuer den Update-Check.

Datei liegt im AppData-Dir (``update_check.json``). Schema bewusst flach,
damit zukuenftige Felder ohne Migration angehaengt werden koennen.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from opn_cockpit.config import get_app_data_dir

CACHE_FILENAME = "update_check.json"


def default_update_cache_path() -> Path:
    return get_app_data_dir() / CACHE_FILENAME


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _now_iso() -> str:
    return _now_utc().strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(slots=True)
class UpdateCache:
    """Cache-Eintrag mit Versionsstand + ETag.

    ``last_checked_iso`` ist die Zeit des letzten ERFOLGREICHEN Checks
    (auch 304 zaehlt als Erfolg). Fehlversuche updaten den Cache nicht,
    damit der naechste Versuch nicht wegen Backoff geblockt wird.
    """

    last_checked_iso: str | None = None
    latest_version: str | None = None
    html_url: str | None = None
    etag: str | None = None

    @classmethod
    def load(cls, path: Path | None = None) -> UpdateCache:
        target = path or default_update_cache_path()
        if not target.exists():
            return cls()
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls()
        if not isinstance(raw, dict):
            return cls()
        return cls._from_dict(raw)

    def save(self, path: Path | None = None) -> None:
        target = path or default_update_cache_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(
            json.dumps(asdict(self), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, target)

    def is_fresh(self, interval_hours: int) -> bool:
        """``True``, wenn ein neuer GitHub-Call erst nach der Frist sinnvoll ist."""
        if not self.last_checked_iso or not self.latest_version:
            return False
        try:
            checked = datetime.strptime(
                self.last_checked_iso, "%Y-%m-%dT%H:%M:%SZ",
            ).replace(tzinfo=UTC)
        except ValueError:
            return False
        return _now_utc() - checked < timedelta(hours=interval_hours)

    def record_success(
        self,
        latest_version: str,
        html_url: str | None,
        etag: str | None,
    ) -> None:
        self.last_checked_iso = _now_iso()
        self.latest_version = latest_version
        if html_url:
            self.html_url = html_url
        if etag:
            self.etag = etag

    def record_check_only(self) -> None:
        """304 oder gleichbleibendes Ergebnis: nur Zeitstempel auffrischen."""
        self.last_checked_iso = _now_iso()

    @classmethod
    def _from_dict(cls, raw: dict[str, Any]) -> UpdateCache:
        def _str_or_none(value: Any) -> str | None:
            return value if isinstance(value, str) and value else None
        return cls(
            last_checked_iso=_str_or_none(raw.get("last_checked_iso")),
            latest_version=_str_or_none(raw.get("latest_version")),
            html_url=_str_or_none(raw.get("html_url")),
            etag=_str_or_none(raw.get("etag")),
        )


__all__ = ["CACHE_FILENAME", "UpdateCache", "default_update_cache_path"]
