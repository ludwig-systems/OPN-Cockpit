# Container-Betrieb (Docker / Proxmox)

OPN-Cockpit läuft als Linux-Container auf Debian-12-Basis. Funktioniert
unter Docker Desktop (Windows/Mac), nativen Linux-Docker und in
Proxmox-LXC-Containern.

## Voraussetzungen

- Docker 20.10+ oder Docker Desktop 4.x+
- Für WSL2 (Windows): WSL2 + Docker Desktop, „WSL2-Backend" aktivieren
- Für Proxmox: LXC-Container mit Docker oder Helper-Skript (Roadmap v3.x)

## Lokal testen (auch unter Windows via Docker Desktop)

```powershell
# Aus dem Repo-Root
docker compose up -d
```

UI auf `http://localhost:9876`. Beim ersten Mal: „Neuen Tresor anlegen…"
im Login-Screen, Pfad wird vorgeschlagen (im Container: `/data/...opnvault`).

Logs:
```powershell
docker compose logs -f
```

Stoppen:
```powershell
docker compose down            # Daten bleiben im Volume
docker compose down -v         # Daten LÖSCHEN
```

## Datenpersistenz

Alle App-Daten landen im Named-Volume `opncockpit-data`, gemountet als
`/data` im Container:

- `/data/...opnvault` — Tresor-Dateien
- `/data/audit.jsonl` — Audit-Log
- `/data/plans/` — Plan-Files + Apply-Reports
- `/data/profiles.json` — Vorlagen
- `/data/settings.json` — App-Settings

Das Volume überlebt `docker compose down` und Container-Neubauten.

## Backup

Vault rauskopieren ohne Container-Stop:
```powershell
docker cp opn-cockpit:/data/produktion.opnvault .
```

Komplettes Volume sichern (Linux/WSL2):
```bash
docker run --rm -v opncockpit-data:/data -v $(pwd):/backup debian:12-slim \
    tar czf /backup/opncockpit-data.tar.gz -C /data .
```

## Konfiguration

Über Environment-Variablen in `docker-compose.yml`:

| Variable | Default | Bedeutung |
|---|---|---|
| `OPNCOCKPIT_HOST` | `0.0.0.0` | Bind-Interface (im Container immer 0.0.0.0) |
| `OPNCOCKPIT_PORT` | `9876` | Port |
| `OPNCOCKPIT_NO_BROWSER` | `1` | Browser-Auto-Open aus (kein Browser im Container) |
| `OPNCOCKPIT_DATA_DIR` | `/data` | Daten-Root (= Mountpoint) |
| `OPNCOCKPIT_AUTH_BACKEND` | `vault` | `vault` (Single-User) oder `user-db` (Multi-User) |
| `OPNCOCKPIT_DEPLOYMENT_MODE` | `single-local` | `single-local` / `single-network` / `multi-server` |
| `OPNCOCKPIT_VAULT_PATH` | _(leer)_ | Default-Pfad zum zentralen Vault im Setup-Wizard |
| `TZ` | `Europe/Berlin` | Zeitzone für Audit-Timestamps |

## Setup-Flow

**Default ist Multi-User-Server** (siehe `docker-compose.yml`). Beim
Erststart:

1. `docker compose up -d`
2. `docker compose logs opn-cockpit` — dort findest du den
   **Bootstrap-Token**:
   ```
   ============================================================
     OPN-Cockpit BOOTSTRAP-TOKEN
     Status: needs-admin
     Token : Z60Z8NYn4XobdjsWqLEsJfONiAesZpa4
   ============================================================
   ```
3. Browser auf `http://localhost:9876` → Setup-Wizard:
   - **Step 1 — Admin anlegen**: Token + Username + Passwort
   - **Step 2 — Vault**: Pfad ist vorausgefüllt (`/data/firewalls.opnvault`),
     Master-Passwort vergeben, **„Tresor neu anlegen, falls die Datei
     nicht existiert"** ankreuzen
