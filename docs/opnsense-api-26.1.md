# OPNsense REST-API — Notizen für Zielversion 26.1

> **Status:** TODO — wird mit Schritt 0 des Umsetzungsplans befüllt
> (30-min API-Spike gegen die laufende OPNsense-Test-Instanz).

Dieses Dokument hält die **tatsächlichen** Endpunkt-Pfade und Feldnamen fest,
die OPN-Cockpit gegen die eingesetzte OPNsense-Version anspricht. Es ist die
Single-Source-of-Truth, gegen die `src/opn_cockpit/core/objects/_endpoints.py`
geschrieben wird.

## Authentifizierung

- HTTP Basic Auth: `API-Key:Secret` (pro Gerät)
- Erzeugt unter *System → Access → Users → API Key*

## Endpunkte (zu verifizieren)

### Statische Routen

| Operation     | Methode | Pfad                                            | Notizen |
|---------------|---------|-------------------------------------------------|---------|
| Hinzufügen    | POST    | `/api/routes/routes/addroute`                   | Gateway via **Name** referenziert |
| Suchen        | GET/POST| `/api/routes/routes/searchroute`                | für Read-back-Verifikation |
| Aktivieren    | POST    | `/api/routes/routes/reconfigure`                | **einmal** pro Geräte-Rollout |

**Felder (`addroute`-Payload):**
- `network` (CIDR)
- `gateway` (Gateway-Name, case-sensitive — z. B. `V2_WANBwIn`)
- `descr` (Beschreibung)
- `disabled` (`0` / `1`)

### Firewall-Aliase

| Operation     | Methode | Pfad                                          | Notizen |
|---------------|---------|-----------------------------------------------|---------|
| Hinzufügen    | POST    | `/api/firewall/alias/addItem`                 | wirft Fehler bei Namens-Kollision (→ Merge separat) |
| Suchen / Get  | GET     | `/api/firewall/alias/searchItem` / `getItem`  | für Read-back |
| Aktualisieren | POST    | `/api/firewall/alias/setItem/{uuid}`          | für `append_to_alias`-Merge |
| Aktivieren    | POST    | `/api/firewall/alias/reconfigure`             | **einmal** pro Geräte-Rollout |

**Felder (`addItem`-Payload, hier `host` als Beispieltyp):**
- `name`
- `type` (`host`, `network`, `port`, `url`, …)
- `content` (Newline-separated bei Mehrfacheinträgen — **prüfen!**)
- `descr`

## Offene Punkte (zur Klärung im Spike)

- [ ] Genauer Wrapping-Stil der `add*`-Payloads (`{"route": {...}}` vs. flach)
- [ ] Merge-Semantik für Aliase: Liefert `getItem` die `content`-Liste in einem Format,
      das wir direkt erweitern und mit `setItem` zurückschreiben können?
- [ ] Erwartetes Response-Schema von `search*` (Pagination, Filter-Parameter)
- [ ] Status-Codes / Fehlerstruktur bei Validierungsfehlern (für sauberes
      Mapping nach `core.errors.ValidationError`)
- [ ] Verhalten von `reconfigure`, wenn keine offenen Änderungen existieren
      (Idempotenz)
- [ ] Health-Endpunkt für `R-DEV-3` (Verbindungstest) — leichtgewichtiger
      Read-Endpunkt, der Auth + Erreichbarkeit testet, ohne Last zu erzeugen

## Quellen

- OPNsense API-Doku: <https://docs.opnsense.org/development/api.html>
- Source der jeweiligen Module unter <https://github.com/opnsense/core> als
  Ground-Truth, wenn die Doku Lücken hat
