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
# Fehler
# ---------------------------------------------------------------------------


class ErrorResponse(BaseModel):
    detail: str
    code: str | None = None
