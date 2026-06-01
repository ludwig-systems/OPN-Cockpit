# OPN-Cockpit als Windows-Dienst installieren (v3.2+).
#
# Wraps den Web-Server mit NSSM (Non-Sucking Service Manager). NSSM ist
# Public Domain (https://nssm.cc/) und wird vom Installer als nssm.exe
# in {app}\bundle\ abgelegt.
#
# Aufruf via Installer (automatisch beim Setup im Service-Mode):
#   powershell.exe -ExecutionPolicy Bypass -File scripts\install-service.ps1
#
# Manueller Aufruf (Admin-PowerShell, im Installations-Verzeichnis):
#   .\scripts\install-service.ps1
#
# Service-Eigenschaften:
#   - Name:        OPN-Cockpit
#   - Display:     OPN-Cockpit Multi-Site-Management
#   - Startup:     Automatic
#   - ObjectName:  NT AUTHORITY\LocalService
#   - Bindung:     0.0.0.0:9876 (per Env)
#   - Logs:        %ProgramData%\OPN-Cockpit\logs\
#   - Token-File:  %ProgramData%\OPN-Cockpit\BOOTSTRAP-TOKEN.txt
#
# WICHTIG (PS 5.1 + CP-1252): KEINE Em-Dashes oder andere Non-ASCII-Zeichen
# in diesem Skript verwenden. PS 5.1 liest die Datei sonst als CP-1252 und
# wirft Parser-Fehler. Beobachtet am 2026-06-01 im Multi-User-Install.

[CmdletBinding()]
param(
    [string]$InstallDir,
    [string]$ServiceName = "OPN-Cockpit",
    [string]$DisplayName = "OPN-Cockpit Multi-Site-Management",
    [string]$LogDir = (Join-Path $env:ProgramData "OPN-Cockpit\logs")
)

# Defensive Execution-Policy fuer diese Session - wirkt auch wenn Group
# Policy das -ExecutionPolicy Bypass-CLI-Flag des Aufrufers ueberstimmt.
# Bei manchen Firmen-Maschinen scheitert der Installer-Aufruf sonst stumm
# und der Service wird nie registriert.
try {
    Set-ExecutionPolicy -ExecutionPolicy Bypass -Scope Process -Force -ErrorAction Stop
} catch {
    Write-Warning ("Execution-Policy konnte nicht auf Bypass gesetzt werden: " +
                   $_.Exception.Message)
}

$ErrorActionPreference = "Stop"

# InstallDir defaultet auf das Eltern-Verzeichnis von $PSScriptRoot.
# Bewusst KEIN Pipeline-Default im param() - PS 5.1 parst das gerne mal
# als unvollstaendigen Ausdruck.
if (-not $InstallDir -or $InstallDir.Length -eq 0) {
    $InstallDir = Split-Path -Parent $PSScriptRoot
}

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
    Write-Error ("NSSM nicht gefunden: " + $nssm +
                 ". Erwarte nssm.exe im bundle\-Verzeichnis (Public-Domain-Binary von nssm.cc).")
    exit 1
}

$bundlePython = Join-Path $InstallDir "python\python.exe"
$devPython    = Join-Path $InstallDir ".venv\Scripts\python.exe"
if (Test-Path $bundlePython) {
    $python = $bundlePython
    Write-Host ("Verwende Embedded-Python-Bundle: " + $python)
} elseif (Test-Path $devPython) {
    $python = $devPython
    Write-Host ("Verwende Dev-venv: " + $python)
} else {
    Write-Error ("Kein Python gefunden - weder Bundle (" + $bundlePython +
                 ") noch venv (" + $devPython + "). Installation defekt.")
    exit 1
}

# Daten-Verzeichnis vorbereiten - der LocalService-Account braucht hier
# Schreibrechte fuer Tresor, Audit, Logs, Bootstrap-Token-File.
$dataDir = Join-Path $env:ProgramData "OPN-Cockpit"
if (-not (Test-Path $dataDir)) {
    New-Item -ItemType Directory -Path $dataDir -Force | Out-Null
}
if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
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

