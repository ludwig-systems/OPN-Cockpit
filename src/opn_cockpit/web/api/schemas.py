"""Pydantic-Schemas fuer alle API-Routen.

Ein zentrales Modul, damit Frontend (TypeScript-Generierung spaeter
moeglich) und Tests eine Single Source of Truth haben.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

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


class VaultListResponse(BaseModel):
    vaults: list[VaultEntry]
    suggested_new_path: str


class CreateVaultRequest(BaseModel):
    path: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)


class CreateVaultResponse(BaseModel):
    path: str
    filename: str
    token: str  # auto-unlock nach Anlegen
    inactivity_timeout_s: int
    seconds_until_expiry: int


# ---------------------------------------------------------------------------
# Inventar
# ---------------------------------------------------------------------------


class DeviceResponse(BaseModel):
    """Read-only Geraete-Sicht fuer das Frontend.

    NIEMALS API-Key/Secret durchreichen. Der Frontend-Client soll keine
    Klartext-Credentials sehen.
    """

    id: str
    name: str
    host: str
    port: int
    tls_verify: bool
    tags: list[str]
    descr: str


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
    """

    device_ids: list[str] | None = None


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
