"""Pydantic-Schemas fuer alle API-Routen.

Ein zentrales Modul, damit Frontend (TypeScript-Generierung spaeter
moeglich) und Tests eine Single Source of Truth haben.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


def _strip_or_none(value: str | None) -> str | None:
    """Trimmt Whitespace und mappt leere Strings auf None.

    Greift bei API-Credentials-Feldern in Device-Schemas: ein per Paste
    eingefuegter Key/Secret aus ``apikey.txt`` kann trailing newline oder
    Tabulator-Reste haben. OPNsense vergleicht bytewise, weshalb solche
    Reste zu "Authentication failed" fuehren obwohl der Wert sonst korrekt
    ist. Defensive Strip am Schema-Eingang verhindert das auch fuer
    Bulk-Import-CSV oder API-Direkt-Aufrufe.
    """
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _strip_required(value: str) -> str:
    """Strip-Variante fuer Pflichtfelder. Leerer String bleibt leer (Pydantic
    rejected ihn dann ueber min_length=1)."""
    return value.strip()

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


class UnlockRequest(BaseModel):
    vault_path: str = Field(..., min_length=1, description="Absoluter Pfad zur .opnvault-Datei")
    password: str = Field(..., min_length=1)


class LoginRequest(BaseModel):
    """Multi-User-Login (POST /api/auth/login).

    Im Single-User-Mode unbenutzt. Pflicht: Username + Passwort gegen
    die zentrale User-DB.
    """

    username: str = Field(..., min_length=1, max_length=120)
    password: str = Field(..., min_length=1)


# ---------------------------------------------------------------------------
# User-Verwaltung (Multi-User-Mode)
# ---------------------------------------------------------------------------


class UserResponse(BaseModel):
    """Read-only User-Sicht. Kein Passwort-Hash — niemals exponieren."""

    id: int
    username: str
    role: str
    allowed_tags: list[str]
    created_at_iso: str
    last_login_at_iso: str | None
    disabled: bool


class UserListResponse(BaseModel):
    users: list[UserResponse]


class UserCreateRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=120)
    password: str = Field(..., min_length=12)
    role: str = Field(..., pattern="^(viewer|operator|admin)$")
    allowed_tags: list[str] = Field(default_factory=list)


class UserUpdateRequest(BaseModel):
    """Aenderung eines Users.

    Felder sind optional — nur was uebergeben wird, wird aktualisiert.
    Username ist absichtlich nicht aenderbar (zerstoert Audit-Spur).
    """

    role: str | None = Field(None, pattern="^(viewer|operator|admin)$")
    allowed_tags: list[str] | None = None
    disabled: bool | None = None


class PasswordChangeRequest(BaseModel):
    """Self-Service-Passwortwechsel des eingeloggten Users."""

    current_password: str = Field(..., min_length=1)
    new_password: str = Field(..., min_length=12)


class AdminPasswordResetRequest(BaseModel):
    """Admin-Reset: setzt das Passwort eines anderen Users.

    Kein ``current_password``-Feld — der Admin kennt das alte sowieso nicht.
    """

    new_password: str = Field(..., min_length=12)


class UnlockResponse(BaseModel):
    token: str
    vault_path: str
    vault_filename: str
    inactivity_timeout_s: int
    seconds_until_expiry: int


class CurrentSessionResponse(BaseModel):
    vault_path: str
    vault_filename: str
    inactivity_timeout_s: int
    seconds_until_expiry: int


# ---------------------------------------------------------------------------
# Vaults
# ---------------------------------------------------------------------------


class VaultEntry(BaseModel):
    path: str
    filename: str
    is_default: bool


class PathSuggestion(BaseModel):
    label: str
    path: str


class VaultListResponse(BaseModel):
    vaults: list[VaultEntry]
    suggested_new_path: str
    suggested_new_directory: str = ""
    suggested_new_name: str = ""
    path_suggestions: list[PathSuggestion] = []


class CreateVaultRequest(BaseModel):
    path: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)


class CreateVaultResponse(BaseModel):
    vault_path: str
    vault_filename: str
    token: str  # auto-unlock nach Anlegen
    inactivity_timeout_s: int
    seconds_until_expiry: int


# F5b: Tresor-Settings (Inaktivitaets-Timeout etc.)


class VaultSettingsResponse(BaseModel):
    inactivity_minutes: int
    max_workers: int
    auto_backup_before_apply: bool = True
    """v0.7: Pre-Apply-Backup an/aus. Default True bei alten Tresoren
    (siehe vault.model._settings_from_dict)."""

    backup_retention_pre_apply: int = 30
    backup_retention_scheduled: int = 90
    # v0.7 #4: Scheduled Auto-Backup
    scheduled_backup_enabled: bool = False
    scheduled_backup_interval_hours: int = 24
    # v0.7 #5: Config-Drift-Erkennung
    drift_detection_enabled: bool = False
    # v0.7 #6: Auto-Retry-Queue fuer Mobile-Racks
    auto_retry_enabled: bool = True
    auto_retry_max_hours: int = 168
    auto_retry_interval_minutes: int = 5


class VaultSettingsUpdateRequest(BaseModel):
    inactivity_minutes: int = Field(..., ge=1, le=240)
    auto_backup_before_apply: bool | None = None
    """Wenn None bleibt der bestehende Wert; sonst wird er gesetzt."""

    backup_retention_pre_apply: int | None = Field(None, ge=1, le=500)
    backup_retention_scheduled: int | None = Field(None, ge=1, le=500)
    # v0.7 #4
    scheduled_backup_enabled: bool | None = None
    scheduled_backup_interval_hours: int | None = Field(None, ge=1, le=168)
    """1h..7d Intervall. Werte < 1h verbietet auch der Scheduler intern."""
    # v0.7 #5
    drift_detection_enabled: bool | None = None
    # v0.7 #6
    auto_retry_enabled: bool | None = None
    auto_retry_max_hours: int | None = Field(None, ge=1, le=720)
    """1h..30d. Defaults 168h (7 Tage)."""
    auto_retry_interval_minutes: int | None = Field(None, ge=1, le=120)


# F5a: Master-Passwort des aktiven Tresors aendern


class ChangeVaultPasswordRequest(BaseModel):
    current_password: str = Field(..., min_length=1)
    new_password: str = Field(..., min_length=12)
    new_password_repeat: str = Field(..., min_length=1)


# ---------------------------------------------------------------------------
# Inventar
# ---------------------------------------------------------------------------


class DeviceResponse(BaseModel):
    """Read-only Geraete-Sicht fuer das Frontend.

    NIEMALS API-Key/Secret/SSH-Key durchreichen. Der Frontend-Client soll
    keine Klartext-Credentials sehen.
    """

    id: str
    name: str
    host: str
    port: int
    tls_verify: bool
    tags: list[str]
    descr: str
    ssh_enabled: bool = False
    ssh_host: str = ""
    ssh_port: int = 22
    ssh_user: str = ""
    ssh_key_present: bool = False
    """True wenn ein Private-Key im Tresor hinterlegt ist. Wert NICHT
    durchreichen - nur die Anwesenheit fuer das UI-Badge."""


class TagSummary(BaseModel):
    name: str
    count: int


class InventoryResponse(BaseModel):
    devices: list[DeviceResponse]
    tags: list[TagSummary]


class DeviceImportResponse(BaseModel):
    """Ergebnis eines Bulk-Device-Imports."""

    added: list[DeviceResponse]
    skipped_existing: list[str]
    parsed_count: int


class VaultImportRequest(BaseModel):
    """Geraete aus einer fremden .opnvault-Datei in den aktiven Vault uebernehmen.

    Quelle wird mit ``source_password`` entsperrt, Inhalt gelesen, Datei
    wieder geschlossen. Nur die Geraete-Stammdaten + Credentials werden
    uebernommen, Settings/Schema des aktiven Vaults bleiben unangetastet.
    Bereits vorhandene Geraete-Namen werden uebersprungen.
    """

    source_path: str = Field(..., min_length=1, description="Pfad zur Quell-.opnvault-Datei")
    source_password: str = Field(..., min_length=1, description="Master-Passwort des Quell-Vaults")


class VaultSwitchRequest(BaseModel):
    """Admin wechselt den aktiven Multi-User-Vault zur Laufzeit.

    Aktive Sessions anderer User werden invalidiert — die muessen sich
    danach neu einloggen. Der Admin behaelt sein Token; der zeigt
    danach auf den neuen Vault.
    """

    vault_path: str = Field(..., min_length=1)
    password: str = Field(..., min_length=12)
    create_if_missing: bool = Field(False)


class TemplateExportRequest(BaseModel):
    """Template-Export: erstellt eine Vault-Kopie mit leeren Secret-Feldern.

    ``template_password`` wird zur Verschluesselung des Templates genutzt
    — typisch ein Passwort, das der Empfaenger spaeter kennt. Kann gleich
    dem Master-Passwort sein.
    """

    template_password: str = Field(..., min_length=12)


class DeviceCreateRequest(BaseModel):
    """Anlegen eines Geraets im Tresor.

    Das Master-Passwort wird beim Unlock einmalig erfragt und in der
    Session gecached — Schreibvorgaenge brauchen es nicht erneut.
    """

    name: str = Field(..., min_length=1, max_length=120)
    host: str = Field(..., min_length=1, max_length=255)
    port: int = Field(443, ge=1, le=65535)
    tls_verify: bool = True
    tags: list[str] = Field(default_factory=list)
    descr: str = Field("", max_length=500)
    api_key: str = Field(..., min_length=1, max_length=255)
    api_secret: str = Field(..., min_length=1, max_length=500)
    ssh_enabled: bool = False
    ssh_host: str = Field("", max_length=255)
    ssh_port: int = Field(22, ge=1, le=65535)
    ssh_user: str = Field("", max_length=80)
    ssh_private_key_pem: str = Field("", max_length=20000)

    @field_validator("api_key", "api_secret", mode="before")
    @classmethod
    def _strip_credentials(cls, value: str) -> str:
        return _strip_required(value) if isinstance(value, str) else value


class DeviceUpdateRequest(BaseModel):
    """Aenderung eines Geraets im Tresor.

    Felder sind optional — nur was uebergeben wird, wird aktualisiert.
    ``api_key`` und ``api_secret`` bleiben unveraendert, wenn leer/weg —
    so kann der User Host/Port/Tags aendern ohne die Credentials erneut
    tippen zu muessen.
    """

    name: str | None = Field(None, min_length=1, max_length=120)
    host: str | None = Field(None, min_length=1, max_length=255)
    port: int | None = Field(None, ge=1, le=65535)
    tls_verify: bool | None = None
    tags: list[str] | None = None
    descr: str | None = Field(None, max_length=500)
    api_key: str | None = Field(None, max_length=255)
    api_secret: str | None = Field(None, max_length=500)
    ssh_enabled: bool | None = None
    ssh_host: str | None = Field(None, max_length=255)
    ssh_port: int | None = Field(None, ge=1, le=65535)
    ssh_user: str | None = Field(None, max_length=80)
    ssh_private_key_pem: str | None = Field(None, max_length=20000)

    @field_validator("api_key", "api_secret", mode="before")
    @classmethod
    def _strip_credentials(cls, value: str | None) -> str | None:
        return _strip_or_none(value) if isinstance(value, str) else value


class DeviceApiKeyResponse(BaseModel):
    """Antwort des Reveal-Endpunkts (nur Key, kein Secret)."""

    device_id: str
    api_key: str


class HeartbeatRequest(BaseModel):
    """Optional: Subset von Geraete-IDs probieren. Leer = alle."""

    device_ids: list[str] = Field(default_factory=list)
    timeout_s: float = Field(2.5, ge=0.1, le=10.0)


class HeartbeatEntry(BaseModel):
    device_id: str
    reachable: bool
    checked_at_iso: str


class HeartbeatResponse(BaseModel):
    results: list[HeartbeatEntry]


class ConnectionTestResponse(BaseModel):
    device_id: str
    reachable: bool
    authenticated: bool
    summary: str


# Firmware-Status (Feature: OPNsense-Version auf der Karte)


class FirmwareStatusRequest(BaseModel):
    """Optional: Subset von Geraete-IDs probieren. Leer = alle sichtbaren."""

    device_ids: list[str] = Field(default_factory=list)


class FirmwareStatusEntry(BaseModel):
    device_id: str
    reachable: bool
    authenticated: bool
    version: str
    status: str
    update_available: bool
    summary: str
    checked_at_iso: str
    new_version: str = ""
    """Zielversion eines verfuegbaren Updates, leer wenn keins ansteht."""

    status_msg: str = ""
    """OPNsense-eigene Status-Beschreibung fuer Tooltip auf der Karte."""


class FirmwareStatusResponse(BaseModel):
    results: list[FirmwareStatusEntry]


# ---------------------------------------------------------------------------
# Cert-Inventur (v0.7 Safety-Net #3)
# ---------------------------------------------------------------------------


class CertEntryResponse(BaseModel):
    """Einzelnes OPNsense-Trust-Cert mit Ablauf-Vorberechnung."""

    uuid: str
    descr: str
    common_name: str
    issuer: str
    not_after_iso: str
    days_until_expiry: int | None
    """Negativ -> bereits abgelaufen. None -> nicht parsbar."""

    in_use: bool


class CertStatusRequest(BaseModel):
    """Optional: Subset von Geraete-IDs. Leer = alle sichtbaren."""

    device_ids: list[str] = Field(default_factory=list)


class CertStatusEntry(BaseModel):
    """Cert-Inventur eines Geraets fuer die Batch-Antwort."""

    device_id: str
    reachable: bool
    authenticated: bool
    summary: str
    checked_at_iso: str
    certs: list[CertEntryResponse]
    soonest_days: int | None
    """Geringste Tagesanzahl bis Ablauf - fuer Kachel-Badge."""


class CertStatusResponse(BaseModel):
    results: list[CertStatusEntry]


# ---------------------------------------------------------------------------
# Config-Drift (v0.7 Safety-Net #5)
# ---------------------------------------------------------------------------


class DriftStatusRequest(BaseModel):
    """Optional Subset von Device-IDs. Leer = alle sichtbaren."""

    device_ids: list[str] = Field(default_factory=list)


class DriftStatusEntry(BaseModel):
    """Drift-Status eines Geraets fuer die Batch-Antwort."""

    device_id: str
    reachable: bool
    authenticated: bool
    summary: str
    checked_at_iso: str
    has_baseline: bool
    """True wenn lokal mindestens ein Backup existiert das wir als Baseline
    vergleichen koennten. False -> Drift nicht ermittelbar."""

    drift_detected: bool | None
    """True/False wenn baseline existiert; None wenn ``has_baseline`` False."""

    baseline_backup_id: str = ""
    baseline_backup_iso: str = ""
    baseline_trigger: str = ""


class DriftStatusResponse(BaseModel):
    results: list[DriftStatusEntry]


# ---------------------------------------------------------------------------
# Config-Compare (v0.7+ Multi-Site-Sync)
# ---------------------------------------------------------------------------


class CompareRequest(BaseModel):
    """N Geraete + Subsystem zum Vergleichen."""

    device_ids: list[str] = Field(..., min_length=2)
    subsystem: str = Field(..., pattern=r"^(aliases|routes|rules|unbound)$")
    """Unterstuetzte Subsysteme: aliases, routes, rules, unbound."""


class CompareCellResponse(BaseModel):
    device_id: str
    status: str  # "present" | "absent" | "unreachable"
    type: str = ""
    content_fingerprint: str = ""
    content_count: int = 0
    description: str = ""
    content: list[str] = Field(default_factory=list)
    """Sortierte Liste der Alias-Eintraege. Wird im Detail-Aufklapp im UI
    angezeigt damit der Admin pruefen kann was synct wird, bevor er klickt."""


class CompareRowResponse(BaseModel):
    name: str
    uniform: bool
    cells: list[CompareCellResponse]


class CompareColumnInfo(BaseModel):
    device_id: str
    device_name: str
    reachable: bool
    summary: str


class CompareResponse(BaseModel):
    subsystem: str
    columns: list[CompareColumnInfo]
    rows: list[CompareRowResponse]
    summary: str
    checked_at_iso: str


class SyncAliasRequest(BaseModel):
    """Sync-Aktion: pull Alias vom Master, pushe Plan zu Targets."""

    master_device_id: str = Field(..., min_length=1)
    target_device_ids: list[str] = Field(..., min_length=1)
    alias_name: str = Field(..., min_length=1)


class SyncAliasResponse(BaseModel):
    plan_id: str
    alias_name: str
    target_count: int
    source_summary: str
    """Kurzfassung was als Master uebernommen wurde."""


class AliasEntryResponse(BaseModel):
    name: str
    type: str
    content: list[str]
    description: str
    content_fingerprint: str


class DeviceAliasesResponse(BaseModel):
    device_id: str
    device_name: str
    reachable: bool
    summary: str
    aliases: list[AliasEntryResponse]
    checked_at_iso: str


class RouteEntryResponse(BaseModel):
    """Einzelne statische Route eines Geraets, sortierbar fuer die UI."""

    network: str
    gateway: str
    descr: str
    disabled: bool


class DeviceRoutesResponse(BaseModel):
    device_id: str
    device_name: str
    reachable: bool
    summary: str
    routes: list[RouteEntryResponse]
    checked_at_iso: str


class UnboundHostEntryResponse(BaseModel):
    """Ein Unbound-Host-Override mit Live-UUID aus searchHostOverride."""

    uuid: str
    enabled: bool
    host: str
    domain: str
    server: str
    description: str


class DeviceUnboundHostsResponse(BaseModel):
    device_id: str
    device_name: str
    reachable: bool
    summary: str
    hosts: list[UnboundHostEntryResponse]
    checked_at_iso: str


class RuleEntryResponse(BaseModel):
    """Eine Firewall-Filter-Regel mit Live-UUID fuer Edit/Delete."""

    uuid: str
    enabled: bool
    action: str
    interface: str
    direction: str
    ipprotocol: str
    protocol: str
    source_net: str
    source_port: str
    source_not: bool
    destination_net: str
    destination_port: str
    destination_not: bool
    gateway: str
    log: bool
    description: str
    sequence: int | None = None


class DeviceRulesResponse(BaseModel):
    device_id: str
    device_name: str
    reachable: bool
    summary: str
    rules: list[RuleEntryResponse]
    checked_at_iso: str


# ---------------------------------------------------------------------------
# Backups (v0.7 Safety-Nets)
# ---------------------------------------------------------------------------


class BackupResponse(BaseModel):
    """Metadaten eines lokal gespeicherten Backups."""

    id: str
    device_id: str
    timestamp_utc: str
    trigger: str
    size_bytes: int
    size_compressed: int
    sha256: str
    related_plan_id: str = ""
    device_name_at_creation: str = ""


class BackupListResponse(BaseModel):
    """Liste der lokal gespeicherten Backups eines Geraets, neueste zuerst."""

    device_id: str
    backups: list[BackupResponse]


# ---------------------------------------------------------------------------
# Plan / Apply
# ---------------------------------------------------------------------------


class RoutePlanRequest(BaseModel):
    """Plan-Erzeugung fuer eine neue statische Route ueber 1..N Geraete."""

    network: str = Field(..., min_length=1, max_length=100)
    gateway: str = Field(..., min_length=1, max_length=120)
    descr: str = Field("", max_length=200)
    disabled: bool = False
    target_device_ids: list[str] = Field(..., min_length=1)


class AliasPlanRequest(BaseModel):
    """Plan-Erzeugung fuer einen Alias (create oder append)."""

    name: str = Field(..., min_length=1, max_length=120)
    type: str = Field(..., min_length=1, max_length=40)
    content: list[str] = Field(..., min_length=1)
    descr: str = Field("", max_length=200)
    merge_mode: str = Field("create", pattern="^(create|append)$")
    target_device_ids: list[str] = Field(..., min_length=1)


class AliasUpdatePlanRequest(BaseModel):
    """Plan-Erzeugung fuer einen Alias-Edit (vorhandenen Alias modifizieren)."""

    name: str = Field(..., min_length=1, max_length=120)
    type: str = Field(..., min_length=1, max_length=40)
    content: list[str] = Field(..., min_length=1)
    descr: str = Field("", max_length=200)
    target_device_ids: list[str] = Field(..., min_length=1)


class AliasDeletePlanRequest(BaseModel):
    """Plan-Erzeugung fuer einen Alias-Delete (vorhandenen Alias entfernen)."""

    name: str = Field(..., min_length=1, max_length=120)
    target_device_ids: list[str] = Field(..., min_length=1)


class RouteUpdatePlanRequest(BaseModel):
    """Plan-Erzeugung fuer einen Route-Edit (vorhandene Route modifizieren).

    Identitaet bleibt (network, gateway); descr und disabled sind editierbar.
    """

    network: str = Field(..., min_length=1, max_length=100)
    gateway: str = Field(..., min_length=1, max_length=120)
    descr: str = Field("", max_length=200)
    disabled: bool = False
    target_device_ids: list[str] = Field(..., min_length=1)


class RouteDeletePlanRequest(BaseModel):
    """Plan-Erzeugung fuer einen Route-Delete (vorhandene Route entfernen)."""

    network: str = Field(..., min_length=1, max_length=100)
    gateway: str = Field(..., min_length=1, max_length=120)
    target_device_ids: list[str] = Field(..., min_length=1)


class _RulePayloadBase(BaseModel):
    """Gemeinsame Felder von Rule-Add und Rule-Update."""

    enabled: bool = True
    action: str = Field("pass", pattern="^(pass|block|reject)$")
    interface: str = Field(..., min_length=1, max_length=80)
    direction: str = Field("in", pattern="^(in|out)$")
    ipprotocol: str = Field("inet", pattern="^(inet|inet6)$")
    protocol: str = Field("any", max_length=20)
    source_net: str = Field("any", max_length=120)
    source_port: str = Field("", max_length=80)
    source_not: bool = False
    destination_net: str = Field("any", max_length=120)
    destination_port: str = Field("", max_length=80)
    destination_not: bool = False
    gateway: str = Field("", max_length=120)
    log: bool = False
    description: str = Field("", max_length=200)
    sequence: int | None = Field(None, ge=1, le=100000)


class RulePlanRequest(_RulePayloadBase):
    """Plan-Erzeugung fuer einen neuen Filter-Regel-Eintrag auf 1..N Geraeten."""

    target_device_ids: list[str] = Field(..., min_length=1)


class RuleUpdatePlanRequest(_RulePayloadBase):
    """Plan-Erzeugung fuer einen Filter-Regel-Edit.

    UUID identifiziert die zu editierende Regel; die UI liest sie aus
    der Live-Liste und schickt sie zurueck.
    """

    uuid: str = Field(..., min_length=1, max_length=64)
    target_device_ids: list[str] = Field(..., min_length=1)


class RuleDeletePlanRequest(BaseModel):
    """Plan-Erzeugung fuer einen Filter-Regel-Delete."""

    uuid: str = Field(..., min_length=1, max_length=64)
    target_device_ids: list[str] = Field(..., min_length=1)


class _UnboundHostPayloadBase(BaseModel):
    enabled: bool = True
    host: str = Field(..., min_length=1, max_length=120)
    domain: str = Field(..., min_length=1, max_length=180)
    server: str = Field(..., min_length=1, max_length=80)
    description: str = Field("", max_length=200)


class UnboundHostPlanRequest(_UnboundHostPayloadBase):
    """Plan-Erzeugung fuer einen neuen Unbound-Host-Override."""

    target_device_ids: list[str] = Field(..., min_length=1)


class UnboundHostUpdatePlanRequest(_UnboundHostPayloadBase):
    """Plan-Erzeugung fuer einen Unbound-Host-Override-Edit.

    Identitaet = (host, domain). Server + Beschreibung + Enabled-Flag
    sind editierbar; host/domain bleiben.
    """

    target_device_ids: list[str] = Field(..., min_length=1)


class UnboundHostDeletePlanRequest(BaseModel):
    """Plan-Erzeugung fuer Unbound-Host-Override-Delete."""

    host: str = Field(..., min_length=1, max_length=120)
    domain: str = Field(..., min_length=1, max_length=180)
    target_device_ids: list[str] = Field(..., min_length=1)


class PlannedActionResponse(BaseModel):
    device_id: str
    device_name: str
    device_host: str
    diff_kind: str
    diff_summary: str
    payload_masked: dict[str, object]


class PlanResponse(BaseModel):
    plan_id: str
    action: str
    subsystem: str
    created_at_utc: str
    target_count: int
    to_apply_count: int
    skip_count: int
    actions: list[PlannedActionResponse]


class PlanSummary(BaseModel):
    plan_id: str
    action: str
    subsystem: str
    created_at_utc: str
    target_count: int


class PlanListResponse(BaseModel):
    plans: list[PlanSummary]


class DeviceResultResponse(BaseModel):
    device_id: str
    device_name: str
    status: str
    short_message: str
    error_kind: str | None = None
    failed_phase: str | None = None
    duration_ms: int


class RolloutReportResponse(BaseModel):
    plan_id: str
    action: str
    subsystem: str
    total: int
    successes: int
    failures: int
    skipped: int
    results: list[DeviceResultResponse]


class ApplyRequest(BaseModel):
    """Optionaler Body fuer Apply: nur eine Untermenge der Geraete ausrollen.

    Wird beim Retry-Pfad genutzt - User schickt die fehlgeschlagenen
    device_ids und der Server wiederholt den Plan nur fuer die.

    ``safety_net`` aktiviert den Cisco-Style-Commit-Confirmed-Pfad:
    nach erfolgreichem Apply hat der User ``window_s`` Zeit zu
    bestaetigen, sonst SSH-Rollback auf Pre-Apply-Backup.
    """

    device_ids: list[str] | None = None
    safety_net: bool = False
    safety_net_window_s: int | None = Field(None, ge=10, le=3600)


class SafetyNetEntryResponse(BaseModel):
    plan_id: str
    device_id: str
    device_name: str
    armed_at_ms: int
    deadline_ms: int
    resolved: bool
    resolution: str
    resolution_summary: str


class SafetyNetStatusResponse(BaseModel):
    entries: list[SafetyNetEntryResponse]


class OutstandingDeviceEntry(BaseModel):
    """Pro Geraet: Anzahl + Liste der Plaene mit ausstehenden Aktionen."""

    device_id: str
    device_name: str
    outstanding_count: int
    plans: list[str]  # Plan-IDs, neueste zuerst


class OutstandingResponse(BaseModel):
    devices: list[OutstandingDeviceEntry]


# ---------------------------------------------------------------------------
# Auto-Retry-Watcher
# ---------------------------------------------------------------------------


class RetryScheduleRequest(BaseModel):
    """Startet einen Auto-Retry fuer einen Plan + Geraete-IDs."""

    plan_id: str = Field(..., min_length=1)
    device_ids: list[str] = Field(..., min_length=1)
    interval_s: int = Field(180, ge=30, le=3600)
    max_duration_s: int = Field(3600, ge=60, le=86400)


class RetryJobResponse(BaseModel):
    plan_id: str
    device_ids: list[str]
    attempts: int
    last_failure_count: int
    started_at_ms: int
    next_attempt_at_ms: int
    paused: bool


class RetryStatusResponse(BaseModel):
    jobs: list[RetryJobResponse]


# ---------------------------------------------------------------------------
# Profile (Templates)
# ---------------------------------------------------------------------------


class ProfileResponse(BaseModel):
    id: str
    name: str
    action: str
    subsystem: str
    default_selector: str
    spec: dict[str, object]


class ProfileListResponse(BaseModel):
    profiles: list[ProfileResponse]


class ProfileCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    action: str = Field(..., min_length=1, max_length=40)
    subsystem: str = Field(..., min_length=1, max_length=40)
    default_selector: str = Field("all", min_length=1, max_length=120)
    spec: dict[str, object]


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


class GatewaySummaryResponse(BaseModel):
    name: str
    address: str
    status: str


class AliasSummaryResponse(BaseModel):
    name: str
    type: str
    descr: str


class GatewayDiscoveryResponse(BaseModel):
    device_id: str
    gateways: list[GatewaySummaryResponse]


class AliasDiscoveryResponse(BaseModel):
    device_id: str
    aliases: list[AliasSummaryResponse]


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


class AuditEntryResponse(BaseModel):
    timestamp_utc: str
    actor: str
    event: str
    summary: str
    action: str | None = None
    target_device_id: str | None = None
    target_device_name: str | None = None
    target_count: int | None = None
    status: str | None = None
    error_kind: str | None = None
    failed_phase: str | None = None
    duration_ms: int | None = None
    vault_path: str | None = None


class AuditListResponse(BaseModel):
    entries: list[AuditEntryResponse]
    total: int
    truncated: bool


# ---------------------------------------------------------------------------
# Fehler
# ---------------------------------------------------------------------------


class ErrorResponse(BaseModel):
    detail: str
    code: str | None = None
