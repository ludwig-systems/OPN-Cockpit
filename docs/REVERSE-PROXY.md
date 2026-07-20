# Reverse-Proxy vor OPN-Cockpit

Anleitung fuer Deployments, in denen ein Reverse-Proxy vor Cockpit
sitzt — typisch fuer Team-Setups mit Let's-Encrypt-Cert, SSO/OAuth
(Authelia, Keycloak), oder einheitlicher Ingress-Konfiguration.

**Kurz-Ueberblick:** Cockpit spricht ab v0.10 default HTTPS auf Port
443 mit einem self-signed Zertifikat. Fuer den Reverse-Proxy-Pfad
setzt du Cockpit auf HTTP (Port frei waehlbar) und dein Proxy
terminiert TLS — Vorteil: du kannst ein echtes Public-Cert (Let's
Encrypt, interne CA) nutzen, ohne es in Cockpit's Custom-Cert-Slot
zu importieren.

---

## Wann brauchst du einen Reverse-Proxy?

Cockpit im Standalone-Modus mit Custom-Cert-Upload deckt die meisten
Faelle ab. Ein Reverse-Proxy wird sinnvoll, wenn du:

- **Let's Encrypt** ohne manuellen Cert-Roundtrip willst
  (nginx-le / Caddy / Traefik machen Renewal automatisch);
- **SSO / OAuth / OIDC** vor Cockpit klemmen willst (Authelia,
  Keycloak, Cloudflare Access) — Cockpit hat kein natives OIDC;
- mehrere Interne Services unter einem Hostname konsolidierst
  (z.B. `admin.example.com/cockpit`);
- ein einheitliches **WAF / Rate-Limit / Access-Log** fuer alle
  Admin-Tools willst.

Wenn nichts davon zutrifft: bleib beim Standalone-Modus. Ist einfacher
und du hast weniger bewegliche Teile.

---

## Netz-Topologie

```
   Browser --- HTTPS ---> [Reverse-Proxy]:443 --- HTTP ---> [Cockpit]:9876
                                    |
                                    +-- terminiert TLS (Let's Encrypt)
                                    +-- optional: OIDC / SSO davor
```

Der Proxy sollte auf **derselben Maschine** oder in einem **privaten
Subnet** laufen — Cockpit spricht HTTP, dieser Hop darf nie ueber
oeffentliches Netz laufen.

---

## Cockpit-seitige Konfiguration

### 1. HTTP-Modus aktivieren

Cockpit macht HTTP nur bewusst — du setzt eine Env-Variable:

```
OPNCOCKPIT_ALLOW_HTTP=1
```

Damit startet Cockpit auf HTTP statt HTTPS. Ins Boot-Log kommt eine
deutliche Warnung, damit man den Modus nicht versehentlich in einem
Standalone-Setup laufen laesst.

**Windows-Service (NSSM):**

```
nssm set opn-cockpit AppEnvironmentExtra OPNCOCKPIT_ALLOW_HTTP=1
nssm restart opn-cockpit
```

**Linux (systemd):** Editier `/etc/systemd/system/opn-cockpit.service`
und ergaenze in der `[Service]`-Sektion:

```ini
Environment="OPNCOCKPIT_ALLOW_HTTP=1"
```

Dann `systemctl daemon-reload && systemctl restart opn-cockpit`.

### 2. Bind-Address einschraenken (empfohlen)

Damit niemand Cockpit direkt am HTTP-Port anspricht, bind explizit an
localhost:

```
OPNCOCKPIT_HOST=127.0.0.1
OPNCOCKPIT_PORT=9876
```

Der Proxy laeuft dann ueber Loopback zu Cockpit. Von aussen ist Port
9876 nicht erreichbar. Firewalls / Reverse-Proxy-Konfig muessen das
respektieren.

---

## Reverse-Proxy-Beispiele

### nginx

Minimales Beispiel mit Let's Encrypt (certbot):

```nginx
server {
    listen 443 ssl http2;
    server_name cockpit.example.com;

    ssl_certificate     /etc/letsencrypt/live/cockpit.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/cockpit.example.com/privkey.pem;

    # Modern-TLS Baseline (siehe Mozilla-Config-Generator).
    ssl_protocols             TLSv1.2 TLSv1.3;
    ssl_prefer_server_ciphers off;

    # Upload-Limit: Vault-Import, Cert-Upload, Backups gehen bis
    # ~5 MB in Einzelfaellen. 10 MB gibt Reserve.
    client_max_body_size 10m;

    location / {
        proxy_pass         http://127.0.0.1:9876;
        proxy_http_version 1.1;

        # WICHTIG: Original-Host + Client-IP durchreichen. Ohne diese
        # Header sieht Cockpit alle Requests als von localhost — was
        # das Audit-Log unnuetz macht.
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # Firmware-Rollouts + Reconcile-Loops koennen 30-60s dauern.
        proxy_read_timeout    120s;
        proxy_send_timeout    120s;
        proxy_connect_timeout 10s;
    }
}

server {
    listen 80;
    server_name cockpit.example.com;
    return 301 https://$host$request_uri;
}
```

### Caddy

Caddy macht Let's Encrypt automatisch — kein separater Renewal-Job:

```caddyfile
cockpit.example.com {
    encode gzip

    # Uploads (Vault, Backup) etwas grosszuegiger als Default.
    request_body {
        max_size 10MB
    }

    reverse_proxy 127.0.0.1:9876 {
        header_up Host              {host}
        header_up X-Real-IP         {remote}
        header_up X-Forwarded-For   {remote}
        header_up X-Forwarded-Proto {scheme}
        transport http {
            read_timeout    120s
            write_timeout   120s
            dial_timeout    10s
        }
    }
}
```

