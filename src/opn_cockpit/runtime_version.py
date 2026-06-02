"""Runtime-Versions-Ermittlung mit Git-Tag-Fallback.

Hintergrund: ``__init__.py.__version__`` zeigt zwischen Releases die Dev-
Version (z.B. ``0.6.3.dev0``). Bei Tag-getriggertem Workflow-Build wird
``__version__`` zwar gepatcht, aber bei Linux/Container-Deployments via
``git reset --hard origin/main`` zieht der Container die unveraenderte
Dev-Version aus main.

Damit About-Modal + Update-Check die *tatsaechlich* installierte Release-
Version zeigen, fragen wir hier zusaetzlich ``git describe --tags`` ab
und nehmen das Tag-Ergebnis bevorzugt. Fallback: ``__version__``.

Cached pro Prozess - der Wert aendert sich nicht ohne Service-Restart.
"""

from __future__ import annotations

import subprocess
from functools import lru_cache
from pathlib import Path

from opn_cockpit import __version__

_TIMEOUT_S = 2.0


def _package_repo_dir() -> Path | None:
    """Findet das Git-Repo, in dem das opn-cockpit-Package liegt.

    ``src/opn_cockpit/__init__.py`` => Repo-Root ist 2 Ebenen hoch.
    Wenn da kein ``.git`` ist, ist die Installation kein git-Checkout
    (z.B. Windows-Installer mit Embedded-Python) - dann liefern wir
    ``None`` zurueck und der Caller faellt auf ``__version__`` zurueck.
    """
    here = Path(__file__).resolve()
    candidate = here.parents[2] if len(here.parents) >= 3 else None
    if candidate is None:
        return None
    if (candidate / ".git").exists():
        return candidate
    return None


def _git_describe_tag(repo: Path) -> str | None:
    """Liefert das aktuellste reachable Tag, oder ``None``."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "describe", "--tags", "--abbrev=0"],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_S,
            check=False,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if result.returncode != 0:
        return None
    tag = result.stdout.strip()
    return tag or None


@lru_cache(maxsize=1)
def get_runtime_version() -> str:
    """Liefert die "tatsaechliche" Version dieses Builds.

    Bevorzugt: aktuellstes Git-Tag (Format ``vX.Y.Z``). Faellt zurueck
    auf ``__version__`` aus ``__init__.py`` wenn:

    * Installation ist kein Git-Checkout (Windows-Installer)
    * Git nicht installiert / nicht im PATH
    * Repo hat noch keine Tags (Fresh-Install vor erstem Release)
    """
    repo = _package_repo_dir()
    if repo is not None:
        tag = _git_describe_tag(repo)
        if tag:
            return tag
    return __version__


def get_runtime_version_detail() -> dict[str, str]:
    """Liefert eine struktierte Version mit Source + Effective.

    Frontend kann beides anzeigen. Bei Windows-Installer sind beide
    gleich, bei Linux-Container kann Source = "0.6.3.dev0" sein
    waehrend Effective = "v0.6.4" ist (Container von main mit
    erreichbarem v0.6.4-Tag).
    """
    return {
        "source": __version__,
        "effective": get_runtime_version(),
    }


__all__ = ["get_runtime_version", "get_runtime_version_detail"]
