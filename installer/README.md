# Installer

Inno-Setup-Skript für OPN-Cockpit v2.0. Erzeugt einen Windows-Installer
mit Desktop-Verknüpfung und Start-Menü-Eintrag.

## Voraussetzungen

- **Inno Setup 6+** auf dem Build-Rechner — https://jrsoftware.org/isinfo.php
- **Auf dem Ziel-System** (vor dem Installer-Lauf):
  - Python 3.11+
  - [`uv`](https://docs.astral.sh/uv/getting-started/installation/)

Der Installer prüft beim Start ob Python und uv da sind und bricht
sonst mit klarer Fehlermeldung ab. Eine spätere Iteration kann Python
als Embedded-Distribution mitliefern, um die Installation komplett
in sich geschlossen zu machen.

## Bauen

```powershell
cd installer
ISCC opn-cockpit.iss
```

Ergebnis: `installer\out\OPN-Cockpit-Setup-0.1.0.exe`.

## Was der Installer tut

1. Kopiert `src\`, `scripts\`, `docs\`, `start.bat`, `pyproject.toml`,
   `README.md`, `CHANGELOG.md` nach `%ProgramFiles%\OPN-Cockpit\`.
2. Ruft `scripts\setup-venv.ps1` auf — legt `.venv\` an und installiert
   die Runtime-Dependencies.
3. Legt eine Desktop-Verknüpfung „OPN-Cockpit" an (Aufruf `start.bat`).
4. Legt Start-Menü-Einträge an: Starten, Quickstart öffnen,
   Deinstallieren.
5. Bietet am Ende „Jetzt starten" an.

## Was nicht im Installer ist

- **Tresor-Dateien** — die liegen in `%APPDATA%\OPN-Cockpit\` und werden
  von der Deinstallation **nicht** angefasst. Der User behält seine
  `.opnvault`-Dateien.
- **Audit-Log und Plan-Store** — ebenfalls in `%APPDATA%\OPN-Cockpit\`.

## Geplante Erweiterungen

- Embedded-Python-Variante, damit der Installer ohne System-Python läuft.
- Optional: Windows-Dienst-Modus (über `pywin32` oder `NSSM-Wrapper`),
  damit der Server beim Systemstart hochkommt — siehe Memory-Notiz im
  Projekt-Status.
- Signing-Zertifikat sobald das Tool veröffentlicht wird.
