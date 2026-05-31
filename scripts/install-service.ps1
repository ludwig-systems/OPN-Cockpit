# OPN-Cockpit als Windows-Dienst installieren (v3.2).
#
# Wraps den Web-Server mit NSSM (Non-Sucking Service Manager). NSSM ist
# Public Domain (https://nssm.cc/) und wird vom Installer als nssm.exe
# neben start.bat abgelegt.
#
# Aufruf via Installer:
#   powershell.exe -ExecutionPolicy Bypass -File scripts\install-service.ps1
#
# Manueller Aufruf (Admin-PowerShell, im Installations-Verzeichnis):
#   .\scripts\install-service.ps1
#
# Service-Eigenschaften:
#   - Name: OPN-Cockpit
#   - Display-Name: OPN-Cockpit Multi-Site-Management
#   - Startup: Automatic
#   - Working-Dir: das Installations-Verzeichnis
#   - Stdout/Stderr-Log: %ProgramData%\OPN-Cockpit\logs\

[CmdletBinding()]
param(
    [string]$InstallDir = $PSScriptRoot | Split-Path -Parent,
    [string]$ServiceName = "OPN-Cockpit",
    [string]$DisplayName = "OPN-Cockpit Multi-Site-Management",
    [string]$LogDir = (Join-Path $env:ProgramData "OPN-Cockpit\logs")
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

$nssm = Join-Path $InstallDir "bundle\nssm.exe"
if (-not (Test-Path $nssm)) {
    Write-Error "NSSM nicht gefunden: $nssm. Erwarte nssm.exe im bundle\-Verzeichnis."
    exit 1
}

$bundlePython = Join-Path $InstallDir "python\python.exe"
$devPython    = Join-Path $InstallDir ".venv\Scripts\python.exe"
if (Test-Path $bundlePython) {
    $python = $bundlePython
    Write-Host "Verwende Embedded-Python-Bundle: $python"
} elseif (Test-Path $devPython) {
    $python = $devPython
    Write-Host "Verwende Dev-venv: $python"
} else {
    Write-Error ("Kein Python gefunden — weder Bundle ($bundlePython) " +
                 "noch venv ($devPython). Installation defekt.")
    exit 1
}

# Log-Verzeichnis vorbereiten
if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
}

# Falls Service schon existiert: erst entfernen.
$existing = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "Vorhandenen Dienst '$ServiceName' wird entfernt..."
    & $nssm stop $ServiceName confirm
    & $nssm remove $ServiceName confirm
    Start-Sleep -Seconds 2
}

Write-Host "Installiere Dienst '$ServiceName'..."

& $nssm install $ServiceName $python "-m" "opn_cockpit"
& $nssm set $ServiceName AppDirectory $InstallDir
& $nssm set $ServiceName DisplayName $DisplayName
& $nssm set $ServiceName Description "OPN-Cockpit Multi-Site-Management fuer OPNsense (Web-Server, Port 9876)"
& $nssm set $ServiceName Start SERVICE_AUTO_START
& $nssm set $ServiceName ObjectName "NT AUTHORITY\LocalService"

# Env-Variablen — Multi-User-Server ist der Default fuer Service-Setups.
# Beim ersten Setup-Wizard-Lauf wird der Tresor automatisch unter
# OPNCOCKPIT_VAULT_PATH angelegt, falls noch keiner da ist.
$dataDir = Join-Path $env:ProgramData "OPN-Cockpit"
$vaultPath = Join-Path $dataDir "firewalls.opnvault"
$envBlock = @(
    "OPNCOCKPIT_HOST=0.0.0.0",
    "OPNCOCKPIT_PORT=9876",
    "OPNCOCKPIT_NO_BROWSER=1",
    "OPNCOCKPIT_DATA_DIR=$dataDir",
    "OPNCOCKPIT_AUTH_BACKEND=user-db",
    "OPNCOCKPIT_DEPLOYMENT_MODE=multi-server",
    "OPNCOCKPIT_VAULT_PATH=$vaultPath",
    "OPNCOCKPIT_VAULT_DIR=$dataDir",
    "OPNCOCKPIT_STORAGE_BACKEND=sqlite"
) -join "`r`n"
& $nssm set $ServiceName AppEnvironmentExtra $envBlock

# Daten-Verzeichnis vorbereiten — der LocalService-Account hat hier
# Schreibrechte (System-Default-Permission auf %ProgramData%-Unterordner).
if (-not (Test-Path $dataDir)) {
    New-Item -ItemType Directory -Path $dataDir -Force | Out-Null
}
$acl = Get-Acl $dataDir
$rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
    "NT AUTHORITY\LocalService",
    "Modify",
    "ContainerInherit,ObjectInherit",
    "None",
    "Allow")
$acl.SetAccessRule($rule)
Set-Acl -Path $dataDir -AclObject $acl

# Log-Routing
& $nssm set $ServiceName AppStdout (Join-Path $LogDir "stdout.log")
& $nssm set $ServiceName AppStderr (Join-Path $LogDir "stderr.log")
& $nssm set $ServiceName AppRotateFiles 1
& $nssm set $ServiceName AppRotateBytes 5242880  # 5 MiB
& $nssm set $ServiceName AppRotateOnline 1

Write-Host "Starte Dienst..."
Start-Service -Name $ServiceName

Start-Sleep -Seconds 3
$state = (Get-Service -Name $ServiceName).Status
Write-Host "Dienst-Status: $state"

if ($state -ne 'Running') {
    Write-Warning "Dienst ist nicht gestartet. Pruefe Logs in $LogDir."
    exit 2
}

Write-Host ""
Write-Host "===================================================="
Write-Host "  OPN-Cockpit laeuft jetzt als Windows-Dienst."
Write-Host "  Browser:  http://localhost:9876"
Write-Host "  Logs:     $LogDir"
Write-Host "  Stoppen:  Stop-Service -Name $ServiceName"
Write-Host "  Starten:  Start-Service -Name $ServiceName"
Write-Host "===================================================="