# Wenn der Dienst schon existiert: erst sauber entfernen.
$existing = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host ("Vorhandenen Dienst '" + $ServiceName + "' wird entfernt...")
    & $nssm stop $ServiceName confirm | Out-Null
    & $nssm remove $ServiceName confirm | Out-Null
    Start-Sleep -Seconds 2
}

Write-Host ("Installiere Dienst '" + $ServiceName + "'...")

& $nssm install $ServiceName $python "-m" "opn_cockpit"
& $nssm set $ServiceName AppDirectory $InstallDir
& $nssm set $ServiceName DisplayName $DisplayName
& $nssm set $ServiceName Description "OPN-Cockpit Multi-Site-Management fuer OPNsense (Web-Server, Port 9876)"
& $nssm set $ServiceName Start SERVICE_AUTO_START
& $nssm set $ServiceName ObjectName "NT AUTHORITY\LocalService"

# Env-Variablen - Multi-User-Server ist der Default fuer Service-Setups.
# Beim ersten Setup-Wizard-Lauf wird der Tresor automatisch unter
# OPNCOCKPIT_VAULT_PATH angelegt, falls noch keiner da ist.
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

# Log-Routing
& $nssm set $ServiceName AppStdout (Join-Path $LogDir "stdout.log")
& $nssm set $ServiceName AppStderr (Join-Path $LogDir "stderr.log")
& $nssm set $ServiceName AppRotateFiles 1
& $nssm set $ServiceName AppRotateBytes 5242880   # 5 MiB
& $nssm set $ServiceName AppRotateOnline 1

Write-Host "Starte Dienst..."
Start-Service -Name $ServiceName

Start-Sleep -Seconds 4
$state = (Get-Service -Name $ServiceName).Status
Write-Host ("Dienst-Status: " + $state)

if ($state -ne 'Running') {
    Write-Warning ("Dienst ist nicht gestartet. Pruefe Logs in " + $LogDir + ".")
    exit 2
}

# Default-Admin-Konto (seit F28: kein Bootstrap-Token mehr, stattdessen
# legt der Server beim Erststart 'admin' mit Default-PW 'OPN-Cockpit!' an
# und verlangt einen sofortigen PW-Wechsel beim ersten Login).
$DefaultAdminUser = "admin"
$DefaultAdminPw   = "OPN-Cockpit!"

# Alte Token-Datei aus vorherigen Installationen weglaeumen, damit User
# nicht denkt der Token wuerde noch gelten.
$oldTokenFile = Join-Path $dataDir "BOOTSTRAP-TOKEN.txt"
if (Test-Path $oldTokenFile) {
    Remove-Item -Force $oldTokenFile -EA SilentlyContinue
}

Write-Host ""
Write-Host "===================================================="
Write-Host "  OPN-Cockpit laeuft jetzt als Windows-Dienst."
Write-Host "  Browser:  http://localhost:9876"
Write-Host "  Logs:     $LogDir"
Write-Host ""
Write-Host "  Default-Admin (beim Erst-Login Pflicht-PW-Wechsel):"
Write-Host ("    Benutzer:  " + $DefaultAdminUser)
Write-Host ("    Passwort:  " + $DefaultAdminPw)
Write-Host ""
Write-Host "  Stoppen:  Stop-Service -Name $ServiceName"
Write-Host "  Starten:  Start-Service -Name $ServiceName"
Write-Host "===================================================="

# Bewusst kein WinForms-Popup mehr (Runde 6 Test-Feedback): Default-User
# und -Passwort stehen ohnehin im Setup-Wizard direkt sichtbar. Konsolen-
# Ausgabe oben reicht — der Inno-Setup-Postinstall-Step oeffnet den Browser
# automatisch auf http://localhost:9876.