### Traefik (Docker-Compose)

Fuer Docker-Deployments — Traefik-Labels am Cockpit-Container:

```yaml
services:
  opn-cockpit:
    image: opn-cockpit:latest
    environment:
      OPNCOCKPIT_ALLOW_HTTP: "1"
      OPNCOCKPIT_HOST: "0.0.0.0"
      OPNCOCKPIT_PORT: "9876"
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.cockpit.rule=Host(`cockpit.example.com`)"
      - "traefik.http.routers.cockpit.tls=true"
      - "traefik.http.routers.cockpit.tls.certresolver=letsencrypt"
      - "traefik.http.services.cockpit.loadbalancer.server.port=9876"
      # 10 MB Upload-Limit
      - "traefik.http.middlewares.cockpit-uploads.buffering.maxRequestBodyBytes=10485760"
      - "traefik.http.routers.cockpit.middlewares=cockpit-uploads"
    networks:
      - proxy
```

---

## SSO / OAuth vor Cockpit (Authelia-Beispiel)

Cockpit hat keinen nativen OIDC-Support — SSO passiert am Proxy.
Beispiel mit **Authelia** vor nginx:

```nginx
location / {
    # Authelia-Auth-Check vor jedem Request
    auth_request /internal/authelia;
    auth_request_set $target_url $scheme://$http_host$request_uri;
    error_page 401 =302 https://auth.example.com/?rd=$target_url;

    proxy_pass http://127.0.0.1:9876;
    # ... (Header wie oben)
}

location = /internal/authelia {
    internal;
    proxy_pass http://127.0.0.1:9091/api/verify;
    proxy_pass_request_body off;
    proxy_set_header Content-Length "";
    proxy_set_header X-Original-URL $scheme://$http_host$request_uri;
}
```

Wichtig zu wissen:

- Cockpit fordert **trotzdem** den Vault-Master-Password-Login intern —
  SSO ist der aeussere Zugriffs-Wall, der Vault ist die innere
  Krypto-Schicht. Man kann Cockpit auch mit SSO davor als Multi-User
  betreiben; TOTP am Cockpit ist dann redundant, aber nicht schaedlich.
- **Session-Cookies** von Authelia + Cockpit koexistieren problemlos —
  sie liegen unter verschiedenen Namen und der Proxy schleust nur
  Cockpit's Cookie ans Backend durch.

---

## Windows-Shortcut anpassen

Nach der Umstellung zeigt der Desktop-Shortcut vermutlich noch auf
`https://cockpit.lab:9876` (Standalone-Default). Aendern auf die neue
Proxy-URL (`https://cockpit.example.com`) — dann verlaesst du dich
nicht mehr auf das self-signed Cert im Browser.

---

## Was du NICHT tun solltest

- **Proxy und Cockpit auf verschiedenen Hosts ueber public network
  fahren.** Cockpit-HTTP unverschluesselt ueber's Internet ist eine
  offene Einladung. Wenn Cross-Host, dann ueber VPN/IPSec/Wireguard
  zwischen den beiden Maschinen.
- **`OPNCOCKPIT_ALLOW_HTTP=1` in einem Standalone-Setup setzen.** Der
  Reverse-Proxy-Modus ist genau dann sinnvoll wenn ein Proxy davor
  ist. Ohne Proxy: Auto-HTTPS lassen.
- **Der Reverse-Proxy als 401/403-Filter.** Cockpit's Auth-Logik ist
  vollstaendig — der Proxy sollte nichts an der Auth aendern. Wer
  SSO/OIDC vorschaltet: der Proxy blockiert unauthorisiert, aber wenn
  er durchlaesst, macht Cockpit seinen eigenen Login-Flow (Vault +
  optional TOTP).

---

## Health-Check-Endpoint

Fuer Load-Balancer / Uptime-Checks:

```
GET /api/system/health
```

Liefert 200 mit JSON `{ "status": "ok", ... }` auch ohne Session.
Kein Auth-Token noetig.

---

## Troubleshooting

**"Alle Requests kommen von 127.0.0.1 im Audit-Log."**
Der Proxy leitet keine `X-Real-IP` / `X-Forwarded-For` weiter. Pruef
die Proxy-Konfig — Cockpit liest beide Header und faellt auf
`request.client.host` zurueck.

**"CSRF-Fehler nach dem SSO-Login."**
Cookies mit `SameSite=Strict` von Authelia koennen mit Cockpit's
Session-Cookie kollidieren wenn beide unter derselben Domain leben.
Loesung: Authelia auf einer Subdomain (`auth.example.com`), Cockpit
unter `cockpit.example.com`.

**"WebSocket-Upgrade schlaegt fehl."**
Cockpit nutzt aktuell keine WebSockets — falls du in Logs
`Upgrade: websocket` siehst, kommt das nicht von Cockpit. Bei
Kompatibilitaets-Problemen (z.B. wenn der Proxy WebSockets erwartet)
kannst du das im Proxy explizit disablen.

---

## Weitere Ressourcen

- [FEATURES.md](FEATURES.md) — Modul-Details (HTTPS, TOTP, Audit).
- [INSTALLATION-WINDOWS.md](INSTALLATION-WINDOWS.md) — Installer
  + Service-Mode.
- [QUICKSTART.md](QUICKSTART.md) — Erste Schritte im Standalone-Modus.
