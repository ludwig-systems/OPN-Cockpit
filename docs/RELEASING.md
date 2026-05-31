# Release-Prozess für Maintainer

Dieses Dokument beschreibt den Schritt-für-Schritt-Ablauf um eine neue
Version zu veröffentlichen. Der Workflow ist so automatisiert wie möglich
und braucht im Idealfall nur einen Tag-Push.

## Voraussetzungen

- Schreibzugriff auf das Repository
- Optionale lokale Tools für Tests:
  - PowerShell 5.1+ (Bundle-Build)
  - Inno Setup 6+ (lokaler Installer-Build)

## Versionsschema

`MAJOR.MINOR.PATCH` ohne v-Präfix in den Quelldateien (`__init__.py`,
`pyproject.toml`, `opn-cockpit.iss`). Der Tag wird mit `v`-Präfix
versehen (`v0.7.0`).

## Standard-Ablauf

### 1. Code vorbereiten

```bash
# Working tree sauber, alle Änderungen committed
git status

# Volle Test-Suite muss grün sein
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/python.exe -m ruff check src tests
.venv/Scripts/python.exe -m mypy src
```

### 2. Version in Code bumpen

Drei Dateien synchronisieren:

```python
# src/opn_cockpit/__init__.py
__version__ = "0.7.0"
```

```toml
# pyproject.toml
[project]
version = "0.7.0"
```

```ini
; installer/opn-cockpit.iss
#define MyAppVersion   "0.7.0"
```

> **Sicherheitsnetz**: Die Release-Action patcht alle drei Dateien
> nochmal beim Build auf den Tag-Wert. Vergessen hier kostet also
> nichts — die EXE wird trotzdem korrekt. Aber das Repo zeigt dann
> die alte Versionsnummer.

### 3. CHANGELOG aktualisieren

Sektion `## v0.7.0 — YYYY-MM-DD — Titel` ans Anfang. Bullet-Stil wie
in vorherigen Einträgen. Die Release-Action extrahiert diesen Abschnitt
automatisch in den GitHub-Release-Body.

### 4. Commit + Tag pushen

```bash
git add src/opn_cockpit/__init__.py pyproject.toml installer/opn-cockpit.iss CHANGELOG.md
git commit -m "chore: bump version to 0.7.0"
git tag v0.7.0
git push origin main
git push origin v0.7.0
```

### 5. Release-Action zuschauen

GitHub Actions baut automatisch:

1. NSSM downloaden (für Service-Komponente)
2. Inno Setup via Chocolatey installieren
3. `installer\bundle-python.ps1` ausführen (Python-Bundle + Deps)
4. ISCC ausführen (Installer kompilieren)
5. SHA256 berechnen
6. GitHub-Release mit Setup-EXE + Checksumme veröffentlichen

Dauer: ~5 Minuten. Status sichtbar unter
[Actions](https://github.com/ludwig-systems/opn-cockpit/actions).

### 6. Update-Check verifizieren

Auf einer bestehenden Installation:

```powershell
curl http://localhost:9876/api/updates/check?force=true
```

Erwartung: `status: "available"`, `latest_version: "v0.7.0"`. Im
Browser sollte beim nächsten Inventar-Load der Banner erscheinen.

## winget-Update (optional)

Nach erfolgreichem GitHub-Release:

```powershell
# SHA256 aus dem Release-Asset holen:
$sha = (Invoke-WebRequest "https://github.com/ludwig-systems/opn-cockpit/releases/download/v0.7.0/Install-OPN-Cockpit-0.7.0.exe.sha256").Content.Split(" ")[0]

# Manifeste generieren:
.\installer\winget\generate-manifests.ps1 `
  -Version 0.7.0 `
  -InstallerUrl "https://github.com/ludwig-systems/opn-cockpit/releases/download/v0.7.0/Install-OPN-Cockpit-0.7.0.exe" `
  -InstallerSha256 $sha
```

Dann den Inhalt von `installer/winget/out/manifests/l/ludwig-systems/opn-cockpit/0.7.0/`
in einen Fork von [microsoft/winget-pkgs](https://github.com/microsoft/winget-pkgs)
kopieren und einen PR öffnen. Siehe [installer/winget/README.md](../installer/winget/README.md).

## Notfall: manueller Workflow-Trigger

Falls der Tag-Push einmal nicht greift (z. B. weil Hooks blockieren):

```
Actions → Release → Run workflow → Branch: main, Version: 0.7.0
```

Der Manuelle Trigger nutzt denselben Build-Pfad — Vorbedingung ist,
dass `main` schon den korrekten Code-Stand hat.

## Wenn die Action scheitert

- **Choco-Install-Fehler**: GitHub-Runner-Image kann mal kaputt sein.
  Workflow im Actions-Tab erneut starten („Re-run all jobs").
- **NSSM-Download-404**: nssm.cc liegt selten down. Workflow erneut starten.
- **Bundle-Build-Fehler**: meistens ein neuer Python-Patch-Release der die
  embed-Zip umbenannt hat. `installer/bundle-python.ps1` Default-Parameter
  `PythonVersion` anpassen und PR.
- **ISCC-Fehler**: Inno-Setup-Syntax-Check. Lokal mit `ISCC installer/opn-cockpit.iss`
  reproduzieren.

## Hotfix-Releases

Für PATCH-Bumps gleicher Ablauf (`0.7.1`). Da unsere Migrations-IDs
versions-unabhängig sind, ist ein Hotfix nicht riskanter als ein
normales Release — der Pre-Update-Backup-Mechanismus greift gleich.

## Pre-Releases

Tags mit Suffix (`v0.7.0-rc1`, `v0.7.0.dev1`) werden vom Tag-Filter
`v*.*.*` NICHT gematched (wegen drei Komponenten + Suffix). Falls
Pre-Releases nötig sind, manuell via `workflow_dispatch` auslösen und
im GitHub-Release-Edit-Mask das Checkbox „This is a pre-release"
nachträglich setzen. Der In-App-Update-Check ignoriert Pre-Releases
absichtlich — User auf Stable-Branches sehen keinen Banner für
`-rc1`-Builds.
