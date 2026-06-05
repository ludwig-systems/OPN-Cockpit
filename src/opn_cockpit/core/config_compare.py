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
    content: tuple[str, ...] = ()
    """Vollstaendiger sortierter Inhalt - fuer den UI-Detail-Aufklapp."""


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
                content=entry.content,
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


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RouteItem:
    """Vergleichbare Repraesentation einer statischen Route."""

    network: str
    gateway: str
    descr: str
    disabled: bool

    @property
    def identity_key(self) -> str:
        """Row-Key in der Compare-Matrix: Netzwerk|Gateway.

        Identitaet einer Route nach OPNsense-Logik. Zwei Routen mit
        gleichem Netz aber unterschiedlichem Gateway sind unterschiedliche
        Routen.
        """
        return f"{self.network}|{self.gateway}"

    @property
    def content_fingerprint(self) -> str:
        """Hash ueber descr + disabled. Wird genutzt um zu erkennen ob die
        Route auf allen Geraeten identisch ist (gleiches Netz/Gateway haben
        sie schon - das ist die Identitaet)."""
        raw = f"{self.descr}\0{'1' if self.disabled else '0'}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def _iter_route_nodes(root: ET.Element) -> list[ET.Element]:
    """Findet alle <route>-Knoten im OPNsense-XML."""
    for path in (
        "./staticroutes/route",
        "./opnsense/staticroutes/route",
    ):
        found = root.findall(path)
        if found:
            return found
    return []


