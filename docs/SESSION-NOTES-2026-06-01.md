# Session-Notes 2026-06-01 — Test-Runden 1-10 + Linux/Proxmox

Sammlung aller Commits, Architektur-Entscheidungen und operationellen
Befunde des Tages. Nachfolger-Sessions starten hier statt das halbe
Tagebuch durchzuackern.

## Commit-Log (oben = neueste)

| Commit | Was |
|---|---|
| `6607b80` | feat(linux): Dual-Mode-Helper (CREATE auf PVE-Host, UPDATE im Container) |
| `8f9cd4e` | fix(uninstall): users.db wird auf Deinstallation entfernt |
| `c15c03e` | fix: Uninstall stoppt Service+Prozesse + Click-Bug Diagnose |
| `c16ef91` | fix(linux): drei Bugs im Proxmox-Helper (Storage-free-Anzeige, USB-statt-local, pveam-update) |
| `a782bf2` | feat(linux): Proxmox-Helper auf whiptail-TUI umgestellt |
| `0959b43` | feat(linux): Proxmox-Wizard auf Community-Scripts-Niveau (DHCP/Static, Gateway, DNS, VLAN, MAC) |
| `ed728ce` | feat(linux): F28-Update fuer Multi-User-Server + konfigurierbares Repo |
| `1f1e14e` | fix(F28): Default-Admin selbstheilend - Check per Username statt count() |
| `68ca8ad` | fix+style(ui): kürzerer Wizard-Text, größerer Header, Click-Bug defensive |
| `73bbfd1` | feat(F28)+fix: Default-Admin statt Bootstrap-Token + Vault-Upload + UX |
| `613bb8a` | fix+feat: Multi-User-Service-Install + Firmware-Version + Backup-Download |
| `bc46d2f` | feat(brand): Design-Guide + Kompass-Stern-Logo + Favicon (F7, F8) |
| `86f68c6` | fix(round3): Settings-Save 500, Datalist re-browse, Settings-Icon |
| `d3a3f23` | fix(round2): Alias-Append wirklich gefixt + Settings-Modal + Bulk-Beispiele |
| `9e8bc8d` | fix(ui+core): Sprint 1 + Alias/Routen-Save-Detection aus Test-Runde 1 |
| `f905ac4` | fix(vault): Parent-Verzeichnis automatisch anlegen |
| `3682ebf` | chore: docs/ aus Tracking nehmen (interne Arbeitsdokumente) |

## Wichtigste Architektur-Entscheidungen

### F28 — Bootstrap-Token raus, Default-Admin rein (`73bbfd1`)

User-Anweisung: „Lass das mit dem Bootstrap-Token, pragmatisch wie
Proxmox — Default-Admin + Pflicht-PW-Wechsel reicht völlig."

Komplett-Refactor:
- UserStore-Schema: `must_change_password` Spalte + ALTER-TABLE-Migration
- `ServerState._ensure_default_admin()` legt beim Erststart `admin` /
  `OPN-Cockpit!` mit `must_change_password=True` an. **In Runde 8 (`1f1e14e`)
  selbstheilend gemacht**: Check per `get_user_by_name("admin")` statt
  `count() > 0`. Wenn admin fehlt → neu anlegen. Wenn admin existiert
  (egal mit welchem PW) → unangetastet lassen.
- `POST /api/bootstrap/admin` → 410 Gone (Legacy-Hinweis)
- `POST /api/bootstrap/vault` kombiniert Admin-Login + PW-Wechsel +
  Vault-Unlock in einem Call. Payload: `admin_username, admin_password,
  [new_admin_password], vault_path, password, create_if_missing`
- Frontend: setup-admin-View entfernt, setup-vault-View hat zwei
  Sektionen in einem Step.
- 17 neue Tests in `test_bootstrap.py`

### Vault-Import via File-Upload (`73bbfd1`)

Multi-User-Server läuft als LocalService → sieht User-Pfade nicht.
Alter Pfad-basierter `/api/imports/vault`-Endpoint war strukturell
broken. Neu: `POST /api/imports/vault-upload` mit `UploadFile + Form`.
Browser uploaded `.opnvault`, Server liest Bytes via neuem
`vault.store.open_vault_bytes()`. Funktioniert in beiden Modi.

### Design-Guide (`bc46d2f`)

`docs/DESIGN-GUIDE.md` ist verbindliche Referenz für UI-Arbeit.
Memory-Eintrag `project_opn_cockpit_design_guide.md` zwingt Claude,
es vor UI-Arbeit zu lesen. Konkrete Schmerz-Stelle vorher: Nach
Compacts driftete die Linie weil Bahnschrift/Olive nicht im Kontext
war.

### Brand-Logo & Favicon (`bc46d2f`)

