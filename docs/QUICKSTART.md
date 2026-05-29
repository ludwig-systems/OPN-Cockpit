# Quickstart — OPN-Cockpit v2

Web-Oberfläche im Browser. Vom ersten Tresor bis zum verifizierten Rollout
in ca. 10 Minuten.

## 1. Setup (einmalig)

Voraussetzung: Python 3.11+ und [uv](https://docs.astral.sh/uv/getting-started/installation/).

```powershell
# Im Repo-Root:
.\scripts\setup-venv.ps1
```

Erzeugt `.venv\`, installiert das Paket samt Dev-Tooling und führt
`pytest -q` als Sanity-Check.

## 2. App starten

```powershell
.\start.bat
```

Doppelklick funktioniert auch. Die Batch:
- Prüft ob Port 9876 von einer alten Cockpit-Instanz belegt ist und beendet
  sie sauber
- Startet den lokalen FastAPI-Server
- Öffnet automatisch den Browser auf `http://127.0.0.1:9876`

Beim allerersten Start klickst du im Login-Screen **„Neuen Tresor anlegen…"**
und vergibst ein Master-Passwort (min. 12 Zeichen). Pfad wird vorgeschlagen
(`%APPDATA%\OPN-Cockpit\…opnvault`).

## 3. Erstes Gerät hinzufügen

Im Inventar links in der Sidebar **„Gerät hinzufügen"** klicken und
ausfüllen:
- Name (z. B. „HQ Berlin")
- Hostname / IP (z. B. `opn-berlin.lab`)
- Port (Default 443)
- TLS verifizieren — abhaken nur bei selbst-signierten Zertifikaten
- Tags komma-getrennt (z. B. `branches, germany, core`)
- API-Key und API-Secret aus der OPNsense

## 4. Verbindung testen

Klick auf die Karte → Detail-Modal → **„Verbindung testen"**. Der Test:
1. Baut einen TLS-Handshake auf (oder akzeptiert das Risiko, wenn
   TLS-Verifikation aus ist)
2. Schickt einen GET gegen `/api/core/menu/tree` mit Basic-Auth
3. Zeigt „erreichbar + authentifiziert", „Auth abgelehnt" oder
   „nicht erreichbar"

Zusätzlich gibt es einen TCP-Heartbeat im Hintergrund (alle 30 s), der
ohne Auth-Versuch nur den Port checkt — Status-Dot wird grün/rot ohne
Last für die OPNsense.

## 5. Bulk-Import von Firewalls

Sidebar **„Bulk-Import"** für CSV oder JSON mit Stammdaten:

```csv
name,host,port,tls_verify,tags,descr,api_key,api_secret
HQ Berlin,opn-berlin.lab,443,true,branches;germany,HQ,KEY,SECRET
Branch Munich,opn-munich.lab,443,false,branches,,KEY2,SECRET2
```

Tags semikolon-getrennt (Komma kollidiert mit CSV-Trennung). Header-Zeile
zwingend. Geräte mit existierendem Namen werden übersprungen.

## 6. Aktion auf mehrere Firewalls ausrollen

Der Plan/Apply-Flow:

1. **Auswählen** — Checkbox oben rechts auf den Karten anhaken (oder
   Schnellauswahl „Alle" / „Nur erreichbare" / „Keine" über dem Grid)
2. **Aktion definieren** — Sidebar „Route hinzufügen" oder „Alias
   hinzufügen". Felder ausfüllen, optional „Vorschläge laden" für die
   Gateway/Alias-Namen.
3. **Vorschau prüfen** — Liste pro Gerät: NEW / UPDATE / SKIP mit
   Diff-Summary. Hier ist noch nichts ausgerollt.
4. **Bestätigen + Aktivieren** — Confirm-Checkbox, dann „Aktivieren".
   Parallel-Rollout über alle Geräte mit Write → Reconfigure → Read-back.
5. **Result-Matrix** — pro Gerät Status (Verifiziert/Fehlgeschlagen/
   Übersprungen) + Dauer.

## 7. Fehlgeschlagene Geräte nachziehen

Wenn ein paar Boxen offline waren:
- **Result-Phase:** Button „N fehlgeschlagene erneut versuchen" — wechselt
  zurück in die Vorschau, gefiltert auf die offenen Geräte.
- **Später:** auf jeder Karte mit offenen Aktionen erscheint ein
  Amber-Badge „N offen". Klick darauf → lädt den jüngsten betroffenen
  Plan, vorausgewählt auf das eine Gerät.

Der Plan und sein Apply-Report bleiben persistiert unter
`%APPDATA%\OPN-Cockpit\plans\{plan-id}.json` (+ `.report.json`). Web und CLI
teilen sich denselben Store.

## 8. Audit-Log einsehen

Topbar-Icon (drei Linien) öffnet das Audit-Modal. Filter nach Event-Kind,
Action, Geräte-ID. Pro Eintrag: Zeit, Event, Summary, Status-Pill.
Speicherort: `%APPDATA%\OPN-Cockpit\audit.jsonl` (append-only JSON Lines).

## 9. Vorlagen (Profile)

Im Plan-Modal kannst du „Als Vorlage speichern" — Aktionsparameter werden
in `%APPDATA%\OPN-Cockpit\profiles.json` abgelegt (ohne Credentials,
Whitelist-sanitisiert). Spätere Plans starten mit „Aus Vorlage laden".

## CLI als Alternative

Die CLI bleibt als Headless-Schnittstelle für Automatisierung erhalten.
Sub-Commands sind unverändert: `create-vault`, `add-device`, `plan`,
`apply`, `audit`, `bulk-import routes`, …

```powershell
.\.venv\Scripts\python.exe -m opn_cockpit.cli --help
```

Plan-Files und Audit-Log sind dieselben — du kannst im Web planen und
per CLI ausrollen, oder umgekehrt.

## Sicherheit auf einen Blick

- Geräte-Inventar + API-Credentials nur im verschlüsselten Tresor
  (Argon2id + AES-256-GCM, RFC 9106 Defaults).
- Master-Passwort wird beim Unlock einmalig erfragt und in der
  Session gecached — Schreibvorgänge brauchen es nicht erneut. Cache
  lebt nur während der entsperrten Session, wird beim Sperren / Auto-
  Lock überschrieben.
- Inaktivitätstimer (Default 10 min, pro Tresor änderbar) sperrt die
  Session automatisch.
- TCP-Heartbeat erzeugt keine Auth-Logs auf der OPNsense.
- Audit-Log enthält maskierte Antworten, keine vollständigen HTTP-Bodies.
- Server bindet auf `127.0.0.1:9876` (Loopback). Multi-User-Erweiterung
  ist im Schema vorbereitet (Token-Auth pro Session), aber v2.0 ist
  Single-User-PAW.
