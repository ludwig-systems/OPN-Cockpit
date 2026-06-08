# OPN-Cockpit

Multi-Site-Management für OPNsense-Firewalls. Zentrale, ferngesteuerte
Konfiguration mehrerer Standorte über die OPNsense-REST-API:

- **Vier CRUD-Subsysteme** mit voller Edit/Delete-Unterstützung:
  Aliase, statische Routen, Filter-Regeln (Automation-Filter, ab
  OPNsense 23.7 in Core), Unbound DNS Host-Overrides + Domain-Overrides
- **Plan/Apply-Vorschau** vor jedem Rollout, Read-back-Verifikation, parallel
  über N Geräte
- **Config-Compare-Matrix** zeigt Drift zwischen 2–N Geräten pro Subsystem
- **Safety-Net via SSH** (Cisco-Style commit-confirmed) — Apply mit
  Countdown; ohne Bestätigung Auto-Rollback zum Pre-Apply-Backup via SSH
- **Auto-Backup** vor jedem Apply + Post-Apply-Snapshot als Drift-Baseline,
  Scheduled Backups, Config-Drift-Erkennung
- **Auto-Retry-Queue** für Mobile-Racks — persistiert über Server-Restart,
  Orphan-Adoption beim nächsten Vault-Unlock
- **Audit-Log** mit HMAC-Hash-Chain, CSV- + signierter PDF-Export, Filter,
  Integrity-Check
- **Bulk-Import** von Firewall-Stammdaten, Sicht-Vorlagen für wiederkehrende
  Aktionen
- **Frontend-Inline-Validierung** beim Tippen (CIDR, Host, Aliase, Ports)
- **Interne CAs unterstützen** — Custom-Root-Zertifikate im Tresor, damit
  OPNsense-Boxen mit interner-CA-Cert mit aktiver TLS-Prüfung verwaltet werden
- **Cockpit-eigenes HTTPS** — Server-Zertifikat hinterlegen, damit
  `https://cockpit.lab:9876` ohne Browser-Warnung erreichbar ist
- **TOTP / 2FA** (Multi-User, opt-in) — pro User aktivierbar mit
  Authenticator-App + 8 Backup-Codes; Admin-Recovery-Reset
- **Wartungsmodus pro Gerät** — geplant offline-Sites vom Polling
  ausschließen, Audit-Log bleibt sauber
- **Disk-Space-Widget** in der Topbar (Linux-Server) — Warnung bei ≥80 %,
  einmaliger Toast bei ≥92 %

Web-Frontend (Vanilla HTML/CSS/JS) auf einem FastAPI-Backend. Läuft als
**Single-User-Desktop** unter Windows, als **Multi-User-Server** unter
Windows oder Linux, oder als **Proxmox-LXC** mit Helper-Wizard.

Lizenz: **Apache License 2.0** (siehe [LICENSE](LICENSE) und
[THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md)).

## Deployment-Varianten

