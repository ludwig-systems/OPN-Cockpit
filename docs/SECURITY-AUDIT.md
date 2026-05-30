# Security-Audit OPN-Cockpit (Stand 2026-05-30, v3.0)

Systematische Sicherheits-Review im Vorfeld des produktiven Multi-User-
Server-Einsatzes. Geprüft wurden 20 Risiko-Bereiche von Krypto-Primitiven
bis Logout-Cleanup.

## Zusammenfassung

| # | Bereich | Status | Severity |
|---|---|---|---|
| 1 | Krypto-Primitives (MD5/SHA1/RC4/DES) | ✅ sauber | — |
| 2 | Argon2-Parameter UserStore | ✅ explizit | — |
| 3 | Vault-KDF (Argon2id-Parameter) | ✅ RFC-9106 | — |
| 4 | **Rate-Limit auf Login** | ⚠️ fehlt | **KRITISCH** |
| 5 | **Bootstrap-Token-Schutz** | ⚠️ ungeschützt | **KRITISCH** |
| 6 | Security-Headers (HTML) | ⚠️ unvollständig | MEDIUM |
| 7 | CSRF-Schutz | ✅ Bearer-Token | — |
| 8 | Egress-Allowlist (HTTP-Client) | ✅ implementiert | — |
| 9 | Read-Modify-Write Race | ⚠️ teils ungeschützt | MEDIUM |
| 10 | save_vault Concurrency | ✅ RLock | — |
| 11 | Audit-Log-Integrity (Hash-Chain) | ⚠️ tamperable | MEDIUM |
| 12 | Klartext-Secret-Leak | ✅ mask_dict | — |
| 13 | TLS-Verify=False im Audit | ⚠️ nicht protokolliert | MEDIUM |
| 14 | Path-Traversal in vault_path | ⚠️ keine Validierung | MEDIUM |
| 15 | XSS im Frontend | ✅ textContent | — |
| 16 | eval/exec/subprocess | ✅ keine | — |
| 17 | Bearer-Token-Generation | ✅ secrets.token_urlsafe(32) | — |
| 18 | Cookie-Settings | ✅ keine Cookies | — |
| 19 | GET-Routen-ACL | ✅ device_visible_to | — |
| 20 | Logout-Cleanup | ✅ vollständig | — |

**Fazit:** Foundation ist solide (keine schwachen Primitives, kein
Cookie-Auth, kein XSS, korrekte Egress-Allowlist). Zwei Critical-Lücken
müssen vor dem Produktiv-Einsatz im Netzwerk geschlossen werden
(Login-Rate-Limit + Bootstrap-Token). Vier Medium-Lücken sind
Härtungsmaßnahmen.

---

## Detail-Befunde

### 1. Kryptographische Primitives — ✅ sauber

Suche nach `md5`, `sha1`, `des`, `rc4` in `src/`: keine Treffer.
Verwendet werden ausschließlich Argon2id (KDF) und AES-256-GCM (AEAD).

### 2. Argon2-Parameter UserStore — ✅ explizit

`src/opn_cockpit/security/users.py:75` instantiiert `PasswordHasher()`
ohne Parameter — das nimmt argon2-cffi-Defaults (`time_cost=3,
memory_cost=64 MiB, parallelism=4`). Das ist RFC-9106-konform.

### 3. Vault-KDF — ✅ RFC-9106

`src/opn_cockpit/vault/crypto.py:28-30`:
- `DEFAULT_TIME_COST = 4`
- `DEFAULT_MEMORY_COST_KIB = 262144` (256 MiB)
- `DEFAULT_PARALLELISM = 2`

Entspricht RFC-9106-Erstempfehlung.

### 4. Rate-Limit auf Login — ⚠️ **KRITISCH**

`POST /api/auth/unlock` und `POST /api/auth/login` (`src/opn_cockpit/web/api/auth.py`)
haben keine Rate-Limitierung. Ein Angreifer kann **beliebig viele Versuche**
ausführen, um Master-Passwort oder User-Passwort zu brute-forcen. Argon2id
bremst pro Versuch, aber bei parallelen Requests wird der Server selbst
zum DoS-Ziel (jeder Versuch = ~250 ms KDF auf Production-Parametern).

**Fix umgesetzt** (Commit folgt): In-Memory Sliding-Window-Limiter pro
Client-IP. 10 fehlgeschlagene Logins pro 15 Minuten → 429 für 5 Minuten.
Erfolgreiche Logins zurücksetzen das Fenster.

### 5. Bootstrap-Token-Schutz — ⚠️ **KRITISCH**

