# Linux-Installation (Multi-User-Server)

Drei Wege, OPN-Cockpit auf Linux laufen zu bekommen — vom Quick-Test bis
zum produktiven Container.

**Standard ist seit v3.x Multi-User-Server.** Beim ersten Start legt der
Server automatisch einen Default-Admin an (`admin` / `OPN-Cockpit!`)
mit Pflicht-Passwort-Wechsel beim ersten Login. Kein Bootstrap-Token mehr.

## Variante 1: Docker (am einfachsten, auch unter Windows via Docker Desktop)

Quelltree enthält ein `Dockerfile` + `docker-compose.yml`:

```bash
git clone https://github.com/ludwig-systems/opn-cockpit.git
cd opn-cockpit
docker compose up -d
```

Daten landen im Named-Volume `opn-cockpit-data` (Vault + User-DB + Audit + Settings).
Port `9876` ist nach außen gemappt.

## Variante 2: Direkt auf Debian-/Ubuntu-Host als systemd-Service

```bash
# Repo clonen
git clone https://github.com/ludwig-systems/opn-cockpit.git /tmp/opn-cockpit
cd /tmp/opn-cockpit

# Installer ausführen (braucht root)
sudo bash installer/linux/install.sh --source .
```

Was passiert:
- Pakete: `python3`, `python3-venv`, `python3-dev`, `build-essential`, `git`, `rsync`
- Service-User `opncockpit` (system user, kein Login-Shell)
- Verzeichnisse: `/opt/opn-cockpit` (Code) + `/var/lib/opn-cockpit` (Daten)
- Python-venv unter `/opt/opn-cockpit/.venv`
- systemd-Unit aktiviert und gestartet
- Bind auf `0.0.0.0:9876`

Konfigurierbare Pfade per Env überschreibbar:
```bash
OPNCOCKPIT_INSTALL_DIR=/usr/local/opn-cockpit \
OPNCOCKPIT_DATA_DIR=/srv/opn-cockpit-data \
sudo -E bash installer/linux/install.sh --source .
```

Service-Operationen:
```bash
systemctl status opn-cockpit
systemctl restart opn-cockpit
journalctl -u opn-cockpit -f
```

## Variante 3: Proxmox-LXC via Helper-Skript

Auf einem Proxmox-VE-Host als root:

```bash
bash -c "$(wget -qLO - https://raw.githubusercontent.com/ludwig-systems/opn-cockpit/main/installer/linux/proxmox-helper.sh)"
```

Eigener Fork / anderer Branch:
```bash
OPNCOCKPIT_REPO_URL=https://github.com/foo/opn-cockpit.git \
OPNCOCKPIT_REPO_BRANCH=feature/xyz \
bash -c "$(wget -qLO - https://raw.githubusercontent.com/ludwig-systems/opn-cockpit/main/installer/linux/proxmox-helper.sh)"
```

**TUI-Wizard (whiptail)** im Stil von community-scripts.org. Storage
und Bridge werden als echte Menüs angezeigt — du wählst per Pfeil-
tasten + Enter, kein Frei-Text. Eingabefelder mit sinnvollen Defaults,
Esc = Abbruch.

| Schritt | Typ | Default | Bemerkung |
|---|---|---|---|
| Container-ID | Eingabe | nächste freie | `pvesh get /cluster/nextid` |
| Hostname | Eingabe | `opn-cockpit` | per Env `CT_HOSTNAME` vorbelegbar |
| Storage-Pool | **Menü** | — | nur Pools mit Content=Container (sortiert nach Größe) |
| Disk-Größe (GB) | Eingabe | 2 | OPN-Cockpit ~200 MB, Rest für Logs/DB |
| CPUs | Eingabe | 1 | reicht bis ~25 Firewalls |
| RAM (MB) | Eingabe | 512 | |
| Bridge | **Menü** | — | alle vmbr* auf dem Host |
| IP-Konfiguration | **Menü** | DHCP | DHCP oder Statisch |
| IPv4 + CIDR | Eingabe | — | nur bei Statisch, z.B. `192.168.1.100/24` |
| Gateway | Eingabe | — | nur bei Statisch |
| VLAN-Tag | Eingabe | leer | optional |
| MAC-Adresse | Eingabe | leer | leer = Proxmox vergibt |
| DNS-Server | Eingabe | leer | leer = vom Host; sonst kommagetrennt |
| DNS-Search-Domain | Eingabe | leer | optional |
| Root-Passwort | Passwordbox | leer | leer = kein PW (Zugang nur per `pct enter` vom Host). Wer SSH ins Container will, setzt hier eins (min. 5 Zeichen, Wiederholung) |
| Zusammenfassung | **Ja/Nein** | — | letzte Bestätigung vor `pct create` |

