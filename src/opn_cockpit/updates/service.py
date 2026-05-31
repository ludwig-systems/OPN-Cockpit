"""Orchestriert Cache + GitHub-Fetch und liefert ``UpdateCheckResult``.

Verwendung im API-Endpunkt::

    checker = default_checker()
    result = checker.check(settings)

Der Checker ist thread-safe — `check()` ruft selbst keinen langen Lock,
nur der Cache-Save ist atomar via tempfile+rename.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import httpx

from opn_cockpit import __github_url__, __version__
from opn_cockpit.config import AppSettings
from opn_cockpit.updates.cache import UpdateCache, default_update_cache_path
from opn_cockpit.updates.github import (
    GitHubReleaseError,
    fetch_latest_release,
    parse_repo_from_url,
)
from opn_cockpit.updates.model import UpdateCheckResult, UpdateStatus
from opn_cockpit.updates.version import compare_versions


@dataclass(frozen=True, slots=True)
class UpdateChecker:
    """Stateless Orchestrator — alle Argumente kommen pro Aufruf rein."""

    repo_url: str
    cache_path: Path | None = None

    def check(
        self,
        settings: AppSettings,
        *,
        client: httpx.Client | None = None,
        force: bool = False,
    ) -> UpdateCheckResult:
        """Liefert den aktuellen Update-Status.

        * **disabled** wenn ``settings.update_check_enabled=False``.
        * **cached** wenn der Cache innerhalb des Intervalls liegt
          (kein GitHub-Aufruf).
        * sonst frischer Fetch — Erfolg landet im Cache.
        """
        if not settings.update_check_enabled and not force:
            return self._disabled_result()

        repo = parse_repo_from_url(self.repo_url)
        if repo is None:
            return self._error_result(last_checked_iso=None)

        cache_path = self.cache_path or default_update_cache_path()
        cache = UpdateCache.load(cache_path)

        if not force and cache.is_fresh(settings.update_check_interval_hours):
            return self._result_from_cache(cache)

        return self._refresh_via_github(repo, cache, cache_path, client)

    def _refresh_via_github(
        self,
        repo: tuple[str, str],
        cache: UpdateCache,
        cache_path: Path,
        client: httpx.Client | None,
    ) -> UpdateCheckResult:
        owner, repo_name = repo
        try:
            release = fetch_latest_release(
                owner, repo_name,
                etag=cache.etag,
                client=client,
            )
        except GitHubReleaseError:
            if cache.latest_version:
                return self._result_from_cache(cache)
            return self._error_result(last_checked_iso=cache.last_checked_iso)

        if release is None:
            # 304 Not Modified — Cache ist weiterhin gueltig.
            cache.record_check_only()
        else:
            cache.record_success(
                latest_version=release.tag_name,
                html_url=release.html_url or None,
                etag=release.etag,
            )
        cache.save(cache_path)
        return self._result_from_cache(cache)

    @staticmethod
    def _disabled_result() -> UpdateCheckResult:
        return UpdateCheckResult(
            status="disabled",
            current_version=__version__,
            latest_version=None,
            html_url=None,
            last_checked_iso=None,
            source="disabled",
        )

    @staticmethod
    def _error_result(*, last_checked_iso: str | None) -> UpdateCheckResult:
        return UpdateCheckResult(
            status="unknown",
            current_version=__version__,
            latest_version=None,
            html_url=None,
            last_checked_iso=last_checked_iso,
            source="error",
        )

    def _result_from_cache(self, cache: UpdateCache) -> UpdateCheckResult:
        latest = cache.latest_version
        if not latest:
            return UpdateCheckResult(
                status="unknown",
                current_version=__version__,
                latest_version=None,
                html_url=cache.html_url,
                last_checked_iso=cache.last_checked_iso,
                source="cache",
            )
        cmp_result = compare_versions(__version__, latest)
        status: UpdateStatus = "available" if cmp_result < 0 else "up-to-date"
        return UpdateCheckResult(
            status=status,
            current_version=__version__,
            latest_version=latest,
            html_url=cache.html_url,
            last_checked_iso=cache.last_checked_iso,
            source="cache",
        )


def default_checker() -> UpdateChecker:
    """Liefert einen Checker mit dem produktiven Repo-URL."""
    return UpdateChecker(repo_url=__github_url__)


__all__ = ["UpdateChecker", "default_checker"]
