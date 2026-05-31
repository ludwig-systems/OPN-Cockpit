<#
.SYNOPSIS
    Erzeugt eine Embedded-Python-Distribution unter installer\bundle\python\.

.DESCRIPTION
    Laedt die offizielle Windows-Embeddable-Distribution von python.org,
    aktiviert die site-packages-Aufloesung, bootstrapt pip und installiert
    alle Runtime-Dependencies des Projekts + das Projekt selbst hinein.

    Ziel: Der Inno-Setup-Installer kann den Ordner als kompletten,
    selbsttragenden Python-Snapshot mitliefern. Auf dem Zielsystem ist
    danach KEIN System-Python notwendig. start.bat erkennt das Bundle
    automatisch und nutzt es vor einer optionalen .venv (Dev-Modus).

    Inkrementell: existiert das Bundle bereits, wird ein Re-Build mit
    -Force erzwungen. Sonst wird die vorhandene Distribution mit
    aktualisierten Dependencies neu bespielt (pip install --upgrade).

.PARAMETER PythonVersion
    Python-Patch-Version (3.11.x). Default 3.11.9 -- passt zum Cockpit-
    Requirement Python 3.11+ und ist die letzte 3.11.x mit publishten
    embeddable-Builds zum Zeitpunkt des Releases.

.PARAMETER Force
    Verwirft eine vorhandene Bundle-Distribution und baut von Grund auf neu.

.NOTES
    Ausfuehren aus dem Repo-Root (oder aus installer\). Greift nur lesend
    auf den Source-Tree zu -- alle Aenderungen landen in
    installer\bundle\python\.

    Voraussetzungen: PowerShell 5.1+, Internet-Zugriff. Kein System-
    Python notwendig (das Embedded-Python wird ueber sich selbst
    bootstrapped).

    ASCII-only: Windows PowerShell 5.1 liest .ps1 als CP-1252; UTF-8-
    Sonderzeichen wuerden den Parser stoeren.
#>

#requires -Version 5.1

[CmdletBinding()]
param(
    [string]$PythonVersion = "3.11.9",
    [switch]$Force
)

$ErrorActionPreference = "Stop"

# --- Pfade ------------------------------------------------------------------

$ScriptDir   = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot    = Split-Path -Parent $ScriptDir
$BundleDir   = Join-Path $ScriptDir "bundle\python"
$DownloadDir = Join-Path $env:TEMP "opn-cockpit-bundle-cache"

if (-not (Test-Path $DownloadDir)) {
    New-Item -ItemType Directory -Path $DownloadDir -Force | Out-Null
}

# --- Force-Reset ------------------------------------------------------------

if ($Force -and (Test-Path $BundleDir)) {
    Write-Host "==> -Force: entferne vorhandenes Bundle ($BundleDir)" -ForegroundColor Yellow
    Remove-Item -Recurse -Force $BundleDir
}

# --- Embedded-Python herunterladen + entpacken ------------------------------

$EmbedUrl  = "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-embed-amd64.zip"
$EmbedZip  = Join-Path $DownloadDir "python-$PythonVersion-embed-amd64.zip"
$BundlePy  = Join-Path $BundleDir "python.exe"

if (-not (Test-Path $BundlePy)) {
    if (-not (Test-Path $EmbedZip)) {
        Write-Host "==> Lade Embedded-Python $PythonVersion ($EmbedUrl)" -ForegroundColor Cyan
        Invoke-WebRequest -Uri $EmbedUrl -OutFile $EmbedZip
    } else {
        Write-Host "==> Embedded-Python-Zip schon im Cache: $EmbedZip" -ForegroundColor DarkGray
    }

    Write-Host "==> Entpacke nach $BundleDir" -ForegroundColor Cyan
    if (-not (Test-Path $BundleDir)) {
        New-Item -ItemType Directory -Path $BundleDir -Force | Out-Null
    }
    Expand-Archive -Path $EmbedZip -DestinationPath $BundleDir -Force
} else {
    Write-Host "==> Embedded-Python existiert schon ($BundlePy)" -ForegroundColor DarkGray
}

