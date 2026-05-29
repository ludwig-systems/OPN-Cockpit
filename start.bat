@echo off
:: OPN-Cockpit Starter (Doppelklick zum Starten der GUI).
::
:: Erwartet, dass scripts\setup-venv.ps1 einmal gelaufen ist und unter
:: .venv\ eine voll eingerichtete Python-Umgebung liegt.

setlocal
set "REPO_ROOT=%~dp0"
set "VENV_PYTHON=%REPO_ROOT%.venv\Scripts\python.exe"

if not exist "%VENV_PYTHON%" (
    echo Fehler: .venv\Scripts\python.exe nicht gefunden.
    echo Bitte erst einmalig scripts\setup-venv.ps1 ausfuehren.
    pause
    exit /b 1
)

"%VENV_PYTHON%" -m opn_cockpit
endlocal
