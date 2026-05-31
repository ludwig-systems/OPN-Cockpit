# Session-Notes 2026-05-31 — v0.6.0 Test-Vorbereitung + Debug

Diese Datei dokumentiert den heutigen Tag: was im Code bewegt wurde, welche
Bugs aufgedeckt wurden, welche Architektur-Entscheidungen getroffen wurden,
und was beim Testen morgen zu beachten ist.

## Commit-Log (heute, neueste oben)

| Commit | Was |
|---|---|
| `4f261cc` | About-Email auf `info@ludwig-systems.de` |
| `43d0664` | Branded `opn-cockpit.exe` (python.exe-Kopie) + nativer Windows-Folder-Picker |
| `bdfdb88` | mtime-basierter Cache-Buster + no-cache für Index-HTML |
| `83ebc39` | Folder-Picker für Vault-Anlage + Tresor-Name separat vom Speicherort |
| `e6fe6a6` | `Path.home()` als erlaubte Vault-Basis (Single-User soll Documents/Desktop nutzen können) |
| `99f8185` | `vault_path`/`vault_filename` in `CreateVaultResponse` + Quick-Pick-Chips |
| `c36889c` | doppelte `const exportBtn` Deklaration brach Bootstrap-JS |
| `38828f7`, `14ed922`, `3499151`, `a87d3fa` | Installer-Politur: Inno-Setup-Keywords, docs/-Ausschluss, opn-cockpit.exe-Pfad |
| `5b6bbef`, `614e622`, `108046e` | bundle-python.ps1: hatchling, ASCII, Execution-Policy-Hinweis |
| `8b9b5c0` | Release-Automation via GitHub Actions + winget-Skelett |

## Was im Stack heute verändert wurde

### 1. Launcher-Mechanismus (`opn-cockpit.exe`)

**Problem aufgedeckt**: pip's `Scripts\opn-cockpit.exe` (Console-Script-Launcher)
hat einen **absoluten Shebang** auf die Python-Executable mit der pip lief —
zur Build-Zeit war das `G:\OPN-Deploy\installer\bundle\python\python.exe`.
Auf einer fremden Maschine zeigt der Pfad ins Leere. Auf der Build-Maschine
selbst (wo `G:\OPN-Deploy\…` weiter existiert) wurde der Launcher
**versehentlich aus dem Source-Tree-Bundle** gestartet — und lud
**altes site-packages**. Konsequenz: alle Live-Patches in
`%ProgramFiles%\OPN-Cockpit\…` waren wirkungslos, der Browser sah immer
denselben veralteten Stand.

**Fix** (`bundle-python.ps1` + `opn-cockpit.iss`):

- Nach `pip install` wird `python.exe` als `opn-cockpit.exe` in dieselbe
  Datei-Lage **kopiert**. Embedded-Python ist nicht an seinen eigenen
  Dateinamen gebunden — die `python311.dll` wird relativ zum Executable
  gesucht.
- Die kaputten `Scripts\opn-cockpit.exe` + `opn-cockpit-cli.exe` werden
  **entfernt** damit niemand sie versehentlich startet.
- Inno-Setup-Verknüpfungen rufen `{app}\python\opn-cockpit.exe -m
  opn_cockpit` auf — Task-Manager zeigt unseren Namen, Funktion ist
  identisch zu `python.exe -m opn_cockpit`.
- `{autodesktop}` → `{userdesktop}` damit die Verknüpfung im
  Nutzerprofil landet (vorher: Public-Desktop, von OneDrive/Profil-Sync
  manchmal verschwiegen).

### 2. Vault-Anlage-UI: Name + Speicherort getrennt + Picker

Vorher: ein einziges Textfeld für den ganzen Pfad. Browser konnten keinen
nativen Save-Dialog auslösen.

**Drei UX-Verbesserungen**:

- **Tresor-Name**-Feld oben (Default `main`, `.opnvault` wird automatisch
  ergänzt)
- **Speicherort**-Feld nur für den Ordner-Pfad
- **"Ordner waehlen"**-Button rechts daneben öffnet primär den
  **nativen Windows-Shell-Folder-Picker** (`SHBrowseForFolderW` via ctypes,
  Vista-Style). Fallback: Web-Picker als Modal, wenn `/api/files/pick-folder`
  mit `501 Not Implemented` (Linux/Mac) oder `403 Forbidden` (Multi-User-Server)
  antwortet
- **Schnellauswahl-Chips** unter dem Speicherort: "Eigene Dokumente",
  "Desktop", "Anwendungsdaten" — vom Server geliefert
- **Live-Preview** "Wird gespeichert als: …" zeigt den finalen Pfad

### 3. Vault-Pfad-Validierung (Audit #14) gelockert

`web/vault_path.py` erlaubt jetzt auch `Path.home()` als Basis. Konsequenz:

- Single-User legt Tresore unter `Documents`, `Desktop`, eigene Unterordner
- Multi-User-Server unverändert restriktiv (User-Home des `LocalService`-
  Accounts enthält keine User-Daten)
- Path-Traversal nach `C:\Windows\…` bleibt abgewiesen

### 4. Cache-Buster `?v=<version>-<hash>`

`web/server.py` errechnet beim Index-Request einen mtime-Hash von
`static/app.js` + `static/styles.css`. Jede Patch-Änderung verschiebt
den Hash → Browser muss neu laden. Index-HTML bekommt `Cache-Control:
no-cache, must-revalidate`. Damit ist `Strg+Shift+R` nach einem Patch
nicht mehr nötig.

### 5. Schema-Konsistenz `vault_path` / `vault_filename`

