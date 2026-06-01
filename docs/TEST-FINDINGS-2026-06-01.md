# Test-Findings 2026-06-01 (Arbeitslaptop, frische Installation)

Erste Testrunde durch den User auf einem Arbeitslaptop (Konto `Dell`,
nicht-elevated). Installation per `Install-OPN-Cockpit-0.6.0.exe` lief
durch.

## Status-Legende

| Symbol | Bedeutung |
|---|---|
| 🔴 | Bug — falsches Verhalten / Crash |
| 🟡 | UX-Glitch — funktioniert, sieht/fühlt sich aber unsauber an |
| 🔵 | Feature-Lücke / nice-to-have |
| 🟢 | gefixt im Lauf dieser Session |

## Funde

### F1 🟢 Vault-Anlage scheitert wenn Ziel-Verzeichnis fehlt

**Beobachtet**: Speicherort `C:\Users\Dell\Desktop\OPN-Cockpit\` getippt,
Ordner existierte nicht → rote Fehlerbox:
`Tresor-Datei nicht schreibbar: ... ([Errno 2] No such file or directory:
'...\TEST.opnvault.tmp')`.

**Ursache**: `vault/store.py::_atomic_write` öffnet die `.tmp`-Datei
direkt, ohne den Eltern-Ordner zu erzeugen.

**Fix**: Parent-Ordner per `mkdir(parents=True, exist_ok=True)` anlegen,
bevor das `.tmp` geschrieben wird. Sicher, weil der Pfad bereits den
`web/vault_path.py`-Validator passiert hat (also unterhalb einer
erlaubten Basis liegt). Fix in dieser Session.

### F2 🟡 Single-User: User-Verwaltung sollte ausgeblendet sein

**Beobachtet**: Im UI gibt es Menüpunkte / Optionen für User-Verwaltung,
obwohl es im Single-User-Modus keine Mehrbenutzer gibt.

**Vorschlag**: User-Verwaltung nur sichtbar wenn Multi-User-Server-Modus
(NSSM-Service) aktiv. Sonst komplett ausblenden.

### F3 🟡 Menü "Eigenes Passwort ändern" missverständlich benannt

**Beobachtet**: Punkt "Eigenes Passwort ändern" — gemeint ist
wahrscheinlich das Tresor-Master-Passwort, nicht ein User-Passwort
(welches es im Single-User-Modus nicht gibt).

**Vorschlag**: Single-User-Modus → Label "Tresor-Passwort ändern".
Multi-User-Modus → bleibt "Eigenes Passwort ändern".

### F4 🔵 Bulk-Import: Beispiel-Dateien zum Download

**Beobachtet**: Bulk-Import bietet CSV und JSON, aber keine
Beispiel-Datei.

**Vorschlag**: Im Bulk-Import-Dialog "Beispiel-CSV herunterladen" +
"Beispiel-JSON herunterladen"-Buttons, die ein gültiges Mini-Schema
mit Kommentaren ausliefern.

### F5 🔵 Auto-Sperre-Timeout in der UI nicht editierbar

**Beobachtet**: Default 10 Minuten Inaktivität → Sperre. Keine UI-Option
gefunden, das z. B. auf 30 Minuten zu setzen.

**Status**: Der Wert ist im Tresor-Settings-Objekt verankert (siehe
Spec Schritt 4: "anpassbar via Tresor-Settings"). UI-Anbindung fehlt.

**Vorschlag**: Settings-Modal mit Feld "Inaktivitäts-Timeout (Minuten)"
plus Validation (1..240).

### F6 🟡 Bild 4: Leeres Element unter Tresor-Name im Header

**Beobachtet**: Auf der Inventar-Seite, direkt unter `OPN-Cockpit` +
`TEST.OPNVAULT`-Badge ist ein flaches weißes Element sichtbar, das aus
wie ein zusammengeschrumpftes Modal/Kachel aussieht.

**Verdacht**: Vermutlich das Search-Input ohne Inhalt oder ein leerer
Status-Badge-Container.

**Aktion**: Anhand der index.html prüfen, was an dieser Stelle gerendert
wird.

### F7 🔵 Design-Guide festschreiben

**Beobachtet**: Nach Prompt-Compacts schweift Claude beim Bauen neuer
Features in die Designsprache ab, was später aufwendiges Redesign
verursacht hat.

**Auftrag**: Aus dem aktuellen v0.6.0-Stand einen Design-Guide ableiten
(Farben, Typografie, Spacing-Skala, Komponenten-Patterns: Modals,
Buttons, Form-Felder, Tabs, Badges). Verbindlich für alle künftigen
UI-Arbeiten.

### F8 🔵 Icon / Logo / Favicon

**Beobachtet**: Aktuell kein Favicon, kein Logo im Header.

**Auftrag**: Icon entwerfen, das zur Calm-Precision-Ästhetik passt
(Olive-Akzent, geometrisch ruhig). Favicon + Header-Variante.

---

## Modal: Route hinzufügen

### F9 🟡 Netzwerk + Gateway vertikal unsauber ausgerichtet

**Beobachtet**: Die zwei Spalten "Netzwerk (CIDR)" und "Gateway-Name"
sind nicht oben bündig — Gateway scheint nach unten ausgerichtet, weil
darunter noch der "Vorschläge laden"-Link sitzt.

**Fix**: Grid-Items mit `align-items: start` ausrichten, statt
implizitem `stretch`/`end`.

### F10 🟡 Inkonsistenter Abstand der "Aktion wird auf X von Y…"-Box

**Beobachtet**: Die Info-Box am Modal-Boden hat einen anderen Abstand
zur Trennlinie als andere Sektionen.

**Fix**: Spacing-Token vereinheitlichen.

### F11 🔴 Button-State nach Validation-Fehler kaputt

**Beobachtet**: Bei ungültiger CIDR (z. B. `/36`) erscheint korrekt eine
Fehlermeldung, **aber**:
- Der Button "Vorschau anzeigen" verschwindet
- Stattdessen erscheint "Aktivieren"
- "Aktivieren" funktioniert natürlich nicht (kein gültiger Plan da)

**Erwartetes Verhalten**: Validation-Fehler darf den Button-State nicht
von "Vorschau" auf "Aktivieren" wechseln. Erst nach erfolgreicher
Vorschau soll der Apply-Button aktiv werden.

### F12 🟡 Modal schließt bei Klick außerhalb → Eingaben futsch

**Beobachtet**: Klick neben das Modal schließt es und löscht alle
Eingaben. Schon 2x in 5 Minuten passiert.

**Fix**: Bei Modals mit Eingabe-Pflicht den Backdrop-Click deaktivieren.
Schließen nur via X-Button, "Abbrechen"-Button oder ESC. Bei rein
informativen Modals (z. B. About) darf Backdrop-Click weiter schließen.

---

## Modal: Alias hinzufügen

### F13 🟡 Alias-Name + Typ vertikal unsauber (siehe F9)

Selbes Problem wie bei Route.

### F14 🟡 Inkonsistenter Abstand "Aktion wird ausgerollt"-Box (siehe F10)

Selbes Problem.

### F15 🔴 Button-State nach Validation-Fehler kaputt (siehe F11)

Selbes Problem.

### F16 🟡 Modal schließt bei Klick außerhalb (siehe F12)

Selbes Problem.

### F17 🟡 Typ wird beim Auswählen aus Suggestions nicht übernommen

**Beobachtet**: User wählt einen vorhandenen Alias-Namen aus der
Suggestion-Liste (z. B. Network-Alias), aber das Typ-Dropdown bleibt
auf "host" (Default) stehen → Validation-Fehler beim Vorschau-Klick.

**Fix**: Beim Suggestion-Click den passenden Typ aus dem Suggestion-
Datensatz mit übernehmen.

### F18 🔴 Alias-Append-Bug: Apply erfolgreich, aber Eintrag fehlt

**Beobachtet**:
1. Alias-Namen aus Suggestion-Liste gewählt
2. Typ auf "Network" gesetzt
3. `1.2.3.4/32` als Inhalt
4. Checkbox "An bestehenden Alias anhängen" aktiviert
5. Vorschau → bestätigt → Apply

**Ergebnis**: Result-Matrix zeigt Erfolg, aber der Eintrag taucht im
Alias auf der OPNsense nicht auf.

**Reproduktion nötig**: Aktion erneut durchspielen, dabei mitlesen:
- Audit-Log: Was steht im `apply`-Event?
- Browser DevTools: Welcher Payload ging an `/api/aliases/apply`?
- OPNsense direkt: ist der Inhalt wirklich nicht drin, oder wurde
  möglicherweise ein anderer Alias mit ähnlichem Namen geändert?
- Konsole des Cockpit-Servers: hat der Read-back stattgefunden?

**Hypothesen**:
1. Append-Logik mergt Inhalte in einen anderen Eintrag (Casing-Bug?)
2. Reconfigure wurde nicht ausgelöst (Schreibvorgang ohne Activate)
3. Verify hat falsche Daten als „erfolgreich" gewertet
4. Race-Condition: Verify lief gegen die alte Version (kein Cache-Bust
   in der OPNsense-API)

Priorität hoch — das ist ein Vertrauens-Brecher (Tool sagt "fertig",
Realität nicht).

---

## Empfohlene Bearbeitungs-Reihenfolge (für morgen)

**Tonight** (User-bestätigt):
- F1 ✅ Vault-Parent-mkdir

**Sprint 1 — Quick UX-Wins (ca. 30–60 min)**:
- F12/F16 Backdrop-Click off
- F9/F13 Grid-Alignment
- F10/F14 Spacing
- F11/F15 Button-State
- F17 Auto-Typ aus Suggestion
- F3 Label-Umbenennung "Tresor-Passwort"
- F6 Header-Element identifizieren + fixen

**Sprint 2 — Functional (ca. 60–120 min)**:
- F18 Alias-Append-Bug debuggen (am wichtigsten!)
- F5 Settings-Modal mit Inaktivitäts-Timeout
- F2 Single-User: User-Menü ausblenden
- F4 Bulk-Import-Beispiele

**Sprint 3 — Strategisch (separat)**:
- F7 Design-Guide schreiben
- F8 Logo + Favicon
