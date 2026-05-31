"""GitHub-Releases-API-Client (anonym).

Verwendet ``httpx`` synchron mit kurzem Timeout. ETag-Support fuer
Conditional-Requests: wir senden ``If-None-Match`` und akzeptieren ``304
Not Modified`` als gueltige Antwort (Cache-Treffer ohne Body).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from http import HTTPStatus
from urllib.parse import urlparse

import httpx

GITHUB_API_BASE = "https://api.github.com"
DEFAULT_TIMEOUT_S = 5.0
USER_AGENT = "opn-cockpit-update-check"

_PATH_PARTS_MIN = 2


class GitHubReleaseError(Exception):
    """Konnte das letzte Release nicht ermitteln (404, 5xx, Timeout, ...)."""


@dataclass(frozen=True, slots=True)
class LatestRelease:
    """Auszug aus dem GitHub-Release-Objekt."""

    tag_name: str
    name: str
    html_url: str
    published_at_iso: str
    etag: str | None


def parse_repo_from_url(url: str) -> tuple[str, str] | None:
    """Extrahiert ``(owner, repo)`` aus einer GitHub-URL.

    Akzeptiert ``https://github.com/owner/repo`` (mit/ohne Trailing
    Slash, mit/ohne ``.git``). Bei Nicht-GitHub-URLs ``None``.
    """
    if not url:
        return None
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    if host not in {"github.com", "www.github.com"}:
        return None
    parts = parsed.path.strip("/").split("/")
    if len(parts) < _PATH_PARTS_MIN or not parts[0] or not parts[1]:
        return None
    owner = parts[0]
    repo = re.sub(r"\.git$", "", parts[1])
    if not repo:
        return None
    return owner, repo


def _parse_release_payload(payload: object) -> LatestRelease:
    """Validiert die JSON-Antwort und baut ein LatestRelease."""
    if not isinstance(payload, dict):
        raise GitHubReleaseError("Antwort hatte unerwartetes Format.")
    if payload.get("draft") or payload.get("prerelease"):
        raise GitHubReleaseError(
            "Aktuelles 'latest' ist Draft/Prerelease — werten wir nicht.",
        )
    tag = payload.get("tag_name")
    if not isinstance(tag, str) or not tag:
        raise GitHubReleaseError("Antwort enthielt kein tag_name.")
    return LatestRelease(
        tag_name=tag,
        name=str(payload.get("name") or tag),
        html_url=str(payload.get("html_url") or ""),
        published_at_iso=str(payload.get("published_at") or ""),
        etag=None,  # wird vom Aufrufer aus Response-Header gesetzt
    )


def _check_status(
    response: httpx.Response, owner: str, repo: str,
) -> None:
    """Wirft GitHubReleaseError fuer alles ausser 200 (und 304 oben behandelt)."""
    status = response.status_code
    if status == HTTPStatus.NOT_FOUND:
        raise GitHubReleaseError(
            f"Repository {owner}/{repo} hat noch kein Release "
            "(oder ist privat).",
        )
    if status == HTTPStatus.FORBIDDEN and "rate limit" in response.text.lower():
        raise GitHubReleaseError("GitHub-Rate-Limit erreicht.")
    if status != HTTPStatus.OK:
        raise GitHubReleaseError(f"GitHub-API antwortete mit HTTP {status}")


def fetch_latest_release(
    owner: str,
    repo: str,
    *,
    etag: str | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    client: httpx.Client | None = None,
) -> LatestRelease | None:
    """Holt das aktuellste Non-Prerelease vom GitHub-API.

    Returns:
        ``LatestRelease`` bei 200, ``None`` bei 304 (Cache noch gueltig).

    Raises:
        GitHubReleaseError: Bei jedem anderen Fehler (404, 5xx, Timeout,
            kaputtes JSON, draft/prerelease im Wege).
    """
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/releases/latest"
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if etag:
        headers["If-None-Match"] = etag

    owns_client = client is None
    http_client = client or httpx.Client(timeout=timeout_s)
    try:
        try:
            response = http_client.get(url, headers=headers)
        except httpx.TimeoutException as exc:
            raise GitHubReleaseError(f"Timeout beim Update-Check: {exc}") from exc
        except httpx.RequestError as exc:
            raise GitHubReleaseError(f"Netzwerk-Fehler: {exc}") from exc

        if response.status_code == HTTPStatus.NOT_MODIFIED:
            return None
        _check_status(response, owner, repo)

        try:
            payload = response.json()
        except ValueError as exc:
            raise GitHubReleaseError(f"Antwort war kein gueltiges JSON: {exc}") from exc

        release = _parse_release_payload(payload)
        return LatestRelease(
            tag_name=release.tag_name,
            name=release.name,
            html_url=release.html_url,
            published_at_iso=release.published_at_iso,
            etag=response.headers.get("ETag"),
        )
    finally:
        if owns_client:
            http_client.close()


__all__ = [
    "GITHUB_API_BASE",
    "GitHubReleaseError",
    "LatestRelease",
    "fetch_latest_release",
    "parse_repo_from_url",
]
