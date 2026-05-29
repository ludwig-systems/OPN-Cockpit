"""CSV-Import fuer Firewall-Geraete (Vault-Devices).

Format (UTF-8, Header-Zeile zwingend):

```csv
name,host,port,tls_verify,tags,descr,api_key,api_secret
HQ Berlin,opn-berlin.lab,443,true,branches;germany,HQ,KEY,SECRET
Branch Munich,opn-munich.lab,443,false,branches;germany,,KEY2,SECRET2
```

Pflichtfelder: ``name``, ``host``, ``api_key``, ``api_secret``.
Optional: ``port`` (Default 443), ``tls_verify`` (Default true),
``tags`` (Semikolon-getrennt, Komma kollidiert mit CSV), ``descr``.

Leere Zeilen und Kommentar-Zeilen (Zeile beginnt mit ``#``) werden
uebersprungen.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

from opn_cockpit.vault.model import VaultDevice

REQUIRED_COLUMNS = ("name", "host", "api_key", "api_secret")
TRUTHY = frozenset({"1", "true", "ja", "yes", "y", "on"})
MIN_PORT = 1
MAX_PORT = 65535


@dataclass(slots=True)
class DeviceCsvImportResult:
    """Erfolge + Zeilen-Fehler bei einem Device-CSV-Import."""

    devices: list[VaultDevice] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return bool(self.errors)


def parse_devices_csv(path: Path | str) -> DeviceCsvImportResult:
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        return DeviceCsvImportResult(errors=[f"Datei nicht lesbar: {p} ({exc})"])

    result = DeviceCsvImportResult()
    reader = csv.DictReader(
        line for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    )
    if reader.fieldnames is None:
        result.errors.append("CSV ist leer oder enthaelt keinen Header.")
        return result
    headers = [h.strip().lower() for h in reader.fieldnames]
    missing = [col for col in REQUIRED_COLUMNS if col not in headers]
    if missing:
        result.errors.append(
            f"Fehlende Pflicht-Spalten: {', '.join(missing)}"
        )
        return result

    norm_keys = {orig: orig.strip().lower() for orig in reader.fieldnames}

    for row_num, row in enumerate(reader, start=2):
        normalized = {
            norm_keys[k]: (v or "").strip()
            for k, v in row.items()
            if k
        }
        try:
            device = _row_to_device(normalized)
        except ValueError as exc:
            result.errors.append(f"Zeile {row_num}: {exc}")
            continue
        result.devices.append(device)
    return result


def _row_to_device(row: dict[str, str]) -> VaultDevice:
    name = row.get("name", "").strip()
    host = row.get("host", "").strip()
    api_key = row.get("api_key", "").strip()
    api_secret = row.get("api_secret", "").strip()
    if not name:
        raise ValueError("Feld 'name' fehlt oder ist leer.")
    if not host:
        raise ValueError("Feld 'host' fehlt oder ist leer.")
    if not api_key:
        raise ValueError("Feld 'api_key' fehlt oder ist leer.")
    if not api_secret:
        raise ValueError("Feld 'api_secret' fehlt oder ist leer.")

    port_raw = row.get("port", "").strip() or "443"
    try:
        port = int(port_raw)
    except ValueError as exc:
        raise ValueError(f"Port '{port_raw}' ist keine Zahl.") from exc
    if port < MIN_PORT or port > MAX_PORT:
        raise ValueError(f"Port {port} ausserhalb von {MIN_PORT}..{MAX_PORT}.")

    tls_raw = row.get("tls_verify", "").strip().lower()
    tls_verify = tls_raw in TRUTHY if tls_raw else True

    tags_raw = row.get("tags", "").strip()
    tags = [t.strip() for t in tags_raw.split(";") if t.strip()] if tags_raw else []

    return VaultDevice(
        id=VaultDevice.new_id(),
        name=name,
        host=host,
        port=port,
        tls_verify=tls_verify,
        tags=tags,
        api_key=api_key,
        api_secret=api_secret,
        descr=row.get("descr", "").strip(),
    )
