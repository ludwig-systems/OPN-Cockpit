# Linux-Installation

Drei Wege, OPN-Cockpit auf Linux zu installieren — vom Quick-Test bis
zum produktiven Container.

## Variante 1: Docker (am einfachsten, auch unter Windows via Docker Desktop)

Siehe [../../docs/DOCKER.md](../../docs/DOCKER.md).

## Variante 2: Direkt auf Debian-/Ubuntu-Host als systemd-Service

```bash
# Repo clonen
git clone https://github.com/your-org/opn-cockpit.git /tmp/opn-cockpit
cd /tmp/opn-cockpit

# Installer ausführen (braucht root)
sudo bash installer/linux/install.sh --source .
```

Was passiert:
- Pakete: `python3`, `python3-venv`, `python3-dev`, `build-essential`, `git`
- Service-User `opncockpit` (system user, kein Login-Shell)
- Verzeichnisse: `/opt/opn-cockpit` (Code) + `/var/lib/opn-cockpit` (Daten)
- Python-venv unter `/opt/opn-cockpit/.venv`
- systemd-Unit aktiviert und gestartet
- Bind auf `0.0.0.0:9876`

Standard-Pfade per Env überschreibbar:
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

Auf einem Proxmox-VE-Host:

```bash
bash -c "$(wget -qLO - https://raw.githubusercontent.com/your-org/opn-cockpit/main/installer/linux/proxmox-helper.sh)"
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

## Konfiguration

systemd-Unit lebt unter `/etc/systemd/system/opn-cockpit.service`. Environment-
Variablen direkt darin anpassen:

| Variable | Default | Bedeutung |
|---|---|---|
| `OPNCOCKPIT_DATA_DIR` | `/var/lib/opn-cockpit` | Daten-Root |
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

## Backup

Daten liegen in `OPNCOCKPIT_DATA_DIR` (Default `/var/lib/opn-cockpit/`):

```bash
tar czf opn-cockpit-backup-$(date +%F).tar.gz -C /var/lib opn-cockpit
```

Wichtig: das ist nicht nur der Vault, sondern auch der Audit-Log und
die Plan-Reports. Komplette Trash → komplettes Restore.

## Sicherheit auf Linux

- Service läuft als unprivilegierter User `opncockpit`
- systemd-Unit hat Hardening-Flags (NoNewPrivileges, ProtectSystem=strict,
  PrivateTmp, etc.)
- Bind ist `0.0.0.0` — wenn nur LAN: per Firewall einschränken
- Für TLS: nginx als Reverse-Proxy davorhängen (siehe DOCKER.md für ein
  Beispiel-Snippet, gleiches Prinzip ohne Docker)

## Bekannte Grenzen v2.2

- Heute läuft auf Linux der **Single-User-Modus** mit Master-Passwort
  und File-basiertem Vault. Multi-User-Login (User-DB + Roles) ist
  v3.0-Roadmap (siehe [../../docs/ROADMAP.md](../../docs/ROADMAP.md)).
- Auto-Retry-Watcher hängt an der entsperrten Session. Im Daemon-Mode
  bedeutet das: solange ein User-Browser-Tab offen ist, läuft der
  Watcher. Wenn alle Tabs zu sind und Session lockt, endet er.
- Der Service-User hat den Master-PW nicht — der wird vom User beim
  Login getippt. Der Service hat lediglich Zugriff auf die `.opnvault`-
  Datei auf Platte, kann sie aber ohne PW nicht lesen.

In v3 wird der zentrale Vault einmalig beim Service-Boot über eine
Bootstrap-UI entsperrt und bleibt im Server-Memory. Bis dahin ist
„Browser-Tab offen halten" das Modell.