Kompass-Stern mit Olive-Pivot. SVG inline überall (Topbar 38px,
Boot 64px, Login 52px). Favicon mit prefers-color-scheme-Mediaquery.
Wahl durch User aus AskUserQuestion mit ASCII-Mockups.

### Multi-User Service-Install — Default-Admin-Anzeige (`613bb8a` -> `c15c03e`)

`install-service.ps1`: ASCII-only, defensive `Set-ExecutionPolicy
Bypass -Scope Process`, zeigt Default-Login in Konsole.
WinForms-Popup nach User-Feedback wieder rausgeworfen
(`68ca8ad`) — User/PW stehen ohnehin im Wizard sichtbar.

Pre-Uninstall (`c15c03e`):
- Neuer `scripts/stop-processes.ps1`: stoppt NSSM-Service mit 15s-
  Timeout-Loop + killt zusätzlich alle Prozesse aus `{app}\python\`
  (auch Single-User-Mode).
- ISS `[UninstallRun]` in 2 Stufen: stop-processes immer, uninstall-
  service nur bei Service-Komponente.
- Beide `waituntilterminated` → kein File-Lock-Rest.

Uninstall-Cleanup (`8f9cd4e`):
- `[UninstallDelete]` entfernt jetzt `users.db`, `BOOTSTRAP-TOKEN.txt`,
  `logs/` aus `%ProgramData%\OPN-Cockpit\`
- BLEIBT: `firewalls.opnvault`, audit/plans-DB, settings.json
- Folgefehler bei Re-Install: ohne diese Bereinigung schleppte der
  Default-Admin den alten User-DB-Eintrag mit (Login mit Default-PW
  ging dann nicht mehr, weil admin's PW schon geändert war).

### Proxmox-Helper — Community-Scripts-Niveau (`a782bf2`, `c16ef91`, `6607b80`)

Komplett-Refactor von simpler `read -rp`-Schleife auf `whiptail`-TUI:
- Storage-Pool: **Menü** (nur Pools mit `Content=Container`, mit „X GB
  frei / Y GB total")
- Bridge: **Menü** (alle erkannten vmbr*)
- IP-Modus: **Menü** (DHCP vs Statisch)
- Optional: VLAN, MAC, DNS-Server, Search-Domain (Inputbox, leer = aus)
- Validation pro Schritt (Zahlen, IPv4-Regex)
- Yes/No-Confirmation mit voller Zusammenfassung vor `pct create`
- Erfolgs-Msgbox + Konsolen-Ausgabe (URL bleibt anklickbar)

Bugs gefixt (`c16ef91`):
- Storage „free space" zeigte `Used` ($5) statt `Available` ($6)
- Template-Storage nahm USB statt local (Loop bevorzugt jetzt `local`)
- 404 beim Template-Download wegen stale Catalog (`pveam update` davor)

Dual-Mode (`6607b80`):
- Auf Proxmox-Host (pveam da) → CREATE-Wizard
- Im Container (`/opt/opn-cockpit` + systemd-Unit da) → UPDATE-Modus
- UPDATE: TUI zeigt explizit was angefasst wird (Code) vs unverändert
  bleibt (User-Daten in `/var/lib/opn-cockpit/`)
- git fetch + reset + pip install + Service-Restart
- Vorher/Nachher-Version + Service-Status im TUI-Abschluss
- Admin-Login bleibt erhalten (selbstheilende Default-Admin-Logik
  greift nur wenn `admin` fehlt)

## Test-Findings Stand

Volle Liste in [`docs/TEST-FINDINGS-2026-06-01.md`](TEST-FINDINGS-2026-06-01.md).

| Bereich | Status |
|---|---|
| F1-F18 Test-Runde 1 (Vault, Modals, Alias-Append) | alle ✅ |
| F19-F21 Test-Runde 2 (Datalist, Settings, Icon) | alle ✅ |
| F22-F28 Test-Runde 3 (Wizard, Header, F28-Bootstrap-Refactor) | alle ✅ |
| Click-Bug Header (Windows) | ✅ via defensive `pointer-events: auto !important` |
| Click-Bug Header (Linux/Proxmox) | ⚠️ noch zu verifizieren — `window.__opnDiag()` in Console |
| Uninstall lässt Files zurück | ✅ via stop-processes.ps1 + [UninstallDelete] |
| Re-Install schlägt Default-Login fehl | ✅ via users.db-Cleanup beim Uninstall |
| Proxmox-Wizard | ✅ TUI mit echten Menüs |
| Update-Mechanismus im Container | ✅ Dual-Mode-Helper |

## Operationelle Lehren

### PowerShell 5.1 + UTF-8

PS 5.1 liest `.ps1` als CP-1252, nicht UTF-8. Em-Dashes (`—`), Anfüh-
rungszeichen („") brechen den Parser. Alle ps1-Scripts müssen ASCII-
only sein. Em-Dash → `-`, deutsche Anführungszeichen → ASCII `"`.

