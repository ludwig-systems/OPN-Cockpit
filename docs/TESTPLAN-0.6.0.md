# Testplan OPN-Cockpit 0.6.0

End-to-End-Test der drei Deployment-Varianten, wie sie ein Endnutzer erleben würde.
Schreib bitte mit, was funktioniert und was hakt.

**Stand 2026-05-31 nach Debug-Session.** Wesentliche Architektur-Änderungen die in
diesem Testplan reflektiert sind — Details siehe
[SESSION-NOTES-0.6.0.md](SESSION-NOTES-0.6.0.md):

- Launcher: pip-generierte `Scripts\opn-cockpit.exe` ist tot, stattdessen
  läuft alles über `python\opn-cockpit.exe -m opn_cockpit` (Kopie der
  Embedded-`python.exe`, Task-Manager zeigt korrekten Namen)
- Folder-Picker: nativer Windows-Shell-Dialog statt Web-Picker
- Vault-Pfad: User-Home (Documents, Desktop) ist im Single-User-Mode erlaubt
- Tresor-Anlage-UI: Name + Speicherort getrennt, Live-Preview
- Cache-Buster: mtime-basiert, kein Strg+Shift+R nach Patch nötig
- About-Mail: `info@ludwig-systems.de`
- Server **niemals** aus einer Admin-PowerShell starten — sonst landen Tresor
  + Settings im Administrator-Profil statt im Nutzerprofil

## Vorbedingungen

- **Build-Rechner (Windows)**:
  - PowerShell 5.1+ mit Internetzugriff (für `bundle-python.ps1`)
  - Inno Setup 6+ (`ISCC.exe` im PATH oder GUI nutzbar)
  - `installer\bundle\nssm.exe` muss vorliegen (von nssm.cc, ~350 KB, Public Domain)
- **Test-Rechner (Windows)**: idealerweise eine frische VM oder Sandbox ohne System-Python — der Punkt ist ja, dass es ohne läuft
- **Linux/Docker**: Docker Desktop oder Linux-Host mit Docker + Compose v2

Alle Tests dürfen parallel laufen (Windows-VM + Docker-Container nebeneinander). Das Tool hat keine Cross-Talks.

---

## A) Windows Single-User-Mode

### A.1 Installer bauen (Build-Rechner)

```powershell
cd g:\OPN-Deploy

# Einmalig pro Session: Execution-Policy fuer dieses PowerShell-Fenster
# lockern, sonst lehnt PowerShell das unsignierte Bundle-Skript ab.
Set-ExecutionPolicy -ExecutionPolicy Bypass -Scope Process -Force

.\installer\bundle-python.ps1
# Erwartung: lädt Python 3.11.9-embed-amd64.zip + get-pip.py von python.org,
# entpackt nach installer\bundle\python\, installiert opn-cockpit + alle Deps.
# Ergebnis: "Bundle fertig: ... (~100 MB)".

ISCC installer\opn-cockpit.iss
# Ergebnis: installer\out\Install-OPN-Cockpit-0.6.0.exe (~80–100 MB)
```

**Pass**, wenn beide Schritte ohne Fehler durchlaufen und die Setup-EXE im `installer\out\`-Ordner liegt.

### A.2 Installation auf Test-Rechner

- Setup-EXE auf den Test-Rechner kopieren und doppelklicken.
- Sprache: Deutsch.
- Setup-Typ wählen: **"Single-User (lokaler Desktop-Start)"**.
- Defaults annehmen (`%ProgramFiles%\OPN-Cockpit\`).
- Desktop-Verknüpfung aktiv lassen.
- Setup-Wizard läuft durch.
- Am Ende den Haken "OPN-Cockpit jetzt starten" lassen.

**Pass**, wenn nach Abschluss die OPN-Cockpit-Konsole aufgeht und im
Standardbrowser `http://localhost:9876` erscheint.

### A.3 Erstes Entsperren / Vault anlegen

- Anmeldebildschirm zeigt Vault-Picker.
- "Neuen Tresor anlegen" wählen.
- **Tresor-Name**-Feld: Default `main`, kannst du wie du willst ändern
  (z. B. `firewalls-2026`).
- **Speicherort**-Feld: zeigt einen vorgeschlagenen Ordner. Drei Wege das zu setzen:
  1. **Button "Ordner waehlen"** → öffnet den **nativen Windows-Folder-Picker**
     (gleiche Optik wie in Word "Speichern unter"). Pick → Pfad steht im Feld.
  2. **Schnellauswahl-Chips** unter dem Feld: "Eigene Dokumente",
     "Desktop", "Anwendungsdaten". Click → Pfad gesetzt.
  3. **Direkt tippen** im Feld.