Am Ende: Whiptail-Msgbox mit Container-Daten + Default-Login,
zusätzlich auf der Konsole damit Browser-URL anklickbar bleibt.

Was passiert:
- Debian-12-Standard-Template wird (falls nötig) heruntergeladen
- LXC-Container wird angelegt (unprivileged, nesting=1)
- Container startet
- `install.sh` läuft im Container, OPN-Cockpit ist als systemd-Service aktiv
- IP wird gemeldet → UI ist sofort unter `http://<container-ip>:9876` erreichbar

Anschließend:
```bash
pct enter <ct-id>                                    # Shell im Container
pct exec <ct-id> -- journalctl -u opn-cockpit -f     # Logs
pct stop <ct-id>                                     # Stop
```

## Erster Login

Browser auf `http://<host-or-container-ip>:9876` öffnen. Der Setup-Wizard
zeigt **einen** Schritt mit zwei Sektionen:

**1. Admin-Login (Default-Admin):**
- Benutzer: `admin`
- Aktuelles Passwort: `OPN-Cockpit!`
- Neues Admin-Passwort: deins (min. 12 Zeichen, ≠ Default)
- Wiederholen

**2. Zentraler Tresor:**
- Pfad: `/var/lib/opn-cockpit/firewalls.opnvault`
- Master-Passwort: deins (min. 12 Zeichen)
- ✅ „Tresor neu anlegen, falls die Datei nicht existiert"

→ „Tresor entsperren / anlegen" klicken. Server wird `ready`, du landest
im Multi-User-Login-Screen. Dort meldest du dich mit deinem neuen
Admin-Passwort an, siehst die leere Inventar-Ansicht und kannst loslegen.

Default-Admin im Log:
```bash
journalctl -u opn-cockpit -n 50 | grep -A 4 Default-Admin
```

## Konfiguration

systemd-Unit lebt unter `/etc/systemd/system/opn-cockpit.service`. Environment-
Variablen direkt darin anpassen:

| Variable | Default | Bedeutung |
|---|---|---|
| `OPNCOCKPIT_DATA_DIR` | `/var/lib/opn-cockpit` | Daten-Root (Vault, User-DB, Audit-DB) |
| `OPNCOCKPIT_VAULT_PATH` | `/var/lib/opn-cockpit/firewalls.opnvault` | Vault-Datei-Pfad |
| `OPNCOCKPIT_VAULT_DIR` | `/var/lib/opn-cockpit` | Erlaubte Vault-Basis (Validator) |
| `OPNCOCKPIT_AUTH_BACKEND` | `user-db` | Multi-User aktivieren |
| `OPNCOCKPIT_DEPLOYMENT_MODE` | `multi-server` | Server-Mode |
| `OPNCOCKPIT_STORAGE_BACKEND` | `sqlite` | SQLite für Audit + Plans (statt JSONL) |
| `OPNCOCKPIT_HOST` | `0.0.0.0` | Bind-Interface |
| `OPNCOCKPIT_PORT` | `9876` | Port |
| `OPNCOCKPIT_NO_BROWSER` | `1` | Kein Browser-Auto-Open auf einem Server |

Nach Änderung:
```bash
systemctl daemon-reload
systemctl restart opn-cockpit
```

## Update

**Empfohlen — derselbe Link wie bei der Installation, im Container ausgeführt:**

```bash
# In der Container-Shell (pct enter / SSH)
bash -c "$(wget -qLO - https://raw.githubusercontent.com/ludwig-systems/opn-cockpit/main/installer/linux/proxmox-helper.sh)"
```

Der Helper erkennt automatisch:
- **Proxmox-Host** (pveam vorhanden) → TUI-Wizard für neuen Container
- **Container mit OPN-Cockpit** (`/opt/opn-cockpit` + systemd-Unit) → Update-Modus