def extract_routes(xml_bytes: bytes) -> list[RouteItem]:
    """Parsed statische Routen aus einem OPNsense-Konfig-Backup."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return []
    items: list[RouteItem] = []
    for node in _iter_route_nodes(root):
        network = (node.findtext("network") or "").strip()
        gateway = (node.findtext("gateway") or "").strip()
        if not network or not gateway:
            continue
        disabled_raw = (node.findtext("disabled") or "").strip()
        items.append(RouteItem(
            network=network,
            gateway=gateway,
            descr=(node.findtext("descr") or node.findtext("description") or "").strip(),
            disabled=disabled_raw in ("1", "true", "yes"),
        ))
    return items


def compare_routes(
    per_device: dict[str, list[RouteItem] | None],
    columns: list[str],
) -> AliasComparison:
    """Bildet aus pro-Geraet-Route-Listen die Vergleichs-Matrix.

    Reuse von ``AliasComparison`` als Zielstruktur damit das Schema im
    Endpoint stabil bleibt - inhaltlich tragen die Felder hier andere
    Semantik:

    * ``row.name`` = Netzwerk (z. B. "10.0.0.0/24")
    * ``cell.type`` = Gateway
    * ``cell.content`` = ["via <gw>", "deaktiviert"|"aktiv", "descr: <text>"]
    * ``cell.content_fingerprint`` = Hash ueber descr + disabled
    """
    all_keys: set[str] = set()
    items_by_device: dict[str, dict[str, RouteItem]] = {}
    for device_id in columns:
        items = per_device.get(device_id)
        if items is None:
            items_by_device[device_id] = {}
            continue
        items_by_device[device_id] = {r.identity_key: r for r in items}
        all_keys.update(r.identity_key for r in items)

    rows: list[AliasRow] = []
    drift_count = 0
    for key in sorted(all_keys, key=str.lower):
        network, gateway = key.split("|", 1)
        cells: list[tuple[str, AliasCell]] = []
        fingerprints_seen: set[str] = set()
        any_absent = False
        for device_id in columns:
            if per_device.get(device_id) is None:
                cells.append((device_id, AliasCell(status="unreachable")))
                continue
            entry = items_by_device[device_id].get(key)
            if entry is None:
                cells.append((device_id, AliasCell(status="absent")))
                any_absent = True
                continue
            detail = [
                f"via {entry.gateway}",
                "deaktiviert" if entry.disabled else "aktiv",
            ]
            if entry.descr:
                detail.append(f"descr: {entry.descr}")
            cells.append((device_id, AliasCell(
                status="present",
                type=entry.gateway,
                content_fingerprint=entry.content_fingerprint,
                content_count=0,
                description=entry.descr,
                content=tuple(detail),
            )))
            fingerprints_seen.add(entry.content_fingerprint)
        uniform = not any_absent and len(fingerprints_seen) == 1
        if not uniform:
            drift_count += 1
        rows.append(AliasRow(name=network, cells=tuple(cells), uniform=uniform))

    if not rows:
        summary = "Keine statischen Routen auf den gewaehlten Geraeten."
    elif drift_count == 0:
        summary = f"{len(rows)} Routen, alle auf allen Geraeten identisch."
    else:
        summary = (
            f"{len(rows)} Routen insgesamt, davon {drift_count} mit "
            "Unterschieden zwischen den Geraeten."
        )

    return AliasComparison(
        columns=tuple(columns),
        rows=tuple(rows),
        summary=summary,
    )


# ---------------------------------------------------------------------------
# Firewall-Filter-Regeln
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RuleItem:
    """Vergleichbare Repraesentation einer Filter-Regel.

    Da Regeln keinen stabilen User-Schluessel haben (UUID ist pro Box
    verschieden), nutzen wir den content_fingerprint als Row-Key + die
    Description (oder ersten 40 Zeichen davon) als sichtbaren Namen.
    Folge: Regeln ohne Beschreibung sind in der Matrix nur per Inhalt
    unterscheidbar - das ist akzeptabel und ein zusaetzlicher Anreiz
    fuer den Admin, Beschreibungen zu pflegen.
    """

    description: str
    enabled: bool
    action: str
    interface: str
    direction: str
    protocol: str
    source_net: str
    source_port: str
    destination_net: str
    destination_port: str
    gateway: str
    log: bool

    @property
    def content_fingerprint(self) -> str:
        raw = "\0".join([
            self.action, self.interface, self.direction, self.protocol,
            self.source_net, self.source_port,
            self.destination_net, self.destination_port,
            self.gateway,
            "1" if self.enabled else "0",
            "1" if self.log else "0",
        ])
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]

    @property
    def identity_key(self) -> str:
        """Row-Key: Description wenn vorhanden, sonst Fingerprint.

        Damit fallen Regeln gleicher Beschreibung in einer Row zusammen -
        auch wenn ihr Inhalt unterschiedlich ist (Drift sichtbar). Regeln
        ohne Beschreibung sind nur ueber den Inhalt vergleichbar.
        """
        if self.description.strip():
            return f"descr:{self.description.strip().lower()}"
        return f"fp:{self.content_fingerprint}"


def _iter_rule_nodes(root: ET.Element) -> list[ET.Element]:
    """Findet alle <rule>-Knoten unter <filter>."""
    for path in (
        "./filter/rule",
        "./opnsense/filter/rule",
    ):
        found = root.findall(path)
        if found:
            return found
    return []


def extract_rules(xml_bytes: bytes) -> list[RuleItem]:
    """Parsed Filter-Regeln aus einem OPNsense-Konfig-Backup.

    Liest direkt aus dem core-XML, nicht ueber die os-firewall-API - damit
    funktioniert der Compare auch wenn das Plugin auf einzelnen Boxen
    fehlt. Felder die der Adapter nicht kennt (statetype etc.) ignorieren
    wir hier bewusst.
    """
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return []
    items: list[RuleItem] = []
    for node in _iter_rule_nodes(root):
        source = node.find("source")
        destination = node.find("destination")
        src_net = ""
        src_port = ""
        if source is not None:
            src_net = (source.findtext("network")
                       or source.findtext("address")
                       or ("any" if source.find("any") is not None else ""))
            src_port = (source.findtext("port") or "").strip()
        dst_net = ""
        dst_port = ""
        if destination is not None:
            dst_net = (destination.findtext("network")
                       or destination.findtext("address")
                       or ("any" if destination.find("any") is not None else ""))
            dst_port = (destination.findtext("port") or "").strip()
        items.append(RuleItem(
            description=(node.findtext("descr")
                         or node.findtext("description")
                         or "").strip(),
            enabled=node.findtext("disabled") in (None, "", "0"),
            action=(node.findtext("type") or "pass").strip(),
            interface=(node.findtext("interface") or "").strip(),
            direction=(node.findtext("direction") or "in").strip(),
            protocol=(node.findtext("protocol") or "any").strip(),
            source_net=(src_net or "").strip(),
            source_port=src_port,
            destination_net=(dst_net or "").strip(),
            destination_port=dst_port,
            gateway=(node.findtext("gateway") or "").strip(),
            log=node.findtext("log") in ("1", "true", "yes"),
        ))
    return items


def compare_rules(
    per_device: dict[str, list[RuleItem] | None],
    columns: list[str],
) -> AliasComparison:
    """Vergleichs-Matrix fuer Filter-Regeln, reused AliasComparison-Schema.

    * ``row.name`` = Description (oder "(ohne Beschreibung)" bei fp-Match)
    * ``cell.type`` = Action (pass/block/reject)
    * ``cell.content`` = ["interface", "proto src->dst", ggf. "log"]
    """
    all_keys: set[str] = set()
    items_by_device: dict[str, dict[str, RuleItem]] = {}
    for device_id in columns:
        items = per_device.get(device_id)
        if items is None:
            items_by_device[device_id] = {}
            continue
        items_by_device[device_id] = {r.identity_key: r for r in items}
        all_keys.update(r.identity_key for r in items)

    rows: list[AliasRow] = []
    drift_count = 0
    for key in sorted(all_keys, key=str.lower):
        display_name = ""
        cells: list[tuple[str, AliasCell]] = []
        fingerprints_seen: set[str] = set()
        any_absent = False
        for device_id in columns:
            if per_device.get(device_id) is None:
                cells.append((device_id, AliasCell(status="unreachable")))
                continue
            entry = items_by_device[device_id].get(key)
            if entry is None:
                cells.append((device_id, AliasCell(status="absent")))
                any_absent = True
                continue
            if not display_name:
                display_name = (
                    entry.description.strip()
                    if entry.description.strip()
                    else f"(ohne Beschreibung, fp={entry.content_fingerprint})"
                )
            proto = entry.protocol if entry.protocol != "any" else ""
            flow = (
                f"{proto + ' ' if proto else ''}"
                f"{entry.source_net or 'any'}"
                f"{':' + entry.source_port if entry.source_port else ''}"
                f" -> {entry.destination_net or 'any'}"
                f"{':' + entry.destination_port if entry.destination_port else ''}"
            )
            detail = [
                f"interface: {entry.interface or '-'}",
                flow,
            ]
            if entry.gateway:
                detail.append(f"gateway: {entry.gateway}")
            if entry.log:
                detail.append("log: an")
            if not entry.enabled:
                detail.append("DEAKTIVIERT")
            cells.append((device_id, AliasCell(
                status="present",
                type=entry.action,
                content_fingerprint=entry.content_fingerprint,
                content_count=0,
                description=entry.description,
                content=tuple(detail),
            )))
            fingerprints_seen.add(entry.content_fingerprint)
        uniform = not any_absent and len(fingerprints_seen) == 1
        if not uniform:
            drift_count += 1
        rows.append(AliasRow(name=display_name or key, cells=tuple(cells), uniform=uniform))

    if not rows:
        summary = "Keine Filter-Regeln auf den gewaehlten Geraeten."
    elif drift_count == 0:
        summary = f"{len(rows)} Filter-Regeln, alle auf allen Geraeten identisch."
    else:
        summary = (
            f"{len(rows)} Filter-Regeln insgesamt, davon {drift_count} mit "
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
    "RouteItem",
    "RuleItem",
    "compare_aliases",
    "compare_routes",
    "compare_rules",
    "extract_aliases",
    "extract_routes",
    "extract_rules",
]
