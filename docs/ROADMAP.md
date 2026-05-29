# Roadmap: v3.x Multi-User + Linux/Container

Strategisches Architektur-Dokument. **Was wir jetzt schon berücksichtigen,
damit wir später nicht refactoren müssen.**

Stand: 2026-05-29 — nach Abschluss v2.0 + v2.1 Iter 1 (Auto-Retry).

---

## Zielbild: drei Deployment-Varianten

Alle drei teilen sich denselben Source-Tree. Der Unterschied steckt in
**Konfiguration** + **Storage-Backend** + **Auth-Backend**, nicht in
unterschiedlichen Codebases.

| Variante | Host | Auth | Storage | Erreichbar via | Use-Case |
|---|---|---|---|---|---|
| **1. Single-User Local** | Windows-PAW | Master-PW + Vault-File | Filesystem (`%APPDATA%`) | `127.0.0.1:9876` | Solo-Admin, einzelner Arbeitsplatz |
| **2. Single-User Network** | Windows-Server | Master-PW + Vault-File | Filesystem (`%APPDATA%`) | LAN-Hostname:9876 | Solo-Admin, will von verschiedenen Geräten zugreifen |
| **3. Multi-User Server** | Linux-Container (Debian 12, Proxmox/Docker) | User-DB + Bcrypt + Roles | SQLite (intern) | LAN-Hostname:9876 | Team von 2-5 Admins, geteiltes Firewall-Inventar |

Variante 1+2 sind heute **bereits funktional** — Unterschied ist nur das
Bind-Interface (`127.0.0.1` vs `0.0.0.0`). Variante 3 ist v3.0-Material.

---

## Architektur-Bruchstellen (was wir vorbereiten)

### 1. Pfad-Abstraktion → bereits jetzt fixbar

**Heute:** `config.get_app_data_dir()` nutzt `%APPDATA%` mit Fallback auf
`~/.opn-cockpit`.

**Soll:** XDG-konform für Linux. Wenn `XDG_DATA_HOME` gesetzt:
`$XDG_DATA_HOME/opn-cockpit`. Sonst `~/.local/share/opn-cockpit`. Windows
bleibt `%APPDATA%\OPN-Cockpit\`.

**Impact:** kleinste Änderung, sofort machbar. Macht den Container-Mode
schon mal möglich.

### 2. Deployment-Mode + Config-Schema → jetzt vorbereiten

**Heute:** Keine explizite Deployment-Konfiguration. Server-Settings
nur über `WebSettings.from_env()`.

**Soll:** Neues Feld in `AppSettings`:
```python
deployment_mode: Literal["single-local", "single-network", "multi-server"] = "single-local"
auth_backend: Literal["vault", "user-db"] = "vault"
storage_backend: Literal["filesystem", "sqlite"] = "filesystem"
```

`WebSettings.from_env()` liest diese, bindet `127.0.0.1` für `single-local`,
`0.0.0.0` für die anderen. Die Felder existieren **jetzt schon**, werden
aber erst in v3 wirklich genutzt — Code prüft nur den Default-Fall.

### 3. Storage-Abstraktion → später, aber Bruchstelle identifiziert

**Heute:** Direkter Filesystem-Zugriff in:
- [vault/store.py](../src/opn_cockpit/vault/store.py) — `.opnvault`-Files
- [audit/log.py](../src/opn_cockpit/audit/log.py) — `audit.jsonl`
- [orchestration/plan_store.py](../src/opn_cockpit/orchestration/plan_store.py) — `{plan_id}.json` + `.report.json`
- [profiles/store.py](../src/opn_cockpit/profiles/store.py) — `profiles.json`

**Soll für v3 (Multi-User):**
- Audit-Log → DB-Tabelle `audit_events` (statt JSONL)
- Plans + Reports → DB-Tabellen `plans` und `plan_reports`
- Profiles → DB-Tabelle `profiles`
- **Vault bleibt File** — auch im Multi-User-Modus. Wird beim Server-Start
  einmal entsperrt, lebt im Server-Memory. User-DB regelt nur, wer auf
  den Server logged.

**Pattern:** Jedes Storage-Modul kriegt ein Interface, das von zwei
Implementierungen erfüllt wird:
```python
class AuditBackend(Protocol):
    def append(self, event: AuditEventKind, **fields) -> AuditRecord: ...
    def query(self, filters: AuditFilter) -> list[AuditRecord]: ...
```
- `FileAuditBackend` (jetzt, wraps JSONL)
- `SqlAuditBackend` (später, SQLAlchemy)

Das Refactoring kann **schrittweise** erfolgen — wir müssen nicht alle
vier Stores gleichzeitig migrieren. Audit ist sinnvollster Einstieg
(größtes Volumen, am meisten Mehrwert durch SQL-Filter).

### 4. Auth-Backend-Abstraktion → vorbereiten, später aktivieren

**Heute:** `SessionManager` ist Token-basiert vorbereitet — gut!
Aber alle Sessions kommen vom `Vault.open_vault()` → entsperrte Session.

**Soll für v3:**
```python
class AuthBackend(Protocol):
    def login(self, credentials: dict) -> Session | None: ...

