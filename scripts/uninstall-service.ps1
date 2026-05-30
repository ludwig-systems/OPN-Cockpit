# OPN-Cockpit Windows-Dienst entfernen (v3.2).
#
# Stoppt und entfernt den NSSM-Service. Belaesst:
#   - venv (wird vom Inno-Installer mit-deinstalliert)
#   - Daten in %APPDATA%\OPN-Cockpit (Vault, Audit, Settings)
#   - Logs in %ProgramData%\OPN-Cockpit\logs
#
# Aufruf:
#   .\scripts\uninstall-service.ps1

[CmdletBinding()]
param(
    [string]$InstallDir = $PSScriptRoot | Split-Path -Parent,
    [string]$ServiceName = "OPN-Cockpit"
)

$ErrorActionPreference = "Stop"

function Test-Admin {
    $currentUser = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($currentUser)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not (Test-Admin)) {
    Write-Error "Dieses Skript muss als Administrator laufen."
    exit 1
}

$existing = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if (-not $existing) {
    Write-Host "Dienst '$ServiceName' existiert nicht. Nichts zu tun."
    exit 0
}

$nssm = Join-Path $InstallDir "bundle\nssm.exe"
if (-not (Test-Path $nssm)) {
    Write-Warning "NSSM nicht gefunden: $nssm. Verwende sc.exe als Fallback."
    sc.exe stop $ServiceName | Out-Null
    Start-Sleep -Seconds 2
    sc.exe delete $ServiceName | Out-Null
} else {
    & $nssm stop $ServiceName confirm
    Start-Sleep -Seconds 2
    & $nssm remove $ServiceName confirm
}

Write-Host "Dienst '$ServiceName' entfernt."
