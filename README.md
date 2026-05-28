# OPN-Cockpit

Lokales Windows-Desktop-Tool zur zentralen, ferngesteuerten Konfiguration mehrerer OPNsense-Firewalls (bis ca. 25 Standorte) über deren REST-API. Aktionen werden mit Vorschau (Plan/Apply-Muster) ausgewählt, ausgerollt und anschließend per Read-back gegen die API verifiziert.

> **Scope v1:** Statische Routen und Aliase auf 1..n Geräten gleichzeitig anlegen — einzeln oder im Bulk via CSV/JSON, mit Best-Effort-Fehlerstrategie, Audit-Log und wiederverwendbaren Templates.

## Designprinzipien

- **Architektonische Einfachheit** — wenige Abhängigkeiten, klar getrennte Schichten (Core / Orchestrierung / GUI).
- **Verifikation vor Vertrauen** — jede Änderung wird nach dem Schreiben per Read-back gegen die API geprüft.
- **Vorschau vor Ausführung** — kein Ausrollen ohne expliziten Dry-Run.
- **Nachvollziehbarkeit** — jede Änderung wird auditierbar protokolliert, Secrets werden maskiert.

## Tech-Stack

- **Sprache:** Python 3.11+
- **GUI:** PySide6 (Qt)
- **HTTP:** `httpx` (synchron) + `ThreadPoolExecutor` für parallelen Rollout
- **Geräte- + Secret-Speicherung:** Verschlüsselter Tresor-File (`.opnvault`) im
  KeePass-Stil — Argon2id-KDF + AES-256-GCM. Geräte-Inventar und API-Keys
  liegen gemeinsam verschlüsselt auf Platte, ein einziges Master-Passwort
  entsperrt sie beim Tool-Start.
