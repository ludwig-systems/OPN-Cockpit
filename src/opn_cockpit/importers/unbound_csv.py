"""CSV Import/Export fuer Unbound-DNS Host-Overrides + Query-Forwards.

Zweck: bei grossem Bestand (User hatte 124 Query-Forwards) ist die
Hand-Eingabe im Modal muehsam. Mit Export kann er die aktuelle Konfig
als CSV in Excel oeffnen, die Werte in einer Kalkulation aufbauen und
per Import zurueck ins Cockpit spielen — dann laufen alle Zeilen durch
den normalen Plan/Apply-Flow inkl. Pre-Apply-Backup.

**CSV-Format (UTF-8, RFC 4180):**

Host-Overrides:

```csv
host,domain,server,description,enabled
router,lab.local,10.0.0.1,MainRouter,true
switch,lab.local,10.0.0.2,,false
```

Query-Forwards:

```csv
domain,server,port,type,verify,description,enabled
,1.1.1.1,853,dot,cloudflare-dns.com,Cloudflare DoT,true
corp.example.com,10.0.0.53,53,forward,,AD-DNS Zone,true
```

**Regeln:**

* Erste Zeile ist Header. Reihenfolge egal, Namen case-insensitive.
* Pflichtspalten Host-Overrides: ``host``, ``domain``, ``server``.
* Pflichtspalten Query-Forwards: ``server``.
* ``enabled`` akzeptiert ``true``/``false``/``1``/``0``/``ja``/``nein``.
  Leer = ``true``.
* Leere Zeilen und Kommentar-Zeilen (beginnen mit ``#``) werden
  uebersprungen.
* Zeilenfehler brechen den Import **nicht** ab — alle Fehler landen in
  ``result.errors`` und der User sieht sie in der Preview zusammen.

**Encoding:** Beim Export schreiben wir UTF-8 **mit BOM**, damit Excel
Umlaute nicht kaputt macht. Der Parser toleriert BOM.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from typing import Iterable

from opn_cockpit.core.objects.unbound import UnboundForwardSpec, UnboundHostSpec

# ---------------------------------------------------------------------------
# Konstanten
# ---------------------------------------------------------------------------

HOST_REQUIRED = ("host", "domain", "server")
FORWARD_REQUIRED = ("server",)

TRUTHY = frozenset({"1", "true", "ja", "yes", "y", "on"})
FALSY = frozenset({"0", "false", "nein", "no", "n", "off"})

# Excel + andere Tabellen-Tools schreiben BOM bei UTF-8. Beim Parsen ignorieren.
UTF8_BOM = "﻿"


# ---------------------------------------------------------------------------
# Ergebnistyp (gemeinsam fuer Hosts + Forwards)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class UnboundCsvImportResult:
    """Ergebnis eines CSV-Imports.

    Analog zu :class:`csv_routes.CsvImportResult`, aber Typ-parametrisch
    genutzt (via ``specs``-Liste — Elemente sind entweder alle
    ``UnboundHostSpec`` oder alle ``UnboundForwardSpec``).
    """

    specs: list = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return bool(self.errors)


# ---------------------------------------------------------------------------
# Bool-Parsing
# ---------------------------------------------------------------------------


def _parse_bool(raw: str, *, default: bool = True) -> bool:
    """Robust gegen Excel/User-Eingaben."""
    val = (raw or "").strip().lower()
    if not val:
        return default
    if val in TRUTHY:
        return True
    if val in FALSY:
        return False
    # Unbekannter Wert - defensiv wie default, kein Fehler (Excel-Zellen
    # koennen komische Wert-Repraesentationen haben).
    return default


def _parse_port(raw: str) -> int:
    val = (raw or "").strip()
    if not val:
        return 53
    try:
        port = int(val)
    except ValueError as exc:
        msg = f"Port muss eine Zahl sein, bekommen: {raw!r}"
        raise ValueError(msg) from exc
    if port < 1 or port > 65535:
        msg = f"Port {port} ausserhalb 1..65535."
        raise ValueError(msg)
    return port


# ---------------------------------------------------------------------------
# Header-Parsing (gemeinsam)
# ---------------------------------------------------------------------------


def _read_rows(
    text: str, required: tuple[str, ...],
) -> tuple[list[dict[str, str]], list[str]]:
    """Liefert (rows, errors).

    ``rows`` sind normalisiert (lowercase keys, stripped values). Bei
    Header-Problemen ist die Liste leer und ``errors`` enthaelt eine
    einzige Beschwerde.
    """
    # BOM abtrimmen falls Excel gespeichert hat.
    if text.startswith(UTF8_BOM):
        text = text[len(UTF8_BOM):]

    reader = csv.DictReader(
        line for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    )
    if reader.fieldnames is None:
        return [], ["CSV ist leer oder enthaelt keinen Header."]

    headers = [h.strip().lower() for h in reader.fieldnames]
    missing = [col for col in required if col not in headers]
    if missing:
        return [], [f"Fehlende Pflicht-Spalten: {', '.join(missing)}"]

    norm_keys = {orig: (orig or "").strip().lower() for orig in reader.fieldnames}
    rows: list[dict[str, str]] = []
    for row_num, row in enumerate(reader, start=2):
        normalized: dict[str, str] = {}
        for k, v in row.items():
            if not k:
                continue
            normalized[norm_keys[k]] = (v or "").strip()
        normalized["__row_num__"] = str(row_num)
        rows.append(normalized)
    return rows, []


# ---------------------------------------------------------------------------
# Host-Overrides
# ---------------------------------------------------------------------------


def parse_unbound_hosts_csv(text: str) -> UnboundCsvImportResult:
    """Parst CSV-Text zu einer Liste von :class:`UnboundHostSpec`.

    Wirft nie — Fehler landen in ``result.errors`` mit Zeilennummer.
    """
    result = UnboundCsvImportResult()
    rows, header_errors = _read_rows(text, HOST_REQUIRED)
    if header_errors:
        result.errors.extend(header_errors)
        return result

    for row in rows:
        row_num = row.get("__row_num__", "?")
        try:
            host = row.get("host", "")
            domain = row.get("domain", "")
            server = row.get("server", "")
            if not host or not domain or not server:
                msg = "host, domain und server sind Pflichtfelder."
                raise ValueError(msg)
            spec = UnboundHostSpec(
                host=host,
                domain=domain,
                server=server,
                description=row.get("description", ""),
                enabled=_parse_bool(row.get("enabled", ""), default=True),
            )
        except ValueError as exc:
            result.errors.append(f"Zeile {row_num}: {exc}")
            continue
        result.specs.append(spec)
    return result


def write_unbound_hosts_csv(specs: Iterable[UnboundHostSpec]) -> str:
    """Serialisiert eine Liste von Host-Overrides als CSV-String.

    Enthaelt UTF-8-BOM als erste Bytes — Excel oeffnet Datei sonst ohne
    Encoding-Detection und macht Umlaute kaputt.
    """
    buf = io.StringIO()
    buf.write(UTF8_BOM)
    writer = csv.writer(buf, lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(["host", "domain", "server", "description", "enabled"])
    for spec in specs:
        writer.writerow([
            spec.host,
            spec.domain,
            spec.server,
            spec.description,
            "true" if spec.enabled else "false",
        ])
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Query-Forwards
# ---------------------------------------------------------------------------


def parse_unbound_forwards_csv(text: str) -> UnboundCsvImportResult:
    """Parst CSV-Text zu einer Liste von :class:`UnboundForwardSpec`."""
    result = UnboundCsvImportResult()
    rows, header_errors = _read_rows(text, FORWARD_REQUIRED)
    if header_errors:
        result.errors.extend(header_errors)
        return result

    for row in rows:
        row_num = row.get("__row_num__", "?")
        try:
            server = row.get("server", "")
            if not server:
                msg = "server ist Pflichtfeld."
                raise ValueError(msg)
            port = _parse_port(row.get("port", ""))
            type_raw = (row.get("type", "") or "forward").strip().lower()
            if type_raw not in ("forward", "dot"):
                msg = f"type={type_raw!r} - erwartet 'forward' oder 'dot'."
                raise ValueError(msg)
            spec = UnboundForwardSpec(
                domain=row.get("domain", ""),
                server=server,
                port=port,
                type=type_raw,
                verify=row.get("verify", ""),
                description=row.get("description", ""),
                enabled=_parse_bool(row.get("enabled", ""), default=True),
            )
        except ValueError as exc:
            result.errors.append(f"Zeile {row_num}: {exc}")
            continue
        result.specs.append(spec)
    return result


def write_unbound_forwards_csv(specs: Iterable[UnboundForwardSpec]) -> str:
    """Serialisiert eine Liste von Query-Forwards als CSV-String."""
    buf = io.StringIO()
    buf.write(UTF8_BOM)
    writer = csv.writer(buf, lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerow([
        "domain", "server", "port", "type", "verify",
        "description", "enabled",
    ])
    for spec in specs:
        writer.writerow([
            spec.domain,
            spec.server,
            str(spec.port),
            spec.type or "forward",
            spec.verify,
            spec.description,
            "true" if spec.enabled else "false",
        ])
    return buf.getvalue()


__all__ = [
    "UnboundCsvImportResult",
    "parse_unbound_forwards_csv",
    "parse_unbound_hosts_csv",
    "write_unbound_forwards_csv",
    "write_unbound_hosts_csv",
]
