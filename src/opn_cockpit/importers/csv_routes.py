"""CSV-Import für statische Routen.

CSV-Format (UTF-8, mit Header-Zeile):

```csv
network,gateway,descr,disabled
10.0.0.0/24,WAN_GW,HQ,0
10.1.0.0/24,WAN_GW,Branch1,1
```

* ``network`` und ``gateway`` sind Pflicht.
* ``descr`` und ``disabled`` sind optional. ``disabled`` akzeptiert
  ``0``/``1``/``true``/``false``/``ja``/``nein``.
* Leere Zeilen und Kommentar-Zeilen (Zeile beginnt mit ``#``) werden
  übersprungen.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

from opn_cockpit.core.errors import ValidationError
from opn_cockpit.core.objects.routes import RouteSpec
from opn_cockpit.core.validation import parse_cidr, validate_gateway_name

REQUIRED_COLUMNS = ("network", "gateway")
TRUTHY = frozenset({"1", "true", "ja", "yes", "y", "on"})


@dataclass(slots=True)
class CsvImportResult:
    """Ergebnis eines CSV-Imports.

    Trennt Erfolg (``specs``) und einzelne Zeilen-Fehler (``errors``).
    Wir brechen NICHT beim ersten Fehler ab — der User sieht alle Fehler
    auf einen Blick und kann die CSV korrigieren.
    """

    specs: list[RouteSpec] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return bool(self.errors)


def parse_routes_csv(path: Path | str) -> CsvImportResult:
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        return CsvImportResult(errors=[f"Datei nicht lesbar: {p} ({exc})"])

    result = CsvImportResult()
    reader = csv.DictReader(

            line for line in text.splitlines()
            if line.strip() and not line.strip().startswith("#")

    )
    if reader.fieldnames is None:
        result.errors.append("CSV ist leer oder enthält keinen Header.")
        return result
    headers = [h.strip().lower() for h in reader.fieldnames]
    missing = [col for col in REQUIRED_COLUMNS if col not in headers]
    if missing:
        result.errors.append(
            f"Fehlende Pflicht-Spalten: {', '.join(missing)}"
        )
        return result

    # Map original-headers → normalisierte
    norm_keys = {orig: orig.strip().lower() for orig in reader.fieldnames}

    for row_num, row in enumerate(reader, start=2):  # ab Zeile 2 (Header = 1)
        normalized = {norm_keys[k]: (v or "").strip() for k, v in row.items() if k}
        try:
            spec = _row_to_route(normalized)
        except ValidationError as exc:
            result.errors.append(f"Zeile {row_num}: {exc}")
            continue
        result.specs.append(spec)
    return result


def _row_to_route(row: dict[str, str]) -> RouteSpec:
    network = row.get("network", "")
    gateway = row.get("gateway", "")
    descr = row.get("descr", "")
    disabled_raw = row.get("disabled", "")

    parse_cidr(network)  # wirft ValidationError bei Schrott
    validate_gateway_name(gateway)

    disabled = disabled_raw.lower() in TRUTHY
    return RouteSpec(
        network=network,
        gateway=gateway,
        descr=descr,
        disabled=disabled,
    )