# --- _pth-Datei patchen: site-packages aktivieren ---------------------------
#
# Die Default-Distribution hat eine Zeile "# import site" (auskommentiert),
# was bewirkt, dass site-packages NICHT durchsucht werden und pip somit
# nichts installieren kann. Wir kommentieren das ein und ergaenzen den
# Lib\site-packages-Pfad explizit, damit das Layout wie bei einer normalen
# Python-Installation aussieht.

$PthFile = Get-ChildItem -Path $BundleDir -Filter "python*._pth" | Select-Object -First 1
if ($null -eq $PthFile) {
    throw "Konnte python*._pth nicht finden in $BundleDir -- Distribution wirkt unvollstaendig."
}

$PthContent = Get-Content $PthFile.FullName
$NeedsPatch = $true
foreach ($line in $PthContent) {
    if ($line -match "^\s*import\s+site\s*$") {
        $NeedsPatch = $false
        break
    }
}
if ($NeedsPatch) {
    Write-Host "==> Patche $($PthFile.Name): site-packages aktivieren" -ForegroundColor Cyan
    $NewContent = @()
    foreach ($line in $PthContent) {
        # "# import site" -> "import site"
        if ($line -match "^\s*#\s*import\s+site\s*$") {
            $NewContent += "import site"
        } else {
            $NewContent += $line
        }
    }
    # Lib\site-packages aufnehmen, falls nicht schon drin.
    if (-not ($NewContent -match "^Lib\\site-packages$")) {
        $NewContent += "Lib\site-packages"
    }
    Set-Content -Path $PthFile.FullName -Value $NewContent -Encoding ASCII
} else {
    Write-Host "==> _pth ist schon gepatcht -- site ist aktiv" -ForegroundColor DarkGray
}

# --- pip bootstrappen -------------------------------------------------------

$PipModule = Join-Path $BundleDir "Lib\site-packages\pip"
if (-not (Test-Path $PipModule)) {
    $GetPip = Join-Path $DownloadDir "get-pip.py"
    if (-not (Test-Path $GetPip)) {
        Write-Host "==> Lade get-pip.py" -ForegroundColor Cyan
        Invoke-WebRequest -Uri "https://bootstrap.pypa.io/get-pip.py" -OutFile $GetPip
    } else {
        Write-Host "==> get-pip.py liegt im Cache" -ForegroundColor DarkGray
    }
    Write-Host "==> Bootstrap pip in das Bundle" -ForegroundColor Cyan
    & $BundlePy $GetPip --no-warn-script-location
    if ($LASTEXITCODE -ne 0) { throw "get-pip.py fehlgeschlagen (Exit $LASTEXITCODE)." }
} else {
    Write-Host "==> pip ist bereits im Bundle" -ForegroundColor DarkGray
}

# --- Projekt-Dependencies installieren --------------------------------------
#
# Wir installieren das Paket selbst (aus dem Repo-Root) im "wheel"-Modus,
# damit das Bundle vom Source-Tree unabhaengig ist. Editable-Install waere
# falsch, weil es einen Pfad zurueck ins Repo benoetigt.

Write-Host "==> Installiere opn-cockpit + Runtime-Dependencies" -ForegroundColor Cyan
$EnvForPip = @{ "PIP_DISABLE_PIP_VERSION_CHECK" = "1" }
foreach ($key in $EnvForPip.Keys) {
    [Environment]::SetEnvironmentVariable($key, $EnvForPip[$key], "Process")
}

