# OPN-Cockpit Design-Guide

Verbindliche Referenz für UI-Arbeit. Vor jeder neuen Komponente oder
Modal-Änderung diese Datei lesen — sonst driftet die Linie.

**Aktuelle Linie:** Calm-Precision · Bahnschrift-Display · Olive-Akzent.
**Vorbild für Mood:** Linear, Vercel Dashboard, klassische deutsche
DIN-Engineering-Tooling. **Nicht** Bootstrap-Material-Defaults.

## Architektur-Prinzipien

1. **Werkzeug-Ruhe vor Flash.** Stille Farbpalette mit einem einzigen
   Akzent (Olive). Animationen subtil und nur funktional.
2. **Information first, Chrome second.** Kachel-Inhalt zählt, Rahmen
   sind weich und zurückhaltend.
3. **Verifikation sichtbar machen.** Statusfarben (online/offline/checking/
   TLS-Risiko) sind die einzige Stelle wo Farbe schreit.
4. **Mono für Werte, Sans für Sprache, Display für Headlines.** Keine
   Vermischung — Tastatur-Shortcuts in Mono, Fließtext in Sans, INVENTAR-
   Überschrift in Display.
5. **System-Defaults respektieren.** Dunkelmodus opt-in via
   `prefers-color-scheme`, Light bleibt aber Default. Cursor, Scrollbars,
   Form-Controls bleiben System-nah.

## Tokens (CSS-Variablen)

Alle Farben kommen aus `:root` + `[data-theme='light/dark']` in
[`web/static/styles.css`](../src/opn_cockpit/web/static/styles.css). **Nie
Hex-Werte hardcoden**, immer Tokens verwenden.

### Akzent (das einzige farbige Element)

| Token | Light | Dark | Verwendung |
|---|---|---|---|
| `--accent` | `#586a26` | `#b5c97a` | Primary-Button-Fläche, Pulse-Dot, Olive-Strich |
| `--accent-strong` | `#455618` | `#cdde94` | Hover/Active, Chip-Text aktiv, starke Olive |
| `--accent-soft` | `rgba(88,106,38,.13)` | `rgba(181,201,122,.16)` | Soft-Background hinter Akzentlinien, Status-Pulse-Glow |
| `--accent-line` | `rgba(88,106,38,.55)` | `rgba(181,201,122,.6)` | Trennlinien wenn Olive gewollt ist |
| `--accent-text-on` | `#ffffff` | `#0d0f10` | Text auf einer Akzent-Fläche |

### Neutralen (Flächen, Text, Rahmen)

| Token | Light | Dark | Verwendung |
|---|---|---|---|
| `--bg` | `#f3f1e9` | `#0d0f10` | Body-Background |
| `--bg-elevated` | `#ffffff` | `#181a1d` | Modal-Cards, Pillen, Quick-Selects |
| `--bg-card` | `#ffffff` | `#1b1d20` | Device-Cards im Inventar |
| `--bg-card-hover` | `#faf8f0` | `#232529` | Card-Hover |
| `--bg-input` | `#ffffff` | `#131517` | Input-Felder |
| `--border` | `rgba(0,0,0,.16)` | `rgba(255,255,255,.12)` | Standard-Rahmen, Trennlinien |
| `--border-strong` | `rgba(0,0,0,.36)` | `rgba(255,255,255,.28)` | Strong-Border bei Hover/Fokus |
| `--text` | `#16181b` | `#f1f2f4` | Body-Text |
| `--text-muted` | `#43464d` | `#c0c3cb` | Labels, sekundärer Text |
| `--text-subtle` | `#71747c` | `#9296a0` | Mini-Caps (Tresor-Name, Versionen) |
| `--text-faint` | `#a8aab1` | `#6a6e78` | Placeholder, Disabled |

### Status (semantisch, nicht dekorativ)

