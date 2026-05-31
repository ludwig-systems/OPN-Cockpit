"""Tests fuer den GitHub-API-Client."""

from __future__ import annotations

import httpx
import pytest

from opn_cockpit.updates.github import (
    GitHubReleaseError,
    fetch_latest_release,
    parse_repo_from_url,
)


class TestParseRepoFromUrl:
    @pytest.mark.parametrize("url,expected", [
        ("https://github.com/owner/repo", ("owner", "repo")),
        ("https://github.com/owner/repo/", ("owner", "repo")),
        ("https://github.com/owner/repo.git", ("owner", "repo")),
        ("https://www.github.com/owner/repo", ("owner", "repo")),
        ("https://github.com/owner/repo/issues/1", ("owner", "repo")),
    ])
    def test_extracts_owner_and_repo(
        self, url: str, expected: tuple[str, str],
    ) -> None:
        assert parse_repo_from_url(url) == expected

    @pytest.mark.parametrize("url", [
        "",
        "https://gitlab.com/owner/repo",
        "https://github.com/",
        "https://github.com/owner",
        "not-a-url",
    ])
    def test_returns_none_for_invalid(self, url: str) -> None:
        assert parse_repo_from_url(url) is None


def _client(handler) -> httpx.Client:
    transport = httpx.MockTransport(handler)
    return httpx.Client(transport=transport)


class TestFetchLatestRelease:
    def test_returns_release_on_200(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert "/repos/foo/bar/releases/latest" in request.url.path
            return httpx.Response(
                200,
                json={
                    "tag_name": "v0.7.0",
                    "name": "Release 0.7.0",
                    "html_url": "https://github.com/foo/bar/releases/0.7.0",
                    "published_at": "2026-06-15T10:00:00Z",
                    "draft": False,
                    "prerelease": False,
                },
                headers={"ETag": 'W/"abc"'},
            )
        with _client(handler) as client:
            release = fetch_latest_release("foo", "bar", client=client)
        assert release is not None
        assert release.tag_name == "v0.7.0"
        assert release.html_url == "https://github.com/foo/bar/releases/0.7.0"
        assert release.etag == 'W/"abc"'

    def test_returns_none_on_304(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.headers.get("If-None-Match") == 'W/"abc"'
            return httpx.Response(304)
        with _client(handler) as client:
            assert fetch_latest_release(
                "foo", "bar", etag='W/"abc"', client=client,
            ) is None

    def test_raises_on_404(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"message": "Not Found"})
        with _client(handler) as client, pytest.raises(GitHubReleaseError):
            fetch_latest_release("foo", "bar", client=client)

    def test_raises_on_rate_limit(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, text="API rate limit exceeded")
        with _client(handler) as client, pytest.raises(GitHubReleaseError, match="Rate-Limit"):
            fetch_latest_release("foo", "bar", client=client)

    def test_raises_on_draft(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "tag_name": "v0.7.0", "draft": True, "prerelease": False,
            })
        with _client(handler) as client, pytest.raises(GitHubReleaseError, match="Draft"):
            fetch_latest_release("foo", "bar", client=client)

    def test_raises_on_prerelease(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "tag_name": "v0.7.0rc1", "draft": False, "prerelease": True,
            })
        with _client(handler) as client, pytest.raises(GitHubReleaseError, match="Prerelease"):
            fetch_latest_release("foo", "bar", client=client)

    def test_raises_on_missing_tag(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "draft": False, "prerelease": False,
            })
        with _client(handler) as client, pytest.raises(GitHubReleaseError, match="tag_name"):
            fetch_latest_release("foo", "bar", client=client)

    def test_raises_on_timeout(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("simulated")
        with _client(handler) as client, pytest.raises(GitHubReleaseError, match="Timeout"):
            fetch_latest_release("foo", "bar", client=client)
