"""Config-Drift-Erkennung: vergleicht OPNsense-Live-Config gegen letztes Backup.

OPNsense's Config-XML enthaelt einen ``<revision>``-Block mit Timestamp +
Beschreibung der letzten Aenderung. Dieser Block aendert sich bei JEDEM
Save (auch ohne sachliche Aenderung), waere also fuer einen reinen
Vergleich Stoer-Signal. Wir normalisieren das XML defensiv:

* Strippen `<revision>` (Timestamps + Free-Text-Beschreibung).
* Strippen `<lastchange>` (alternative Schreibweise in alten Releases).
* Whitespace-Normalisierung zwischen Tags damit Indentation-Aenderungen
  durch OPNsense-Updates kein Drift signalisieren.

Der vergleichbare Hash ist die SHA256-Hex des normalisierten Texts.
Reicht fuer Drift-Erkennung; nicht fuer kryptographische Integritaet
(dafuer ``BackupRecord.sha256`` des Original-Bytes).

Defensiv: bei XML-Parsing-Fehler nicht crashen, sondern den
Original-Hash als Fallback nutzen (kann zu False-Positive-Drift fuehren,
ist aber besser als der UI-Crash).
"""

from __future__ import annotations

import hashlib
import re

# Top-Level-Elemente die bei jedem Save flackern und kein Drift-Signal sind.
# Wir matchen den OEFFNENDEN Tag mit allen Attributen + den naechsten
# schliessenden Tag (multiline). XML ist genug deterministisch dass das
# robust funktioniert ohne vollen Parser.
_VOLATILE_TAGS = ("revision", "lastchange", "lastrevisiondate")

_RE_BLANK_BETWEEN_TAGS = re.compile(r">\s+<", flags=re.DOTALL)
_RE_LEADING_TRAILING_WS = re.compile(r"^\s+|\s+$", flags=re.MULTILINE)


def normalize_config_xml(content: bytes) -> str:
    """Strippt volatile Elemente + Whitespace fuer den Drift-Vergleich."""
    try:
        text = content.decode("utf-8", errors="replace")
    except (UnicodeDecodeError, AttributeError):
        text = ""
    for tag in _VOLATILE_TAGS:
        pattern = re.compile(
            rf"<{tag}(\s[^>]*)?>.*?</{tag}>", flags=re.DOTALL | re.IGNORECASE,
        )
        text = pattern.sub("", text)
        # Self-closing-Varianten ("<lastchange/>") gleich mit
        pattern_self = re.compile(
            rf"<{tag}(\s[^>]*)?/>", flags=re.IGNORECASE,
        )
        text = pattern_self.sub("", text)
    text = _RE_BLANK_BETWEEN_TAGS.sub("><", text)
    text = _RE_LEADING_TRAILING_WS.sub("", text)
    return text


def compute_drift_hash(content: bytes) -> str:
    """SHA256-Hex des normalisierten Configs - vergleichbar zwischen Sessions."""
    normalized = normalize_config_xml(content)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


__all__ = [
    "compute_drift_hash",
    "normalize_config_xml",
]
