@echo off
:: OPN-Cockpit Starter (Doppelklick zum Starten der Web-GUI).
::
:: Erwartet, dass scripts\setup-venv.ps1 einmal gelaufen ist und unter
:: .venv\ eine voll eingerichtete Python-Umgebung liegt.
::
:: Vor dem Start wird geprueft, ob der Port schon belegt ist. Wenn ja
:: und der Prozess ein Python ist, wird er als alte Cockpit-Instanz
:: erkannt und beendet. Fremde Prozesse (anderer App) werden nicht
:: angetastet - dann meldet die Batch das und bricht ab.

setlocal
set "REPO_ROOT=%~dp0"
set "VENV_PYTHON=%REPO_ROOT%.venv\Scripts\python.exe"

if not exist "%VENV_PYTHON%" (
    echo Fehler: .venv\Scripts\python.exe nicht gefunden.
    echo Bitte erst einmalig scripts\setup-venv.ps1 ausfuehren.
    pause
    exit /b 1
)

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

"%VENV_PYTHON%" -m opn_cockpit
endlocal
