# OPN-Cockpit

Lokales Windows-Tool zur zentralen, ferngesteuerten Konfiguration mehrerer
OPNsense-Firewalls (bis ca. 25 Standorte) über deren REST-API. Aktionen
werden mit Vorschau (Plan/Apply-Muster) ausgewählt, parallel ausgerollt
und per Read-back gegen die API verifiziert.

**v2.0** läuft komplett im Browser — lokaler FastAPI-Server, Vanilla-JS-
Frontend mit Calm-Precision-Linie (Bahnschrift-Display, Olive-Akzent).
v1.x mit PySide6 wurde abgelöst.

## Scope

Statische Routen und Aliase auf 1..n Geräten gleichzeitig anlegen —
einzeln oder im Bulk via CSV/JSON, mit Best-Effort-Fehlerstrategie,
Audit-Log, wiederverwendbaren Profilen und Retry für nicht erreichbare
Geräte.

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
- **Web-Backend:** FastAPI + Uvicorn (lokal, `127.0.0.1:9876`)
- **Frontend:** Vanilla HTML/CSS/JS, keine Build-Pipeline
- **HTTP zur OPNsense:** `httpx` (synchron) + `ThreadPoolExecutor` für
  parallelen Rollout
- **Tresor:** verschlüsselte Datei (`.opnvault`) im KeePass-Stil —
  Argon2id-KDF (RFC 9106 Defaults) + AES-256-GCM. Geräte-Inventar und
  API-Schlüssel liegen gemeinsam verschlüsselt auf Platte, ein einziges
  Master-Passwort entsperrt sie beim Tool-Start.
- **Paketmanager:** [`uv`](https://docs.astral.sh/uv/)

## Setup (Windows-PAW)

```powershell
.\scripts\setup-venv.ps1
```

Erzeugt `.venv\`, installiert Runtime + Dev-Tooling, führt `pytest -q` aus.

## Starten

```powershell
.\start.bat
```

Doppelklick reicht. Die Batch killt eine eventuelle alte Cockpit-Instanz
auf Port 9876, startet den Server und öffnet den Browser. Beim ersten
Mal: „Neuen Tresor anlegen…" im Login-Screen.

Vollständiger End-to-End-Walkthrough: [docs/QUICKSTART.md](docs/QUICKSTART.md).

## Was du im Browser hast

- **Inventar** als Kachel-Grid mit Status-Dot (Heartbeat), TLS-Badge,
  Tags, Outstanding-Indikator für offene Aktionen, Direkt-Link zur
  OPNsense-Weboberfläche.
- **Plan/Apply** für Routen + Aliase mit drei Phasen (Eingabe →
  Vorschau → Result-Matrix) und Confirm-Gate vor jedem Ausrollen.
- **Auto-Suggest** für Gateway-/Alias-Namen aus der laufenden OPNsense
  (vermeidet case-sensitive Tippfehler wie `V2_WANBwIn` vs `v2_wanbwin`).
- **Bulk-Import** von Firewall-Stammdaten als CSV oder JSON.
- **Profile / Vorlagen** für wiederkehrende Aktionen, sanitisiert ohne
  Credentials.
- **Audit-Log** mit Filter nach Event-Kind, Action, Geräte-ID.
- **Retry für Fehlgeschlagene** — entweder direkt nach dem Apply oder
  später via Outstanding-Badge auf der Karte.

## CLI (Headless / Automation)

Bleibt als Alternative für Skripte erhalten:

```powershell
.\.venv\Scripts\python.exe -m opn_cockpit.cli --help
```

Sub-Commands: `create-vault`, `add-device`, `plan`, `apply`, `audit`,
`bulk-import`, `discover`, `profile`. Plan-Files und Audit-Log werden mit
der Web-Variante geteilt.

## Tests

```powershell
pytest -q                       # Unit-Tests
pytest --cov                    # mit Coverage-Report
pytest -m live                  # Integrationstests gegen Test-OPNsense
```

568 Tests, 100 % im Web-Layer + Core-/Orchestration-Coverage.

## Sicherheitshinweise

- Geräte-Inventar und API-Credentials liegen ausschließlich verschlüsselt
  in der `.opnvault`-Tresor-Datei. Niemals im Klartext auf Platte.
- Tresor-Dateien sind **portabel**: per CLI `export-template` entsteht eine
  zweite Datei mit identischem Inventar aber geleerten Secret-Feldern,
  die sicher an andere Admins weitergegeben werden kann.
- Nach konfigurierbarer Inaktivität (Default: 10 min) verlangt das Tool
  eine erneute Master-Passwort-Eingabe.
- Geräte mit deaktivierter TLS-Verifikation werden in der UI deutlich
  als Risiko markiert (rot getintete Karten + Warn-Badge).
- TCP-Heartbeat zur Status-Anzeige erzeugt keine Auth-Versuche und
  damit keine Logs auf der OPNsense.
- Server bindet auf `127.0.0.1`. Eine spätere Multi-User-/Server-Variante
  ist im Schema vorbereitet (Token-Auth pro Session, TLS-Felder in den
  Settings).
- Audit-Log enthält maskierte Antwort-Kurzfassungen, keine vollständigen
  HTTP-Bodies.

## Projektstruktur

```
src/opn_cockpit/
├── core/                # API-/Logik-Schicht
├── orchestration/       # Plan/Apply, Bulk-Plan, PlanStore + Reports
├── inventory/           # Geräte-Stammdaten (ohne Secrets)
├── security/            # Master-Passwort, Session, Masking
├── vault/               # Verschlüsselter Tresor (Argon2id + AES-256-GCM)
├── audit/               # JSON-Lines-Log, append-only
├── profiles/            # Aktions-Templates
├── importers/           # CSV/JSON-Bulk-Import (Routen, Aliase, Devices)
├── cli/                 # Headless-CLI
└── web/                 # FastAPI-Backend + Vanilla-JS-Frontend
    ├── api/             # Route-Handler pro Modul
    ├── auth/            # SessionManager + Bearer-Token-Auth
    ├── static/          # styles.css + app.js (cache-busted via ?v=…)
    └── templates/       # index.html (Jinja2)
```

## Referenz

- [docs/QUICKSTART.md](docs/QUICKSTART.md) — Dev-Walkthrough
- [docs/INSTALLATION-WINDOWS.md](docs/INSTALLATION-WINDOWS.md) — Endnutzer-Installation (SmartScreen, Updates)
- [docs/SERVICE-MODE.md](docs/SERVICE-MODE.md) — Multi-User-Server unter Windows
- [docs/DOCKER.md](docs/DOCKER.md) — Linux/Docker-Deployment
- [docs/RELEASING.md](docs/RELEASING.md) — Release-Prozess für Maintainer
- [docs/TESTPLAN-0.6.0.md](docs/TESTPLAN-0.6.0.md) — End-to-End-Tests für v0.6.0
- [docs/SESSION-NOTES-0.6.0.md](docs/SESSION-NOTES-0.6.0.md) — Debug-Session-Journal + Architektur-Entscheidungen
- [installer/README.md](installer/README.md) — Inno-Setup-Build
- [installer/winget/README.md](installer/winget/README.md) — winget-Manifeste
- [CHANGELOG.md](CHANGELOG.md)
- [docs/opnsense-api-26.1.md](docs/opnsense-api-26.1.md) — API-Endpoint-Notizen
- [Anforderungen](Anforderungen) — Anforderungskatalog v1.0
