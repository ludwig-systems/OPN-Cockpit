# Linux-Installation (Multi-User-Server)

Drei Wege, OPN-Cockpit auf Linux laufen zu bekommen — vom Quick-Test bis
zum produktiven Container.

**Standard ist seit v3.x Multi-User-Server.** Beim ersten Start legt der
Server automatisch einen Default-Admin an (`admin` / `OPN-Cockpit!`)
mit Pflicht-Passwort-Wechsel beim ersten Login. Kein Bootstrap-Token mehr.

## Variante 1: Docker (am einfachsten, auch unter Windows via Docker Desktop)

Siehe [../../docs/DOCKER.md](../../docs/DOCKER.md).

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

Interaktive Abfrage:
- Container-ID (Default: nächste freie)
- Storage-Pool
- Disk-Größe (Default 2 GB), CPUs (1), RAM (512 MB)
- Netzwerk-Bridge (Default `vmbr0`, DHCP)

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

```bash
# Auf dem Host
cd /opt/opn-cockpit
sudo -u opncockpit git pull
sudo -u opncockpit .venv/bin/pip install -e .
sudo systemctl restart opn-cockpit
```

Im Proxmox-Container:
```bash
pct exec <ct-id> -- bash -c "cd /opt/opn-cockpit && \
    sudo -u opncockpit git pull && \
    sudo -u opncockpit .venv/bin/pip install -e . && \
    systemctl restart opn-cockpit"
```

Auto-Migration: beim Restart führt der Server `run_pending_migrations()`
aus (Schema-Upgrades). Bei Schema-Änderungen landet vorher ein Backup
in `/var/lib/opn-cockpit/backups/`.

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
  mit TLS + Client-Cert / mTLS. nginx-Beispiel siehe
  [../../docs/DOCKER.md](../../docs/DOCKER.md).
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