Pattern in jedem ps1:
```powershell
try {
    Set-ExecutionPolicy -ExecutionPolicy Bypass -Scope Process -Force -ErrorAction Stop
} catch {}
```

Wirkt auch wenn Group Policy das `-ExecutionPolicy Bypass`-CLI-Flag
überstimmt (gepolizten Firmen-Maschinen).

### pveam Catalog ist stale

`pveam available` zeigt was im lokalen Katalog steht. Wenn der nicht
upgedatet wurde (oder lange nicht), liefert er Template-Versionen die
Proxmox vom CDN entfernt hat → 404 beim Download. **Immer `pveam
update` vor `pveam available`** im Setup-Skript.

### Storage-Pool-Auswahl per Frei-Text ist fehleranfällig

User-Test offenbarte: bei Frei-Text-Input nimmt das Skript auch
falsche Storages an. TUI-Menü mit echten Werten aus `pvesm status
-content <type>` ist die einzige robuste Lösung — gleich wie bei
community-scripts.org.

### Inno-Setup [UninstallRun] braucht `waituntilterminated`

Sonst läuft die Uninstall-Phase weiter während der Service noch
Dateien lockt → `%ProgramData%\OPN-Cockpit\` bleibt teilweise stehen.
Plus: stop-processes muss VOR uninstall-service laufen, sonst hängt
das remove am laufenden Process.

### Pfad-Trennung Code vs Daten = strukturelle Update-Sicherheit

Linux-Layout:
- Code in `/opt/opn-cockpit/` (git-tracked, wird upgradet)
- Daten in `/var/lib/opn-cockpit/` (systemd-HOME, bleibt unverändert)

Wenn beide ineinander wären, müssten wir bei jedem Update sorgsam
um User-Files herumarbeiten. So ist es einfach: git reset --hard fasst
NUR /opt/... an, /var/lib/... existiert in einem ganz anderen Pfad.

Windows-Pendant: Code in `C:\Program Files\OPN-Cockpit\`, Daten in
`C:\ProgramData\OPN-Cockpit\`. Selbes Prinzip.

## Wie du jetzt testest

### Windows-Test (Multi-User, frische Installation)

1. Existierende Installation deinstallieren („Apps & Features")
2. `%ProgramData%\OPN-Cockpit\` jetzt auch komplett weg? Sollte nach
   `8f9cd4e` zumindest `users.db` weg sein.
3. Frische [`Install-OPN-Cockpit-0.6.0.exe`](../installer/out/Install-OPN-Cockpit-0.6.0.exe)
   doppelklicken → **Multi-User-Server** wählen → durchklicken
4. Browser auf `http://localhost:9876`
5. Default-Login `admin` / `OPN-Cockpit!` → PW-Wechsel + Vault-Setup
6. Im Inventar: Header-Icons + Sidebar-Filter mit der Maus klicken
7. Falls Click-Bug: F12 → Console → `window.__opnDiag()` → Output rüber

### Linux/Proxmox-Test

```bash
# Auf Proxmox-Host als root — CREATE-Modus
bash -c "$(wget -qLO - https://raw.githubusercontent.com/ludwig-systems/opn-cockpit/main/installer/linux/proxmox-helper.sh)"

# Im Container (per pct enter oder SSH) — UPDATE-Modus
bash -c "$(wget -qLO - https://raw.githubusercontent.com/ludwig-systems/opn-cockpit/main/installer/linux/proxmox-helper.sh)"
```

Voraussetzung beider: der Code muss auf GitHub gepusht sein.

## Was zu pushen ist

4 lokale Commits nicht auf `origin/main`:

```
bc8a8e6 chore(installer): Em-Dashes aus ISS raus (CI-Safety)
6607b80 feat(linux): Dual-Mode-Helper (CREATE auf PVE-Host, UPDATE im Container)
8f9cd4e fix(uninstall): users.db wird auf Deinstallation entfernt
c15c03e fix: Uninstall stoppt Service+Prozesse + Click-Bug Diagnose
```

Befehl (vom User auszuführen):

```powershell
cd g:\OPN-Deploy
git push origin main
```

**Wichtig**: Remote-URL wurde während der Session von
`OPN-Deploy.git` (mit Großschreibung wie der lokale Ordnername) auf
`opn-cockpit.git` umgeschrieben. Wenn das Repo auf GitHub anders heißt
oder noch nicht existiert:

- Bei GitHub das Repo `ludwig-systems/opn-cockpit` anlegen (Public oder
  Private, README **nicht** initialisieren — sonst macht der erste
  Push ärger)
