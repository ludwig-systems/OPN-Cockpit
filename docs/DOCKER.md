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

## Multi-User-Modus aktivieren

Der Container startet per Default im **Single-User-Modus**. So aktivierst
du Multi-User:

**Schritt 1 — Vault anlegen** (einmalig, im Single-Modus):
1. `docker compose up -d`
2. Browser auf `http://localhost:9876`
3. „Neuen Tresor anlegen…" → Pfad `/data/firewalls.opnvault`, starkes
   Master-PW vergeben
4. Optional: ein paar Test-Firewalls anlegen
5. Im Browser sperren, dann `docker compose down`

**Schritt 2 — Multi-User aktivieren**: in `docker-compose.yml` die
Env-Variablen einkommentieren:
```yaml
    environment:
      TZ: Europe/Berlin
      OPNCOCKPIT_AUTH_BACKEND: "user-db"
      OPNCOCKPIT_DEPLOYMENT_MODE: "multi-server"
      OPNCOCKPIT_VAULT_PATH: "/data/firewalls.opnvault"
```

**Schritt 3 — Neustart + Setup-Wizard**:
```powershell
docker compose up -d
```
Auf `http://localhost:9876` erscheint jetzt der Setup-Wizard:
1. Admin-Konto anlegen (mind. 12 Zeichen)
2. Zentralen Vault entsperren (Pfad ist vorausgefüllt)

Danach erscheint der Multi-User-Login. Weitere User legt der Admin in
der User-Verwaltungs-UI an (kommt mit v3.0 Iter 3).

**Zurück zu Single-User**: Env-Variablen wieder rausnehmen, neustarten.
Vault-Daten + User-DB bleiben im Volume erhalten, sind aber im
Single-Modus inaktiv.

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
