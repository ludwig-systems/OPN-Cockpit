"""Strukturierte Extraktion + paarweise Vergleichs-Matrix fuer Firewall-Konfigs.

Greift den per ``download_backup`` geholten Live-XML-Body und liefert
verglichbare Objekt-Listen pro Subsystem. Aktueller Stand:

* **Aliases** — Vollstaendig (Name, Typ, sortierter Inhalt, Description).

Routes / Firewall-Rules / Unbound-DNS folgen nach demselben Muster.

Vergleichs-Logik fuer N Geraete:

* Pro Alias-Name baut ``compare_aliases`` einen Matrix-Eintrag mit
  pro-Geraet-Status: present | absent | unreachable. Bei "present"
  steht zusaetzlich der content-Hash drin damit "selber Name, anderer
  Inhalt" sichtbar gemacht werden kann.
* "uniform" pro Row: True wenn alle erreichbaren Geraete denselben
  Eintrag haben. False = es gibt Drift oder Luecke.
"""

from __future__ import annotations

import hashlib
import xml.etree.ElementTree as ET
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AliasItem:
    """Vergleichbare Repraesentation eines einzelnen Aliases."""

    name: str
    type: str
    content: tuple[str, ...]   # sortierte Liste, damit Reihenfolge irrelevant
    description: str

    @property
    def content_fingerprint(self) -> str:
        """SHA256-Hex des sortierten Contents — kurz, fuer UI-Diff-Markierung."""
        joined = "\n".join(self.content)
        return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:12]


def _normalize_alias_content(raw: str | None) -> tuple[str, ...]:
    """OPNsense schreibt Alias-Content als Newline-/Space-getrennten String.

    Wir splitten auf alle Whitespace-Zeichen, filtern leere Eintraege,
    sortieren um Reihenfolgen-Unterschiede zwischen Boxen zu eliminieren
    (semantisch sind Aliase Mengen, nicht Listen).
    """
    if not raw:
        return ()
    parts = [p.strip() for p in raw.replace(",", "\n").split()]
    cleaned = [p for p in parts if p]
    return tuple(sorted(cleaned))


def _iter_alias_nodes(root: ET.Element) -> list[ET.Element]:
    """Findet alle <alias>-Knoten - egal ob neue (OPNsense/Firewall/Alias/aliases)
    oder alte (top-level <aliases>) XML-Struktur."""
    # Neue OPNsense-Struktur
    for path in (
        "./OPNsense/Firewall/Alias/aliases/alias",
        "./aliases/alias",
        "./opnsense/aliases/alias",
    ):
        found = root.findall(path)
        if found:
            return found
    return []


def extract_aliases(xml_bytes: bytes) -> list[AliasItem]:
    """Parsed Aliases aus einem OPNsense-Konfig-Backup.

    Defensiv: bei Parse-Fehler -> leere Liste. UI signalisiert das als
    "kein Vergleichsstand" fuer die Spalte.
    """
    try:
        # OPNsense-Backups sind unsere eigene Datenquelle - kein
        # untrusted-input. ParseError ist die einzige relevante
        # Fehlerklasse beim Schrott-Input.
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return []
    items: list[AliasItem] = []
    for node in _iter_alias_nodes(root):
        name = (node.findtext("name") or "").strip()
        if not name:
            continue
        items.append(AliasItem(
            name=name,
            type=(node.findtext("type") or "").strip(),
            content=_normalize_alias_content(node.findtext("content")),
            description=(node.findtext("description") or node.findtext("descr") or "").strip(),
        ))
    return items


# ---------------------------------------------------------------------------
# Vergleichs-Matrix
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AliasCell:
    """Status eines Aliases auf einem konkreten Geraet."""

    status: str  # "present" | "absent" | "unreachable"
    type: str = ""
    content_fingerprint: str = ""
    content_count: int = 0
    description: str = ""


@dataclass(frozen=True, slots=True)
class AliasRow:
    """Ein Alias-Name in der Vergleichs-Matrix mit Pro-Geraet-Status."""

    name: str
    cells: tuple[tuple[str, AliasCell], ...]
    """Geordnete (device_id, AliasCell)-Tupel — selbe Reihenfolge wie ``columns``."""

    uniform: bool
    """True wenn alle erreichbaren Geraete denselben content_fingerprint+type
    haben. False = Drift / fehlend / type-mismatch. UI markiert non-uniform."""


@dataclass(frozen=True, slots=True)
class AliasComparison:
    """Vollstaendige Matrix fuer eine Geraeteauswahl."""

    columns: tuple[str, ...]                   # device_ids in stabiler Reihenfolge
    rows: tuple[AliasRow, ...]                 # nach Name alphabetisch sortiert
    summary: str                               # Kurz-Zusammenfassung fuer UI


def compare_aliases(
    per_device: dict[str, list[AliasItem] | None],
    columns: list[str],
) -> AliasComparison:
    """Bildet aus pro-Geraet-Alias-Listen die Matrix.

    ``per_device[device_id]`` = None bedeutet "nicht erreichbar"; die Cells
    werden als ``unreachable`` markiert. Leere Liste = erreichbar aber
    keine Aliase definiert.
    """
    # Alle gesehenen Namen sammeln
    all_names: set[str] = set()
    items_by_device: dict[str, dict[str, AliasItem]] = {}
    for device_id in columns:
        items = per_device.get(device_id)
        if items is None:
            items_by_device[device_id] = {}
            continue
        items_by_device[device_id] = {a.name: a for a in items}
        all_names.update(a.name for a in items)

    rows: list[AliasRow] = []
    drift_count = 0
    for name in sorted(all_names, key=str.lower):
        cells: list[tuple[str, AliasCell]] = []
        fingerprints_seen: set[tuple[str, str]] = set()  # (type, fp)
        any_absent = False
        for device_id in columns:
            if per_device.get(device_id) is None:
                cells.append((device_id, AliasCell(status="unreachable")))
                continue
            entry = items_by_device[device_id].get(name)
            if entry is None:
                cells.append((device_id, AliasCell(status="absent")))
                any_absent = True
                continue
            cell = AliasCell(
                status="present",
                type=entry.type,
                content_fingerprint=entry.content_fingerprint,
                content_count=len(entry.content),
                description=entry.description,
            )
            fingerprints_seen.add((entry.type, entry.content_fingerprint))
            cells.append((device_id, cell))
        # uniform = nur erreichbare Geraete betrachten + keine luecke + ein einziger fingerprint
        uniform = not any_absent and len(fingerprints_seen) == 1
        if not uniform:
            drift_count += 1
        rows.append(AliasRow(name=name, cells=tuple(cells), uniform=uniform))

    if not rows:
        summary = "Keine Aliase auf den gewaehlten Geraeten gefunden."
    elif drift_count == 0:
        summary = f"{len(rows)} Aliase, alle auf allen Geraeten identisch."
    else:
        summary = (
            f"{len(rows)} Aliase insgesamt, davon {drift_count} mit "
            "Unterschieden zwischen den Geraeten."
        )

    return AliasComparison(
        columns=tuple(columns),
        rows=tuple(rows),
        summary=summary,
    )


__all__ = [
    "AliasCell",
    "AliasComparison",
    "AliasItem",
    "AliasRow",
    "compare_aliases",
    "extract_aliases",
]
