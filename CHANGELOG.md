# Changelog

Alle nennenswerten Änderungen pro Release.

## v0.8.0 — in Arbeit — CRUD-Erweiterung

### Wartungsmodus pro Gerät

Geräte können einzeln in den Wartungsmodus geschaltet werden (Checkbox im
Edit-Dialog). Solange aktiv:

- **Heartbeat** überspringt das Gerät — kein TCP-Probe, eigener neutraler
  Status-Dot statt rot/grün, "Wartung"-Badge auf der Karte (visuell gedimmt).
- **Scheduled Backups** überspringen das Gerät — kein Audit-Log-Spam mehr
  bei planmäßig offline-Standorten (Hardware-Tausch, Mobile-Rack im Transit,
  Stilllegung).
- **Manuelle Aktionen** bleiben erlaubt: Test-Connection, Plan/Apply,
  Backup-Download — der User entscheidet bewusst.

Persistiert in `VaultDevice.maintenance` (Bool, Default `False`); existierende
Tresore migrieren transparent.

### DNS: drei Sub-Tabs (Host-Overrides, Domain-Overrides, Abfrage-Weiterleitungen)

OPNsense kennt drei separate DNS-Subsysteme — alle drei werden jetzt im
Geräte-DNS-Tab als eigene Sub-Tabs gerendert:

- **Host-Overrides** (Hostname → IP-Override) — CRUD wie bisher.
- **Domain-Overrides** (Domain → Resolver) — read-only via
  `searchDomainOverride`.
- **Abfrage-Weiterleitungen** / Query-Forwards (globale DoT/DoH-Forwarder)
  — read-only via `searchForward`.

Die Compare-Matrix bekommt entsprechend drei DNS-Subsysteme: **DNS-Hosts**,
**DNS-Overrides**, **DNS-Weiterleitungen** (alle mit Drift-Erkennung).

### DNS-Host-Override-Sync (Compare-Matrix)

Master-→-Targets-Sync gibt es jetzt zusätzlich zu Aliasen auch für
Unbound-Host-Overrides: in der Compare-Matrix-Subsystem-Spalte „DNS-Hosts"
erscheint pro Drift-Zeile ein **Sync-Button**, der einen Plan
(`add_unbound_host`) auf alle Target-Geräte erzeugt und direkt in die
Plan-Vorschau springt. Identitäts-Matching via `display_name`
(`"host.domain"`) gegen die Master-Konfig.

Domain-Overrides + Query-Forwards bleiben read-only (kein CRUD-Adapter).

### „Mein Konto" konsolidiert (Passwort + 2FA)

Statt zwei Topbar-Icons (Passwort ändern + 2FA einrichten) gibt es jetzt nur
noch **„Mein Konto"** mit beiden Sektionen im selben (breiteren) Modal.
Passwort-Wechsel oben, TOTP-Einrichtung/Backup-Codes/Disable darunter,
QR-Code mit Code-Eingabefeld nebeneinander statt untereinander.

### Disk-Space-Widget in der Topbar (Server-Setup)

Topbar zeigt einen schmalen Progress-Bar mit Prozent des belegten Speichers
auf dem App-Data-Volume (Backups, Audit-Log, SQLite-DBs). Hover-Tooltip:
Path + Free-GB / Total-GB. Schwellen: gelb ab 80 %, rot ab 92 % + einmaliger
Toast.

Auf Windows-Single-User-Loopback liefert das Backend `relevant=false` und das
Widget bleibt versteckt — Admin sieht den Platz dort ohnehin im Explorer.
Endpoint: `GET /api/system/disk` (Bearer-auth).

### Vault-Settings: Trust-CA + Cockpit-HTTPS inline integriert

Die zwei früheren Sub-Modals (Trust-CA hinzufügen, Server-TLS hochladen) sind
ersatzlos entfallen — beide Bereiche klappen jetzt **direkt im
Vault-Settings-Modal** auf. Jedes Feld hat einen primären Datei-Picker
(File-Input) mit Filename-Anzeige plus eine PEM-Textarea als
Copy&Paste-Fallback. Vault-Settings-Modal selbst wurde auf die Breite des
Geräte-Modals umgestellt — vorher waren die integrierten Cert-Bereiche
gequetscht.

### SSH-Key-Anleitung im Add/Edit-Device-Modal

Neben „Safety-Net via SSH aktivieren" erscheint ein **„Anleitung"-Link**, der
ein eigenes Modal mit kompakter Schritt-für-Schritt-Anleitung öffnet:

1. Key-Paar erzeugen (`ssh-keygen -t ed25519`-Befehle für Windows und
   Linux/macOS, Hinweis „Passphrase leer lassen")
2. Public-Key auf der OPNsense hinterlegen (UI-Pfad, authorized-keys-Feld,
   SSH-Service aktivieren)
3. Private-Key ins Cockpit kopieren (`Get-Content | Set-Clipboard` /
   `cat | xclip/pbcopy`) + Sicherheits-Hinweise

Plus **Helper-Scripts zum Download** für nicht-CLI-fitte Admins
(`opncockpit-ssh-helper.ps1` für Windows, `.sh` für Linux/macOS): erzeugen
das Key-Paar, legen den Public-Key in die Zwischenablage, öffnen beide
Keys im Editor und drucken eine farbige Console-Anleitung. Liegen unter
`/static/scripts/`. Der manuelle Pfad bleibt im Modal komplett erhalten.

### os-firewall: Doku-Klarstellung

OPNsense hat das Plugin seit 23.7 in Core integriert (Tab **Firewall →
Automation → Filter**). Doku in README, QUICKSTART, FEATURES und API-Error-
Messages stellt das klar: Cockpit zeigt **nur Automation-Filter**; klassische
„Firewall → Rules" (Legacy-XML-Editor) sind nicht API-zugänglich. Empty-State
im UI verweist auf diese Trennung.

### Zwei-Faktor-Authentifizierung (TOTP, opt-in)

Multi-User-Mode kennt jetzt TOTP nach RFC 6238 als optionale 2FA pro User:

- **Self-Service-Enrollment:** "Mein Konto → TOTP" zeigt Provisioning-URI
  (für QR-Render in der Authenticator-App, z.&nbsp;B. Google/Microsoft/Aegis/
  Bitwarden) plus das Base32-Secret für die manuelle Eingabe. Nach
  Bestätigung mit dem ersten 6-stelligen Code schaltet 2FA scharf und der
  Server liefert **8 Backup-Codes** zur einmaligen Anzeige.
- **Zwei-Schritt-Login:** ``POST /api/auth/login`` antwortet bei aktivem
  TOTP mit ``totp_required=true`` plus einer 5-Minuten-Challenge (signiert
  via HMAC-SHA256). ``POST /api/auth/login/totp`` schließt mit dem
  6-stelligen Code oder einem Backup-Code ab.
- **Defense-in-Depth beim Disable:** Selbst-Deaktivierung verlangt
  **aktuelles Passwort + aktuellen Code**, damit ein gestohlener Session-
  Token allein 2FA nicht ausschaltet.
- **Admin-Recovery:** ``POST /api/users/{id}/totp/disable`` schaltet 2FA
  für einen anderen User ab (Audit-protokolliert) — Recovery-Pfad falls
  Authenticator + Backup-Codes verloren gingen.
- **Persistenz:** ``totp_secret``, ``totp_enabled`` und die SHA-256-Hashes
  der Backup-Codes liegen in ``users.db``; ein verbrauchter Backup-Code
  wird automatisch entfernt.

Neue Abhängigkeit: ``pyotp>=2.9``. Single-User-PAW-Modus ist unverändert
(kein User-Konzept, kein 2FA).

### Security-Audit (Gesamtbetrachtung)

Vollständiger Self-Audit über die gesamte v0.8-Codebasis (siehe
``docs/SECURITY-AUDIT-FULL-0.8.local.md``). Sieben Findings gefixt,
fünf akzeptiert.

- **G1 (HIGH)** — OPNsense-Backup-XMLs (``backups/<device>/<uuid>.xml.gz``)
  werden jetzt mit ``chmod 0o600`` geschrieben (Files + Index). Schützt
  Cert/Key/PSK-Material in den Configs auf Multi-User-Linux gegen
  unprivilegierte Lokal-User. Windows-NTFS no-op (Service-User ist Owner).
- **G2 (MEDIUM)** — Rate-Limiter respektiert ``X-Forwarded-For`` nur noch
  wenn ``OPNCOCKPIT_TRUST_FORWARDED_FOR=true`` gesetzt ist. Default off
  verhindert XFF-Spoofing-Bypass bei Direkt-Bind ohne Reverse-Proxy.
- **G3 (MEDIUM)** — File-Picker-Endpoints (``/api/files/browse``,
  ``/pick-folder``, ``/pick-file``) prüfen jetzt auf Loopback-Origin via
  ``ipaddress.is_loopback`` plus Marker-Whitelist (``localhost``,
  ``testclient``). Schutz, falls Single-User versehentlich auf 0.0.0.0
  gebunden wurde.
- **G4 (LOW)** — Bearer-Authorization-Header wird jetzt RFC-6750-konform
  case-insensitive akzeptiert.
- **G5 (LOW)** — ``audit.jsonl`` wird initial mit ``O_CREAT`` Mode 0o600
  angelegt plus Defense-in-Depth ``chmod`` bei jedem Write.
- **G6 (LOW)** — Neuer ``Permissions-Policy``-Header sperrt nicht
  gebrauchte Browser-Features (geolocation, camera, microphone, payment,
  usb, sensors).
- **G7 (LOW)** — Neuer ``USER_LOGIN_SUCCESS``-Audit-Event-Kind ersetzt das
  überladene ``VAULT_OPENED`` für Multi-User-Logins; ermöglicht saubere
  Login-Forensik im UI-Filter.

Akzeptierte Findings (G8–G13) im Audit-Doku dokumentiert mit Begründung.

### TLS-Vertrauen + Cockpit-eigenes HTTPS

**Security-Härtung (Post-Audit, gleicher Release):**

- ``require_admin_role``-Helper neu in ``web/acl.py`` — strikter als
  ``require_admin``, lässt aber Single-User-Mode durchwinkend
  (User ist implizit admin). Trust-CA POST/DELETE und Server-TLS
  POST/DELETE auf diesen Helper umgestellt: im Multi-User-Mode
  jetzt admin-only (Trust-Anker sind security-impacting).
- Server-TLS-Upload validiert ``ssl.SSLContext.load_cert_chain``
  vor dem Save - Cert+Key-Mismatch fliegt mit 422 zurück statt
  Cockpit beim nächsten Boot zu briken (DoS-Schutz).
- Soft-Cap ``MAX_TRUSTED_CAS = 64`` im POST-Pfad — Sanity-Check
  gegen versehentliche Massen-Uploads.
- Magic-Value ``"STALE"`` im Delete-Pfad entfernt — undokumentierte
  Recovery-API, ersetzt durch Export/Reimport-Workflow.
- Audit-Summary für Trust-CA-Add/Delete und Server-TLS-Set enthält
  jetzt den (gekürzten) Fingerprint zusätzlich zur Subject CN.

Audit-Bericht inkl. Findings F6-F9 (Accept, dokumentiert) in
``docs/SECURITY-AUDIT-0.8-TLS.local.md`` (gitignored).



Zwei verwandte aber separate TLS-Themen:

**Custom Root-CAs fuer ausgehende Verbindungen.** Wer eine interne CA
betreibt und mit ihr OPNsense-Zertifikate ausstellt, kann diese CA jetzt
einmal im Tresor hinterlegen statt pro Geraet `tls_verify=false` zu setzen.

- ``VaultSettings.trusted_ca_pems`` — Liste von PEM-Root-CAs, wird beim
  Vault-Save als Teil des verschluesselten Inhalts persistiert.
- ``HttpTuning.trusted_ca_pems`` (Tuple) reicht die Liste an alle
  Aufrufer durch. Neuer ``tuning_from_settings(settings)``-Helper
  ersetzt die 20+ inline HttpTuning-Konstruktoren in inventory.py /
  plans.py / CLI / Scheduler / Discover — Custom-CAs greifen ueberall.
- ``HttpClient`` baut beim Init einen ``ssl.SSLContext`` der System-CAs
  + Custom-PEMs kombiniert. Wenn keine PEMs hinterlegt sind, bleibt das
  Default (``verify=True`` = System-CAs).
- ``core/trust_store.py`` parst PEM-Bloecke (Multi-PEM unterstuetzt) und
  liefert Metadaten (Fingerprint, Subject, Issuer, Gueltigkeit, CA-Bit,
  self-signed) - der Builder ueberspringt kaputte PEMs einzeln.
- Endpoints: ``GET/POST/DELETE /api/vaults/settings/trusted-cas`` und
  ``POST .../inspect`` (Preview vor Save). Idempotent ueber den
  SHA256-Fingerprint.
- UI: neuer Block "Vertrauenswuerdige Root-CAs" im Tresor-Settings-Modal
  mit Liste (Subject + Gueltigkeit + Fingerprint), "Hinzufuegen"-Modal
  mit Inspect-Preview + "Entfernen" pro Eintrag.

**Cockpit-eigenes HTTPS-Server-Zertifikat.** Damit User auf
``https://cockpit.lab:9876`` ohne Browser-Warnung zugreifen, kann das
Cockpit jetzt sein eigenes TLS-Cert hinterlegt bekommen.

- ``AppSettings.server_tls_cert_path`` + ``server_tls_key_path`` in
  ``settings.json`` (NICHT im Tresor - der Server muss vor dem
  Vault-Unlock hochkommen). ``resolved_tls_paths()``-Helper liefert
  beide Pfade nur wenn beide Dateien existieren.
- ``WebSettings.from_env()`` faellt jetzt auf AppSettings zurueck wenn
  die ``OPNCOCKPIT_TLS_CERT/KEY``-Env-Vars nicht gesetzt sind. Cockpit
  startet automatisch auf HTTPS sobald die Pfade da sind.
- Neue Endpoints ``GET /api/server/tls``, ``POST .../tls``,
  ``DELETE .../tls`` (admin-only). Upload schreibt Cert + Key nach
  ``<app_data>/server_tls/`` mit 0600 fuer den Key.
- UI: Block "Cockpit HTTPS-Zertifikat" im Tresor-Settings-Modal mit
  Status (aktive Subject + Gueltigkeit), Upload-Modal fuer Fullchain +
  Key, klarer Restart-Hinweis (uvicorn liest TLS nur beim Boot).

### Lizenz: Apache 2.0

- Projekt-Lizenz von "Proprietary" auf **Apache License 2.0** umgestellt.
  Begruendung: permissiv (kommerzielle Adoption moeglich), expliziter
  Patent-Grant (schuetzt User vor Patentklagen), kompatibel mit allen
  Runtime-Deps (MIT/BSD/Apache/LGPL). Industriestandard fuer
  Infrastruktur-Tools.
- ``LICENSE``-Datei + ``THIRD-PARTY-NOTICES.md`` mit Attribution fuer
  alle Runtime-Deps (FastAPI, httpx, paramiko, fpdf2, cryptography,
  argon2-cffi, ...) und Bundle-Komponenten (Python Embedded, NSSM,
  Inno Setup).
- ``pyproject.toml`` ``license = "Apache-2.0"`` + ``license-files``;
  ``__license_label__`` analog; About-Modal zeigt das Label + Link
  zur Drittanbieter-Datei.

### Safety-Net via SSH (Cisco-Style commit-confirmed)

- Neues Apply-Mode "Mit Sicherheitsnetz": nach erfolgreichem Verify
  hat der User ``safety_net_window_s`` Zeit zu **Bestaetigen**, sonst
  rollt Cockpit ueber SSH automatisch auf das Pre-Apply-Backup zurueck.
  Use-Case: ein Filter-Regel-Apply nimmt versehentlich Cockpit's
  eigene IP aus - Box ist ueber API unerreichbar, aber SSH bleibt
  meistens drin.
- Pro VaultDevice neue Felder: ``ssh_enabled``, ``ssh_host``,
  ``ssh_port`` (Default 22), ``ssh_user``, ``ssh_private_key_pem``.
  Key liegt verschluesselt im Tresor (gleicher Schutz wie API-Secret);
  ``DeviceResponse`` zeigt nur ``ssh_key_present`` als Boolean an.
- Pro VaultSettings neue Felder ``safety_net_enabled`` (Default AUS,
  global aktivieren) und ``safety_net_window_s`` (Default 120).
- Neuer in-memory ``SafetyNetWatcher`` (Daemon-Thread, 1 s Tick):
  pro armed Apply ein Eintrag mit Deadline + device_lookup-Closure
  fuer den Rollback. Beim Deadline-Hit holt der Watcher die
  Pre-Apply-XML aus dem Backup-Store, schreibt sie via SFTP nach
  ``/conf/config.xml`` und triggert ``configctl webgui restart;
  configctl filter reload; configctl interface reconfigure;
  configctl service reload all``.
- SSH-Rollback ist multi-Format-Key-faehig (Ed25519, ECDSA, RSA, DSA).
  Klartext-Key wird sofort nach Connect wieder freigegeben (``del``
  im finally-Block) damit er nicht in Tracebacks landet.
- Neue Endpoints: ``GET /api/plans/{id}/safety-net``,
  ``POST .../safety-net/confirm``, ``POST .../safety-net/abort``.
  Apply-Endpoint nimmt ``safety_net: true`` + optional
  ``safety_net_window_s`` im Body.
- UI: Checkbox "Mit Sicherheitsnetz ausrollen" in der Plan-Vorschau
  (nur sichtbar wenn mindestens ein Ziel-Geraet SSH konfiguriert hat);
  nach Apply Banner mit Countdown + Bestaetigen-/Abort-Buttons +
  Live-Status pro Geraet (Polling alle 3 s).
- Device-Form um vollstaendige SSH-Felder erweitert (Host, Port,
  User, Private-Key im Textarea). Bestehender Key wird beim Edit
  als "im Tresor hinterlegt" angezeigt und nur ueberschrieben wenn
  der User wirklich was tippt.
- Neue Runtime-Dep: ``paramiko>=3.4``. Audit-Trail komplett: arm,
  confirm, rollback, rollback_failed - alle vier Events sind im
  PRE_APPLY_BACKUP-Bucket einsortiert (kein neues Event-Kind noetig).
- Hinweis: SafetyNetWatcher ist **nicht** persistent. Server-Restart
  laesst aktive Entries fallen - der Watcher ist auf wenige Minuten
  Lifetime ausgelegt, deshalb akzeptabel.

### Signierter PDF-Audit-Report

- Neuer Download ``GET /api/audit/export.pdf`` parallel zu
  ``export.csv``. Liefert einen Querformat-A4-Report mit Header
  (Erstellt-Zeit, Filter, Eintragszahl), Tabelle der Records
  (Zeit, Akteur, Event, Zusammenfassung) und Signatur-Footer.
- Signatur = HMAC-SHA256 ueber alle Records mit dem bereits
  vorhandenen Audit-Chain-Secret. HMAC + SHA256(Inhalt) landen
  sichtbar im Footer + maschinell auslesbar in den PDF-Metadaten
  (``Keywords: OPN-COCKPIT-AUDIT-SIG-v1:<hex>``).
- Verifikation via ``audit.pdf_report.verify_pdf_signature``:
  Records + erwartete Signatur + Secret -> konstantzeit-Bool.
  Render-Pfad ist deterministisch (Realtime-Daten nur in den
  Metadaten, nicht in den signierten Bytes) damit Reproduktion
  funktioniert.
- Neue Runtime-Dep: ``fpdf2>=2.7``. Leichter als reportlab,
  baut sauber auf Windows + Linux.
- UI: Neuer Button "Als PDF (signiert)" im Audit-Modal-Footer
  neben dem CSV-Export.

### Unbound-DNS-CRUD + Compare

- Viertes Subsystem in der Plan-Pipeline: ``unbound_hosts``. Adapter
  fuer Host-Overrides via ``/api/unbound/settings/{add,set,del}HostOverride``
  + ``/api/unbound/service/reconfigure``. Identitaet = ``(host, domain)``.
- **DNS-Tab im Device-Modal**: Live-Liste der Host-Overrides via
  ``GET /api/inventory/devices/{id}/unbound-hosts``, mit
  **Neuer Host-Override** / **Bearbeiten** / **Loeschen**. Edit-Modal
  hat 5 Felder (host, domain, server-IP, enabled, description); die
  Identitaet (host + domain) ist beim Edit gesperrt.
- Neue Plan-Endpoints ``/api/plans/unbound-host``,
  ``/api/plans/unbound-host-update``, ``/api/plans/unbound-host-delete``.
- **Config-Compare-Tab "DNS"**: ``compare_unbound_hosts`` mit
  Identity-Key ``host|domain``, Fingerprint ueber Server + Description
  + Enabled-Flag. Cell-Label im Compare-Matrix zeigt die Ziel-IP.
- Damit haben jetzt alle vier produktiven Subsysteme den vollen
  CRUD-Kreis: Aliase, Routen, Filter-Regeln, Unbound-Hosts.

### Frontend-Inline-Validierung

- Inputs mit ``data-validate="<key>"`` bekommen jetzt eine
  Client-Validierung beim Tippen + Blur. Falsche Werte zeigen einen
  roten Border + kleinen Inline-Hint unter dem Feld; Submit bleibt
  weiterhin vom Server geprueft (Defense-in-Depth).
- Validatoren: ``cidr`` (IPv4 + Host-Bits-Check), ``ipv4``,
  ``host`` (FQDN oder IPv4), ``aliasName``,
  ``gatewayName``, ``port`` (Zahl, Range, "any" oder Alias-Name).
- Markiert: Route-Network + Gateway-Name, Alias-Name, Rule-Src/Dst-
  Port + Gateway, Device-Host beim Anlegen. Andere Felder folgen
  bei Bedarf, das System ist erweiterbar (1x ``data-validate``
  setzen reicht).

### Auto-Retry-Persistenz + Orphan-Adoption

- ``RetryWatcher``-Queue ueberlebt jetzt Server-Restart UND Session-
  Sperre. State liegt in ``<app_data>/state/retry-queue.json`` und wird
  nach jeder Mutation atomar geschrieben (write-to-tmp + os.replace).
- Jobs tragen jetzt einen ``vault_path`` neben dem ``session_token``.
  Beim Lock wird der Token nicht mehr geloescht, sondern auf ``""``
  gesetzt (Orphan). Beim naechsten Vault-Unlock adoptiert der Watcher
  den Job ueber den vault_path und uebernimmt den neuen Token.
- Restart-Pfad: nach Watcher-Init werden persistierte Jobs als Orphan
  geladen; Daemon-Thread startet automatisch, wenn die Queue nicht
  leer ist. Sobald wieder jemand den entsprechenden Tresor entsperrt,
  laufen die Retries weiter (bis max_duration ablaeuft).
- Folge fuer LXC-Updates: ``apt update``, ``opn-cockpit``-Restart -
  arme Retries gehen nicht mehr verloren.

### Config-Compare fuer Routes + Rules

- Compare-Modal kann jetzt zwischen drei Subsystemen wechseln:
  **Aliase**, **Routen**, **Regeln** (Tab-Strip oben). Backend-Endpoint
  ``POST /api/inventory/compare`` akzeptiert ``subsystem=routes|rules``
  zusaetzlich zu ``aliases``.
- ``config_compare.py`` erweitert um ``extract_routes`` /
  ``compare_routes`` (Identitaet = ``network|gateway``,
  Fingerprint ueber descr + disabled) und ``extract_rules`` /
  ``compare_rules`` (Identitaet = Description-Key oder Fingerprint
  bei fehlender Description, Fingerprint ueber Action/Interface/
  Direction/Protocol/Src/Dst/Gateway/Log/Enabled).
- Rules werden aus dem core-XML gelesen, nicht ueber die
  os-firewall-API - der Vergleich funktioniert auch wenn das Plugin
  auf einzelnen Boxen fehlt.
- UI: Cell-Label adaptiert sich pro Subsystem (Aliases: "N type",
  Routes: "via gw", Rules: "action"). Drift-Markierung weiterhin
  master-relativ via Fingerprint-Vergleich.
- **Sync-Button** bleibt vorerst Alias-only. Routes/Rules-Sync
  folgt - der OPNsense-Inhaltstransfer pro Subsystem ist mehr Arbeit
  als nur das UI-Feld umzustellen.

### Firewall-Rules CRUD (Erst-Iteration)

- Neues Subsystem ``firewall_rules`` mit ``RuleAdapter`` +
  ``RulesController``. Identity = OPNsense-UUID (Rules haben keinen
  stabilen User-Schluessel - die UI uebergibt die UUID aus der
  Live-Liste). ``RuleSpec`` deckt die haeufigsten Felder ab: enabled,
  action, interface, direction, ipprotocol, protocol, source_net/port/not,
  destination_net/port/not, gateway, log, description, sequence.
- **Regeln-Tab im Device-Modal**: Live-Liste aller Filter-Regeln via
  OPNsense-``searchRule`` mit Filter + **Neue Regel** /
  **Bearbeiten** / **Loeschen**. Edit oeffnet ein dediziertes
  Rule-Modal (modal-card-wide), Submit erzeugt einen Plan und springt
  in die Preview.
- Neue Endpoints:
  ``GET /api/inventory/devices/{id}/firewall-rules``,
  ``POST /api/plans/rule``, ``POST /api/plans/rule-update``,
  ``POST /api/plans/rule-delete``.
- **Voraussetzung**: ``os-firewall``-Plugin auf der OPNsense
  (in 24+/26+ Standard). Bei fehlendem Plugin liefert der List-
  Endpoint ``reachable=false`` mit Hinweis statt 500.

### Route-CRUD vollstaendig

- **Routen-Tab** im Device-Modal: integrierte Liste aller statischen
  Routen pro Geraet (gezogen ueber den OPNsense-``searchroute``-Endpoint),
  jeweils mit **Bearbeiten**- und **Loeschen**-Button. Bearbeiten oeffnet
  das Plan-Modal mit vorbefuelltem Routen-Formular und gesperrter
  Identitaet (network + gateway); Loeschen geht ueber ein Confirm-Dialog
  in den Delete-Plan-Flow.
- ``RouteAdapter`` hat jetzt echte ``update``- und ``delete``-Impls
  (setroute/{uuid} bzw. delroute/{uuid}) plus ``diff_for_update`` und
  ``diff_for_delete``. ``_search_uuid``-Helper findet die Routen-UUID
  via CIDR-normalisiertem Vergleich.
- Neuer Save-Failed-Helper ``_raise_if_saved_failed`` zieht das frueher
  in ``add`` inline geprueften ``result=failed``-Pattern in einen
  gemeinsamen Helper (von add/update/delete genutzt).
- Neue Endpoints ``GET /api/inventory/devices/{id}/routes``,
  ``POST /api/plans/route-update`` und ``POST /api/plans/route-delete``.

### Alias-CRUD vollstaendig

- **Alias bearbeiten**: Im Device-Modal → Aliase-Tab steht pro Alias jetzt
  ein **Bearbeiten**-Button. Oeffnet das Plan-Modal mit den aktuellen
  Werten vorbefuellt; Submit erzeugt einen ``update_alias``-Plan und
  landet direkt im Preview. Pre-Apply-Backup + Audit + Post-Apply-
  Baseline kommen automatisch mit (gleicher Flow wie Add).
- **Alias loeschen**: Pro Alias ein **Loeschen**-Button mit Confirm.
  Bei Bestaetigung erzeugt Cockpit einen ``delete_alias``-Plan und
  navigiert in die Preview. Idempotent: Geraete ohne den Alias werden
  als SKIP gefuehrt.
- Neue Endpoints ``POST /api/plans/alias-update`` und
  ``POST /api/plans/alias-delete``. Verb ``update_alias`` /
  ``delete_alias`` taucht im Audit + Plan-Listen-Filter auf.

### Architektur

- ``ObjectAdapter``-Protocol um ``update``, ``delete``, ``diff_for_update``,
  ``diff_for_delete`` erweitert. ``DiffKind.DELETE`` ergaenzt.
- ``Plan`` traegt jetzt ``action_kind`` (``ActionKind.ADD|UPDATE|DELETE``).
  Executor switcht die Adapter-Method anhand des Kind; Verify-Erwartung
  invertiert bei DELETE (found=False == Erfolg). Default ADD haelt
  pre-0.8-Plaene rueckwaerts-kompatibel.
- ``RouteAdapter`` hat Stub-Methoden fuer Update/Delete mit
  ``NotImplementedError`` - Route-CRUD ist die naechste Iteration.

---

## v0.7.0 — 2026-06-03 — Safety-Nets, Multi-Site-Tools, Windowless-Install

Großer Funktions-Schub rund um die Themen "ich will sehen was passiert
bevor es schiefgeht" (Safety-Nets), "ich will meine N gleichen Boxen
synchron halten" (Multi-Site-Tools) und "der Single-User-Desktop soll
nicht mit Konsolen-Fenster nerven" (Windowless-Install).

### Safety-Nets

- **Auto-Backup vor Apply** (v0.7 #2): Der Executor zieht vor jedem
  schreibenden Apply automatisch ein gzip-Backup pro Gerät. Scheitert
  das Backup, wird der Apply auf diesem Gerät blockiert (Audit:
  `backup_blocked`). Retention 30 (default).
- **Cert-Ablauf-Badge** (v0.7 #3): Karten zeigen gelben Badge bei
  Cert-Restlaufzeit < 30 Tagen, rot bei < 7 Tagen. Klick öffnet
  Cert-Detail-Liste pro Gerät mit Aussteller, IN-USE-Marker.
- **Scheduled Auto-Backup** (v0.7 #4): Hintergrund-Thread zieht im
  konfigurierten Intervall (Default 24h) ein Backup pro Gerät. Eigener
  Retention-Pool, separat vom Pre-Apply. Default AUS (Opt-In).
- **Config-Drift-Erkennung** (v0.7 #5): Vergleich der Live-OPNsense-
  Config-Hashes (volatile `<revision>`-Blöcke gestripped) gegen das
  letzte lokale Backup. Drift-Badge auf der Kachel wenn ungleich.
  Default AUS.

### Multi-Site-Tools

- **Config-Compare zwischen N Geräten**: "Vergleichen"-Button erscheint
  ab 2 selektierten Geräten. Matrix zeigt Aliase pro Gerät mit Status
  (present/absent/drift/unreachable). Drift-Zeilen gelb markiert.
  Erste Iteration: nur Aliases; Routes/Firewall-Rules folgen.
- **Alias-Sync (Master → Targets)**: In jeder Drift-Row gibt es einen
  "Sync…"-Button. Wählt einen Master (bei mehreren Varianten via
  Prompt), erzeugt einen add_alias-Plan für alle anderen Geräte der
  Zeile. Springt nach Erfolg in den Plan-View.
- **Alias-Manager-View** (read-only): Pro Gerät eine durchsuchbare
  Liste aller Aliase mit Inhalt, Beschreibung, Deep-Link zur OPNsense-
  Bearbeitung. Vorbereitung für künftigen Edit/Delete-Adapter.
- **Auto-Retry-Queue für Mobile-Racks**: Wenn Geräte beim Apply als
  FAILED zurückkommen, schedult Cockpit automatisch einen Retry-Job.
  Default-Wartezeit 7 Tage, Intervall 5 Min. Sobald die Box wieder
  erreichbar ist, zieht der Watcher den Plan ohne weiteres Zutun nach.

### Diagnose-Verbesserungen

- **TCP-Timeout-Aufsplittung**: `connect_timeout` vs `read_timeout`
  in `error_kind` und Frontend-Anzeige. Halbiert Diagnose-Suchfeld.
- **ICMP-Probe-Fallback** bei `connect_timeout`: Wenn die Box per Ping
  erreichbar ist aber TCP nicht, sagt Cockpit jetzt "Host antwortet
  auf Ping, aber Port X ist zu — Firewall/Routing/Service-Status
  prüfen". Spart bei asymmetrischen Routing-Setups Stunden Diagnose.

### Installer + Setup

- **Windowless Single-User-Install (Windows)**: Desktop-Shortcut
  nutzt jetzt `opn-cockpitw.exe` (= pythonw.exe-Kopie). Kein
  schwarzes Konsolen-Fenster mehr beim Doppelklick. Logs landen in
  `%APPDATA%\OPN-Cockpit\logs\opn-cockpit.log` (Auto-Redirect bei
  `sys.stdout==None`, naive Rotation bei >5 MiB).
- **Uninstaller-Sanierung**: Service wird jetzt sauber entfernt
  (nicht nur gestoppt), bundle/python/scripts werden zuverlässig
  geräumt, kein Lock-out-Problem mehr beim Re-Install.
- **Proxmox-Helper-Wizard**:
  - Getrennte und sprechende Fragen für Container-Rootfs-Storage und
    Template-Storage (vorher mehrfach verwechselt)
  - Locale wird auf `C.UTF-8` gesetzt → keine `apt-listchanges`-
    Warnings mehr während des Installs
  - Container-Notes-Feld wird automatisch mit Login + Update-One-Liner
    + GitHub-Links befüllt (Markdown-gerendert in Proxmox-UI)
  - Deutlicher Hard-Reload-Hinweis am Ende des Updates

### UX-Politur

- **Nativer Windows-Datei-Dialog im Tresor-Switch-Modal**: "Datei
  suchen..."-Button öffnet `comdlg32.GetOpenFileNameW` auf dem Server
  (Single-User-Mode + Windows). Single-User-Vault-Path-Restriktion
  entfernt → USB-Sticks etc. funktionieren ohne Konfiguration.
- **API-Key sichtbar im Edit-Dialog** zur Verifikation (Secret bleibt
  maskiert).

### Bug-Fixes

- **Cert-Inventur**: `/api/trust/cert/search` liefert nur Metadaten,
  Validity-Felder kamen leer durch. Fix: pro Cert `/cert/get/<uuid>`
  nachschieben und PEM lokal mit `cryptography.x509` parsen.
- **TLS-Verifikations-Fehler** als eigene Kategorie statt Netzwerk-
  Fehler (vorher: "nicht erreichbar: network"; jetzt: "TLS-Verifikation
  fehlgeschlagen: Cert ungültig").
- **Hostname-Validierung** akzeptiert ganze URLs (`https://host.lan/`
  → `host.lan`) und interne TLDs (`.lan`, `.local`).
- **Whitespace im API-Secret** wird beim Speichern getrimmt.
- **Plan-Cancel räumt Plan auf**: "Plan verwerfen"-Button auf Vorschau
  + automatisches Aufräumen beim Abbrechen aus der Bearbeitungs-Phase.

### Internal

- `BackupScheduler` (web/backup_scheduler.py) als neuer Daemon-Thread,
  Pattern analog zum bestehenden RetryWatcher.
- `core/config_drift.py` + `core/config_compare.py` als neue Module
  für die XML-Hash- bzw. Matrix-Logik.
- `SessionManager.snapshot_active()` für den Scheduler-Tick.
- Neue AuditEventKinds: `SCHEDULED_BACKUP`.
- VaultSettings erweitert um 7 neue Felder (alle mit Defaults für
  Backwards-Compat mit alten Tresoren).
- `iputils-ping` im Linux-Installer (für ICMP-Probe).

### Phase 2 (nach erstem v0.7-Test)

User-Test deckte mehrere Punkte auf, die wir im selben Release noch
mitgenommen haben:

**Bugs:**
- Login-Maske + alle anderen Versions-Anzeigen zeigten statisch
  `v0.6.3.dev0` statt der echten Release-Version → `get_runtime_version()`
  überall durchgereicht (statt nur in About). Git-Tag-Praefix `v` wird
  jetzt vor dem Template-Render gestrippt, sonst rendert das Brand-
  Label `vv0.7.0`.
- `BackupScheduler` lief nur solange eine Browser-Session offen war.
  Im Multi-User-Server-Mode (LXC) wird der zentrale Vault aber durchgehend
  entsperrt gehalten — Scheduler nutzt jetzt `server_state.opened_vault`
  als Quelle, deduppt gegen Sessions. 24/7-Operation jetzt garantiert.
- `__version__` von `0.6.3.dev0` auf `0.7.0.dev0` gebumpt damit der
  Fallback-Wert ehrlich ist (Windows-Installer ohne Git-Repo).
- Login-Maske hatte das Dropdown noch — File-Picker-Refactor jetzt
  auch dort (gleiche Pattern wie Vault-Switch-Modal nach Login).
- Alias-Sync → "Vorschau anzeigen" gab Fehler "Pflichtfelder" weil
  ich `openPlanModal(plan_id)` rief statt einer Funktion die einen
  bestehenden Plan in die Vorschau lädt. Neue `openExistingPlanInPreview`
  springt direkt zur Preview-Phase.

**Compare-Matrix UX:**
- Master-Wahl per ◀ / ▶ / ★ direkt im Spalten-Header (statt
  Prompt-Picker). Spalte ganz links = Master, optisch hervorgehoben
  mit Olive-Border + ★-Pill.
- Cells werden relativ zum Master gefärbt: identischer Fingerprint =
  grün, abweichend = gelb. Schneller erkennbar wo Drift sitzt.
- Detail-Aufklapp pro Alias-Row (▶ / ▼ Icon vor dem Namen): zeigt den
  vollständigen Inhalt pro Gerät vor dem Sync-Klick. Backend gibt
  jetzt `content: list[str]` pro Cell mit.

**Device-Modal Restrukturierung:**
- Modal-Card-Wide statt -Narrow.
- Browser-Tab-Style Tab-Strip: **Info | Updates | Backups | Aliase**.
- Info-Tab: 4 Haupt-Buttons im 2×2-Grid (Test, OPNsense, Duplizieren,
  Update-Check), Detail-Liste, URL, Sekundär-Buttons (Bearbeiten,
  Backup herunterladen) im Footer-Bereich.
- Updates-Tab: installierte + verfügbare Version, "Aktuell"-/"Update
  verfügbar"-Badge, OPNsense-Status-Message, "Erneut prüfen".
- Backups-Tab: integrierte History (Pre-Apply / Manuell / Geplant),
  "Backup jetzt ziehen"-Button.
- Aliase-Tab: integrierter Browse-View. Beide vorher separaten Modale
  (Aliases + Backup-History) komplett entfernt.
- Karten-Badge-Klicks (Backups, Aliase) öffnen das Device-Modal mit
  dem passenden Tab aktiv — kein zweites Modal hinter der Karte mehr.

**Karten-Polish:**
- "Port / TLS / Heartbeat-Alter"-Stats-Zeile entfernt (Info im Modal).
- Hover-Quick-Actions in der Status-Row: OPNsense öffnen, Updates
  suchen, Duplizieren — fade-in beim Card-Hover (opacity-Transition).
  Default-State der Karte deutlich ruhiger.

**Sonstiges:**
- User-Anlage: Tag-ACL-Hint von "Heute kosmetisch" auf "User sieht
  nur Geräte mit mindestens einem der Tags" aktualisiert — die ACL
  ist seit v3.0 Iter 4 (Mai 2026) live, der Hint war veraltet.

### Phase 3 (nach zweitem v0.7-Test)

- **Post-Apply-Backup**: Nach jedem erfolgreich verifizierten Apply
  zieht der Executor ein zweites Backup (`trigger="post-apply"`) und
  schreibt es in den gleichen Retention-Pool wie pre-apply/manual.
  Damit wird die Drift-Erkennung nach Apply nicht mehr von der
  Pre-Apply-Baseline als False-Positive ausgelöst — der neueste
  Snapshot reflektiert die jetzt-live-Konfig.
- **"Backup erzeugen" im Backups-Tab** (statt "Backup ziehen"):
  Neuer Endpoint `POST /api/inventory/devices/{id}/backups` erzeugt
  ein Backup ausschließlich auf dem Server (lokal persistiert, kein
  Browser-Download-Dialog). Der "Backup herunterladen"-Button im
  Info-Tab bleibt unverändert (XML-Stream zum Client).

---

## v0.6.0 — 2026-06-01 — Multi-User-Server + Linux-Deployment

Erste Release-Version mit echter Mehr-Plattform- und Mehr-Nutzer-Auslieferung.
v0.6.0 hat denselben Funktionsumfang wie v2.0 (intern), läuft aber jetzt in
vier produktiv nutzbaren Varianten: Windows-Single-User, Windows-Multi-User-
Server (NSSM-Dienst), Linux-Server (systemd) und Proxmox-LXC. Erster
Public-Release auf GitHub.

### Authentifizierung — Default-Admin statt Bootstrap-Token

- Beim ersten Start legt der Server automatisch einen Default-Admin an
  (`admin` / `OPN-Cockpit!`) mit Pflicht-Passwort-Wechsel beim ersten Login.
  Pragmatisch wie Proxmox: kein Token-Kopieren mehr, keine Token-Datei,
  keine Setup-Schritte „erster Boot wartet auf Konsolen-Output".
- `users.db`-Schema versteht `must_change_password` (SQLite `ALTER TABLE`-
  Migration läuft beim Boot).
- Selbstheilender Check via `get_user_by_name` statt `count()` — bei
  zerstörter User-Tabelle wird der Default-Admin neu angelegt.
- Tresor-Operationen sind blockiert, solange das Default-Passwort gilt
  (Server-Status `ready_with_default_password`).
- Bootstrap-Vault-Endpoint kombiniert Login + PW-Wechsel + Vault-Setup
  in einem Schritt — der Setup-Wizard zeigt nur noch **einen** Step.

### Multi-User-Server (Windows + Linux)

- **Windows**: Installer-Wizard hat neuen Komponententyp „Multi-User-Server".
  Registriert OPN-Cockpit als NSSM-basierten Windows-Dienst mit Autostart,
  bindet auf `0.0.0.0:9876`. Service läuft unter `LocalService`. Vault-Upload
  via `multipart/form-data` statt Pfad-Eingabe (LocalService kann User-
  Pfade nicht sehen).
- **Linux/Debian**: `installer/linux/install.sh` legt System-User
  `opncockpit` an, baut Python-venv unter `/opt/opn-cockpit`, packt Daten
  nach `/var/lib/opn-cockpit`, aktiviert systemd-Unit mit Hardening-Flags
  (NoNewPrivileges, ProtectSystem=strict, PrivateTmp, ProtectHome).
- Beide Modi nutzen den Default-Admin-Flow.

### Proxmox-LXC-Helper (whiptail-TUI im Community-Scripts-Stil)

- `installer/linux/proxmox-helper.sh` — ein Befehl auf dem PVE-Host:
  ```bash
  bash -c "$(wget -qLO - https://raw.githubusercontent.com/ludwig-systems/opn-cockpit/main/installer/linux/proxmox-helper.sh)"
  ```
- **Dual-Mode**: derselbe Link funktioniert auf PVE-Host (Container anlegen)
  UND im Container (in-place Update). Erkennung über `pveam`-Vorhandensein
  bzw. `/opt/opn-cockpit + systemd-Unit`.
- TUI führt durch Container-ID, Hostname, Storage-Pool (Auswahl-Menü aus
  `pvesm status -content rootdir`), Disk-Größe, CPUs, RAM, Bridge (Menü
  aller `vmbr*`), DHCP/Statisch, IPv4/Gateway/VLAN/MAC/DNS — alles per
  Pfeiltasten + Enter, kein Frei-Text mehr.
- Update-Modus belässt `/var/lib/opn-cockpit/` (Vault, Audit, User-DB,
  Settings) unangetastet; nur `/opt/opn-cockpit` wird via `git fetch +
  reset --hard` + `pip install` aktualisiert. Migrations laufen beim
  Service-Start mit Pre-Backup.

### Brand, UI und Design-Guide

- Kompass-Stern-Logo als Inline-SVG überall (Boot-Splash, Header, Login,
  About-Modal); Favicon mit `prefers-color-scheme`-Variante.
- Header höher (`topbar-height: 82px`), Brand-Logo 38 px, Headline 24 px,
  Icon-Buttons 40 px — proportional skaliert.
- Interner Design-Guide als verbindliche Referenz für künftige UI-Arbeit
  (Calm-Precision + Bahnschrift-Display + Olive-Akzent).
- Defensive CSS-Regeln gegen Click-Bug auf Proxmox-Browser-Stack
  (`pointer-events: auto !important` auf interaktive Elemente,
  `[hidden] { display: none !important; }`).
- Diagnose-Helper `window.__opnDiag()` in der Browser-Konsole für
  Click-Bug-Reports (listet fullscreen overlays + Element unter erstem
  Topbar-Icon).

### Features (aus FR-Liste umgesetzt)

- **OPNsense-Firmware-Version** pro Karte (`/api/core/firmware/status`)
  mit Caching pro Heartbeat-Intervall.
- **Backup ziehen** pro Karte (`/api/core/backup/download/this`) als
  direkter Download-Stream; neuer Audit-Event `BACKUP_DOWNLOADED`.

### Release-Pipeline

- GitHub-Actions-Release-Workflow gefixt: PowerShell-Here-String im
  YAML-`run: |`-Block sorgt für Indentation-Konflikt und Parse-Failure.
  Ersetzt durch String-Array + `-join "`n"`. Em-Dashes aus ISS-Kommentaren
  und Workflow-Inputs raus (CI-Sicherheit gegen non-UTF-8-Encoding bei
  alten Toolchains).
- `installer/opn-cockpit.iss` `[UninstallRun]` in 2 Stufen mit
  `waituntilterminated`: Pre-Step killt laufende Prozesse (Service +
  Single-User), zweiter Step entfernt NSSM-Service.
- `[UninstallDelete]` entfernt zusätzlich `users.db`, `BOOTSTRAP-TOKEN.txt`,
  `logs/` aus `%ProgramData%\OPN-Cockpit` — Re-Install bekommt sauberen
  Default-Admin-State.

### Operationelle Lehren

- PowerShell 5.1 liest .ps1-Files als CP-1252; Em-Dashes (`—`) brechen
  den Parser. **Alle Scripts ASCII-only.**
- Defensives `Set-ExecutionPolicy Bypass -Scope Process` oben in jedem
  Script gegen Group-Policy-Sperren.
- `pveam update` vor `pveam available` — sonst veralteter Katalog → 404.
- Vault-Upload für Multi-User-Server: Path-Validator kann User-Verzeichnis
  des aufrufenden Browsers nicht sehen, also wird die `.opnvault`-Datei
  per Multipart-Upload an den Server gestreamt und über `open_vault_bytes`
  entschlüsselt.

### Tests + Migration

- 580+ Tests grün (`tests/unit/`), 100 % Coverage im Web-Layer.
- SQLite-Migration-Framework läuft beim ersten Boot jedes Updates,
  schreibt Pre-Backup nach `/var/lib/opn-cockpit/backups/<ts>-pre-<v>/`.

---

## v2.0.0 — Web-Pivot

Komplette Umstellung der Präsentations-Schicht von PySide6-Desktop-GUI auf
lokale **FastAPI + Web-Frontend**. Core, Orchestrierung, Vault, Audit
bleiben unverändert. User-Entscheidung nach Mockup-Vergleich zugunsten
einer publikations-tauglichen Optik („Calm Precision"-Aesthetik).

### Iterations-Plan

- ✅ **Iter 1** (`d0743d2`): FastAPI-Backend-Skeleton, uvicorn-Boot,
  Browser-Auto-Open auf 127.0.0.1:9876, `/health` + `/api/version`,
  Boot-Splash.
- ✅ **Iter 2** (`582df22`): Auth-Flow (`POST /api/auth/unlock|lock`,
  `GET /api/auth/me`), Vault-Discovery + Inline-Create
  (`GET/POST /api/vaults`), Login-UI mit Tresor-Picker, Bearer-Token-
  Session in `sessionStorage`, 30 s-Expiry-Ticker mit Auto-Lock-UI.
- ✅ **Iter 3**: Inventar-Kachelansicht aus dem Mockup geliftet:
  Sidebar mit Tag-Filter + Aktionen, Karten-Grid mit Status-Dot, TLS-
  Badge und Heartbeat-Label, Topbar-Suche (Strg K), Add-/Detail-Modal.
  Backend: `GET /api/inventory`, `POST /api/inventory/devices`,
  `DELETE /api/inventory/devices/{id}`, `POST /api/inventory/heartbeat`
  (paralleler TCP-Probe ohne Auth-Last auf der OPNsense),
  `POST /api/inventory/devices/{id}/test-connection` (vollwertiger
  HTTPS-Auth-Probe).
- ✅ **Iter 3.1** (Polish + UX-Feedback):
  - Master-Passwort wird nur einmal beim Unlock erfragt und in der
    Session gecached — Add/Remove brauchen es nicht mehr. Der Cache
    lebt nur während der Session und wird beim Lock/Auto-Lock
    überschrieben. Spiegelt das Vertrauensmodell „erfahrene Admins,
    Schutz vor unbefugtem Zugriff, nicht Schutz vor sich selbst".
  - Farbpalette von Sage- auf Oliv-/Militärgrün gewechselt, Kontrast in
    beiden Themes deutlich erhöht (Text-Hierarchie, Borders, Karten-
    Stats jetzt klar lesbar).
  - Detail-Modal mit Buttons „OPNsense öffnen" (neuer Tab auf
    `https://host:port/`), „Duplizieren" (öffnet Add-Modal mit
    vorausgefüllten Feldern, API-Key/Secret bleiben leer) und einem
    2-Klick-Lösch-Pfad mit Puls-Animation.
  - Externer-Link-Icon auf jeder Karte für den direkten OPNsense-
    Webaufruf.
- ✅ **Iter 4** (`55d3925`): Plan/Apply für Routen + Aliase via Web — drei-
  stufiges Modal (Eingabe → Vorschau mit Confirm-Gate → Result-Matrix),
  Multi-Device-Picker, Bulk-Plan-Support, Cross-Tool-Sharing mit der CLI
  über denselben PlanStore.
- ✅ **Iter 5a** (`daf8144`): Audit-View. Topbar-Icon öffnet ein Modal mit
  Filter-Reihe (Event-Kind / Action / Geräte-ID), Truncate-Hinweis,
  Status-Pills semantisch eingefärbt.
- ✅ **Iter 5b** (`27dad8c`): Discovery — `/api/discover/...` für Gateway-
  und Alias-Namen aus der laufenden OPNsense. Frontend integriert das als
  Lazy-Load-Datalist im Plan-Modal („Vorschläge laden"-Button), nicht
  automatisch beim Modal-Öffnen wegen Offline-Geräten.
- ✅ **Iter 5c** (`c465a03`): Profile (Templates) — Vorlagen-Dropdown im
  Plan-Modal + „Als Vorlage speichern". Secrets werden beim Speichern
  via Whitelist sanitisiert, gemeinsamer Store mit der CLI.
- ✅ **Iter 5d** (`723044a`, später ersetzt durch 5.1): erste Bulk-Import-
  Variante für Routen-CSV / Alias-JSON.
- ✅ **Iter 5.1** (`59e73e8`): Architektur-Refactor nach User-Feedback —
  Bulk-Import sind jetzt **Firewall-Stammdaten** (CSV/JSON), nicht mehr
  Routen/Aliase. Karten kriegen sichtbare Auswahl-Checkbox mit Olive-
  Highlight. Neue Selection-Bar über dem Grid mit „Alle / Nur erreichbare
  / Keine". Plan-Modal hat keinen eigenen Device-Picker mehr — Aktionen
  laufen gegen die globale Karten-Selektion. Karten-Padding angepasst,
  damit die Auswahl-Checkbox die TLS- und URL-Icons nicht überdeckt
  (`021be1f`).
- ✅ **Iter 5.2** (`c0bf7b3`): Apply-Reports werden persistiert, neuer
  Endpunkt `/api/plans/outstanding`, Retry-Pfad mit `device_ids`-Filter
  im Apply-Body. Karten zeigen einen Amber-„N offen"-Badge, wenn Geräte
  in alten Plänen noch nicht Verifiziert sind. Result-Phase bietet
  „N fehlgeschlagene erneut versuchen". Verlustarme Auto-Recovery für
  „eine Box war offline"-Szenarien.
- ✅ **Iter 6**: PySide6 + alle GUI-Tests entfernt, README + QUICKSTART
  auf Web-First umgeschrieben, Inno-Setup-Skript in `installer/`
  abgelegt, CHANGELOG abgeschlossen.

### Architektur-Entscheidungen

- **Multi-User-fähig vorbereitet**: SessionManager mappt Token →
  Session-Objekt; spätere User-DB hängt sich ohne Schema-Bruch ein.
  TLS-Felder in `WebSettings` für späteren Server-Modus vorhanden.
- **Vanilla HTML/CSS/JS** ohne Build-Pipeline. Frontend ist eine
  Single-Page-State-Machine (boot/login/main) in `web/static/app.js`.
- **API-Schemas** zentral in `web/api/schemas.py` (Pydantic).
- **Token in sessionStorage** (per-Tab), nicht localStorage, kein
  Cookie → CSRF nicht relevant.

### Dependencies

Hinzugefügt: `fastapi >= 0.115`, `uvicorn[standard] >= 0.32`,
`jinja2 >= 3.1`, `python-multipart >= 0.0.12`. `PySide6` bleibt
vorerst installiert, wird in Iter 6 entfernt.

### Tests-Stand v2.0-Final

550+ Tests grün (130+ im `tests/unit/web/`-Tree). PySide6-Tests komplett
entfallen. ruff + mypy strict clean. 81 Source-Files in `src/`.

---

## v1.2.0 — 2026-05-28 — GUI-first Boot-Flow

Letzte PySide6-Iteration. Boot-Flow ohne Shell:
- `vault/discovery.py` scannt `%APPDATA%/OPN-Cockpit/*.opnvault` +
  Recent-Vault-Liste der App-Settings.
- Neuer `CreateVaultDialog` für den Erst-Setup.
- `LoginDialog`-Rewrite mit ComboBox je nach Treffer-Anzahl
  (0 Tresore → Hinweis, 1 → vorausgewählt, >1 → Auswahl).
- `start.bat` + `start.ps1` als Doppelklick-Launcher.

12 neue Tests, 459 Tests gesamt.

---

## v1.1.0 — 2026-05-28 — Heartbeat + API-Discovery

### Hinzugefügt

- **TCP-Reachability-Heartbeat im Inventar**: Pro Gerät zeigt die GUI ein
  Pünktchen (grün/rot/gelb/grau) für den letzten TCP-Connect-Status auf
  den API-Port an. Im Hintergrund alle 30 s automatisch aktualisiert,
  manueller Refresh über den "Heartbeat jetzt prüfen"-Button.
  Bewusst kein ICMP-Ping (Windows-Admin-Rechte + Firewall-Probleme) —
  TCP auf den Konfig-Port ist der ehrlichste Erreichbarkeits-Indikator,
  weil es exakt der Pfad ist, den das Tool auch zum Schreiben benutzt.
- **API-Discovery für Gateways und Aliase**: Neues `core/discovery`-Modul
  mit `list_gateways(client, target, key, secret)` und
  `list_aliases(client, target, key, secret)`. Defensiv gegen
  Schema-Drift — unbekannte Antwort-Formate führen zu leerer Liste,
  nur HTTP-Fehler wandern als `DiscoveryError` zum Aufrufer.
- **CLI**: `discover gateways --target id:X` und
  `discover aliases --target id:X` listen die vorhandenen Namen
  tabellarisch.
- **GUI**: Action-Dialoge für Route und Alias bieten optionale
  "Vorschläge laden"-Buttons. Bei Klick erscheint die ComboBox mit
  den Namen vom ausgewählten Referenz-Gerät — Tippfehler bei
  case-sensitiven Gateway-Namen (`V2_WANBwIn` vs `v2_wanbwin`)
  werden so deutlich seltener.

### Geändert

- `RouteAdapter` und `AliasAdapter` blieben unverändert — Discovery
  arbeitet außerhalb der bestehenden Adapter und nutzt die bekannten
  Endpoints.
- Action-Dialoge sind rückwärtskompatibel: ohne injizierte Callbacks
  verhalten sie sich wie in v0.1.0 (reine Freitext-Eingabe).

### Tests

- 447 Tests (32 neu vs v0.1.0), Core 91–100 %, ruff & mypy strict clean.

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
