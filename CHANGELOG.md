# Changelog

Alle nennenswerten Änderungen pro Release.

## v0.7.0 — 2026-06-03 — Safety-Nets, Multi-Site-Tools, Windowless-Install

Großer Funktions-Schub rund um die Themen "ich will sehen was passiert
bevor es schiefgeht" (Safety-Nets), "ich will meine N gleichen Boxen
synchron halten" (Multi-Site-Tools) und "der Single-User-Desktop soll
nicht mit Konsolen-Fenster nerven" (Windowless-Install).

### Safety-Nets

- **Auto-Backup vor Apply** (v0.7 #2): Der Executor zieht vor jedem
  schreibenden Apply automatisch ein gzip-Backup pro Gerät. Scheitert
  das Backup, wird der Apply auf diesem Gerät blockiert (Audit:
  `backup_blocked`). Retention 30 (default).
- **Cert-Ablauf-Badge** (v0.7 #3): Karten zeigen gelben Badge bei
  Cert-Restlaufzeit < 30 Tagen, rot bei < 7 Tagen. Klick öffnet
  Cert-Detail-Liste pro Gerät mit Aussteller, IN-USE-Marker.
- **Scheduled Auto-Backup** (v0.7 #4): Hintergrund-Thread zieht im
  konfigurierten Intervall (Default 24h) ein Backup pro Gerät. Eigener
  Retention-Pool, separat vom Pre-Apply. Default AUS (Opt-In).
- **Config-Drift-Erkennung** (v0.7 #5): Vergleich der Live-OPNsense-
  Config-Hashes (volatile `<revision>`-Blöcke gestripped) gegen das
  letzte lokale Backup. Drift-Badge auf der Kachel wenn ungleich.
  Default AUS.

### Multi-Site-Tools

- **Config-Compare zwischen N Geräten**: "Vergleichen"-Button erscheint
  ab 2 selektierten Geräten. Matrix zeigt Aliase pro Gerät mit Status
  (present/absent/drift/unreachable). Drift-Zeilen gelb markiert.
  Erste Iteration: nur Aliases; Routes/Firewall-Rules folgen.
- **Alias-Sync (Master → Targets)**: In jeder Drift-Row gibt es einen
  "Sync…"-Button. Wählt einen Master (bei mehreren Varianten via
  Prompt), erzeugt einen add_alias-Plan für alle anderen Geräte der
  Zeile. Springt nach Erfolg in den Plan-View.
- **Alias-Manager-View** (read-only): Pro Gerät eine durchsuchbare
  Liste aller Aliase mit Inhalt, Beschreibung, Deep-Link zur OPNsense-
  Bearbeitung. Vorbereitung für künftigen Edit/Delete-Adapter.
- **Auto-Retry-Queue für Mobile-Racks**: Wenn Geräte beim Apply als
  FAILED zurückkommen, schedult Cockpit automatisch einen Retry-Job.
  Default-Wartezeit 7 Tage, Intervall 5 Min. Sobald die Box wieder
  erreichbar ist, zieht der Watcher den Plan ohne weiteres Zutun nach.

### Diagnose-Verbesserungen

- **TCP-Timeout-Aufsplittung**: `connect_timeout` vs `read_timeout`
  in `error_kind` und Frontend-Anzeige. Halbiert Diagnose-Suchfeld.
- **ICMP-Probe-Fallback** bei `connect_timeout`: Wenn die Box per Ping
  erreichbar ist aber TCP nicht, sagt Cockpit jetzt "Host antwortet
  auf Ping, aber Port X ist zu — Firewall/Routing/Service-Status
  prüfen". Spart bei asymmetrischen Routing-Setups Stunden Diagnose.

### Installer + Setup

- **Windowless Single-User-Install (Windows)**: Desktop-Shortcut
  nutzt jetzt `opn-cockpitw.exe` (= pythonw.exe-Kopie). Kein
  schwarzes Konsolen-Fenster mehr beim Doppelklick. Logs landen in
  `%APPDATA%\OPN-Cockpit\logs\opn-cockpit.log` (Auto-Redirect bei
  `sys.stdout==None`, naive Rotation bei >5 MiB).
- **Uninstaller-Sanierung**: Service wird jetzt sauber entfernt
  (nicht nur gestoppt), bundle/python/scripts werden zuverlässig
  geräumt, kein Lock-out-Problem mehr beim Re-Install.
- **Proxmox-Helper-Wizard**:
  - Getrennte und sprechende Fragen für Container-Rootfs-Storage und
    Template-Storage (vorher mehrfach verwechselt)
  - Locale wird auf `C.UTF-8` gesetzt → keine `apt-listchanges`-
    Warnings mehr während des Installs
  - Container-Notes-Feld wird automatisch mit Login + Update-One-Liner
    + GitHub-Links befüllt (Markdown-gerendert in Proxmox-UI)
  - Deutlicher Hard-Reload-Hinweis am Ende des Updates

### UX-Politur

- **Nativer Windows-Datei-Dialog im Tresor-Switch-Modal**: "Datei
  suchen..."-Button öffnet `comdlg32.GetOpenFileNameW` auf dem Server
  (Single-User-Mode + Windows). Single-User-Vault-Path-Restriktion
  entfernt → USB-Sticks etc. funktionieren ohne Konfiguration.
- **API-Key sichtbar im Edit-Dialog** zur Verifikation (Secret bleibt
  maskiert).

### Bug-Fixes

- **Cert-Inventur**: `/api/trust/cert/search` liefert nur Metadaten,
  Validity-Felder kamen leer durch. Fix: pro Cert `/cert/get/<uuid>`
  nachschieben und PEM lokal mit `cryptography.x509` parsen.
- **TLS-Verifikations-Fehler** als eigene Kategorie statt Netzwerk-
  Fehler (vorher: "nicht erreichbar: network"; jetzt: "TLS-Verifikation
  fehlgeschlagen: Cert ungültig").
- **Hostname-Validierung** akzeptiert ganze URLs (`https://host.lan/`
  → `host.lan`) und interne TLDs (`.lan`, `.local`).
- **Whitespace im API-Secret** wird beim Speichern getrimmt.
- **Plan-Cancel räumt Plan auf**: "Plan verwerfen"-Button auf Vorschau
  + automatisches Aufräumen beim Abbrechen aus der Bearbeitungs-Phase.

### Internal

- `BackupScheduler` (web/backup_scheduler.py) als neuer Daemon-Thread,
  Pattern analog zum bestehenden RetryWatcher.
- `core/config_drift.py` + `core/config_compare.py` als neue Module
  für die XML-Hash- bzw. Matrix-Logik.
- `SessionManager.snapshot_active()` für den Scheduler-Tick.
- Neue AuditEventKinds: `SCHEDULED_BACKUP`.
- VaultSettings erweitert um 7 neue Felder (alle mit Defaults für
  Backwards-Compat mit alten Tresoren).
- `iputils-ping` im Linux-Installer (für ICMP-Probe).

---

## v0.6.0 — 2026-06-01 — Multi-User-Server + Linux-Deployment

Erste Release-Version mit echter Mehr-Plattform- und Mehr-Nutzer-Auslieferung.
v0.6.0 hat denselben Funktionsumfang wie v2.0 (intern), läuft aber jetzt in
vier produktiv nutzbaren Varianten: Windows-Single-User, Windows-Multi-User-
Server (NSSM-Dienst), Linux-Server (systemd) und Proxmox-LXC. Erster
Public-Release auf GitHub.

### Authentifizierung — Default-Admin statt Bootstrap-Token

- Beim ersten Start legt der Server automatisch einen Default-Admin an
  (`admin` / `OPN-Cockpit!`) mit Pflicht-Passwort-Wechsel beim ersten Login.
  Pragmatisch wie Proxmox: kein Token-Kopieren mehr, keine Token-Datei,
  keine Setup-Schritte „erster Boot wartet auf Konsolen-Output".
- `users.db`-Schema versteht `must_change_password` (SQLite `ALTER TABLE`-
  Migration läuft beim Boot).
- Selbstheilender Check via `get_user_by_name` statt `count()` — bei
  zerstörter User-Tabelle wird der Default-Admin neu angelegt.
- Tresor-Operationen sind blockiert, solange das Default-Passwort gilt
  (Server-Status `ready_with_default_password`).
- Bootstrap-Vault-Endpoint kombiniert Login + PW-Wechsel + Vault-Setup
  in einem Schritt — der Setup-Wizard zeigt nur noch **einen** Step.

### Multi-User-Server (Windows + Linux)

- **Windows**: Installer-Wizard hat neuen Komponententyp „Multi-User-Server".
  Registriert OPN-Cockpit als NSSM-basierten Windows-Dienst mit Autostart,
  bindet auf `0.0.0.0:9876`. Service läuft unter `LocalService`. Vault-Upload
  via `multipart/form-data` statt Pfad-Eingabe (LocalService kann User-
  Pfade nicht sehen).
- **Linux/Debian**: `installer/linux/install.sh` legt System-User
  `opncockpit` an, baut Python-venv unter `/opt/opn-cockpit`, packt Daten
  nach `/var/lib/opn-cockpit`, aktiviert systemd-Unit mit Hardening-Flags
  (NoNewPrivileges, ProtectSystem=strict, PrivateTmp, ProtectHome).
- Beide Modi nutzen den Default-Admin-Flow.

### Proxmox-LXC-Helper (whiptail-TUI im Community-Scripts-Stil)

- `installer/linux/proxmox-helper.sh` — ein Befehl auf dem PVE-Host:
  ```bash
  bash -c "$(wget -qLO - https://raw.githubusercontent.com/ludwig-systems/opn-cockpit/main/installer/linux/proxmox-helper.sh)"
  ```
- **Dual-Mode**: derselbe Link funktioniert auf PVE-Host (Container anlegen)
  UND im Container (in-place Update). Erkennung über `pveam`-Vorhandensein
  bzw. `/opt/opn-cockpit + systemd-Unit`.
- TUI führt durch Container-ID, Hostname, Storage-Pool (Auswahl-Menü aus
  `pvesm status -content rootdir`), Disk-Größe, CPUs, RAM, Bridge (Menü
  aller `vmbr*`), DHCP/Statisch, IPv4/Gateway/VLAN/MAC/DNS — alles per
  Pfeiltasten + Enter, kein Frei-Text mehr.
- Update-Modus belässt `/var/lib/opn-cockpit/` (Vault, Audit, User-DB,
  Settings) unangetastet; nur `/opt/opn-cockpit` wird via `git fetch +
  reset --hard` + `pip install` aktualisiert. Migrations laufen beim
  Service-Start mit Pre-Backup.

### Brand, UI und Design-Guide

- Kompass-Stern-Logo als Inline-SVG überall (Boot-Splash, Header, Login,
  About-Modal); Favicon mit `prefers-color-scheme`-Variante.
- Header höher (`topbar-height: 82px`), Brand-Logo 38 px, Headline 24 px,
  Icon-Buttons 40 px — proportional skaliert.
- Interner Design-Guide als verbindliche Referenz für künftige UI-Arbeit
  (Calm-Precision + Bahnschrift-Display + Olive-Akzent).
- Defensive CSS-Regeln gegen Click-Bug auf Proxmox-Browser-Stack
  (`pointer-events: auto !important` auf interaktive Elemente,
  `[hidden] { display: none !important; }`).
- Diagnose-Helper `window.__opnDiag()` in der Browser-Konsole für
  Click-Bug-Reports (listet fullscreen overlays + Element unter erstem
  Topbar-Icon).

### Features (aus FR-Liste umgesetzt)

- **OPNsense-Firmware-Version** pro Karte (`/api/core/firmware/status`)
  mit Caching pro Heartbeat-Intervall.
- **Backup ziehen** pro Karte (`/api/core/backup/download/this`) als
  direkter Download-Stream; neuer Audit-Event `BACKUP_DOWNLOADED`.

### Release-Pipeline

- GitHub-Actions-Release-Workflow gefixt: PowerShell-Here-String im
  YAML-`run: |`-Block sorgt für Indentation-Konflikt und Parse-Failure.
  Ersetzt durch String-Array + `-join "`n"`. Em-Dashes aus ISS-Kommentaren
  und Workflow-Inputs raus (CI-Sicherheit gegen non-UTF-8-Encoding bei
  alten Toolchains).
- `installer/opn-cockpit.iss` `[UninstallRun]` in 2 Stufen mit
  `waituntilterminated`: Pre-Step killt laufende Prozesse (Service +
  Single-User), zweiter Step entfernt NSSM-Service.
- `[UninstallDelete]` entfernt zusätzlich `users.db`, `BOOTSTRAP-TOKEN.txt`,
  `logs/` aus `%ProgramData%\OPN-Cockpit` — Re-Install bekommt sauberen
  Default-Admin-State.

### Operationelle Lehren

- PowerShell 5.1 liest .ps1-Files als CP-1252; Em-Dashes (`—`) brechen
  den Parser. **Alle Scripts ASCII-only.**
- Defensives `Set-ExecutionPolicy Bypass -Scope Process` oben in jedem
  Script gegen Group-Policy-Sperren.
- `pveam update` vor `pveam available` — sonst veralteter Katalog → 404.
- Vault-Upload für Multi-User-Server: Path-Validator kann User-Verzeichnis
  des aufrufenden Browsers nicht sehen, also wird die `.opnvault`-Datei
  per Multipart-Upload an den Server gestreamt und über `open_vault_bytes`
  entschlüsselt.

### Tests + Migration

- 580+ Tests grün (`tests/unit/`), 100 % Coverage im Web-Layer.
- SQLite-Migration-Framework läuft beim ersten Boot jedes Updates,
  schreibt Pre-Backup nach `/var/lib/opn-cockpit/backups/<ts>-pre-<v>/`.

---

## v2.0.0 — Web-Pivot

Komplette Umstellung der Präsentations-Schicht von PySide6-Desktop-GUI auf
lokale **FastAPI + Web-Frontend**. Core, Orchestrierung, Vault, Audit
bleiben unverändert. User-Entscheidung nach Mockup-Vergleich zugunsten
einer publikations-tauglichen Optik („Calm Precision"-Aesthetik).

### Iterations-Plan

- ✅ **Iter 1** (`d0743d2`): FastAPI-Backend-Skeleton, uvicorn-Boot,
  Browser-Auto-Open auf 127.0.0.1:9876, `/health` + `/api/version`,
  Boot-Splash.
- ✅ **Iter 2** (`582df22`): Auth-Flow (`POST /api/auth/unlock|lock`,
  `GET /api/auth/me`), Vault-Discovery + Inline-Create
  (`GET/POST /api/vaults`), Login-UI mit Tresor-Picker, Bearer-Token-
  Session in `sessionStorage`, 30 s-Expiry-Ticker mit Auto-Lock-UI.
- ✅ **Iter 3**: Inventar-Kachelansicht aus dem Mockup geliftet:
  Sidebar mit Tag-Filter + Aktionen, Karten-Grid mit Status-Dot, TLS-
  Badge und Heartbeat-Label, Topbar-Suche (Strg K), Add-/Detail-Modal.
  Backend: `GET /api/inventory`, `POST /api/inventory/devices`,
  `DELETE /api/inventory/devices/{id}`, `POST /api/inventory/heartbeat`
  (paralleler TCP-Probe ohne Auth-Last auf der OPNsense),
  `POST /api/inventory/devices/{id}/test-connection` (vollwertiger
  HTTPS-Auth-Probe).
- ✅ **Iter 3.1** (Polish + UX-Feedback):
  - Master-Passwort wird nur einmal beim Unlock erfragt und in der
    Session gecached — Add/Remove brauchen es nicht mehr. Der Cache
    lebt nur während der Session und wird beim Lock/Auto-Lock
    überschrieben. Spiegelt das Vertrauensmodell „erfahrene Admins,
    Schutz vor unbefugtem Zugriff, nicht Schutz vor sich selbst".
  - Farbpalette von Sage- auf Oliv-/Militärgrün gewechselt, Kontrast in
    beiden Themes deutlich erhöht (Text-Hierarchie, Borders, Karten-
    Stats jetzt klar lesbar).
  - Detail-Modal mit Buttons „OPNsense öffnen" (neuer Tab auf
    `https://host:port/`), „Duplizieren" (öffnet Add-Modal mit
    vorausgefüllten Feldern, API-Key/Secret bleiben leer) und einem
    2-Klick-Lösch-Pfad mit Puls-Animation.
  - Externer-Link-Icon auf jeder Karte für den direkten OPNsense-
    Webaufruf.
- ✅ **Iter 4** (`55d3925`): Plan/Apply für Routen + Aliase via Web — drei-
  stufiges Modal (Eingabe → Vorschau mit Confirm-Gate → Result-Matrix),
  Multi-Device-Picker, Bulk-Plan-Support, Cross-Tool-Sharing mit der CLI
  über denselben PlanStore.
- ✅ **Iter 5a** (`daf8144`): Audit-View. Topbar-Icon öffnet ein Modal mit
  Filter-Reihe (Event-Kind / Action / Geräte-ID), Truncate-Hinweis,
  Status-Pills semantisch eingefärbt.
- ✅ **Iter 5b** (`27dad8c`): Discovery — `/api/discover/...` für Gateway-
  und Alias-Namen aus der laufenden OPNsense. Frontend integriert das als
  Lazy-Load-Datalist im Plan-Modal („Vorschläge laden"-Button), nicht
  automatisch beim Modal-Öffnen wegen Offline-Geräten.
- ✅ **Iter 5c** (`c465a03`): Profile (Templates) — Vorlagen-Dropdown im
  Plan-Modal + „Als Vorlage speichern". Secrets werden beim Speichern
  via Whitelist sanitisiert, gemeinsamer Store mit der CLI.
- ✅ **Iter 5d** (`723044a`, später ersetzt durch 5.1): erste Bulk-Import-
  Variante für Routen-CSV / Alias-JSON.
- ✅ **Iter 5.1** (`59e73e8`): Architektur-Refactor nach User-Feedback —
  Bulk-Import sind jetzt **Firewall-Stammdaten** (CSV/JSON), nicht mehr
  Routen/Aliase. Karten kriegen sichtbare Auswahl-Checkbox mit Olive-
  Highlight. Neue Selection-Bar über dem Grid mit „Alle / Nur erreichbare
  / Keine". Plan-Modal hat keinen eigenen Device-Picker mehr — Aktionen
  laufen gegen die globale Karten-Selektion. Karten-Padding angepasst,
  damit die Auswahl-Checkbox die TLS- und URL-Icons nicht überdeckt
  (`021be1f`).
- ✅ **Iter 5.2** (`c0bf7b3`): Apply-Reports werden persistiert, neuer
  Endpunkt `/api/plans/outstanding`, Retry-Pfad mit `device_ids`-Filter
  im Apply-Body. Karten zeigen einen Amber-„N offen"-Badge, wenn Geräte
  in alten Plänen noch nicht Verifiziert sind. Result-Phase bietet
  „N fehlgeschlagene erneut versuchen". Verlustarme Auto-Recovery für
  „eine Box war offline"-Szenarien.
- ✅ **Iter 6**: PySide6 + alle GUI-Tests entfernt, README + QUICKSTART
  auf Web-First umgeschrieben, Inno-Setup-Skript in `installer/`
  abgelegt, CHANGELOG abgeschlossen.

### Architektur-Entscheidungen

- **Multi-User-fähig vorbereitet**: SessionManager mappt Token →
  Session-Objekt; spätere User-DB hängt sich ohne Schema-Bruch ein.
  TLS-Felder in `WebSettings` für späteren Server-Modus vorhanden.
- **Vanilla HTML/CSS/JS** ohne Build-Pipeline. Frontend ist eine
  Single-Page-State-Machine (boot/login/main) in `web/static/app.js`.
- **API-Schemas** zentral in `web/api/schemas.py` (Pydantic).
- **Token in sessionStorage** (per-Tab), nicht localStorage, kein
  Cookie → CSRF nicht relevant.

### Dependencies

Hinzugefügt: `fastapi >= 0.115`, `uvicorn[standard] >= 0.32`,
`jinja2 >= 3.1`, `python-multipart >= 0.0.12`. `PySide6` bleibt
vorerst installiert, wird in Iter 6 entfernt.

### Tests-Stand v2.0-Final

550+ Tests grün (130+ im `tests/unit/web/`-Tree). PySide6-Tests komplett
entfallen. ruff + mypy strict clean. 81 Source-Files in `src/`.

---

## v1.2.0 — 2026-05-28 — GUI-first Boot-Flow

Letzte PySide6-Iteration. Boot-Flow ohne Shell:
- `vault/discovery.py` scannt `%APPDATA%/OPN-Cockpit/*.opnvault` +
  Recent-Vault-Liste der App-Settings.
- Neuer `CreateVaultDialog` für den Erst-Setup.
- `LoginDialog`-Rewrite mit ComboBox je nach Treffer-Anzahl
  (0 Tresore → Hinweis, 1 → vorausgewählt, >1 → Auswahl).
- `start.bat` + `start.ps1` als Doppelklick-Launcher.

12 neue Tests, 459 Tests gesamt.

---

## v1.1.0 — 2026-05-28 — Heartbeat + API-Discovery

### Hinzugefügt

- **TCP-Reachability-Heartbeat im Inventar**: Pro Gerät zeigt die GUI ein
  Pünktchen (grün/rot/gelb/grau) für den letzten TCP-Connect-Status auf
  den API-Port an. Im Hintergrund alle 30 s automatisch aktualisiert,
  manueller Refresh über den "Heartbeat jetzt prüfen"-Button.
  Bewusst kein ICMP-Ping (Windows-Admin-Rechte + Firewall-Probleme) —
  TCP auf den Konfig-Port ist der ehrlichste Erreichbarkeits-Indikator,
  weil es exakt der Pfad ist, den das Tool auch zum Schreiben benutzt.
- **API-Discovery für Gateways und Aliase**: Neues `core/discovery`-Modul
  mit `list_gateways(client, target, key, secret)` und
  `list_aliases(client, target, key, secret)`. Defensiv gegen
  Schema-Drift — unbekannte Antwort-Formate führen zu leerer Liste,
  nur HTTP-Fehler wandern als `DiscoveryError` zum Aufrufer.
- **CLI**: `discover gateways --target id:X` und
  `discover aliases --target id:X` listen die vorhandenen Namen
  tabellarisch.
- **GUI**: Action-Dialoge für Route und Alias bieten optionale
  "Vorschläge laden"-Buttons. Bei Klick erscheint die ComboBox mit
  den Namen vom ausgewählten Referenz-Gerät — Tippfehler bei
  case-sensitiven Gateway-Namen (`V2_WANBwIn` vs `v2_wanbwin`)
  werden so deutlich seltener.

### Geändert

- `RouteAdapter` und `AliasAdapter` blieben unverändert — Discovery
  arbeitet außerhalb der bestehenden Adapter und nutzt die bekannten
  Endpoints.
- Action-Dialoge sind rückwärtskompatibel: ohne injizierte Callbacks
  verhalten sie sich wie in v0.1.0 (reine Freitext-Eingabe).

### Tests

- 447 Tests (32 neu vs v0.1.0), Core 91–100 %, ruff & mypy strict clean.

## v0.1.0 — 2026-05-28 — Erste lauffähige Version

### Hinzugefügt

- **Tresor-Modell** (`vault/`): `.opnvault`-Datei im KeePass-Stil mit
  Argon2id-KDF (RFC 9106 Empfehlung) + AES-256-GCM. Geräte-Inventar
  und API-Credentials liegen gemeinsam verschlüsselt auf Platte.
- **Master-Passwort** mit Mindestlänge 12 Zeichen.
- **Template-Export**: `.opnvault`-Kopie mit geleerten Secret-Feldern
  zum Weitergeben an andere Admins.
- **Plan/Apply-Muster** (Terraform-Stil): Aktionen werden erst als
  Vorschau (Plan) generiert und persistiert, dann nach expliziter
  Bestätigung (`ja`) ausgerollt.
- **Phasen-Pipeline pro Gerät**: WRITE → ACTIVATE (genau ein
  `reconfigure`) → VERIFY (Read-back).
- **Read-back-Verifikation**: Erfolg gilt nur, wenn der Such-Endpunkt
  den geschriebenen Eintrag tatsächlich zurückgibt — **nicht** an der
  `add`-Antwort.
- **Best-Effort-Rollout**: Geräte-Fehler blockieren die übrigen nicht;
  parallelisierter ThreadPool, Worker-Anzahl im Tresor konfigurierbar.
- **Egress-Allowlist**: `http_client` lehnt jeden Request gegen
  Hosts ab, die nicht im Inventar stehen.
- **Audit-Log** (`audit/`): append-only JSON Lines unter
  `%APPDATA%\OPN-Cockpit\audit.jsonl`. Whitelist verhindert Drive-by-
  Leaks; sensitiv klingende Schlüssel werden vor dem Schreiben durch
  `mask_dict` gefiltert.
- **`MaskedStr`** als eigener Typ (kein `str`-Subclass): überschreibt
  `__str__`/`__repr__`/`__format__` und verhindert f-string-/`%s`-Leaks.
- **CLI** (`cli/`):
  - Vault-Wartung: `create-vault`, `change-password`, `export-template`
  - Inventar: `list-devices`, `add-device`, `remove-device`
  - Verbindungstest: `test-connection --target SELECTOR`
  - Plan: `plan add-route`, `plan add-alias`, `plan append-alias`
  - Apply: `apply PLAN_ID_OR_PATH`
  - Audit: `audit --event --action --device-id --since --until --limit`
  - Profile: `profile list/save-route/save-alias/apply/delete`
  - Bulk: `bulk-import routes FILE`, `bulk-import aliases FILE [--append]`
- **GUI** (`gui/`, PySide6):
  - Login-Dialog mit Tresor-Pfad-Auswahl
  - Hauptfenster mit Tabs (Inventar, Audit-Log)
  - Action-Dialoge für Route, Alias, Device
  - Plan-Vorschau-Dialog mit Bestätigungs-Checkbox
  - Result-Matrix nach Apply
  - Inaktivitäts-Timer mit Auto-Sperre
  - `sys.excepthook`, der Tracebacks vor Output maskiert
  - TLS-Verify-Off-Risiko-Badge je Gerät
- **Selektor-Sprache**: `all`, `tag:X`, `group:X`, `id:X`, `name:X`,
  Komma-getrennte Union, case-insensitive.
- **Profile/Templates** (`profiles/`): JSON-Storage für wiederverwendbare
  Aktions-Vorlagen. Sanitizer entfernt versehentlich geratene
  Secret-Felder beim Speichern UND Laden.
- **Bulk-Import** (`importers/`): CSV-Routen (Header `network`,
  `gateway`, `descr`, `disabled`) und JSON-Aliase. Validiert
  zeilenweise, bricht nicht beim ersten Fehler ab.
- **Subsystem-Registry**: Erweiterbarkeit für künftige Objekttypen
  (Unbound DNS, Firewall-Regeln) ohne Umbau von Orchestrierung/GUI.

### Implementierungs-Statistik

- 11 Sub-Module: `core` (Adapter-Protokoll + Routes/Aliases),
  `orchestration` (Planner/Executor/Reporter/PlanStore/Registry),
  `vault`, `security`, `audit`, `profiles`, `importers`, `inventory`,
  `cli`, `gui`, `config`.
- **415 Tests**, ruff & mypy strict clean.
- Coverage: Core ≥ 91 %, Orchestrierung ≥ 89 %, Vault/Security ≥ 92 %.

### Bekannte Einschränkungen

- API-Spike gegen die laufende OPNsense-Version vor dem ersten
  Live-Lauf erforderlich; Endpoint-Pfade in
  `src/opn_cockpit/core/objects/_endpoints.py` sind gegen die
  Standard-26.1-Doku gebaut und ggf. anzupassen.
- In-Place-Updates von bestehenden Routen werden in v1 nicht
  unterstützt — Konflikte werden in der Vorschau als `UPDATE` mit
  Warnung markiert, der Apply schlägt fehl. Drift muss im OPNsense-UI
  aufgelöst werden.
- Statt `keyring` (ursprünglich geplant) wird das Tresor-Modell
  verwendet, weil Geräte-Inventar zwischen Admins teilbar sein soll.
- Auslieferung erfolgt als Python-Skript + uv-basiertes Setup;
  PyInstaller-Exe ist für v2 vorgesehen.