- Oder: alternative Remote-URL setzen via `git remote set-url origin
  <url>` und dann pushen

## Offene Punkte

### Click-Bug Linux/Proxmox

Auf Windows mit der `pointer-events: auto !important`-Defensive ist
der Bug weg. Auf Linux/Proxmox berichtete User dass er bestand. Lass
das mit `window.__opnDiag()` aus der Browser-Console diagnostizieren:

```javascript
// F12 → Console → eintippen + Enter
window.__opnDiag()
```

Output zeigt Vollflächen-Overlays (falls vorhanden) und welches DOM-
Element unter dem ersten Topbar-Icon liegt. Damit ist die Ursache
identifizierbar.

### Linux-Test ausstehend

- `proxmox-helper.sh` CREATE-Modus auf gepushtem main
- UPDATE-Modus im Container (Code-Pull + Restart, Daten bleiben)
- Backup-Snapshot in `/var/lib/opn-cockpit/backups/` verifizieren bei
  künftiger Schema-Migration

### GitHub-Actions Release-Workflow scheitert

User-Befund: `.github/workflows/release.yml` wirft jedes Mal einen
Fehler. Ohne Log-Zugriff nur Vermutung — wahrscheinlichste Kandidaten:

1. **Em-dashes in `.iss`-Comments** → ISCC liest die Datei in
   System-Codepage, UTF-8-Multi-Bytes können Compiler verwirren.
   **Präventiv gefixt in `bc8a8e6`** (alle em-dashes auf ASCII `-`).
2. **Pip-Install im Embedded-Python**: `pip install --no-build-isolation .`
   in `bundle-python.ps1` kann fehlen wenn ein Wheel für die exakte
   Python-Version nicht verfügbar ist (cryptography, argon2-cffi
   brauchen oft pre-built wheels).
3. **NSSM-Download** (`https://nssm.cc/release/nssm-2.24.zip`): wenn
   nssm.cc gerade down ist → step failt mit Connection-Error.
4. **`choco install -y innosetup`**: choco kann zeitweise instabil sein.

Was du für die Diagnose brauchst:

1. Auf GitHub → Actions-Tab → fehlgeschlagenen Run öffnen
2. Roten Step aufklappen, erste rote Zeile kopieren
3. Mir die Zeile schicken — dann gezielter Fix

Plus: `bundle-python.ps1` wird im Workflow OHNE `-Force` aufgerufen.
Auf frischen CI-Runnern egal (kein Cache), aber wenn du das lokal
debuggst, brauchst du `-Force` um sauber durchzulaufen.

### Strategische TODOs

Aus früheren Sessions noch nicht erledigt:
- F7 Design-Guide ✅ erledigt heute
- F8 Logo/Favicon ✅ erledigt heute
- Code-Signing-Zertifikat (SmartScreen-Warning) — extern (SSL.com ~$250/Jahr)
- Signierter PDF-Audit-Report — CSV-Export existiert
- Frontend-Inline-Validation beim Tippen

Aus `Feature-Requests`-Datei (Parking-Lot, nicht aktiv):
- OPNsense-Version pro Karte ✅ erledigt heute (`613bb8a`)
- Backup-Download pro Karte ✅ erledigt heute (`613bb8a`)
- Gateway/NTP/CARP/Interface-Status — offen
- Firewall-Regeln Plan/Apply — offen (groß)
- Unbound-DNS-Forwarder — offen
- Firmware-Update auslösen — offen (UX-heikel)
- Sammelaktion Firmware-Update für Tag-Gruppe — offen

## Ablage

- **Build-Artefakte**: `installer\out\Install-OPN-Cockpit-0.6.0.exe`
- **Bundle (lokal, gitignored)**: `installer\bundle\python\`
- **Linux-Scripts**: `installer\linux\` (gepusht via Git)
- **Doku**:
  - [docs/DESIGN-GUIDE.md](DESIGN-GUIDE.md) — verbindliche UI-Linie
  - [docs/TEST-FINDINGS-2026-06-01.md](TEST-FINDINGS-2026-06-01.md) — alle Findings
  - [docs/SESSION-NOTES-2026-06-01.md](SESSION-NOTES-2026-06-01.md) — diese Datei
  - [docs/TESTPLAN-0.6.0.md](TESTPLAN-0.6.0.md) — End-to-End-Plan
- **Memory** (lokal, `C:\Users\whooz\.claude\projects\g--OPN-Deploy\memory\`):
  - `project_opn_cockpit_v2_status.md` — Iterations-Tracker
  - `project_opn_cockpit_roadmap.md` — Strategie + offene Items
  - `project_opn_cockpit_design_guide.md` — Verweis auf docs/DESIGN-GUIDE.md
