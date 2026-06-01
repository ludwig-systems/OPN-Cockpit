# OPN-Cockpit Windows-Dienst entfernen.
#
# Stoppt + entfernt den NSSM-Service. Belaesst:
#   - Daten in %ProgramData%\OPN-Cockpit (Vault, Audit, Settings, Logs)
#
# WICHTIG: stop-processes.ps1 sollte VOR diesem Skript laufen, um die
# laufenden Python-Prozesse sauber zu beenden. Inno-Setup macht das im
# [UninstallRun]-Block in dieser Reihenfolge.
#
# ASCII-only fuer PowerShell-5.1-CP-1252-Kompatibilitaet.

[CmdletBinding()]
param(
    [string]$InstallDir,
    [string]$ServiceName = "OPN-Cockpit"
)

# Defensive Execution-Policy fuer GP-restriktive Maschinen
try {
    Set-ExecutionPolicy -ExecutionPolicy Bypass -Scope Process -Force -ErrorAction Stop
} catch {}

$ErrorActionPreference = "Continue"

if (-not $InstallDir -or $InstallDir.Length -eq 0) {
    $InstallDir = Split-Path -Parent $PSScriptRoot
}

function Test-Admin {
    $currentUser = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($currentUser)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not (Test-Admin)) {
    Write-Warning "Dieses Skript muss als Administrator laufen."
    exit 0  # Inno-Uninstall nicht abbrechen
}

$existing = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if (-not $existing) {
    Write-Host ("Dienst '" + $ServiceName + "' existiert nicht. Nichts zu tun.")
    exit 0
}

$nssm = Join-Path $InstallDir "bundle\nssm.exe"
if (-not (Test-Path $nssm)) {
    Write-Warning ("NSSM nicht gefunden: " + $nssm + ". Verwende sc.exe als Fallback.")
    & sc.exe stop $ServiceName | Out-Null
    Start-Sleep -Seconds 3
    & sc.exe delete $ServiceName | Out-Null
} else {
    & $nssm stop $ServiceName confirm | Out-Null
    Start-Sleep -Seconds 3
    & $nssm remove $ServiceName confirm | Out-Null
}

Write-Host ("Dienst '" + $ServiceName + "' entfernt.")
exit 0
