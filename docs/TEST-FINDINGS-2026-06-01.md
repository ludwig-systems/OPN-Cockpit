# Test-Findings 2026-06-01 (Arbeitslaptop, frische Installation)

Erste Testrunde durch den User auf einem Arbeitslaptop (Konto `Dell`,
nicht-elevated). Installation per `Install-OPN-Cockpit-0.6.0.exe` lief
durch.

## Status-Legende

| Symbol | Bedeutung |
|---|---|
| 🔴 | Bug — falsches Verhalten / Crash, offen |
| 🟡 | UX-Glitch — funktioniert aber unsauber, offen |
| 🔵 | Feature-Lücke / nice-to-have, offen |
| ✅ | erledigt — siehe **Resolution** im Item |

**Konvention**: Jedes Item bekommt am Ende einen `**Resolution**:`-Block
mit Datum, Commit-SHA und 1–2 Sätzen WIE es gefixt wurde, sobald es
erledigt ist. Symbol wechselt auf ✅. So sieht man auf einen Blick was
noch offen ist.

## Funde

### F1 ✅ Vault-Anlage scheitert wenn Ziel-Verzeichnis fehlt

**Beobachtet**: Speicherort `C:\Users\Dell\Desktop\OPN-Cockpit\` getippt,
Ordner existierte nicht → rote Fehlerbox:
`Tresor-Datei nicht schreibbar: ... ([Errno 2] No such file or directory:
'...\TEST.opnvault.tmp')`.

**Ursache**: `vault/store.py::_atomic_write` öffnet die `.tmp`-Datei
direkt, ohne den Eltern-Ordner zu erzeugen.

**Resolution** (2026-06-01, `f905ac4`): `_atomic_write` legt vor dem
`.tmp`-Schreiben `path.parent.mkdir(parents=True, exist_ok=True)` an —
sicher, weil der Pfad durch `web/vault_path.py` schon auf erlaubte
Basen (Home/Documents/Desktop/AppData) eingeschränkt ist. Regression-
Test `test_creates_missing_parent_directory`.

### F2 ✅ Single-User: User-Verwaltung sollte ausgeblendet sein

**Beobachtet**: Im UI gibt es Menüpunkte / Optionen für User-Verwaltung,
obwohl es im Single-User-Modus keine Mehrbenutzer gibt.

**Resolution** (2026-06-01, `9e8bc8d`): Indirekt gelöst durch F6 —
der `[hidden] { display:none !important }`-Sammel-Fix lässt den
`users-open-btn` und `password-self-btn` im Single-User-Mode endlich
korrekt verschwinden. JS setzte `hidden=true` schon vorher korrekt,
nur die CSS-Override durch `display: inline-flex` blockierte es.

### F3 ✅ Menü "Eigenes Passwort ändern" missverständlich benannt

**Beobachtet**: Punkt "Eigenes Passwort ändern" — gemeint ist
wahrscheinlich das Tresor-Master-Passwort, nicht ein User-Passwort
(welches es im Single-User-Modus nicht gibt).

**Resolution** (2026-06-01, Runde 2): Im Single-User-Modus ist der
`pwself-btn` korrekt versteckt (war F6-Override-Bug, jetzt gefixt).
Stattdessen jetzt **Tresor-Einstellungs-Modal** mit eigener Sektion
"Master-Passwort ändern" (siehe F5a). Multi-User-Modus behält
"Eigenes Passwort ändern" als User-Passwort.

### F4 ✅ Bulk-Import: Beispiel-Dateien zum Download

**Beobachtet**: Bulk-Import bietet CSV und JSON, aber keine
Beispiel-Datei.

**Resolution** (2026-06-01, Runde 2): Zwei neue anonyme Endpoints
`GET /api/imports/examples/devices.csv` und `.json` liefern eine
befüllte Vorlage mit Kommentaren als Download. Im Bulk-Modal sind
sie als „Beispiel-CSV / Beispiel-JSON"-Links unter dem File-Input
angeklickbar. Tests: `test_example_devices_csv_serves_template`
und `_json_serves_template`.

### F5a ✅ Vault-Master-Passwort ändern in der UI

**Beobachtet** (Runde 2): Keine Möglichkeit das Vault-Master-Passwort
über die Web-UI zu ändern — nur via CLI. Im Single-User-Mode also
de facto blockiert.

**Resolution** (2026-06-01, Runde 2): Neues Settings-Modal (Zahnrad-
Icon in der Top-Bar) mit Sektion „Master-Passwort ändern". Backend:
`POST /api/vaults/change-password` ruft `vault.store.change_password`
auf, aktualisiert die Session unter dem neuen Passwort (kein
Re-Login nötig). Validation: aktuelles Passwort als Bestätigung
verlangt, neues min. 12 Zeichen, beide Eingaben müssen identisch
sein, neu ≠ alt.

### F5b ✅ Auto-Sperre-Timeout in der UI nicht editierbar

**Beobachtet**: Default 10 Minuten Inaktivität → Sperre. Keine UI-Option
gefunden, das z. B. auf 30 Minuten zu setzen.

**Resolution** (2026-06-01, Runde 2): Im selben Settings-Modal Sektion
„Auto-Sperre" mit Number-Input (1–240 Minuten). Backend:
`GET/POST /api/vaults/settings` liest/schreibt `inactivity_minutes`
in die Tresor-Settings und persistiert. Greift sofort für die
laufende Session (`Session.inactivity_timeout_s` ist computed property).

### F6 ✅ Bild 4: Leeres Element unter Tresor-Name im Header

**Beobachtet**: Auf der Inventar-Seite, direkt unter `OPN-Cockpit` +
`TEST.OPNVAULT`-Badge ist ein flaches weißes Element sichtbar, das aus
wie ein zusammengeschrumpftes Modal/Kachel aussieht.

**Resolution** (2026-06-01, `9e8bc8d`): Ursache war `<span class=
"user-badge" hidden></span>` im Brand-Block. Die `.user-badge`-CSS-
Regel definiert `display: inline-flex` — gewinnt gegen das User-Agent-
`[hidden] { display: none }` (gleiche Spezifizität, Autor-CSS sticht
User-Agent). Mit `background: var(--bg-elevated)` blieb eine leere
weiße Pille sichtbar. Fix: globale Regel `[hidden] { display: none
!important }` direkt nach den `:root`-Tokens — wirkt auch für alle
`.icon-btn[hidden]` (F2 nebenbei mit erschlagen).

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

### F9 ✅ Netzwerk + Gateway vertikal unsauber ausgerichtet

**Beobachtet**: Die zwei Spalten "Netzwerk (CIDR)" und "Gateway-Name"
sind nicht oben bündig — Gateway scheint nach unten ausgerichtet, weil
darunter noch der "Vorschläge laden"-Link sitzt.

**Resolution** (2026-06-01, `9e8bc8d`): `.form-row { align-items:
flex-end }` → `flex-start`. Wirkt global für alle form-rows, da das
Pattern in allen Modals identisch ist.

### F10 ✅ Inkonsistenter Abstand der "Aktion wird auf X von Y…"-Box

**Beobachtet**: Die Info-Box am Modal-Boden hat einen anderen Abstand
zur Trennlinie als andere Sektionen.

**Resolution** (2026-06-01, `9e8bc8d`): `.selection-summary` mit
expliziter `margin-top: 12px` versehen, deckt die zu enge 4px-
Bottom-Margin des `form-divider` davor ab.

### F11 ✅ Button-State nach Validation-Fehler kaputt

**Beobachtet**: Bei ungültiger CIDR (z. B. `/36`) erscheint korrekt eine
Fehlermeldung, **aber**:
- Der Button "Vorschau anzeigen" verschwindet
- Stattdessen erscheint "Aktivieren"
- "Aktivieren" funktioniert natürlich nicht (kein gültiger Plan da)

**Resolution** (2026-06-01, `9e8bc8d`): `app.js` `submitPlanInput()`
hatte `next.textContent = 'Aktivieren'` im `finally` — feuerte auch
im Validation-Fehler-Pfad. Jetzt: `finally` setzt nur `disabled = false`,
und falls `planPhase === 'input'` (Fehler-Fall) den Text auf
`Vorschau anzeigen` zurück. `showPlanPhase('preview')` setzt ihn im
Erfolgsfall korrekt auf `Aktivieren`.

### F12 ✅ Modal schließt bei Klick außerhalb → Eingaben futsch

**Beobachtet**: Klick neben das Modal schließt es und löscht alle
Eingaben. Schon 2x in 5 Minuten passiert.

**Resolution** (2026-06-01, `9e8bc8d`): Backdrop-Click bei Eingabe-
Modals abgeschaltet (`plan-modal` außer in `result`-Phase, `add-modal`,
`bulk-modal`, `pwself-modal`, `vault-settings-modal`). Audit/About/
Device bleiben backdrop-schließbar (read-only).

---

## Modal: Alias hinzufügen

### F13 ✅ Alias-Name + Typ vertikal unsauber (siehe F9)

Mitbehoben durch F9 (`flex-start` ist global). `9e8bc8d`.

### F14 ✅ Inkonsistenter Abstand "Aktion wird ausgerollt"-Box (siehe F10)

Mitbehoben durch F10 (`.selection-summary margin-top`). `9e8bc8d`.

### F15 ✅ Button-State nach Validation-Fehler kaputt (siehe F11)

Mitbehoben durch F11 (selber Code-Pfad für Route + Alias). `9e8bc8d`.

### F16 ✅ Modal schließt bei Klick außerhalb (siehe F12)

Mitbehoben durch F12. `9e8bc8d`.

### F17 ✅ Typ wird beim Auswählen aus Suggestions nicht übernommen

**Beobachtet**: User wählt einen vorhandenen Alias-Namen aus der
Suggestion-Liste (z. B. Network-Alias), aber das Typ-Dropdown bleibt
auf "host" (Default) stehen → Validation-Fehler beim Vorschau-Klick.

**Resolution** (2026-06-01, `9e8bc8d`): `loadAliasSuggestions`
befüllt zusätzlich eine `aliasSuggestionTypes`-Map. Neue Funktion
`syncAliasTypeFromSuggestion` hört auf `input`/`change` von
`#pl-alias-name` und setzt das Typ-Dropdown, wenn der eingegebene
Name exakt einer Suggestion entspricht.

