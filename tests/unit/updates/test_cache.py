"""Tests fuer UpdateCache (Persistenz + Freshness-Check)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from opn_cockpit.updates.cache import UpdateCache


class TestCachePersistence:
    def test_load_missing_returns_empty(self, tmp_path: Path) -> None:
        cache = UpdateCache.load(tmp_path / "missing.json")
        assert cache.latest_version is None
        assert cache.etag is None

    def test_load_invalid_returns_empty(self, tmp_path: Path) -> None:
        target = tmp_path / "broken.json"
        target.write_text("invalid", encoding="utf-8")
        cache = UpdateCache.load(target)
        assert cache.latest_version is None

    def test_save_roundtrip(self, tmp_path: Path) -> None:
        target = tmp_path / "cache.json"
        cache = UpdateCache(
            last_checked_iso="2026-06-01T00:00:00Z",
            latest_version="0.7.0",
            html_url="https://github.com/foo/bar/releases/0.7.0",
            etag='W/"xyz"',
        )
        cache.save(target)
        loaded = UpdateCache.load(target)
        assert loaded.latest_version == "0.7.0"
        assert loaded.etag == 'W/"xyz"'


class TestFreshness:
    def test_empty_cache_is_not_fresh(self) -> None:
        assert UpdateCache().is_fresh(24) is False

    def test_recent_check_is_fresh(self) -> None:
        recent = datetime.now(UTC) - timedelta(hours=1)
        cache = UpdateCache(
            last_checked_iso=recent.strftime("%Y-%m-%dT%H:%M:%SZ"),
            latest_version="0.7.0",
        )
        assert cache.is_fresh(24) is True

    def test_old_check_is_not_fresh(self) -> None:
        old = datetime.now(UTC) - timedelta(hours=48)
        cache = UpdateCache(
            last_checked_iso=old.strftime("%Y-%m-%dT%H:%M:%SZ"),
            latest_version="0.7.0",
        )
        assert cache.is_fresh(24) is False


class TestRecordHelpers:
    def test_record_success_sets_all_fields(self) -> None:
        cache = UpdateCache()
        cache.record_success(
            latest_version="0.7.0",
            html_url="https://example.invalid/0.7.0",
            etag='W/"abc"',
        )
        assert cache.latest_version == "0.7.0"
        assert cache.html_url == "https://example.invalid/0.7.0"
        assert cache.etag == 'W/"abc"'
        assert cache.last_checked_iso is not None

    def test_record_check_only_keeps_existing_version(self) -> None:
        cache = UpdateCache(latest_version="0.7.0", html_url="x")
        old_ts = "2026-01-01T00:00:00Z"
        cache.last_checked_iso = old_ts
        cache.record_check_only()
        assert cache.last_checked_iso != old_ts
        assert cache.latest_version == "0.7.0"

    @pytest.mark.parametrize("bad_ts", ["", "garbage", "2026-13-99"])
    def test_invalid_timestamp_treated_as_stale(self, bad_ts: str) -> None:
        cache = UpdateCache(
            last_checked_iso=bad_ts,
            latest_version="0.7.0",
        )
        assert cache.is_fresh(24) is False