| Token | Light | Dark | Bedeutung |
|---|---|---|---|
| `--online` | `#2f6b3e` | `#86d8a4` | Heartbeat OK |
| `--offline` | `#9d2f24` | `#e57164` | Heartbeat fehlgeschlagen / Fehler |
| `--checking` | `#966115` | `#efc26a` | Heartbeat läuft (gelb-Olive) |
| `--danger` | `#9d2f24` | `#e57164` | Validation-/Action-Fehler, FEHLGESCHLAGEN-Badge |
| `--tls-bad` | `#9d2f24` | `#e57164` | TLS-AUS-Markierung auf Karten |

**Regel:** Status-Farben **nur** für Status. Nie für „hübsche" Hervorhebung.

### Geometrie

```
--radius-sm: 6px;   /* Buttons, Pillen schmal, Inputs */
--radius-md: 10px;  /* Modal-Cards, Result-Boxes */
--radius-lg: 14px;  /* große Container, Logo-Rahmen */

--topbar-height: 64px;
--sidebar-width: 274px;
```

## Typografie

```
--font-display:  Bahnschrift > DIN Next/2014 > Eurostile > Univers Cond > system-ui
--font-sans:     ui-sans-serif, system-ui, Segoe UI Variable, Segoe UI
--font-mono:     ui-monospace, SF Mono, Cascadia Mono, JetBrains Mono, Consolas
```

### Skala + Verwendung

| Use | Font | Größe | Caps | Letter-Spacing |
|---|---|---|---|---|
| Hauptüberschrift (`INVENTAR`) | display | 26-32px | UPPERCASE | 0.04em |
| Modal-Titel (`Tresor anlegen`) | display | 18-20px | normal | -0.005em |
| Brand `OPN-Cockpit` | display | 22px | normal | -0.01em |
| Section-Title in Modal | display | 13.5px | UPPERCASE | 0.04em |
| Sektion-Eyebrow (`Tresor · TEST`) | mono | 10.5px | UPPERCASE | 0.06em |
| Body-Text | sans | 14px | normal | -0.005em |
| Form-Label | sans | 11-12px | UPPERCASE | 0.04em |
| Form-Hint | sans | 12px | normal | -0.005em |
| Form-Error | sans | 13px | normal | -0.005em |
| Mono-Wert (CIDR, Port, IDs) | mono | 12-13px | je Wert | 0 |
| Tastatur-Shortcut (kbd) | mono | 11.5px | normal | 0 |

**Bahnschrift-Trick:** `font-feature-settings: 'ss01', 'cv11'` aktivieren
für schmalere Ziffern + offenere „a"/„g". Schon global im body gesetzt.

## Spacing

Kein striktes 4px/8px-Raster. Stattdessen häufige Werte aus dem
bestehenden CSS:

```
3, 4, 6, 8, 10, 12, 14, 18, 20, 24, 28px
```

Faustregeln:
- **Innerhalb einer Komponente:** 4–8px
- **Zwischen verwandten Bereichen** (Form-Felder einer Section): 8–12px
- **Zwischen Sektionen** (vor `form-divider`): 18px oben, 4px unten
- **Modal-Body Padding:** 20px
- **Outer Layout** (Topbar zu Sidebar/Main): 24–28px

## Layout-Shell

```
┌────────────────────────────────────────────────┐
│ Topbar (64px) — Brand · Search · Actions       │
├──────────┬─────────────────────────────────────┤
│          │                                     │
│ Sidebar  │  Main Content                       │
│ (274px)  │  ┌──────────────┐ ┌──────────────┐  │
│          │  │ Device-Card  │ │ Device-Card  │  │
│ Filter   │  └──────────────┘ └──────────────┘  │
│ Aktionen │  ┌──────────────┐ ┌──────────────┐  │
│ Shortcuts│  │              │ │              │  │
│          │  └──────────────┘ └──────────────┘  │
└──────────┴─────────────────────────────────────┘
```

- **Topbar** trägt Brand + Vault-Indikator (mit pulsierendem olive
  Dot vor `vault-name`) + globale Such-Box + Icon-Buttons + Lock.
- **Sidebar** ist fix 274px, gruppiert in Sektionen (`Gruppen`,
  `Aktionen`, `Shortcuts`).
- **Main** ist Card-Grid mit Auto-Fill (min 280px, gap 16px).

## Komponenten-Pattern

### Modal