`POST /api/bootstrap/admin` ist im `needs-admin`-Status frei zugänglich.
Wer als erster den Server erreicht (z.B. in einem geteilten Office-LAN
nach dem ersten Container-Start), wird Admin und kontrolliert das gesamte
Multi-User-System.

**Fix umgesetzt** (Commit folgt): Beim Server-Start im Multi-Mode wird ein
zufälliger Bootstrap-Token generiert und in `stdout` / systemd-Journal
geschrieben. Der Setup-Wizard verlangt den Token im Body (`X-Bootstrap-Token`-
Header). Nach erfolgreichem Bootstrap-Admin wird der Token entwertet.

Bei Server-Restart in `needs-vault-unlock` wird ein neuer Token rotiert.
Der Server-Admin muss per SSH / `docker compose logs` reinschauen und
kennt den Token — ein Netzwerk-Angreifer nicht.

### 6. Security-Headers — ⚠️ MEDIUM

`src/opn_cockpit/web/server.py:28-46` setzt nur `Cache-Control` auf
statischen Assets. Fehlend:

- `X-Frame-Options: DENY` (Clickjacking)
- `X-Content-Type-Options: nosniff` (MIME-Sniffing)
- `Content-Security-Policy` (XSS-Defense-in-Depth)
- `Referrer-Policy` (kein Header-Leak)
- `Strict-Transport-Security` (nur bei TLS sinnvoll, Reverse-Proxy-Aufgabe)

**Fix umgesetzt** (Commit folgt): Globale Middleware setzt die obigen
Headers auf alle HTML- und JSON-Responses. HSTS wird nur gesetzt wenn
`OPNCOCKPIT_TLS_CERT` konfiguriert ist (sonst bricht es Reverse-Proxy-
Setups mit eigenem HSTS).

### 7. CSRF-Schutz — ✅ Bearer-Token

Auth läuft per `Authorization: Bearer <token>` aus dem `sessionStorage`.
Kein Cookie-Auth, also kein klassisches CSRF. Bei Reverse-Proxy mit
Cookie-Forwarding wäre das anders — heute nicht der Fall.

### 8. Egress-Allowlist — ✅ implementiert

`src/opn_cockpit/core/http_client.py:117-186` baut beim Konstruktor eine
Allowlist `(host, port)`-Paare aus den übergebenen Targets. Jeder Request
prüft `target.key not in self._allowed` und wirft `EgressDeniedError`.
Spec R-SEC-7 erfüllt.

### 9. Read-Modify-Write Race im Multi-User-Mode — ⚠️ MEDIUM

In `src/opn_cockpit/web/api/inventory.py` (`update_device`, `remove_device`):

```python
devices = session.opened.data.devices       # 1) read
index = next((i for i, d in enumerate(devices) if d.id == device_id), -1)
current = devices[index]
current.name = payload.name                  # 2) mutate
persist_session_vault(...)                   # 3) save (unter Lock)
```

Im Multi-User-Mode zeigen alle Sessions auf das **gleiche** OpenedVault.
Schritte 1+2 laufen ohne Lock — zwei parallele Updates auf demselben
Gerät können sich überholen, das letzte schreibt seinen Stand. Konkrete
Folgen: vergessene Edits, inkonsistente Indices, im schlimmsten Fall
`IndexError` wenn ein anderer Thread parallel `pop`t.

**Fix umgesetzt** (Commit folgt): `ServerState.vault_mutation_lock()`
Context-Manager. Alle Inventory-Mutate-Routen halten den Lock von Read
bis Save. Im Single-Mode ist es ein No-Op (kein Sharing).

### 10. save_vault Concurrency — ✅ RLock

`ServerState.save_vault_central` läuft unter `self._lock` (RLock). Nach
erfolgreichem Write wird `_opened_vault` zentral aktualisiert UND alle
aktiven Sessions des gleichen Vaults bekommen die neue Referenz via
`SessionManager.replace_opened_everywhere`. Das schützt gegen
Nonce-Reuse und Header-Mismatch.

### 11. Audit-Log-Integrity — ⚠️ MEDIUM

`src/opn_cockpit/audit/log.py` schreibt JSON-Lines append-only. Es gibt
keinen Hash-Chain. Ein Angreifer mit Dateisystem-Zugriff kann beliebige
Einträge ändern oder löschen, ohne dass das später erkennbar wäre.

