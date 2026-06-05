# Feature-Anleitungen

Schritt-für-Schritt-Bedienung der OPN-Cockpit-Features pro Subsystem.
Für den groben Quickstart siehe [QUICKSTART.md](QUICKSTART.md).

Inhalt:
- [Aliase verwalten](#aliase-verwalten)
- [Statische Routen](#statische-routen)
- [Firewall-Filter-Regeln](#firewall-filter-regeln)
- [Unbound-DNS Host-Overrides](#unbound-dns-host-overrides)
- [Config-Compare zwischen Geräten](#config-compare-zwischen-geraeten)
- [Auto-Backup vor + nach Apply](#auto-backup-vor--nach-apply)
- [Config-Drift-Erkennung](#config-drift-erkennung)
- [Auto-Retry für Mobile-Racks](#auto-retry-fuer-mobile-racks)
- [Safety-Net via SSH](#safety-net-via-ssh)
- [Audit-Log + signierter PDF-Export](#audit-log--signierter-pdf-export)
- [Inline-Validierung](#inline-validierung)

---

## Aliase verwalten

**Anlegen:** Eine oder mehrere Karten markieren → Sidebar **„Alias
hinzufügen"**. Name, Typ (`host`, `network`, `port`, `url`, `urltable`,
`geoip`, `external`), Inhalte (komma-getrennt), Beschreibung, optional
Merge-Mode (`create` = neu anlegen, `append` = an bestehenden hängen).
Vorschau → Bestätigen → Apply.

**Bearbeiten:** Karte → **Aliase-Tab** → bei der Zeile auf **„Bearbeiten"**
klicken. Das Plan-Modal öffnet sich mit den aktuellen Werten vorbefüllt;
der Alias-Name ist gesperrt (Identität bleibt). Submit erzeugt einen
`update_alias`-Plan, der das gesamte `content`/`type`/`description`
ersetzt. Pre-Apply-Backup wird gezogen.

**Löschen:** Karte → **Aliase-Tab** → **„Löschen"**. Confirm-Dialog,
danach `delete_alias`-Plan → Vorschau → Apply. Idempotent: Geräte, auf
denen der Alias schon weg ist, werden als SKIP gemeldet.

**Endpoint** intern: `POST /api/firewall/alias/{addItem,setItem,delItem}/...`
+ `reconfigure`. Read-back gegen `searchItem` mit Name-Match.

---

## Statische Routen

**Anlegen:** Karte(n) markieren → Sidebar **„Route hinzufügen"**. Netz
(CIDR mit Host-Bits-Check, Inline-Validierung), Gateway-Name, optional
Beschreibung + `disabled`-Flag. Apply läuft via `addroute` + `reconfigure`,
Verify per `searchroute` mit Netz/Gateway-Match.

**Bearbeiten/Löschen:** Karte → **Routen-Tab** → Bearbeiten/Löschen pro
Zeile. Identität (Netz + Gateway) ist beim Edit gesperrt; Beschreibung
und `disabled` sind editierbar. Identische Werte → SKIP.

---

## Firewall-Filter-Regeln

> ⚠️ **Voraussetzung:** `os-firewall`-Plugin auf der OPNsense (ab 24+
> Standard). Ohne Plugin liefert der List-Endpoint `reachable=false` mit
> Hinweis.

**Live-Liste:** Karte → **Regeln-Tab** → Cockpit ruft
`POST /api/firewall/filter/searchRule` und zeigt alle Regeln mit
Aktion · Interface · Direction · Protocol · Source → Destination.

**Anlegen:** Im Regeln-Tab oben **„Neue Regel"**. Modal mit 15 Feldern:

| Feld | Werte |
|---|---|
| Aktiv | Checkbox |
| Action | `pass` / `block` / `reject` |
| Richtung | `in` / `out` |
| Interface | OPNsense-Interface-Identifier (`lan`, `opt1`, …) |
| IP-Version | `inet` / `inet6` |
| Protokoll | `any` / `tcp` / `udp` / `tcp/udp` / `icmp` / `esp` / `ah` |
| Quelle / Quell-Port | Netz / Alias / `any`; Port-Validierung inline |
| Quelle invertieren | Checkbox |
| Ziel / Ziel-Port | analog |
| Ziel invertieren | Checkbox |
| Gateway | optional |
| Sequenz | optional, Ganzzahl |
| Log | Checkbox |
| Beschreibung | freier Text |

**Identität = OPNsense-UUID.** Da Regeln keinen stabilen User-Schlüssel
haben, identifiziert Cockpit Edit/Delete per UUID aus der Live-Liste.
Ein zweiter „Add"-Klick erzeugt eine zweite Regel — kein Duplikat-Check.

**Bearbeiten:** Zeile → **„Bearbeiten"**. Vorbefülltes Modal, UUID
implizit; Submit → `update_rule`-Plan → Vorschau → Apply.

**Felder die Cockpit (noch) nicht abbildet:** `statetype`,
`statetimeout`, `tagging`, `schedule`, `quick`. Müssen direkt in der
OPNsense-Web-UI gesetzt werden.

---

## Unbound-DNS Host-Overrides

**Live-Liste:** Karte → **DNS-Tab** → `POST /api/unbound/settings/searchHostOverride`.
Pro Eintrag: `host.domain → server-IP`, optional Beschreibung,
deaktiviert-Flag.

**Anlegen:** Tab → **„Neuer Host-Override"**. Felder:

| Feld | Beschreibung |
|---|---|
| Hostname | linker Teil (z. B. `opnsense`) |
| Domain | rechter Teil (z. B. `lab.local`) |
| Ziel-IP | IPv4-Validierung inline |
| Aktiv | Checkbox |
| Beschreibung | freier Text |

**Identität = (host, domain).** Beim Edit sind beide gesperrt. Server-IP +
Beschreibung + Aktiv-Flag sind editierbar. Identische Werte → SKIP.

---

## Config-Compare zwischen Geräten

Mindestens zwei Karten markieren → Selektions-Toolbar **„Vergleichen"**.

**Tab-Strip** oben: *Aliase | Routen | Regeln | DNS*. Beim Wechsel wird
die Matrix für das ausgewählte Subsystem neu geladen.

**Matrix-Aufbau:**
- Spalten = Geräte. Die linkeste Spalte ist der **Master** (Olive-Border
  + ★-Pill). Master per ◀ / ▶ / ★ im Spalten-Header verschiebbar.
- Zeilen = Einheiten:
  - Aliase: Name
  - Routen: Netzwerk (Gateway in der Cell)
  - Regeln: Description (oder Fingerprint wenn leer)
  - DNS: `host.domain`

**Cells** zeigen Status + master-relative Drift-Marker:
- 🟢 **Vorhanden + identisch** zum Master (gleicher Fingerprint)
- 🟡 **Drift** — vorhanden, aber unterschiedlicher Inhalt
- ⚪ **Fehlt** — Eintrag existiert nicht auf diesem Gerät
- ❓ **Unerreichbar** — Gerät war beim Compare nicht ladbar

**Detail-Aufklapp:** ▶-Icon vor jeder Zeile zeigt den vollen Inhalt
pro Gerät (Aliase: Mitglieder; Routen: descr + disabled; Regeln:
flow-string; DNS: server + descr).

**Sync (nur Aliase):** in Drift-Zeilen erscheint **„Sync ←"** —
erzeugt einen `add_alias`-Plan vom Master zu allen anderen Spalten,
springt direkt in die Preview. Sync für Routen/Rules/DNS folgt
in einer späteren Iteration.

**Rules-Quelle:** Cockpit liest Regeln für den Compare aus dem
**Konfig-XML** (`download_backup`), nicht aus der os-firewall-API.
Damit funktioniert der Vergleich auch wenn das Plugin auf einzelnen
Boxen fehlt.

---

## Auto-Backup vor + nach Apply

**Vor Apply:** Wenn `auto_backup_before_apply` in den Tresor-
Einstellungen an ist (Default an), zieht der Executor pro Gerät ein
gzip-Backup *vor* dem Write. Scheitert das Backup, wird der Apply auf
diesem Gerät **blockiert** — kein erfolgreicher Apply ohne Rollback-
Anker.

**Nach Apply:** Symmetrisch zum Pre-Apply: nach erfolgreichem
`Status.VERIFIED` zieht der Executor ein zweites Backup mit
`trigger="post-apply"`. Damit ist der jüngste Snapshot wieder = die
jetzt-live-Konfig — und die Drift-Erkennung schlägt nach einem eigenen
Apply nicht als False-Positive an.

**Retention:** `manual`, `pre-apply` und `post-apply` teilen sich den
Pre-Apply-Pool (Pre/Post-Apply-Paare verfallen gemeinsam). `scheduled`
hat einen eigenen Pool.

**Backup-Tab** im Device-Modal listet alles auf. **„Backup erzeugen"**
legt einen `manual`-Snapshot rein (server-only, kein Download-Dialog).
**„Backup herunterladen"** im Info-Tab streamt die aktuelle Konfig zum
Browser.

---

## Config-Drift-Erkennung

Vault-Setting `drift_detection_enabled` aktivieren. Cockpit ruft
periodisch pro Karte den Live-Config-Hash ab und vergleicht ihn mit
dem SHA256 des jüngsten lokalen Backups. Volatile Felder
(`<revision>`, `<lastchange>`, Whitespace) werden vor dem Hash gestrippt.

**Karten-Badge:** orange Marker mit Tooltip „Drift erkannt gegen Backup
vom YYYY-MM-DD …". Ein Klick öffnet das Backups-Tab.

False-Positive nach eigenem Apply ist seit v0.8 ausgeschlossen: das
Post-Apply-Backup wird zur neuen Baseline.

---

## Auto-Retry für Mobile-Racks

Use-Case: Apply auf 25 Boxen, 3 sind gerade offline. Statt manuell
stundenlang zu warten:

- `auto_retry_enabled = True` im Tresor (Default).
- Nach jedem Apply wandern FAILED-Geräte automatisch in den
  RetryWatcher (in-process Daemon-Thread).
- Watcher probiert alle `auto_retry_interval_minutes` Minuten neu;
  Jobs laufen bis `auto_retry_max_hours` (Default 168h = 7 Tage).

**Persistenz seit v0.8:** Queue liegt in
`<app_data>/state/retry-queue.json` und überlebt Server-Restart UND
Session-Lock. Beim Lock werden Jobs auf Orphan-Status gesetzt
(`session_token=""`); beim nächsten Vault-Unlock adoptiert der Watcher
sie über den `vault_path` und übernimmt das neue Token. Damit gehen
keine armed Retries durch ein `apt update` oder Auto-Lock verloren.

**UI:** Auf den Karten erscheint ein **Amber „N offen"-Badge**, das die
Anzahl offener Aktionen zeigt. Klick lädt den jüngsten betroffenen
Plan, vorausgewählt auf das eine Gerät — für manuelles Nachziehen.

---

## Safety-Net via SSH

Cisco-Style commit-confirmed: nach einem Apply hat man X Sekunden
zum **Bestätigen**, sonst rollt Cockpit per SSH auf das Pre-Apply-
Backup zurück. Greift den Fall ab, dass ein Apply die eigene Cockpit-
Sicht auf die Box kappt (Filter-Regel, Interface-Down, Routing-Fehler).

### Setup

Pro Gerät, das das Feature nutzen soll, in vier Schritten:

#### Schritt 1: SSH auf der OPNsense aktivieren

OPNsense → **System → Settings → Administration**, im Block *Secure
Shell*:

- ☑ **Enable Secure Shell**
- ☑ **Permit root user login** *(falls du `root` für den Rollback nutzen
  willst — siehe Schritt 2)*
- ☑ **Permit password login** kurz angehakt lassen, bis der Key getestet
  ist. **Nach erfolgreichem Test wieder ABHAKEN** — Public-Key-Auth ist
  sicherer als Passwort-Auth.
- **Listen Interfaces**: am besten ein MGMT-Interface, nicht WAN.
- **Save** unten.

Der SSH-User braucht Schreibrechte auf `/conf/config.xml` und darf
`configctl` ausführen. **`root` erfüllt beides out of the box** und ist
die einfachste Wahl. Wer einen separaten User will, muss
ihm in *System → Access → Users* die Gruppe `wheel` zuweisen — sonst
scheitert der Rollback an Rechtemangel.

#### Schritt 2: SSH-Key-Paar generieren

Ein Key-Paar besteht aus zwei Dateien:

- **Privater Key** (geht in den Cockpit-Tresor)
- **Öffentlicher Key** (`.pub`, geht in die OPNsense)

##### Windows (PowerShell)

```powershell
# Arbeitsverzeichnis (Beispiel)
cd "$env:USERPROFILE\Documents"

# Key erzeugen — Algorithmus Ed25519 (klein, schnell, modern)
ssh-keygen -t ed25519 -f opn-cockpit-rollback -C "opn-cockpit-safety-net"
```

`ssh-keygen` fragt nach einer Passphrase — **leer lassen** (Enter
drücken). Cockpit muss den Key headless im Hintergrund-Thread benutzen;
mit Passphrase würde der Rollback scheitern.

Danach liegen zwei Dateien:

| Datei | Inhalt | Wo hin |
|---|---|---|
| `opn-cockpit-rollback` | Privater Key (`-----BEGIN OPENSSH PRIVATE KEY-----`) | In den Cockpit-Tresor (Schritt 4) |
| `opn-cockpit-rollback.pub` | Public-Key (`ssh-ed25519 AAAA…`) | In die OPNsense (Schritt 3) |

> Falls `ssh-keygen` nicht gefunden wird: Windows 10/11 hat seit Version
> 1809 einen OpenSSH-Client. Aktivieren via
> **Einstellungen → Apps → Optionale Features → "OpenSSH-Client" hinzufügen**,
> oder per PowerShell als Admin:
> ```powershell
> Add-WindowsCapability -Online -Name OpenSSH.Client~~~~0.0.1.0
> ```

##### Linux / macOS

```bash
ssh-keygen -t ed25519 -f ~/opn-cockpit-rollback -C "opn-cockpit-safety-net"
```

(Passphrase wieder leer lassen.)

#### Schritt 3: Public-Key in die OPNsense legen

1. **Inhalt der `.pub`-Datei in die Zwischenablage kopieren:**

   PowerShell:
   ```powershell
   Get-Content opn-cockpit-rollback.pub | Set-Clipboard
   ```
   Linux/macOS:
   ```bash
   cat ~/opn-cockpit-rollback.pub | xclip -selection clipboard   # X11
   cat ~/opn-cockpit-rollback.pub | pbcopy                       # macOS
   ```

   Der String beginnt mit `ssh-ed25519 AAAA…` und ist eine Zeile.

2. **OPNsense-Web-UI → System → Access → Users.**
3. Den User aufmachen, mit dem der Rollback laufen soll (z. B. `root`).
4. Scrollen zum Block **"authorized keys"** — Textbox. **Den
   kompletten `.pub`-Inhalt** dort einfügen (an bestehende Keys
   anhängen, nicht überschreiben — jede Zeile = ein Key).
5. **Save** unten.

#### Schritt 4: Key in Cockpit hinterlegen

1. **Privaten Key in die Zwischenablage kopieren** — KOMPLETT, inklusive
   `-----BEGIN OPENSSH PRIVATE KEY-----` und `-----END OPENSSH PRIVATE KEY-----`.

   PowerShell:
   ```powershell
   Get-Content opn-cockpit-rollback | Set-Clipboard
   ```
   Linux/macOS:
   ```bash
   cat ~/opn-cockpit-rollback | xclip -selection clipboard   # X11
   cat ~/opn-cockpit-rollback | pbcopy                       # macOS
   ```

2. In Cockpit: **Karte → "Bearbeiten"** → unten scrollen bis zum Block
   *Safety-Net via SSH aktivieren*.

3. Felder ausfüllen:

   | Feld | Wert |
   |---|---|
   | ☑ **Safety-Net via SSH aktivieren** | Häkchen setzen |
   | **SSH-Host** | leer = wie API-Host (Default); explizit nur wenn die OPNsense SSH auf einer anderen Adresse/FQDN hört als die API |
   | **SSH-Port** | Default `22` |
   | **SSH-User** | z. B. `root` |
   | **SSH-Private-Key (PEM)** | privaten Key aus der Zwischenablage einfügen — **mit** den `-----BEGIN ...-----` / `-----END ...-----` Zeilen |

4. **Speichern** im Modal-Footer.

5. Karte wieder öffnen → der Hint unter dem Key-Feld sagt **"Key ist im
   Tresor hinterlegt — leer lassen = unverändert."** → Setup ist durch.

#### Schritt 5: Vorab-Test (empfohlen)

Bevor du den Key produktiv einsetzt, prüfe ihn von Hand:

```powershell
# Funktioniert der Login per Key?
ssh -i opn-cockpit-rollback -p 22 root@<opnsense-host> "echo OK; uname -a"
```

Erwartete Ausgabe:
```
OK
FreeBSD opnsense 14.2-RELEASE-p3 ...
```

- Wenn das funktioniert: Cockpit kann's auch. Public-Key-Auth in OPNsense
  jetzt erzwingen (Schritt 1, "Permit password login" abhaken).
- Wenn nicht: ssh-Output gibt meist die Ursache (Permission denied →
  Public-Key nicht oder falsch eingetragen; Connection refused →
  SSH-Service aus oder falscher Port).

#### Schritt 6: Privaten Key sichern + lokal löschen

Der Key liegt ab jetzt verschlüsselt im **`.opnvault`**-Tresor. Die
lokale Datei `opn-cockpit-rollback` brauchst du nicht mehr — bewahre
sie nur in einem **Passwort-Safe** auf (KeePass, 1Password, …) und
lösche die lokale Klartext-Kopie:

```powershell
Remove-Item opn-cockpit-rollback, opn-cockpit-rollback.pub
```

Wer den Tresor weitergibt, gibt damit auch den SSH-Zugang auf die OPNsense
weiter — der Tresor-Master-Passwort-Schutz ist der einzige Gate.

### Akzeptierte Key-Formate

`paramiko` versucht der Reihe nach: **Ed25519, ECDSA, RSA, DSA**.
Empfehlung: Ed25519 (klein, schnell, modern).

### Apply mit Sicherheitsnetz

1. Plan erzeugen (Route/Alias/Regel/DNS — egal welches Subsystem) →
   Vorschau.
2. Direkt unter der Confirm-Checkbox erscheint die Box **„Mit
   Sicherheitsnetz ausrollen"** — nur sichtbar wenn mindestens ein
   Ziel-Gerät SSH konfiguriert hat. Häkchen setzen.
3. „Aktivieren" — Apply läuft normal. Nach Verify zeigt das Modal
   einen **roten Banner mit Countdown** (Default 120 s; per
   `safety_net_window_s` in den Tresor-Einstellungen anpassbar).
4. Zwei Knöpfe:
   - **„Bestätigen (alle)"** — alle armed Entries auflösen, Apply
     bleibt aktiv.
   - **„Sofort verwerfen (Rollback jetzt)"** — sofortiger SSH-Rollback
     ohne auf den Countdown zu warten.
5. Ohne Klick: bei Deadline-Hit greift der Watcher von selbst.

### Was beim Auto-Rollback passiert

1. `paramiko` verbindet sich per Private-Key auf SSH-Host/Port.
2. Aktuelle `/conf/config.xml` wird nach
   `/conf/config.xml.opncockpit-before-restore` kopiert (Forensik).
3. Pre-Apply-XML wird per SFTP nach `/conf/config.xml` geschrieben.
4. Reload-Sequenz: `configctl webgui restart renew; configctl filter
   reload; configctl interface reconfigure; configctl service reload all`.
5. Audit-Eintrag mit Trigger (`deadline` / `abort`) und Resultat.

### Troubleshooting

| Symptom | Wahrscheinliche Ursache |
|---|---|
| Checkbox „Mit Sicherheitsnetz" nicht sichtbar | Kein Ziel-Gerät hat `ssh_enabled` UND einen Private-Key im Tresor |
| Rollback meldet „Authentifizierung fehlgeschlagen" | Public-Key nicht in `~/.ssh/authorized_keys` des SSH-Users / falscher User-Name in Cockpit |
| Rollback meldet „SSH-Private-Key nicht lesbar" | Format nicht erkannt — Ed25519/ECDSA/RSA/DSA als PEM erwartet, keine PuTTY-PPKs |
| `Remote-Befehl fehlgeschlagen (rc=N)` | SSH-User hat keine Schreibrechte auf `/conf/config.xml` oder darf `configctl` nicht ausführen — meist nur `root` reicht |
| Rollback startet nicht obwohl Deadline überschritten | SafetyNetWatcher ist nicht persistent — bei Server-Restart fallen armed Entries aus. Bestätigen oder neu starten |

### Sicherheitshinweise

- SSH-Private-Keys werden **verschlüsselt** im Vault gespeichert
  (Argon2id + AES-256-GCM, gleicher Schutz wie API-Secrets).
- Klartext-Key wird nach `client.connect()` sofort wieder freigegeben
  (`del private_key` im `finally`-Block), nicht in Tracebacks.
- Password-Auth ist bewusst **nicht** implementiert — wer keinen
  Key hinterlegen will, hat kein Safety-Net.
- Host-Key-Verifikation läuft im Trust-on-first-use-Modus
  (`AutoAddPolicy`); MitM-Schutz für Recovery-SSH ist kein realistisches
  Bedrohungsmodell für ein LAN-Management-Tool.

---

## Audit-Log + signierter PDF-Export

**Öffnen:** Topbar → Drei-Linien-Icon.

**Filter:** Event-Kind (Dropdown mit allen `AuditEventKind`-Werten),
Action (Freitext-Substring), Geräte-ID (Freitext-Substring).

**Integrität prüfen** ruft die HMAC-Hash-Chain-Verifikation (nur
sinnvoll im SQLite-Backend, das Default im Server-Mode). Liefert
Total + Anzahl geprüfter Eintraege + Liste der „broken" Indices
(Tampering-Verdacht).

**Export:**
- **„Als CSV exportieren"** — alle gefilterten Records als CSV
  (`opn-cockpit-audit.csv`).
- **„Als PDF (signiert)"** — A4-Querformat-Report mit:
  - Header: Erstellt-Zeit, Erstellt von, Filter-Zusammenfassung,
    Eintragszahl
  - Tabelle: Zeit (UTC), Akteur, Event, Zusammenfassung
  - Footer mit zwei Hex-Werten:
    - `SHA256 (Inhalt)` — Reiner Hash über die Records (ohne Secret)
    - `HMAC (Cockpit)` — HMAC-SHA256 mit dem Audit-Chain-Secret
  - Signatur landet zusätzlich in den PDF-Metadaten als
    `Keywords: OPN-COCKPIT-AUDIT-SIG-v1:<hex>` — maschinell auslesbar.

**Verifikation:** Das Render-Format ist deterministisch (Real-Time-
Daten nur in Header + Metadata, nicht in den signierten Bytes).
`audit.pdf_report.verify_pdf_signature(records, expected_sig, secret)`
liefert konstantzeit-bool. Wer das Cockpit-Audit-Secret kennt
(`load_or_generate_secret`), kann den Report reproduzieren.

---

## Inline-Validierung

Felder mit `data-validate="<key>"` bekommen Live-Validierung beim
Tippen/Blur. Falsche Werte zeigen einen roten Border + kleinen
Hint unter dem Feld. Leere Felder sind immer OK (der Submit-Pfad
prüft Pflichtfelder).

Validatoren:

| Key | Regel |
|---|---|
| `cidr` | IPv4-CIDR, Host-Bits müssen 0 sein (z. B. `10.0.0.0/24`) |
| `ipv4` | Oktette 0-255 |
| `host` | FQDN oder IPv4, nur a-z/A-Z/0-9/`-`/`.` |
| `aliasName` | Buchstabe am Anfang, dann a-z/A-Z/0-9/`_`, max 32 |
| `gatewayName` | analog `aliasName` |
| `port` | Zahl 1-65535, Range mit `-` / `:`, „any", oder Alias-Name |

System ist erweiterbar: neue Felder brauchen nur das `data-validate`-
Attribut und werden vom `setupInlineValidators()`-Bootstrap automatisch
eingebunden. Server-Validierung bleibt Defense-in-Depth.
