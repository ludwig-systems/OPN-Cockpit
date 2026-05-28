# OPN-Cockpit

Lokales Windows-Desktop-Tool zur zentralen, ferngesteuerten Konfiguration mehrerer OPNsense-Firewalls (bis ca. 25 Standorte) über deren REST-API. Aktionen werden mit Vorschau (Plan/Apply-Muster) ausgewählt, ausgerollt und anschließend per Read-back gegen die API verifiziert.

> **Scope v1:** Statische Routen und Aliase auf 1..n Geräten gleichzeitig anlegen, mit Best-Effort-Fehlerstrategie, Audit-Log und Templates.

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
├── security/            # Master-Passwort, Session-Lock, keyring, Masking
├── audit/               # JSON-Lines-Log, append-only
├── profiles/            # Aktions-Templates
├── importers/           # CSV/JSON-Bulk-Import
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
- Die App spricht ausschließlich mit Hosts, die im Inventar stehen
  (Egress-Allowlist im `http_client`).
- Audit-Log enthält maskierte Antwort-Kurzfassungen, keine vollständigen
  HTTP-Bodies.

## Referenz

Anforderungskatalog: [`Anforderungen`](Anforderungen) (v1.0).
