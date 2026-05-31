"""Tests fuer den UpdateChecker (Orchestrierung)."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from opn_cockpit import __version__
from opn_cockpit.config import AppSettings
from opn_cockpit.updates.cache import UpdateCache
from opn_cockpit.updates.service import UpdateChecker


@pytest.fixture()
def cache_path(tmp_path: Path) -> Path:
    return tmp_path / "update_check.json"


def _stub_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


class TestDisabled:
    def test_disabled_via_settings(self, cache_path: Path) -> None:
        checker = UpdateChecker(
            repo_url="https://github.com/foo/bar",
            cache_path=cache_path,
        )
        settings = AppSettings(update_check_enabled=False)
        result = checker.check(settings)
        assert result.status == "disabled"
        assert result.source == "disabled"


class TestRepoExtraction:
    def test_invalid_url_returns_unknown(self, cache_path: Path) -> None:
        checker = UpdateChecker(repo_url="not-a-url", cache_path=cache_path)
        result = checker.check(AppSettings())
        assert result.status == "unknown"


class TestFetchAndCache:
    def test_fetches_when_no_cache(self, cache_path: Path) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "tag_name": "v999.0.0",
                "html_url": "https://example.invalid/999",
                "published_at": "2026-06-15T00:00:00Z",
                "draft": False, "prerelease": False,
            })
        checker = UpdateChecker(
            repo_url="https://github.com/foo/bar",
            cache_path=cache_path,
        )
        with _stub_client(handler) as client:
            result = checker.check(AppSettings(), client=client)
        assert result.status == "available"
        assert result.latest_version == "v999.0.0"
        assert result.html_url == "https://example.invalid/999"
        assert cache_path.exists()

    def test_skips_fetch_when_cache_fresh(self, cache_path: Path) -> None:
        # Fresh cache mit hoeherer Version → kein Fetch noetig.
        fresh = UpdateCache()
        fresh.record_success("v999.0.0", "https://x.invalid", 'W/"abc"')
        fresh.save(cache_path)
        called = {"n": 0}

        def handler(_request: httpx.Request) -> httpx.Response:
            called["n"] += 1
            return httpx.Response(500)

        checker = UpdateChecker(
            repo_url="https://github.com/foo/bar",
            cache_path=cache_path,
        )
        with _stub_client(handler) as client:
            result = checker.check(AppSettings(), client=client)
        assert called["n"] == 0
        assert result.status == "available"
        assert result.source == "cache"

    def test_force_bypasses_cache(self, cache_path: Path) -> None:
        fresh = UpdateCache()
        fresh.record_success("v999.0.0", "https://x.invalid", None)
        fresh.save(cache_path)
        called = {"n": 0}

        def handler(_request: httpx.Request) -> httpx.Response:
            called["n"] += 1
            return httpx.Response(200, json={
                "tag_name": "v1000.0.0",
                "html_url": "https://example.invalid/1000",
                "draft": False, "prerelease": False,
            })

        checker = UpdateChecker(
            repo_url="https://github.com/foo/bar",
            cache_path=cache_path,
        )
        with _stub_client(handler) as client:
            result = checker.check(AppSettings(), client=client, force=True)
        assert called["n"] == 1
        assert result.latest_version == "v1000.0.0"

    def test_304_reuses_cache(self, cache_path: Path) -> None:
        old = UpdateCache()
        old.record_success("v999.0.0", "https://x.invalid", 'W/"abc"')
        old.save(cache_path)
        # Erzwinge stale cache durch leere is_fresh-Antwort:
        # last_checked_iso wird auf alt gesetzt
        stale = UpdateCache.load(cache_path)
        stale.last_checked_iso = "2026-01-01T00:00:00Z"
        stale.save(cache_path)

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.headers.get("If-None-Match") == 'W/"abc"'
            return httpx.Response(304)

        checker = UpdateChecker(
            repo_url="https://github.com/foo/bar",
            cache_path=cache_path,
        )
        with _stub_client(handler) as client:
            result = checker.check(AppSettings(), client=client)
        assert result.status == "available"
        assert result.latest_version == "v999.0.0"


class TestErrorHandling:
    def test_network_error_with_existing_cache_returns_cached(
        self, cache_path: Path,
    ) -> None:
        old = UpdateCache()
        old.last_checked_iso = "2026-01-01T00:00:00Z"
        old.latest_version = "v999.0.0"
        old.html_url = "https://x.invalid"
        old.save(cache_path)

        def handler(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("simulated")

        checker = UpdateChecker(
            repo_url="https://github.com/foo/bar",
            cache_path=cache_path,
        )
        with _stub_client(handler) as client:
            result = checker.check(AppSettings(), client=client)
        # Kein Crash, faellt auf den alten Cache zurueck.
        assert result.latest_version == "v999.0.0"

    def test_network_error_without_cache_returns_unknown(
        self, cache_path: Path,
    ) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("simulated")

        checker = UpdateChecker(
            repo_url="https://github.com/foo/bar",
            cache_path=cache_path,
        )
        with _stub_client(handler) as client:
            result = checker.check(AppSettings(), client=client)
        assert result.status == "unknown"
        assert result.source == "error"


class TestUpToDate:
    def test_same_version_returns_up_to_date(self, cache_path: Path) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "tag_name": __version__,
                "html_url": "https://example.invalid",
                "draft": False, "prerelease": False,
            })
        checker = UpdateChecker(
            repo_url="https://github.com/foo/bar",
            cache_path=cache_path,
        )
        with _stub_client(handler) as client:
            result = checker.check(AppSettings(), client=client)
        assert result.status == "up-to-date"
        assert result.to_dict()["update_available"] is False
