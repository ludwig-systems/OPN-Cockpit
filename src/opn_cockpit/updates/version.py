"""Versions-Vergleich (semver-Lite).

Bewusst ohne externe Dependency — die GitHub-Tags des Projekts folgen
``X.Y.Z`` bzw. ``vX.Y.Z``. Pre-Release-Suffixe (``.dev0``, ``rc1``,
``-beta.2``) werden tolerant geparst, gelten aber stets als
**aelter** als die gleiche Base-Version ohne Suffix.

Beispiele::

    parse_version("0.6.0")    -> (0, 6, 0, False)
    parse_version("v0.6.0")   -> (0, 6, 0, False)
    parse_version("0.7.0rc1") -> (0, 7, 0, True)
    parse_version("0.7.0.dev0") -> (0, 7, 0, True)
    compare_versions("0.6.0", "0.7.0") -> -1
"""

from __future__ import annotations

import re

# (major, minor, patch, is_prerelease)
ParsedVersion = tuple[int, int, int, bool]

_VERSION_RE = re.compile(
    r"""
    ^v?\s*
    (?P<major>\d+)
    \.
    (?P<minor>\d+)
    \.
    (?P<patch>\d+)
    (?P<rest>.*)$
    """,
    re.VERBOSE | re.IGNORECASE,
)

_PRERELEASE_HINT_RE = re.compile(
    r"(rc|alpha|beta|dev|pre|a|b)\d*",
    re.IGNORECASE,
)


def parse_version(raw: str) -> ParsedVersion | None:
    """Parst ``raw`` in ein Tuple oder gibt ``None`` zurueck."""
    if not raw:
        return None
    match = _VERSION_RE.match(raw.strip())
    if not match:
        return None
    major = int(match.group("major"))
    minor = int(match.group("minor"))
    patch = int(match.group("patch"))
    rest = match.group("rest") or ""
    is_prerelease = bool(rest.strip()) and bool(_PRERELEASE_HINT_RE.search(rest))
    return (major, minor, patch, is_prerelease)


def compare_versions(left: str, right: str) -> int:
    """Liefert ``-1`` wenn ``left < right``, ``0`` wenn gleich, ``+1`` wenn groesser.

    Wenn eine Seite nicht parsebar ist, wird ``0`` zurueckgegeben (=
    kein Update sichtbar). Das verhindert, dass kaputte Tags den User
    mit einem falschen Banner stoeren.
    """
    pleft = parse_version(left)
    pright = parse_version(right)
    if pleft is None or pright is None or pleft == pright:
        return 0
    base_l, base_r = pleft[:3], pright[:3]
    if base_l != base_r:
        return -1 if base_l < base_r else 1
    # Gleiche Base — Non-Prerelease (is_prerelease=False) gewinnt.
    if pleft[3] == pright[3]:
        return 0
    return -1 if pleft[3] else 1


__all__ = ["ParsedVersion", "compare_versions", "parse_version"]