- **Live-Preview** "Wird gespeichert als: …" zeigt den finalen Pfad
  (Verzeichnis + Name + `.opnvault`).
- Master-Passwort min. 12 Zeichen, zweimal eingeben.
- "Tresor anlegen" klicken.

**Pass**, wenn die Hauptansicht (Inventar) erscheint, leerer Zustand mit
"Noch keine Geräte". Die `.opnvault`-Datei sollte unter dem gewählten
Pfad existieren (mit Explorer prüfen).

Pfade die akzeptiert sind:
- alles unter `%APPDATA%\OPN-Cockpit\` (Default)
- alles unter `%USERPROFILE%\` (Documents, Desktop, eigene Unterordner)
- alles unter `OPNCOCKPIT_VAULT_DIR` (Env-Override für Custom-Pfade)

Pfade die **abgelehnt** werden mit 400 "ausserhalb der erlaubten Basen":
alles andere (z. B. `C:\Windows\…`, `D:\Sonstwas\…` ohne Env-Override).

### A.4 Smoke-Tests

Klicke der Reihe nach:

1. **About**: Info-Icon (Topbar) → Modal zeigt
   - Name: OPN-Cockpit
   - Version: 0.6.0
   - Entwickler: Ludwig Systems
   - Kontakt: info@ludwig-systems.de
   - GitHub: ludwig-systems/opn-cockpit (Link klickbar)
   - Lizenz: Proprietary
2. **Audit-Log**: Topbar-Icon → Modal mit mindestens dem `VAULT_CREATED` und `VAULT_OPENED` Event.
3. **Audit-Verify**: Im Audit-Modal "Integrität prüfen" → grüne Statusbox.
4. **Gerät hinzufügen**: Sidebar → "Gerät hinzufügen". Beispiel:
   - Name: `Test-FW`, Host: `192.168.1.1`, Port: `443`, TLS-Verify aus, Tags: `test`
   - Speichern. Karte erscheint in der Inventar-Ansicht.
5. **Plausibilitätsprüfung**: Erneut "Gerät hinzufügen", Host `999.999.999.1`. Fehlermeldung erwartet, kein Speichern.
6. **Tresor exportieren**: Topbar-Download-Icon → "Backup jetzt herunterladen". Datei landet im Browser-Download.
7. **Tresor wechseln**: Topbar-Pfeil-Icon → Picker mit Recent-Liste. Test ohne tatsächlich zu wechseln.
8. **Sperren**: Topbar "Sperren". Login-Maske erscheint, erneut entsperren funktioniert.

**Pass**, wenn alle acht Punkte ohne Fehler oder UI-Hänger ablaufen.

### A.5 Migrations-Smoke (No-Op heute)

```powershell
# Normale PowerShell (nicht Admin):
& "$env:ProgramFiles\OPN-Cockpit\python\python.exe" -m opn_cockpit.cli migrate
```

**Pass**, wenn Ausgabe lautet "Keine Migration ausstehend." und Exit-Code 0.

Optional: `migrations.json` unter `%APPDATA%\OPN-Cockpit\` öffnen. Sollte `last_app_version: "0.6.0"` enthalten.

### A.6 Restart-Persistenz

- Browser-Tab schliessen, Launcher-Konsole beenden (Strg+C oder Fenster zu).
- Desktop-Verknüpfung erneut starten.
- Login-Maske erscheint, mit demselben Master-Passwort entsperren.

**Pass**, wenn das Test-Gerät aus A.4 immer noch in der Inventar-Ansicht steht.

### A.7 Deinstallation

- Systemsteuerung → Programme → OPN-Cockpit → Deinstallieren.
- `%ProgramFiles%\OPN-Cockpit\` wird entfernt.
- `%APPDATA%\OPN-Cockpit\` (Tresor + Audit) bleibt **stehen**.

**Pass**, wenn nach Reboot alles weg ist außer `%APPDATA%\OPN-Cockpit\` mit dem Tresor.

---

## B) Windows Multi-User-Server (Service-Mode)

Dieselbe Setup-EXE wie in A.1.

### B.1 Installation

- Setup-EXE als Administrator starten.
- Setup-Typ wählen: **"Multi-User-Server (Windows-Dienst, Autostart)"**.
- Defaults annehmen.
- Setup endet mit "OPN-Cockpit im Browser öffnen".

**Pass**, wenn der Dienst `OPN-Cockpit` in `services.msc` als "Wird ausgeführt"
gelistet wird und der Browser `http://localhost:9876` öffnet.

### B.2 Bootstrap-Token holen

```powershell
Get-Content "$env:ProgramData\OPN-Cockpit\logs\stderr.log" -Tail 20
```

Erwartung: Block mit `BOOTSTRAP-TOKEN` und einer Token-Zeile (URL-safe, ~30 Zeichen).

