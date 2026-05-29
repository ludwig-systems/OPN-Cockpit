# OPN-Cockpit Starter (Rechtsklick "Mit PowerShell ausführen").
#
# Erwartet, dass scripts\setup-venv.ps1 einmal gelaufen ist.

$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $VenvPython)) {
    Write-Error "Setup unvollstaendig: $VenvPython fehlt. Bitte erst einmalig scripts\setup-venv.ps1 ausfuehren."
    exit 1
}

& $VenvPython -m opn_cockpit