- **Paketmanager:** [`uv`](https://docs.astral.sh/uv/) (schnell, deterministisch)

## Setup (Windows-PAW)

Voraussetzung: Python 3.11+ und [`uv`](https://docs.astral.sh/uv/getting-started/installation/) installiert.

```powershell
# Aus dem Repo-Root:
.\scripts\setup-venv.ps1
```

Das Skript legt eine venv (`.venv`) an, installiert Runtime- und Dev-Dependencies und führt einen Sanity-Check (`pytest -q`) aus.

### Manuelles Setup

```powershell
uv venv
uv pip install -e ".[dev]"
.\.venv\Scripts\Activate.ps1
pytest -q
```

## Erste Schritte

Siehe [docs/QUICKSTART.md](docs/QUICKSTART.md) für einen End-to-End-Walkthrough vom ersten Tresor bis zum ausgerollten Route-Plan.

```powershell
# 1. Neuen Tresor anlegen
.\.venv\Scripts\python.exe -m opn_cockpit.cli create-vault C:\vaults\prod.opnvault

# 2. Erstes Gerät hinzufügen
.\.venv\Scripts\python.exe -m opn_cockpit.cli `
    --vault C:\vaults\prod.opnvault add-device `
    --name "HQ Berlin" --host opn-berlin.lab --tags branches,core

# 3. Verbindung testen
.\.venv\Scripts\python.exe -m opn_cockpit.cli `
    --vault C:\vaults\prod.opnvault test-connection --target all

# 4. GUI öffnen
.\.venv\Scripts\python.exe -m opn_cockpit
```

## CLI-Oberfläche

| Sub-Command         | Zweck                                                                 |
|---------------------|----------------------------------------------------------------------|
| `create-vault`      | Neuen `.opnvault`-Tresor anlegen                                      |
| `change-password`   | Master-Passwort eines Tresors ändern                                  |
| `export-template`   | Tresor mit geleerten Secret-Feldern als Template exportieren          |
| `list-devices`      | Inventar des aktuellen Tresors auflisten                              |
| `add-device`        | Neues Gerät mit API-Key/Secret hinzufügen                             |
| `remove-device`     | Gerät aus dem Tresor löschen                                          |
| `test-connection`   | Erreichbarkeit + API-Auth gegen Geräte prüfen                         |
| `plan add-route`    | Vorschau für eine neue Route über N Geräte erzeugen                   |
| `plan add-alias`    | Vorschau für einen neuen Alias                                        |
| `plan append-alias` | Vorschau für das Erweitern eines bestehenden Alias (Merge)            |
| `apply`             | Vorher erzeugten Plan ausrollen                                       |
| `audit`             | Audit-Log filtern und anzeigen                                        |
| `profile list`      | Gespeicherte Aktions-Templates auflisten                              |
| `profile save-route`/`save-alias` | Aktion als Template speichern                                |
| `profile apply`     | Template laden und ausrollen                                           |
| `profile delete`    | Template löschen                                                       |
| `bulk-import routes`  | Mehrere Routen aus CSV importieren                                   |
| `bulk-import aliases` | Mehrere Aliasse aus JSON importieren                                 |
| `discover gateways`   | Vorhandene Gateway-Namen auf einem Gerät über die API auflisten      |
| `discover aliases`    | Bestehende Aliase auf einem Gerät über die API auflisten             |

Detailhilfe je Sub-Command: `python -m opn_cockpit.cli <command> --help`.

## Bulk-Import

CSV-Routen ([Beispiel](docs/example-routes.csv)):

```csv
network,gateway,descr,disabled
10.99.0.0/24,WAN_GW,Branch Berlin,0
10.99.1.0/24,WAN_GW,Branch Munich,0
```

JSON-Aliase ([Beispiel](docs/example-aliases.json)):

```json
[
  {"name": "branch_ips", "type": "host", "content": ["10.99.0.1", "10.99.1.1"]},
  {"name": "lab_ports", "type": "port", "content": [22, 80, 443]}
]
```

```powershell
python -m opn_cockpit.cli --vault C:\vaults\prod.opnvault `
    bulk-import routes docs\example-routes.csv --target tag:branches
```

## Anwendung starten

```powershell
# GUI
python -m opn_cockpit

# CLI (headless, für Batch-Skripte oder Automatisierung)
python -m opn_cockpit.cli --help
```

## Projektstruktur

```
src/opn_cockpit/
├── core/                # API-/Logik-Schicht (KEINE GUI-/keyring-Imports)
├── orchestration/       # Plan/Apply, Best-Effort-Rollout über mehrere Geräte
├── inventory/           # Geräte-Stammdaten (ohne Secrets)
├── security/            # Master-Passwort, Session-Lock, Masking
├── vault/               # Verschlüsselter Tresor (Argon2id + AES-256-GCM)
├── audit/               # JSON-Lines-Log, append-only
├── profiles/            # Aktions-Templates (R-TPL-*)
├── importers/           # CSV/JSON-Bulk-Import (R-IMP-*)
├── cli/                 # Headless-CLI (Batch-Modus)
└── gui/                 # PySide6-Präsentation
```

## Tests

```powershell
pytest -q                       # Unit-Tests
pytest -m live                  # Integrationstests gegen Test-OPNsense (Test-Lab nötig)
pytest --cov                    # mit Coverage-Report
```

## Sicherheitshinweise

- Geräte-Inventar und API-Keys/Secrets liegen ausschließlich in einer
  **verschlüsselten Tresor-Datei** (`.opnvault`, Argon2id + AES-256-GCM),
  niemals im Klartext auf Platte. Ein einziges Master-Passwort (min. 12 Zeichen)
  entsperrt sie für die Session.
- Tresor-Dateien sind **portabel**: per "Als Template exportieren" entsteht eine
  zweite Datei mit identischem Inventar aber geleerten Secret-Feldern, die
  sicher an andere Admins weitergegeben werden kann. Empfänger setzen ihr
  eigenes Master-Passwort, fügen ihre Secrets ein.
- Nach konfigurierbarer Inaktivität (Default: 10 min, im Tresor anpassbar)
  verlangt das Tool eine erneute Master-Passwort-Eingabe und löscht
  entschlüsselte Daten aus dem Speicher.
- Geräte mit **deaktivierter TLS-Verifikation** werden in der Oberfläche
  deutlich als Risiko markiert.
- Im Inventar zeigt ein TCP-Heartbeat (Pünktchen pro Gerät) auf einen Blick,
  welche OPNsense-Instanzen gerade erreichbar sind — ohne Auth-Versuch
  und ohne Last auf den OPNsense-Endpoints.
- Action-Dialoge bieten optional Auto-Complete für **Gateway-** und
  **Alias-Namen** aus der laufenden OPNsense, damit case-sensitive
  Tippfehler (z. B. `V2_WANBwIn` vs `v2_wanbwin`) seltener werden.
- Die App spricht ausschließlich mit Hosts, die im Inventar stehen
  (Egress-Allowlist im `http_client`).
- Audit-Log enthält maskierte Antwort-Kurzfassungen, keine vollständigen
  HTTP-Bodies.

## Referenz

- [Anforderungskatalog](Anforderungen) (v1.0)
- [docs/QUICKSTART.md](docs/QUICKSTART.md)
- [CHANGELOG.md](CHANGELOG.md)
- [docs/opnsense-api-26.1.md](docs/opnsense-api-26.1.md) — Notizen für den API-Spike vor dem ersten Live-Lauf