**Empfehlung (Roadmap):** HMAC-Chain — jeder Eintrag hängt vom Hash des
vorherigen ab, mit Server-Secret. Reicht für Tamper-Evidence, nicht
gegen den Server-Compromise selbst. **Heute nicht umgesetzt**, weil
Audit-Log primär forensisches Hilfsmittel ist (User mit Filesystem-
Zugriff hat eh den Vault) und der Aufwand vs Nutzen für die heutige
Größenordnung (5-25 Geräte, 2-5 Admins) gering ist.

### 12. Klartext-Secret-Leak — ✅ mask_dict

`AuditLog.append` maskiert `parameters` rekursiv via
`security.masking.mask_dict`. Test `tests/unit/audit/test_log.py`
verifiziert, dass `password`, `api_key`, `api_secret`-Felder als
`***` erscheinen. Grep nach direkter Log-Verwendung von `api_secret`
findet nur Tests + Vault-Modul.

### 13. TLS-Verify=False im Audit — ⚠️ MEDIUM

Beim Apply gegen ein Gerät mit `tls_verify=False` (selbst-signiertes
Zertifikat) wird der TLS-Bypass weder pro-Gerät noch pro-Apply im
Audit-Log mit-protokolliert. Ein Audit-Reviewer kann nicht erkennen,
welche Operations gegen "blind" konfigurierte Targets liefen.

**Fix umgesetzt** (Commit folgt): `DeviceResult` erhält ein
`tls_verify`-Feld; im Audit-Log-Summary erscheint `[TLS-AUS]` bei
betroffenen Devices.

### 14. Path-Traversal in vault_path — ⚠️ MEDIUM

`POST /api/auth/unlock` und `POST /api/bootstrap/vault` akzeptieren
einen beliebigen `vault_path` ohne Pfad-Validierung. Konkret:

- Im Single-Mode kann ein Angreifer (per Browser-Request gegen `:9876`)
  Pfade wie `C:\Windows\System32\config\SAM` als vault_path versuchen.
  Liefert keine Daten zurück (Magic-Check failt → 503), aber ist eine
  Read-Probe.
- Im Multi-Mode kann der Server-Admin (bewusst) jeden Pfad öffnen —
  ist Teil seines Privilegs.

**Fix umgesetzt** (Commit folgt): vault_path muss auf `.opnvault`
enden UND innerhalb von `get_app_data_dir()` (resp. einem
`OPNCOCKPIT_VAULT_DIR`-Override) liegen. Symlinks werden via
`Path.resolve()` aufgelöst, dann gegen `Path.is_relative_to(base)`
geprüft.

### 15. XSS im Frontend — ✅ textContent

Grep auf `.innerHTML` in `app.js` findet 40+ Stellen — alle entweder
Clear (`= ''`) oder hardcoded-SVG (`= '<svg ...>'`). Sämtliche
API-Response-Strings landen in `.textContent`. Keine XSS-Vektoren.

### 16. eval/exec/subprocess — ✅ keine

Keine Treffer im gesamten `src/`-Tree.

### 17. Bearer-Token-Generation — ✅ secrets.token_urlsafe(32)

256 Bit Entropie, URL-safe Base64, 43 Zeichen. Standard-Praxis.

### 18. Cookie-Settings — ✅ keine Cookies

Auth läuft via sessionStorage + Bearer-Header. Kein Cookie wird gesetzt.

### 19. GET-Routen-ACL — ✅ checked

`/api/plans`, `/api/plans/outstanding`, `/api/plans/{id}` filtern alle
über `device_visible_to(d, session)`. Plans mit nicht-erlaubten Devices
sind nicht sichtbar (404 statt 403 — Existenz nicht verraten).

### 20. Logout-Cleanup — ✅ vollständig

`POST /api/auth/lock` revoked Token, ruft `Session.lock()` (setzt alle
sensitiven Felder auf `None`), bricht Retry-Watcher für das Token ab,
schreibt Audit-Eintrag.

---

## Was nach diesem Audit gefixt wurde

Sechs Commits im Audit-Patch (Commits direkt nach diesem Dokument):

1. **Rate-Limit** auf `/api/auth/unlock` + `/api/auth/login` + Bootstrap-
   Endpunkte (Sliding-Window, In-Memory, 10/15min pro IP)
2. **Bootstrap-Token** für `/api/bootstrap/admin` + Vault-Unlock im
   Multi-Mode. Token im Server-Log bei Start.
3. **Security-Headers**-Middleware für HTML + JSON
4. **vault_mutation_lock** für Multi-User-Read-Modify-Write
5. **TLS-Verify-Status** im Audit-Eintrag pro Apply-Device
6. **Path-Validierung** für vault_path (Endung + relative-to-base)

Audit-Log-HMAC und HSTS-Switch bleiben Roadmap-Items.
