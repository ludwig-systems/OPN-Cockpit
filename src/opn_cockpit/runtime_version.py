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


def _run_git(repo: Path, *args: str) -> str | None:
    """Helfer: laeuft 'git -C repo args'. None bei Fehler/Timeout/leerer Output."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_S,
            check=False,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if result.returncode != 0:
        return None
    out = result.stdout.strip()
    return out or None


def _git_describe_tag(repo: Path) -> str | None:
    """Liefert das aktuellste reachable Release-Tag, oder ``None``.

    Zwei Stufen:
    1. ``git describe --tags --abbrev=0`` - semantisch korrekt ("auf
       welchem Tag basiert HEAD"), braucht aber History + Tag-Refs.
    2. ``git tag --list 'v*' --sort=-v:refname | head -1`` - Fallback,
       liefert hoechstes v-Tag aus dem Tag-Universum. Funktioniert
       auch bei shallow Clones, solange Tag-Refs vorhanden sind.

    Bei Shallow-Clones (--depth 1) ohne --tags gibt's beide nicht;
    dann liefern wir None und der Caller faellt auf __version__ zurueck.
    """
    tag = _run_git(repo, "describe", "--tags", "--abbrev=0")
    if tag:
        return tag
    tags = _run_git(repo, "tag", "--list", "v*", "--sort=-v:refname")
    if tags:
        # Erstes Tag der Liste = hoechste Version (--sort=-v:refname)
        return tags.split("\n")[0].strip() or None
    return None


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