```html
<div class="modal-backdrop" id="…-modal" hidden>
  <div class="modal-card" role="dialog" aria-modal="true" …>
    <div class="modal-header"><h2 id="…-title">…</h2><button class="icon-btn modal-close">×-SVG</button></div>
    <div class="modal-body">
      <h3 class="modal-section-title">SEKTION</h3>
      <label class="form-label">…</label>
      <input class="form-input" />
      <div class="form-hint">…</div>
      <div class="form-divider"></div>
      <div class="form-error" hidden></div>
    </div>
    <div class="modal-footer">
      <button class="btn-link">Abbrechen</button>
      <button class="btn-primary btn-primary-inline">Speichern</button>
    </div>
  </div>
</div>
```

**Backdrop-Click-Regel:**
- **Eingabe-Modal** (anlegen/ändern/Bulk/PW/Settings) → Backdrop-Click
  schließt **nicht** (User-Frust durch verlorene Eingaben).
- **Read-only-Modal** (Audit, About, Device-Details, Plan-Modal *im
  Result-Phase*) → Backdrop-Click schließt **schon**.

**Schließen-Wege bei Eingabe:** X-Button + Abbrechen + ESC. Punkt.

**Modal-Card-Varianten:**
- Standard: ~500px breit
- `modal-card-narrow`: ~420px (PW-Felder, kleines Modal)
- `modal-card-wide`: ~720px (Plan-Modal mit Result-Matrix)

### Button-Hierarchie

| Klasse | Wirkung | Verwendung |
|---|---|---|
| `btn-primary` | Olive gefüllt, Caps-light | Hauptaktion pro Screen/Modal (genau eine pro Sicht) |
| `btn-primary-inline` | Olive gefüllt, kompakter, im Modal-Footer | Modal-Hauptaktion |
| `btn-secondary` | Border + transparenter Hintergrund | Alternative wie „Bearbeiten", „Vorlage speichern" |
| `btn-link` | Reiner Text + Underline-on-hover | Sekundär-Action wie „Abbrechen", Links wie „Vorschläge laden" |
| `icon-btn` | 34×34px Quadrat, nur SVG | Topbar, Modal-Close |

**Regeln:**
- Pro Modal-Footer **eine** primäre Aktion rechts, alle anderen als
  `btn-link` links daneben.
- **Disable, kein Hide:** Button bleibt sichtbar wenn Bedingung fehlt,
  Tooltip erklärt. Verschwindende Buttons sind verwirrend.
- **Loading-Zustand:** `btn.disabled = true; btn.textContent = '…läuft…'`.
  Im `finally` zurück auf Original. **Achtung:** Reset-Text immer
  state-abhängig setzen, sonst F11-Bug (siehe TEST-FINDINGS).

### Form-Felder

```
LABEL CAPS in --text-muted
[ Input mit subtilem Border, 6px radius, white/elevated bg            ]
Form-Hint in --text-subtle, 12px sans
Wenn ergänzender Helfer wie 'Vorschläge laden':
  Button als btn-link unter dem Input.
```

**Input-mit-Button** (Picker-Pattern):
```html
<div class="form-input-with-button">
  <input class="form-input" />
  <button class="btn-secondary">Aktion</button>
</div>
```

**Validation-Fehler:** rote `.form-error`-Box unter der Section, nicht
unter dem einzelnen Feld. Globale Section-Fehler sind klarer als
Felder-Pop-Tooltips.

