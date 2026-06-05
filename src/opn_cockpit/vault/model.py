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

    # v0.8 #8 Safety-Net via SSH. Default AUS - User muss SSH-Zugang
    # explizit aktivieren und einen Private-Key hinterlegen. Der Key
    # liegt verschluesselt im Tresor (gleicher Schutz wie api_secret).
    # ssh_host=leer -> faellt auf api-host zurueck, gleicher Pattern wie
    # ein Backup-Switch.
    ssh_enabled: bool = False
    ssh_host: str = ""
    ssh_port: int = 22
    ssh_user: str = ""
    ssh_private_key_pem: str = ""

    @staticmethod
    def new_id() -> str:
        return str(uuid.uuid4())


@dataclass(slots=True)
class VaultSettings:
    """Pro-Tresor-Settings, die mit ihm wandern (Portabilität).

    ``inactivity_minutes`` ist gemäß User-Anforderung änderbar — wer den
    Tresor öffnet, hat die im Tresor hinterlegte Inaktivitätszeit als
    Default.

    Auto-Backup-Felder (v0.7-Theme "Safety Nets"):

    * ``auto_backup_before_apply`` — wenn True, zieht der Executor vor
      jedem schreibenden Apply ein Backup pro Geraet. Scheitert ein
      Backup, wird das Apply auf dem Geraet **blockiert**. Default True;
      kann pro Tresor abgeschaltet werden.
    * ``backup_retention_pre_apply`` — Anzahl der zu behaltenden
      pre-apply + manual Backups pro Geraet (gemeinsamer Pool, weil
      User-Erwartung ist dass manuelle nicht durch scheduled rausfallen).
    * ``backup_retention_scheduled`` — Anzahl der zu behaltenden
      scheduled Backups pro Geraet (eigener Pool fuer kommende
      Hintergrund-Snapshots).
    """

    inactivity_minutes: int = 10
    max_workers: int = 8
    connect_timeout_s: float = 5.0
    read_timeout_s: float = 30.0
    reconfigure_timeout_s: float = 60.0
    retry_count: int = 2
    auto_backup_before_apply: bool = True
    backup_retention_pre_apply: int = 30
    backup_retention_scheduled: int = 90
    # v0.7 #4 Scheduled Auto-Backup. Default AUS - manche Setups wollen
    # keine automatische Verbindung zur OPNsense ohne explizite Freigabe
    # (Audit-Eintraege auf der Box). Wer aktiviert, kriegt pro Geraet
    # alle ``scheduled_backup_interval_hours`` Stunden ein Snapshot.
    scheduled_backup_enabled: bool = False
    scheduled_backup_interval_hours: int = 24
    # v0.7 #5 Config-Drift-Erkennung. Default AUS - braucht einen API-
    # Call pro Geraet pro Inventar-Refresh. Wenn an: Cockpit faerbt eine
    # Drift-Anzeige auf der Karte wenn der Live-Config-Hash vom letzten
    # Backup-Hash abweicht (Indikator fuer ungesicherte Aenderungen).
    drift_detection_enabled: bool = False
    # v0.7 #6 Auto-Retry fuer Mobile-Racks. Default an - der Watcher gibt
    # einem offline-Geraet bis zu auto_retry_max_hours Stunden Zeit
    # wieder erreichbar zu werden und wendet den Plan dann ohne weiteres
    # Zutun an. Solange die Session lebt, laeuft der Watcher weiter.
    auto_retry_enabled: bool = True
    auto_retry_max_hours: int = 168     # 7 Tage - mobile Racks koennen lang offline sein
    auto_retry_interval_minutes: int = 5

    # v0.8 #8 Safety-Net via SSH. Default AUS - braucht SSH-Zugang
    # auf den Boxen. Wenn aktiv: User kann "Apply mit Sicherheitsnetz"
    # waehlen; nach erfolgreichem Apply hat er die hier konfigurierten
    # Sekunden Zeit zu bestaetigen. Tut er das nicht (z. B. weil die
    # Box nach dem Apply unerreichbar ist), rollt der SafetyNetWatcher
    # via SSH auf das Pre-Apply-Backup zurueck.
    safety_net_enabled: bool = False
    safety_net_window_s: int = 120


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
        ssh_enabled=bool(raw.get("ssh_enabled", False)),
        ssh_host=str(raw.get("ssh_host", "")),
        ssh_port=int(raw.get("ssh_port", 22)),
        ssh_user=str(raw.get("ssh_user", "")),
        ssh_private_key_pem=str(raw.get("ssh_private_key_pem", "")),
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
        auto_backup_before_apply=bool(
            raw.get("auto_backup_before_apply", defaults.auto_backup_before_apply),
        ),
        backup_retention_pre_apply=int(
            raw.get("backup_retention_pre_apply", defaults.backup_retention_pre_apply),
        ),
        backup_retention_scheduled=int(
            raw.get("backup_retention_scheduled", defaults.backup_retention_scheduled),
        ),
        scheduled_backup_enabled=bool(
            raw.get("scheduled_backup_enabled", defaults.scheduled_backup_enabled),
        ),
        scheduled_backup_interval_hours=int(
            raw.get("scheduled_backup_interval_hours", defaults.scheduled_backup_interval_hours),
        ),
        drift_detection_enabled=bool(
            raw.get("drift_detection_enabled", defaults.drift_detection_enabled),
        ),
        auto_retry_enabled=bool(
            raw.get("auto_retry_enabled", defaults.auto_retry_enabled),
        ),
        auto_retry_max_hours=int(
            raw.get("auto_retry_max_hours", defaults.auto_retry_max_hours),
        ),
        auto_retry_interval_minutes=int(
            raw.get("auto_retry_interval_minutes", defaults.auto_retry_interval_minutes),
        ),
        safety_net_enabled=bool(
            raw.get("safety_net_enabled", defaults.safety_net_enabled),
        ),
        safety_net_window_s=int(
            raw.get("safety_net_window_s", defaults.safety_net_window_s),
        ),
    )