`CreateVaultResponse` hatte `path` + `filename`, `UnlockResponse` hatte
`vault_path` + `vault_filename`. Frontend erwartete die Unlock-Form →
Crash beim Anlegen. Beide Schemas jetzt einheitlich.

## Operationelle Fallstricke (wichtig für morgen!)

### Server NIE aus Admin-PowerShell starten

Wenn du `opn-cockpit.exe` über `Start-Process` oder per Doppelklick aus
einer **Admin-PowerShell** startest, läuft der Server unter dem
Administrator-Konto. `%APPDATA%` ist dann `C:\Users\Administrator\…`,
nicht dein User-Profil. Konsequenz:

- Tresor landet im Admin-Profil
- About-Modal zeigt eventuell verwirrenden Kontext
- Vault-Pfad-Validierung lehnt dein eigenes `Documents` ab

**Richtig**: Server über Start-Menü starten **oder** in einer normalen
nicht-elevated PowerShell aufrufen.

### Browser-Cache nach Patch

Mit dem mtime-Cache-Buster sollte das jetzt verschwinden. Falls trotzdem
keine UI-Änderung sichtbar wird:

1. Erst checken ob der Server die neue HTML serviert:
   ```powershell
   $h = (Invoke-WebRequest "http://127.0.0.1:9876/" -UseBasicParsing).Content
   [regex]::Match($h, 'app\.js\?v=[^"]+').Value
   ```
   Wenn `app.js?v=0.6.0-<10-stelliger-Hash>` → Server ist neu. Wenn nur
   `app.js?v=0.6.0` → Server ist alt, Patch nicht aktiv.
2. Wenn Server neu, Cache leeren: F12 → Application → Clear site data.

### Welche python.exe läuft eigentlich?

Wenn etwas hakt:

```powershell
Get-NetTCPConnection -LocalPort 9876 -State Listen -EA SilentlyContinue |
  ForEach-Object {
    $p = Get-Process -Id $_.OwningProcess -EA SilentlyContinue
    "$($p.Name) ($($p.Id)): $($p.Path)"
  }
```

Erwartet: `opn-cockpit.exe` oder `python.exe`, Path unter
`C:\Program Files\OPN-Cockpit\python\…`.

**Wenn der Path auf `G:\OPN-Deploy\installer\bundle\python\…` zeigt** →
es läuft die Source-Tree-Version (Build-Bundle), nicht die Installation.
Killen + via Start-Menü / Install-Pfad neu starten.

### Sauberer Neu-Bau wenn Bundle korrupt

Nach jeder Änderung in `src/opn_cockpit/`:

```powershell
.\installer\bundle-python.ps1 -Force
ISCC installer\opn-cockpit.iss
```

Das `-Force` ist wichtig — sonst sieht pip "opn-cockpit 0.6.0 already
installed" und macht nix. Mit `-Force` wird `installer\bundle\python\`
komplett neu gebaut, inklusive der `opn-cockpit.exe`-Kopie.

## Was morgen zu testen ist

Schon vorbereitet im [TESTPLAN-0.6.0.md](TESTPLAN-0.6.0.md). Schwerpunkte:

1. **A.1–A.7 Windows Single-User** — komplett, inkl. neuer Vault-Anlage-UI
   und nativem Picker
2. **B.1–B.7 Windows Multi-User-Server** — NSSM-Service, Bootstrap-Token-Flow
3. **C.1–C.7 Linux/Docker** — `docker compose up` + Bootstrap
4. **E.1–E.4 Update-Check** — gegen leeren GitHub-Release sollte
   "unknown" rauskommen (kein Banner)

Bei jedem Fund: kurze Notiz mit:

- Was hast du gemacht (Klickpfad / Konsolen-Befehl)
- Was war erwartet
- Was kam tatsächlich
- Falls Browser involviert: F12 → Console + Network Tab

Damit kann ich morgen gezielt fixen.

## Live-Patch-Workflow (falls morgen was schnell behoben werden muss)

Nicht jedes Mal `bundle-python.ps1 -Force` + `ISCC` + Reinstall. Für
schnelle Iterationen reicht:

```powershell
# Admin-PowerShell
$src = "g:\OPN-Deploy\src\opn_cockpit"
$dst = "$env:ProgramFiles\OPN-Cockpit\python\Lib\site-packages\opn_cockpit"
# Hier die geaenderten Pfade einfuegen, z.B.:
Copy-Item -Force "$src\web\static\app.js" "$dst\web\static\app.js"
Copy-Item -Force "$src\web\templates\index.html" "$dst\web\templates\index.html"
# Bei Python-Datei: zugehoerige .pyc loeschen
Remove-Item -Force -EA SilentlyContinue "$dst\web\__pycache__\server.cpython-311.pyc"
```

Dann Server stoppen + neu starten (normale PowerShell). Browser bekommt
durch den mtime-Hash automatisch neue Asset-URLs.

## Offene Polish-Items (nach den Tests)

- **Windowless-Modus**: `[project.gui-scripts]` statt `[project.scripts]`
  damit kein Konsolen-Fenster aufpoppt. Bedingung: vorher File-Logging
  einbauen (sonst sind Fehler unsichtbar).
- **Tray-Icon-Stopp**: kleines Icon in der Taskleiste mit "Beenden",
  damit User den Server schließen kann ohne Task-Manager.
- **Setup-EXE Code-Signing**: SmartScreen-Warnung beseitigen. EV-Cert
  bei SSL.com (~$250/Jahr).
- **Native Picker auf Linux**: tkinter? Oder GTK via ctypes? Erst wenn
  Linux-Single-User-Mode konkret gewünscht ist.
- **GitHub-Release erstellen**: damit der Update-Check tatsächlich was
  zurückliefert. Repo ist da, aber kein Release getaggt.
