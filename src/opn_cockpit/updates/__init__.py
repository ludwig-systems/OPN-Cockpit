"""Update-Check via GitHub-Releases-API (v6-Pass 3).

Frontend zeigt einen dezenten Banner sobald eine neuere Version
veroeffentlicht wurde. Der Check ist:

* **anonym** — GitHub-Releases-API erlaubt ~60 Requests/Stunde/IP ohne
  Token, das reicht fuer 1x Check pro Tag pro Server lange.
* **gecached** — `update_check.json` im AppData-Dir merkt sich das
  Ergebnis fuer ``update_check_interval_hours`` Stunden. ETag-basierte
  Conditional-Requests reduzieren den Traffic auf 304-Antworten.
* **fehler-tolerant** — Netzwerkprobleme/Rate-Limit/404 setzen den
  Status auf "unknown" und lassen den UI-Banner einfach weg.
* **opt-out** — ``OPNCOCKPIT_UPDATE_CHECK_ENABLED=0`` (oder
  ``AppSettings.update_check_enabled=False``) deaktiviert den Check
  komplett — z. B. fuer Air-gapped-Installationen.
"""

from opn_cockpit.updates.cache import UpdateCache, default_update_cache_path
from opn_cockpit.updates.github import (
    GitHubReleaseError,
    fetch_latest_release,
    parse_repo_from_url,
)
from opn_cockpit.updates.model import UpdateCheckResult, UpdateStatus
from opn_cockpit.updates.service import UpdateChecker, default_checker
from opn_cockpit.updates.version import compare_versions, parse_version

__all__ = [
    "GitHubReleaseError",
    "UpdateCache",
    "UpdateCheckResult",
    "UpdateChecker",
    "UpdateStatus",
    "compare_versions",
    "default_checker",
    "default_update_cache_path",
    "fetch_latest_release",
    "parse_repo_from_url",
    "parse_version",
]
