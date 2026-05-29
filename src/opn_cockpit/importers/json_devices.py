"""JSON-Import fuer Firewall-Geraete (Vault-Devices).

Format (UTF-8): Liste von Objekten.

```json
[
  {"name": "HQ Berlin", "host": "opn-berlin.lab", "port": 443,
   "tls_verify": true, "tags": ["branches", "germany"], "descr": "HQ",
   "api_key": "KEY", "api_secret": "SECRET"}
]
```

Pflichtfelder: ``name``, ``host``, ``api_key``, ``api_secret``.
Optional: ``port`` (Default 443), ``tls_verify`` (Default true),
``tags`` (Liste), ``descr``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from opn_cockpit.vault.model import VaultDevice

MIN_PORT = 1
MAX_PORT = 65535


@dataclass(slots=True)
class DeviceJsonImportResult:
    devices: list[VaultDevice] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return bool(self.errors)


def parse_devices_json(path: Path | str) -> DeviceJsonImportResult:
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        return DeviceJsonImportResult(errors=[f"Datei nicht lesbar: {p} ({exc})"])
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        return DeviceJsonImportResult(errors=[f"JSON nicht parsbar: {exc}"])

    result = DeviceJsonImportResult()
    if not isinstance(raw, list):
        result.errors.append("JSON-Wurzel muss eine Liste sein.")
        return result

    for idx, entry in enumerate(raw, start=1):
        if not isinstance(entry, dict):
            found = type(entry).__name__
            result.errors.append(f"Eintrag {idx}: Objekt erwartet, gefunden {found}.")
            continue
        try:
            device = _entry_to_device(entry)
        except ValueError as exc:
            result.errors.append(f"Eintrag {idx}: {exc}")
            continue
        result.devices.append(device)
    return result


def _entry_to_device(raw: dict[str, Any]) -> VaultDevice:
    name = str(raw.get("name", "")).strip()
    host = str(raw.get("host", "")).strip()
    api_key = str(raw.get("api_key", "")).strip()
    api_secret = str(raw.get("api_secret", "")).strip()

    if not name:
        raise ValueError("Feld 'name' fehlt oder ist leer.")
    if not host:
        raise ValueError("Feld 'host' fehlt oder ist leer.")
    if not api_key:
        raise ValueError("Feld 'api_key' fehlt oder ist leer.")
    if not api_secret:
        raise ValueError("Feld 'api_secret' fehlt oder ist leer.")

    port = raw.get("port", 443)
    try:
        port = int(port)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Port '{port}' ist keine Zahl.") from exc
    if port < MIN_PORT or port > MAX_PORT:
        raise ValueError(f"Port {port} ausserhalb von {MIN_PORT}..{MAX_PORT}.")

    tls_verify = bool(raw.get("tls_verify", True))

    tags_raw = raw.get("tags", [])
    if isinstance(tags_raw, list):
        tags = [str(t).strip() for t in tags_raw if str(t).strip()]
    else:
        tags = []

    return VaultDevice(
        id=VaultDevice.new_id(),
        name=name,
        host=host,
        port=port,
        tls_verify=tls_verify,
        tags=tags,
        api_key=api_key,
        api_secret=api_secret,
        descr=str(raw.get("descr", "")),
    )
