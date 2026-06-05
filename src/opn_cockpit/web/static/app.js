// OPN-Cockpit Frontend (v2.0 Iter 3).
//
// State-Machine:
//   boot  -> versuche /api/auth/me mit gespeichertem Token
//             ok -> main
//             401 -> login
//   login -> Tresor-Auswahl + Passwort, oder Create-Vault-Inline-Dialog
//   main  -> Inventar-View (Sidebar + Kacheln) mit Heartbeat-Polling
//
// Token-Storage: sessionStorage (per Tab, beim Schliessen weg).

(function () {
  'use strict';

  // -------------------- LastPass-Modal-Killer (Holzhammer v2) --------------------
  //
  // LastPass-Save-Popup steckt in einer CLOSED Shadow-DOM unter
  // <div data-lastpass-root>. Der Host selbst ist 0x0 Pixel gross
  // (deshalb hat ihn die v1-Regel mit Groessen-Filter ignoriert).
  // Der Iframe IM Shadow ist position:fixed/full-viewport — visuell
  // der Modal-Inhalt. CSS und JS koennen den Iframe NICHT direkt
  // erreichen (closed shadow), aber wenn der Host weg ist, geht
  // Shadow+Iframe mit.
  //
  // Strategy: Host UNCONDITIONAL entfernen sobald er auftaucht.
  // Throttle gegen LP-Re-Inject-Loop (max ~5 Entfernungen pro Sekunde).
  //
  // Auswirkung: LastPass-Auto-Fill-Icons + Save-Popup beide weg auf
  // diesem Tab. Wer LP fuer das Cockpit haben will, deaktiviert den
  // Killer per LASTPASS_KILL_OFF localStorage-Flag (siehe unten).
  let lpRemovedThisTick = 0;
  let lpResetTimer = null;
  const LP_MAX_REMOVALS_PER_SEC = 5;
  const LP_SELECTORS = [
    '[data-lastpass-root]',
    '[data-lastpass-icon-root]',
    '[data-bitwarden-watching]',
    '[data-onepassword-overlay-root]',
    '[data-dashlane-rid]',
  ].join(',');
  function killLastPassModals(root) {
    if (lpRemovedThisTick >= LP_MAX_REMOVALS_PER_SEC) return;
    if (!root || typeof root.querySelectorAll !== 'function') return;
    let found;
    try { found = root.querySelectorAll(LP_SELECTORS); } catch (_e) { return; }
    for (const el of found) {
      if (lpRemovedThisTick >= LP_MAX_REMOVALS_PER_SEC) break;
      try {
        el.remove();
        lpRemovedThisTick++;
      } catch (_e) {
        /* ignore */
      }
    }
    if (!lpResetTimer && lpRemovedThisTick > 0) {
      lpResetTimer = setTimeout(() => {
        lpRemovedThisTick = 0;
        lpResetTimer = null;
      }, 1000);
    }
  }
  function setupLastPassKiller() {
    if (!document.body || typeof MutationObserver === 'undefined') return;
    // Opt-out fuer User die LP doch wollen
    try {
      if (localStorage.getItem('LASTPASS_KILL_OFF') === '1') return;
    } catch (_e) { /* private mode */ }
    killLastPassModals(document.documentElement);
    const obs = new MutationObserver((mutations) => {
      for (const m of mutations) {
        if (m.type === 'childList') {
          for (const node of m.addedNodes) {
            if (node.nodeType === 1) {
              // Pruefe Node selbst + Subtree
              if (node.matches && node.matches(LP_SELECTORS)) {
                try { node.remove(); lpRemovedThisTick++; } catch (_e) {}
              } else {
                killLastPassModals(node);
              }
            }
          }
        }
      }
    });
    obs.observe(document.documentElement, { childList: true, subtree: true });
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', setupLastPassKiller, { once: true });
  } else {
    setupLastPassKiller();
  }

  const STATE_KEY = 'opn-cockpit-token';
  const THEME_KEY = 'opn-cockpit-theme';
  const HEARTBEAT_INTERVAL_MS = 30000;
  const SESSION_TICK_MS = 15000;
  const HEARTBEAT_STALE_AFTER_MS = 90000;

  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => Array.from(document.querySelectorAll(sel));

  // -------------------- Inline-Validierung --------------------
  // Wird vom DOMContentLoaded-Setup aktiviert: jedes Input mit
  // data-validate="<key>" bekommt einen input-Listener, der den
  // passenden Validator ruft. Validatoren liefern null bei OK oder
  // einen Fehlertext (kurz, deutsch). Empty value gilt immer als OK
  // damit Pflichtfelder nicht beim Tippen rot leuchten - der
  // Submit-Pfad prueft "leer aber required".
  const VALIDATORS = {
    cidr(v) {
      // IPv4-CIDR-Pruefung clientseitig - lehnt Host-Bits ab (strict).
      // IPv6 ueberlassen wir dem Server (Frontend bleibt schmal).
      const m = v.match(/^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})\/(\d{1,2})$/);
      if (!m) return 'CIDR erwartet (z. B. 10.0.0.0/24).';
      const octets = m.slice(1, 5).map(Number);
      if (octets.some((o) => o < 0 || o > 255)) return 'Oktett ausserhalb 0-255.';
      const prefix = Number(m[5]);
      if (prefix < 0 || prefix > 32) return 'Prefix 0-32.';
      // Host-Bits muessen 0 sein
      const ip = (octets[0] << 24 >>> 0) + (octets[1] << 16) + (octets[2] << 8) + octets[3];
      const mask = prefix === 0 ? 0 : (~0 << (32 - prefix)) >>> 0;
      if ((ip & ~mask) >>> 0) return 'Host-Bits nicht 0 - Netz-Adresse erwartet.';
      return null;
    },
    ipv4(v) {
      const m = v.match(/^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/);
      if (!m) return 'IPv4-Adresse erwartet.';
      const octets = m.slice(1).map(Number);
      if (octets.some((o) => o < 0 || o > 255)) return 'Oktett ausserhalb 0-255.';
      return null;
    },
    host(v) {
      // FQDN ODER IPv4. Akzeptiert any-letter-Hostnamen ohne Punkt
      // (z. B. "opn-1" intern). Lehnt offensichtlich falsche Zeichen ab.
      if (/^(\d{1,3}\.){3}\d{1,3}$/.test(v)) return VALIDATORS.ipv4(v);
      if (!/^[a-zA-Z0-9][a-zA-Z0-9-.]{0,253}$/.test(v)) {
        return 'Nur Buchstaben, Ziffern, "-" und "." erlaubt.';
      }
      if (v.endsWith('.') || v.includes('..')) return 'Punkt-Fehler im Hostnamen.';
      return null;
    },
    aliasName(v) {
      // OPNsense: Buchstaben, Ziffern, Underscore, max 32 Zeichen,
      // muss mit Buchstaben starten.
      if (!/^[A-Za-z][A-Za-z0-9_]{0,31}$/.test(v)) {
        return 'Buchstabe am Anfang, dann nur a-z A-Z 0-9 _ (max 32).';
      }
      return null;
    },
    gatewayName(v) {
      // OPNsense Gateway-Identifier: Buchstabe + Buchstabe/Ziffer/_,
      // typischerweise GROSSGESCHRIEBEN, aber wir tolerieren beides.
      if (!/^[A-Za-z][A-Za-z0-9_]{0,31}$/.test(v)) {
        return 'Gateway-Name: Buchstabe am Anfang, dann a-z A-Z 0-9 _ .';
      }
      return null;
    },
    port(v) {
      // Erlaubt: leere Eingabe, "any", einzelner Port, Range, Alias-Name.
      // Konservativ: nur Port-Zahl oder Range strikt pruefen, sonst OK
      // durchwinken (Alias-Namen koennen alles sein).
      if (v.toLowerCase() === 'any') return null;
      if (/^\d+$/.test(v)) {
        const n = Number(v);
        if (n < 1 || n > 65535) return 'Port 1-65535.';
        return null;
      }
      if (/^\d+[-:]\d+$/.test(v)) {
        const [lo, hi] = v.split(/[-:]/).map(Number);
        if (lo < 1 || hi > 65535 || lo > hi) return 'Ungueltige Port-Range.';
        return null;
      }
      // Alias-Name oder Sonderform - nicht clientseitig blocken.
      return null;
    },
  };

  function attachInlineValidator(input) {
    const key = input.dataset.validate;
    const validator = VALIDATORS[key];
    if (!validator) return;
    const errEl = document.createElement('span');
    errEl.className = 'form-inline-error';
    errEl.hidden = true;
    if (input.parentNode) input.parentNode.insertBefore(errEl, input.nextSibling);
    const run = () => {
      const v = (input.value || '').trim();
      if (!v) {
        input.classList.remove('is-invalid');
        errEl.hidden = true;
        return;
      }
      const error = validator(v);
      if (error) {
        input.classList.add('is-invalid');
        errEl.textContent = error;
        errEl.hidden = false;
      } else {
        input.classList.remove('is-invalid');
        errEl.hidden = true;
      }
    };
    input.addEventListener('input', run);
    input.addEventListener('blur', run);
  }

  function setupInlineValidators() {
    document.querySelectorAll('input[data-validate]').forEach(attachInlineValidator);
  }

  // -------------------- App-State --------------------

  const state = {
    devices: [],          // DeviceResponse[]
    tags: [],             // TagSummary[]
    heartbeat: {},        // device_id -> { reachable: bool, checked_at_ms: number }
    activeTag: null,      // null = "alle"
    search: '',           // freitext
    sessionInfo: null,
    heartbeatInFlight: false,
    selectedDeviceIds: new Set(),  // globale Multi-Auswahl fuer Plan/Apply
    outstandingByDevice: {},       // device_id -> { count, plans[] }
    backupsByDevice: {},           // device_id -> { count, latestTs }
    certsByDevice: {},             // device_id -> { count, soonestDays, certs[], summary }
    driftByDevice: {},             // device_id -> { drift, hasBaseline, summary, baselineIso }
    vaultSettings: null,           // gecachte /api/vaults/settings - fuer Opt-In-Features wie Drift
    firmware: {},                  // device_id -> { version, status, update_available, summary, checked_at_iso }
    firmwareLoading: false,
    serverMode: 'vault',           // 'vault' (single) | 'user-db' (multi)
    bootstrapStatus: 'single-user', // single-user | needs-admin | needs-vault-unlock | ready
  };

  let heartbeatHandle = null;
  let sessionTickHandle = null;

  // -------------------- Theme --------------------

  function initTheme() {
    const saved = localStorage.getItem(THEME_KEY);
    if (saved === 'light' || saved === 'dark') {
      document.documentElement.setAttribute('data-theme', saved);
      return;
    }
    if (window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches) {
      document.documentElement.setAttribute('data-theme', 'light');
    }
  }

  function toggleTheme() {
    const cur = document.documentElement.getAttribute('data-theme') || 'dark';
    const next = cur === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    try { localStorage.setItem(THEME_KEY, next); } catch (_) {}
  }

  // -------------------- Token --------------------

  function getToken() {
    try { return sessionStorage.getItem(STATE_KEY); } catch (_) { return null; }
  }

  function setToken(t) {
    try { sessionStorage.setItem(STATE_KEY, t); } catch (_) {}
  }

  function clearToken() {
    try { sessionStorage.removeItem(STATE_KEY); } catch (_) {}
  }

  // -------------------- API --------------------

  async function apiGet(path) {
    const headers = { Accept: 'application/json' };
    const t = getToken();
    if (t) headers.Authorization = `Bearer ${t}`;
    return await fetch(path, { headers });
  }

  async function apiPost(path, body) {
    const headers = { 'Content-Type': 'application/json', Accept: 'application/json' };
    const t = getToken();
    if (t) headers.Authorization = `Bearer ${t}`;
    return await fetch(path, {
      method: 'POST',
      headers,
      body: body ? JSON.stringify(body) : undefined,
    });
  }

  async function apiDelete(path, body) {
    const headers = { 'Content-Type': 'application/json', Accept: 'application/json' };
    const t = getToken();
    if (t) headers.Authorization = `Bearer ${t}`;
    return await fetch(path, {
      method: 'DELETE',
      headers,
      body: body ? JSON.stringify(body) : undefined,
    });
  }

  async function apiPatch(path, body) {
    const headers = { 'Content-Type': 'application/json', Accept: 'application/json' };
    const t = getToken();
    if (t) headers.Authorization = `Bearer ${t}`;
    return await fetch(path, {
      method: 'PATCH',
      headers,
      body: body ? JSON.stringify(body) : undefined,
    });
  }

  // -------------------- Screen Switching --------------------

  function showScreen(name) {
    document.getElementById('app').setAttribute('data-state', name);
    $$('.screen').forEach((s) => { s.hidden = s.dataset.screen !== name; });
  }

  function showLoginView(name) {
    $$('.login-view').forEach((v) => { v.hidden = v.dataset.view !== name; });
    const loginErr = $('#login-error'); if (loginErr) loginErr.hidden = true;
    const createErr = $('#create-error'); if (createErr) createErr.hidden = true;
    const muErr = $('#mu-error'); if (muErr) muErr.hidden = true;
    const adminErr = $('#setup-admin-error'); if (adminErr) adminErr.hidden = true;
    const vaultErr = $('#setup-vault-error'); if (vaultErr) vaultErr.hidden = true;
  }

  // -------------------- Bootstrap-Mode-Detection --------------------

  async function fetchBootstrapStatus() {
    const response = await fetch('/api/bootstrap/status', {
      headers: { Accept: 'application/json' },
    });
    if (!response.ok) throw new Error('Bootstrap-Status nicht abrufbar.');
    const data = await response.json();
    state.serverMode = data.mode;
    state.bootstrapStatus = data.status;
    state.adminRequiresPwChange = !!data.admin_requires_password_change;
    applySetupWizardMode();
    return data;
  }

  // Setup-Wizard-Maske an Server-Status anpassen:
  // - Default-Admin hat noch sein Initial-PW (admin_requires_password_change
  //   = True) -> Felder "Neues Admin-Passwort" einblenden, Pflicht.
  // - Default-Admin hat schon ein eigenes PW (False) -> Felder ausblenden,
  //   die Wizard-Maske wird zur reinen Vault-Unlock-Form mit Auth.
  function applySetupWizardMode() {
    const requirePw = !!state.adminRequiresPwChange;
    const block = document.getElementById('su-newpw-block');
    if (block) block.hidden = !requirePw;
    const hint = document.getElementById('su-hint');
    if (hint) {
      if (requirePw) {
        hint.innerHTML =
          '<strong>Erste Einrichtung.</strong> ' +
          'Default-Login <code>admin</code> / <code>OPN-Cockpit!</code> — ' +
          'beim ersten Mal Pflicht: neues Admin-Passwort setzen + zentralen ' +
          'Tresor entsperren.';
      } else {
        hint.innerHTML =
          '<strong>Tresor entsperren.</strong> ' +
          'Nach Service-Neustart muss der zentrale Tresor mit deinem ' +
          'Master-Passwort wieder geoeffnet werden. Logge dich dazu mit ' +
          'deinem Admin-Account ein.';
      }
    }
  }

  // -------------------- Multi-User-Login --------------------

  async function doMultiUserLogin() {
    const username = $('#mu-username').value.trim();
    const password = $('#mu-password').value;
    if (!username || !password) return;
    const errorBox = $('#mu-error');
    errorBox.hidden = true;
    const btn = $('#mu-login-btn');
    btn.disabled = true;
    btn.textContent = 'Anmelden…';
    try {
      const response = await apiPost('/api/auth/login', {
        username,
        password,
      });
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        throw new Error(body.detail || `Fehler ${response.status}`);
      }
      const data = await response.json();
      setToken(data.token);
      $('#mu-password').value = '';
      await enterMain(data);
    } catch (err) {
      errorBox.textContent = err.message;
      errorBox.hidden = false;
      btn.disabled = false;
      btn.textContent = 'Anmelden';
    }
  }

  // -------------------- Setup-Wizard (Multi-User-First-Run, seit F28) --------------------

  async function doSetupUnlockVault() {
    const adminUser = $('#su-admin-user').value.trim();
    const adminPw = $('#su-admin-pw').value;
    const newPw1 = $('#su-admin-newpw').value;
    const newPw2 = $('#su-admin-newpw2').value;
    const path = $('#su-vault-path').value.trim();
    const vaultPw = $('#su-vault-pw').value;
    const createIfMissing = $('#su-vault-create').checked;
    const errorBox = $('#setup-vault-error');
    errorBox.hidden = true;
    const requirePwChange = !!state.adminRequiresPwChange;
    if (!adminUser) return showSetupError(errorBox, 'Admin-Benutzername fehlt (Default: admin).');
    if (!adminPw) return showSetupError(errorBox, 'Aktuelles Admin-Passwort fehlt.');
    if (requirePwChange) {
      if (newPw1.length < 12) return showSetupError(errorBox, 'Neues Admin-Passwort muss mindestens 12 Zeichen haben.');
      if (newPw1 !== newPw2) return showSetupError(errorBox, 'Die beiden neuen Admin-Passwoerter stimmen nicht ueberein.');
      if (newPw1 === adminPw) return showSetupError(errorBox, 'Neues Admin-Passwort darf nicht mit dem Default identisch sein.');
    }
    if (!path) return showSetupError(errorBox, 'Pfad zur Tresor-Datei fehlt.');
    if (vaultPw.length < 12) return showSetupError(errorBox, 'Tresor-Master-Passwort muss mindestens 12 Zeichen haben.');
    const btn = $('#setup-vault-btn');
    btn.disabled = true;
    btn.textContent = 'Entsperre…';
    try {
      const response = await fetch('/api/bootstrap/vault', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Accept: 'application/json',
        },
        body: JSON.stringify({
          vault_path: path,
          password: vaultPw,
          create_if_missing: createIfMissing,
          admin_username: adminUser,
          admin_password: adminPw,
          // new_admin_password nur mitschicken wenn der Server das verlangt,
          // sonst wertet er es als "User will Passwort wechseln" und scheitert
          // an der Identitaets-Pruefung (Default != new). Server toleriert
          // ein fehlendes Feld wenn must_change_password=False.
          ...(requirePwChange ? { new_admin_password: newPw1 } : {}),
        }),
      });
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        throw new Error(body.detail || `Fehler ${response.status}`);
      }
      const data = await response.json().catch(() => ({}));
      if (data && data.token) {
        // Server hat den Wizard-Schritt als vollwertige Authentifizierung
        // gewertet (User-DB-Login + Master-PW) und uns direkt eine Session
        // mitgegeben. Token speichern und ohne zweiten Login in den Main-View.
        setToken(data.token);
        // serverMode + adminRequiresPwChange aktualisieren, sonst zeigt das
        // Frontend Multi-User-spezifische Buttons falsch.
        try { await fetchBootstrapStatus(); } catch (_e) {}
        await enterMain({
          vault_path: data.vault_path,
          vault_filename: data.vault_filename,
          inactivity_timeout_s: data.inactivity_timeout_s,
          seconds_until_expiry: data.seconds_until_expiry,
        });
        return;
      }
      // Fallback: kein Token -> normaler Multi-User-Login als zweiter Schritt.
      await fetchBootstrapStatus();
      enterBootstrapPhase();
    } catch (err) {
      showSetupError(errorBox, err.message);
    } finally {
      btn.disabled = false;
      btn.textContent = 'Tresor entsperren / anlegen';
    }
  }

  function showSetupError(box, msg) {
    box.textContent = msg;
    box.hidden = false;
  }

  function enterBootstrapPhase() {
    // Nach jedem Status-Wechsel: passenden Screen zeigen. Seit F28 gibt es
    // 'needs-admin' nicht mehr als eigener Step — Default-Admin wird vom
    // Server angelegt, dann startet der Setup-Wizard direkt mit dem
    // kombinierten "Admin-PW wechseln + Vault entsperren"-Formular.
    const s = state.bootstrapStatus;
    if (s === 'needs-admin' || s === 'needs-vault-unlock') {
      showScreen('setup');
      showLoginView('setup-vault');
      setTimeout(() => $('#su-admin-pw').focus(), 0);
    } else if (s === 'ready') {
      showScreen('login');
      showLoginView('multi-user');
      setTimeout(() => $('#mu-username').focus(), 0);
    } else {
      // single-user (sollte beim Multi-User-Pfad nicht erscheinen).
      showScreen('login');
      showLoginView('picker');
      fetchVaultsAndPopulate().catch(() => {});
    }
  }

  // -------------------- Login (unchanged from Iter 2) --------------------

  async function fetchVaultsAndPopulate() {
    const response = await apiGet('/api/vaults');
    if (!response.ok) throw new Error('Konnte Tresor-Liste nicht abrufen.');
    const data = await response.json();

    const pathInput = $('#login-vault-path');
    const knownBox = $('#login-known-vaults');
    knownBox.innerHTML = '';

    if (!data.vaults || data.vaults.length === 0) {
      $('#login-hint').textContent =
        'Es wurde noch kein Tresor gefunden. Klicke „Neuen Tresor anlegen…" um zu starten, oder waehle einen Tresor von einem anderen Speicherort (z. B. USB-Stick).';
      pathInput.value = '';
      knownBox.hidden = true;
    } else {
      const n = data.vaults.length;
      $('#login-hint').textContent =
        n === 1
          ? 'Ein Tresor gefunden — bitte Passwort eingeben.'
          : `${n} Tresore gefunden — anklicken oder Pfad eintragen.`;
      // Default vault als vorausgefuelltes Pfad-Feld
      const def = data.vaults.find((v) => v.is_default) || data.vaults[0];
      if (def && !pathInput.value) pathInput.value = def.path;

      // Bekannte Tresore als Klick-Chips unter dem Pfad-Input
      const label = document.createElement('div');
      label.className = 'ssw-known-vaults-label';
      label.textContent = 'Bekannte Tresore (Klick uebernimmt den Pfad):';
      knownBox.appendChild(label);
      for (const v of data.vaults) {
        const chip = document.createElement('button');
        chip.type = 'button';
        chip.className = 'ssw-vault-chip';
        chip.textContent = v.filename;
        chip.title = v.path;
        chip.addEventListener('click', () => {
          pathInput.value = v.path;
          $('#password-input').focus();
        });
        knownBox.appendChild(chip);
      }
      knownBox.hidden = false;
      // Button + Passwort sind im neuen Markup immer enabled
      $('#unlock-btn').disabled = false;
      $('#password-input').disabled = false;
      setTimeout(() => $('#password-input').focus(), 0);
    }

    // "Datei suchen..."-Button nur im Single-User-Mode anzeigen.
    // Im Multi-User-Server-Mode laeuft der Server remote, der native
    // Picker waere dort unsichtbar.
    const browseBtn = $('#login-browse-btn');
    if (browseBtn) browseBtn.hidden = state.serverMode === 'user-db';

    const nameInput = $('#new-vault-name');
    const dirInput = $('#new-vault-directory');
    if (nameInput && !nameInput.value) nameInput.value = data.suggested_new_name || 'main';
    if (dirInput && !dirInput.value) dirInput.value = data.suggested_new_directory || '';
    renderPathSuggestions(data.path_suggestions || []);
    updateVaultTargetPreview();
  }

  async function pickLoginVaultFile() {
    // Triggert serverseitig den nativen Windows-File-Dialog (gleicher
    // Endpoint wie der Tresor-Switch-Modal nach dem Login).
    const btn = $('#login-browse-btn');
    if (btn) btn.disabled = true;
    try {
      const response = await apiGet('/api/files/pick-file');
      if (response.status === 403) {
        showLoginError('Datei-Picker steht nur im Single-User-Mode zur Verfuegung.');
        return;
      }
      if (response.status === 501) {
        showLoginError('Datei-Picker steht derzeit nur unter Windows zur Verfuegung. Bitte Pfad eintragen.');
        return;
      }
      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        showLoginError(data.detail || 'Datei-Picker fehlgeschlagen.');
        return;
      }
      const data = await response.json();
      if (data.cancelled || !data.path) return;
      $('#login-vault-path').value = data.path;
      $('#password-input').focus();
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  function showLoginError(msg) {
    const box = $('#login-error');
    if (!box) return;
    box.textContent = msg;
    box.hidden = false;
  }

  function joinPath(dir, filename) {
    if (!dir) return filename;
    const trimmed = dir.replace(/[\\\/]+$/g, '');
    return `${trimmed}\\${filename}`;
  }

  function ensureVaultSuffix(name) {
    if (!name) return '';
    return /\.opnvault$/i.test(name) ? name : `${name}.opnvault`;
  }

  function updateVaultTargetPreview() {
    const nameRaw = ($('#new-vault-name')?.value || '').trim();
    const dirRaw = ($('#new-vault-directory')?.value || '').trim();
    const preview = $('#new-vault-target-preview');
    const hidden = $('#new-vault-path');
    if (!nameRaw || !dirRaw) {
      if (preview) preview.textContent = '—';
      if (hidden) hidden.value = '';
      return;
    }
    const target = joinPath(dirRaw, ensureVaultSuffix(nameRaw));
    if (preview) preview.textContent = target;
    if (hidden) hidden.value = target;
  }

  // -------------------- Folder-Browser-Modal --------------------

  let fbState = { current: '', parent: null };

  async function openFolderPicker() {
    // Versuche zuerst den nativen OS-Picker (Single-User + Windows).
    // Bei 501/403/Netzwerkfehler fallback auf den Web-Picker.
    try {
      const response = await fetch('/api/files/pick-folder');
      if (response.ok) {
        const data = await response.json();
        if (data.cancelled || !data.path) return;
        const dirInput = $('#new-vault-directory');
        if (dirInput) dirInput.value = data.path;
        updateVaultTargetPreview();
        return;
      }
      // 501 (nicht-Windows) oder 403 (Multi-User) -> Fallback ohne Toast.
      if (response.status === 501 || response.status === 403) {
        await openWebFolderBrowser();
        return;
      }
      // Andere Fehler: Toast + Fallback.
      const body = await response.json().catch(() => ({}));
      showToast(body.detail || `Native Picker Fehler ${response.status}`, true);
      await openWebFolderBrowser();
    } catch (_err) {
      // Netzwerkproblem (sehr unwahrscheinlich auf localhost) -> Web-Picker.
      await openWebFolderBrowser();
    }
  }

  async function openWebFolderBrowser() {
    const dirInput = $('#new-vault-directory');
    const startPath = dirInput ? dirInput.value.trim() : '';
    $('#fb-error').hidden = true;
    $('#folder-browser-modal').hidden = false;
    await loadFolderBrowser(startPath);
  }

  function closeFolderBrowser() {
    $('#folder-browser-modal').hidden = true;
  }

  function deriveParentDir(raw) {
    if (!raw) return '';
    const s = raw.replace(/\//g, '\\');
    const idx = s.lastIndexOf('\\');
    return idx >= 0 ? s.substring(0, idx) : '';
  }

  function deriveFilename(raw) {
    if (!raw) return '';
    const s = raw.replace(/\//g, '\\');
    const idx = s.lastIndexOf('\\');
    return idx >= 0 ? s.substring(idx + 1) : s;
  }

  async function loadFolderBrowser(path) {
    const list = $('#fb-list');
    list.innerHTML = '<div class="fb-loading">Lade…</div>';
    $('#fb-error').hidden = true;
    try {
      const url = '/api/files/browse' + (path ? `?path=${encodeURIComponent(path)}` : '');
      const response = await fetch(url, { headers: { Accept: 'application/json' } });
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        throw new Error(body.detail || `Fehler ${response.status}`);
      }
      const data = await response.json();
      fbState = { current: data.current || '', parent: data.parent };
      $('#fb-current').textContent = data.current || 'Laufwerke';
      $('#fb-up-btn').disabled = data.parent === null;
      renderFolderBrowserList(data.entries || []);
    } catch (err) {
      list.innerHTML = '';
      const errBox = $('#fb-error');
      errBox.textContent = err.message;
      errBox.hidden = false;
    }
  }

  function renderFolderBrowserList(entries) {
    const list = $('#fb-list');
    list.innerHTML = '';
    if (!entries.length) {
      list.innerHTML = '<div class="fb-empty">(leer)</div>';
      return;
    }
    entries.forEach((e) => {
      const row = document.createElement('button');
      row.type = 'button';
      row.className = `fb-row fb-row-${e.kind}`;
      const icon = e.kind === 'vault' ? '🔐' : (e.kind === 'drive' ? '💽' : '📁');
      row.innerHTML = `<span class="fb-row-icon">${icon}</span><span class="fb-row-name">${escapeHtml(e.name)}</span>`;
      row.addEventListener('click', () => {
        if (e.kind === 'vault') {
          // Bestehende Vault-Datei angeklickt: Name (ohne Endung) ins
          // Name-Feld uebernehmen, Modal schliessen, Verzeichnis = aktueller.
          const baseName = e.name.replace(/\.opnvault$/i, '');
          const nameInput = $('#new-vault-name');
          if (nameInput) nameInput.value = baseName;
          const dirInput = $('#new-vault-directory');
          if (dirInput) dirInput.value = fbState.current;
          updateVaultTargetPreview();
          closeFolderBrowser();
        } else {
          // Ordner / Drive: hineinwechseln.
          loadFolderBrowser(e.path);
        }
      });
      list.appendChild(row);
    });
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function folderBrowserUp() {
    if (fbState.parent === null) return;
    loadFolderBrowser(fbState.parent || '');
  }

  function acceptFolderBrowser() {
    if (!fbState.current) {
      const errBox = $('#fb-error');
      errBox.textContent = 'Bitte einen Ordner waehlen (in ein Laufwerk wechseln).';
      errBox.hidden = false;
      return;
    }
    const dirInput = $('#new-vault-directory');
    if (dirInput) dirInput.value = fbState.current;
    updateVaultTargetPreview();
    closeFolderBrowser();
  }

  function renderPathSuggestions(suggestions) {
    const container = $('#new-vault-path-suggestions');
    if (!container) return;
    container.innerHTML = '';
    if (!suggestions.length) {
      container.hidden = true;
      return;
    }
    container.hidden = false;
    const label = document.createElement('span');
    label.className = 'path-suggestions-label';
    label.textContent = 'Schnellauswahl:';
    container.appendChild(label);
    const dirInput = $('#new-vault-directory');
    suggestions.forEach((s) => {
      const chip = document.createElement('button');
      chip.type = 'button';
      chip.className = 'path-suggestion-chip';
      chip.textContent = s.label;
      chip.title = s.path;
      chip.addEventListener('click', () => {
        if (dirInput) dirInput.value = s.path;
        updateVaultTargetPreview();
      });
      container.appendChild(chip);
    });
  }

  async function doUnlock() {
    const path = $('#login-vault-path').value.trim();
    const password = $('#password-input').value;
    if (!path) {
      showLoginError('Bitte Pfad zum Tresor eintragen oder bekannten Tresor anklicken.');
      return;
    }
    if (!password) {
      showLoginError('Master-Passwort fehlt.');
      return;
    }
    const errorBox = $('#login-error');
    errorBox.hidden = true;

    const btn = $('#unlock-btn');
    btn.disabled = true;
    btn.textContent = 'Entsperre…';
    try {
      const response = await apiPost('/api/auth/unlock', {
        vault_path: path,
        password: password,
      });
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        throw new Error(body.detail || `Fehler ${response.status}`);
      }
      const data = await response.json();
      setToken(data.token);
      $('#password-input').value = '';
      await enterMain(data);
    } catch (err) {
      errorBox.textContent = err.message;
      errorBox.hidden = false;
      btn.disabled = false;
      btn.textContent = 'Entsperren';
    }
  }

  async function doCreateVault() {
    // Hidden Pfad-Feld wird per updateVaultTargetPreview gefuettert.
    updateVaultTargetPreview();
    const name = ($('#new-vault-name').value || '').trim();
    const dir = ($('#new-vault-directory').value || '').trim();
    const path = ($('#new-vault-path').value || '').trim();
    const pw1 = $('#new-vault-pw1').value;
    const pw2 = $('#new-vault-pw2').value;
    const errorBox = $('#create-error');
    errorBox.hidden = true;

    if (!name) return showCreateError('Tresor-Name fehlt.');
    if (!dir) return showCreateError('Speicherort fehlt — bitte einen Ordner waehlen.');
    if (!path) return showCreateError('Pfad konnte nicht gebildet werden.');
    if (pw1.length < 12) return showCreateError('Passwort muss mindestens 12 Zeichen haben.');
    if (pw1 !== pw2) return showCreateError('Die beiden Passwörter stimmen nicht überein.');

    const btn = $('#create-confirm-btn');
    btn.disabled = true;
    btn.textContent = 'Lege an…';
    try {
      const response = await apiPost('/api/vaults', { path: path, password: pw1 });
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        throw new Error(body.detail || `Fehler ${response.status}`);
      }
      const data = await response.json();
      setToken(data.token);
      await enterMain(data);
    } catch (err) {
      showCreateError(err.message);
      btn.disabled = false;
      btn.textContent = 'Tresor anlegen';
    }
  }

  function showCreateError(msg) {
    const errorBox = $('#create-error');
    errorBox.textContent = msg;
    errorBox.hidden = false;
  }

  // -------------------- Main: Boot --------------------

  async function enterMain(sessionInfo) {
    state.sessionInfo = sessionInfo;
    $('#current-vault').textContent = sessionInfo.vault_filename;
    $('#timeout-display').textContent = Math.round(sessionInfo.inactivity_timeout_s / 60);
    $('#content-eyebrow').textContent = `Tresor · ${sessionInfo.vault_filename.replace(/\.opnvault$/i, '')}`;
    updateExpiry(sessionInfo.seconds_until_expiry);
    showScreen('main');
    applyMultiUserVisibility();
    await loadInventory();
    startHeartbeat();
    startSessionTicker();
    startRetryPolling();
    checkForUpdate();
  }

  function applyMultiUserVisibility() {
    // User-Mgmt-Button nur fuer Admins, Self-Service-PW fuer alle Multi-User.
    const isMulti = state.serverMode === 'user-db';
    const usersBtn = $('#users-open-btn');
    const pwBtn = $('#password-self-btn');
    const userBadge = $('#current-user-badge');
    const singleSwitchBtn = $('#single-switch-btn');
    if (pwBtn) pwBtn.hidden = !isMulti;
    if (usersBtn) usersBtn.hidden = true;
    if (userBadge) userBadge.hidden = !isMulti;
    // Single-Mode-Switch-Button nur im Single-Mode sichtbar
    if (singleSwitchBtn) singleSwitchBtn.hidden = isMulti;
    if (!isMulti) return;
    // Rolle aus Session via /api/users probe (200 = admin, 403 = nicht-admin).
    apiGet('/api/users').then(async (response) => {
      if (response.status === 200 && usersBtn) usersBtn.hidden = false;
      // User-Badge anzeigen — Username steht in keiner /me-Antwort, also
      // probieren wir es ueber /api/users (Admin) oder lassen es generisch.
      if (userBadge) {
        if (response.status === 200) {
          const body = await response.json();
          // Welcher User ist eingeloggt? Wir kennen nicht direkt — kennzeichnen
          // wir den als "admin".
          userBadge.textContent = `admin · ${body.users.length} User`;
        } else if (response.status === 403) {
          userBadge.textContent = 'eingeloggt';
        }
      }
    }).catch(() => {});
  }

  // -------------------- Main: Inventar laden --------------------

  async function loadInventory() {
    const response = await apiGet('/api/inventory');
    if (response.status === 401) {
      handleSessionLost();
      return;
    }
    if (!response.ok) {
      showToast('Inventar konnte nicht geladen werden.', true);
      return;
    }
    const data = await response.json();
    state.devices = data.devices || [];
    state.tags = data.tags || [];
    pruneSelectionToExistingDevices();
    renderSidebar();
    renderGrid();
    loadOutstanding().catch(() => {});
    // Firmware-Status laeuft nicht im 30s-Takt mit dem Heartbeat (das ist
    // ein authentifizierter Call und erscheint im OPNsense-Audit). Einmal
    // nach Inventar-Laden + manuelles Refresh in der Karten-Action.
    loadFirmwareStatus().catch(() => {});
    loadBackupCounts().catch(() => {});
    loadCertStatus().catch(() => {});
    refreshVaultSettingsCache().then(() => {
      if (state.vaultSettings?.drift_detection_enabled) {
        loadDriftStatus().catch(() => {});
      }
    });
  }

  async function refreshVaultSettingsCache() {
    try {
      const r = await apiGet('/api/vaults/settings');
      if (r.ok) {
        state.vaultSettings = await r.json();
      }
    } catch (_e) { /* nicht kritisch */ }
  }

  async function loadDriftStatus(deviceIds = null) {
    // Drift-Check ist Opt-In (settings.drift_detection_enabled). Wer das
    // anschaltet weiss dass pro Refresh ein API-Call pro Geraet entsteht
    // (entsprechend ein OPNsense-Audit-Eintrag) - analog zum Cert-Probe.
    try {
      const body = deviceIds ? { device_ids: deviceIds } : { device_ids: [] };
      const r = await apiPost('/api/inventory/drift-status', body);
      if (r.status === 401) { handleSessionLost(); return; }
      if (!r.ok) return;
      const data = await r.json();
      for (const entry of data.results || []) {
        state.driftByDevice[entry.device_id] = {
          drift: entry.drift_detected,
          hasBaseline: entry.has_baseline,
          summary: entry.summary,
          baselineIso: entry.baseline_backup_iso,
          baselineTrigger: entry.baseline_trigger,
        };
      }
      renderGrid();
    } catch (_e) { /* still */ }
  }

  async function loadCertStatus(deviceIds = null) {
    // Holt pro Geraet die OPNsense-Cert-Inventur. Einmal pro Inventar-
    // Load - kein 30s-Polling (Audit-Eintraege in der OPNsense).
    try {
      const body = deviceIds ? { device_ids: deviceIds } : { device_ids: [] };
      const r = await apiPost('/api/inventory/cert-status', body);
      if (r.status === 401) { handleSessionLost(); return; }
      if (!r.ok) return;
      const data = await r.json();
      for (const entry of data.results || []) {
        state.certsByDevice[entry.device_id] = {
          count: (entry.certs || []).length,
          soonestDays: entry.soonest_days,
          certs: entry.certs || [],
          summary: entry.summary,
          reachable: entry.reachable,
          authenticated: entry.authenticated,
        };
      }
      renderGrid();
    } catch (_e) { /* still */ }
  }

  async function loadBackupCounts() {
    // Pro sichtbares Geraet die Anzahl lokaler Backups holen, damit die
    // Kachel den Indikator "X Backups" zeigt. Pro Geraet ein Aufruf, aber
    // billig (Datei-Listing). Auf Bulk-Endpoint koennen wir spaeter
    // optimieren wenn's bei 100+ Geraeten merkbar wird.
    const counts = {};
    await Promise.all((state.devices || []).map(async (d) => {
      try {
        const r = await apiGet(`/api/inventory/devices/${encodeURIComponent(d.id)}/backups`);
        if (!r.ok) return;
        const data = await r.json();
        const backups = data.backups || [];
        if (backups.length > 0) {
          counts[d.id] = {
            count: backups.length,
            latestTs: backups[0].timestamp_utc,
            latestTrigger: backups[0].trigger,
          };
        }
      } catch (_e) { /* still */ }
    }));
    state.backupsByDevice = counts;
    renderGrid();
  }

  async function loadFirmwareStatus(deviceIds = null) {
    if (state.firmwareLoading) return;
    state.firmwareLoading = true;
    try {
      const body = deviceIds ? { device_ids: deviceIds } : { device_ids: [] };
      const response = await apiPost('/api/inventory/firmware-status', body);
      if (response.status === 401) { handleSessionLost(); return; }
      if (!response.ok) return;
      const data = await response.json();
      for (const r of data.results || []) {
        state.firmware[r.device_id] = {
          version: r.version,
          status: r.status,
          update_available: r.update_available,
          new_version: r.new_version || '',
          status_msg: r.status_msg || '',
          summary: r.summary,
          reachable: r.reachable,
          authenticated: r.authenticated,
          checked_at_iso: r.checked_at_iso,
        };
      }
      renderGrid();
    } catch (_e) {
      // bewusst still — Firmware-Status ist nice-to-have, kein UI-Block
    } finally {
      state.firmwareLoading = false;
    }
  }

  // -------------------- Retry-Status (Topbar-Indikator) --------------------

  let retryPollHandle = null;

  function startRetryPolling() {
    if (retryPollHandle !== null) return;
    pollRetryStatus();
    retryPollHandle = setInterval(pollRetryStatus, 20000);
  }

  function stopRetryPolling() {
    if (retryPollHandle !== null) {
      clearInterval(retryPollHandle);
      retryPollHandle = null;
    }
  }

  async function pollRetryStatus() {
    try {
      const response = await apiGet('/api/retry/status');
      if (response.status === 401) { handleSessionLost(); return; }
      if (!response.ok) return;
      const data = await response.json();
      const count = (data.jobs || []).length;
      const btn = $('#retry-indicator-btn');
      const countEl = $('#retry-indicator-count');
      btn.hidden = count === 0;
      countEl.textContent = String(count);
      // Side-effect: Outstanding neu laden, weil sich die Resultate
      // im Hintergrund veraendert haben koennten.
      if (count > 0) loadOutstanding().catch(() => {});
    } catch (_) { /* Netz-Hickup */ }
  }

  async function showRetryStatus() {
    try {
      const response = await apiGet('/api/retry/status');
      if (!response.ok) return;
      const data = await response.json();
      const jobs = data.jobs || [];
      if (!jobs.length) {
        showToast('Aktuell läuft kein Auto-Retry.');
        return;
      }
      const lines = jobs.map((j) => {
        const next = new Date(j.next_attempt_at_ms);
        const nextStr = next.toLocaleTimeString();
        return `${j.plan_id}: ${j.device_ids.length} Gerät(e), ${j.attempts} Versuche, nächster ${nextStr}`;
      });
      // Toast kann mehrere Zeilen — nutzen wir vorhandene Mechanik.
      showToast(lines.join(' · '));
    } catch (err) {
      showToast(err.message, true);
    }
  }

  async function loadOutstanding() {
    const response = await apiGet('/api/plans/outstanding');
    if (response.status === 401) { handleSessionLost(); return; }
    if (!response.ok) return;
    const data = await response.json();
    state.outstandingByDevice = {};
    for (const entry of data.devices || []) {
      state.outstandingByDevice[entry.device_id] = {
        count: entry.outstanding_count,
        plans: entry.plans,
      };
    }
    renderGrid();
  }

  // -------------------- Sidebar --------------------

  function renderSidebar() {
    const list = $('#group-list');
    list.innerHTML = '';

    const totalCount = state.devices.length;
    list.appendChild(makeGroupItem({
      label: 'Alle Geräte',
      count: totalCount,
      tag: null,
    }));

    for (const t of state.tags) {
      list.appendChild(makeGroupItem({
        label: t.name,
        count: t.count,
        tag: t.name,
      }));
    }
  }

  function makeGroupItem({ label, count, tag }) {
    const li = document.createElement('li');
    li.className = 'group-item';
    if ((tag === null && state.activeTag === null) || tag === state.activeTag) {
      li.classList.add('active');
    }
    const labelSpan = document.createElement('span');
    labelSpan.textContent = label;
    li.appendChild(labelSpan);
    const countSpan = document.createElement('span');
    countSpan.className = 'group-count';
    countSpan.textContent = String(count);
    li.appendChild(countSpan);
    li.addEventListener('click', () => {
      state.activeTag = tag;
      renderSidebar();
      renderGrid();
    });
    return li;
  }

  // -------------------- Grid + Status-Summary --------------------

  function renderGrid() {
    const grid = $('#card-grid');
    const empty = $('#empty-state');

    if (state.devices.length === 0) {
      grid.innerHTML = '';
      empty.hidden = false;
      $('#status-summary').innerHTML = '';
      $('#selection-bar').hidden = true;
      updateSelectionBar();
      return;
    }

    empty.hidden = true;
    $('#selection-bar').hidden = false;
    const visible = state.devices.filter(deviceMatchesFilter);

    grid.innerHTML = '';
    for (const device of visible) {
      grid.appendChild(renderCard(device));
    }

    renderStatusSummary(state.devices);
    updateSelectionBar();
  }

  function deviceMatchesFilter(device) {
    if (state.activeTag && !device.tags.includes(state.activeTag)) return false;
    if (state.search) {
      const q = state.search;
      const hay = [
        device.name,
        device.host,
        device.descr || '',
        ...(device.tags || []),
      ].join(' ').toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  }

  function renderCard(device) {
    const hb = state.heartbeat[device.id];
    const reachability = computeReachability(hb);
    const article = document.createElement('article');
    article.className = 'card';
    if (reachability === 'offline') article.classList.add('offline');
    if (!device.tls_verify) article.classList.add('tls-warning');
    if (state.selectedDeviceIds.has(device.id)) article.classList.add('selected');

    // Checkbox (oben rechts)
    const checkbox = document.createElement('div');
    checkbox.className = 'card-checkbox';
    checkbox.title = 'Für Aktion auswählen';
    checkbox.innerHTML = `<svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
      <path d="M2 6.5l2.5 2.5L10 3.5"/>
    </svg>`;
    checkbox.addEventListener('click', (e) => {
      e.stopPropagation();
      toggleDeviceSelection(device.id);
    });
    article.appendChild(checkbox);

    // Status row
    const row = document.createElement('div');
    row.className = 'card-status-row';
    const dot = document.createElement('span');
    dot.className = `status-dot ${reachability}`;
    row.appendChild(dot);
    const hostname = document.createElement('span');
    hostname.className = 'card-hostname';
    hostname.textContent = device.host;
    row.appendChild(hostname);
    if (!device.tls_verify) {
      const warn = document.createElement('span');
      warn.className = 'card-warning-badge';
      warn.title = 'TLS-Zertifikat wird nicht geprüft';
      warn.innerHTML = `<svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round">
        <path d="M7 1.4L2 3.2v3.5c0 3 2 5.8 5 6.7 3-.9 5-3.7 5-6.7V3.2L7 1.4z"/>
        <line x1="7" y1="5" x2="7" y2="7.5"/>
        <circle cx="7" cy="9.4" r="0.55" fill="currentColor"/>
      </svg>`;
      row.appendChild(warn);
    }
    // Quick-Action-Icons: erscheinen auf Hover. Container damit wir das
    // ganze Set ueber CSS einblenden koennen.
    const quickActions = document.createElement('div');
    quickActions.className = 'card-quick-actions';

    const openLink = document.createElement('a');
    openLink.className = 'card-quick-btn';
    openLink.href = `https://${device.host}:${device.port}/`;
    openLink.target = '_blank';
    openLink.title = 'OPNsense-Weboberfläche öffnen';
    openLink.innerHTML = `<svg width="15" height="15" viewBox="0 0 13 13" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
      <path d="M6.5 2H3a1 1 0 00-1 1v7a1 1 0 001 1h7a1 1 0 001-1V6.5"/>
      <path d="M8 1.5h3.5V5"/>
      <line x1="6" y1="7" x2="11.5" y2="1.5"/>
    </svg>`;
    openLink.addEventListener('click', (e) => {
      e.stopPropagation();
      e.preventDefault();
      openWebUrl(`https://${device.host}:${device.port}/`);
    });
    quickActions.appendChild(openLink);

    const fwQuickBtn = document.createElement('button');
    fwQuickBtn.type = 'button';
    fwQuickBtn.className = 'card-quick-btn';
    fwQuickBtn.title = 'Updates suchen';
    fwQuickBtn.innerHTML = `<svg width="15" height="15" viewBox="0 0 13 13" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
      <path d="M11 6.5A4.5 4.5 0 0 1 2.7 9"/>
      <polyline points="2.5 11 2.5 9 4.5 9"/>
      <path d="M2 6.5A4.5 4.5 0 0 1 10.3 4"/>
      <polyline points="10.5 2 10.5 4 8.5 4"/>
    </svg>`;
    fwQuickBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      doFirmwareCheckForDevice(device.id);
    });
    quickActions.appendChild(fwQuickBtn);

    const dupQuickBtn = document.createElement('button');
    dupQuickBtn.type = 'button';
    dupQuickBtn.className = 'card-quick-btn';
    dupQuickBtn.title = 'Geraet duplizieren';
    dupQuickBtn.innerHTML = `<svg width="15" height="15" viewBox="0 0 13 13" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
      <rect x="3.5" y="3.5" width="7" height="7" rx="1"/>
      <path d="M2 8.5V2.5a1 1 0 011-1H9"/>
    </svg>`;
    dupQuickBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      duplicateDeviceById(device.id);
    });
    quickActions.appendChild(dupQuickBtn);

    row.appendChild(quickActions);
    article.appendChild(row);

    // Name
    const name = document.createElement('div');
    name.className = 'card-name';
    name.textContent = device.name;
    article.appendChild(name);

    // Outstanding-Badge: zeigt wie viele Plans noch offen sind
    const outstanding = state.outstandingByDevice[device.id];
    if (outstanding && outstanding.count > 0) {
      const badge = document.createElement('button');
      badge.type = 'button';
      badge.className = 'card-outstanding-badge';
      badge.title = 'Offene Aktionen — klicken zum Nachziehen';
      badge.innerHTML = `<svg width="11" height="11" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
        <circle cx="6" cy="6" r="4.5"/>
        <line x1="6" y1="4" x2="6" y2="6.5"/>
        <circle cx="6" cy="8.3" r="0.5" fill="currentColor"/>
      </svg><span>${outstanding.count} offen</span>`;
      badge.addEventListener('click', (e) => {
        e.stopPropagation();
        openRetryForDevice(device.id, outstanding.plans[0]);
      });
      article.appendChild(badge);
    }

    // Tags
    if (device.tags && device.tags.length) {
      const tagsRow = document.createElement('div');
      tagsRow.className = 'card-tags';
      for (const t of device.tags) {
        const tag = document.createElement('span');
        tag.className = 'tag';
        tag.textContent = t;
        tagsRow.appendChild(tag);
      }
      article.appendChild(tagsRow);
    }

    // (Stats-Zeile "Port/TLS/Heartbeat-Alter" bewusst entfernt - die Info
    // ist im Detail-Modal sichtbar, auf der Kachel war sie nur Rauschen.
    // TLS-Risiko wird weiterhin oben durch das card-warning-badge angezeigt.)

    // Firmware-Zeile (nur wenn Daten da sind — sonst leiser Platzhalter
    // damit das Karten-Layout nicht springt wenn die Daten nachgeladen
    // werden).
    const fw = state.firmware[device.id];
    if (fw && fw.version && fw.version !== 'unknown') {
      const fwRow = document.createElement('div');
      fwRow.className = 'card-firmware';
      const label = document.createElement('span');
      label.className = 'card-firmware-label';
      label.textContent = 'OPNsense';
      const value = document.createElement('span');
      value.className = 'card-firmware-version';
      value.textContent = fw.version;
      fwRow.appendChild(label);
      fwRow.appendChild(value);
      if (fw.update_available) {
        const badge = document.createElement('span');
        badge.className = 'card-firmware-update';
        // Wenn OPNsense uns die Zielversion verraet, zeigen wir die direkt
        // - sonst der Generik-Text wie bisher.
        badge.textContent = fw.new_version
          ? `Update v${fw.new_version}`
          : 'Update verfuegbar';
        // OPNsense-eigene Beschreibung als Tooltip - bewusst kein Modal,
        // calm-precision-Linie. Fallback auf die Generik-Zeile.
        const tooltip = fw.status_msg || (fw.new_version
          ? `Update von v${fw.version} auf v${fw.new_version} verfuegbar`
          : 'Update verfuegbar');
        badge.title = tooltip;
        fwRow.appendChild(badge);
      }
      article.appendChild(fwRow);
    }

    // Cert-Ablauf-Indikator - nur sichtbar wenn das fruehste Cert
    // weniger als 30 Tage Restlaufzeit hat. <7 Tage = rot, <30 = gelb.
    // Hinter Ablauf = rot mit Minus-Tagen ("Cert abgelaufen vor 12d").
    const certInfo = state.certsByDevice[device.id];
    if (certInfo && certInfo.soonestDays !== null && certInfo.soonestDays !== undefined
        && certInfo.soonestDays < 30) {
      const certBadge = document.createElement('button');
      certBadge.type = 'button';
      const severity = certInfo.soonestDays < 7 ? 'critical' : 'warning';
      certBadge.className = `card-cert-badge card-cert-badge-${severity}`;
      const days = certInfo.soonestDays;
      let label;
      if (days < 0) {
        label = `Cert abgelaufen (${Math.abs(days)}d)`;
      } else if (days === 0) {
        label = 'Cert laeuft heute ab';
      } else if (days === 1) {
        label = 'Cert laeuft morgen ab';
      } else {
        label = `Cert ${days}d`;
      }
      certBadge.title = (
        `${certInfo.count} Cert(s) inventarisiert. Klick fuer Details.`
      );
      certBadge.innerHTML = `<svg width="11" height="11" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
        <path d="M6 1l4 1.5v3c0 2.6-1.7 5-4 5.7-2.3-.7-4-3.1-4-5.7v-3L6 1z"/>
        <path d="M4.5 6L5.7 7.2 8 4.5"/>
      </svg><span>${label}</span>`;
      certBadge.addEventListener('click', (e) => {
        e.stopPropagation();
        openCertDetailModal(device.id);
      });
      article.appendChild(certBadge);
    }

    // Backup-Indikator - nur sichtbar wenn lokal Backups existieren
    // (Design-Constraint: "ansprechend und simpel zugleich"; Zero-State
    // wird bewusst nicht angezeigt um die Kachel ruhig zu halten).
    const backupInfo = state.backupsByDevice[device.id];
    if (backupInfo && backupInfo.count > 0) {
      const backupBadge = document.createElement('button');
      backupBadge.type = 'button';
      backupBadge.className = 'card-backup-badge';
      backupBadge.title = (
        `${backupInfo.count} Backup(s) lokal gespeichert. `
        + `Neuestes: ${backupInfo.latestTs} (${backupInfo.latestTrigger})`
      );
      backupBadge.innerHTML = `<svg width="11" height="11" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
        <rect x="2" y="3" width="8" height="6.5" rx="0.8"/>
        <line x1="2" y1="5" x2="10" y2="5"/>
      </svg><span>${backupInfo.count} Backup${backupInfo.count === 1 ? '' : 's'}</span>`;
      backupBadge.addEventListener('click', (e) => {
        e.stopPropagation();
        openBackupHistoryModal(device.id);
      });
      article.appendChild(backupBadge);
    }

    // Drift-Indikator - nur wenn drift_detection_enabled UND tatsaechlich
    // Drift erkannt wurde. has_baseline=false / drift=null gibt KEIN
    // Badge (Zero-State ist still, sonst wirkt jede frisch hinzugefuegte
    // Box rot bevor sie ein Backup hat).
    const driftInfo = state.driftByDevice[device.id];
    if (driftInfo && driftInfo.drift === true) {
      const driftBadge = document.createElement('button');
      driftBadge.type = 'button';
      driftBadge.className = 'card-drift-badge';
      driftBadge.title = driftInfo.summary
        || 'Live-Config weicht vom letzten Backup ab.';
      driftBadge.innerHTML = `<svg width="11" height="11" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">
        <path d="M6 2v4"/>
        <circle cx="6" cy="9" r="0.4" fill="currentColor"/>
      </svg><span>Drift</span>`;
      driftBadge.addEventListener('click', (e) => {
        e.stopPropagation();
        showToast(driftInfo.summary || 'Drift erkannt.');
      });
      article.appendChild(driftBadge);
    }

    article.addEventListener('click', () => openDeviceModal(device.id));
    return article;
  }

  function computeReachability(hb) {
    if (!hb) return 'checking';
    const age = Date.now() - hb.checked_at_ms;
    if (age > HEARTBEAT_STALE_AFTER_MS) return 'checking';
    return hb.reachable ? 'online' : 'offline';
  }

  function formatHeartbeatLabel(hb, reach) {
    if (!hb) return 'prüfe…';
    if (reach === 'checking') return 'prüfe…';
    const age = Math.round((Date.now() - hb.checked_at_ms) / 1000);
    if (age < 5) return 'gerade eben';
    if (age < 60) return `vor ${age} s`;
    const mins = Math.round(age / 60);
    return `vor ${mins} min`;
  }

  // -------------------- Selektion --------------------

  function toggleDeviceSelection(deviceId) {
    if (state.selectedDeviceIds.has(deviceId)) {
      state.selectedDeviceIds.delete(deviceId);
    } else {
      state.selectedDeviceIds.add(deviceId);
    }
    renderGrid();
  }

  function selectAllDevices() {
    state.selectedDeviceIds = new Set(state.devices.map((d) => d.id));
    renderGrid();
  }

  function selectNoDevices() {
    state.selectedDeviceIds = new Set();
    renderGrid();
  }

  function selectReachableDevices() {
    state.selectedDeviceIds = new Set(
      state.devices
        .filter((d) => computeReachability(state.heartbeat[d.id]) === 'online')
        .map((d) => d.id),
    );
    renderGrid();
  }

  function updateSelectionBar() {
    const n = state.selectedDeviceIds.size;
    const total = state.devices.length;
    const label = n === 0
      ? `0 ausgewählt · ${total} insgesamt`
      : n === total
        ? `alle ${n} ausgewählt`
        : `${n} von ${total} ausgewählt`;
    const el = $('#selection-count');
    el.textContent = label;
    el.classList.toggle('has-selection', n > 0);
    // Vergleichs-Button nur ab 2 ausgewaehlten Geraeten sichtbar
    const cmpBtn = $('#sel-compare');
    if (cmpBtn) cmpBtn.hidden = n < 2;
  }

  // -------------------- Config-Compare-Modal --------------------

  let currentCompareSubsystem = 'aliases';

  async function openCompareModal(subsystem = 'aliases') {
    const ids = Array.from(state.selectedDeviceIds);
    if (ids.length < 2) {
      showToast('Mindestens 2 Geräte für den Vergleich auswählen.');
      return;
    }
    currentCompareSubsystem = subsystem;
    // Aufklapp + Spalten-Reihenfolge sind subsystem-spezifisch (Row-Keys
    // unterscheiden sich) - bei jedem Wechsel resetten.
    compareExpandedRows.clear();
    compareColumnOrder = [];
    // Tab-Highlight + Modal-Titel anpassen
    document.querySelectorAll('.compare-tab').forEach((btn) => {
      btn.classList.toggle('is-active', btn.dataset.subsystem === subsystem);
    });
    $('#compare-modal').hidden = false;
    $('#cmp-status').textContent = 'Lade Vergleich…';
    $('#cmp-head').innerHTML = '';
    $('#cmp-body').innerHTML = '';
    try {
      const r = await apiPost('/api/inventory/compare', {
        device_ids: ids, subsystem,
      });
      if (r.status === 401) { handleSessionLost(); return; }
      if (!r.ok) {
        const body = await r.json().catch(() => ({}));
        $('#cmp-status').textContent = body.detail || `Fehler ${r.status}`;
        return;
      }
      const data = await r.json();
      renderCompareTable(data);
    } catch (err) {
      $('#cmp-status').textContent = err.message;
    }
  }

  function closeCompareModal() {
    $('#compare-modal').hidden = true;
  }

  // Master-Auswahl erfolgt jetzt via ◀▶★-Buttons in den Spalten-Headern
  // der Compare-Matrix (siehe renderCompareTableInner). Master = linkeste
  // Spalte, optisch hervorgehoben. doSyncAlias ist der Endpoint, das
  // Picker-Modal davor entfaellt.

  async function doSyncAlias(aliasName, masterId, targetIds) {
    try {
      const r = await apiPost('/api/inventory/compare/sync-aliases', {
        master_device_id: masterId,
        target_device_ids: targetIds,
        alias_name: aliasName,
      });
      if (r.status === 401) { handleSessionLost(); return; }
      if (!r.ok) {
        const body = await r.json().catch(() => ({}));
        showToast(body.detail || `Sync fehlgeschlagen (${r.status}).`);
        return;
      }
      const data = await r.json();
      showToast(`Plan erzeugt: ${data.source_summary} → ${data.target_count} Gerät(e)`);
      closeCompareModal();
      // In die Vorschau-Phase des Plan-Modals springen — Plan ist
      // server-seitig schon befuellt, User muss nur reviewen + applyen.
      await openExistingPlanInPreview(data.plan_id);
    } catch (err) {
      showToast(`Sync fehlgeschlagen: ${err.message}`);
    }
  }

  async function openExistingPlanInPreview(planId) {
    // Holt einen bereits erzeugten Plan und oeffnet den Plan-Modal direkt
    // in der Vorschau-Phase (statt im leeren Input-Form). Wird vom Sync-
    // Pfad aus der Compare-Matrix gebraucht.
    try {
      const response = await apiGet(`/api/plans/${encodeURIComponent(planId)}`);
      if (response.status === 401) { handleSessionLost(); return; }
      if (!response.ok) {
        showToast('Plan nicht abrufbar — vielleicht wurde er gelöscht.', true);
        return;
      }
      currentPlan = await response.json();
      planMode = currentPlan.subsystem === 'routes' ? 'route' : 'alias';
      retryDeviceIds = null;
      planPhase = 'preview';
      $('#plan-modal-title').textContent = 'Sync: Plan-Vorschau';
      $('#plan-modal-error').hidden = true;
      $('#plan-preview-error').hidden = true;
      $('#plan-modal').hidden = false;
      renderPreview(currentPlan);
      showPlanPhase('preview');
    } catch (err) {
      showToast(err.message, true);
    }
  }

  // Compare-State: aktuelle Spalten-Reihenfolge + Detail-Auftaster pro Row
  let currentCompareData = null;
  let compareColumnOrder = [];  // device_ids in displayed order, [0] = Master
  const compareExpandedRows = new Set(); // row.name -> expanded

  function renderCompareTable(data) {
    currentCompareData = data;
    if (!compareColumnOrder.length
        || compareColumnOrder.length !== data.columns.length
        || !compareColumnOrder.every((id) => data.columns.some((c) => c.device_id === id))) {
      // Erste Anzeige oder Geraete-Set hat sich geaendert -> Default-Order
      compareColumnOrder = data.columns.map((c) => c.device_id);
    }
    compareExpandedRows.clear();
    renderCompareTableInner();
  }

  function renderCompareTableInner() {
    const data = currentCompareData;
    if (!data) return;
    const colsById = Object.fromEntries(data.columns.map((c) => [c.device_id, c]));
    const orderedCols = compareColumnOrder.map((id) => colsById[id]).filter(Boolean);
    const masterId = orderedCols[0]?.device_id || null;

    $('#cmp-status').textContent = `${data.summary} (Master: ${colsById[masterId]?.device_name || '—'})`;
    const head = $('#cmp-head');
    const body = $('#cmp-body');
    head.innerHTML = '';
    body.innerHTML = '';

    // Kopf: 1. Spalte = Alias-Name + Detail-Spalte, dann pro Geraet eine Spalte
    const th0 = document.createElement('th');
    th0.textContent = 'Alias';
    th0.style.width = '180px';
    head.appendChild(th0);
    orderedCols.forEach((col, idx) => {
      const th = document.createElement('th');
      if (idx === 0) th.classList.add('cmp-col-master');
      const wrap = document.createElement('div');
      wrap.className = 'cmp-col-head';
      const name = document.createElement('span');
      name.className = 'cmp-col-name';
      name.textContent = col.device_name;
      name.title = col.reachable ? 'erreichbar' : `nicht erreichbar: ${col.summary}`;
      if (!col.reachable) name.style.color = 'var(--text-subtle)';
      wrap.appendChild(name);
      if (idx === 0) {
        const masterTag = document.createElement('span');
        masterTag.className = 'cmp-master-tag';
        masterTag.textContent = '★ Master';
        wrap.appendChild(masterTag);
      }
      // Steuerung: nach links / als Master
      const ctrls = document.createElement('div');
      ctrls.className = 'cmp-col-ctrls';
      if (idx > 0) {
        const leftBtn = document.createElement('button');
        leftBtn.type = 'button';
        leftBtn.className = 'cmp-arrow-btn';
        leftBtn.textContent = '◀';
        leftBtn.title = 'Diese Spalte nach links verschieben';
        leftBtn.addEventListener('click', () => moveCompareColumn(idx, idx - 1));
        ctrls.appendChild(leftBtn);
      }
      if (idx > 0) {
        const masterBtn = document.createElement('button');
        masterBtn.type = 'button';
        masterBtn.className = 'cmp-arrow-btn cmp-master-btn';
        masterBtn.textContent = '★';
        masterBtn.title = 'Diese Spalte zum Master machen (an Position 1)';
        masterBtn.addEventListener('click', () => moveCompareColumn(idx, 0));
        ctrls.appendChild(masterBtn);
      }
      if (idx < orderedCols.length - 1) {
        const rightBtn = document.createElement('button');
        rightBtn.type = 'button';
        rightBtn.className = 'cmp-arrow-btn';
        rightBtn.textContent = '▶';
        rightBtn.title = 'Diese Spalte nach rechts verschieben';
        rightBtn.addEventListener('click', () => moveCompareColumn(idx, idx + 1));
        ctrls.appendChild(rightBtn);
      }
      wrap.appendChild(ctrls);
      th.appendChild(wrap);
      head.appendChild(th);
    });

    if (data.rows.length === 0) {
      const tr = document.createElement('tr');
      const td = document.createElement('td');
      td.colSpan = orderedCols.length + 1;
      td.style.textAlign = 'center';
      td.style.color = 'var(--text-subtle)';
      td.style.padding = '20px';
      td.textContent = 'Nichts zu vergleichen.';
      tr.appendChild(td);
      body.appendChild(tr);
      return;
    }

    for (const row of data.rows) {
      const tr = document.createElement('tr');
      if (!row.uniform) tr.classList.add('cmp-row-drift');
      const cellsByDevice = Object.fromEntries(row.cells.map((c) => [c.device_id, c]));

      // Name-Spalte: Aufklapp-Icon + Name + Sync-Button
      const nameTd = document.createElement('td');
      const expandBtn = document.createElement('button');
      expandBtn.type = 'button';
      expandBtn.className = 'cmp-expand-btn';
      const isExpanded = compareExpandedRows.has(row.name);
      expandBtn.textContent = isExpanded ? '▼' : '▶';
      expandBtn.title = isExpanded ? 'Details ausblenden' : 'Inhalte anzeigen';
      expandBtn.addEventListener('click', () => {
        if (compareExpandedRows.has(row.name)) compareExpandedRows.delete(row.name);
        else compareExpandedRows.add(row.name);
        renderCompareTableInner();
      });
      nameTd.appendChild(expandBtn);
      const nameSpan = document.createElement('span');
      nameSpan.textContent = row.name;
      nameSpan.style.fontWeight = '600';
      nameSpan.style.marginLeft = '6px';
      nameTd.appendChild(nameSpan);
      // Sync-Button (Master = Spalte 0). Nur sichtbar wenn Master einen
      // Wert hat UND es Drift / Absent-Cells gibt.
      if (!row.uniform && data.subsystem === 'aliases') {
        const masterCell = cellsByDevice[masterId];
        if (masterCell && masterCell.status === 'present') {
          const syncBtn = document.createElement('button');
          syncBtn.type = 'button';
          syncBtn.className = 'btn-link compare-sync-btn';
          syncBtn.textContent = 'Sync ←';
          syncBtn.title = `'${colsById[masterId].device_name}' als Master uebernehmen`;
          syncBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            const targets = orderedCols.slice(1).map((c) => c.device_id);
            const ok = confirm(
              `Alias '${row.name}' von '${colsById[masterId].device_name}' auf ` +
              `${targets.length} Gerät(e) uebertragen?\n\n` +
              targets.map((tid) => `· ${colsById[tid]?.device_name || tid}`).join('\n')
              + '\n\nEin Plan wird erzeugt, du kannst ihn vor dem Apply pruefen.',
            );
            if (!ok) return;
            doSyncAlias(row.name, masterId, targets);
          });
          nameTd.appendChild(syncBtn);
        }
      }
      tr.appendChild(nameTd);

      // Fingerprint-Map: gleicher fp -> gleicher Inhalt; unterschiedlich -> drift
      const fps = new Set(row.cells
        .filter((c) => c.status === 'present')
        .map((c) => c.content_fingerprint));
      const hasDrift = fps.size > 1;
      const masterFp = cellsByDevice[masterId]?.content_fingerprint || null;

      for (const col of orderedCols) {
        const cell = cellsByDevice[col.device_id];
        const td = document.createElement('td');
        if (col.device_id === masterId) td.classList.add('cmp-col-master');
        const wrap = document.createElement('span');
        wrap.className = 'compare-cell';
        const dot = document.createElement('span');
        dot.className = 'cmp-dot';
        let label = '';
        let title = '';
        if (cell.status === 'present') {
          // Master-relativer Drift: gleich wie Master = gruen, ungleich = gelb
          const matchesMaster = masterFp !== null && cell.content_fingerprint === masterFp;
          dot.classList.add(matchesMaster ? 'cmp-dot-present' : 'cmp-dot-drift');
          if (!matchesMaster && hasDrift) {
            // markiere als drift
          }
          // Subsystem-spezifischer Cell-Label: bei Aliases "N type", bei
          // Routes "via gw", bei Rules nur die Action.
          if (data.subsystem === 'routes') {
            label = `via ${cell.type}`;
          } else if (data.subsystem === 'rules') {
            label = cell.type || '?';
          } else {
            label = `${cell.content_count} ${cell.type}`;
          }
          title = (
            `${data.subsystem === 'aliases' ? 'Typ' : (data.subsystem === 'routes' ? 'Gateway' : 'Action')}: ${cell.type}\n` +
            (data.subsystem === 'aliases' ? `Eintraege: ${cell.content_count}\n` : '') +
            `Fingerprint: ${cell.content_fingerprint}\n` +
            (cell.description ? `Beschreibung: ${cell.description}` : '')
          );
        } else if (cell.status === 'absent') {
          dot.classList.add('cmp-dot-absent');
          label = '—';
          title = 'Nicht vorhanden';
        } else {
          dot.classList.add('cmp-dot-unreachable');
          label = '?';
          title = 'Gerät nicht erreichbar';
        }
        const text = document.createElement('span');
        text.textContent = label;
        text.title = title;
        wrap.appendChild(dot);
        wrap.appendChild(text);
        td.appendChild(wrap);
        tr.appendChild(td);
      }
      body.appendChild(tr);

      // Detail-Aufklapp-Zeile
      if (compareExpandedRows.has(row.name)) {
        const detail = document.createElement('tr');
        detail.className = 'cmp-detail-row';
        // Erste Spalte (leer + Stil) damit Layout passt
        const lead = document.createElement('td');
        detail.appendChild(lead);
        for (const col of orderedCols) {
          const cell = cellsByDevice[col.device_id];
          const td = document.createElement('td');
          if (col.device_id === masterId) td.classList.add('cmp-col-master');
          const box = document.createElement('div');
          box.className = 'cmp-detail-content';
          if (cell.status !== 'present') {
            box.textContent = cell.status === 'absent' ? '(nicht vorhanden)' : '(unerreichbar)';
            box.style.color = 'var(--text-subtle)';
            box.style.fontStyle = 'italic';
          } else if (!cell.content || cell.content.length === 0) {
            box.textContent = '(leer)';
            box.style.color = 'var(--text-subtle)';
            box.style.fontStyle = 'italic';
          } else {
            box.textContent = cell.content.join('\n');
          }
          td.appendChild(box);
          detail.appendChild(td);
        }
        body.appendChild(detail);
      }
    }
  }

  function moveCompareColumn(fromIdx, toIdx) {
    if (fromIdx === toIdx || fromIdx < 0 || toIdx < 0) return;
    if (fromIdx >= compareColumnOrder.length || toIdx >= compareColumnOrder.length) return;
    const [item] = compareColumnOrder.splice(fromIdx, 1);
    compareColumnOrder.splice(toIdx, 0, item);
    renderCompareTableInner();
  }

  function pruneSelectionToExistingDevices() {
    const ids = new Set(state.devices.map((d) => d.id));
    for (const sid of Array.from(state.selectedDeviceIds)) {
      if (!ids.has(sid)) state.selectedDeviceIds.delete(sid);
    }
  }

  function renderStatusSummary(devices) {
    const summary = $('#status-summary');
    let online = 0, offline = 0, checking = 0;
    let tlsRisk = 0;
    for (const d of devices) {
      const r = computeReachability(state.heartbeat[d.id]);
      if (r === 'online') online += 1;
      else if (r === 'offline') offline += 1;
      else checking += 1;
      if (!d.tls_verify) tlsRisk += 1;
    }

    const parts = [];
    parts.push(summaryItem('online', online, 'erreichbar'));
    parts.push(`<div class="status-summary-separator"></div>`);
    parts.push(summaryItem('offline', offline, 'offline'));
    if (checking) {
      parts.push(`<div class="status-summary-separator"></div>`);
      parts.push(summaryItem('checking', checking, 'Prüfung läuft'));
    }
    if (tlsRisk) {
      parts.push(`<div class="status-summary-separator"></div>`);
      parts.push(summaryItem('warn', tlsRisk, 'TLS-Risiko'));
    }
    summary.innerHTML = parts.join('');
  }

  function summaryItem(kind, n, label) {
    const dotPart = (kind === 'warn')
      ? ''
      : `<span class="status-dot ${kind}" style="width:6px;height:6px;box-shadow:none"></span>`;
    return `
      <div class="status-summary-item ${kind}">
        ${dotPart}<strong>${n}</strong><span>${label}</span>
      </div>
    `;
  }

  // -------------------- Heartbeat-Polling --------------------

  function startHeartbeat() {
    if (heartbeatHandle !== null) return;
    pollHeartbeat();
    heartbeatHandle = setInterval(pollHeartbeat, HEARTBEAT_INTERVAL_MS);
  }

  function stopHeartbeat() {
    if (heartbeatHandle !== null) {
      clearInterval(heartbeatHandle);
      heartbeatHandle = null;
    }
  }

  async function pollHeartbeat() {
    if (state.heartbeatInFlight) return;
    if (!state.devices.length) return;
    state.heartbeatInFlight = true;
    try {
      const response = await apiPost('/api/inventory/heartbeat', { device_ids: [], timeout_s: 2.5 });
      if (response.status === 401) {
        handleSessionLost();
        return;
      }
      if (!response.ok) return;
      const data = await response.json();
      const now = Date.now();
      for (const entry of data.results) {
        state.heartbeat[entry.device_id] = {
          reachable: entry.reachable,
          checked_at_ms: now,
        };
      }
      renderGrid();
    } catch (_) {
      // Netzwerk-Hickup, nächster Tick versucht erneut.
    } finally {
      state.heartbeatInFlight = false;
    }
  }

  // -------------------- Session-Ticker --------------------

  function startSessionTicker() {
    if (sessionTickHandle !== null) return;
    sessionTickHandle = setInterval(async () => {
      try {
        const response = await apiGet('/api/auth/me');
        if (response.status === 401) { handleSessionLost(); return; }
        if (!response.ok) return;
        const data = await response.json();
        state.sessionInfo = data;
        updateExpiry(data.seconds_until_expiry);
      } catch (_) { /* Netzwerk-Hickup */ }
    }, SESSION_TICK_MS);
  }

  function stopSessionTicker() {
    if (sessionTickHandle !== null) {
      clearInterval(sessionTickHandle);
      sessionTickHandle = null;
    }
  }

  function updateExpiry(seconds) {
    const el = $('#expiry-display');
    if (!el) return;
    if (!seconds || seconds <= 0) { el.textContent = '00:00'; return; }
    const mins = Math.floor(seconds / 60);
    const secs = Math.floor(seconds % 60);
    el.textContent = `${String(mins).padStart(2, '0')}:${String(secs).padStart(2, '0')}`;
  }

  async function handleSessionLost() {
    stopHeartbeat();
    stopSessionTicker();
    stopRetryPolling();
    clearToken();
    state.devices = [];
    state.heartbeat = {};
    state.sessionInfo = null;
    // Bootstrap-Status frisch ziehen: in Multi-User-Server bleibt der
    // zentrale Vault offen wenn die Session weg ist (status='ready'),
    // dann braucht der naechste User nur Username + Passwort, NICHT den
    // Vault erneut entsperren. Single-User landet wie bisher beim Picker.
    try {
      await fetchBootstrapStatus();
    } catch (_) {
      // Status nicht abrufbar: defensive auf Picker fallen.
    }
    const s = state.bootstrapStatus;
    if (s === 'ready' && state.serverMode === 'user-db') {
      showScreen('login');
      showLoginView('multi-user');
      setTimeout(() => { const el = $('#mu-username'); if (el) el.focus(); }, 0);
    } else if (s === 'needs-admin' || s === 'needs-vault-unlock') {
      showScreen('setup');
      showLoginView('setup-vault');
      setTimeout(() => { const el = $('#su-admin-pw'); if (el) el.focus(); }, 0);
    } else {
      showScreen('login');
      showLoginView('picker');
      fetchVaultsAndPopulate().catch(() => {});
    }
  }

  async function doLock() {
    // Confirm-Gate: schuetzt gegen versehentliche Klicks aus dem
    // Topbar-Icon (z.B. wenn Browser-Extensions wie LastPass ein
    // Save-Password-Popup an derselben Stelle einblenden).
    const msg = state.serverMode === 'user-db'
      ? 'Session beenden? Du musst dich danach mit Username + Passwort neu einloggen.'
      : 'Vault sperren? Du musst dich danach mit Master-Passwort neu entsperren.';
    if (!confirm(msg)) return;
    try { await apiPost('/api/auth/lock'); } catch (_) {}
    handleSessionLost();
  }

  // -------------------- Add/Edit-Modal --------------------
  //
  // Das Add-Modal dient auch als Edit-Modal. Der Modus steckt im state:
  //   modalMode === 'add'  -> POST /api/inventory/devices, alles required
  //   modalMode === 'edit' -> PATCH /api/inventory/devices/{id}, Keys optional

  let modalMode = 'add';
  let editingDeviceId = null;

  function openAddModal(prefill) {
    const data = prefill || {};
    modalMode = 'add';
    editingDeviceId = null;
    $('#ad-name').value = data.name || '';
    $('#ad-host').value = data.host || '';
    $('#ad-port').value = String(data.port || 443);
    $('#ad-tags').value = (data.tags || []).join(', ');
    $('#ad-descr').value = data.descr || '';
    $('#ad-tls').checked = data.tls_verify !== undefined ? data.tls_verify : true;
    $('#ad-apikey').value = '';
    $('#ad-apisecret').value = '';
    $('#ad-apikey').placeholder = '';
    $('#ad-apisecret').placeholder = '';
    $('#ad-credentials-hint').hidden = true;
    $('#add-modal-title').textContent = data.duplicateOf
      ? `„${data.duplicateOf}" duplizieren`
      : 'Gerät hinzufügen';
    $('#add-modal-confirm').textContent = 'Hinzufügen';
    $('#add-modal-error').hidden = true;
    $('#add-modal').hidden = false;
    setTimeout(() => $('#ad-name').focus(), 0);
  }

  // Beim Edit-Open vom Server geladener Key - wird beim Submit-Vergleich
  // genutzt: nur wenn der User wirklich was geaendert hat, schicken wir
  // api_key mit; sonst kein PATCH-Wert (vermeidet sinnlosen Schreibvorgang
  // + Audit-Rauschen).
  let prefilledApiKey = '';

  function openEditModal(device) {
    modalMode = 'edit';
    editingDeviceId = device.id;
    prefilledApiKey = '';
    $('#ad-name').value = device.name;
    $('#ad-host').value = device.host;
    $('#ad-port').value = String(device.port);
    $('#ad-tags').value = (device.tags || []).join(', ');
    $('#ad-descr').value = device.descr || '';
    $('#ad-tls').checked = device.tls_verify;
    $('#ad-apikey').value = '';
    $('#ad-apisecret').value = '';
    $('#ad-apikey').placeholder = 'Lade…';
    $('#ad-apisecret').placeholder = '(unverändert)';
    $('#ad-credentials-hint').hidden = false;
    $('#add-modal-title').textContent = `„${device.name}" bearbeiten`;
    $('#add-modal-confirm').textContent = 'Speichern';
    $('#add-modal-error').hidden = true;
    $('#add-modal').hidden = false;
    setTimeout(() => $('#ad-name').focus(), 0);
    // API-Key sichtbar vorladen, damit der Admin verifizieren kann was
    // im Tresor steht (Secret bleibt unsichtbar). Server-Aufruf laeuft
    // im Hintergrund - bei Fehler placeholder zurueck auf "(unveraendert)".
    loadDeviceApiKeyIntoEditForm(device.id);
  }

  async function loadDeviceApiKeyIntoEditForm(deviceId) {
    try {
      const response = await apiGet(
        `/api/inventory/devices/${encodeURIComponent(deviceId)}/api-key`
      );
      if (response.status === 401) { handleSessionLost(); return; }
      if (!response.ok) {
        $('#ad-apikey').placeholder = '(unverändert)';
        return;
      }
      const data = await response.json();
      // Modal koennte in der Zwischenzeit geschlossen worden sein
      if (modalMode !== 'edit' || editingDeviceId !== deviceId) return;
      prefilledApiKey = data.api_key || '';
      $('#ad-apikey').value = prefilledApiKey;
      $('#ad-apikey').placeholder = '';
    } catch (_e) {
      $('#ad-apikey').placeholder = '(unverändert)';
    }
  }

  function closeAddModal() {
    $('#add-modal').hidden = true;
    modalMode = 'add';
    editingDeviceId = null;
  }

  async function doAddOrEditDevice() {
    const errorBox = $('#add-modal-error');
    errorBox.hidden = true;

    const name = $('#ad-name').value.trim();
    const host = $('#ad-host').value.trim();
    const portRaw = $('#ad-port').value;
    const tagsRaw = $('#ad-tags').value.trim();
    const descr = $('#ad-descr').value.trim();
    const tlsVerify = $('#ad-tls').checked;
    const apiKey = $('#ad-apikey').value.trim();
    // Secret genauso trimmen wie Key. apikey.txt-Pastes haben oft
    // trailing-Whitespace/Newline; OPNsense's Auth-Vergleich ist
    // bytewise und kippt dann mit "Authentication failed".
    const apiSecret = $('#ad-apisecret').value.trim();

    if (modalMode === 'add') {
      if (!name || !host || !apiKey || !apiSecret) {
        return showAddError('Bitte Name, Hostname, API-Key und API-Secret ausfüllen.');
      }
    } else {
      // Edit: Name + Host bleiben Pflicht, Keys optional
      if (!name || !host) {
        return showAddError('Name und Hostname sind Pflichtfelder.');
      }
    }
    const port = parseInt(portRaw, 10);
    if (!port || port < 1 || port > 65535) {
      return showAddError('Port muss zwischen 1 und 65535 liegen.');
    }
    const tags = tagsRaw
      ? tagsRaw.split(',').map((t) => t.trim()).filter((t) => t.length > 0)
      : [];

    const btn = $('#add-modal-confirm');
    const originalLabel = btn.textContent;
    btn.disabled = true;
    btn.textContent = 'Speichere…';
    try {
      let response;
      if (modalMode === 'add') {
        response = await apiPost('/api/inventory/devices', {
          name, host, port,
          tls_verify: tlsVerify,
          tags, descr,
          api_key: apiKey,
          api_secret: apiSecret,
        });
      } else {
        const body = {
          name, host, port,
          tls_verify: tlsVerify,
          tags, descr,
        };
        // Key nur senden wenn der User ihn wirklich geaendert hat - der
        // Edit-Dialog laedt den aktuellen Key vor, sodass die meisten
        // Saves den Key unveraendert lassen wuerden.
        if (apiKey && apiKey !== prefilledApiKey) body.api_key = apiKey;
        if (apiSecret) body.api_secret = apiSecret;
        response = await apiPatch(
          `/api/inventory/devices/${encodeURIComponent(editingDeviceId)}`,
          body,
        );
      }
      if (response.status === 401) { handleSessionLost(); return; }
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        showAddError(body.detail || `Fehler ${response.status}`);
        return;
      }
      const wasEdit = modalMode === 'edit';
      closeAddModal();
      await loadInventory();
      pollHeartbeat();
      showToast(wasEdit ? `Gerät „${name}" aktualisiert.` : `Gerät „${name}" angelegt.`);
    } catch (err) {
      showAddError(err.message);
    } finally {
      btn.disabled = false;
      btn.textContent = originalLabel;
    }
  }

  function showAddError(msg) {
    const errorBox = $('#add-modal-error');
    errorBox.textContent = msg;
    errorBox.hidden = false;
  }


  // -------------------- Device-Modal (Detail + Aktionen) --------------------

  let currentDeviceId = null;
  let deleteArmed = false;

  function openDeviceModal(deviceId) {
    const device = state.devices.find((d) => d.id === deviceId);
    if (!device) return;
    currentDeviceId = deviceId;
    deleteArmed = false;

    $('#device-modal-title').textContent = device.name;
    const dl = $('#device-detail-list');
    dl.innerHTML = '';
    appendDetail(dl, 'Hostname', device.host);
    appendDetail(dl, 'Port', String(device.port));
    appendDetail(
      dl,
      'TLS',
      device.tls_verify
        ? '<span class="tls-on">verifiziert</span>'
        : '<span class="tls-off">deaktiviert (Risiko)</span>',
      true,
    );
    if (device.tags && device.tags.length) {
      const dt = document.createElement('dt');
      dt.textContent = 'Tags';
      dl.appendChild(dt);
      const dd = document.createElement('dd');
      dd.className = 'detail-tags';
      for (const t of device.tags) {
        const tag = document.createElement('span');
        tag.className = 'tag';
        tag.textContent = t;
        dd.appendChild(tag);
      }
      dl.appendChild(dd);
    }
    if (device.descr) appendDetail(dl, 'Notiz', device.descr);
    appendDetail(dl, 'Geräte-ID', device.id);

    $('#device-test-result').textContent = '';
    $('#device-test-btn').disabled = false;
    $('#device-modal-error').hidden = true;

    // OPNsense-Web-Link: echter Anchor mit konkreter href + sichtbarer
    // Mono-Text. Falls Chromium den target=_blank-Tab leer rendert (etwa
    // bei nicht aufloesbaren Hosts), kann der User die URL trotzdem
    // kopieren oder direkt in eine Adressleiste ziehen.
    const url = `https://${device.host}:${device.port}/`;
    const webLink = $('#device-open-web-btn');
    webLink.href = url;
    $('#device-url-text').textContent = url;

    resetDeleteButton();
    // Default: Info-Tab aktiv beim Oeffnen.
    switchDeviceTab('info');
    $('#device-modal').hidden = false;
  }

  function switchDeviceTab(tabName) {
    document.querySelectorAll('#device-modal-tabs .modal-tab').forEach((btn) => {
      btn.classList.toggle('is-active', btn.dataset.tab === tabName);
    });
    document.querySelectorAll('#device-modal [data-tab-pane]').forEach((pane) => {
      pane.hidden = pane.dataset.tabPane !== tabName;
    });
    // Lazy-Load der Tab-Inhalte
    if (tabName === 'aliases') {
      loadAliasesTab().catch(() => {});
    } else if (tabName === 'routes') {
      loadRoutesTab().catch(() => {});
    } else if (tabName === 'rules') {
      loadRulesTab().catch(() => {});
    } else if (tabName === 'unbound') {
      loadUnboundTab().catch(() => {});
    } else if (tabName === 'updates') {
      renderUpdatesTab();
    } else if (tabName === 'backups') {
      loadBackupsTab().catch(() => {});
    }
  }

  function renderUpdatesTab() {
    if (!currentDeviceId) return;
    const fw = state.firmware[currentDeviceId];
    const box = $('#device-updates-summary');
    if (!fw || !fw.version || fw.version === 'unknown') {
      box.innerHTML = '<div class="form-hint">Noch keine Firmware-Info geladen. Klicke "Erneut pruefen".</div>';
      return;
    }
    const lines = [];
    lines.push(`<div class="device-updates-summary-title">Installiert: v${fw.version}</div>`);
    if (fw.update_available) {
      const target = fw.new_version ? `v${fw.new_version}` : '(neuere Version verfuegbar)';
      lines.push(`<div class="device-updates-summary-meta">Verfuegbar: ${target}</div>`);
      lines.push(`<span class="device-updates-summary-badge has-update">Update verfuegbar</span>`);
    } else {
      lines.push(`<span class="device-updates-summary-badge up-to-date">Aktuell</span>`);
    }
    if (fw.status_msg) {
      const safe = fw.status_msg.replace(/</g, '&lt;').replace(/>/g, '&gt;');
      lines.push(`<div class="device-updates-summary-meta" style="margin-top:8px;">${safe}</div>`);
    }
    box.innerHTML = lines.join('');
  }

  function appendDetail(dl, label, value, html) {
    const dt = document.createElement('dt');
    dt.textContent = label;
    dl.appendChild(dt);
    const dd = document.createElement('dd');
    if (html) dd.innerHTML = value;
    else dd.textContent = value;
    dl.appendChild(dd);
  }

  function closeDeviceModal() {
    $('#device-modal').hidden = true;
    currentDeviceId = null;
    deleteArmed = false;
    // Tab-spezifischer State zuruecksetzen damit beim naechsten Open
    // der frische Stand geladen wird.
    almLoadedForDeviceId = null;
    almRawAliases = [];
    almCurrentDevice = null;
    bhLoadedForDeviceId = null;
    currentBackupDeviceId = null;
  }

  // -------------------- Aliase als Tab im Device-Modal --------------------

  let almRawAliases = [];
  let almCurrentDevice = null;
  let almLoadedForDeviceId = null;

  async function loadAliasesTab(force = false) {
    if (!currentDeviceId) return;
    const device = state.devices.find((d) => d.id === currentDeviceId);
    if (!device) return;
    // Cachen pro Geraet damit Tab-Wechsel nicht jedesmal nachlaedt
    if (!force && almLoadedForDeviceId === currentDeviceId && almRawAliases.length) {
      renderAliasManagerList();
      return;
    }
    almCurrentDevice = device;
    almRawAliases = [];
    almLoadedForDeviceId = currentDeviceId;
    $('#alm-status').textContent = 'Lade…';
    $('#alm-filter').value = '';
    $('#alm-list').innerHTML = '';
    try {
      const r = await apiGet(`/api/inventory/devices/${currentDeviceId}/aliases`);
      if (r.status === 401) { handleSessionLost(); return; }
      if (!r.ok) {
        const body = await r.json().catch(() => ({}));
        $('#alm-status').textContent = body.detail || `Fehler ${r.status}`;
        return;
      }
      const data = await r.json();
      almRawAliases = data.aliases || [];
      $('#alm-status').textContent = data.summary;
      renderAliasManagerList();
    } catch (err) {
      $('#alm-status').textContent = err.message;
    }
  }

  function renderAliasManagerList() {
    const list = $('#alm-list');
    list.innerHTML = '';
    const filter = ($('#alm-filter').value || '').trim().toLowerCase();
    const matching = filter
      ? almRawAliases.filter((a) =>
          a.name.toLowerCase().includes(filter)
          || a.content.some((c) => c.toLowerCase().includes(filter))
          || (a.description || '').toLowerCase().includes(filter))
      : almRawAliases;
    if (matching.length === 0) {
      const empty = document.createElement('div');
      empty.style.padding = '24px';
      empty.style.textAlign = 'center';
      empty.style.color = 'var(--text-subtle)';
      empty.textContent = filter
        ? 'Kein Treffer fuer den Filter.'
        : 'Keine Aliase auf diesem Geraet.';
      list.appendChild(empty);
      return;
    }
    for (const a of matching) {
      const row = document.createElement('div');
      row.className = 'alm-row';
      const head = document.createElement('div');
      head.className = 'alm-row-head';
      const name = document.createElement('span');
      name.className = 'alm-name';
      name.textContent = a.name;
      const meta = document.createElement('span');
      meta.className = 'alm-meta';
      meta.textContent = `${a.type} · ${a.content.length} Eintrag${a.content.length === 1 ? '' : 'e'}`;
      head.appendChild(name);
      head.appendChild(meta);
      row.appendChild(head);
      const content = document.createElement('div');
      content.className = 'alm-content';
      content.textContent = a.content.join('\n') || '(leer)';
      row.appendChild(content);
      if (a.description) {
        const descr = document.createElement('div');
        descr.className = 'alm-descr';
        descr.textContent = a.description;
        row.appendChild(descr);
      }
      const actions = document.createElement('div');
      actions.className = 'alm-actions';
      // Edit + Delete laufen ueber das Cockpit-Plan/Apply-Framework
      // (Pre-Apply-Backup + Audit + Drift-Baseline kommen automatisch dazu).
      const editBtn = document.createElement('button');
      editBtn.type = 'button';
      editBtn.className = 'btn-secondary';
      editBtn.textContent = 'Bearbeiten';
      editBtn.addEventListener('click', () => openAliasEditFromManager(a));
      actions.appendChild(editBtn);

      const delBtn = document.createElement('button');
      delBtn.type = 'button';
      delBtn.className = 'btn-danger';
      delBtn.textContent = 'Loeschen';
      delBtn.addEventListener('click', () => openAliasDeleteFromManager(a));
      actions.appendChild(delBtn);

      // Sekundaerer Deep-Link in die OPNsense (z. B. fuer Features die
      // Cockpit nicht abdeckt).
      if (almCurrentDevice) {
        const editLink = document.createElement('a');
        editLink.className = 'btn-link';
        editLink.target = '_blank';
        editLink.rel = 'noopener';
        editLink.href = `https://${almCurrentDevice.host}:${almCurrentDevice.port}/ui/firewall/alias`;
        editLink.textContent = 'In OPNsense oeffnen';
        actions.appendChild(editLink);
      }
      row.appendChild(actions);
      list.appendChild(row);
    }
  }

  function openAliasEditFromManager(alias) {
    // Plan-Modal als Alias-Edit oeffnen: Felder mit dem aktuellen Stand
    // vorbefuellen, Selektion auf das aktuelle Geraet einschraenken,
    // Submit landet auf /api/plans/alias-update. Device-Modal schliessen
    // damit nicht zwei Modale uebereinander liegen.
    if (!currentDeviceId) return;
    const targetDeviceId = currentDeviceId;
    closeDeviceModal();
    state.selectedDeviceIds.clear();
    state.selectedDeviceIds.add(targetDeviceId);
    renderGrid();
    planMode = 'alias-update';
    planPhase = 'input';
    currentPlan = null;
    resetPlanInputs();
    showPlanFieldSet('alias');
    renderPlanSelectionSummary();
    showPlanPhase('input');
    // Felder mit dem aktuellen Stand des Alias vorbelegen
    $('#pl-alias-name').value = alias.name || '';
    if ($('#pl-alias-type')) $('#pl-alias-type').value = alias.type || 'host';
    $('#pl-alias-content').value = (alias.content || []).join(', ');
    $('#pl-alias-descr').value = alias.description || '';
    const merge = $('#pl-alias-merge');
    if (merge) {
      merge.checked = false;
      // Update ersetzt komplett - merge-Mode irrefuehrend, deshalb verstecken
      const mergeRow = merge.closest('.form-row, .form-col, .form-checkbox');
      if (mergeRow) mergeRow.style.display = 'none';
    }
    $('#plan-modal-title').textContent = `Alias "${alias.name}" bearbeiten`;
    $('#plan-modal-error').hidden = true;
    $('#plan-preview-error').hidden = true;
    $('#plan-modal').hidden = false;
    setTimeout(() => $('#pl-alias-content').focus(), 0);
  }

  async function openAliasDeleteFromManager(alias) {
    if (!currentDeviceId) return;
    const device = state.devices.find((d) => d.id === currentDeviceId);
    const devName = device ? device.name : currentDeviceId;
    const ok = window.confirm(
      `Alias "${alias.name}" wirklich auf ${devName} loeschen?\n\n`
      + `Pre-Apply-Backup wird gezogen; ein Rollback ist via Backup-Tab moeglich.`,
    );
    if (!ok) return;
    try {
      const r = await apiPost('/api/plans/alias-delete', {
        name: alias.name,
        target_device_ids: [currentDeviceId],
      });
      if (r.status === 401) { handleSessionLost(); return; }
      if (!r.ok) {
        const body = await r.json().catch(() => ({}));
        showToast(body.detail || `Fehler ${r.status}`, true);
        return;
      }
      const plan = await r.json();
      closeDeviceModal();
      openExistingPlanInPreview(plan.plan_id);
    } catch (err) {
      showToast(err.message, true);
    }
  }

  // -------------------- Routen-Tab im Device-Modal --------------------

  let rtmRawRoutes = [];
  let rtmLoadedForDeviceId = null;

  async function loadRoutesTab(force = false) {
    if (!currentDeviceId) return;
    const device = state.devices.find((d) => d.id === currentDeviceId);
    if (!device) return;
    if (!force && rtmLoadedForDeviceId === currentDeviceId && rtmRawRoutes.length) {
      renderRoutesList();
      return;
    }
    rtmRawRoutes = [];
    rtmLoadedForDeviceId = currentDeviceId;
    $('#rtm-status').textContent = 'Lade…';
    $('#rtm-filter').value = '';
    $('#rtm-list').innerHTML = '';
    try {
      const r = await apiGet(`/api/inventory/devices/${currentDeviceId}/routes`);
      if (r.status === 401) { handleSessionLost(); return; }
      if (!r.ok) {
        const body = await r.json().catch(() => ({}));
        $('#rtm-status').textContent = body.detail || `Fehler ${r.status}`;
        return;
      }
      const data = await r.json();
      rtmRawRoutes = data.routes || [];
      $('#rtm-status').textContent = data.summary;
      renderRoutesList();
    } catch (err) {
      $('#rtm-status').textContent = err.message;
    }
  }

  function renderRoutesList() {
    const list = $('#rtm-list');
    list.innerHTML = '';
    const filter = ($('#rtm-filter').value || '').trim().toLowerCase();
    const matching = filter
      ? rtmRawRoutes.filter((r) =>
          r.network.toLowerCase().includes(filter)
          || r.gateway.toLowerCase().includes(filter)
          || (r.descr || '').toLowerCase().includes(filter))
      : rtmRawRoutes;
    if (matching.length === 0) {
      const empty = document.createElement('div');
      empty.style.padding = '24px';
      empty.style.textAlign = 'center';
      empty.style.color = 'var(--text-subtle)';
      empty.textContent = filter
        ? 'Kein Treffer fuer den Filter.'
        : 'Keine statischen Routen auf diesem Geraet.';
      list.appendChild(empty);
      return;
    }
    for (const r of matching) {
      const row = document.createElement('div');
      row.className = 'alm-row';
      const head = document.createElement('div');
      head.className = 'alm-row-head';
      const name = document.createElement('span');
      name.className = 'alm-name';
      name.textContent = r.network;
      const meta = document.createElement('span');
      meta.className = 'alm-meta';
      meta.textContent = `via ${r.gateway}${r.disabled ? ' · deaktiviert' : ''}`;
      head.appendChild(name);
      head.appendChild(meta);
      row.appendChild(head);
      if (r.descr) {
        const descr = document.createElement('div');
        descr.className = 'alm-descr';
        descr.textContent = r.descr;
        row.appendChild(descr);
      }
      const actions = document.createElement('div');
      actions.className = 'alm-actions';
      const editBtn = document.createElement('button');
      editBtn.type = 'button';
      editBtn.className = 'btn-secondary';
      editBtn.textContent = 'Bearbeiten';
      editBtn.addEventListener('click', () => openRouteEditFromManager(r));
      actions.appendChild(editBtn);
      const delBtn = document.createElement('button');
      delBtn.type = 'button';
      delBtn.className = 'btn-danger';
      delBtn.textContent = 'Loeschen';
      delBtn.addEventListener('click', () => openRouteDeleteFromManager(r));
      actions.appendChild(delBtn);
      row.appendChild(actions);
      list.appendChild(row);
    }
  }

  function openRouteEditFromManager(route) {
    if (!currentDeviceId) return;
    const targetDeviceId = currentDeviceId;
    closeDeviceModal();
    state.selectedDeviceIds.clear();
    state.selectedDeviceIds.add(targetDeviceId);
    renderGrid();
    planMode = 'route-update';
    planPhase = 'input';
    currentPlan = null;
    resetPlanInputs();
    showPlanFieldSet('route');
    renderPlanSelectionSummary();
    showPlanPhase('input');
    $('#pl-route-network').value = route.network || '';
    $('#pl-route-gateway').value = route.gateway || '';
    $('#pl-route-descr').value = route.descr || '';
    $('#pl-route-disabled').checked = !!route.disabled;
    // Identitaet (network + gateway) bleibt bei Update unveraendert -
    // im UI deaktivieren damit es klar ist.
    $('#pl-route-network').readOnly = true;
    $('#pl-route-gateway').readOnly = true;
    $('#plan-modal-title').textContent =
      `Route ${route.network} via ${route.gateway} bearbeiten`;
    $('#plan-modal-error').hidden = true;
    $('#plan-preview-error').hidden = true;
    $('#plan-modal').hidden = false;
    setTimeout(() => $('#pl-route-descr').focus(), 0);
  }

  // -------------------- DNS-Tab im Device-Modal (Unbound Host-Overrides) --------------------

  let unbRawHosts = [];
  let unbLoadedForDeviceId = null;
  let unbEditMode = 'add';
  let unbEditOriginalHost = '';
  let unbEditOriginalDomain = '';
  let unbTargetDeviceId = '';

  async function loadUnboundTab(force = false) {
    if (!currentDeviceId) return;
    const device = state.devices.find((d) => d.id === currentDeviceId);
    if (!device) return;
    if (!force && unbLoadedForDeviceId === currentDeviceId && unbRawHosts.length) {
      renderUnboundList();
      return;
    }
    unbRawHosts = [];
    unbLoadedForDeviceId = currentDeviceId;
    $('#unb-status').textContent = 'Lade…';
    $('#unb-filter').value = '';
    $('#unb-list').innerHTML = '';
    try {
      const r = await apiGet(`/api/inventory/devices/${currentDeviceId}/unbound-hosts`);
      if (r.status === 401) { handleSessionLost(); return; }
      if (!r.ok) {
        const body = await r.json().catch(() => ({}));
        $('#unb-status').textContent = body.detail || `Fehler ${r.status}`;
        return;
      }
      const data = await r.json();
      unbRawHosts = data.hosts || [];
      $('#unb-status').textContent = data.summary;
      renderUnboundList();
    } catch (err) {
      $('#unb-status').textContent = err.message;
    }
  }

  function renderUnboundList() {
    const list = $('#unb-list');
    list.innerHTML = '';
    const filter = ($('#unb-filter').value || '').trim().toLowerCase();
    const matching = filter
      ? unbRawHosts.filter((h) =>
          h.host.toLowerCase().includes(filter)
          || h.domain.toLowerCase().includes(filter)
          || (h.server || '').toLowerCase().includes(filter)
          || (h.description || '').toLowerCase().includes(filter))
      : unbRawHosts;
    if (matching.length === 0) {
      const empty = document.createElement('div');
      empty.style.padding = '24px';
      empty.style.textAlign = 'center';
      empty.style.color = 'var(--text-subtle)';
      empty.textContent = filter
        ? 'Kein Treffer fuer den Filter.'
        : 'Keine Unbound-Host-Overrides auf diesem Geraet.';
      list.appendChild(empty);
      return;
    }
    for (const h of matching) {
      const row = document.createElement('div');
      row.className = 'alm-row';
      const head = document.createElement('div');
      head.className = 'alm-row-head';
      const name = document.createElement('span');
      name.className = 'alm-name';
      const enabledMarker = h.enabled ? '' : ' (deaktiviert)';
      name.textContent = `${h.host}.${h.domain}${enabledMarker}`;
      const meta = document.createElement('span');
      meta.className = 'alm-meta';
      meta.textContent = `→ ${h.server}`;
      head.appendChild(name);
      head.appendChild(meta);
      row.appendChild(head);
      if (h.description) {
        const descr = document.createElement('div');
        descr.className = 'alm-descr';
        descr.textContent = h.description;
        row.appendChild(descr);
      }
      const actions = document.createElement('div');
      actions.className = 'alm-actions';
      const editBtn = document.createElement('button');
      editBtn.type = 'button';
      editBtn.className = 'btn-secondary';
      editBtn.textContent = 'Bearbeiten';
      editBtn.addEventListener('click', () => openUnboundEditModal(h));
      actions.appendChild(editBtn);
      const delBtn = document.createElement('button');
      delBtn.type = 'button';
      delBtn.className = 'btn-danger';
      delBtn.textContent = 'Loeschen';
      delBtn.addEventListener('click', () => deleteUnboundFromManager(h));
      actions.appendChild(delBtn);
      row.appendChild(actions);
      list.appendChild(row);
    }
  }

  function openUnboundAddModal() {
    if (!currentDeviceId) return;
    unbEditMode = 'add';
    unbEditOriginalHost = '';
    unbEditOriginalDomain = '';
    unbTargetDeviceId = currentDeviceId;
    $('#unbound-modal-title').textContent = 'Neuer Host-Override';
    $('#ub-host').value = '';
    $('#ub-host').readOnly = false;
    $('#ub-domain').value = '';
    $('#ub-domain').readOnly = false;
    $('#ub-server').value = '';
    $('#ub-enabled').checked = true;
    $('#ub-descr').value = '';
    $('#unbound-modal-error').hidden = true;
    $('#unbound-modal').hidden = false;
    setTimeout(() => $('#ub-host').focus(), 0);
  }

  function openUnboundEditModal(host) {
    if (!currentDeviceId) return;
    unbEditMode = 'update';
    unbEditOriginalHost = host.host;
    unbEditOriginalDomain = host.domain;
    unbTargetDeviceId = currentDeviceId;
    $('#unbound-modal-title').textContent =
      `Host-Override bearbeiten (${host.host}.${host.domain})`;
    $('#ub-host').value = host.host;
    $('#ub-host').readOnly = true;
    $('#ub-domain').value = host.domain;
    $('#ub-domain').readOnly = true;
    $('#ub-server').value = host.server || '';
    $('#ub-enabled').checked = !!host.enabled;
    $('#ub-descr').value = host.description || '';
    $('#unbound-modal-error').hidden = true;
    $('#unbound-modal').hidden = false;
    setTimeout(() => $('#ub-server').focus(), 0);
  }

  function closeUnboundModal() {
    $('#unbound-modal').hidden = true;
    $('#ub-host').readOnly = false;
    $('#ub-domain').readOnly = false;
    unbEditMode = 'add';
    unbEditOriginalHost = '';
    unbEditOriginalDomain = '';
    unbTargetDeviceId = '';
  }

  async function submitUnboundModal() {
    if (!unbTargetDeviceId) {
      showUnboundModalError('Kein Ziel-Geraet bekannt - Modal neu oeffnen.');
      return;
    }
    const host = $('#ub-host').value.trim();
    const domain = $('#ub-domain').value.trim();
    const server = $('#ub-server').value.trim();
    if (!host || !domain || !server) {
      showUnboundModalError('Host, Domain und Ziel-IP sind Pflichtfelder.');
      return;
    }
    const payload = {
      host, domain, server,
      enabled: $('#ub-enabled').checked,
      description: $('#ub-descr').value.trim(),
      target_device_ids: [unbTargetDeviceId],
    };
    const url = unbEditMode === 'update'
      ? '/api/plans/unbound-host-update'
      : '/api/plans/unbound-host';
    const confirm = $('#unbound-modal-confirm');
    confirm.disabled = true;
    confirm.textContent = 'Erzeuge Plan…';
    try {
      const r = await apiPost(url, payload);
      if (r.status === 401) { handleSessionLost(); return; }
      if (!r.ok) {
        const body = await r.json().catch(() => ({}));
        showUnboundModalError(body.detail || `Fehler ${r.status}`);
        return;
      }
      const plan = await r.json();
      closeUnboundModal();
      closeDeviceModal();
      openExistingPlanInPreview(plan.plan_id);
    } catch (err) {
      showUnboundModalError(err.message);
    } finally {
      confirm.disabled = false;
      confirm.textContent = 'Plan erzeugen';
    }
  }

  function showUnboundModalError(msg) {
    const box = $('#unbound-modal-error');
    box.textContent = msg;
    box.hidden = false;
  }

  async function deleteUnboundFromManager(hostOverride) {
    if (!currentDeviceId) return;
    const device = state.devices.find((d) => d.id === currentDeviceId);
    const devName = device ? device.name : currentDeviceId;
    const label = `${hostOverride.host}.${hostOverride.domain}`;
    const ok = window.confirm(
      `Host-Override "${label}" wirklich auf ${devName} loeschen?\n\n`
      + `Pre-Apply-Backup wird gezogen; ein Rollback ist via Backup-Tab moeglich.`,
    );
    if (!ok) return;
    try {
      const r = await apiPost('/api/plans/unbound-host-delete', {
        host: hostOverride.host,
        domain: hostOverride.domain,
        target_device_ids: [currentDeviceId],
      });
      if (r.status === 401) { handleSessionLost(); return; }
      if (!r.ok) {
        const body = await r.json().catch(() => ({}));
        showToast(body.detail || `Fehler ${r.status}`, true);
        return;
      }
      const plan = await r.json();
      closeDeviceModal();
      openExistingPlanInPreview(plan.plan_id);
    } catch (err) {
      showToast(err.message, true);
    }
  }

  // -------------------- Regeln-Tab im Device-Modal (Firewall-Filter) --------------------

  let frmRawRules = [];
  let frmLoadedForDeviceId = null;
  // Mode: 'add' | 'update'. UUID nur bei update gesetzt.
  let frmEditMode = 'add';
  let frmEditUuid = '';
  let frmTargetDeviceId = '';

  async function loadRulesTab(force = false) {
    if (!currentDeviceId) return;
    const device = state.devices.find((d) => d.id === currentDeviceId);
    if (!device) return;
    if (!force && frmLoadedForDeviceId === currentDeviceId && frmRawRules.length) {
      renderRulesList();
      return;
    }
    frmRawRules = [];
    frmLoadedForDeviceId = currentDeviceId;
    $('#frm-status').textContent = 'Lade…';
    $('#frm-filter').value = '';
    $('#frm-list').innerHTML = '';
    try {
      const r = await apiGet(`/api/inventory/devices/${currentDeviceId}/firewall-rules`);
      if (r.status === 401) { handleSessionLost(); return; }
      if (!r.ok) {
        const body = await r.json().catch(() => ({}));
        $('#frm-status').textContent = body.detail || `Fehler ${r.status}`;
        return;
      }
      const data = await r.json();
      frmRawRules = data.rules || [];
      $('#frm-status').textContent = data.summary;
      renderRulesList();
    } catch (err) {
      $('#frm-status').textContent = err.message;
    }
  }

  function renderRulesList() {
    const list = $('#frm-list');
    list.innerHTML = '';
    const filter = ($('#frm-filter').value || '').trim().toLowerCase();
    const matching = filter
      ? frmRawRules.filter((r) =>
          (r.description || '').toLowerCase().includes(filter)
          || (r.interface || '').toLowerCase().includes(filter)
          || (r.action || '').toLowerCase().includes(filter)
          || (r.source_net || '').toLowerCase().includes(filter)
          || (r.destination_net || '').toLowerCase().includes(filter))
      : frmRawRules;
    if (matching.length === 0) {
      const empty = document.createElement('div');
      empty.style.padding = '24px';
      empty.style.textAlign = 'center';
      empty.style.color = 'var(--text-subtle)';
      empty.textContent = filter
        ? 'Kein Treffer fuer den Filter.'
        : 'Keine Filter-Regeln auf diesem Geraet (oder os-firewall-Plugin nicht installiert).';
      list.appendChild(empty);
      return;
    }
    for (const r of matching) {
      const row = document.createElement('div');
      row.className = 'alm-row';
      const head = document.createElement('div');
      head.className = 'alm-row-head';
      const name = document.createElement('span');
      name.className = 'alm-name';
      const enabledMarker = r.enabled ? '' : ' (deaktiviert)';
      name.textContent = `${r.action} · ${r.interface}${enabledMarker}`;
      const meta = document.createElement('span');
      meta.className = 'alm-meta';
      const proto = r.protocol === 'any' ? '' : r.protocol + ' ';
      meta.textContent = `${proto}${r.source_net}${r.source_port ? ':' + r.source_port : ''} → ${r.destination_net}${r.destination_port ? ':' + r.destination_port : ''}`;
      head.appendChild(name);
      head.appendChild(meta);
      row.appendChild(head);
      if (r.description) {
        const descr = document.createElement('div');
        descr.className = 'alm-descr';
        descr.textContent = r.description;
        row.appendChild(descr);
      }
      const actions = document.createElement('div');
      actions.className = 'alm-actions';
      const editBtn = document.createElement('button');
      editBtn.type = 'button';
      editBtn.className = 'btn-secondary';
      editBtn.textContent = 'Bearbeiten';
      editBtn.addEventListener('click', () => openRuleEditModal(r));
      actions.appendChild(editBtn);
      const delBtn = document.createElement('button');
      delBtn.type = 'button';
      delBtn.className = 'btn-danger';
      delBtn.textContent = 'Loeschen';
      delBtn.addEventListener('click', () => deleteRuleFromManager(r));
      actions.appendChild(delBtn);
      row.appendChild(actions);
      list.appendChild(row);
    }
  }

  function openRuleAddModal() {
    if (!currentDeviceId) return;
    frmEditMode = 'add';
    frmEditUuid = '';
    frmTargetDeviceId = currentDeviceId;
    $('#rule-modal-title').textContent = 'Neue Filter-Regel';
    // Felder auf Defaults
    $('#rl-enabled').checked = true;
    $('#rl-action').value = 'pass';
    $('#rl-direction').value = 'in';
    $('#rl-interface').value = '';
    $('#rl-ipprotocol').value = 'inet';
    $('#rl-protocol').value = 'any';
    $('#rl-src-net').value = 'any';
    $('#rl-src-port').value = '';
    $('#rl-src-not').checked = false;
    $('#rl-dst-net').value = 'any';
    $('#rl-dst-port').value = '';
    $('#rl-dst-not').checked = false;
    $('#rl-gateway').value = '';
    $('#rl-sequence').value = '';
    $('#rl-log').checked = false;
    $('#rl-descr').value = '';
    $('#rule-modal-error').hidden = true;
    $('#rule-modal').hidden = false;
    setTimeout(() => $('#rl-interface').focus(), 0);
  }

  function openRuleEditModal(rule) {
    if (!currentDeviceId) return;
    frmEditMode = 'update';
    frmEditUuid = rule.uuid;
    frmTargetDeviceId = currentDeviceId;
    $('#rule-modal-title').textContent =
      `Regel bearbeiten (${rule.description || rule.uuid})`;
    $('#rl-enabled').checked = !!rule.enabled;
    $('#rl-action').value = rule.action || 'pass';
    $('#rl-direction').value = rule.direction || 'in';
    $('#rl-interface').value = rule.interface || '';
    $('#rl-ipprotocol').value = rule.ipprotocol || 'inet';
    $('#rl-protocol').value = rule.protocol || 'any';
    $('#rl-src-net').value = rule.source_net || 'any';
    $('#rl-src-port').value = rule.source_port || '';
    $('#rl-src-not').checked = !!rule.source_not;
    $('#rl-dst-net').value = rule.destination_net || 'any';
    $('#rl-dst-port').value = rule.destination_port || '';
    $('#rl-dst-not').checked = !!rule.destination_not;
    $('#rl-gateway').value = rule.gateway || '';
    $('#rl-sequence').value = (rule.sequence === null || rule.sequence === undefined) ? '' : rule.sequence;
    $('#rl-log').checked = !!rule.log;
    $('#rl-descr').value = rule.description || '';
    $('#rule-modal-error').hidden = true;
    $('#rule-modal').hidden = false;
  }

  function closeRuleModal() {
    $('#rule-modal').hidden = true;
    frmEditMode = 'add';
    frmEditUuid = '';
    frmTargetDeviceId = '';
  }

  async function submitRuleModal() {
    if (!frmTargetDeviceId) {
      showRuleModalError('Kein Ziel-Geraet bekannt - Modal neu oeffnen.');
      return;
    }
    const interfaceVal = $('#rl-interface').value.trim();
    if (!interfaceVal) {
      showRuleModalError('Interface ist Pflichtfeld.');
      return;
    }
    const seqRaw = $('#rl-sequence').value.trim();
    const sequence = seqRaw === '' ? null : Number(seqRaw);
    if (sequence !== null && !Number.isInteger(sequence)) {
      showRuleModalError('Sequenz muss eine Ganzzahl sein.');
      return;
    }
    const payload = {
      enabled: $('#rl-enabled').checked,
      action: $('#rl-action').value,
      interface: interfaceVal,
      direction: $('#rl-direction').value,
      ipprotocol: $('#rl-ipprotocol').value,
      protocol: $('#rl-protocol').value,
      source_net: $('#rl-src-net').value.trim() || 'any',
      source_port: $('#rl-src-port').value.trim(),
      source_not: $('#rl-src-not').checked,
      destination_net: $('#rl-dst-net').value.trim() || 'any',
      destination_port: $('#rl-dst-port').value.trim(),
      destination_not: $('#rl-dst-not').checked,
      gateway: $('#rl-gateway').value.trim(),
      log: $('#rl-log').checked,
      description: $('#rl-descr').value.trim(),
      sequence,
      target_device_ids: [frmTargetDeviceId],
    };
    let url = '/api/plans/rule';
    if (frmEditMode === 'update') {
      payload.uuid = frmEditUuid;
      url = '/api/plans/rule-update';
    }
    const confirm = $('#rule-modal-confirm');
    confirm.disabled = true;
    confirm.textContent = 'Erzeuge Plan…';
    try {
      const r = await apiPost(url, payload);
      if (r.status === 401) { handleSessionLost(); return; }
      if (!r.ok) {
        const body = await r.json().catch(() => ({}));
        showRuleModalError(body.detail || `Fehler ${r.status}`);
        return;
      }
      const plan = await r.json();
      closeRuleModal();
      closeDeviceModal();
      openExistingPlanInPreview(plan.plan_id);
    } catch (err) {
      showRuleModalError(err.message);
    } finally {
      confirm.disabled = false;
      confirm.textContent = 'Plan erzeugen';
    }
  }

  function showRuleModalError(msg) {
    const box = $('#rule-modal-error');
    box.textContent = msg;
    box.hidden = false;
  }

  async function deleteRuleFromManager(rule) {
    if (!currentDeviceId) return;
    const device = state.devices.find((d) => d.id === currentDeviceId);
    const devName = device ? device.name : currentDeviceId;
    const label = rule.description || rule.uuid;
    const ok = window.confirm(
      `Regel "${label}" wirklich auf ${devName} loeschen?\n\n`
      + `Pre-Apply-Backup wird gezogen; ein Rollback ist via Backup-Tab moeglich.`,
    );
    if (!ok) return;
    try {
      const r = await apiPost('/api/plans/rule-delete', {
        uuid: rule.uuid,
        target_device_ids: [currentDeviceId],
      });
      if (r.status === 401) { handleSessionLost(); return; }
      if (!r.ok) {
        const body = await r.json().catch(() => ({}));
        showToast(body.detail || `Fehler ${r.status}`, true);
        return;
      }
      const plan = await r.json();
      closeDeviceModal();
      openExistingPlanInPreview(plan.plan_id);
    } catch (err) {
      showToast(err.message, true);
    }
  }

  async function openRouteDeleteFromManager(route) {
    if (!currentDeviceId) return;
    const device = state.devices.find((d) => d.id === currentDeviceId);
    const devName = device ? device.name : currentDeviceId;
    const ok = window.confirm(
      `Route ${route.network} via ${route.gateway} wirklich auf ${devName} loeschen?\n\n`
      + `Pre-Apply-Backup wird gezogen; ein Rollback ist via Backup-Tab moeglich.`,
    );
    if (!ok) return;
    try {
      const r = await apiPost('/api/plans/route-delete', {
        network: route.network,
        gateway: route.gateway,
        target_device_ids: [currentDeviceId],
      });
      if (r.status === 401) { handleSessionLost(); return; }
      if (!r.ok) {
        const body = await r.json().catch(() => ({}));
        showToast(body.detail || `Fehler ${r.status}`, true);
        return;
      }
      const plan = await r.json();
      closeDeviceModal();
      openExistingPlanInPreview(plan.plan_id);
    } catch (err) {
      showToast(err.message, true);
    }
  }

  async function doTestConnection() {
    if (!currentDeviceId) return;
    const btn = $('#device-test-btn');
    const result = $('#device-test-result');
    btn.disabled = true;
    const labelSpan = btn.querySelector('span');
    const originalLabel = labelSpan ? labelSpan.textContent : btn.textContent;
    if (labelSpan) labelSpan.textContent = 'Teste…';
    result.textContent = '';
    try {
      const response = await apiPost(`/api/inventory/devices/${encodeURIComponent(currentDeviceId)}/test-connection`);
      if (response.status === 401) { handleSessionLost(); return; }
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        result.textContent = body.detail || `Fehler ${response.status}`;
        return;
      }
      const data = await response.json();
      result.textContent = data.summary;
    } catch (err) {
      result.textContent = err.message;
    } finally {
      btn.disabled = false;
      if (labelSpan) labelSpan.textContent = originalLabel;
    }
  }

  function duplicateDeviceById(deviceId) {
    // Quick-Action-Variante von doDuplicate: oeffnet direkt das Add-Modal
    // mit den Werten des Quell-Geraets, ohne den Umweg ueber Device-Modal.
    const device = state.devices.find((d) => d.id === deviceId);
    if (!device) return;
    openAddModal({
      name: `${device.name} (Kopie)`,
      host: device.host,
      port: device.port,
      tls_verify: device.tls_verify,
      tags: device.tags,
      descr: device.descr,
      duplicateOf: device.name,
    });
  }

  async function doFirmwareCheckForDevice(deviceId) {
    // Quick-Action-Variante von doFirmwareCheck: triggert den OPNsense-
    // Check ohne Modal-spezifische UI-Updates. Toast zeigt Status.
    showToast('OPNsense pruefend (bis ~15s)…');
    try {
      const response = await apiPost(
        `/api/inventory/devices/${encodeURIComponent(deviceId)}/firmware-check`,
      );
      if (response.status === 401) { handleSessionLost(); return; }
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        showToast(body.detail || `Update-Check fehlgeschlagen (${response.status})`, true);
        return;
      }
      const data = await response.json();
      state.firmware[data.device_id] = {
        version: data.version,
        status: data.status,
        update_available: data.update_available,
        new_version: data.new_version || '',
        status_msg: data.status_msg || '',
      };
      renderGrid();
      if (data.update_available) {
        showToast(
          data.new_version
            ? `Update verfuegbar: v${data.new_version}`
            : 'Update verfuegbar',
        );
      } else {
        showToast('Aktuell — kein Update verfuegbar.');
      }
    } catch (err) {
      showToast(`Update-Check fehlgeschlagen: ${err.message}`, true);
    }
  }

  async function doFirmwareCheck() {
    // Stoesst auf der OPNsense den "Check for updates"-Vorgang an. Backend
    // blockiert ~5-12s waehrend der Check durchlaeuft, danach kommt der
    // frische Firmware-Status zurueck und die Kachel-Badge aktualisiert sich.
    if (!currentDeviceId) return;
    const btn = $('#device-update-check-btn');
    const result = $('#device-test-result');
    const labelSpan = btn.querySelector('span');
    const originalLabel = labelSpan ? labelSpan.textContent : btn.textContent;
    btn.disabled = true;
    if (labelSpan) labelSpan.textContent = 'Pruefe…';
    result.textContent = 'OPNsense pruefend (bis ~15s)…';
    try {
      const response = await apiPost(
        `/api/inventory/devices/${encodeURIComponent(currentDeviceId)}/firmware-check`,
      );
      if (response.status === 401) { handleSessionLost(); return; }
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        result.textContent = body.detail || `Fehler ${response.status}`;
        showToast(body.detail || `Fehler ${response.status}`, true);
        return;
      }
      const data = await response.json();
      // State aktualisieren, damit die Kachel beim naechsten Render den
      // neuen Stand zeigt (Badge erscheint/verschwindet sofort).
      state.firmware[data.device_id] = {
        version: data.version,
        status: data.status,
        update_available: data.update_available,
        new_version: data.new_version || '',
        status_msg: data.status_msg || '',
        summary: data.summary,
        reachable: data.reachable,
        authenticated: data.authenticated,
        checked_at_iso: data.checked_at_iso,
      };
      result.textContent = data.summary;
      renderGrid();
      const toastMsg = data.update_available
        ? `Update verfuegbar: v${data.new_version || '(unbekannt)'}`
        : 'Aktuell — kein Update verfuegbar.';
      showToast(toastMsg);
    } catch (err) {
      result.textContent = err.message;
      showToast(err.message, true);
    } finally {
      btn.disabled = false;
      if (labelSpan) labelSpan.textContent = originalLabel;
    }
  }

  async function doBackupDownload() {
    if (!currentDeviceId) return;
    const btn = $('#device-backup-btn');
    const result = $('#device-test-result');
    const labelSpan = btn.querySelector('span');
    const originalLabel = labelSpan ? labelSpan.textContent : btn.textContent;
    btn.disabled = true;
    if (labelSpan) labelSpan.textContent = 'Lade Backup…';
    result.textContent = '';
    try {
      // Bearer-Token muss mit — apiGet wraps das fuer JSON; hier brauchen wir
      // den Blob fuer den File-Download.
      const url = `/api/inventory/devices/${encodeURIComponent(currentDeviceId)}/backup`;
      const t = getToken();
      const headers = t ? { Authorization: `Bearer ${t}` } : {};
      const response = await fetch(url, { method: 'GET', headers });
      if (response.status === 401) { handleSessionLost(); return; }
      if (!response.ok) {
        let detail = `Fehler ${response.status}`;
        try {
          const body = await response.json();
          if (body.detail) detail = body.detail;
        } catch (_e) { /* nicht-json ist ok */ }
        result.textContent = detail;
        showToast(detail, true);
        return;
      }
      // Datei-Name aus Content-Disposition lesen, sonst fallback.
      let filename = 'opnsense-config.xml';
      const disposition = response.headers.get('Content-Disposition') || '';
      const match = disposition.match(/filename="?([^";]+)"?/i);
      if (match) filename = match[1];
      const blob = await response.blob();
      const dlUrl = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = dlUrl;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(dlUrl);
      result.textContent = `${filename} (${formatBytes(blob.size)}) heruntergeladen.`;
      showToast(`Backup geladen: ${filename}`);
    } catch (err) {
      result.textContent = err.message;
      showToast(err.message, true);
    } finally {
      btn.disabled = false;
      if (labelSpan) labelSpan.textContent = originalLabel;
    }
  }

  async function doBackupCreateServer() {
    // Backup auf dem Server erzeugen + persistieren, OHNE Download-Popup.
    // Verwendet vom Backups-Tab im Device-Modal ("Backup erzeugen").
    if (!currentDeviceId) return;
    const btn = $('#device-backup-now-btn');
    if (!btn) return;
    const labelSpan = btn.querySelector('span');
    const originalLabel = labelSpan ? labelSpan.textContent : btn.textContent;
    btn.disabled = true;
    if (labelSpan) labelSpan.textContent = 'Erzeuge Backup…';
    try {
      const r = await apiPost(
        `/api/inventory/devices/${encodeURIComponent(currentDeviceId)}/backups`,
      );
      if (r.status === 401) { handleSessionLost(); return; }
      if (!r.ok) {
        let detail = `Fehler ${r.status}`;
        try {
          const body = await r.json();
          if (body.detail) detail = body.detail;
        } catch (_e) { /* nicht-json ist ok */ }
        showToast(detail, true);
        return;
      }
      const body = await r.json();
      showToast(
        `Backup erzeugt (${formatBytes(body.size_bytes)}, gespeichert auf Server).`,
      );
    } catch (err) {
      showToast(err.message, true);
    } finally {
      btn.disabled = false;
      if (labelSpan) labelSpan.textContent = originalLabel;
    }
  }

  function formatBytes(n) {
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
    return `${(n / 1024 / 1024).toFixed(2)} MB`;
  }

  function doDuplicate() {
    const device = state.devices.find((d) => d.id === currentDeviceId);
    if (!device) return;
    const newName = `${device.name} (Kopie)`;
    closeDeviceModal();
    openAddModal({
      name: newName,
      host: device.host,
      port: device.port,
      tls_verify: device.tls_verify,
      tags: device.tags,
      descr: device.descr,
      duplicateOf: device.name,
    });
  }

  function doEditFromDetail() {
    const device = state.devices.find((d) => d.id === currentDeviceId);
    if (!device) return;
    closeDeviceModal();
    openEditModal(device);
  }

  function openWebUrl(url) {
    // window.open ohne windowFeatures-string oeffnet einen regulaeren
    // Tab. Bei Edge funktioniert das auch in Situationen, in denen ein
    // <a target="_blank">-Klick den Tab leer laesst (Edge-Bug bei
    // bestimmten rel/target-Kombinationen, gerade mit nicht aufloesbaren
    // Hosts).
    const w = window.open(url, '_blank');
    if (!w) {
      // Popup-Blocker hat zugeschlagen. URL ist im Modal sichtbar +
      // Copy-Button als Fallback.
      showToast('Browser hat den Tab blockiert. URL aus dem Modal kopieren.', true);
    }
  }

  async function doCopyUrl() {
    const text = $('#device-url-text').textContent || '';
    if (!text || text === '—') return;
    try {
      await navigator.clipboard.writeText(text);
      showToast('URL kopiert.');
    } catch (_) {
      // Clipboard-API kann je nach Browser/Kontext blockiert sein.
      // Fallback: alten exec-Command versuchen.
      const tmp = document.createElement('textarea');
      tmp.value = text;
      document.body.appendChild(tmp);
      tmp.select();
      try { document.execCommand('copy'); showToast('URL kopiert.'); }
      catch (_) { showToast('Kopieren fehlgeschlagen.', true); }
      document.body.removeChild(tmp);
    }
  }

  function resetDeleteButton() {
    deleteArmed = false;
    const btn = $('#device-modal-delete');
    btn.textContent = 'Gerät löschen';
    btn.classList.remove('btn-danger-armed');
  }

  async function doDeleteDevice() {
    if (!currentDeviceId) return;
    const btn = $('#device-modal-delete');
    const errorBox = $('#device-modal-error');
    errorBox.hidden = true;

    if (!deleteArmed) {
      // Zweiter Klick erforderlich — erfahrene Admins, aber keine Versehentliches.
      deleteArmed = true;
      btn.textContent = 'Wirklich löschen?';
      btn.classList.add('btn-danger-armed');
      setTimeout(() => {
        if (deleteArmed) resetDeleteButton();
      }, 5000);
      return;
    }

    btn.disabled = true;
    btn.textContent = 'Lösche…';
    try {
      const response = await apiDelete(
        `/api/inventory/devices/${encodeURIComponent(currentDeviceId)}`,
      );
      if (response.status === 401) {
        handleSessionLost();
        return;
      }
      if (!response.ok && response.status !== 204) {
        const body = await response.json().catch(() => ({}));
        errorBox.textContent = body.detail || `Fehler ${response.status}`;
        errorBox.hidden = false;
        return;
      }
      closeDeviceModal();
      await loadInventory();
      showToast('Gerät gelöscht.');
    } catch (err) {
      errorBox.textContent = err.message;
      errorBox.hidden = false;
    } finally {
      btn.disabled = false;
      resetDeleteButton();
    }
  }

  // -------------------- Plan-Modal --------------------

  // Modus: 'route' oder 'alias'
  // Phasen: 'input' -> 'preview' -> 'result'
  // Zielgeraete kommen aus state.selectedDeviceIds (globale Karten-Auswahl).
  let planMode = 'route';
  let planPhase = 'input';
  let currentPlan = null;       // PlanResponse vom Server
  let retryDeviceIds = null;    // null = normales Apply; Array = nur diese

  function openPlanModal(mode) {
    if (state.selectedDeviceIds.size === 0) {
      showToast('Bitte erst Firewalls auswählen (Checkbox auf den Karten).', true);
      return;
    }
    planMode = mode;
    planPhase = 'input';
    currentPlan = null;
    resetPlanInputs();
    showPlanFieldSet(mode);
    renderPlanSelectionSummary();
    showPlanPhase('input');
    $('#plan-modal-title').textContent =
      mode === 'route' ? 'Neue Route auf Auswahl ausrollen' : 'Neuer Alias auf Auswahl ausrollen';
    $('#plan-modal-error').hidden = true;
    $('#plan-preview-error').hidden = true;
    $('#plan-modal').hidden = false;
    loadPlanProfiles().catch(() => {});
    setTimeout(() => {
      const focusEl = mode === 'route' ? $('#pl-route-network') : $('#pl-alias-name');
      if (focusEl) focusEl.focus();
    }, 0);
  }

  async function discardPlanOnServer(planId) {
    // Stilles DELETE - der Plan ist nur lokal sichtbar gewesen, ein
    // Fehler beim Loeschen blockiert das UI nicht (Toast zeigt es an).
    try {
      const response = await fetch(`/api/plans/${encodeURIComponent(planId)}`, {
        method: 'DELETE',
        headers: { Authorization: `Bearer ${getToken() || ''}` },
      });
      if (response.status === 401) { handleSessionLost(); return false; }
      if (!response.ok && response.status !== 404) {
        // 404 = schon weg, ist OK
        const body = await response.json().catch(() => ({}));
        showToast(`Plan konnte nicht entfernt werden: ${body.detail || response.status}`, true);
        return false;
      }
      return true;
    } catch (e) {
      showToast(`Plan konnte nicht entfernt werden: ${e.message}`, true);
      return false;
    }
  }

  function closePlanModal(opts) {
    // opts.discardPlan: wenn true, wird der aktuelle (noch nicht angewandte)
    // Plan auf dem Server geloescht. Bewusst Default false, weil der
    // Result-Phase-Schliessen-Pfad den Plan bewusst stehen lassen will.
    const shouldDiscard = !!(opts && opts.discardPlan);
    const planIdToDiscard = shouldDiscard && currentPlan ? currentPlan.plan_id : null;
    $('#plan-modal').hidden = true;
    planMode = 'route';
    planPhase = 'input';
    currentPlan = null;
    retryDeviceIds = null;
    if (planIdToDiscard) {
      // Outstanding aktualisieren wenn das DELETE durch ist
      discardPlanOnServer(planIdToDiscard).then((ok) => {
        if (ok) loadOutstanding().catch(() => {});
      });
    }
  }

  function cancelPlanModal() {
    // Vom Cancel-/X-Klick aufgerufen. Wenn wir gerade auf der Vorschau
    // sitzen oder mit einem nicht-angewandten Plan im Input-Phase
    // gelandet sind (Back), wegwerfen - sonst (Result-Phase oder reiner
    // Input ohne Plan) einfach schliessen.
    const inProgress = (planPhase === 'preview') ||
      (planPhase === 'input' && currentPlan !== null);
    closePlanModal({ discardPlan: inProgress });
  }

  async function openRetryForDevice(deviceId, planId) {
    if (!planId) return;
    try {
      const response = await apiGet(`/api/plans/${encodeURIComponent(planId)}`);
      if (response.status === 401) { handleSessionLost(); return; }
      if (!response.ok) {
        showToast('Plan nicht abrufbar — vielleicht wurde er gelöscht.', true);
        return;
      }
      currentPlan = await response.json();
      retryDeviceIds = [deviceId];
      planMode = currentPlan.subsystem === 'routes' ? 'route' : 'alias';
      $('#plan-modal-title').textContent = `Nachziehen für ${state.devices.find((d) => d.id === deviceId)?.name || deviceId}`;
      $('#plan-modal-error').hidden = true;
      $('#plan-preview-error').hidden = true;
      // Vorschau auf die gefilterte Selection beschränken
      const filtered = {
        ...currentPlan,
        actions: currentPlan.actions.filter((a) => a.device_id === deviceId),
      };
      filtered.to_apply_count = filtered.actions.filter((a) => a.diff_kind !== 'skip').length;
      filtered.skip_count = filtered.actions.length - filtered.to_apply_count;
      filtered.target_count = filtered.actions.length;
      $('#plan-modal').hidden = false;
      renderPreview(filtered);
      showPlanPhase('preview');
    } catch (err) {
      showToast(err.message, true);
    }
  }

  function renderPlanSelectionSummary() {
    const summary = $('#pl-selection-summary');
    const ids = Array.from(state.selectedDeviceIds);
    const total = state.devices.length;
    const selected = state.devices.filter((d) => state.selectedDeviceIds.has(d.id));
    if (selected.length === 0) {
      summary.className = 'selection-summary no-selection';
      summary.textContent = 'Keine Firewalls ausgewählt — Modal schließen und Karten markieren.';
      return;
    }
    summary.className = 'selection-summary';
    const names = selected.map((d) => d.name).join(', ');
    const namesShort = names.length > 200 ? names.substring(0, 197) + '…' : names;
    summary.innerHTML = `
      <div>Aktion wird auf <span class="selection-summary-count">${selected.length}</span> von ${total} Firewalls ausgerollt.</div>
      <div class="selection-summary-list">${namesShort}</div>
    `;
  }

  function resetPlanInputs() {
    $('#pl-route-network').value = '';
    $('#pl-route-gateway').value = '';
    // Identitaets-Felder im naechsten Add-Plan wieder editierbar machen
    $('#pl-route-network').readOnly = false;
    $('#pl-route-gateway').readOnly = false;
    $('#pl-route-descr').value = '';
    $('#pl-route-disabled').checked = false;
    $('#pl-alias-name').value = '';
    $('#pl-alias-type').value = 'host';
    $('#pl-alias-content').value = '';
    $('#pl-alias-descr').value = '';
    $('#pl-alias-merge').checked = false;
    $('#pl-confirm').checked = false;
    // Merge-Checkbox-Row wieder einblenden (alias-update mode hatte sie versteckt)
    const merge = $('#pl-alias-merge');
    if (merge) {
      const mergeRow = merge.closest('.form-row, .form-col, .form-checkbox');
      if (mergeRow) mergeRow.style.display = '';
    }
    // Suggestion-Caches leeren — sonst greift F19-Rebrowse fuer Eintraege
    // die vom vorherigen Geraet kamen.
    aliasSuggestionTypes = new Map();
    gatewaySuggestionNames = new Set();
  }

  function showPlanFieldSet(mode) {
    $$('.plan-field-set').forEach((set) => {
      set.hidden = set.dataset.kind !== mode;
    });
  }

  function showPlanPhase(phase) {
    planPhase = phase;
    $$('.plan-phase').forEach((p) => {
      p.hidden = p.dataset.phase !== phase;
    });
    // Footer-Buttons anpassen
    const cancel = $('#plan-modal-cancel');
    const back = $('#plan-back-btn');
    const next = $('#plan-next-btn');
    const saveProfile = $('#plan-save-profile-btn');
    const discard = $('#plan-discard-btn');
    if (phase === 'input') {
      cancel.textContent = 'Abbrechen';
      back.hidden = true;
      next.hidden = false;
      next.textContent = 'Vorschau anzeigen';
      next.disabled = false;
      saveProfile.hidden = false;
      discard.hidden = true;
    } else if (phase === 'preview') {
      cancel.textContent = 'Abbrechen';
      // Im Retry-Flow (Plan via Offen-Badge geoeffnet) macht "Bearbeiten"
      // keinen Sinn - der Plan ist persistiert, die Input-Felder wuerden
      // leer aufgehen weil das Form nicht aus dem Plan vorgeladen wird.
      // Da bleibt nur Apply oder "Plan verwerfen".
      back.hidden = retryDeviceIds !== null;
      next.hidden = false;
      next.textContent = 'Aktivieren';
      next.disabled = !$('#pl-confirm').checked;
      saveProfile.hidden = true;
      discard.hidden = false;
    } else if (phase === 'result') {
      cancel.textContent = 'Schließen';
      back.hidden = true;
      next.hidden = true;
      saveProfile.hidden = true;
      discard.hidden = true;
    }
  }

  async function planNextOrApply() {
    if (planPhase === 'input') {
      await submitPlanInput();
    } else if (planPhase === 'preview') {
      await submitApply();
    }
  }

  async function submitPlanInput() {
    const errorBox = $('#plan-modal-error');
    errorBox.hidden = true;
    if (state.selectedDeviceIds.size === 0) {
      return showPlanError('Keine Firewalls ausgewählt — Modal schließen und Karten markieren.');
    }
    let body = null;
    let url = null;
    if (planMode === 'route' || planMode === 'route-update') {
      const network = $('#pl-route-network').value.trim();
      const gateway = $('#pl-route-gateway').value.trim();
      if (!network || !gateway) {
        return showPlanError('Netzwerk (CIDR) und Gateway sind Pflichtfelder.');
      }
      body = {
        network,
        gateway,
        descr: $('#pl-route-descr').value.trim(),
        disabled: $('#pl-route-disabled').checked,
        target_device_ids: Array.from(state.selectedDeviceIds),
      };
      url = planMode === 'route-update' ? '/api/plans/route-update' : '/api/plans/route';
    } else {
      const name = $('#pl-alias-name').value.trim();
      const type = $('#pl-alias-type').value;
      const contentRaw = $('#pl-alias-content').value.trim();
      if (!name || !contentRaw) {
        return showPlanError('Alias-Name und Inhalte sind Pflichtfelder.');
      }
      const content = contentRaw
        .split(',')
        .map((c) => c.trim())
        .filter((c) => c.length > 0);
      if (!content.length) {
        return showPlanError('Mindestens ein Alias-Inhalt erforderlich.');
      }
      if (planMode === 'alias-update') {
        body = {
          name,
          type,
          content,
          descr: $('#pl-alias-descr').value.trim(),
          target_device_ids: Array.from(state.selectedDeviceIds),
        };
        url = '/api/plans/alias-update';
      } else {
        body = {
          name,
          type,
          content,
          descr: $('#pl-alias-descr').value.trim(),
          merge_mode: $('#pl-alias-merge').checked ? 'append' : 'create',
          target_device_ids: Array.from(state.selectedDeviceIds),
        };
        url = '/api/plans/alias';
      }
    }

    const next = $('#plan-next-btn');
    next.disabled = true;
    next.textContent = 'Erzeuge Plan…';
    try {
      const response = await apiPost(url, body);
      if (response.status === 401) { handleSessionLost(); return; }
      if (!response.ok) {
        const respBody = await response.json().catch(() => ({}));
        showPlanError(respBody.detail || `Fehler ${response.status}`);
        return;
      }
      currentPlan = await response.json();
      renderPreview(currentPlan);
      showPlanPhase('preview');
    } catch (err) {
      showPlanError(err.message);
    } finally {
      next.disabled = false;
      // Bei Erfolg setzt showPlanPhase('preview') den Text auf 'Aktivieren'.
      // Bei Validation-/Netz-Fehler bleiben wir in 'input' und muessen den
      // Erzeuge-Plan…-Zwischentext wieder auf 'Vorschau anzeigen' zuruecknehmen.
      if (planPhase === 'input') {
        next.textContent = 'Vorschau anzeigen';
      }
    }
  }

  function renderPreview(plan) {
    const summary = $('#pl-preview-summary');
    summary.innerHTML = `
      <span>Plan <strong>${plan.plan_id}</strong></span>
      <span class="pill new">${plan.to_apply_count} schreiben</span>
      <span class="pill skip">${plan.skip_count} überspringen</span>
    `;
    const list = $('#pl-preview-list');
    list.innerHTML = '';
    for (const action of plan.actions) {
      const row = document.createElement('div');
      row.className = 'preview-row';
      const device = document.createElement('span');
      device.className = 'preview-row-device';
      device.textContent = action.device_name;
      const diff = document.createElement('span');
      diff.className = 'preview-row-diff';
      diff.textContent = action.diff_summary || '—';
      const kind = document.createElement('span');
      const k = action.diff_kind.toLowerCase();
      kind.className = `preview-row-kind ${k}`;
      kind.textContent = k;
      row.appendChild(device);
      row.appendChild(diff);
      row.appendChild(kind);
      list.appendChild(row);
    }
    $('#pl-confirm').checked = false;
    $('#plan-preview-error').hidden = true;
  }

  async function submitApply() {
    if (!currentPlan) return;
    if (!$('#pl-confirm').checked) {
      const err = $('#plan-preview-error');
      err.textContent = 'Bitte die Vorschau bestätigen.';
      err.hidden = false;
      return;
    }
    const next = $('#plan-next-btn');
    const back = $('#plan-back-btn');
    next.disabled = true;
    back.disabled = true;
    next.textContent = 'Rolle aus…';
    try {
      const body = retryDeviceIds ? { device_ids: retryDeviceIds } : null;
      const response = await apiPost(
        `/api/plans/${encodeURIComponent(currentPlan.plan_id)}/apply`,
        body,
      );
      if (response.status === 401) { handleSessionLost(); return; }
      if (!response.ok) {
        const respBody = await response.json().catch(() => ({}));
        const err = $('#plan-preview-error');
        err.textContent = respBody.detail || `Fehler ${response.status}`;
        err.hidden = false;
        return;
      }
      const report = await response.json();
      renderResult(report);
      showPlanPhase('result');
      pollHeartbeat();
      // Outstanding-Indikator hat sich vermutlich geaendert.
      loadOutstanding().catch(() => {});
    } catch (err) {
      const errBox = $('#plan-preview-error');
      errBox.textContent = err.message;
      errBox.hidden = false;
    } finally {
      next.disabled = false;
      back.disabled = false;
    }
  }

  function renderResult(report) {
    const summary = $('#pl-result-summary');
    let retryAction = '';
    if (report.failures > 0) {
      retryAction = `
        <div class="result-retry-group">
          <button class="btn-secondary result-retry-btn" id="result-retry-btn">
            ${report.failures} jetzt erneut versuchen
          </button>
          <button class="btn-secondary result-retry-auto-btn" id="result-retry-auto-btn" title="Im Hintergrund alle 3 Minuten erneut probieren, bis sie erreichbar sind">
            Auto-Retry starten
          </button>
        </div>
      `;
    }
    summary.innerHTML = `
      <div class="result-summary-item ok"><strong>${report.successes}</strong><span>erfolgreich</span></div>
      <div class="result-summary-item fail"><strong>${report.failures}</strong><span>fehlgeschlagen</span></div>
      <div class="result-summary-item skip"><strong>${report.skipped}</strong><span>übersprungen</span></div>
      ${retryAction}
    `;
    const retryBtn = $('#result-retry-btn');
    if (retryBtn) {
      retryBtn.addEventListener('click', () => doRetryFailed(report));
    }
    const autoBtn = $('#result-retry-auto-btn');
    if (autoBtn) {
      autoBtn.addEventListener('click', () => doScheduleAutoRetry(report));
    }
    const list = $('#pl-result-list');
    list.innerHTML = '';
    for (const r of report.results) {
      const row = document.createElement('div');
      row.className = 'result-row';
      const device = document.createElement('span');
      device.className = 'result-row-device';
      device.textContent = r.device_name;
      const msg = document.createElement('span');
      msg.className = 'result-row-msg';
      msg.textContent = r.short_message || '—';
      const statusEl = document.createElement('span');
      const kind = r.status === 'Verifiziert' || r.status === 'Übersprungen'
        ? (r.status === 'Übersprungen' ? 'skip' : 'ok')
        : 'fail';
      statusEl.className = `result-row-status ${kind}`;
      statusEl.textContent = r.status;
      const duration = document.createElement('span');
      duration.className = 'result-row-duration';
      duration.textContent = r.duration_ms ? `${r.duration_ms} ms` : '';
      row.appendChild(device);
      row.appendChild(msg);
      row.appendChild(statusEl);
      row.appendChild(duration);
      list.appendChild(row);
    }
  }

  function planBack() {
    // Beim Schritt zurueck den Preview-Plan auf dem Server entsorgen -
    // sonst entsteht beim erneuten "Vorschau anzeigen" ein neuer Plan
    // mit anderer ID und der alte verwaist als "Offen"-Eintrag.
    if (planPhase === 'preview' && currentPlan?.plan_id) {
      const oldId = currentPlan.plan_id;
      currentPlan = null;
      discardPlanOnServer(oldId).then((ok) => {
        if (ok) loadOutstanding().catch(() => {});
      });
    }
    showPlanPhase('input');
  }

  function planDiscard() {
    // Expliziter "Plan verwerfen"-Button in der Preview - laeuft durch
    // dieselbe Discard-Logik wie Abbrechen, ist aber explizit benannt
    // damit der User weiss dass er den Plan wegwirft (nicht nur das Modal).
    if (!currentPlan?.plan_id) {
      closePlanModal();
      return;
    }
    const targetCount = currentPlan.target_count || (currentPlan.actions?.length ?? 0);
    const msg = retryDeviceIds
      ? 'Plan komplett verwerfen?\n\nDamit verschwindet die "offen"-Markierung von ALLEN Geräten dieses Plans, nicht nur dem aktuell ausgewählten.'
      : `Plan komplett verwerfen?\n\nBetrifft ${targetCount} Gerät(e).`;
    if (!confirm(msg)) return;
    closePlanModal({ discardPlan: true });
    showToast('Plan verworfen.');
  }

  async function doScheduleAutoRetry(report) {
    if (!currentPlan) return;
    const failedIds = report.results
      .filter((r) => r.status === 'Fehlgeschlagen')
      .map((r) => r.device_id);
    if (!failedIds.length) return;
    try {
      const response = await apiPost('/api/retry/schedule', {
        plan_id: currentPlan.plan_id,
        device_ids: failedIds,
        interval_s: 180,
        max_duration_s: 3600,
      });
      if (response.status === 401) { handleSessionLost(); return; }
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        showToast(body.detail || `Fehler ${response.status}`, true);
        return;
      }
      showToast(`Auto-Retry für ${failedIds.length} Gerät(e) aktiv (alle 3 min, max 1 h).`);
      pollRetryStatus().catch(() => {});
      closePlanModal();
    } catch (err) {
      showToast(err.message, true);
    }
  }

  async function doRetryFailed(report) {
    const failedIds = report.results
      .filter((r) => r.status === 'Fehlgeschlagen')
      .map((r) => r.device_id);
    if (!failedIds.length) return;
    retryDeviceIds = failedIds;
    // Vorschau auf die fehlgeschlagenen filtern
    const filtered = {
      ...currentPlan,
      actions: currentPlan.actions.filter((a) => failedIds.includes(a.device_id)),
    };
    filtered.to_apply_count = filtered.actions.filter((a) => a.diff_kind !== 'skip').length;
    filtered.skip_count = filtered.actions.length - filtered.to_apply_count;
    filtered.target_count = filtered.actions.length;
    renderPreview(filtered);
    showPlanPhase('preview');
  }

  function showPlanError(msg) {
    const errorBox = $('#plan-modal-error');
    errorBox.textContent = msg;
    errorBox.hidden = false;
  }

  // -------------------- Discovery (Auto-Suggest) --------------------

  function _pickDiscoveryDevice() {
    // Erstes global ausgewaehltes Geraet — sonst null.
    if (state.selectedDeviceIds.size === 0) return null;
    for (const id of state.selectedDeviceIds) {
      const d = state.devices.find((x) => x.id === id);
      if (d) return d;
    }
    return null;
  }

  // -------------------- Profile (Templates) --------------------

  let planProfiles = [];

  async function loadPlanProfiles() {
    const select = $('#pl-profile-select');
    select.innerHTML = '<option value="">(keine)</option>';
    $('#pl-profile-delete').hidden = true;
    try {
      const response = await apiGet('/api/profiles');
      if (!response.ok) return;
      const data = await response.json();
      const wantedSubsystem =
        (planMode === 'route' || planMode === 'route-update')
          ? 'routes' : 'firewall_alias';
      planProfiles = (data.profiles || []).filter((p) => p.subsystem === wantedSubsystem);
      for (const p of planProfiles) {
        const opt = document.createElement('option');
        opt.value = p.id;
        opt.textContent = p.name;
        select.appendChild(opt);
      }
    } catch (_) {}
  }

  function applyProfile(profileId) {
    const profile = planProfiles.find((p) => p.id === profileId);
    $('#pl-profile-delete').hidden = !profileId;
    if (!profile) return;
    const s = profile.spec || {};
    if (planMode === 'route') {
      $('#pl-route-network').value = s.network || '';
      $('#pl-route-gateway').value = s.gateway || '';
      $('#pl-route-descr').value = s.descr || '';
      $('#pl-route-disabled').checked = !!s.disabled;
    } else {
      $('#pl-alias-name').value = s.name || '';
      $('#pl-alias-type').value = s.type || 'host';
      const content = s.content;
      $('#pl-alias-content').value = Array.isArray(content) ? content.join(', ') : (content || '');
      $('#pl-alias-descr').value = s.descr || '';
      $('#pl-alias-merge').checked = (s.merge_mode === 'append');
    }
  }

  async function deleteCurrentProfile() {
    const id = $('#pl-profile-select').value;
    if (!id) return;
    const profile = planProfiles.find((p) => p.id === id);
    if (!profile) return;
    if (!confirm(`Vorlage "${profile.name}" löschen?`)) return;
    try {
      const response = await apiDelete(`/api/profiles/${encodeURIComponent(id)}`);
      if (response.status === 401) { handleSessionLost(); return; }
      if (response.status !== 204 && !response.ok) {
        const body = await response.json().catch(() => ({}));
        showToast(body.detail || `Fehler ${response.status}`, true);
        return;
      }
      showToast(`Vorlage "${profile.name}" gelöscht.`);
      await loadPlanProfiles();
    } catch (err) {
      showToast(err.message, true);
    }
  }

  async function saveCurrentAsProfile() {
    const name = prompt('Name der Vorlage:');
    if (!name) return;
    let spec;
    let action;
    let subsystem;
    if (planMode === 'route' || planMode === 'route-update') {
      spec = {
        network: $('#pl-route-network').value.trim(),
        gateway: $('#pl-route-gateway').value.trim(),
        descr: $('#pl-route-descr').value.trim(),
        disabled: $('#pl-route-disabled').checked,
      };
      action = 'add_route';
      subsystem = 'routes';
      if (!spec.network || !spec.gateway) {
        showToast('Bitte Netzwerk und Gateway ausfüllen.', true);
        return;
      }
    } else {
      const contentRaw = $('#pl-alias-content').value.trim();
      const content = contentRaw
        ? contentRaw.split(',').map((c) => c.trim()).filter((c) => c.length > 0)
        : [];
      spec = {
        name: $('#pl-alias-name').value.trim(),
        type: $('#pl-alias-type').value,
        content,
        descr: $('#pl-alias-descr').value.trim(),
        merge_mode: $('#pl-alias-merge').checked ? 'append' : 'create',
      };
      action = $('#pl-alias-merge').checked ? 'append_alias' : 'add_alias';
      subsystem = 'firewall_alias';
      if (!spec.name || !content.length) {
        showToast('Bitte Alias-Name und Inhalte ausfüllen.', true);
        return;
      }
    }
    try {
      const response = await apiPost('/api/profiles', {
        name: name.trim(),
        action,
        subsystem,
        default_selector: 'all',
        spec,
      });
      if (response.status === 401) { handleSessionLost(); return; }
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        showToast(body.detail || `Fehler ${response.status}`, true);
        return;
      }
      showToast(`Vorlage "${name}" gespeichert.`);
      await loadPlanProfiles();
    } catch (err) {
      showToast(err.message, true);
    }
  }

  async function loadGatewaySuggestions() {
    const device = _pickDiscoveryDevice();
    if (!device) {
      showToast('Mindestens ein Zielgerät auswählen.', true);
      return;
    }
    const btn = $('#pl-load-gateways');
    btn.disabled = true;
    btn.textContent = 'Lade von ' + device.name + '…';
    try {
      const response = await apiGet(`/api/discover/devices/${encodeURIComponent(device.id)}/gateways`);
      if (response.status === 401) { handleSessionLost(); return; }
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        showToast(body.detail || `Fehler ${response.status}`, true);
        return;
      }
      const data = await response.json();
      const dl = $('#pl-gateway-suggestions');
      dl.innerHTML = '';
      gatewaySuggestionNames = new Set();
      for (const g of data.gateways) {
        const opt = document.createElement('option');
        opt.value = g.name;
        opt.label = g.address ? `${g.name} — ${g.address} (${g.status})` : g.name;
        dl.appendChild(opt);
        gatewaySuggestionNames.add(g.name);
      }
      showToast(`${data.gateways.length} Gateway(s) gefunden auf ${device.name}.`);
    } catch (err) {
      showToast(err.message, true);
    } finally {
      btn.disabled = false;
      btn.textContent = 'Vorschläge laden';
    }
  }

  // Map<name, type> aus der letzten discover/aliases-Antwort, damit wir bei
  // Datalist-Auswahl den Typ automatisch ins Dropdown uebernehmen koennen.
  let aliasSuggestionTypes = new Map();
  // Set<name> der zuletzt geladenen Gateways — fuer F19 Re-Browse-Trick.
  let gatewaySuggestionNames = new Set();

  // F19 v2: Wir verzichten auf die Chip-Liste (sprengt das Modal ab ~20 Eintraegen)
  // und sorgen stattdessen dafuer, dass das native Dropdown re-browsable bleibt.
  // Browser blenden datalist-Optionen aus wenn input.value exakt einer Option
  // entspricht. Workaround: beim erneuten Fokus/Mousedown den Wert temporaer
  // leeren — User sieht wieder alle Optionen, kann einen neuen waehlen.
  function enableDatalistRebrowse(inputId, knownValuesSetGetter) {
    const inp = $(`#${inputId}`);
    if (!inp) return;
    const tryClear = () => {
      const v = inp.value.trim();
      if (v && knownValuesSetGetter().has(v)) {
        inp.dataset.lastPick = v;
        inp.value = '';
      }
    };
    inp.addEventListener('focus', tryClear);
    inp.addEventListener('mousedown', () => {
      // Falls focus schon mal lief, mousedown trotzdem ausloesen damit
      // ein erneuter Klick auf das schon fokussierte Feld wieder leert.
      setTimeout(tryClear, 0);
    });
    inp.addEventListener('blur', () => {
      // Wenn nach dem Verlassen nichts gewaehlt wurde, alten Wert restaurieren
      // — sonst verliert der User seine vorige Auswahl beim Wegklicken.
      setTimeout(() => {
        if (!inp.value && inp.dataset.lastPick) {
          inp.value = inp.dataset.lastPick;
          inp.dispatchEvent(new Event('input', { bubbles: true }));
        }
        delete inp.dataset.lastPick;
      }, 150);
    });
  }

  async function loadAliasSuggestions() {
    const device = _pickDiscoveryDevice();
    if (!device) {
      showToast('Mindestens ein Zielgerät auswählen.', true);
      return;
    }
    const btn = $('#pl-load-aliases');
    btn.disabled = true;
    btn.textContent = 'Lade von ' + device.name + '…';
    try {
      const response = await apiGet(`/api/discover/devices/${encodeURIComponent(device.id)}/aliases`);
      if (response.status === 401) { handleSessionLost(); return; }
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        showToast(body.detail || `Fehler ${response.status}`, true);
        return;
      }
      const data = await response.json();
      const dl = $('#pl-alias-suggestions');
      dl.innerHTML = '';
      aliasSuggestionTypes = new Map();
      for (const a of data.aliases) {
        const opt = document.createElement('option');
        opt.value = a.name;
        opt.label = a.type ? `${a.name} (${a.type})` : a.name;
        dl.appendChild(opt);
        if (a.type) aliasSuggestionTypes.set(a.name, String(a.type).toLowerCase());
      }
      showToast(`${data.aliases.length} Alias(e) gefunden auf ${device.name}.`);
    } catch (err) {
      showToast(err.message, true);
    } finally {
      btn.disabled = false;
      btn.textContent = 'Vorschläge laden';
    }
  }

  function syncAliasTypeFromSuggestion() {
    // F17: Wenn der eingegebene Name exakt einer Suggestion entspricht,
    // setze das Typ-Dropdown automatisch — der bestehende Alias-Typ ist
    // bindend (siehe aliases._append), Default 'host' ist sonst meistens falsch.
    const nameInput = $('#pl-alias-name');
    const typeSelect = $('#pl-alias-type');
    if (!nameInput || !typeSelect) return;
    const t = aliasSuggestionTypes.get(nameInput.value.trim());
    if (!t) return;
    // Nur setzen wenn der Wert in den vorhandenen Options-Wert existiert,
    // sonst bleibt das Dropdown auf dem User-gewaehlten Wert.
    const known = Array.from(typeSelect.options).some((o) => o.value === t);
    if (known) typeSelect.value = t;
  }

  // -------------------- Bulk-Import (Firewalls) --------------------

  function openBulkModal() {
    $('#bk-fmt-csv').checked = true;
    $('#bk-fmt-json').checked = false;
    $('#bk-fmt-vault').checked = false;
    $('#bk-file').value = '';
    $('#bk-vault-file').value = '';
    $('#bk-vault-pw').value = '';
    $('#bulk-modal-error').hidden = true;
    $('#bulk-parse-errors').hidden = true;
    updateBulkFormatHint();
    $('#bulk-modal').hidden = false;
  }

  async function submitVaultImport() {
    const errorBox = $('#bulk-modal-error');
    errorBox.hidden = true;
    const file = $('#bk-vault-file').files[0];
    const password = $('#bk-vault-pw').value;
    if (!file || !password) {
      errorBox.textContent = 'Bitte Vault-Datei waehlen und Master-Passwort eingeben.';
      errorBox.hidden = false;
      return;
    }
    const form = new FormData();
    form.append('file', file);
    form.append('password', password);
    const btn = $('#bulk-submit-btn');
    btn.disabled = true;
    btn.textContent = 'Importiere…';
    try {
      const headers = {};
      const token = getToken();
      if (token) headers.Authorization = `Bearer ${token}`;
      const response = await fetch('/api/imports/vault-upload', {
        method: 'POST',
        headers,
        body: form,
      });
      if (response.status === 401) {
        const body = await response.json().catch(() => ({}));
        errorBox.textContent = body.detail || 'Master-Passwort falsch.';
        errorBox.hidden = false;
        return;
      }
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        errorBox.textContent = body.detail || `Fehler ${response.status}`;
        errorBox.hidden = false;
        return;
      }
      const result = await response.json();
      closeBulkModal();
      await loadInventory();
      pollHeartbeat();
      const skipText = result.skipped_existing.length
        ? ` (${result.skipped_existing.length} bereits vorhanden, übersprungen)`
        : '';
      showToast(`${result.added.length} Firewall(s) aus Quell-Tresor übernommen${skipText}.`);
    } catch (err) {
      errorBox.textContent = err.message;
      errorBox.hidden = false;
    } finally {
      btn.disabled = false;
      btn.textContent = 'Importieren';
    }
  }

  function closeBulkModal() {
    $('#bulk-modal').hidden = true;
  }

  function updateBulkFormatHint() {
    const isCsv = $('#bk-fmt-csv').checked;
    const isJson = $('#bk-fmt-json').checked;
    const isVault = $('#bk-fmt-vault').checked;
    // File-Feld umschalten: CSV/JSON nutzen #bk-file, Vault nutzt #bk-vault-file.
    $('#bulk-file-row').hidden = isVault;
    $('#bulk-vault-file-row').hidden = !isVault;
    $('#bulk-vault-pw-row').hidden = !isVault;
    if (isCsv) {
      $('#bk-format-hint').innerHTML =
        'CSV-Spalten: <code>name, host, port, tls_verify, tags, descr, api_key, api_secret</code>. ' +
        'Tags semikolon-getrennt. Header-Zeile zwingend.';
      $('#bk-file').setAttribute('accept', '.csv,text/csv');
    } else if (isJson) {
      $('#bk-format-hint').innerHTML =
        'JSON-Array von Objekten: <code>{name, host, port, tls_verify, tags[], descr, api_key, api_secret}</code>.';
      $('#bk-file').setAttribute('accept', '.json,application/json');
    }
  }

  async function submitBulkImport() {
    const errorBox = $('#bulk-modal-error');
    const parseErrBox = $('#bulk-parse-errors');
    errorBox.hidden = true;
    parseErrBox.hidden = true;

    // Vault-Import-Pfad ist ein eigener API-Endpoint (kein Multipart-Upload).
    if ($('#bk-fmt-vault').checked) {
      return submitVaultImport();
    }

    const file = $('#bk-file').files[0];
    if (!file) {
      errorBox.textContent = 'Bitte eine Datei auswählen.';
      errorBox.hidden = false;
      return;
    }

    const fmt = $('#bk-fmt-csv').checked ? 'csv' : 'json';
    const form = new FormData();
    form.append('file', file);
    form.append('format', fmt);

    const btn = $('#bulk-submit-btn');
    btn.disabled = true;
    btn.textContent = 'Importiere…';
    try {
      const headers = {};
      const token = getToken();
      if (token) headers.Authorization = `Bearer ${token}`;
      const response = await fetch('/api/imports/devices', {
        method: 'POST', body: form, headers,
      });
      if (response.status === 401) { handleSessionLost(); return; }
      if (response.status === 400) {
        const body = await response.json().catch(() => ({}));
        const detail = body.detail || {};
        const msg = detail.message || 'Parse-Fehler';
        const errors = detail.errors || [];
        errorBox.textContent = msg;
        errorBox.hidden = false;
        if (errors.length) {
          parseErrBox.textContent = errors.join('\n');
          parseErrBox.hidden = false;
        }
        return;
      }
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        errorBox.textContent = body.detail || `Fehler ${response.status}`;
        errorBox.hidden = false;
        return;
      }
      const result = await response.json();
      closeBulkModal();
      await loadInventory();
      pollHeartbeat();
      const skipText = result.skipped_existing.length
        ? ` (${result.skipped_existing.length} bereits vorhanden, übersprungen)`
        : '';
      showToast(`${result.added.length} Firewall(s) importiert${skipText}.`);
    } catch (err) {
      errorBox.textContent = err.message;
      errorBox.hidden = false;
    } finally {
      btn.disabled = false;
      btn.textContent = 'Importieren';
    }
  }

  // -------------------- User-Verwaltung (admin-only) --------------------

  async function openUsersModal() {
    $('#users-modal').hidden = false;
    $('#users-add-form').hidden = true;
    $('#users-modal-error').hidden = true;
    await reloadUsers();
  }

  function closeUsersModal() {
    $('#users-modal').hidden = true;
  }

  async function reloadUsers() {
    const list = $('#users-list');
    list.innerHTML = '<div class="audit-empty">Lade…</div>';
    try {
      const response = await apiGet('/api/users');
      if (response.status === 401) { handleSessionLost(); return; }
      if (response.status === 403) {
        list.innerHTML = '<div class="audit-empty">Kein Zugriff (nur Admins).</div>';
        return;
      }
      if (!response.ok) {
        list.innerHTML = `<div class="audit-empty">Fehler ${response.status}</div>`;
        return;
      }
      const data = await response.json();
      renderUsersList(data.users || []);
    } catch (err) {
      list.innerHTML = `<div class="audit-empty">${err.message}</div>`;
    }
  }

  function renderUsersList(users) {
    const list = $('#users-list');
    list.innerHTML = '';
    if (!users.length) {
      list.innerHTML = '<div class="audit-empty">Keine User angelegt.</div>';
      return;
    }
    for (const u of users) {
      const row = document.createElement('div');
      row.className = 'user-row';
      if (u.disabled) row.classList.add('disabled');

      const nameBlock = document.createElement('div');
      const name = document.createElement('div');
      name.className = 'user-row-name';
      name.textContent = u.username + (u.disabled ? ' (deaktiviert)' : '');
      const meta = document.createElement('div');
      meta.className = 'user-row-meta';
      const lastLogin = u.last_login_at_iso
        ? `letzter Login: ${formatAuditTime(u.last_login_at_iso)}`
        : 'noch kein Login';
      meta.textContent = `${lastLogin} · Tags: ${u.allowed_tags.length ? u.allowed_tags.join(', ') : 'alle'}`;
      nameBlock.appendChild(name);
      nameBlock.appendChild(meta);
      row.appendChild(nameBlock);

      // Rolle als Select
      const roleSelect = document.createElement('select');
      roleSelect.className = 'user-role-select form-select';
      for (const r of ['viewer', 'operator', 'admin']) {
        const opt = document.createElement('option');
        opt.value = r;
        opt.textContent = r;
        if (u.role === r) opt.selected = true;
        roleSelect.appendChild(opt);
      }
      roleSelect.addEventListener('change', () => updateUserField(u.id, { role: roleSelect.value }));
      row.appendChild(roleSelect);

      const actions = document.createElement('div');
      actions.className = 'user-row-actions';

      const toggleBtn = document.createElement('button');
      toggleBtn.className = 'btn-link';
      toggleBtn.textContent = u.disabled ? 'aktivieren' : 'deaktivieren';
      toggleBtn.addEventListener('click', () => updateUserField(u.id, { disabled: !u.disabled }));
      actions.appendChild(toggleBtn);

      const pwBtn = document.createElement('button');
      pwBtn.className = 'btn-link';
      pwBtn.textContent = 'PW reset';
      pwBtn.addEventListener('click', () => adminResetPassword(u));
      actions.appendChild(pwBtn);

      const delBtn = document.createElement('button');
      delBtn.className = 'btn-link';
      delBtn.textContent = 'loeschen';
      delBtn.addEventListener('click', () => deleteUser(u));
      actions.appendChild(delBtn);

      row.appendChild(actions);
      list.appendChild(row);
    }
  }

  async function updateUserField(userId, body) {
    try {
      const response = await apiPatch(`/api/users/${userId}`, body);
      if (response.status === 401) { handleSessionLost(); return; }
      if (!response.ok) {
        const respBody = await response.json().catch(() => ({}));
        showToast(respBody.detail || `Fehler ${response.status}`, true);
        return;
      }
      await reloadUsers();
    } catch (err) {
      showToast(err.message, true);
    }
  }

  async function adminResetPassword(user) {
    const newPw = prompt(`Neues Passwort fuer "${user.username}" (min. 12 Zeichen):`);
    if (newPw === null) return;
    if (newPw.length < 12) {
      showToast('Passwort muss mindestens 12 Zeichen haben.', true);
      return;
    }
    try {
      const response = await apiPost(
        `/api/users/${user.id}/password`,
        { new_password: newPw },
      );
      if (response.status === 401) { handleSessionLost(); return; }
      if (response.status !== 204 && !response.ok) {
        const body = await response.json().catch(() => ({}));
        showToast(body.detail || `Fehler ${response.status}`, true);
        return;
      }
      showToast(`Passwort von "${user.username}" zurueckgesetzt.`);
    } catch (err) {
      showToast(err.message, true);
    }
  }

  async function deleteUser(user) {
    if (!confirm(`User "${user.username}" wirklich loeschen?`)) return;
    try {
      const response = await apiDelete(`/api/users/${user.id}`);
      if (response.status === 401) { handleSessionLost(); return; }
      if (response.status !== 204 && !response.ok) {
        const body = await response.json().catch(() => ({}));
        showToast(body.detail || `Fehler ${response.status}`, true);
        return;
      }
      showToast(`User "${user.username}" geloescht.`);
      await reloadUsers();
    } catch (err) {
      showToast(err.message, true);
    }
  }

  function openAddUserForm() {
    $('#nu-username').value = '';
    $('#nu-password').value = '';
    $('#nu-tags').value = '';
    $('#nu-role').value = 'operator';
    $('#users-add-error').hidden = true;
    $('#users-add-form').hidden = false;
    setTimeout(() => $('#nu-username').focus(), 0);
  }

  function closeAddUserForm() {
    $('#users-add-form').hidden = true;
  }

  async function submitAddUser() {
    const username = $('#nu-username').value.trim();
    const password = $('#nu-password').value;
    const role = $('#nu-role').value;
    const tagsRaw = $('#nu-tags').value.trim();
    const allowed_tags = tagsRaw
      ? tagsRaw.split(',').map((t) => t.trim()).filter((t) => t.length > 0)
      : [];
    const errorBox = $('#users-add-error');
    errorBox.hidden = true;
    if (!username) return showUserAddError('Benutzername fehlt.');
    if (password.length < 12) return showUserAddError('Passwort muss mindestens 12 Zeichen haben.');
    try {
      const response = await apiPost('/api/users', {
        username, password, role, allowed_tags,
      });
      if (response.status === 401) { handleSessionLost(); return; }
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        return showUserAddError(body.detail || `Fehler ${response.status}`);
      }
      showToast(`User "${username}" angelegt.`);
      closeAddUserForm();
      await reloadUsers();
    } catch (err) {
      showUserAddError(err.message);
    }
  }

  function showUserAddError(msg) {
    const box = $('#users-add-error');
    box.textContent = msg;
    box.hidden = false;
  }

  // -------------------- Tresor-Einstellungen (F5a + F5b) --------------------

  async function openVaultSettingsModal() {
    $('#vs-timeout').value = '';
    $('#vs-autobackup').checked = true;
    $('#vs-retention-pre-apply').value = '30';
    $('#vs-retention-scheduled').value = '90';
    $('#vs-sched-enabled').checked = false;
    $('#vs-sched-interval').value = '24';
    $('#vs-drift-enabled').checked = false;
    $('#vs-retry-enabled').checked = true;
    $('#vs-retry-max-hours').value = '168';
    $('#vs-retry-interval-min').value = '5';
    $('#vs-pw-current').value = '';
    $('#vs-pw-new1').value = '';
    $('#vs-pw-new2').value = '';
    $('#vs-timeout-error').hidden = true;
    $('#vs-timeout-ok').hidden = true;
    $('#vs-backup-error').hidden = true;
    $('#vs-backup-ok').hidden = true;
    $('#vs-pw-error').hidden = true;
    $('#vs-pw-ok').hidden = true;
    $('#vault-settings-modal').hidden = false;
    setTimeout(() => $('#vs-timeout').focus(), 0);
    // Aktuelle Settings laden — kein blockierendes Warten
    try {
      const r = await apiGet('/api/vaults/settings');
      if (r.status === 401) { handleSessionLost(); return; }
      if (r.ok) {
        const data = await r.json();
        $('#vs-timeout').value = data.inactivity_minutes;
        $('#vs-autobackup').checked = data.auto_backup_before_apply !== false;
        if (typeof data.backup_retention_pre_apply === 'number') {
          $('#vs-retention-pre-apply').value = data.backup_retention_pre_apply;
        }
        if (typeof data.backup_retention_scheduled === 'number') {
          $('#vs-retention-scheduled').value = data.backup_retention_scheduled;
        }
        $('#vs-sched-enabled').checked = data.scheduled_backup_enabled === true;
        if (typeof data.scheduled_backup_interval_hours === 'number') {
          $('#vs-sched-interval').value = data.scheduled_backup_interval_hours;
        }
        $('#vs-drift-enabled').checked = data.drift_detection_enabled === true;
        $('#vs-retry-enabled').checked = data.auto_retry_enabled !== false;
        if (typeof data.auto_retry_max_hours === 'number') {
          $('#vs-retry-max-hours').value = data.auto_retry_max_hours;
        }
        if (typeof data.auto_retry_interval_minutes === 'number') {
          $('#vs-retry-interval-min').value = data.auto_retry_interval_minutes;
        }
      }
    } catch (_e) { /* Modal kann auch mit leerem Feld bedient werden */ }
  }

  async function saveBackupSettings() {
    const errBox = $('#vs-backup-error');
    const okBox = $('#vs-backup-ok');
    errBox.hidden = true;
    okBox.hidden = true;
    const autobackup = $('#vs-autobackup').checked;
    const preApply = parseInt($('#vs-retention-pre-apply').value, 10);
    const scheduled = parseInt($('#vs-retention-scheduled').value, 10);
    const schedEnabled = $('#vs-sched-enabled').checked;
    const schedInterval = parseInt($('#vs-sched-interval').value, 10);
    const driftEnabled = $('#vs-drift-enabled').checked;
    const retryEnabled = $('#vs-retry-enabled').checked;
    const retryMaxH = parseInt($('#vs-retry-max-hours').value, 10);
    const retryIntervalMin = parseInt($('#vs-retry-interval-min').value, 10);
    if (!Number.isFinite(retryMaxH) || retryMaxH < 1 || retryMaxH > 720) {
      errBox.textContent = 'Retry-Wartezeit muss zwischen 1 und 720 Stunden liegen.';
      errBox.hidden = false;
      return;
    }
    if (!Number.isFinite(retryIntervalMin) || retryIntervalMin < 1 || retryIntervalMin > 120) {
      errBox.textContent = 'Retry-Intervall muss zwischen 1 und 120 Minuten liegen.';
      errBox.hidden = false;
      return;
    }
    if (!Number.isFinite(preApply) || preApply < 1 || preApply > 500) {
      errBox.textContent = 'Apply-Retention muss zwischen 1 und 500 liegen.';
      errBox.hidden = false;
      return;
    }
    if (!Number.isFinite(scheduled) || scheduled < 1 || scheduled > 500) {
      errBox.textContent = 'Scheduled-Retention muss zwischen 1 und 500 liegen.';
      errBox.hidden = false;
      return;
    }
    if (!Number.isFinite(schedInterval) || schedInterval < 1 || schedInterval > 168) {
      errBox.textContent = 'Backup-Intervall muss zwischen 1 und 168 Stunden liegen.';
      errBox.hidden = false;
      return;
    }
    // Timeout-Wert muss mitgeschickt werden weil das Schema ihn als Pflichtfeld
    // hat. Wenn das Feld leer ist (User hat noch nichts geaendert), nehmen wir
    // den aktuell angezeigten Wert oder fallen auf 10.
    const minutes = parseInt($('#vs-timeout').value, 10) || 10;
    const btn = $('#vs-backup-save');
    btn.disabled = true;
    btn.textContent = 'Speichere…';
    try {
      const response = await apiPost('/api/vaults/settings', {
        inactivity_minutes: minutes,
        auto_backup_before_apply: autobackup,
        backup_retention_pre_apply: preApply,
        backup_retention_scheduled: scheduled,
        scheduled_backup_enabled: schedEnabled,
        scheduled_backup_interval_hours: schedInterval,
        drift_detection_enabled: driftEnabled,
        auto_retry_enabled: retryEnabled,
        auto_retry_max_hours: retryMaxH,
        auto_retry_interval_minutes: retryIntervalMin,
      });
      if (response.status === 401) { handleSessionLost(); return; }
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        errBox.textContent = body.detail || `Fehler ${response.status}`;
        errBox.hidden = false;
        return;
      }
      okBox.hidden = false;
      showToast('Backup-Einstellungen gespeichert.');
    } catch (err) {
      errBox.textContent = err.message;
      errBox.hidden = false;
    } finally {
      btn.disabled = false;
      btn.textContent = 'Backup-Einstellungen speichern';
    }
  }

  function closeVaultSettingsModal() {
    $('#vault-settings-modal').hidden = true;
  }

  // -------------------- Backup-History-Modal --------------------

  let currentBackupDeviceId = null;
  let bhLoadedForDeviceId = null;

  async function openBackupHistoryModal(deviceId) {
    // Backup-Liste ist jetzt ein Tab im Device-Modal. Wenn das Modal noch
    // nicht offen ist (z.B. Klick aufs Backup-Badge auf der Kachel),
    // oeffnen wir es und schalten auf den Backups-Tab.
    if ($('#device-modal').hidden) {
      openDeviceModal(deviceId);
    }
    switchDeviceTab('backups');
  }

  async function loadBackupsTab(force = false) {
    if (!currentDeviceId) return;
    if (!force && bhLoadedForDeviceId === currentDeviceId) return;
    currentBackupDeviceId = currentDeviceId;
    bhLoadedForDeviceId = currentDeviceId;
    $('#bh-list').innerHTML = '<div class="form-hint">Lade…</div>';
    await refreshBackupHistory();
  }

  async function refreshBackupHistory() {
    if (!currentBackupDeviceId) return;
    try {
      const r = await apiGet(
        `/api/inventory/devices/${encodeURIComponent(currentBackupDeviceId)}/backups`,
      );
      if (r.status === 401) { handleSessionLost(); return; }
      if (!r.ok) {
        $('#bh-list').innerHTML = '<div class="form-error">Backup-Liste nicht abrufbar.</div>';
        return;
      }
      const data = await r.json();
      renderBackupList(data.backups || []);
    } catch (e) {
      $('#bh-list').innerHTML = `<div class="form-error">${e.message}</div>`;
    }
  }

  function renderBackupList(backups) {
    const list = $('#bh-list');
    list.innerHTML = '';
    if (backups.length === 0) {
      list.innerHTML = '<div class="form-hint">Noch keine Backups fuer dieses Geraet.</div>';
      return;
    }
    for (const b of backups) {
      const row = document.createElement('div');
      row.className = 'backup-row';
      const triggerLabel = ({
        'pre-apply': 'vor Apply',
        'manual': 'manuell',
        'scheduled': 'geplant',
      })[b.trigger] || b.trigger;
      const sizeKb = (b.size_compressed / 1024).toFixed(1);
      row.innerHTML = `
        <div class="backup-row-main">
          <div class="backup-row-ts">${b.timestamp_utc}</div>
          <div class="backup-row-meta">
            <span class="backup-row-trigger">${triggerLabel}</span>
            <span class="backup-row-size">${sizeKb} KB gzip</span>
            ${b.related_plan_id ? `<span class="backup-row-plan">Plan ${b.related_plan_id}</span>` : ''}
          </div>
        </div>
        <div class="backup-row-actions">
          <button class="btn-link" data-action="download" data-id="${b.id}">Download</button>
          <button class="btn-link btn-danger-text" data-action="delete" data-id="${b.id}">Loeschen</button>
        </div>
      `;
      list.appendChild(row);
    }
    list.querySelectorAll('[data-action="download"]').forEach((btn) => {
      btn.addEventListener('click', () => downloadStoredBackup(btn.dataset.id));
    });
    list.querySelectorAll('[data-action="delete"]').forEach((btn) => {
      btn.addEventListener('click', () => deleteStoredBackup(btn.dataset.id));
    });
  }

  async function downloadStoredBackup(backupId) {
    if (!currentBackupDeviceId) return;
    const url = (
      `/api/inventory/devices/${encodeURIComponent(currentBackupDeviceId)}`
      + `/backups/${encodeURIComponent(backupId)}`
    );
    const t = getToken();
    const headers = t ? { Authorization: `Bearer ${t}` } : {};
    try {
      const response = await fetch(url, { headers });
      if (response.status === 401) { handleSessionLost(); return; }
      if (!response.ok) {
        showToast(`Download fehlgeschlagen (${response.status}).`, true);
        return;
      }
      let filename = `opnsense-config-${backupId}.xml`;
      const disposition = response.headers.get('Content-Disposition') || '';
      const match = disposition.match(/filename="?([^";]+)"?/i);
      if (match) filename = match[1];
      const blob = await response.blob();
      const dlUrl = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = dlUrl;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(dlUrl);
      showToast(`${filename} (${formatBytes(blob.size)}) heruntergeladen.`);
    } catch (e) {
      showToast(e.message, true);
    }
  }

  async function deleteStoredBackup(backupId) {
    if (!currentBackupDeviceId) return;
    if (!confirm('Backup wirklich loeschen?\n\nDer lokale Snapshot wird endgueltig entfernt.')) {
      return;
    }
    const url = (
      `/api/inventory/devices/${encodeURIComponent(currentBackupDeviceId)}`
      + `/backups/${encodeURIComponent(backupId)}`
    );
    const t = getToken();
    const headers = t ? { Authorization: `Bearer ${t}` } : {};
    try {
      const response = await fetch(url, { method: 'DELETE', headers });
      if (response.status === 401) { handleSessionLost(); return; }
      if (!response.ok && response.status !== 404) {
        const body = await response.json().catch(() => ({}));
        showToast(body.detail || `Loeschen fehlgeschlagen (${response.status}).`, true);
        return;
      }
      showToast('Backup geloescht.');
      await refreshBackupHistory();
      await loadBackupCounts();
    } catch (e) {
      showToast(e.message, true);
    }
  }

  // closeBackupHistoryModal entfaellt - Backup-History ist jetzt ein Tab
  // im Device-Modal; das Modal-Schliessen raeumt den State per closeDeviceModal auf.

  // -------------------- Cert-Detail-Modal --------------------

  function openCertDetailModal(deviceId) {
    const device = state.devices.find((d) => d.id === deviceId);
    const info = state.certsByDevice[deviceId];
    $('#cd-title').textContent = device
      ? `Zertifikate: ${device.name}`
      : 'Zertifikate';
    if (!info || !info.certs || info.certs.length === 0) {
      $('#cd-list').innerHTML = (
        '<div class="form-hint">Keine Zertifikate fuer dieses Geraet inventarisiert '
        + 'oder OPNsense nicht erreichbar.</div>'
      );
    } else {
      renderCertList(info.certs);
    }
    $('#cert-detail-modal').hidden = false;
  }

  function renderCertList(certs) {
    const list = $('#cd-list');
    list.innerHTML = '';
    // Sortieren: kuerzeste Restlaufzeit zuerst (abgelaufen ganz oben)
    const sorted = [...certs].sort((a, b) => {
      const aDays = a.days_until_expiry ?? 999999;
      const bDays = b.days_until_expiry ?? 999999;
      return aDays - bDays;
    });
    for (const c of sorted) {
      const row = document.createElement('div');
      row.className = 'cert-row';
      let severityClass = '';
      let daysLabel = '–';
      if (c.days_until_expiry !== null && c.days_until_expiry !== undefined) {
        const d = c.days_until_expiry;
        if (d < 0) {
          severityClass = 'cert-row-critical';
          daysLabel = `abgelaufen vor ${Math.abs(d)}d`;
        } else if (d < 7) {
          severityClass = 'cert-row-critical';
          daysLabel = `${d}d Restlaufzeit`;
        } else if (d < 30) {
          severityClass = 'cert-row-warning';
          daysLabel = `${d}d Restlaufzeit`;
        } else {
          daysLabel = `${d}d Restlaufzeit`;
        }
      }
      if (severityClass) row.classList.add(severityClass);
      row.innerHTML = `
        <div class="cert-row-main">
          <div class="cert-row-name">${c.descr || c.common_name || '(ohne Beschreibung)'}</div>
          <div class="cert-row-meta">
            <span>CN: ${c.common_name || '–'}</span>
            <span>Aussteller: ${c.issuer || '–'}</span>
            ${c.in_use ? '<span class="cert-row-inuse">IN USE</span>' : ''}
          </div>
          <div class="cert-row-expiry">
            <span class="cert-row-date">${c.not_after_iso || 'kein Ablauf-Datum'}</span>
            <span class="cert-row-days">${daysLabel}</span>
          </div>
        </div>
      `;
      list.appendChild(row);
    }
  }

  function closeCertDetailModal() {
    $('#cert-detail-modal').hidden = true;
  }

  async function saveInactivityTimeout() {
    const errBox = $('#vs-timeout-error');
    const okBox = $('#vs-timeout-ok');
    errBox.hidden = true;
    okBox.hidden = true;
    const raw = $('#vs-timeout').value.trim();
    const minutes = parseInt(raw, 10);
    if (!Number.isFinite(minutes) || minutes < 1 || minutes > 240) {
      errBox.textContent = 'Bitte eine Zahl zwischen 1 und 240 eingeben.';
      errBox.hidden = false;
      return;
    }
    const btn = $('#vs-timeout-save');
    btn.disabled = true;
    btn.textContent = 'Speichere…';
    try {
      const response = await apiPost('/api/vaults/settings', {
        inactivity_minutes: minutes,
      });
      if (response.status === 401) { handleSessionLost(); return; }
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        errBox.textContent = body.detail || `Fehler ${response.status}`;
        errBox.hidden = false;
        return;
      }
      okBox.hidden = false;
      // Footer-Anzeige + Session-State synchron halten — sonst zeigt der Footer
      // den alten 10-Min-Wert obwohl die Session schon mit der neuen Frist laeuft.
      $('#timeout-display').textContent = minutes;
      if (state.sessionInfo) {
        state.sessionInfo.inactivity_timeout_s = minutes * 60;
      }
      showToast(`Auto-Sperre auf ${minutes} Minuten gesetzt.`);
    } catch (err) {
      errBox.textContent = err.message;
      errBox.hidden = false;
    } finally {
      btn.disabled = false;
      btn.textContent = 'Speichern';
    }
  }

  async function changeVaultPassword() {
    const errBox = $('#vs-pw-error');
    const okBox = $('#vs-pw-ok');
    errBox.hidden = true;
    okBox.hidden = true;
    const current = $('#vs-pw-current').value;
    const n1 = $('#vs-pw-new1').value;
    const n2 = $('#vs-pw-new2').value;
    if (!current) {
      errBox.textContent = 'Aktuelles Passwort eingeben.';
      errBox.hidden = false;
      return;
    }
    if (n1.length < 12) {
      errBox.textContent = 'Neues Passwort muss mindestens 12 Zeichen lang sein.';
      errBox.hidden = false;
      return;
    }
    if (n1 !== n2) {
      errBox.textContent = 'Die beiden neuen Passwoerter stimmen nicht ueberein.';
      errBox.hidden = false;
      return;
    }
    const btn = $('#vs-pw-submit');
    btn.disabled = true;
    btn.textContent = 'Aendere…';
    try {
      const response = await apiPost('/api/vaults/change-password', {
        current_password: current,
        new_password: n1,
        new_password_repeat: n2,
      });
      if (response.status === 401) {
        errBox.textContent = 'Aktuelles Passwort ist falsch.';
        errBox.hidden = false;
        return;
      }
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        errBox.textContent = body.detail || `Fehler ${response.status}`;
        errBox.hidden = false;
        return;
      }
      okBox.hidden = false;
      $('#vs-pw-current').value = '';
      $('#vs-pw-new1').value = '';
      $('#vs-pw-new2').value = '';
      showToast('Tresor-Master-Passwort geaendert.');
    } catch (err) {
      errBox.textContent = err.message;
      errBox.hidden = false;
    } finally {
      btn.disabled = false;
      btn.textContent = 'Passwort aendern';
    }
  }

  // -------------------- Vault-Switch (admin-only, Multi-Mode) --------------------

  function openVaultSwitchModal() {
    $('#vsw-path').value = '';
    $('#vsw-pw').value = '';
    $('#vsw-create').checked = false;
    $('#vault-switch-error').hidden = true;
    $('#vault-switch-modal').hidden = false;
    setTimeout(() => $('#vsw-path').focus(), 0);
  }

  function closeVaultSwitchModal() {
    $('#vault-switch-modal').hidden = true;
  }

  async function submitVaultSwitch() {
    const path = $('#vsw-path').value.trim();
    const pw = $('#vsw-pw').value;
    const createIfMissing = $('#vsw-create').checked;
    const errorBox = $('#vault-switch-error');
    errorBox.hidden = true;
    if (!path) return showVaultSwitchError('Pfad zum neuen Tresor fehlt.');
    if (pw.length < 12) return showVaultSwitchError('Passwort muss mindestens 12 Zeichen haben.');
    if (!confirm(`Tresor wirklich wechseln?\n\nAlle anderen Sessions werden invalidiert.`)) return;
    const btn = $('#vault-switch-submit');
    btn.disabled = true;
    btn.textContent = 'Wechsle…';
    try {
      const response = await apiPost('/api/vaults/switch', {
        vault_path: path,
        password: pw,
        create_if_missing: createIfMissing,
      });
      if (response.status === 401) {
        return showVaultSwitchError('Master-Passwort falsch.');
      }
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        return showVaultSwitchError(body.detail || `Fehler ${response.status}`);
      }
      const result = await response.json();
      closeVaultSwitchModal();
      closeUsersModal();
      const desc = result.created === 'true' ? 'neu angelegt' : 'entsperrt';
      showToast(`Tresor gewechselt (${desc}). ${result.revoked_sessions} Session(s) invalidiert.`);
      await loadInventory();
      pollHeartbeat();
    } catch (err) {
      showVaultSwitchError(err.message);
    } finally {
      btn.disabled = false;
      btn.textContent = 'Wechseln';
    }
  }

  function showVaultSwitchError(msg) {
    const box = $('#vault-switch-error');
    box.textContent = msg;
    box.hidden = false;
  }

  // -------------------- Self-Service Passwort --------------------

  function openSelfPasswordModal() {
    $('#pwself-current').value = '';
    $('#pwself-new1').value = '';
    $('#pwself-new2').value = '';
    $('#pwself-error').hidden = true;
    $('#pwself-modal').hidden = false;
    setTimeout(() => $('#pwself-current').focus(), 0);
  }

  function closeSelfPasswordModal() {
    $('#pwself-modal').hidden = true;
  }

  async function submitSelfPassword() {
    const current = $('#pwself-current').value;
    const n1 = $('#pwself-new1').value;
    const n2 = $('#pwself-new2').value;
    const errorBox = $('#pwself-error');
    errorBox.hidden = true;
    if (n1.length < 12) {
      errorBox.textContent = 'Neues Passwort muss mindestens 12 Zeichen haben.';
      errorBox.hidden = false;
      return;
    }
    if (n1 !== n2) {
      errorBox.textContent = 'Die beiden Passwoerter stimmen nicht ueberein.';
      errorBox.hidden = false;
      return;
    }
    try {
      const response = await apiPost('/api/users/me/password', {
        current_password: current,
        new_password: n1,
      });
      if (response.status === 401) {
        errorBox.textContent = 'Aktuelles Passwort falsch.';
        errorBox.hidden = false;
        return;
      }
      if (response.status !== 204 && !response.ok) {
        const body = await response.json().catch(() => ({}));
        errorBox.textContent = body.detail || `Fehler ${response.status}`;
        errorBox.hidden = false;
        return;
      }
      closeSelfPasswordModal();
      showToast('Passwort geaendert.');
    } catch (err) {
      errorBox.textContent = err.message;
      errorBox.hidden = false;
    }
  }

  // -------------------- Single-Mode Vault wechseln --------------------

  async function openSingleSwitchModal() {
    $('#ssw-vault-path').value = '';
    $('#ssw-pw').value = '';
    $('#ssw-new-path').value = '';
    $('#ssw-new-pw1').value = '';
    $('#ssw-new-pw2').value = '';
    $('#ssw-create-block').hidden = true;
    $('#ssw-error').hidden = true;
    $('#ssw-toggle-create').textContent = 'Stattdessen neuen Tresor anlegen…';
    $('#single-switch-modal').hidden = false;
    // "Datei suchen..."-Button nur im Single-User-Mode anzeigen (im Multi-
    // User-Mode laeuft der Server eh remote, der native Picker waere dort
    // nicht sichtbar).
    const browseBtn = $('#ssw-browse-btn');
    if (browseBtn) browseBtn.hidden = state.serverMode === 'user-db';
    // Bekannte Tresore als Klick-Chips unter dem Pfad-Eingabefeld.
    // Der User kann aber auch jeden beliebigen Pfad eintippen (USB-Stick,
    // externes Laufwerk, eigene Ordner) - die Auswahl ist nur Komfort.
    try {
      const response = await apiGet('/api/vaults');
      if (!response.ok) throw new Error('Vault-Liste nicht erreichbar.');
      const data = await response.json();
      const box = $('#ssw-known-vaults');
      box.innerHTML = '';
      const currentName = state.sessionInfo?.vault_filename;
      const known = (data.vaults || []).filter((v) => v.filename !== currentName);
      if (known.length > 0) {
        const label = document.createElement('div');
        label.className = 'ssw-known-vaults-label';
        label.textContent = 'Bekannte Tresore (Klick fuellt den Pfad):';
        box.appendChild(label);
        for (const v of known) {
          const chip = document.createElement('button');
          chip.type = 'button';
          chip.className = 'ssw-vault-chip';
          chip.textContent = v.filename;
          chip.title = v.path;
          chip.addEventListener('click', () => {
            $('#ssw-vault-path').value = v.path;
            $('#ssw-pw').focus();
          });
          box.appendChild(chip);
        }
        box.hidden = false;
      } else {
        box.hidden = true;
      }
      $('#ssw-new-path').value = data.suggested_new_path || '';
    } catch (err) {
      showSswError(err.message);
    }
    setTimeout(() => $('#ssw-vault-path').focus(), 0);
  }

  function closeSingleSwitchModal() {
    $('#single-switch-modal').hidden = true;
  }

  async function pickVaultFileNative() {
    // Triggert serverseitig den nativen Windows-File-Dialog. Funktioniert
    // nur im Single-User-Local-Setup (Server und Browser auf gleicher
    // Maschine). Server liefert 403/501 zurueck wenn nicht moeglich -
    // dann zeigen wir einen Hinweis statt zu raten.
    const btn = $('#ssw-browse-btn');
    if (btn) btn.disabled = true;
    try {
      const response = await apiGet('/api/files/pick-file');
      if (response.status === 403) {
        showSswError('Datei-Picker steht nur im Single-User-Mode zur Verfuegung.');
        return;
      }
      if (response.status === 501) {
        showSswError('Datei-Picker steht derzeit nur unter Windows zur Verfuegung. Bitte Pfad eintragen.');
        return;
      }
      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        showSswError(data.detail || 'Datei-Picker fehlgeschlagen.');
        return;
      }
      const data = await response.json();
      if (data.cancelled || !data.path) return;
      $('#ssw-vault-path').value = data.path;
      $('#ssw-pw').focus();
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  function toggleSswCreate() {
    const block = $('#ssw-create-block');
    block.hidden = !block.hidden;
    $('#ssw-toggle-create').textContent = block.hidden
      ? 'Stattdessen neuen Tresor anlegen…'
      : '← Zurueck zur Tresor-Auswahl';
  }

  async function submitSingleSwitch() {
    const errorBox = $('#ssw-error');
    errorBox.hidden = true;
    const createMode = !$('#ssw-create-block').hidden;
    if (createMode) {
      const newPath = $('#ssw-new-path').value.trim();
      const pw1 = $('#ssw-new-pw1').value;
      const pw2 = $('#ssw-new-pw2').value;
      if (!newPath) return showSswError('Pfad zum neuen Tresor fehlt.');
      if (pw1.length < 12) return showSswError('Passwort muss mindestens 12 Zeichen haben.');
      if (pw1 !== pw2) return showSswError('Die beiden Passwoerter stimmen nicht ueberein.');
      await doSingleSwitchCreate(newPath, pw1);
    } else {
      const path = $('#ssw-vault-path').value.trim();
      const pw = $('#ssw-pw').value;
      if (!path) return showSswError('Bitte Pfad zum Tresor eintragen oder einen neuen anlegen.');
      if (!pw) return showSswError('Master-Passwort fehlt.');
      await doSingleSwitchUnlock(path, pw);
    }
  }

  async function doSingleSwitchUnlock(path, pw) {
    const errorBox = $('#ssw-error');
    try {
      // Erst alten Tresor sperren
      await apiPost('/api/auth/lock').catch(() => {});
      clearToken();
      // Neuen Tresor entsperren
      const response = await apiPost('/api/auth/unlock', {
        vault_path: path,
        password: pw,
      });
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        // Token war schon weg — kein handleSessionLost noetig
        showSswError(body.detail || `Fehler ${response.status}`);
        return;
      }
      const data = await response.json();
      setToken(data.token);
      closeSingleSwitchModal();
      stopHeartbeat();
      stopSessionTicker();
      stopRetryPolling();
      await enterMain(data);
      showToast(`Tresor gewechselt: ${data.vault_filename}`);
    } catch (err) {
      showSswError(err.message);
    }
  }

  async function doSingleSwitchCreate(path, pw) {
    const errorBox = $('#ssw-error');
    try {
      await apiPost('/api/auth/lock').catch(() => {});
      clearToken();
      const response = await apiPost('/api/vaults', { path, password: pw });
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        showSswError(body.detail || `Fehler ${response.status}`);
        return;
      }
      const data = await response.json();
      setToken(data.token);
      closeSingleSwitchModal();
      stopHeartbeat();
      stopSessionTicker();
      stopRetryPolling();
      // Vault-Anlegen-Response hat kein vault_filename — selber bauen
      await enterMain({
        vault_filename: data.filename,
        vault_path: data.path,
        token: data.token,
        inactivity_timeout_s: data.inactivity_timeout_s,
        seconds_until_expiry: data.seconds_until_expiry,
      });
      showToast(`Neuer Tresor angelegt: ${data.filename}`);
    } catch (err) {
      showSswError(err.message);
    }
  }

  function showSswError(msg) {
    const box = $('#ssw-error');
    box.textContent = msg;
    box.hidden = false;
  }

  // -------------------- Vault-Export (Backup + Template) --------------------

  function openExportModal() {
    $('#export-template-pw').value = '';
    $('#export-error').hidden = true;
    $('#export-modal').hidden = false;
  }

  function closeExportModal() {
    $('#export-modal').hidden = true;
  }

  async function doExportBackup() {
    const btn = $('#export-backup-btn');
    btn.disabled = true;
    const originalLabel = btn.textContent;
    btn.textContent = 'Lade…';
    try {
      const token = getToken();
      const response = await fetch('/api/vaults/export/backup', {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
      });
      if (response.status === 401) { handleSessionLost(); return; }
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        showExportError(body.detail || `Fehler ${response.status}`);
        return;
      }
      const blob = await response.blob();
      const cd = response.headers.get('Content-Disposition') || '';
      const m = cd.match(/filename="?([^";]+)"?/i);
      const filename = (m && m[1]) || (state.sessionInfo?.vault_filename) || 'backup.opnvault';
      triggerDownload(blob, filename);
      showToast(`Backup heruntergeladen: ${filename}`);
    } catch (err) {
      showExportError(err.message);
    } finally {
      btn.disabled = false;
      btn.textContent = originalLabel;
    }
  }

  async function doExportTemplate() {
    const pw = $('#export-template-pw').value;
    if (pw.length < 12) {
      return showExportError('Template-Passwort muss mindestens 12 Zeichen haben.');
    }
    const btn = $('#export-template-btn');
    btn.disabled = true;
    const originalLabel = btn.textContent;
    btn.textContent = 'Erzeuge…';
    try {
      const token = getToken();
      const response = await fetch('/api/vaults/export/template', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
        },
        body: JSON.stringify({ template_password: pw }),
      });
      if (response.status === 401) { handleSessionLost(); return; }
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        showExportError(body.detail || `Fehler ${response.status}`);
        return;
      }
      const blob = await response.blob();
      const cd = response.headers.get('Content-Disposition') || '';
      const m = cd.match(/filename="?([^";]+)"?/i);
      const filename = (m && m[1]) || 'template.opnvault';
      triggerDownload(blob, filename);
      showToast(`Template heruntergeladen: ${filename}`);
      closeExportModal();
    } catch (err) {
      showExportError(err.message);
    } finally {
      btn.disabled = false;
      btn.textContent = originalLabel;
    }
  }

  function showExportError(msg) {
    const box = $('#export-error');
    box.textContent = msg;
    box.hidden = false;
  }

  function triggerDownload(blob, filename) {
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 0);
  }

  // -------------------- Update-Banner --------------------

  const UPDATE_DISMISS_KEY = 'opnc.update.dismissedVersion';

  async function checkForUpdate() {
    const banner = $('#update-banner');
    if (!banner) return;
    try {
      const response = await fetch('/api/updates/check', {
        headers: { Accept: 'application/json' },
      });
      if (!response.ok) return;
      const data = await response.json();
      if (data.status !== 'available' || !data.latest_version) {
        banner.hidden = true;
        return;
      }
      // Wenn der User diese Version schon weggeklickt hat, nicht erneut zeigen.
      let dismissed = null;
      try { dismissed = sessionStorage.getItem(UPDATE_DISMISS_KEY); } catch (_e) {}
      if (dismissed === data.latest_version) {
        banner.hidden = true;
        return;
      }
      $('#update-banner-version').textContent = data.latest_version;
      const link = $('#update-banner-link');
      if (data.html_url) {
        link.href = data.html_url;
        link.hidden = false;
      } else {
        link.hidden = true;
      }
      banner.hidden = false;
      banner.dataset.version = data.latest_version;
    } catch (_err) {
      /* still no banner — silent */
    }
  }

  function dismissUpdateBanner() {
    const banner = $('#update-banner');
    if (!banner) return;
    const version = banner.dataset.version;
    if (version) {
      try { sessionStorage.setItem(UPDATE_DISMISS_KEY, version); } catch (_e) {}
    }
    banner.hidden = true;
  }

  // -------------------- About-Modal --------------------

  let aboutLoaded = false;

  async function openAboutModal() {
    const modal = $('#about-modal');
    modal.hidden = false;
    // About-Stammdaten (Version, Author, etc.) sind statisch und brauchen
    // nicht jedes Mal nachgeladen werden.
    if (!aboutLoaded) {
      try {
        const response = await fetch('/api/about');
        if (response.ok) {
          const data = await response.json();
          $('#about-name').textContent = data.name || 'OPN-Cockpit';
          // 'version' ist die effektive Release-Version (Git-Tag wenn
          // verfuegbar, sonst __version__). Nur die zeigen wir an -
          // 'version_source' ist nur fuer API-Konsumenten / Debugging.
          $('#about-version').textContent = data.version || '—';
          $('#about-author').textContent = data.author || '—';
          const email = $('#about-email');
          if (data.author_email) {
            email.textContent = data.author_email;
            email.href = `mailto:${data.author_email}`;
          }
          const repo = $('#about-github');
          if (data.github_url) {
            repo.textContent = data.github_url;
            repo.href = data.github_url;
          }
          $('#about-license').textContent = data.license || '—';
          aboutLoaded = true;
        }
      } catch (_err) {
        /* still show what's in the markup; nothing fatal */
      }
    }
    // Update-Status JEDES Mal frisch holen (force=true, kein Cache).
    // Macht das About-Modal zur natuerlichen "ist was Neues da?"-Stelle.
    refreshAboutUpdateStatus();
  }

  async function refreshAboutUpdateStatus() {
    const block = $('#about-update-block');
    const value = $('#about-update-value');
    const link = $('#about-update-link');
    if (!block || !value) return;
    // Loading-Zustand setzen
    block.dataset.status = 'loading';
    value.innerHTML = '<span class="about-update-spinner" aria-hidden="true"></span>Pruefe Update-Quelle …';
    if (link) link.hidden = true;
    let data;
    try {
      const response = await fetch('/api/updates/check?force=true', {
        headers: { Accept: 'application/json' },
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      data = await response.json();
    } catch (_err) {
      block.dataset.status = 'error';
      value.textContent = 'Update-Quelle nicht erreichbar.';
      return;
    }
    const current = data.current_version || '—';
    const latest = data.latest_version || '';
    if (data.status === 'available' && latest) {
      block.dataset.status = 'available';
      value.innerHTML = `<strong>${escapeHtml(latest)}</strong> verfuegbar (du hast ${escapeHtml(current)}).`;
      if (link && data.html_url) {
        link.href = data.html_url;
        link.hidden = false;
      }
      // Banner oben auf der Seite zusaetzlich anstossen
      checkForUpdate();
    } else if (data.status === 'up-to-date') {
      block.dataset.status = 'up-to-date';
      value.textContent = `Du hast die neueste Version (${current}).`;
    } else if (data.status === 'disabled') {
      block.dataset.status = 'disabled';
      value.textContent = 'Update-Check ist deaktiviert (Settings).';
    } else {
      block.dataset.status = 'unknown';
      value.textContent = data.last_checked_iso
        ? `Konnte den Status nicht ermitteln (letzter erfolgreicher Check: ${data.last_checked_iso}).`
        : 'Konnte den Status nicht ermitteln.';
    }
  }

  function escapeHtml(s) {
    if (typeof s !== 'string') return '';
    return s.replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }

  function closeAboutModal() {
    $('#about-modal').hidden = true;
  }

  // -------------------- Audit-Modal --------------------

  let auditEventKindsLoaded = false;

  async function openAuditModal() {
    if (!auditEventKindsLoaded) await loadAuditEventKinds();
    $('#au-filter-event').value = '';
    $('#au-filter-action').value = '';
    $('#au-filter-device').value = '';
    $('#audit-modal').hidden = false;
    await reloadAudit();
  }

  function closeAuditModal() {
    $('#audit-modal').hidden = true;
  }

  async function loadAuditEventKinds() {
    try {
      const response = await apiGet('/api/audit/events');
      if (!response.ok) return;
      const events = await response.json();
      const select = $('#au-filter-event');
      // Default option behalten
      for (const ev of events) {
        const opt = document.createElement('option');
        opt.value = ev;
        opt.textContent = ev;
        select.appendChild(opt);
      }
      auditEventKindsLoaded = true;
    } catch (_) {}
  }

  async function reloadAudit() {
    const params = new URLSearchParams();
    const event = $('#au-filter-event').value;
    const action = $('#au-filter-action').value.trim();
    const device = $('#au-filter-device').value.trim();
    if (event) params.set('event', event);
    if (action) params.set('action', action);
    if (device) params.set('target_device_id', device);
    params.set('limit', '200');

    const list = $('#au-list');
    list.innerHTML = '<div class="audit-empty">Lade…</div>';
    try {
      const response = await apiGet(`/api/audit?${params.toString()}`);
      if (response.status === 401) { handleSessionLost(); return; }
      if (!response.ok) {
        list.innerHTML = `<div class="audit-empty">Fehler ${response.status}</div>`;
        return;
      }
      const data = await response.json();
      renderAudit(data);
    } catch (err) {
      list.innerHTML = `<div class="audit-empty">${err.message}</div>`;
    }
  }

  function renderAudit(data) {
    const summary = $('#au-summary');
    summary.innerHTML = `
      <span><strong>${data.entries.length}</strong> Einträge angezeigt</span>
      <span>${data.total} gesamt im Log</span>
      ${data.truncated ? '<span>(neueste zuerst, älteste abgeschnitten)</span>' : ''}
    `;
    const list = $('#au-list');
    list.innerHTML = '';
    if (!data.entries.length) {
      list.innerHTML = '<div class="audit-empty">Keine Einträge passen zum Filter.</div>';
      return;
    }
    for (const entry of data.entries) {
      const row = document.createElement('div');
      row.className = 'audit-row';

      const time = document.createElement('span');
      time.className = 'audit-row-time';
      time.textContent = formatAuditTime(entry.timestamp_utc);
      const event = document.createElement('span');
      event.className = 'audit-row-event';
      event.textContent = entry.event;
      const summaryEl = document.createElement('span');
      summaryEl.className = 'audit-row-summary';
      summaryEl.textContent = entry.summary;
      const status = document.createElement('span');
      if (entry.status) {
        status.className = `audit-row-status ${entry.status.toLowerCase().replace('ü','ue')}`;
        status.textContent = entry.status;
      } else {
        status.textContent = '';
      }

      row.appendChild(time);
      row.appendChild(event);
      row.appendChild(summaryEl);
      row.appendChild(status);
      list.appendChild(row);
    }
  }

  async function verifyAuditChain() {
    const box = $('#au-integrity');
    box.hidden = false;
    box.className = 'audit-integrity na';
    box.textContent = 'Pruefe Hash-Chain…';
    try {
      const response = await apiGet('/api/audit/verify');
      if (response.status === 401) { handleSessionLost(); return; }
      if (!response.ok) {
        box.className = 'audit-integrity broken';
        box.textContent = `Verifikation fehlgeschlagen (${response.status}).`;
        return;
      }
      const data = await response.json();
      if (data.status === 'not-available') {
        box.className = 'audit-integrity na';
        box.textContent = `Hash-Chain im aktuellen Backend nicht verfuegbar (nur SQLite). ${data.total} Eintraege ungesichert.`;
        return;
      }
      if (data.status === 'ok') {
        box.className = 'audit-integrity ok';
        box.innerHTML = `✓ Hash-Chain intakt. <strong>${data.total}</strong> Eintraege ueberprueft, keine Manipulation erkannt.`;
        return;
      }
      box.className = 'audit-integrity broken';
      const idx = (data.broken || []).join(', ');
      box.innerHTML = `⚠ Manipulation erkannt! ${data.total} Eintraege geprueft, defekt: <strong>${idx}</strong>`;
    } catch (err) {
      box.className = 'audit-integrity broken';
      box.textContent = err.message;
    }
  }

  async function exportAuditCsv() {
    const params = new URLSearchParams();
    const event = $('#au-filter-event').value;
    const action = $('#au-filter-action').value.trim();
    const device = $('#au-filter-device').value.trim();
    if (event) params.set('event', event);
    if (action) params.set('action', action);
    if (device) params.set('target_device_id', device);
    const url = `/api/audit/export.csv?${params.toString()}`;
    try {
      const token = getToken();
      const response = await fetch(url, {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
      });
      if (response.status === 401) { handleSessionLost(); return; }
      if (!response.ok) {
        showToast(`Audit-Export fehlgeschlagen (${response.status}).`, true);
        return;
      }
      const blob = await response.blob();
      triggerDownload(blob, 'opn-cockpit-audit.csv');
      showToast('Audit-CSV heruntergeladen.');
    } catch (err) {
      showToast(err.message, true);
    }
  }

  function formatAuditTime(iso) {
    // 2026-05-29T12:34:56.789Z -> "29.05. 12:34:56"
    const m = iso.match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})/);
    if (!m) return iso;
    return `${m[3]}.${m[2]}. ${m[4]}:${m[5]}:${m[6]}`;
  }

  // -------------------- Toast --------------------

  let toastEl = null;
  let toastTimer = null;

  function showToast(msg, isError) {
    if (!toastEl) {
      toastEl = document.createElement('div');
      toastEl.className = 'toast';
      document.body.appendChild(toastEl);
    }
    toastEl.textContent = msg;
    toastEl.classList.toggle('toast-error', !!isError);
    toastEl.hidden = false;
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { toastEl.hidden = true; }, 3200);
  }

  // -------------------- Bootstrap + Events --------------------

  async function bootstrap() {
    initTheme();
    bindStaticEvents();
    setupInlineValidators();

    const status = $('#boot-status');

    try {
      const v = await fetch('/api/version').then((r) => r.json());
      status.textContent = `Backend bereit · v${v.version}`;
    } catch (_) {
      status.textContent = 'Backend nicht erreichbar.';
      return;
    }

    // Mode-Detection: erst Auth-Backend ermitteln, dann passenden Screen.
    let bootData;
    try {
      bootData = await fetchBootstrapStatus();
    } catch (_) {
      status.textContent = 'Server-Status nicht abrufbar.';
      return;
    }

    // Vorschlag fuer Vault-Pfad einsetzen (falls Server einen kennt).
    if (bootData.suggested_vault_path) {
      const vaultInput = $('#su-vault-path');
      if (vaultInput && !vaultInput.value) vaultInput.value = bootData.suggested_vault_path;
    }

    // Vorhandener Token? -> direkt in main, sonst weiter unten.
    if (getToken()) {
      try {
        const response = await apiGet('/api/auth/me');
        if (response.ok) {
          const session = await response.json();
          await enterMain(session);
          return;
        }
        clearToken();
      } catch (_) { /* fallthrough auf Login */ }
    }

    // Multi-User-Pfad: Setup-Wizard oder Multi-User-Login.
    if (state.serverMode === 'user-db') {
      enterBootstrapPhase();
      return;
    }

    // Single-User-Pfad (Default, v2-Verhalten).
    showScreen('login');
    showLoginView('picker');
    try {
      await fetchVaultsAndPopulate();
    } catch (err) {
      const e = $('#login-error');
      e.textContent = err.message;
      e.hidden = false;
    }
  }

  function bindStaticEvents() {
    // Login
    $('#unlock-btn').addEventListener('click', doUnlock);
    $('#password-input').addEventListener('keydown', (e) => {
      if (e.key === 'Enter') doUnlock();
    });
    const loginBrowse = $('#login-browse-btn');
    if (loginBrowse) loginBrowse.addEventListener('click', pickLoginVaultFile);
    const loginPath = $('#login-vault-path');
    if (loginPath) loginPath.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') $('#password-input').focus();
    });
    $('#create-vault-btn').addEventListener('click', () => {
      showLoginView('create');
      $('#new-vault-pw1').focus();
    });
    $('#create-back-btn').addEventListener('click', () => showLoginView('picker'));
    $('#create-confirm-btn').addEventListener('click', doCreateVault);
    const newNameInput = $('#new-vault-name');
    const newDirInput = $('#new-vault-directory');
    if (newNameInput) newNameInput.addEventListener('input', updateVaultTargetPreview);
    if (newDirInput) newDirInput.addEventListener('input', updateVaultTargetPreview);
    $('#theme-toggle-login').addEventListener('click', toggleTheme);

    // Multi-User-Login
    const muLoginBtn = $('#mu-login-btn');
    if (muLoginBtn) {
      muLoginBtn.addEventListener('click', doMultiUserLogin);
      $('#mu-password').addEventListener('keydown', (e) => {
        if (e.key === 'Enter') doMultiUserLogin();
      });
      $('#mu-username').addEventListener('keydown', (e) => {
        if (e.key === 'Enter') $('#mu-password').focus();
      });
    }
    const themeMulti = $('#theme-toggle-multi');
    if (themeMulti) themeMulti.addEventListener('click', toggleTheme);

    // Setup-Wizard (seit F28 nur noch ein Step — setup-admin-View entfernt)
    const setupVaultBtn = $('#setup-vault-btn');
    if (setupVaultBtn) {
      setupVaultBtn.addEventListener('click', doSetupUnlockVault);
      $('#su-vault-pw').addEventListener('keydown', (e) => {
        if (e.key === 'Enter') doSetupUnlockVault();
      });
    }

    // Main: top bar
    $('#theme-toggle-main').addEventListener('click', toggleTheme);
    $('#lock-btn').addEventListener('click', doLock);
    $('#audit-open-btn').addEventListener('click', openAuditModal);
    $('#retry-indicator-btn').addEventListener('click', showRetryStatus);
    const exportBtn = $('#vault-export-btn');
    if (exportBtn) exportBtn.addEventListener('click', openExportModal);

    // Single-Mode Vault-Switch
    const sswBtn = $('#single-switch-btn');
    if (sswBtn) sswBtn.addEventListener('click', openSingleSwitchModal);
    const sswBrowse = $('#ssw-browse-btn');
    if (sswBrowse) sswBrowse.addEventListener('click', pickVaultFileNative);
    const sswClose = $('#ssw-close');
    if (sswClose) {
      sswClose.addEventListener('click', closeSingleSwitchModal);
      $('#ssw-cancel').addEventListener('click', closeSingleSwitchModal);
      $('#ssw-toggle-create').addEventListener('click', toggleSswCreate);
      $('#ssw-submit').addEventListener('click', submitSingleSwitch);
      $('#single-switch-modal').addEventListener('click', (e) => {
        if (e.target.id === 'single-switch-modal') closeSingleSwitchModal();
      });
    }

    // Export-Modal
    const expClose = $('#export-close');
    if (expClose) {
      expClose.addEventListener('click', closeExportModal);
      $('#export-cancel').addEventListener('click', closeExportModal);
      $('#export-backup-btn').addEventListener('click', doExportBackup);
      $('#export-template-btn').addEventListener('click', doExportTemplate);
      $('#export-modal').addEventListener('click', (e) => {
        if (e.target.id === 'export-modal') closeExportModal();
      });
    }

    // About-Modal
    const aboutBtn = $('#about-open-btn');
    if (aboutBtn) aboutBtn.addEventListener('click', openAboutModal);
    const aboutClose = $('#about-close');
    if (aboutClose) {
      aboutClose.addEventListener('click', closeAboutModal);
      $('#about-cancel').addEventListener('click', closeAboutModal);
      $('#about-modal').addEventListener('click', (e) => {
        if (e.target.id === 'about-modal') closeAboutModal();
      });
    }

    // Update-Banner-Dismiss
    const updateDismiss = $('#update-banner-dismiss');
    if (updateDismiss) updateDismiss.addEventListener('click', dismissUpdateBanner);

    // Folder-Picker (native primary, web fallback)
    const fbBtn = $('#new-vault-browse-btn');
    if (fbBtn) fbBtn.addEventListener('click', openFolderPicker);
    const fbClose = $('#fb-close');
    if (fbClose) {
      fbClose.addEventListener('click', closeFolderBrowser);
      $('#fb-cancel').addEventListener('click', closeFolderBrowser);
      $('#fb-up-btn').addEventListener('click', folderBrowserUp);
      $('#fb-accept').addEventListener('click', acceptFolderBrowser);
      $('#folder-browser-modal').addEventListener('click', (e) => {
        if (e.target.id === 'folder-browser-modal') closeFolderBrowser();
      });
    }
    const usersBtn = $('#users-open-btn');
    if (usersBtn) usersBtn.addEventListener('click', openUsersModal);
    const pwSelfBtn = $('#password-self-btn');
    if (pwSelfBtn) pwSelfBtn.addEventListener('click', openSelfPasswordModal);

    // User-Verwaltungs-Modal
    const umClose = $('#users-modal-close');
    if (umClose) {
      umClose.addEventListener('click', closeUsersModal);
      $('#users-modal-cancel').addEventListener('click', closeUsersModal);
      $('#users-add-btn').addEventListener('click', openAddUserForm);
      $('#users-add-cancel').addEventListener('click', closeAddUserForm);
      $('#users-add-submit').addEventListener('click', submitAddUser);
      $('#users-modal').addEventListener('click', (e) => {
        if (e.target.id === 'users-modal') closeUsersModal();
      });
      const switchBtn = $('#users-switch-vault-btn');
      if (switchBtn) {
        switchBtn.addEventListener('click', openVaultSwitchModal);
      }
    }

    // Vault-Switch-Modal
    const vswClose = $('#vault-switch-close');
    if (vswClose) {
      vswClose.addEventListener('click', closeVaultSwitchModal);
      $('#vault-switch-cancel').addEventListener('click', closeVaultSwitchModal);
      $('#vault-switch-submit').addEventListener('click', submitVaultSwitch);
      $('#vault-switch-modal').addEventListener('click', (e) => {
        if (e.target.id === 'vault-switch-modal') closeVaultSwitchModal();
      });
    }

    // Tresor-Einstellungen (F5)
    const vsBtn = $('#vault-settings-btn');
    if (vsBtn) {
      vsBtn.addEventListener('click', openVaultSettingsModal);
      $('#vault-settings-close').addEventListener('click', closeVaultSettingsModal);
      $('#vault-settings-cancel').addEventListener('click', closeVaultSettingsModal);
      // (Backup-History ist jetzt ein Tab im Device-Modal - keine eigenen
      // bh-close/bh-cancel-Bindings noetig.)
      $('#cd-close').addEventListener('click', closeCertDetailModal);
      $('#cd-cancel').addEventListener('click', closeCertDetailModal);
      $('#cert-detail-modal').addEventListener('click', (e) => {
        if (e.target.id === 'cert-detail-modal') closeCertDetailModal();
      });
      $('#vs-timeout-save').addEventListener('click', saveInactivityTimeout);
      $('#vs-backup-save').addEventListener('click', saveBackupSettings);
      $('#vs-pw-submit').addEventListener('click', changeVaultPassword);
      // Backdrop-Click bewusst nicht — Eingabe-Modal.
    }

    // Self-Service-Passwort-Modal
    const pwClose = $('#pwself-close');
    if (pwClose) {
      pwClose.addEventListener('click', closeSelfPasswordModal);
      $('#pwself-cancel').addEventListener('click', closeSelfPasswordModal);
      $('#pwself-submit').addEventListener('click', submitSelfPassword);
      $('#pwself-new2').addEventListener('keydown', (e) => {
        if (e.key === 'Enter') submitSelfPassword();
      });
      // Backdrop-Click bewusst nicht — Eingabe-Modal, kein Datenverlust durch Klick.
    }

    // Audit-Modal
    $('#audit-modal-close').addEventListener('click', closeAuditModal);
    $('#audit-modal-cancel').addEventListener('click', closeAuditModal);
    $('#au-reload').addEventListener('click', reloadAudit);
    const verifyBtn = $('#au-verify-btn');
    if (verifyBtn) verifyBtn.addEventListener('click', verifyAuditChain);
    const auExportBtn = $('#au-export-btn');
    if (auExportBtn) auExportBtn.addEventListener('click', exportAuditCsv);
    $('#audit-modal').addEventListener('click', (e) => {
      if (e.target.id === 'audit-modal') closeAuditModal();
    });

    // Main: search
    $('#search-input').addEventListener('input', (e) => {
      state.search = e.target.value.trim().toLowerCase();
      renderGrid();
    });

    // Main: Selektions-Toolbar
    $('#sel-all').addEventListener('click', selectAllDevices);
    $('#sel-none').addEventListener('click', selectNoDevices);
    $('#sel-reachable').addEventListener('click', selectReachableDevices);
    const selCmp = $('#sel-compare');
    if (selCmp) selCmp.addEventListener('click', () => openCompareModal('aliases'));
    const cmpClose = $('#cmp-close');
    if (cmpClose) cmpClose.addEventListener('click', closeCompareModal);
    const cmpCancel = $('#cmp-cancel');
    if (cmpCancel) cmpCancel.addEventListener('click', closeCompareModal);
    const cmpModal = $('#compare-modal');
    if (cmpModal) cmpModal.addEventListener('click', (e) => {
      if (e.target.id === 'compare-modal') closeCompareModal();
    });
    document.querySelectorAll('.compare-tab').forEach((btn) => {
      btn.addEventListener('click', () => {
        const subsystem = btn.dataset.subsystem;
        if (!subsystem || subsystem === currentCompareSubsystem) return;
        openCompareModal(subsystem).catch(() => {});
      });
    });

    // Sidebar actions
    $('#add-device-btn').addEventListener('click', openAddModal);
    $('#empty-add-btn').addEventListener('click', openAddModal);
    $('#add-route-btn').addEventListener('click', () => openPlanModal('route'));
    $('#add-alias-btn').addEventListener('click', () => openPlanModal('alias'));
    $('#bulk-import-btn').addEventListener('click', openBulkModal);

    // Bulk-Modal (Firewall-Import)
    $('#bulk-modal-close').addEventListener('click', closeBulkModal);
    $('#bulk-modal-cancel').addEventListener('click', closeBulkModal);
    $('#bulk-submit-btn').addEventListener('click', submitBulkImport);
    $('#bk-fmt-csv').addEventListener('change', updateBulkFormatHint);
    $('#bk-fmt-json').addEventListener('change', updateBulkFormatHint);
    $('#bk-fmt-vault').addEventListener('change', updateBulkFormatHint);
    // Bulk-Modal: Backdrop-Click bewusst nicht — Eingabe-Modal.

    // Plan-Modal
    $('#plan-modal-close').addEventListener('click', cancelPlanModal);
    $('#plan-modal-cancel').addEventListener('click', cancelPlanModal);
    $('#plan-discard-btn').addEventListener('click', planDiscard);
    $('#plan-back-btn').addEventListener('click', planBack);
    $('#plan-next-btn').addEventListener('click', planNextOrApply);
    $('#pl-load-gateways').addEventListener('click', loadGatewaySuggestions);
    $('#pl-load-aliases').addEventListener('click', loadAliasSuggestions);
    // F17: Datalist-Auswahl feuert ein input-Event mit dem fertigen Wert.
    $('#pl-alias-name').addEventListener('input', syncAliasTypeFromSuggestion);
    $('#pl-alias-name').addEventListener('change', syncAliasTypeFromSuggestion);
    // F19 v2: Re-Browse via focus/mousedown-Clear bei bekannter Auswahl.
    enableDatalistRebrowse('pl-alias-name', () => new Set(aliasSuggestionTypes.keys()));
    enableDatalistRebrowse('pl-route-gateway', () => gatewaySuggestionNames);
    $('#pl-profile-select').addEventListener('change', (e) => applyProfile(e.target.value));
    $('#pl-profile-delete').addEventListener('click', deleteCurrentProfile);
    $('#pl-save-profile-btn') || null;  // Button id ist plan-save-profile-btn
    $('#plan-save-profile-btn').addEventListener('click', saveCurrentAsProfile);
    $('#pl-confirm').addEventListener('change', (e) => {
      $('#plan-next-btn').disabled = !e.target.checked;
    });
    // Backdrop-Click schliesst Eingabe-Phasen NICHT (User-Frust durch
    // verlorene Eingaben). In der Result-Phase ist es ein reines Read-Only-
    // Modal, da darf ein Klick daneben schliessen.
    $('#plan-modal').addEventListener('click', (e) => {
      if (e.target.id !== 'plan-modal') return;
      if (planPhase === 'result') closePlanModal();
    });

    // Add-Modal
    $('#add-modal-close').addEventListener('click', closeAddModal);
    $('#add-modal-cancel').addEventListener('click', closeAddModal);
    $('#add-modal-confirm').addEventListener('click', doAddOrEditDevice);
    // Backdrop-Click bewusst nicht — Eingabe-Modal, X / Abbrechen reichen.

    // Device-Modal
    $('#device-modal-close').addEventListener('click', closeDeviceModal);
    $('#device-modal-cancel').addEventListener('click', closeDeviceModal);
    $('#device-modal-delete').addEventListener('click', doDeleteDevice);
    $('#device-test-btn').addEventListener('click', doTestConnection);
    // Edge hat target=_blank-Anchors bei bestimmten URLs leer gerendert
    // (vermutlich Edge-Bug mit rel/target + nicht-aufloesbaren Hosts).
    // Wir fangen den Klick ab und navigieren per window.open — Anchor-
    // href bleibt als rechtsklick/middle-click Fallback erhalten.
    $('#device-open-web-btn').addEventListener('click', (e) => {
      e.preventDefault();
      const a = e.currentTarget;
      const url = a.getAttribute('href');
      if (url && url !== '#') openWebUrl(url);
    });
    $('#device-edit-btn').addEventListener('click', doEditFromDetail);
    $('#device-duplicate-btn').addEventListener('click', doDuplicate);
    $('#device-backup-btn').addEventListener('click', doBackupDownload);
    const backupNowBtn = $('#device-backup-now-btn');
    if (backupNowBtn) backupNowBtn.addEventListener('click', async () => {
      await doBackupCreateServer();
      bhLoadedForDeviceId = null;
      await loadBackupsTab(true);
    });
    $('#device-update-check-btn').addEventListener('click', doFirmwareCheck);
    // Aliase-Tab im Device-Modal: Filter-Input
    const almFilter = $('#alm-filter');
    if (almFilter) almFilter.addEventListener('input', renderAliasManagerList);
    const rtmFilter = $('#rtm-filter');
    if (rtmFilter) rtmFilter.addEventListener('input', renderRoutesList);
    const frmFilter = $('#frm-filter');
    if (frmFilter) frmFilter.addEventListener('input', renderRulesList);
    const frmAdd = $('#frm-add-btn');
    if (frmAdd) frmAdd.addEventListener('click', openRuleAddModal);
    // Unbound-Tab + Modal
    const unbFilter = $('#unb-filter');
    if (unbFilter) unbFilter.addEventListener('input', renderUnboundList);
    const unbAdd = $('#unb-add-btn');
    if (unbAdd) unbAdd.addEventListener('click', openUnboundAddModal);
    const unbCancel = $('#unbound-modal-cancel');
    if (unbCancel) unbCancel.addEventListener('click', closeUnboundModal);
    const unbClose = $('#unbound-modal-close');
    if (unbClose) unbClose.addEventListener('click', closeUnboundModal);
    const unbConfirm = $('#unbound-modal-confirm');
    if (unbConfirm) unbConfirm.addEventListener('click', () => {
      submitUnboundModal().catch((err) => showUnboundModalError(err.message));
    });
    // Rule-Modal-Buttons
    const ruleCancel = $('#rule-modal-cancel');
    if (ruleCancel) ruleCancel.addEventListener('click', closeRuleModal);
    const ruleClose = $('#rule-modal-close');
    if (ruleClose) ruleClose.addEventListener('click', closeRuleModal);
    const ruleConfirm = $('#rule-modal-confirm');
    if (ruleConfirm) ruleConfirm.addEventListener('click', () => {
      submitRuleModal().catch((err) => showRuleModalError(err.message));
    });
    // Device-Modal Tab-Switching
    document.querySelectorAll('#device-modal-tabs .modal-tab').forEach((btn) => {
      btn.addEventListener('click', () => switchDeviceTab(btn.dataset.tab));
    });
    // Updates-Tab: "Erneut pruefen"-Button triggert vorhandenen Check
    const updRecheck = $('#device-updates-recheck-btn');
    if (updRecheck) updRecheck.addEventListener('click', async () => {
      if (!currentDeviceId) return;
      await doFirmwareCheckForDevice(currentDeviceId);
      renderUpdatesTab();
    });
    $('#device-url-copy').addEventListener('click', doCopyUrl);
    $('#device-modal').addEventListener('click', (e) => {
      if (e.target.id === 'device-modal') closeDeviceModal();
    });

    // Global hotkeys: Strg+K -> Suche, Esc -> Modal schliessen
    document.addEventListener('keydown', (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
        if (document.getElementById('app').dataset.state !== 'main') return;
        const search = document.getElementById('search-input');
        if (search) { e.preventDefault(); search.focus(); search.select(); }
      }
      if (e.key === 'Escape') {
        if (!$('#add-modal').hidden) closeAddModal();
        else if (!$('#device-modal').hidden) closeDeviceModal();
      }
    });
  }

  document.addEventListener('DOMContentLoaded', bootstrap);

  // Diagnose-Helfer fuer den Click-Bug (Test-Runde 7+):
  // window.__opnDiag() in der Browser-Console aufrufen — listet alle
  // sichtbaren Vollflaechen-Overlays (position fixed/absolute mit inset 0
  // oder Top-Left + 100% Groesse) auf, plus jedes Element ueber den
  // Topbar-Icons. So sieht man sofort welches DOM-Element die Klicks
  // abfaengt ohne F12-Layer-Panel manuell durchgehen zu muessen.
  window.__opnDiag = function () {
    const all = document.querySelectorAll('*');
    const overlays = [];
    for (const el of all) {
      const s = getComputedStyle(el);
      if ((s.position === 'fixed' || s.position === 'absolute') &&
          s.pointerEvents !== 'none' && s.visibility !== 'hidden' &&
          s.display !== 'none') {
        const r = el.getBoundingClientRect();
        const fullWidth = r.width >= window.innerWidth * 0.8;
        const fullHeight = r.height >= window.innerHeight * 0.8;
        if (fullWidth && fullHeight) {
          overlays.push({
            tag: el.tagName,
            id: el.id || '(no id)',
            cls: el.className || '(no class)',
            zIndex: s.zIndex,
            rect: `${Math.round(r.width)}x${Math.round(r.height)} @ ${Math.round(r.left)},${Math.round(r.top)}`,
          });
        }
      }
    }
    console.group('OPN-Cockpit Click-Diagnose: Vollflaechen-Overlays');
    if (overlays.length === 0) {
      console.log('Keine Vollflaechen-Overlays gefunden.');
    } else {
      console.table(overlays);
    }
    console.groupEnd();
    // Welches Element liegt unter dem ersten icon-btn der Topbar?
    const topbarBtn = document.querySelector('.topbar .icon-btn:not([hidden])');
    if (topbarBtn) {
      const r = topbarBtn.getBoundingClientRect();
      const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
      const elAtPoint = document.elementFromPoint(cx, cy);
      console.group('Element unter Topbar-Icon (' + Math.round(cx) + ',' + Math.round(cy) + ')');
      console.log('Erwartet:', topbarBtn);
      console.log('Tatsaechlich:', elAtPoint);
      console.log('Gleich?', elAtPoint === topbarBtn || topbarBtn.contains(elAtPoint));
      console.groupEnd();
    }
    return overlays;
  };
})();