### F18 ✅ Alias-Append-Bug: Apply erfolgreich, aber Eintrag fehlt

**Beobachtet**:
1. Alias-Namen aus Suggestion-Liste gewählt
2. Typ auf "Network" gesetzt
3. `1.2.3.4/32` als Inhalt
4. Checkbox "An bestehenden Alias anhängen" aktiviert
5. Vorschau → bestätigt → Apply

**Ergebnis**: Result-Matrix zeigt Erfolg, aber der Eintrag taucht im
Alias auf der OPNsense nicht auf.

**Resolution** (2026-06-01, zwei Stufen):

1. **`9e8bc8d`** — Erste Hälfte: OPNsense liefert bei Validation-
   Fehlern HTTP 200 mit `{"result":"failed","validations":{...}}`.
   Vorher wurde das als Erfolg gewertet. Neue Helper
   `_raise_if_not_saved` (Aliase) bzw. Inline-Block (Routen) prüft
   `result`-Feld und wirft `ApiError` mit Validations-Details.
   Dadurch schaltet die Matrix korrekt auf FEHLGESCHLAGEN um.

2. **Runde 2** — Zweite Hälfte (die eigentliche Ursache warum der
   OPNsense-Save überhaupt failed): `_row_to_spec` parste das
   `type`-Feld aus `getItem` als `str(dict)`, weil OPNsense Select-
   Felder als `{key: {value:..., selected:0|1}}` zurückgibt. Im
   Append-Pfad wurde so der Typ als Müll-String an `setItem`
   gesendet → OPNsense lehnte ab. Neue Helper `_selected_key` und
   erweiterte `_content_from_api` extrahieren den `selected: 1`-
   Key korrekt. Tests:
   `test_append_decodes_opnsense_select_type_dict`.

   Außerdem: `ApiError.context.summary` wird jetzt mit dem OPNsense-
   Validations-String befüllt, damit die Result-Matrix und das
   Audit-Log den OPNsense-Originalsatz zeigen (vorher nur der
   Default „Schreibvorgang fehlgeschlagen.").

---

## Zusätzliche Befunde aus Test-Runde 2

### F19 ✅ Datalist-Dropdown nach Auswahl unbrauchbar

**Beobachtet** (Runde 2): Im Alias-Modal „Vorschläge laden" → Eintrag
ausgewählt → das Datalist-Dropdown zeigt anschließend keine anderen
Optionen mehr, bis der Input-Wert komplett gelöscht ist.

**Ursache**: Browser-Standard-Verhalten von `<datalist>`: wenn `input.
value` exakt einer Option entspricht, wird die Liste ausgeblendet
(„nichts mehr zum Vorschlagen").

**Resolution v1** (2026-06-01, Runde 2, Commit `d3a3f23`):
Suggestion-Chip-Liste unter dem Input. **Verworfen in Runde 3** —
sprengt das Modal sobald 20+ Aliase geladen sind.

**Resolution v2** (2026-06-01, Runde 3): Neuer Helper
`enableDatalistRebrowse(inputId, knownValuesGetter)`. Hängt
`focus`+`mousedown`-Listener an: wenn `input.value` exakt einer
Suggestion entspricht, wird der Wert temporär geleert (alter Wert
in `dataset.lastPick` gemerkt) → der Browser zeigt die volle Liste
wieder. `blur` ohne Auswahl restauriert den alten Wert. Aktiv für
Alias- und Gateway-Input. CSS-Chips-Regeln entfernt.

## Test-Runde 3 (Folgebefunde)

### F20 ✅ Settings-Save liefert Fehler 500 — Wert wird aber teilweise übernommen

**Beobachtet** (Runde 3): Timeout im Settings-Modal auf 60 setzen,
Speichern → rote Box „Fehler 500". Aber Footer-Anzeige bleibt bei
10 Min, **die Session-Restzeit-Anzeige hingegen läuft schon mit
~60 Minuten**.

**Ursache**: Mein `update_vault_settings`-Handler rief
`persist_session_vault(session)` mit falscher Signatur auf (echt ist
`persist_session_vault(request, session, vault_path, *, rollback=…)`).
Server crashte mit TypeError → 500. Vorher hatte ich aber schon
`session.opened.data.settings = new_settings` direkt mutiert — die
laufende Session sieht den neuen Wert, der File-Save kam nicht
zustande. Footer war ein statisch initialisiertes Feld aus
`sessionInfo.inactivity_timeout_s` und wurde nie aktualisiert.

**Resolution** (2026-06-01, Runde 3): Drei Fixes:
1. Korrekte `persist_session_vault(request, session, vault_path,
   rollback=…)`-Signatur. `request` als FastAPI-Dependency dazu.
2. Rollback-Closure stellt bei Save-Fehler die alten Settings in
   der Session wieder her — keine In-Memory/Platte-Drift mehr.
3. Frontend `saveInactivityTimeout` updated nach Erfolg auch
   `#timeout-display` und `state.sessionInfo.inactivity_timeout_s`.
Regression-Tests: `TestVaultSettingsAndChangePassword` mit 6 Cases
(get/update/range/change-pw/wrong-current/mismatch).

### F21 ✅ Settings-Icon visuell identisch mit Dark-Mode-Toggle

**Beobachtet** (Runde 3): Mein Zahnrad-SVG war ein Sonnen-Symbol —
gleich wie der Theme-Toggle daneben.

**Resolution** (2026-06-01, Runde 3): Neues SVG mit echten Zahnrad-
Zähnen (kurze Rechtecke an den 4 Kardinalpunkten zusätzlich zu den
diagonalen Strichen).

### F19v1 → F19v2 ✅ Suggestion-Chips entfernt — Modal sprengte ab ~20 Aliasen

**Beobachtet** (Runde 3): Die in Runde 2 hinzugefügten Suggestion-Chips
unter dem Alias-Input zeigten alle ~25 Aliase gleichzeitig — sprengte
das Modal vertikal.

**Resolution**: siehe F19 oben — Chips entfernt, Re-Browse jetzt per
`enableDatalistRebrowse`-Helper (Wert-Clear bei Re-Fokus auf bekannte
Auswahl, Restore bei Blur ohne Auswahl).

## Bearbeitungs-Reihenfolge — Stand 2026-06-01 (Runde 3)

**Erledigt** (✅ in den Items oben):

- **Runde 1** (`f905ac4`, `9e8bc8d`): F1, F2, F3, F6, F9/F13,
  F10/F14, F11/F15, F12/F16, F17, F18 (Stufe 1: result-Detection).
- **Runde 2** (`d3a3f23`): F4, F5a, F5b, F18 (Stufe 2: Type-Dict-
  Parsing + Summary-Detail), F19 v1.
- **Runde 3** (Commit folgt): F19 v2 (Chips weg, Re-Browse-Helper),
  F20 (Settings-500 + Footer-Refresh), F21 (Zahnrad-Icon).

**Noch offen**:

- F7 🔵 Design-Guide schreiben (verbindlich für künftige UI-Arbeit).
- F8 🔵 Logo / Favicon entwerfen.
