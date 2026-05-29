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


class DeviceCreateRequest(BaseModel):
    """Anlegen eines Geraets im Tresor.

    ``master_password`` wird benoetigt, damit die Aenderung verschluesselt
    auf Platte landet — Spiegelung des CLI/GUI-Verhaltens. Auch nach dem
    Entsperren halten wir das Passwort nie im Server-Speicher.
    """

    name: str = Field(..., min_length=1, max_length=120)
    host: str = Field(..., min_length=1, max_length=255)
    port: int = Field(443, ge=1, le=65535)
    tls_verify: bool = True
    tags: list[str] = Field(default_factory=list)
    descr: str = Field("", max_length=500)
    api_key: str = Field(..., min_length=1, max_length=255)
    api_secret: str = Field(..., min_length=1, max_length=500)
    master_password: str = Field(..., min_length=1)


class DeviceDeleteRequest(BaseModel):
    """Loeschen eines Geraets — Passwort separat (DELETE-Body, kein Header)."""

    master_password: str = Field(..., min_length=1)


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
# Fehler
# ---------------------------------------------------------------------------


class ErrorResponse(BaseModel):
    detail: str
    code: str | None = None
