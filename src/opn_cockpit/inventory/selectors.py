"""Geräte-Auswahl per kurzer String-Notation.

Wird vom CLI und (in Schritt 8) von der GUI-Filterleiste genutzt, um aus
allen inventarisierten Geräten eine Teilmenge zu wählen, gegen die eine
Aktion läuft (R-ACT-3, R-DEV-4).

Notation:

* ``all`` (oder leerer String) — alle Geräte
* ``tag:branches`` / ``group:branches`` — alle Geräte mit Tag "branches"
* ``id:<exakt>`` — Gerät mit dieser ID (eindeutig)
* ``name:<teilwort>`` — Geräte, deren Name ``teilwort`` enthält (case-insensitive)
* ``<teilwort>`` (ohne Präfix) — wie ``name:<teilwort>``

Komma-getrennte Mehrfachselektoren werden als Vereinigung interpretiert,
Duplikate werden entfernt (Ordnung der ersten Auswahl bleibt erhalten).
"""

from __future__ import annotations

from collections.abc import Iterable

from opn_cockpit.inventory.model import Device

KNOWN_KINDS = ("tag", "group", "id", "name")


class SelectorError(ValueError):
    """Selektor war syntaktisch falsch oder verwies auf einen unbekannten Typ."""


def apply_selector(devices: Iterable[Device], selector: str) -> list[Device]:
    """Filtert ``devices`` nach ``selector``.

    Mehrere Selektoren werden mit Komma getrennt, das Ergebnis ist die
    Vereinigung in Reihenfolge des Auftretens.
    """
    devices_list = list(devices)
    raw = selector.strip()
    if not raw or raw.lower() == "all":
        return list(devices_list)

    seen_ids: set[str] = set()
    result: list[Device] = []
    for part in (p.strip() for p in raw.split(",")):
        if not part:
            continue
        for device in _select_one(devices_list, part):
            if device.id not in seen_ids:
                seen_ids.add(device.id)
                result.append(device)
    return result


def _select_one(devices: list[Device], selector: str) -> list[Device]:
    if ":" not in selector:
        needle = selector.lower()
        return [d for d in devices if needle in d.name.lower()]
    kind, _, value = selector.partition(":")
    kind = kind.strip().lower()
    value = value.strip()
    if not value:
        raise SelectorError(f"Leerer Wert im Selektor '{selector}'.")
    if kind in ("tag", "group"):
        needle = value.lower()
        return [d for d in devices if any(t.lower() == needle for t in d.tags)]
    if kind == "id":
        return [d for d in devices if d.id == value]
    if kind == "name":
        needle = value.lower()
        return [d for d in devices if needle in d.name.lower()]
    raise SelectorError(
        f"Unbekannter Selektor-Typ '{kind}'. Erlaubt: {', '.join(KNOWN_KINDS)}."
    )
