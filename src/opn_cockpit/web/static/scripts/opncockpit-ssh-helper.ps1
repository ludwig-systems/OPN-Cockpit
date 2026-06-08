# =============================================================================
#  OPN-Cockpit  -  SSH-Key-Helper (Windows / PowerShell)
# =============================================================================
#
#  Erzeugt ein Ed25519-Key-Paar fuer das Safety-Net-Feature,
#  oeffnet beide Keys in Notepad (zum einfachen Copy-Paste),
#  legt den Public-Key direkt in die Windows-Zwischenablage
#  und zeigt eine kurze Anleitung wohin welcher Key gehoert.
#
#  Verwendung:
#    1. Datei aus dem OPN-Cockpit-SSH-Anleitung-Modal herunterladen
#    2. Rechtsklick -> "Mit PowerShell ausfuehren"
#       (falls Signatur-Block: PowerShell oeffnen, dann
#        Set-ExecutionPolicy -Scope Process Bypass; .\opncockpit-ssh-helper.ps1)
#    3. Den Notepad-Anweisungen folgen
#
#  Voraussetzung: ssh-keygen (Windows 10/11 ab 1803 hat OpenSSH-Client default).
#
# =============================================================================

$ErrorActionPreference = 'Stop'

# ---------- Vorpruefung -------------------------------------------------------

if (-not (Get-Command ssh-keygen -ErrorAction SilentlyContinue)) {
    Write-Host ""
    Write-Host "  FEHLER: ssh-keygen nicht gefunden." -ForegroundColor Red
    Write-Host ""
    Write-Host "  Installiere den OpenSSH-Client:" -ForegroundColor Yellow
    Write-Host "    Einstellungen -> Apps -> Optionale Features -> "
    Write-Host "    'OpenSSH-Client' hinzufuegen"
    Write-Host ""
    Read-Host "  Mit Enter beenden"
    exit 1
}

# ---------- Arbeitsverzeichnis -----------------------------------------------

$workDir = Join-Path $env:USERPROFILE "Documents\OPN-Cockpit-SSH"
New-Item -ItemType Directory -Path $workDir -Force | Out-Null

$stamp   = Get-Date -Format "yyyyMMdd-HHmmss"
$comment = "opncockpit-safetynet-$($env:COMPUTERNAME)-$stamp"
$keyPath = Join-Path $workDir "opncockpit-safetynet-$stamp"

if (Test-Path $keyPath) {
    Write-Host "  Datei existiert schon: $keyPath" -ForegroundColor Red
    Read-Host "  Mit Enter beenden"
    exit 1
}

# ---------- Key erzeugen -----------------------------------------------------

Write-Host ""
Write-Host "=================================================================" -ForegroundColor Cyan
Write-Host "  OPN-Cockpit  Safety-Net  SSH-Key-Helper" -ForegroundColor Cyan
Write-Host "=================================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Erzeuge Ed25519-Key-Paar..." -ForegroundColor White
Write-Host "  Verzeichnis: $workDir"
Write-Host ""

# -N "" = leere Passphrase (Cockpit kann keine Passphrase-Keys)
# -q    = silent
& ssh-keygen -t ed25519 -f $keyPath -C $comment -N '""' -q
if ($LASTEXITCODE -ne 0) {
    Write-Host "  FEHLER: ssh-keygen ist mit Exit-Code $LASTEXITCODE gescheitert." -ForegroundColor Red
    Read-Host "  Mit Enter beenden"
    exit 1
}

# ---------- Public-Key in Zwischenablage -------------------------------------

$pubKey = Get-Content "$keyPath.pub" -Raw
Set-Clipboard -Value $pubKey

# ---------- Notepad-Fenster oeffnen ------------------------------------------

Write-Host "  Oeffne zwei Notepad-Fenster:" -ForegroundColor White
Write-Host "    1. PUBLIC -> OPNsense"
Write-Host "    2. PRIVATE -> OPN-Cockpit"
Write-Host ""

Start-Process notepad.exe -ArgumentList "`"$keyPath.pub`""
Start-Sleep -Milliseconds 400
Start-Process notepad.exe -ArgumentList "`"$keyPath`""

# ---------- Anleitung --------------------------------------------------------

Write-Host "=================================================================" -ForegroundColor Yellow
Write-Host "  SCHRITT 1  -  Public-Key -> OPNsense" -ForegroundColor Yellow
Write-Host "=================================================================" -ForegroundColor Yellow
Write-Host ""
Write-Host "  Im ersten Notepad steht der Public-Key (eine Zeile,"
Write-Host "  faengt mit 'ssh-ed25519 ...' an)."
Write-Host ""
Write-Host "  Er ist BEREITS in der Zwischenablage. In der OPNsense-Web-GUI:"
Write-Host ""
Write-Host "    System -> Access -> Users  ->  [dein SSH-User]"
Write-Host "    Feld 'authorized keys'  ->  rechte Maustaste -> Einfuegen"
Write-Host "    Save (unten)"
Write-Host ""

Write-Host "=================================================================" -ForegroundColor Green
Write-Host "  SCHRITT 2  -  Private-Key -> OPN-Cockpit" -ForegroundColor Green
Write-Host "=================================================================" -ForegroundColor Green
Write-Host ""
Write-Host "  Im zweiten Notepad steht der Private-Key (Block,"
Write-Host "  beginnt mit '-----BEGIN OPENSSH PRIVATE KEY-----')."
Write-Host ""
Write-Host "  Diesen Block KOMPLETT (Strg+A, Strg+C) ins OPN-Cockpit:"
Write-Host ""
Write-Host "    Geraet -> Bearbeiten -> 'SSH-Private-Key (PEM)'"
Write-Host "    Speichern"
Write-Host ""

Write-Host "=================================================================" -ForegroundColor Red
Write-Host "  DANACH (wichtig)" -ForegroundColor Red
Write-Host "=================================================================" -ForegroundColor Red
Write-Host ""
Write-Host "  Die zwei Dateien hier liegen UNVERSCHLUESSELT auf der Platte:" -ForegroundColor White
Write-Host "    $keyPath"
Write-Host "    $keyPath.pub"
Write-Host ""
Write-Host "  Nach erfolgreichem Speichern im Cockpit:" -ForegroundColor White
Write-Host "    1. Notepad-Fenster ohne Speichern schliessen."
Write-Host "    2. Die beiden Dateien aus $workDir loeschen"
Write-Host "       (oder das ganze Verzeichnis)."
Write-Host ""
Write-Host "  Der Private-Key liegt im Cockpit-Tresor verschluesselt mit dem"
Write-Host "  Master-Passwort - das reicht als Persistenz."
Write-Host ""

Read-Host "  Mit Enter beenden"
