# Installer

Inno-Setup-Skript für OPN-Cockpit v3.2. Erzeugt einen Windows-Installer
mit zwei Installations-Modi:

- **Single-User**: Desktop-Verknüpfung, Aufruf via `start.bat`. Der
  User entscheidet manuell wann der Server läuft.
- **Multi-User-Server**: Windows-Dienst (NSSM-basiert) mit Autostart.
  Im Netzwerk erreichbar (Standard-Port 9876), Single-File-Backup
  möglich (SQLite-Backends via `OPNCOCKPIT_STORAGE_BACKEND=sqlite`).

## Voraussetzungen

- **Inno Setup 6+** auf dem Build-Rechner — https://jrsoftware.org/isinfo.php
- **NSSM** (nur für Service-Mode): `nssm.exe` aus https://nssm.cc/
  herunterladen und nach `installer\bundle\nssm.exe` legen. Public
  Domain, etwa 350 KB. Ohne `nssm.exe` wird die Service-Komponente
  trotzdem gebaut, schlägt aber bei der Service-Registrierung fehl.

## Bauen

```powershell
cd installer
ISCC opn-cockpit.iss
```

Ergebnis: `installer\out\OPN-Cockpit-Setup-0.1.0.exe`.

## Auf dem Ziel-System

Der Installer prüft beim Start ob Python 3.11+ und `uv` installiert
sind. Fehlen sie, bietet er **automatisch den Download an** (Python
direkt von python.org, uv per `irm https://astral.sh/uv/install.ps1
| iex`). Der User kann das auch ablehnen und manuell nachholen.

## Mode-Wahl im Installer

Während der Installation fragt der Setup-Wizard nach dem Modus:

| Mode | Wann sinnvoll |
|---|---|
| Single-User | Du bist allein der Admin, eigene Workstation/PAW |
| Multi-User-Server | Mehrere Admins, Server soll bei Systemstart hochkommen |

Bei **Multi-User-Server**:
1. NSSM registriert den Dienst `OPN-Cockpit` (Startup: Automatisch)
2. Service läuft als `NT AUTHORITY\LocalService`
3. Logs landen in `%ProgramData%\OPN-Cockpit\logs\`
4. Vor dem ersten Setup-Wizard musst du `OPNCOCKPIT_AUTH_BACKEND=user-db`
   in der Service-Env setzen (`nssm edit OPN-Cockpit` → Environment).

## Was nicht im Installer ist

- **Tresor-Dateien** — die liegen in `%APPDATA%\OPN-Cockpit\` (oder
  `%ProgramData%\OPN-Cockpit\` im Service-Mode) und werden von der
  Deinstallation **nicht** angefasst.
- **Audit-Log und Plan-Store** — ebenfalls.
- **NSSM-Binary** — selbst herunterladen, siehe oben.

## Geplante Erweiterungen

- Embedded-Python-Variante (komplett ohne System-Python).
- Auto-Setzen der Service-Env-Variablen via Installer-Prompt (statt
  manuelles `nssm edit`).
- Code-Signing-Zertifikat sobald das Tool veröffentlicht wird.