**Validation-Erfolg:** olive `.form-success`-Box („Gespeichert.").
Auto-fade ist okay, aber nicht zwingend.

### Datalist + Suggest-Pattern

```html
<input list="datalist-id" />
<datalist id="datalist-id"></datalist>
<button class="btn-link suggest-btn">Vorschläge laden</button>
```

**Re-Browse-Trick** (siehe `enableDatalistRebrowse` in app.js): bei
focus/mousedown den Wert temporär leeren wenn er einer Suggestion
entspricht — sonst zeigt der Browser nach Auswahl keine Alternativen
mehr. Bei blur ohne Auswahl restaurieren.

**Suggestion-Chips sind verworfen** (F19v1 → v2). Chip-Liste sprengt das
Modal bei 20+ Einträgen. Nicht wieder einbauen.

### Status-Indikator (Heartbeat-Dot)

```css
width: 5–8px; height: gleich; border-radius: 50%;
background: var(--online | --offline | --checking);
box-shadow: 0 0 0 1.5px var(--…-glow);  /* sanftes Glow als 2. Ring */
```

**Pulse-Animation** für `checking`-State:
```css
@keyframes status-pulse {
  0%, 100% { opacity: 1; }
  50%      { opacity: 0.55; }
}
animation: status-pulse 1.4s ease-in-out infinite;
```

Nur für `checking` — `online` und `offline` bleiben statisch.

### Badge / Pill

```css
display: inline-flex;
padding: 2-4px 8-10px;
border-radius: 999px;  /* immer pill */
font-family: var(--font-mono);
font-size: 11-12px;
letter-spacing: 0.04em;
text-transform: uppercase;
border: 1px solid var(--border);
background: var(--bg-elevated);
```

**Status-Badges** auf Karten: rote Hintergrund bei FEHLGESCHLAGEN/TLS-
AUS. Olive bei OK. Sonst neutral.

### Selection-Summary (Info-Box)

```css
margin-top: 12px;
padding: 12px 14px;
border: 1px solid var(--border);
border-radius: var(--radius-md);
background: var(--bg-elevated);
font-size: 13px;
```

**Variant** `no-selection`: rote Border + Background, Mono-Font.
Macht klar dass die Aktion auf nichts ausgerollt würde.

## Motion

```css
transition: all 0.15s;       /* Hover-State */
transition: all 0.18s ease;  /* Theme-Switch */
```

**Regeln:**
- Keine Bouncing-/Spring-Animationen.
- Keine Scroll-Trigger-Animationen.
- Page-Load: Inhalt direkt da, nicht „faded in".
- Heartbeat-Pulse für `checking` ist die **einzige** Endlos-Animation
  auf dem Screen.

## Iconographie

- **SVG-Icons** inline im HTML (nie Icon-Font, nie PNG-Sprites).
- Stroke-only, kein Fill: `fill="none" stroke="currentColor" stroke-width="1.4"`.
- Größe: 13–16px für Buttons, 24px+ für illustrative Icons.
- Stroke-Linecap immer `round`, Linejoin `round`.
- ViewBox auf das Icon zugeschnitten, kein Padding im SVG.

**Beispiele in [`web/templates/index.html`](../src/opn_cockpit/web/templates/index.html):**
Lock, Audit, About, Theme, Settings, Folder-Picker, Lupe.

## Do / Don't

### Tun

- ✅ Tokens (`var(--…)`) statt Hex-Werte.
- ✅ Bahnschrift für Überschriften und CAPS, Sans für Fließtext.
- ✅ Eine Akzentfarbe pro Screen, sonst neutral.
- ✅ Mono-Font für Werte (CIDR, IP, Port, Hostname, Ports).
- ✅ Backdrop-Click sperren bei Eingabe, freigeben bei Read-only.
- ✅ Validation-Fehler als Section-Box, nicht Tooltip.

### Lassen

- ❌ Inter, Roboto, Arial — wir haben Bahnschrift/System-Sans aus Grund.
- ❌ Lila Gradients, Glasmorphismus, Neumorphismus.
- ❌ Mehrere Akzentfarben gleichzeitig (Lila + Cyan, etc.).
- ❌ Mehr als 2 Schriftgewichte pro Komponente.
- ❌ Hidden-Attribute auf Elementen mit `display: …` ohne `[hidden]`-
  Override (haben wir global geregelt, aber nicht überdrehen).
- ❌ Buttons die je nach State erscheinen/verschwinden ohne Erklärung.

## Pflegen

Wenn du eine neue UI-Komponente baust und sie nicht in diesem Dokument
auftaucht: **erst hier ergänzen, dann coden**. So bleibt der Guide
synchron statt veraltet.

Bei sichtbaren Änderungen am visuellen Stil → Memory-Datei
`project_opn_cockpit_v2_aesthetic.md` updaten + hier verlinken.
