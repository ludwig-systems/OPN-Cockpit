# OPN-Cockpit

Multi-Site-Management für OPNsense-Firewalls. Zentrale, ferngesteuerte
Konfiguration mehrerer Standorte (bis ca. 25 Geräte) über die OPNsense-
REST-API: Routen, Aliase, Bulk-Import, Audit-Log, Read-back-Verifikation
und Plan/Apply-Vorschau vor jedem Rollout.

Web-Frontend (Vanilla HTML/CSS/JS) auf einem FastAPI-Backend. Läuft als
**Single-User-Desktop** unter Windows, als **Multi-User-Server** unter
Windows oder Linux, oder als **Proxmox-LXC** mit Helper-Wizard.

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

## Was du im Browser hast

- **Inventar** als Kachel-Grid mit Status-Dot (Heartbeat), TLS-Badge,
  OPNsense-Firmware-Version, Tags, „Offen"-Indikator für unverifizierte
  Aktionen, Direkt-Link zur OPNsense-Weboberfläche.
- **Plan/Apply** für Routen und Aliase mit drei Phasen (Eingabe →
  Vorschau → Result-Matrix) und expliziter Confirm-Gate vor jedem
  Rollout.
- **Auto-Suggest** für Gateway- und Alias-Namen aus der laufenden
  OPNsense (case-sensitive Tippfehler wie `V2_WANBwIn` vs `v2_wanbwin`
  vermeiden).
- **Bulk-Import** von Firewall-Stammdaten als CSV oder JSON.
- **Profile / Vorlagen** für wiederkehrende Aktionen, sanitisiert ohne
  Credentials.
- **Audit-Log** mit Filter nach Event-Kind, Action, Geräte-ID.
- **Retry-Pfad** für fehlgeschlagene Geräte — direkt nach dem Apply
  oder später via „Offen"-Badge auf der Karte.
- **Backup-Download** pro Karte (zieht ein aktuelles OPNsense-Config-
  Backup über die API).

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

- Geräte-Inventar und API-Credentials liegen ausschließlich verschlüsselt
  in der `.opnvault`-Tresor-Datei. Niemals im Klartext auf Platte.
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
  hinter Reverse-Proxy mit TLS und Client-Cert / mTLS. Rate-Limit auf
  Login + Bootstrap (10 Versuche / 15 min pro IP).
- **Linux/systemd**: Hardening-Flags `NoNewPrivileges`,
  `ProtectSystem=strict`, `PrivateTmp`, `ProtectHome`,
  `ProtectKernelTunables`/`Modules`. Service läuft als unprivilegierter
  User `opncockpit`.
- Audit-Log ist HMAC-Chain-protected (Tamper-Evidence) und enthält nur
  maskierte Antwort-Kurzfassungen, keine vollständigen HTTP-Bodies.

## Entwicklung

```powershell
# Windows / PAW
.\scripts\setup-venv.ps1     # erzeugt .venv\, installiert Runtime + Dev-Tooling, läuft pytest -q
.\start.bat                  # killt alte Cockpit-Instanzen, startet Server, öffnet Browser
```

```bash
# Linux / macOS
uv sync                      # Dev-Dependencies installieren
uv run python -m opn_cockpit # Server starten (Browser-Auto-Open)
```

Vollständiger Dev-Walkthrough: [docs/QUICKSTART.md](docs/QUICKSTART.md).

### Tests

```powershell
pytest -q                    # Unit-Tests (~580)
pytest --cov                 # mit Coverage-Report
pytest -m live               # Integrationstests gegen Test-OPNsense
```

100 % Coverage im Web-Layer, ≥ 90 % in Core und Orchestrierung,
ruff + mypy strict clean.

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

- [docs/INSTALLATION-WINDOWS.md](docs/INSTALLATION-WINDOWS.md) — Windows-Installation (SmartScreen, Updates, Service-Mode)
- [installer/linux/README.md](installer/linux/README.md) — Linux-Server, Proxmox-LXC, Docker
- [docs/QUICKSTART.md](docs/QUICKSTART.md) — Dev-Walkthrough
- [CHANGELOG.md](CHANGELOG.md)
