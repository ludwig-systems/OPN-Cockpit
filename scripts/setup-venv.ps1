#requires -Version 5.1
<#
.SYNOPSIS
    Richtet eine isolierte venv für OPN-Cockpit ein und installiert Runtime- und Dev-Dependencies.

.DESCRIPTION
    Voraussetzung: Python 3.11+ und 'uv' (https://docs.astral.sh/uv/) installiert.
    Das Skript erzeugt .venv im Repo-Root, installiert das Paket editierbar samt Dev-Extras
    und führt zur Sanity-Prüfung 'pytest -q' aus.

.NOTES
    Aus dem Repo-Root ausführen: .\scripts\setup-venv.ps1
#>

[CmdletBinding()]
param(
    [switch]$SkipTests
)

$ErrorActionPreference = 'Stop'

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

Write-Host "==> Repo-Root: $RepoRoot" -ForegroundColor Cyan

# --- Voraussetzungen prüfen --------------------------------------------------

$uv = Get-Command uv -ErrorAction SilentlyContinue
if ($null -eq $uv) {
    Write-Host ""
    Write-Host "'uv' wurde nicht gefunden." -ForegroundColor Yellow
    Write-Host ""
    Write-Host "Installation (einmalig pro Maschine):" -ForegroundColor Yellow
    Write-Host "  powershell -ExecutionPolicy ByPass -c ""irm https://astral.sh/uv/install.ps1 | iex""" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "Anschliessend dieses Skript erneut ausfuehren." -ForegroundColor Yellow
    exit 1
}

$python = Get-Command python -ErrorAction SilentlyContinue
if ($null -eq $python) {
    Write-Error "Python ist nicht im PATH gefunden."
    exit 1
}

$versionRaw = (& python --version) 2>$null
Write-Host "==> Gefunden: $versionRaw" -ForegroundColor Cyan

# --- venv anlegen ------------------------------------------------------------

if (Test-Path .\.venv) {
    Write-Host "==> .venv existiert bereits — überspringe Anlage." -ForegroundColor Yellow
} else {
    Write-Host "==> Lege .venv mit uv an ..." -ForegroundColor Cyan
    uv venv
    if ($LASTEXITCODE -ne 0) { throw "uv venv ist fehlgeschlagen." }
}

# --- Dependencies installieren ----------------------------------------------

Write-Host "==> Installiere opn-cockpit editierbar inkl. [dev] ..." -ForegroundColor Cyan
uv pip install -e ".[dev]"
if ($LASTEXITCODE -ne 0) { throw "uv pip install ist fehlgeschlagen." }

# --- Sanity-Check ------------------------------------------------------------

if ($SkipTests) {
    Write-Host "==> Tests übersprungen (--SkipTests). Setup fertig." -ForegroundColor Green
    exit 0
}

Write-Host "==> Sanity-Check: pytest -q" -ForegroundColor Cyan
& .\.venv\Scripts\python.exe -m pytest -q
if ($LASTEXITCODE -ne 0) {
    Write-Warning "pytest meldet Fehler. Setup ist installiert, aber die Test-Suite ist nicht grün."
    exit $LASTEXITCODE
}

Write-Host ""
Write-Host "==> Fertig. venv unter .\.venv aktivierbar via:" -ForegroundColor Green
Write-Host "    .\.venv\Scripts\Activate.ps1" -ForegroundColor Green
