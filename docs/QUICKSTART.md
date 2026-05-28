# Quickstart — OPN-Cockpit v1

End-to-End-Walkthrough vom ersten Tresor bis zum verifizierten Rollout
einer Route auf mehreren OPNsense-Instanzen. Dauert ca. 10 Minuten.

## 0. Vor dem ersten Live-Lauf

Bitte einmal den **API-Spike** gegen die laufende OPNsense-Test-Instanz
durchführen und die in [opnsense-api-26.1.md](opnsense-api-26.1.md)
notierten Endpoints bestätigen. Das Tool ist gegen die dort dokumentierten
Pfade gebaut. Wenn deine Version andere Pfade hat, passen sich nur die
Konstanten in `src/opn_cockpit/core/objects/_endpoints.py` an —
Orchestrierung, CLI und GUI bleiben unverändert.

## 1. Setup

```powershell
# Im Repo-Root:
.\scripts\setup-venv.ps1
```

Das Skript erzeugt `.venv\`, installiert das Paket editierbar samt
Dev-Tooling und führt `pytest -q` als Sanity-Check.

## 2. Tresor anlegen

```powershell
.\.venv\Scripts\python.exe -m opn_cockpit.cli `
    create-vault C:\vaults\produktion.opnvault
```

Du wirst nach einem Master-Passwort gefragt (min. 12 Zeichen). Der Tresor
wird mit Argon2id-Schlüsselableitung + AES-256-GCM verschlüsselt geschrieben.

## 3. Erstes Gerät hinzufügen

```powershell
.\.venv\Scripts\python.exe -m opn_cockpit.cli `
    --vault C:\vaults\produktion.opnvault add-device `
    --name "HQ Berlin" `
    --host opn-berlin.lab `
    --port 443 `
    --tls-verify `
    --tags branches,germany,core
```

Anschließend interaktiv:
- Master-Passwort des Tresors (zum Entsperren)
- API-Key des OPNsense-Geräts
- API-Secret des OPNsense-Geräts
- nochmal Master-Passwort (zum Speichern der Änderung)

Wiederholt für jede weitere OPNsense.

## 4. Verbindungstest

```powershell
.\.venv\Scripts\python.exe -m opn_cockpit.cli `
    --vault C:\vaults\produktion.opnvault test-connection --target all
```

Erwartete Ausgabe:

```
Gerät                          Status         Hinweis
------------------------------------------------------------------------------
HQ Berlin                      OK             erreichbar + authentifiziert (opn-berlin.lab)
HQ München                     OK             erreichbar + authentifiziert (opn-muenchen.lab)
Zweigstelle Hamburg            NO-AUTH        erreichbar, aber Auth abgelehnt: Schlüssel/Secret falsch
```

## 5. Erste Route ausrollen

### Plan erzeugen

```powershell
.\.venv\Scripts\python.exe -m opn_cockpit.cli `
    --vault C:\vaults\produktion.opnvault plan add-route `
    --network 10.99.0.0/24 `
    --gateway WAN_GW `
    --descr "Pilot-Tunnel" `
    --target tag:branches
```

Du siehst die Vorschau pro Gerät: `NEW` (wird angelegt), `SKIP`
(existiert bereits identisch), inklusive maskierter Payload. Am Ende
wird die Plan-ID gedruckt, z. B. `pl-A1B2C3D4`.

### Plan ausrollen

```powershell
.\.venv\Scripts\python.exe -m opn_cockpit.cli `
    --vault C:\vaults\produktion.opnvault apply pl-A1B2C3D4
```

Vor der Ausführung erscheint die Vorschau erneut und verlangt eine
explizite Bestätigung mit `ja` (R-PRE-2). Anschließend laufen pro Gerät:

1. **WRITE** — alle `add`-Calls
2. **ACTIVATE** — genau ein `reconfigure`
3. **VERIFY** — Read-back gegen den Such-Endpoint

Die Result-Matrix zeigt pro Gerät den finalen Status:

```
Gerät                          Status   Phase      ms  Hinweis
------------------------------------------------------------------------
HQ Berlin                      OK       verify   1240  1 Eintrag/Einträge ok.
HQ München                     OK       verify   1180  1 Eintrag/Einträge ok.

Gesamt: 2/2 ok, 0 fehlgeschlagen, 0 übersprungen
```

## 6. Audit-Log einsehen

```powershell
.\.venv\Scripts\python.exe -m opn_cockpit.cli audit --limit 20
```

Alle Aktionen, Vault-Operationen und Resultate sind als JSON-Lines unter
`%APPDATA%\OPN-Cockpit\audit.jsonl` persistiert. Klartext-Secrets erscheinen
dort nie — sensitive Schlüssel werden vor dem Schreiben durch
`security.masking.mask_dict` maskiert.

## 7. Wiederverwendbares Profil speichern

```powershell
.\.venv\Scripts\python.exe -m opn_cockpit.cli profile save-route `
    --name "Standard Branch Tunnel" `
    --network 10.99.0.0/24 `
    --gateway WAN_GW `
    --target tag:branches
```

Profile enthalten **keine** Credentials und sind portabel. Anwenden:

```powershell
.\.venv\Scripts\python.exe -m opn_cockpit.cli `
    --vault C:\vaults\produktion.opnvault `
    profile apply "Standard Branch Tunnel"
```

## 8. Bulk-Import aus CSV

```powershell
.\.venv\Scripts\python.exe -m opn_cockpit.cli `
    --vault C:\vaults\produktion.opnvault `
    bulk-import routes docs\example-routes.csv --target tag:branches
```

Validierungsfehler werden pro Zeile gemeldet. Beim Teil-Erfolg fragt
das Tool nach, ob trotzdem ausgerollt werden soll. Plan-Vorschau und
`ja`-Bestätigung bleiben Pflicht.

## 9. Tresor an anderen Admin weitergeben (Template)

```powershell
.\.venv\Scripts\python.exe -m opn_cockpit.cli `
    --vault C:\vaults\produktion.opnvault `
    export-template C:\vaults\template-fuer-kollegen.opnvault
```

Der Empfänger kennt das Master-Passwort (sicheres Kanalbedarf). Nach dem
Öffnen leert das Template alle `api_key` und `api_secret`. Der Kollege
trägt seine eigenen Credentials ein und kann das Master-Passwort ändern.

## 10. GUI starten

```powershell
.\.venv\Scripts\python.exe -m opn_cockpit
```

Login-Fenster → Master-Passwort → Hauptfenster mit Tabs (Inventar,
Audit-Log) und Aktions-Menü. Inaktivitäts-Sperre nach 10 Minuten (im
Tresor anpassbar).

## Fehlersuche

| Symptom | Lösung |
|---|---|
| `Tresor-Datei nicht gefunden` | Pfad mit `--vault` prüfen oder Default in `%APPDATA%\OPN-Cockpit\settings.json` setzen. |
| `Master-Passwort falsch` | Passwort prüfen. Audit-Log enthält `LOGIN_FAILED`-Einträge. |
| `Auth abgelehnt` | API-Key/Secret in der OPNsense unter *System → Access → Users → API Key* erneut erzeugen. |
| Read-back schlägt fehl | OPNsense-Version + Endpoint-Pfade in `docs/opnsense-api-26.1.md` abgleichen. |
| GUI startet nicht | `python -m opn_cockpit.cli --help` prüfen (CLI funktioniert weiterhin). PySide6 via `uv pip install PySide6` nachinstallieren. |
