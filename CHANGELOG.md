# Changelog

Alle nennenswerten Änderungen pro Release.

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
