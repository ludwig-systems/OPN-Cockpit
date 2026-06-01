# OPN-Cockpit alle laufenden Prozesse beenden — Pre-Uninstall-Helper.
#
# Wirkt fuer beide Install-Modi:
#   - Single-User:  opn-cockpit.exe / python.exe direkt aus {app}\python\
#   - Multi-User:   NSSM-Service 'OPN-Cockpit'
#
# Wird vom Inno-Setup-Uninstall AUSSER der Service-Komponente UNVERZICHTBAR
# aufgerufen, damit Datei-Locks (DLLs, .pyd, sqlite-DBs) freigegeben werden
# und die Deinstallation alle Files entfernen kann.
#
# ASCII-only fuer PowerShell-5.1-CP-1252-Kompatibilitaet.

[CmdletBinding()]
param(
    [string]$InstallDir,
    [string]$ServiceName = "OPN-Cockpit",
    [int]$StopTimeoutSec = 15
)

# Defensive Execution-Policy
try {
    Set-ExecutionPolicy -ExecutionPolicy Bypass -Scope Process -Force -ErrorAction Stop
} catch {}

# ErrorAction Continue — Uninstall darf nicht abbrechen wenn ein Schritt
# scheitert, sonst bleibt der Installer haengen.
$ErrorActionPreference = "Continue"

if (-not $InstallDir -or $InstallDir.Length -eq 0) {
    $InstallDir = Split-Path -Parent $PSScriptRoot
}

Write-Host "[opn-cockpit] Pre-Uninstall: Prozesse beenden..."

# ---- Schritt 1: Service-Mode sauber stoppen ----
$svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($svc) {
    Write-Host ("  Dienst '" + $ServiceName + "' (Status: " + $svc.Status + ") stoppen...")
    try {
        Stop-Service -Name $ServiceName -Force -ErrorAction Stop
        # Warten bis wirklich gestoppt
        $sw = [System.Diagnostics.Stopwatch]::StartNew()
        while ($sw.Elapsed.TotalSeconds -lt $StopTimeoutSec) {
            $svc.Refresh()
            if ($svc.Status -eq 'Stopped') { break }
            Start-Sleep -Milliseconds 500
        }
        Write-Host ("  Dienst-Status nach Stop: " + $svc.Status)
    } catch {
        Write-Warning ("  Stop-Service fehlgeschlagen: " + $_.Exception.Message)
    }
} else {
    Write-Host "  Kein Dienst gefunden (Single-User-Mode oder schon entfernt)."
}

# ---- Schritt 2: Single-User-Prozesse killen ----
# Sucht alle Prozesse mit Image-Pfad unter {app}\python\ und beendet sie.
# Image-Vergleich case-insensitive, vollstaendigen Pfad pruefen.
$pythonDir = Join-Path $InstallDir "python"
$pythonDirNorm = $pythonDir.TrimEnd('\').ToLowerInvariant()

try {
    $procs = Get-Process -ErrorAction SilentlyContinue | Where-Object {
        $_.Path -ne $null -and
        $_.Path.ToLowerInvariant().StartsWith($pythonDirNorm)
    }
} catch {
    $procs = @()
}

if ($procs.Count -gt 0) {
    Write-Host ("  " + $procs.Count + " Prozess(e) aus '" + $pythonDir + "' beenden...")
    foreach ($p in $procs) {
        Write-Host ("    PID " + $p.Id + " : " + $p.ProcessName + " -> " + $p.Path)
        try {
            $p.Kill()
            $p.WaitForExit(5000) | Out-Null
        } catch {
            Write-Warning ("    Konnte PID " + $p.Id + " nicht beenden: " + $_.Exception.Message)
        }
    }
} else {
    Write-Host "  Keine offenen Single-User-Prozesse."
}

# Sicherheits-Puffer damit Filesystem die Locks freigibt
Start-Sleep -Seconds 1

Write-Host "[opn-cockpit] Pre-Uninstall fertig."
exit 0
