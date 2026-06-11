# Quickstart — OPN-Cockpit v2

Web-Oberfläche im Browser. Vom ersten Tresor bis zum verifizierten Rollout
in ca. 10 Minuten.

## 1. Setup (einmalig)

Endnutzer installieren über den fertigen Installer — siehe
[INSTALLATION-WINDOWS.md](INSTALLATION-WINDOWS.md) oder
[../installer/linux/README.md](../installer/linux/README.md).

Aus dem Source-Tree für Dev-Arbeit:

Voraussetzung: Python 3.11+ und
[uv](https://docs.astral.sh/uv/getting-started/installation/).

```powershell
uv sync                              # erzeugt .venv\ + installiert Runtime + Dev-Tooling
```

## 2. App starten

```powershell
uv run python -m opn_cockpit
```

- Startet den lokalen FastAPI-Server auf `http://127.0.0.1:9876`
- Öffnet automatisch den Browser

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
3. **Vorschau prüfen** — Liste pro Gerät: NEW / UPDATE / SKIP / DELETE
   mit Diff-Summary. Hier ist noch nichts ausgerollt.
4. **Optional: „Mit Sicherheitsnetz ausrollen"** — wird angeboten wenn
   mindestens ein Ziel-Gerät SSH konfiguriert hat. Nach Verify hat man
   X Sekunden zu bestätigen, sonst SSH-Rollback auf Pre-Apply-Backup.
   Siehe [FEATURES.md → Safety-Net](FEATURES.md#safety-net-via-ssh).
5. **Bestätigen + Aktivieren** — Confirm-Checkbox, dann „Aktivieren".
   Parallel-Rollout über alle Geräte mit Write → Reconfigure → Read-back.
6. **Result-Matrix** — pro Gerät Status (Verifiziert/Fehlgeschlagen/
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

## 10. Inventar pro Gerät: Live-Listen + Edit/Delete

Klick auf eine Karte → Device-Modal mit sechs Tabs:

- **Info** — Test-Connection, Update-Check, Bearbeiten, Duplizieren,
  Backup herunterladen
- **Updates** — installierte/verfügbare OPNsense-Version
- **Backups** — alle lokal gespeicherten Backups dieses Gerätes
  (Pre-Apply / Post-Apply / Manuell / Scheduled) zum Download,
  plus „Backup erzeugen" (server-only ohne Browser-Download-Dialog)
- **Aliase** — Live-Liste mit Filter + Bearbeiten/Löschen pro Eintrag
- **Routen** — Live-Liste der statischen Routen + Bearbeiten/Löschen
- **Regeln** — Live-Liste der **Automation-Filter-Regeln** (Firewall →
  Automation → Filter), plus „Neue Regel" / Bearbeiten / Löschen. Ab
  OPNsense 23.7 in Core integriert (vorher als `os-firewall`-Plugin).
  Klassische „Firewall → Rules" (Legacy-XML) sind nicht API-zugänglich
  und werden nicht angezeigt
- **DNS** — drei Sub-Tabs: **Host-Overrides** (CRUD, „Neuer
  Host-Override" / Bearbeiten / Löschen), **Domain-Overrides**
  (read-only) und **Abfrage-Weiterleitungen** (read-only — die
  globalen Query-Forwards inkl. DoT/DoH)

Bearbeiten/Löschen läuft immer durch den Plan/Apply-Flow:
Vorschau → Bestätigen → Apply mit Pre-Apply-Backup + Audit-Eintrag.
Identitäts-Felder (Alias-Name, Netz+Gateway, host+domain) sind beim
Edit gesperrt, um aus einer Aktion zwei zu machen.

## 11. Config-Compare zwischen Geräten

Mindestens zwei Karten markieren → in der Selektions-Toolbar
**„Vergleichen"** klicken.

Tab-Strip oben: *Aliase | Routen | Regeln | DNS-Hosts | DNS-Overrides |
DNS-Weiterleitungen*. Tab-Klick lädt die jeweilige Matrix. Die linkeste
Spalte ist der Master (per ◀ / ▶ / ★ verschiebbar); andere Spalten
werden master-relativ farbig markiert: grün = identisch, gelb = Drift,
leer = fehlt, ? = unerreichbar. Jede Zeile ist per ▶-Icon aufklappbar
und zeigt den vollen Inhalt pro Gerät.

Bei **Aliase** und **DNS-Hosts** erscheint zusätzlich ein
**„Sync ←"**-Button in Drift-Zeilen: Klick erzeugt einen Plan
(`add_alias` bzw. `add_unbound_host`) vom Master zu allen anderen
Spalten und springt direkt in die Preview.

## 12. Audit-Log einsehen + exportieren

Topbar-Icon (drei Linien) öffnet das Audit-Modal:
- Filter nach Event-Kind / Action / Geräte-ID
- **Integrität prüfen** — verifiziert die HMAC-Hash-Chain (nur sinnvoll
  bei SQLite-Backend, das Default im Server-Mode)
- **Als CSV exportieren** — alle gefilterten Records als CSV
- **Als PDF (signiert)** — gefilterte Records als A4-Querformat-Report
  mit HMAC-SHA256-Signatur im Footer + in den PDF-Metadaten

## 13. Interne PKI integrieren (optional)

Wer eine interne CA betreibt, hat zwei Hebel im **Tresor-Einstellungen**-Modal:

- **Vertrauenswürdige Root-CAs** — die interne CA einmal als PEM
  einfügen → Cockpit akzeptiert alle damit ausgestellten OPNsense-Certs
  ohne pro-Gerät-`tls_verify=false`. Inspect-Preview vor Save.
- **Cockpit HTTPS-Zertifikat** — eigenes Server-Cert + Key für Cockpit
  selbst hinterlegen, damit `https://cockpit.lab:9876` ohne Warnung
  funktioniert. App-weit (in `settings.json`, nicht im Tresor),
  Restart erforderlich.

Schritt-für-Schritt mit step-ca / OpenSSL / AD-CS-Beispielen in
[FEATURES.md → Interne CAs vertrauen](FEATURES.md#interne-cas-vertrauen)
und [FEATURES.md → HTTPS für Cockpit selbst](FEATURES.md#https-fuer-cockpit-selbst).

## 14. Safety-Net via SSH

Optional pro Gerät: SSH-Zugang mit Private-Key in den Tresor legen
(Gerät → Bearbeiten → unten „Safety-Net via SSH aktivieren" + Felder
ausfüllen). Beim Apply erscheint dann die Checkbox **„Mit
Sicherheitsnetz ausrollen"** — nach Verify hat man X Sekunden zum
Bestätigen, sonst SSH-Rollback auf das Pre-Apply-Backup.

Im selben Edit-Dialog gibt es neben der Checkbox einen
**„Anleitung"-Link** — ein Modal mit den `ssh-keygen`-Befehlen für
Windows und Linux + dem OPNsense-UI-Pfad, falls du den Setup-Flow
schnell brauchst. Im Modal oben gibt es zusätzlich
**Helper-Scripts zum Download** (PowerShell für Windows, Bash für
Linux/macOS), die das Key-Paar in einem Schritt erzeugen, den
Public-Key in die Zwischenablage legen und beide Keys im Editor öffnen
— bequemer Pfad ohne manuelle CLI-Befehle. Längere Version inkl.
Troubleshooting:
[FEATURES.md → Safety-Net via SSH](FEATURES.md#safety-net-via-ssh).

## 15. Wartungsmodus für planmäßig offline Geräte

Wenn ein Standort längere Zeit aus ist (Hardware-Tausch, Mobile-Rack im
Transit), Gerät → Bearbeiten → **„Wartungsmodus (Polling
deaktivieren)"** anhaken. Heartbeat, Scheduled Backups und Drift-Check
überspringen das Gerät; der Audit-Log bleibt sauber. Manuelle Aktionen
(Test-Connection, Plan/Apply, Backup-Download) bleiben möglich.

Die Karte zeigt dann einen neutralen Status-Dot + „Wartung"-Badge
statt rot/grün.

## 16. Mein Konto (Passwort + 2FA)

Multi-User-Mode: Topbar → Person-Icon **„Mein Konto"**. Im selben
Modal: Passwort ändern (oben) und Zwei-Faktor-Authentifizierung
einrichten/verwalten (unten). TOTP-Details + Backup-Codes-Handhabung
siehe [FEATURES.md → TOTP](FEATURES.md#zwei-faktor-authentifizierung-totp).

## 17. Disk-Space im Auge behalten (Server)

Auf Linux-Servern und Multi-User-Windows-Setups zeigt die Topbar einen
schmalen **Progress-Bar mit Prozent** für den belegten Speicher auf
dem App-Data-Volume (Backups, Audit-Log, SQLite-DBs). Hover-Tooltip
zeigt Pfad + Free-GB. Gelb ab 80 %, rot ab 92 % (einmaliger Toast).
Auf Single-User-Windows ist das Widget ausgeblendet.

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
