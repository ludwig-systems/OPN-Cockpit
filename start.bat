@echo off
:: OPN-Cockpit Starter (Doppelklick zum Starten der Web-GUI).
::
:: Erkennt zwei Layouts automatisch:
::   1. Installer-Mode: python\python.exe (Embedded-Python-Bundle aus dem
::      Inno-Setup-Installer, kein System-Python noetig).
::   2. Dev-Mode: .venv\Scripts\python.exe (vom Entwickler ueber
::      scripts\setup-venv.ps1 eingerichtet).
::
:: Vor dem Start wird geprueft, ob der Port schon belegt ist. Eine
:: bestehende Python-Instanz auf diesem Port wird als alte Cockpit-Instanz
:: gewertet und beendet. Fremde Prozesse bleiben unangetastet — dann
:: meldet die Batch das und bricht ab.

setlocal
set "REPO_ROOT=%~dp0"
set "BUNDLE_PYTHON=%REPO_ROOT%python\python.exe"
set "DEV_PYTHON=%REPO_ROOT%.venv\Scripts\python.exe"
set "PYTHON="

if exist "%BUNDLE_PYTHON%" (
    set "PYTHON=%BUNDLE_PYTHON%"
    set "MODE=installer-bundle"
) else if exist "%DEV_PYTHON%" (
    set "PYTHON=%DEV_PYTHON%"
    set "MODE=dev-venv"
) else (
    echo Fehler: Weder Embedded-Python-Bundle noch Dev-venv gefunden.
    echo   Erwartet: %BUNDLE_PYTHON%
    echo   Oder:     %DEV_PYTHON%
    echo.
    echo Wenn du das via Installer bekommen hast, ist die Installation
    echo defekt — bitte neu installieren. Im Dev-Repo bitte einmalig
    echo scripts\setup-venv.ps1 ausfuehren.
    pause
    exit /b 1
)

echo OPN-Cockpit starten (Modus: %MODE%).

if "%OPNCOCKPIT_PORT%"=="" (
    set "PORT=9876"
) else (
    set "PORT=%OPNCOCKPIT_PORT%"
)

powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='SilentlyContinue'; $c = Get-NetTCPConnection -LocalPort %PORT% -State Listen | Select-Object -First 1; if (-not $c) { Write-Host ('OPN-Cockpit: Port %PORT% ist frei.') -ForegroundColor DarkGray; exit 0 }; $p = Get-Process -Id $c.OwningProcess; if (-not $p) { Write-Host ('OPN-Cockpit: Port %PORT% belegt, Prozess nicht greifbar - bitte manuell pruefen.') -ForegroundColor Red; exit 1 }; if ($p.ProcessName -like 'python*') { Write-Host ('OPN-Cockpit: Beende vorherige Instanz (PID ' + $p.Id + ').') -ForegroundColor Yellow; Stop-Process -Id $p.Id -Force; Start-Sleep -Milliseconds 700; exit 0 } else { Write-Host ('OPN-Cockpit: Port %PORT% ist von ' + $p.ProcessName + ' (PID ' + $p.Id + ') belegt - kein Python.') -ForegroundColor Red; Write-Host 'Bitte den Prozess manuell beenden oder OPNCOCKPIT_PORT auf einen anderen Port setzen.' -ForegroundColor Red; exit 1 }"

if errorlevel 1 (
    echo.
    pause
    exit /b 1
)

"%PYTHON%" -m opn_cockpit
endlocal