class VaultAuthBackend:        # heute
    def login(self, credentials):
        password = credentials["master_password"]
        opened = open_vault(self.vault_path, password)
        return Session.from_opened(opened, password)

class UserDbAuthBackend:        # später
    def login(self, credentials):
        user = self.user_db.authenticate(credentials["user"], credentials["password"])
        if not user: return None
        # Vault wird vom Server-Start beim Boot entsperrt, ist global verfügbar
        return Session.from_user(user, self.global_vault)
```

Im Server-Modus entsperrt der Service-User beim Boot **einmal** den
zentralen Vault (Master-PW kommt aus einer Service-Config oder per
Web-UI beim ersten Start). Danach loggen sich User mit User+PW gegen
die User-DB ein und teilen sich den global entsperrten Vault.

**Bewusstes Trade-off:** Im Multi-User-Server-Modus hat der Server-Admin
(root/Linux-User) effektiv Zugriff auf alle Firewall-Credentials, weil
der entsperrte Vault im Server-Memory liegt. Das ist die Konsequenz
der Antwort "Server kennt alle Boxen". Trust-Modell ist explizit
dokumentiert.

### 5. Hintergrund-Watcher → Mode-aware

**Heute:** RetryWatcher hängt an einer Session, stirbt beim Auto-Lock.

**Soll für v3:**
- `single-local`: wie heute, Watcher endet bei Auto-Lock
- `multi-server`: Watcher läuft als globaler Service-Thread, weil
  der zentrale Vault entsperrt bleibt. Pro Plan-Apply mit failures
  bekommt der Watcher einen Job, läuft bis Erfolg oder max_duration.

Die `RetryWatcher`-Klasse selbst muss kaum geändert werden — sie
braucht nur einen `session_resolver(token) -> Session` der im
Server-Modus die globale Session statt User-Token nutzt.

### 6. Inventory-ACLs → Datenmodell-Vorbereitung

**Heute:** Jeder, der den Vault öffnet, sieht alle Geräte.

**Soll für v3:** User-DB hat:
- Users mit Roles (`viewer`, `operator`, `admin`)
- Optional: Device-Tags-Whitelist pro User (z.B. User "Branch-Admin"
  darf nur Geräte mit Tag `branches` sehen)

Datenmodell-Stub für v3:
```sql
CREATE TABLE users (
  id INTEGER PRIMARY KEY,
  username TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL,  -- bcrypt
  role TEXT NOT NULL,           -- viewer/operator/admin
  allowed_tags TEXT             -- comma-separated, NULL = alle
);
```

---

## Container/Docker/Proxmox

### Phase 1: Dockerfile + docker-compose

```dockerfile
FROM debian:12-slim
RUN apt-get update && apt-get install -y python3 python3-pip python3-venv \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml src ./
RUN python3 -m venv .venv && .venv/bin/pip install -e .
EXPOSE 9876
VOLUME ["/data"]
ENV OPNCOCKPIT_DATA_DIR=/data
ENV OPNCOCKPIT_HOST=0.0.0.0
ENV OPNCOCKPIT_DEPLOYMENT_MODE=multi-server
CMD [".venv/bin/python", "-m", "opn_cockpit"]
```

### Phase 2: Proxmox-Helper-Skript

Nach Vorbild von [community-scripts/ProxmoxVE](https://github.com/community-scripts/ProxmoxVE) —
ein Bash-Skript, das auf einem Proxmox-Host läuft und einen LXC-Container
mit Debian 12 + automatischer OPN-Cockpit-Installation hochzieht.

```bash
bash -c "$(wget -qLO - https://raw.githubusercontent.com/<org>/opn-cockpit/main/installer/proxmox-helper.sh)"
```

Im Helper:
- LXC-Container anlegen (1 vCPU, 512 MB RAM reichen)
- Debian 12 base
- apt install python3 + uv
- Repo clonen, venv setup
- systemd-Unit für `opn_cockpit.service`
- Default-Setup-Modus „multi-server, sqlite", erstes Login = Bootstrap-Token

### Phase 3: systemd-Unit

```ini
[Unit]
Description=OPN-Cockpit Multi-Site OPNsense Manager
After=network.target

[Service]
Type=simple
User=opncockpit
Group=opncockpit
WorkingDirectory=/opt/opn-cockpit
Environment="OPNCOCKPIT_DATA_DIR=/var/lib/opn-cockpit"
Environment="OPNCOCKPIT_HOST=0.0.0.0"
Environment="OPNCOCKPIT_DEPLOYMENT_MODE=multi-server"
ExecStart=/opt/opn-cockpit/.venv/bin/python -m opn_cockpit
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