| Variante | Wer | So gehts |
|---|---|---|
| **Windows Single-User** | Ein Admin am eigenen PAW | `Install-OPN-Cockpit-X.Y.Z.exe` von [Releases](https://github.com/ludwig-systems/opn-cockpit/releases) → Doppelklick → Single-User wählen |
| **Windows Multi-User-Server** | Team auf Windows-Server | Selber Installer, im Wizard „Multi-User-Server" wählen — registriert NSSM-Dienst, Autostart, Bind auf `0.0.0.0:9876` |
| **Linux-Server (Debian/Ubuntu)** | Team auf Linux-Server | `git clone … && sudo bash installer/linux/install.sh --source .` — systemd-Unit mit Hardening-Flags |
| **Proxmox-LXC** | Team auf PVE-Cluster | Auf dem PVE-Host: `bash -c "$(wget -qLO - https://raw.githubusercontent.com/ludwig-systems/opn-cockpit/main/installer/linux/proxmox-helper.sh)"` — whiptail-TUI führt durch CT-ID, Storage, Bridge, Netz; derselbe Befehl im Container = Update |
| **Docker** | Container-First-Setup | Image im Quelltree (`Dockerfile`, `docker-compose.yml`) |

Beim ersten Start in jeder Variante:

1. Browser auf `http://<host>:9876`
2. Login `admin` / `OPN-Cockpit!` (Default-Admin, **erzwingt Passwort-Wechsel beim ersten Login**)
3. Tresor-Pfad + Master-Passwort setzen
4. Geräte hinzufügen — einzeln oder Bulk-CSV/JSON

Details pro Plattform:

- [docs/INSTALLATION-WINDOWS.md](docs/INSTALLATION-WINDOWS.md) — Windows-Installer + SmartScreen + Auto-Update + Multi-User-Server
- [installer/linux/README.md](installer/linux/README.md) — Linux-Server, Proxmox-LXC, Docker

## API-Key + Secret in OPNsense erzeugen

Pro OPNsense-Instanz braucht OPN-Cockpit ein **API-Key/Secret-Paar**, das
einem eigens dafür angelegten User zugeordnet ist. Empfehlung: **nicht**
den `root`-Account verwenden, sondern einen dedizierten Service-User mit
minimalen Rechten.

1. In der OPNsense-Weboberfläche: **System → Access → Users → +** (neuer User)
   - Username: z. B. `opn-cockpit`
   - Passwort: nicht relevant für API-Nutzung (langes Zufallspasswort genügt,
     Login wird nicht gebraucht)
   - **Privileges** je nach geplantem Einsatz (Beispiel-Set für alle
     v0.8-Features — Routen, Aliase, Filter-Regeln, Unbound-DNS, Backup,
     Firmware-Status):
     - `Diagnostics: Configuration History` *(Backup-Download)*
     - `Firewall: Aliases: Edit`
     - `Firewall: Rules: Edit` *(Automation-Filter, ab OPNsense 23.7 in Core; vorher als `os-firewall`-Plugin)*
     - `Services: Unbound DNS: Edit Host Override`
     - `Services: Unbound DNS: Restart Service`
     - `System: Firmware`
     - `System: Static Routes: Edit`
     - `System: Trust: Certificate Management` *(Cert-Ablauf-Badge)*
2. User speichern → ihn nochmal öffnen → unten bei **API keys** auf **+** klicken.
3. OPNsense lädt eine Datei `apikey.txt` mit zwei Zeilen herunter:
   ```
   key=AbCdEf…XyZ==
   secret=1234…abcd==
   ```
4. Diese beiden Werte beim **Gerät anlegen** in OPN-Cockpit eintragen
   (Inventar → *Gerät hinzufügen*). Sie werden direkt verschlüsselt im
   Tresor abgelegt, niemals im Klartext gespeichert.
5. Vor dem Anlegen empfiehlt sich ein **Test-Connection-Klick** — OPN-Cockpit
   ruft damit den Firmware-Status-Endpunkt und meldet Erreichbarkeit + Auth
   zurück.

> Tipp für TLS-Verifikation: Wenn die OPNsense ein selbstsigniertes Web-GUI-
> Zertifikat nutzt, kann pro Gerät die TLS-Prüfung abgeschaltet werden — das
> Tool markiert diese Geräte dann **rot** im Inventar. Sauberer ist: das
> OPNsense-Web-GUI auf ein vertrauenswürdiges Zertifikat (interne CA oder
> ACME/Let's Encrypt) umstellen und TLS-Prüfung aktiv lassen.
>
> Wer eine **interne CA** betreibt und damit die OPNsense-Zertifikate
> ausstellt, kann die CA einmal im Tresor hinterlegen — Cockpit akzeptiert
> sie dann zusätzlich zum System-CA-Bundle, kein per-Gerät-`tls_verify=false`
> mehr nötig. Siehe [docs/FEATURES.md → Interne CAs vertrauen](docs/FEATURES.md#interne-cas-vertrauen).

## Was du im Browser hast

### Inventar
- Kachel-Grid mit Status-Dot (Heartbeat), TLS-Badge,
  OPNsense-Firmware-Version, Cert-Ablauf-Indikator (gelb < 30 Tage,
  rot < 7 Tage), Drift-Indikator, Tags, „Offen"-Indikator für
  unverifizierte Aktionen, Direkt-Link zur OPNsense-Weboberfläche.
- **Hover-Quick-Actions** auf der Karte: OPNsense öffnen,
  Updates suchen, Duplizieren.

### Device-Modal (Karten-Klick) — sechs Tabs
- **Info** — Test-Connection, Bearbeiten, Duplizieren, Update-Check,
  Backup herunterladen
- **Updates** — installierte/verfügbare OPNsense-Version, „Erneut prüfen"
- **Backups** — Liste aller lokal gespeicherten Backups
  (Pre-Apply / Post-Apply / Manuell / Scheduled), Download +
  „Backup erzeugen" (server-only)
- **Aliase** — Live-Liste mit Filter, Edit/Delete pro Eintrag
- **Routen** — Live-Liste aller statischen Routen, Edit/Delete pro Eintrag
- **Regeln** — Live-Liste der Automation-Filter-Regeln (Firewall →
  Automation → Filter), Add/Edit/Delete. Klassische „Firewall → Rules"
  (Legacy-XML-Editor) sind nicht API-zugänglich und werden nicht angezeigt.
- **DNS** mit drei Sub-Tabs: **Host-Overrides** (CRUD),
  **Domain-Overrides** (read-only, `searchDomainOverride`),
  **Abfrage-Weiterleitungen** (read-only, `searchForward` — DoT/DoH-Forwarder)
- **DNS** — Live-Liste der Unbound-Host-Overrides, Add/Edit/Delete

### Plan/Apply-Flow
- Drei-Phasen-Modal: Eingabe → Vorschau → Result-Matrix
- Diff pro Gerät (NEW/UPDATE/SKIP/DELETE) inklusive Inline-Hinweis
- Confirm-Gate vor jedem Rollout
- Optional: **„Mit Sicherheitsnetz ausrollen"** (sichtbar wenn Targets
  SSH konfiguriert haben) — Cisco-Style commit-confirmed mit Countdown
- **Auto-Suggest** für Gateway- und Alias-Namen aus der laufenden OPNsense

### Multi-Site-Tools
- **Config-Compare-Matrix** über 2..N Geräte mit Tab-Strip
  *Aliase | Routen | Regeln | DNS-Hosts | DNS-Overrides | DNS-Weiterleitungen*.
  Master-Spalte links, Drift master-relativ farbig markiert,
  Detail-Aufklapp pro Zeile.
- **Master-→-Targets-Sync** direkt aus der Compare-Matrix für
  **Aliase** und **DNS-Host-Overrides** — ein Klick erzeugt den Plan
  und springt in die Vorschau.

### Safety-Nets
- **Auto-Backup vor Apply** + **Post-Apply-Snapshot** als neue
  Drift-Baseline (gegen False-Positive-Drift-Warnungen).
- **Scheduled Backups** (Hintergrund-Thread mit eigener Retention).
- **Config-Drift-Erkennung** gegen das letzte lokale Backup
  (volatile `<revision>`-Felder gestripped).
- **Auto-Retry für Mobile-Racks** — fehlgeschlagene Geräte landen in
  einer persistenten Queue (`state/retry-queue.json`), Orphan-Adoption
  beim nächsten Vault-Unlock, läuft bis konfigurierbare Max-Dauer.
- **Safety-Net via SSH** — siehe [docs/FEATURES.md](docs/FEATURES.md#safety-net-via-ssh).

### Audit-Log
- HMAC-Hash-Chain für Tamper-Evidence, append-only.
- Filter nach Event-Kind, Action, Geräte-ID, Zeitfenster.
- Export als **CSV** oder **signiertes PDF** (HMAC-SHA256 sichtbar im
  Footer + in PDF-Metadata).

### Sonstiges
- **Bulk-Import** von Firewall-Stammdaten als CSV oder JSON.
- **Profile / Vorlagen** für wiederkehrende Aktionen, sanitisiert ohne
  Credentials.
- **Retry-Pfad** direkt nach dem Apply oder später via „Offen"-Badge
  auf der Karte.
- **Frontend-Inline-Validierung** beim Tippen: CIDR mit Host-Bits-Check,
  IPv4, Host/FQDN, Alias-Name, Gateway-Name, Port (Zahl/Range/Alias).

## Designprinzipien

- **Architektonische Einfachheit** — wenige Abhängigkeiten, klar getrennte
  Schichten (Core / Orchestrierung / Web-Präsentation).
- **Verifikation vor Vertrauen** — jede Änderung wird nach dem Schreiben
  per Read-back gegen die API geprüft.
- **Vorschau vor Ausführung** — kein Ausrollen ohne expliziten Confirm-Klick.
- **Nachvollziehbarkeit** — jede Änderung wird auditierbar protokolliert,
  Secrets werden maskiert.
- **Auswählen vor Aktion** — Multi-Select auf den Karten + Schnellauswahl
  (Alle / Nur erreichbare / Keine), dann Aktion definieren.
- **Retry-Pfad immer offen** — Apply-Reports bleiben persistiert,
  fehlgeschlagene Geräte werden als „Offen"-Badge auf der Karte markiert
  und können einzeln nachgezogen werden.

## Tech-Stack

- **Sprache:** Python 3.11+
- **Web-Backend:** FastAPI + Uvicorn — bind `127.0.0.1:9876` (Single-User)
  oder `0.0.0.0:9876` (Multi-User-Server)
- **Frontend:** Vanilla HTML/CSS/JS, kein Build-Step
- **HTTP zur OPNsense:** `httpx` (synchron) + `ThreadPoolExecutor` für
  parallelen Rollout
- **Tresor:** verschlüsselte Datei (`.opnvault`) im KeePass-Stil —
  Argon2id-KDF (RFC 9106 Defaults) + AES-256-GCM. Geräte-Inventar und
  API-Schlüssel liegen gemeinsam verschlüsselt auf Platte, ein einziges
  Master-Passwort entsperrt sie beim Tool-Start.
- **Persistenz:** SQLite für Audit-Log + Plan-Reports + User-DB
  (Multi-User-Server). Migrations laufen beim Start mit Pre-Backup.
- **Authentifizierung Multi-User:** Default-Admin (`admin` /
  `OPN-Cockpit!`) mit Pflicht-Wechsel beim ersten Login. Weitere User
  via UI oder CLI. Bearer-Token-Session, Per-Tab-Storage.
- **Paketmanager:** [`uv`](https://docs.astral.sh/uv/) für Dev-Setup;
  Endnutzer-Installer bringt Embedded-Python mit (keine System-Python-
  Installation nötig).

## Sicherheit

- Geräte-Inventar, API-Credentials, SSH-Private-Keys und Custom-CA-PEMs
  liegen ausschließlich verschlüsselt in der `.opnvault`-Tresor-Datei.
  Niemals im Klartext auf Platte.
- Tresor-Dateien sind **portabel**: per CLI `export-template` entsteht eine
  zweite Datei mit identischem Inventar aber geleerten Secret-Feldern,
  die sicher an andere Admins weitergegeben werden kann.
- Default-Admin-Passwort (`OPN-Cockpit!`) ist bekannt — der Server blockt
  **alle** Vault-Operationen, solange das Default-Passwort gilt. Erst
  nach erzwungenem Wechsel sind Tresor-Operationen möglich.
- Nach konfigurierbarer Inaktivität (Default: 10 min) verlangt das Tool
  eine erneute Master-Passwort-Eingabe.
- Geräte mit deaktivierter TLS-Verifikation werden in der UI deutlich
  als Risiko markiert (rot getintete Karten + Warn-Badge).
- TCP-Heartbeat zur Status-Anzeige erzeugt keine Auth-Versuche und
  damit keine Logs auf der OPNsense.
- **Multi-User-Server**: bind auf `0.0.0.0` ist Default — produktiv
  hinter Reverse-Proxy mit TLS und Client-Cert / mTLS, oder direkt mit
  einem eigenen Server-Zertifikat aus deiner internen CA
  (siehe [docs/FEATURES.md → HTTPS für Cockpit selbst](docs/FEATURES.md#https-fuer-cockpit-selbst)).
  Rate-Limit auf Login + Bootstrap (10 Versuche / 15 min pro IP).
- **Linux/systemd**: Hardening-Flags `NoNewPrivileges`,
  `ProtectSystem=strict`, `PrivateTmp`, `ProtectHome`,
  `ProtectKernelTunables`/`Modules`. Service läuft als unprivilegierter
  User `opncockpit`.
- Audit-Log ist HMAC-Chain-protected (Tamper-Evidence) und enthält nur
  maskierte Antwort-Kurzfassungen, keine vollständigen HTTP-Bodies.
- **Signierte PDF-Audit-Reports**: HMAC-SHA256 über alle Records mit dem
  Audit-Chain-Secret. Signatur sichtbar im Footer + maschinell in
  PDF-Metadata (`OPN-COCKPIT-AUDIT-SIG-v1:<hex>`). Reproduzierbar via
  `audit.pdf_report.verify_pdf_signature`.
- **Safety-Net SSH-Rollback** nutzt `paramiko` mit Private-Key-Auth
  (Password-Auth bewusst nicht implementiert). Klartext-Key wird sofort
  nach `connect()` freigegeben und nicht in Tracebacks geleakt.
- **TOTP / 2FA** (Multi-User-Mode, opt-in pro User): RFC 6238, 6-stellig,
  ±30s Clock-Skew. 8 SHA-256-gehashte Backup-Codes pro User; jeder
  Verbrauch wird automatisch entfernt. Self-Disable verlangt aktuelles
  Passwort + aktuellen Code. Admin-Reset (für Recovery bei Verlust der
  Authenticator-App) ist möglich und wird auditiert. Siehe
  [docs/FEATURES.md → Zwei-Faktor-Authentifizierung](docs/FEATURES.md#zwei-faktor-authentifizierung-totp).

## Entwicklung

Voraussetzung: Python 3.11+ und [uv](https://docs.astral.sh/uv/).

```powershell
uv sync                              # Runtime + Dev-Tooling
uv run python -m opn_cockpit         # Server starten (Browser-Auto-Open)
```

Vollständiger Dev-Walkthrough: [docs/QUICKSTART.md](docs/QUICKSTART.md).

## CLI (Headless / Automation)

Als Alternative für Skripte und Automatisierung. CLI und Web-Variante
teilen denselben Plan-Store und Audit-Log.

```powershell
.\.venv\Scripts\python.exe -m opn_cockpit.cli --help
```

Sub-Commands: `create-vault`, `add-device`, `plan`, `apply`, `audit`,
`bulk-import`, `discover`, `profile`, `change-password`, `export-template`.
Selektor-Sprache: `all`, `tag:X`, `group:X`, `id:X`, `name:X` —
komma-getrennt, case-insensitive.

## Projektstruktur

```
src/opn_cockpit/
├── core/                # API-/Logik-Schicht (Adapter, http_client, validation)
├── orchestration/       # Plan/Apply, Bulk-Plan, PlanStore + Reports
├── inventory/           # Geräte-Stammdaten (ohne Secrets)
├── security/            # Master-Passwort, Session, Masking
├── vault/               # Verschlüsselter Tresor (Argon2id + AES-256-GCM)
├── audit/               # JSON-Lines + SQLite, HMAC-Chain, append-only
├── profiles/            # Aktions-Templates
├── importers/           # CSV/JSON-Bulk-Import (Routen, Aliase, Devices)
├── updates/             # GitHub-Releases-Check für In-App-Update-Banner
├── users/               # User-DB (Multi-User-Server)
├── migrations/          # SQLite-Migrations-Framework
├── cli/                 # Headless-CLI
└── web/                 # FastAPI-Backend + Vanilla-JS-Frontend
    ├── api/             # Route-Handler pro Modul
    ├── auth/            # SessionManager + Bearer-Token-Auth
    ├── static/          # styles.css + app.js (cache-busted via ?v=…)
    └── templates/       # index.html (Jinja2)
```

## Referenz

- [docs/QUICKSTART.md](docs/QUICKSTART.md) — Dev-Walkthrough + erster Apply
- [docs/FEATURES.md](docs/FEATURES.md) — Feature-Anleitungen pro Subsystem
  (Aliase, Routen, Filter-Regeln, Unbound-DNS, Safety-Net, Backups, Audit)
- [docs/INSTALLATION-WINDOWS.md](docs/INSTALLATION-WINDOWS.md) —
  Windows-Installation (SmartScreen, Updates, Service-Mode)
- [installer/linux/README.md](installer/linux/README.md) — Linux-Server,
  Proxmox-LXC, Docker
- [CHANGELOG.md](CHANGELOG.md) — Pro Release was sich geändert hat
- [LICENSE](LICENSE) — Apache License 2.0
- [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md) — Drittanbieter-Attribution