Push-Location $RepoRoot
try {
    & $BundlePy -m pip install --upgrade pip --no-warn-script-location
    if ($LASTEXITCODE -ne 0) { throw "pip self-upgrade fehlgeschlagen." }

    # Embedded-Python + _pth: pip's Build-Isolation funktioniert nicht,
    # weil PYTHONPATH im _pth-Modus ignoriert wird. Wir installieren das
    # Build-Backend (hatchling) explizit ins Bundle und nutzen dann
    # --no-build-isolation fuer den Projekt-Install.
    & $BundlePy -m pip install --upgrade hatchling --no-warn-script-location
    if ($LASTEXITCODE -ne 0) { throw "hatchling-Install fehlgeschlagen." }

    & $BundlePy -m pip install --upgrade --no-build-isolation . --no-warn-script-location
    if ($LASTEXITCODE -ne 0) { throw "Projekt-Install fehlgeschlagen." }
} finally {
    Pop-Location
}

# --- Sanity-Check -----------------------------------------------------------

Write-Host "==> Sanity-Check: import opn_cockpit" -ForegroundColor Cyan
$SanityResult = & $BundlePy -c "import opn_cockpit; print(opn_cockpit.__version__)"
if ($LASTEXITCODE -ne 0) { throw "Bundle ist kaputt -- import opn_cockpit fehlgeschlagen." }
Write-Host "==> Bundle-Version: $SanityResult" -ForegroundColor Green

# --- Branded Launcher: python.exe -> opn-cockpit.exe ------------------------
#
# Pip's generierte console_scripts-Launcher (Scripts\opn-cockpit.exe) haben
# einen absoluten Shebang auf die Build-Zeit-python.exe -- auf einer fremden
# Maschine zeigt der ins Leere und ein Server-Start scheitert. Ausserdem
# soll der Task-Manager-Eintrag "opn-cockpit.exe" heissen statt "python.exe".
#
# Embedded-Python-Trick: python.exe ist nicht an seinen eigenen Dateinamen
# gebunden -- die python311.dll wird relativ zum Executable gesucht. Wir
# duplizieren python.exe als opn-cockpit.exe und rufen sie spaeter mit
# "-m opn_cockpit" auf. Image-Name im Task-Manager: opn-cockpit.exe.
$BrandedLauncher = Join-Path $BundleDir "opn-cockpit.exe"
Copy-Item -Force $BundlePy $BrandedLauncher
Write-Host "==> Branded Launcher: $BrandedLauncher" -ForegroundColor Cyan
$LauncherCheck = & $BrandedLauncher -c "import opn_cockpit; print('launcher ok:', opn_cockpit.__version__)"
if ($LASTEXITCODE -ne 0) { throw "Branded Launcher startet kein Python (Bundle kaputt)." }
Write-Host "==> $LauncherCheck" -ForegroundColor Green

# Pip's Scripts/opn-cockpit.exe und opn-cockpit-cli.exe haben den kaputten
# Absoluten-Pfad-Shebang -- die wollen wir definitiv NICHT mit ausliefern,
# damit niemand sie versehentlich startet. Entfernen.
foreach ($brokenLauncher in @("opn-cockpit.exe", "opn-cockpit-cli.exe")) {
    $broken = Join-Path $BundleDir "Scripts\$brokenLauncher"
    if (Test-Path $broken) {
        Remove-Item -Force $broken
        Write-Host "==> Entferne pip-Launcher mit Build-Zeit-Shebang: Scripts\$brokenLauncher" -ForegroundColor DarkGray
    }
}

# --- Aufraeumen: pip-Cache + __pycache__ raus --------------------------------

Write-Host "==> Raeume __pycache__ aus dem Bundle" -ForegroundColor Cyan
Get-ChildItem -Path $BundleDir -Recurse -Directory -Filter "__pycache__" |
    ForEach-Object { Remove-Item -Recurse -Force $_.FullName }

# Bundle-Groesse ausgeben
$Size = (Get-ChildItem $BundleDir -Recurse -File | Measure-Object Length -Sum).Sum
$SizeMb = [math]::Round($Size / 1MB, 1)
Write-Host ""
Write-Host "==> Bundle fertig: $BundleDir  ($SizeMb MB)" -ForegroundColor Green
Write-Host "==> Naechster Schritt: ISCC installer\opn-cockpit.iss" -ForegroundColor Green