Im Update-Modus zeigt der TUI-Dialog explizit:
- Aktuelle Version + Commit-Hash → Neue Version
- Was angefasst wird: nur `/opt/opn-cockpit` (Code)
- Was unverändert bleibt: `/var/lib/opn-cockpit/` (Vault, Audit, User-DB, Settings)

Nach Bestätigung läuft:
1. `systemctl stop opn-cockpit`
2. `git fetch --depth 1 origin <branch>` + `git reset --hard origin/<branch>`
3. `pip install -e .` (neue/geänderte Dependencies)
4. `systemctl start opn-cockpit` — beim Start läuft `run_pending_migrations()`
5. Migrations schreiben **vor** Schema-Änderungen ein Backup in `/var/lib/opn-cockpit/backups/<ts>-pre-<version>/`

**Manuell ohne Helper** (z.B. für Skripte):

```bash
# Im Container
sudo systemctl stop opn-cockpit
sudo -u opncockpit git -C /opt/opn-cockpit fetch --depth 1 origin main
sudo -u opncockpit git -C /opt/opn-cockpit reset --hard origin/main
sudo -u opncockpit /opt/opn-cockpit/.venv/bin/pip install --quiet --upgrade -e /opt/opn-cockpit
sudo systemctl start opn-cockpit
```

**Vom Proxmox-Host aus** (Einzeiler):

```bash
pct exec <ct-id> -- bash -c "$(wget -qLO - https://raw.githubusercontent.com/ludwig-systems/opn-cockpit/main/installer/linux/proxmox-helper.sh)"
```

### Was passiert NICHT beim Update

User-Daten werden **nicht angefasst**. Konkret:

- Tresor (`firewalls.opnvault`) bleibt — Inventar + API-Credentials intakt
- User-DB (`users.db`) bleibt — Admin-Login + andere User intakt
- Audit-Log (`audit.db`) bleibt — Historie erhalten
- Plans + Reports bleiben
- Settings (`settings.json`) bleibt

Nach Update kannst du dich direkt mit deinen bestehenden
Credentials weiter einloggen.

## Backup

Daten liegen in `OPNCOCKPIT_DATA_DIR` (Default `/var/lib/opn-cockpit/`):

```bash
systemctl stop opn-cockpit
tar czf opn-cockpit-backup-$(date +%F).tar.gz -C /var/lib opn-cockpit
systemctl start opn-cockpit
```

Wichtig: das sind Vault + User-DB + Audit-DB + Plan-Reports + Backups.
Komplette Datei → komplettes Restore.

## Sicherheit auf Linux

- Service läuft als unprivilegierter User `opncockpit`
- systemd-Unit hat Hardening-Flags (NoNewPrivileges, ProtectSystem=strict,
  PrivateTmp, ProtectHome, ProtectKernelTunables/Modules etc.)
- Default-Admin-Passwort ist **bekannt** — Pflicht-PW-Wechsel beim
  ersten Login. Solange das Default-PW gilt, blockt der Server alle
  Vault-Operationen.
- Bind ist `0.0.0.0` — bei produktivem Betrieb hinter Reverse-Proxy
  mit TLS + Client-Cert / mTLS (Standard-nginx- oder Caddy-Setup).
- Rate-Limit auf Login + Bootstrap (10 Versuche / 15 min pro IP).
- Audit-Log ist HMAC-Chain-protected → Tamper-Evidence.

## Code-Walkthrough: F28-Kompatibilität

Diese systemd-Unit + install.sh sind explizit gegen den aktuellen
F28-Code-Pfad geprüft:

- `OPNCOCKPIT_AUTH_BACKEND=user-db` triggert `_ensure_default_admin()`
- `OPNCOCKPIT_VAULT_DIR=/var/lib/opn-cockpit` wird vom Vault-Path-
  Validator als erlaubte Basis akzeptiert
- Service-User-Home = `/var/lib/opn-cockpit` → User-DB + Vault liegen
  dort, Schreibrechte stimmen
- Default-Admin-Banner landet in `journalctl -u opn-cockpit`

Wenn du eigene Pfade nutzt, verschiebt sich `OPNCOCKPIT_VAULT_PATH` UND
`OPNCOCKPIT_VAULT_DIR` mit — sonst lehnt der Validator den Vault-Pfad ab.
