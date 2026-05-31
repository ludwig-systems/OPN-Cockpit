# winget-Submission

Ein winget-Paket wird per Pull-Request gegen das offizielle Repository
[microsoft/winget-pkgs](https://github.com/microsoft/winget-pkgs)
eingereicht. Nach Merge können Windows-Nutzer das Tool per

```powershell
winget install ludwig-systems.opn-cockpit
winget upgrade ludwig-systems.opn-cockpit
```

installieren bzw. updaten — ohne SmartScreen-Klick, weil winget die
Datei selbst per SHA256-Hash verifiziert.

## Voraussetzungen vor dem ersten Submit

1. Ein **veröffentlichter GitHub-Release** mit der Installer-EXE als
   Asset (das passiert automatisch via `.github/workflows/release.yml`).
2. Die SHA256-Pruefsumme der EXE (steht in `*.exe.sha256` neben dem
   Asset).
3. Eine **PackageIdentifier** registriert auf `ludwig-systems.opn-cockpit`
   (der wird beim ersten Submit angelegt; danach unveränderbar).

## Manifeste generieren

Das Hilfsskript `generate-manifests.ps1` füllt die Templates aus
`template/` mit der echten Versionsnummer + URL + Hash:

```powershell
.\installer\winget\generate-manifests.ps1 `
  -Version 0.7.0 `
  -InstallerUrl "https://github.com/ludwig-systems/opn-cockpit/releases/download/v0.7.0/Install-OPN-Cockpit-0.7.0.exe" `
  -InstallerSha256 abc123...  # aus *.exe.sha256
```

Ergebnis: `installer/winget/out/manifests/l/ludwig-systems/opn-cockpit/0.7.0/`
mit den drei YAML-Dateien:

- `ludwig-systems.opn-cockpit.yaml`               (Version-Manifest)
- `ludwig-systems.opn-cockpit.installer.yaml`     (Installer-Manifest)
- `ludwig-systems.opn-cockpit.locale.de-DE.yaml`  (Default-Locale)

## Submit-Prozess

1. Fork von [microsoft/winget-pkgs](https://github.com/microsoft/winget-pkgs)
   anlegen (einmalig).
2. Den generierten Ordner `manifests/l/ludwig-systems/opn-cockpit/0.7.0/`
   in die gleiche Pfad-Struktur im Fork kopieren.
3. Mit `winget validate manifests/l/ludwig-systems/opn-cockpit/0.7.0`
   lokal prüfen.
4. Optional: `winget install --manifest manifests/l/ludwig-systems/opn-cockpit/0.7.0`
   für einen Live-Test auf dem eigenen Rechner.
5. Branch + Commit + Push, PR gegen `microsoft/winget-pkgs:master` öffnen.
6. Automatische Validierung läuft im PR, Merge erfolgt durch Microsoft-Bot
   sobald Reviews durch sind (übliche Wartezeit: paar Stunden bis ein Tag).

## Auf Folge-Releases

Beim nächsten Release nur das Skript erneut mit den neuen Daten laufen
lassen — ein neues Versions-Verzeichnis (`0.8.0/`) entsteht, der Submit
ist wieder ein PR. PackageIdentifier bleibt gleich, deshalb erkennen
bestehende Installationen `winget upgrade` automatisch.

## Was das Skript NICHT macht

- Es schreibt nichts ins winget-pkgs-Repo. Der PR-Schritt bleibt manuell
  (oder via separater Action — siehe Roadmap unten).

## Roadmap

- **Auto-Submit per Action**: ein zweiter Workflow könnte nach jedem
  Release den Fork updaten und den PR via `gh pr create` öffnen. Dazu
  braucht es ein Personal-Access-Token mit `repo`-Scope (kann später
  hinzugefügt werden, wenn die manuelle Routine etabliert ist).