### B.3 Setup-Wizard

1. **Admin anlegen**:
   - Bootstrap-Token einfügen
   - Username: `admin`
   - Passwort min. 12 Zeichen
   - "Admin anlegen"
2. **Tresor entsperren**:
   - Token erneut aus den Logs holen (er rotiert nach Schritt 1)
   - Pfad ist vorausgefüllt (`...\firewalls.opnvault`)
   - "Tresor neu anlegen, falls die Datei nicht existiert" **ankreuzen**
   - Master-Passwort vergeben
   - "Tresor entsperren / anlegen"

**Pass**, wenn die Multi-User-Login-Maske erscheint und du dich mit `admin` + dem in Schritt 1 vergebenen PW einloggen kannst.

### B.4 Multi-User-Funktionen

1. **User-Verwaltung**: Topbar-Personen-Icon (nur Admins sichtbar) → "User hinzufügen".
   - Username: `viewer1`, Rolle: `viewer`, Passwort min. 12.
   - Speichern.
2. **Logout + Login mit viewer1**: Sperren → Multi-User-Login mit viewer1.
   - Erwartung: kein "Gerät hinzufügen"-Button (viewer-Rolle).
   - Audit-Log lesbar, kein "Integrität prüfen"-Button (admin-only).
3. **About-Modal**: funktioniert auch für viewer.

**Pass**, wenn die Rollen-Trennung sichtbar greift.

### B.5 Service-Restart-Persistenz

```powershell
Restart-Service -Name OPN-Cockpit
Start-Sleep -Seconds 5
Get-Service -Name OPN-Cockpit
```

**Pass**, wenn Status nach Restart `Running` ist und du dich erneut mit denselben Credentials einloggen kannst (kein Token nötig — Bootstrap ist abgeschlossen).

### B.6 Migrations + Backup-Smoke

```powershell
# Stoppen, "Update" simulieren (heute keine Schema-Aenderung → No-Op):
Stop-Service -Name OPN-Cockpit
& "$env:ProgramFiles\OPN-Cockpit\python\python.exe" -m opn_cockpit.cli migrate
# Erwartung: "Keine Migration ausstehend."
Start-Service -Name OPN-Cockpit
```

`%ProgramData%\OPN-Cockpit\backups\` sollte noch leer sein (heute hat keine Migration einen Backup-Bedarf gemeldet).

**Pass**, wenn der Dienst sauber stoppt + startet und keine Backup-Verzeichnisse fälschlich erzeugt werden.

### B.7 Deinstallation

- Systemsteuerung → OPN-Cockpit → Deinstallieren.
- Dienst wird automatisch entfernt (`uninstall-service.ps1` läuft).
- `%ProgramData%\OPN-Cockpit\` bleibt stehen.

**Pass**, wenn nach Reboot `Get-Service OPN-Cockpit` nichts findet, aber `%ProgramData%\OPN-Cockpit\firewalls.opnvault` noch da ist.

---

## C) Linux/Docker (Multi-User-Server)

### C.1 Bauen + Starten

```bash
cd /path/to/OPN-Deploy
docker compose build
docker compose up -d
docker compose logs -f opn-cockpit
```

**Pass**, wenn die Logs den Bootstrap-Token-Block zeigen und `/health` antwortet:

```bash
curl http://localhost:9876/health
# Erwartung: ok
```

### C.2 Bootstrap

- Browser auf `http://<docker-host>:9876`.
- Setup-Wizard wie unter B.3, Token aus `docker compose logs`.

**Pass**, wenn der Multi-User-Login erscheint und du eingeloggt bist.

### C.3 Smoke-Tests

Dieselben Punkte wie A.4 (About-Modal, Inventar, Audit, Export, Validierung).

### C.4 Volume-Persistenz

```bash
docker compose down                 # OHNE -v !
docker compose up -d
```

**Pass**, wenn nach `up -d` der Tresor noch existiert und du dich ohne Bootstrap einloggen kannst.

### C.5 Migrations-Check (No-Op)

```bash
docker compose exec opn-cockpit python3 -m opn_cockpit.cli migrate
# Erwartung: "Keine Migration ausstehend."
```

### C.6 Reverse-Proxy-Smoke (optional)

Wenn ein nginx/Caddy davor steht, prüfen ob `OPNCOCKPIT_HSTS_ENABLED=1` Header korrekt setzt:

```bash
docker compose exec opn-cockpit env | grep HSTS
```

### C.7 Aufräumen

```bash
docker compose down                 # Container weg, Volume bleibt
docker compose down -v              # Daten weg (Vorsicht!)
```

---

## D) Pass/Fail-Kriterien gesamt

