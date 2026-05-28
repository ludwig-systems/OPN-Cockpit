"""Datenmodell des entschlüsselten Tresor-Inhalts.

JSON-Serialisierung läuft über ``dataclasses.asdict`` + ``json.dumps``. Der
Reader ist defensiv: unbekannte Felder werden ignoriert, fehlende Felder
durch Defaults aufgefüllt — so überleben künftige Schema-Erweiterungen einen
älteren Reader (forward-kompatibel) und ein älterer Tresor lässt sich von
einem neueren Reader öffnen (backward-kompatibel).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from opn_cockpit.vault.errors import CorruptVaultError

SCHEMA_VERSION_CURRENT = 1


@dataclass(slots=True)
class VaultDevice:
    """Eine OPNsense-Instanz inklusive API-Credentials.

    Lebt **nur** innerhalb des entsperrten Tresors. Außerhalb (UI, Audit)
    wird die Sicht über ``inventory.Device`` reduziert, das die Secret-Felder
    nicht weitergibt.
    """

    id: str
    name: str
    host: str
    port: int = 443
    tls_verify: bool = True
    tags: list[str] = field(default_factory=list)
    api_key: str = ""
    api_secret: str = ""
    descr: str = ""

    @staticmethod
    def new_id() -> str:
        return str(uuid.uuid4())


@dataclass(slots=True)
class VaultSettings:
    """Pro-Tresor-Settings, die mit ihm wandern (Portabilität).

    ``inactivity_minutes`` ist gemäß User-Anforderung änderbar — wer den
    Tresor öffnet, hat die im Tresor hinterlegte Inaktivitätszeit als
    Default.
    """

    inactivity_minutes: int = 10
    max_workers: int = 8
    connect_timeout_s: float = 5.0
    read_timeout_s: float = 30.0
    reconfigure_timeout_s: float = 60.0
    retry_count: int = 2


@dataclass(slots=True)
class VaultData:
    """Gesamter Klartext-Inhalt eines Tresors."""

    schema_version: int = SCHEMA_VERSION_CURRENT
    devices: list[VaultDevice] = field(default_factory=list)
    settings: VaultSettings = field(default_factory=VaultSettings)

    # ----- Serialisierung -----

    def to_json_bytes(self) -> bytes:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2).encode("utf-8")

    @classmethod
    def from_json_bytes(cls, data: bytes) -> VaultData:
        try:
            decoded = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CorruptVaultError(
                f"Tresor-Inhalt ist kein gültiges JSON: {exc}"
            ) from exc
        if not isinstance(decoded, dict):
            raise CorruptVaultError("Tresor-Wurzelobjekt ist kein JSON-Objekt.")
        return cls._from_dict(decoded)

    # ----- Internals -----

    @classmethod
    def _from_dict(cls, raw: dict[str, Any]) -> VaultData:
        devices = [
            _device_from_dict(d)
            for d in raw.get("devices", [])
            if isinstance(d, dict)
        ]
        settings = _settings_from_dict(raw.get("settings") or {})
        return cls(
            schema_version=int(raw.get("schema_version", SCHEMA_VERSION_CURRENT)),
            devices=devices,
            settings=settings,
        )


def _device_from_dict(raw: dict[str, Any]) -> VaultDevice:
    """Defensiv: jeder Wert wird in den erwarteten Typ konvertiert."""
    tags_raw = raw.get("tags", [])
    tags = [str(t) for t in tags_raw] if isinstance(tags_raw, list) else []
    return VaultDevice(
        id=str(raw.get("id") or VaultDevice.new_id()),
        name=str(raw.get("name", "")),
        host=str(raw.get("host", "")),
        port=int(raw.get("port", 443)),
        tls_verify=bool(raw.get("tls_verify", True)),
        tags=tags,
        api_key=str(raw.get("api_key", "")),
        api_secret=str(raw.get("api_secret", "")),
        descr=str(raw.get("descr", "")),
    )


def _settings_from_dict(raw: dict[str, Any]) -> VaultSettings:
    defaults = VaultSettings()
    return VaultSettings(
        inactivity_minutes=int(raw.get("inactivity_minutes", defaults.inactivity_minutes)),
        max_workers=int(raw.get("max_workers", defaults.max_workers)),
        connect_timeout_s=float(raw.get("connect_timeout_s", defaults.connect_timeout_s)),
        read_timeout_s=float(raw.get("read_timeout_s", defaults.read_timeout_s)),
        reconfigure_timeout_s=float(
            raw.get("reconfigure_timeout_s", defaults.reconfigure_timeout_s)
        ),
        retry_count=int(raw.get("retry_count", defaults.retry_count)),
    )
