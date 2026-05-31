# Installer

Inno-Setup-Skript für OPN-Cockpit (v6 — Embedded-Python-Variante).
Erzeugt einen Windows-Installer mit zwei Installations-Modi:

- **Single-User**: Desktop-Verknüpfung, manueller Start per Doppelklick.
  Der User entscheidet wann der Server läuft.
- **Multi-User-Server**: Windows-Dienst (NSSM-basiert) mit Autostart. Im
  Netzwerk erreichbar (Standard-Port 9876), Single-File-Backup möglich
  (SQLite-Backends via `OPNCOCKPIT_STORAGE_BACKEND=sqlite`).

Seit v0.6.0 ist der **gesamte Python-Interpreter inkl. Dependencies im
Installer enthalten**. Auf dem Zielsystem muss kein System-Python mehr
installiert sein.

## Voraussetzungen auf dem Build-Rechner

- **Inno Setup 6+** — https://jrsoftware.org/isinfo.php
- **PowerShell 5.1+** mit Internet-Zugriff (für `bundle-python.ps1`)
- **NSSM** (nur für Service-Mode): `nssm.exe` aus https://nssm.cc/
  herunterladen und nach `installer\bundle\nssm.exe` legen. Public
  Domain, etwa 350 KB. Ohne `nssm.exe` wird die Service-Komponente
  trotzdem gebaut, schlägt aber bei der Service-Registrierung fehl.

## Bauen

```powershell
# 0. Einmalig pro Session: Execution-Policy lockern, sonst weigert sich
#    PowerShell unsignierte Skripte zu starten.
Set-ExecutionPolicy -ExecutionPolicy Bypass -Scope Process -Force

# 1. Embedded-Python + alle Dependencies in installer\bundle\python\ legen.
#    Lädt python-3.11.x-embed-amd64.zip + get-pip.py von python.org und
#    bestueckt das Bundle. Dauert je nach Internet ~1-2 Min, danach hat
#    der Ordner ~100 MB.
.\installer\bundle-python.ps1

# 2. Inno-Setup-Compiler aufrufen.
ISCC installer\opn-cockpit.iss
```

> **Tipp**: wenn du das öfter machst, einmalig
> `Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser`
> setzen — erlaubt lokale Skripte dauerhaft, blockt aber weiterhin
> unsignierte Skripte aus dem Internet. Alternativ direkt:
> `powershell -NoProfile -ExecutionPolicy Bypass -File .\installer\bundle-python.ps1`

Ergebnis: `installer\out\Install-OPN-Cockpit-0.6.0.exe` (typische Größe
~80-100 MB; alles ist drin).

Re-Build mit aktualisierten Dependencies:

```powershell
.\installer\bundle-python.ps1 -Force   # frischer Bundle-Lauf
ISCC installer\opn-cockpit.iss
```

## Auf dem Ziel-System

Der Installer kopiert das Embedded-Python nach
`%ProgramFiles%\OPN-Cockpit\python\` und das Source-Tree daneben.
Der Setup-Launcher (`Scripts\opn-cockpit.exe`) wird beim Install via
pip aus dem entry_point in `pyproject.toml` erzeugt und ist ein echter
Windows-Launcher mit Datei-Properties. `start.bat` ist nur noch fuer den
Dev-Mode da und erkennt das Bundle automatisch und nutzt es vor einer
eventuell vorhandenen `.venv` (Dev-Modus).

**Updates ohne Datenverlust**: Beim ersten Boot nach einem Update
läuft der Migrations-Runner (v6-Pass 1). Wenn Schema-Migrationen
ausstehen, wird vorher ein Snapshot der Daten in
`<AppData>\OPN-Cockpit\backups\<timestamp>-pre-<version>\` angelegt.

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
4. Beim ersten Setup-Wizard-Aufruf legt der Server den Tresor automatisch
   unter dem in der Service-Env hinterlegten `OPNCOCKPIT_VAULT_PATH` an.

## Was nicht im Installer ist

- **Tresor-Dateien** — die liegen in `%APPDATA%\OPN-Cockpit\` (oder
  `%ProgramData%\OPN-Cockpit\` im Service-Mode) und werden von der
  Deinstallation **nicht** angefasst.
- **Audit-Log, Plan-Store, Migrations-Status** — ebenfalls.
- **NSSM-Binary** — selbst herunterladen (nssm.cc), siehe oben.

## Größenoptimierung

Das Embedded-Bundle enthält den vollen Python-Interpreter. Wenn du das
Image schmaler willst:

- `installer\bundle\python\Doc\` löschen (~10 MB, falls vorhanden)
- `installer\bundle\python\tcl\` löschen (~25 MB) — Tk-Stack wird nicht
  benötigt, ist im embeddable-Build standardmäßig schon raus
- pip's Build-Cache und `pyc`-Dateien werden vom `bundle-python.ps1`
  automatisch entfernt

## Geplante Erweiterungen

- **v6-Pass 3**: Update-Check via GitHub-Releases-API + In-App-Banner.
- Code-Signing-Zertifikat sobald das Tool veröffentlicht wird.
