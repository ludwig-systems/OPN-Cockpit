# OPN-Cockpit Pre-Uninstall-Aufraeumer.
#
# Macht ALLES was vor dem Loeschen der Files passieren muss, damit die
# Deinstallation keine gelockten Dateien zuruecklaesst:
#
#   1. NSSM-Dienst stoppen (oder sc.exe-Fallback)
#   2. NSSM-Dienst ENTFERNEN (sonst bleibt nssm.exe als Service-Host
#      gelockt und das ganze bundle\-Verzeichnis kann nicht weg)
#   3. Hardes Killen aller Prozesse die noch unter {app}\ laufen
#   4. taskkill-Fallback per Image-Name (opn-cockpit.exe, opn-cockpitw.exe, nssm.exe)
#   5. Wartezeit damit Windows die File-Handles wirklich freigibt
#
# Wird vom Inno-Setup-Uninstall UNCONDITIONAL aufgerufen - kein
# Components-Filter. Wenn kein Service existiert (Single-User-Install),
# sind die Service-Schritte einfach No-Ops.
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

# Continue: kein Schritt darf den Uninstall blockieren - wir wollen am
# Ende moeglichst viel weg haben, lieber Best-Effort als Komplett-Stop.
$ErrorActionPreference = "Continue"

if (-not $InstallDir -or $InstallDir.Length -eq 0) {
    $InstallDir = Split-Path -Parent $PSScriptRoot
}

Write-Host "[opn-cockpit] Pre-Uninstall: Service + Prozesse aufraeumen..."
Write-Host ("  InstallDir: " + $InstallDir)

# ---- Schritt 1: Dienst stoppen ----
$svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
$serviceExisted = $false
if ($svc) {
    $serviceExisted = $true
    Write-Host ("  Dienst '" + $ServiceName + "' (Status: " + $svc.Status + ") stoppen...")
    if ($svc.Status -ne 'Stopped') {
        try {
            Stop-Service -Name $ServiceName -Force -ErrorAction Stop
        } catch {
            Write-Warning ("  Stop-Service fehlgeschlagen: " + $_.Exception.Message)
            # Fallback ueber sc.exe
            & sc.exe stop $ServiceName | Out-Null
        }
        # Warten bis wirklich gestoppt
        $sw = [System.Diagnostics.Stopwatch]::StartNew()
        while ($sw.Elapsed.TotalSeconds -lt $StopTimeoutSec) {
            $svc.Refresh()
            if ($svc.Status -eq 'Stopped') { break }
            Start-Sleep -Milliseconds 500
        }
        Write-Host ("  Dienst-Status nach Stop: " + $svc.Status)
    }
} else {
    Write-Host "  Kein Dienst gefunden (Single-User-Mode oder schon entfernt)."
}

# ---- Schritt 2: Dienst ENTFERNEN ----
# Das war der bisher fehlende Schritt: ohne 'nssm remove' bleibt der
# Service-Control-Manager-Eintrag bestehen, und solange der existiert
# haelt SCM eine Referenz auf nssm.exe -> bundle\nssm.exe ist gelockt.
if ($serviceExisted) {
    $nssm = Join-Path $InstallDir "bundle\nssm.exe"
    if (Test-Path $nssm) {
        Write-Host ("  NSSM-Dienst entfernen via " + $nssm + "...")
        try {
            & $nssm remove $ServiceName confirm | Out-Null
        } catch {
            Write-Warning ("  nssm remove fehlgeschlagen: " + $_.Exception.Message)
        }
        Start-Sleep -Seconds 1
    }
    # sc.exe delete IMMER zusaetzlich versuchen (Idempotent) - haelt auch
    # dann den SCM-Eintrag los wenn nssm.exe selbst nicht mehr da ist
    # oder die NSSM-Variante still gescheitert ist.
    Write-Host "  sc.exe delete als Sicherheits-Fallback..."
    & sc.exe delete $ServiceName | Out-Null
    Start-Sleep -Seconds 1

    # Verifikation
    $stillThere = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if ($stillThere) {
        Write-Warning ("  Dienst '" + $ServiceName + "' ist immer noch registriert. " +
                       "SCM-Konsole evtl. offen - bitte Computer neustarten + manuell 'sc delete " +
                       $ServiceName + "' ausfuehren.")
    } else {
        Write-Host "  Dienst entfernt."
    }
}

# ---- Schritt 3: Prozesse killen (per Pfad) ----
# Sucht alle Prozesse mit Image-Pfad unter {app}\ und beendet sie.
$installNorm = $InstallDir.TrimEnd('\').ToLowerInvariant()

try {
    $procs = Get-Process -ErrorAction SilentlyContinue | Where-Object {
        $_.Path -ne $null -and
        $_.Path.ToLowerInvariant().StartsWith($installNorm)
    }
} catch {
    $procs = @()
}

if ($procs.Count -gt 0) {
    Write-Host ("  " + $procs.Count + " Prozess(e) unter '" + $InstallDir + "' beenden...")
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
    Write-Host "  Keine Prozesse mehr mit Image-Pfad unter '$InstallDir'."
}

# ---- Schritt 4: taskkill-Fallback per Image-Name ----
# Get-Process.Path kann fuer manche System/Service-Prozesse null sein
# (Zugriff verweigert). taskkill /F /IM faengt das ab.
foreach ($imgName in @("opn-cockpit.exe", "opn-cockpitw.exe", "nssm.exe")) {
    # 2>$null + Out-Null: taskkill schreibt "kein Prozess gefunden" als
    # Error-Output, das ist hier ok und nicht der Rede wert.
    & taskkill.exe /F /IM $imgName 2>$null | Out-Null
}

# ---- Schritt 5: Lock-Release-Wartezeit ----
# Windows braucht nach Process-Exit ein paar Sekunden bis die DLL-Handles
# vom System wirklich losgelassen sind. Lieber 3 Sekunden zu viel als
# einen gelockten python311.dll-Rest.
Start-Sleep -Seconds 3

Write-Host "[opn-cockpit] Pre-Uninstall fertig."
exit 0