---

## Windows-Installer-Upgrade

### Heute (v2.0)
- Prüft Python + uv, **bricht ab** wenn fehlt.

### Soll für v2.1+
- Prüft Python + uv
- **Bei Bedarf: Download anbieten** mit Fortschrittsbalken
- Nach Installation: Setup nahtlos weiter
- Bei Single-User-Modus: Desktop-Verknüpfung auf `127.0.0.1:9876`
- Bei Network-Modus (User-Wahl im Installer): Bind auf `0.0.0.0`, Windows-
  Dienst über `nssm.exe` (mit ins Installer-Bundle gepackt) anlegen
- Bei beiden Modi: **Autostart bei Systemstart** zwingend (Single-User
  als „Start-Menü Autostart"-Eintrag, Network als Windows-Dienst)
- Desktop-Verknüpfung öffnet den eigenen Server-URL (lokal oder
  Hostname) im Default-Browser

Inno-Setup-Skript bekommt:
- `[Components]` mit Choice „Single-User (lokal)" vs „Single-User
  (Netzwerk)" vs „Service-Mode"
- `[Code]` mit Download-Funktion für fehlende Vorvoraussetzungen
  (Inno-Setup hat einen `idpsetup`-Plugin oder `ITDownloader`)

---

## Konkrete nächste Iterationen

### v2.2 (sofort umsetzbar, ohne v3-Architektur)
- [ ] config.py auf XDG-Pfad für Linux (Quick-Win, heute gemacht)
- [ ] `deployment_mode` + `auth_backend` + `storage_backend` als Felder
      in AppSettings (Quick-Win, heute gemacht)
- [ ] Installer: Python+uv-Auto-Download via `ITDownloader` oder
      `idpsetup`-Plugin
- [ ] Installer: User-Wahl Single-User vs Service-Mode
- [ ] Installer: Service-Mode via NSSM (mit-bundlen)
- [ ] Dockerfile + docker-compose.yml für Linux-Test
- [ ] Audit-Backend-Interface (FileAuditBackend extrahiert,
      SqlAuditBackend als Stub)

### v3.0 (großer Sprung)
- [ ] User-DB-Modul (`auth/users.py`, SQLite)
- [ ] `AuthBackend`-Interface mit Vault- und UserDb-Implementierung
- [ ] Multi-User-Login-Screen (Username + Passwort statt nur Master-PW)
- [ ] Inventory-ACL (User darf nur ihre Tag-Whitelist sehen)
- [ ] SQL-Storage für Audit, Plans, Profiles (SQLAlchemy)
- [ ] Proxmox-Helper-Skript
- [ ] systemd-Unit
- [ ] Migration-Skript: bestehender Filesystem-Storage → SQLite

### v3.1+
- [ ] Reverse-Proxy-Modus (nginx vorhängen, TLS-Termination)
- [ ] LDAP/AD-Auth-Backend
- [ ] Audit-Export (CSV, Syslog)
- [ ] Webhook-Integration (Alerts bei wiederholten Apply-Fehlern)

---

## Was wir NICHT vorbereiten

Bewusst raus, weil es uns einfriert oder den Single-User-Pfad verkompliziert:

- **Keine ORM-Migration für File-Stores in v2** — Vault bleibt File,
  Audit bleibt JSONL bis v3. Erst dann SQL.
- **Kein Plugin-System** — alle Subsysteme bleiben in `core/objects/`.
- **Keine API-Versionierung** — `/api/...` heute, `/api/v1/...` erst
  wenn wir einen Breaking-Change brauchen.
- **Keine WebSockets** — alle Updates per Polling. Für 2-5 User in
  einem Tool das pro Aktion ein paar Sekunden Apply braucht ist das
  völlig ausreichend.

---

## Homogenität: Was muss in allen Varianten gleich sein

| Bereich | Quelle der Wahrheit |
|---|---|
| Frontend-Code (HTML/JS/CSS) | identisch in allen Varianten |
| Core-Logic (HTTP-Client, Plan/Apply, Adapter) | identisch |
| API-Schemas (Pydantic) | identisch |
| API-Routen | identisch — Auth-Backend tauscht nur das Login |
| Audit-Format | Schema gleich, Storage tauschbar |
| Profile-Format | Schema gleich, Storage tauschbar |

**Ein-Bug-fixt-überall** ist das Versprechen.

---

## Wann was

| v2.2 | Quick-Wins (Pfade, Settings-Felder, Installer-Polish, Dockerfile). |
| v3.0 | Multi-User-Server-Modus mit SQLite + User-DB. |
| v3.x | Polishing (LDAP, Proxy-Modus, Webhooks). |

v2.2 kostet ein paar Iterationen aber bricht nichts. v3.0 ist ein
größerer Sprung, aber durch die Vorbereitungen in v2.2 dann
inkrementell umsetzbar.