Der Test gilt als **bestanden**, wenn:

- Beide Windows-Modi (Single + Service) installier- und nutzbar sind ohne System-Python
- About-Modal zeigt Version 0.6.0 + Entwickler-Info
- Audit-Log + Verify funktionieren in allen drei Setups
- Daten überleben Service- bzw. Container-Restart
- Deinstall/down löscht Code, lässt Daten stehen
- `cli migrate` läuft als No-Op (heute) und legt korrekt `migrations.json` an

## E) Update-Check (v6-Pass 3)

Beim ersten Anzeigen der Inventar-Ansicht ruft das Frontend `/api/updates/check`
auf. Wenn GitHub-Releases eine neuere Version meldet, erscheint am oberen Rand
ein dezenter Banner mit "Version X.Y verfügbar — Release-Notes" und einem
Dismiss-Button. Cache-Dauer: 24 h (per Env steuerbar).

### E.1 Banner-Smoke

- Nach Login mit der Inventar-Ansicht: F12 → Network-Tab → Reload.
- Erwartung: ein `GET /api/updates/check` mit Status 200.
- Body sollte `current_version: "0.6.0"` und `update_available: false` enthalten,
  solange GitHub keine `> 0.6.0`-Release fuer `ludwig-systems/opn-cockpit` hat.

### E.2 Cache-Datei

- Datei `<app_data>/update_check.json` sollte nach dem ersten Aufruf
  existieren — bei `update_available=false` enthaelt sie nur einen
  Zeitstempel + ETag, sonst auch `latest_version`.

### E.3 Opt-out für Offline-Installationen

```powershell
# Windows-Service-Mode:
& "$env:ProgramFiles\OPN-Cockpit\bundle\nssm.exe" set OPN-Cockpit AppEnvironmentExtra `
  "OPNCOCKPIT_UPDATE_CHECK_ENABLED=0"
Restart-Service -Name OPN-Cockpit
```

```bash
# Docker:
# in docker-compose.yml unter environment:
#   OPNCOCKPIT_UPDATE_CHECK_ENABLED: "0"
docker compose up -d --force-recreate
```

Nach einem Reload muss `/api/updates/check` mit `status: "disabled"` antworten
und kein Banner erscheinen.

### E.4 Dismiss-Verhalten

- Banner wegklicken (X).
- Browser-Tab neu laden.
- Erwartung: Banner bleibt weg, weil die abgewiesene Version in
  `sessionStorage` gespeichert ist. Nach Tab-Schließen + Wieder-Öffnen
  zeigt der Banner sich wieder.

## F) Bekannte Limitierungen v0.6.0

- **Code-Signing fehlt** — Windows SmartScreen warnt bei der Setup-EXE
  ("App schützen: Weitere Informationen → Trotzdem ausführen"). Erwartetes
  Verhalten bis ein Signing-Cert da ist.
- **Embedded-Python ist x64-only** — kein ARM64-Build dabei.
- **Frontend-Inline-Validierung** beim Tippen fehlt noch (Server-Antwort
  reicht heute).
- **Update-Check fragt GitHub anonym** — 60 Requests/h/IP. Reicht für ~24h-
  Cache und mehrere Browser-Tabs, aber bei sehr aggressivem manuellem
  `?force=true`-Aufruf kann das Rate-Limit greifen. Solange das Repo
  `ludwig-systems/opn-cockpit` noch kein Release auf GitHub hat, liefert
  der Check ohnehin "unknown" und der Banner bleibt weg.
- **Konsole bleibt im Single-Mode offen** — pip-Console-Subsystem,
  Schliessen = Server stoppen. Wechsel auf `gui_scripts` (windowless)
  ist Polish-Item für nächste Iteration.
- **Native Folder-Picker nur unter Windows** — auf Linux/Mac würde der
  Web-Picker als Fallback greifen (501 von /api/files/pick-folder).
- **Server-Stop = Konsole zu**. Wechsel zu Tray-Icon-Bedienung oder
  "Sperren = Stop"-Logik kommt später.

## G) Wenn etwas nicht passt

Bitte das Problem als kurze Notiz festhalten und beim nächsten Sync zeigen.
Wenn der Server gar nicht hochkommt:

- Windows Single-Mode: Launcher-Konsole zeigt den Stack-Trace
- Windows Service-Mode: `%ProgramData%\OPN-Cockpit\logs\stderr.log`
- Docker: `docker compose logs --tail=50 opn-cockpit`

Backup-Verzeichnisse falls vorhanden zur Diagnose mitschicken:
- Windows: `%APPDATA%\OPN-Cockpit\backups\` bzw. `%ProgramData%\OPN-Cockpit\backups\`
- Docker: `docker compose exec opn-cockpit ls /data/backups`