4. Multi-User-Login erscheint. Weitere User legst du als Admin in der
   User-Verwaltungs-UI an.

Der Token rotiert nach jedem Schritt — schau für Step 2 nochmal in die
Logs.

## Vault-Backup ziehen

Über die UI: **Tresor-Export**-Symbol in der Topbar (Download-Pfeil).
Zwei Optionen:

- **Backup** — kompletter Vault als-ist verschlüsselt, geeignet als
  Offsite-Backup. Wird mit dem Master-Passwort wieder geöffnet.
- **Template** — Kopie ohne API-Credentials, zum Weitergeben an
  andere Admins. Eigenes Passwort fürs Template wählbar.

Alternativ aus dem Container heraus (für automatisierte Backups):
```powershell
docker cp opn-cockpit:/data/firewalls.opnvault .\backup\
```

## Firewalls aus anderem Tresor übernehmen

Falls du schon einen `.opnvault` mit Firewalls hast und nicht alles
neu anlegen willst:

1. Datei ins Volume kopieren: `docker cp meine-firewalls.opnvault opn-cockpit:/data/import.opnvault`
2. Im UI: „Bulk-Import" → Tab „Anderer Tresor (.opnvault)"
3. Pfad: `/data/import.opnvault`, Master-Passwort des Quell-Vaults
4. Firewalls werden in den aktiven Vault übernommen (Duplikate
   übersprungen)

## Zurück zu Single-User

In `docker-compose.yml` die drei Env-Variablen
(`OPNCOCKPIT_AUTH_BACKEND`, `OPNCOCKPIT_DEPLOYMENT_MODE`,
`OPNCOCKPIT_VAULT_PATH`) auskommentieren, `docker compose up -d`.
Der klassische Vault-Picker erscheint.

## Reverse-Proxy mit TLS

Für Netzwerk-Betrieb hinter nginx/Traefik mit Let's Encrypt:

```yaml
services:
  opn-cockpit:
    # ... wie oben, aber Port-Mapping weg:
    expose:
      - "9876"
    networks:
      - reverse-proxy

  nginx:
    image: nginx:alpine
    ports:
      - "443:443"
    volumes:
      - ./nginx.conf:/etc/nginx/nginx.conf:ro
      - ./certs:/etc/nginx/certs:ro
    networks:
      - reverse-proxy

networks:
  reverse-proxy:
```

Beispiel-`nginx.conf`:
```nginx
server {
    listen 443 ssl http2;
    server_name opn-cockpit.lan;
    ssl_certificate     /etc/nginx/certs/fullchain.pem;
    ssl_certificate_key /etc/nginx/certs/privkey.pem;
    location / {
        proxy_pass http://opn-cockpit:9876;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

## Proxmox: LXC-Container

Variante A — manueller LXC:
1. Debian-12-LXC-Container anlegen (1 vCPU, 512 MB RAM reichen für 25 Boxen)
2. `apt install docker.io docker-compose-plugin`
3. Repo clonen, `docker compose up -d`

Variante B — Helper-Skript (Roadmap, kommt mit v3.0):
```bash
bash -c "$(wget -qLO - https://raw.githubusercontent.com/.../opn-cockpit/main/installer/proxmox-helper.sh)"
```

## Update auf neue Version

```powershell
git pull
docker compose build --no-cache
docker compose up -d
```

Vault + Audit + Plans bleiben erhalten (Volume).

## Stand v3.0 Iter 2

- Single-User-Modus (Default) und Multi-User-Modus (Setup-Wizard +
  Login per Username/PW) sind beide nutzbar.
- User-Verwaltungs-UI (Admin legt weitere User an) kommt mit Iter 3.
- Inventory-ACL (allowed_tags pro User) kommt mit Iter 4.

Siehe [ROADMAP.md](ROADMAP.md) für den Vollausbau.
