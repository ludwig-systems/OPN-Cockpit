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

  const STATE_KEY = 'opn-cockpit-token';
  const THEME_KEY = 'opn-cockpit-theme';
  const HEARTBEAT_INTERVAL_MS = 30000;
  const SESSION_TICK_MS = 15000;
  const HEARTBEAT_STALE_AFTER_MS = 90000;

  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => Array.from(document.querySelectorAll(sel));

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
    return data;
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

  // -------------------- Setup-Wizard (Multi-User-First-Run) --------------------

  async function doSetupAdmin() {
    const token = $('#su-token').value.trim();
    const username = $('#su-username').value.trim();
    const pw1 = $('#su-pw1').value;
    const pw2 = $('#su-pw2').value;
    const errorBox = $('#setup-admin-error');
    errorBox.hidden = true;
    if (!token) return showSetupError(errorBox, 'Bootstrap-Token fehlt (siehe Server-Log).');
    if (!username) return showSetupError(errorBox, 'Benutzername fehlt.');
    if (pw1.length < 12) return showSetupError(errorBox, 'Passwort muss mindestens 12 Zeichen haben.');
    if (pw1 !== pw2) return showSetupError(errorBox, 'Die beiden Passwoerter stimmen nicht ueberein.');
    const btn = $('#setup-admin-btn');
    btn.disabled = true;
    btn.textContent = 'Lege an…';
    try {
      const response = await fetch('/api/bootstrap/admin', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Accept: 'application/json',
          'X-Bootstrap-Token': token,
        },
        body: JSON.stringify({ username, password: pw1 }),
      });
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        throw new Error(body.detail || `Fehler ${response.status}`);
      }
      // Admin angelegt — naechster Schritt: Vault entsperren.
      await fetchBootstrapStatus();
      enterBootstrapPhase();
    } catch (err) {
      showSetupError(errorBox, err.message);
    } finally {
      btn.disabled = false;
      btn.textContent = 'Admin anlegen';
    }
  }

  async function doSetupUnlockVault() {
    const token = $('#su-vault-token').value.trim();
    const path = $('#su-vault-path').value.trim();
    const password = $('#su-vault-pw').value;
    const createIfMissing = $('#su-vault-create').checked;
    const errorBox = $('#setup-vault-error');
    errorBox.hidden = true;
    if (!token) return showSetupError(errorBox, 'Bootstrap-Token fehlt (siehe Server-Log).');
    if (!path) return showSetupError(errorBox, 'Pfad zur Tresor-Datei fehlt.');
    if (password.length < 12) return showSetupError(errorBox, 'Master-Passwort muss mindestens 12 Zeichen haben.');
    const btn = $('#setup-vault-btn');
    btn.disabled = true;
    btn.textContent = 'Entsperre…';
    try {
      const response = await fetch('/api/bootstrap/vault', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Accept: 'application/json',
          'X-Bootstrap-Token': token,
        },
        body: JSON.stringify({
          vault_path: path,
          password,
          create_if_missing: createIfMissing,
        }),
      });
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        throw new Error(body.detail || `Fehler ${response.status}`);
      }
      // Server ist ready — auf Multi-User-Login schwenken.
      await fetchBootstrapStatus();
      enterBootstrapPhase();
    } catch (err) {
      showSetupError(errorBox, err.message);
    } finally {
      btn.disabled = false;
      btn.textContent = 'Tresor entsperren';
    }
  }

  function showSetupError(box, msg) {
    box.textContent = msg;
    box.hidden = false;
  }

  function enterBootstrapPhase() {
    // Nach jedem Status-Wechsel: passenden Screen zeigen.
    const s = state.bootstrapStatus;
    if (s === 'needs-admin') {
      showScreen('setup');
      showLoginView('setup-admin');
      setTimeout(() => $('#su-username').focus(), 0);
    } else if (s === 'needs-vault-unlock') {
      showScreen('setup');
      showLoginView('setup-vault');
      setTimeout(() => $('#su-vault-pw').focus(), 0);
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
    const select = $('#vault-select');
    select.innerHTML = '';

    if (!data.vaults || data.vaults.length === 0) {
      $('#login-hint').textContent =
        'Es wurde noch kein Tresor gefunden. Klicke „Neuen Tresor anlegen…" um zu starten.';
      $('#unlock-btn').disabled = true;
      $('#password-input').disabled = true;
      select.disabled = true;
      select.innerHTML = '<option>(kein Tresor vorhanden)</option>';
    } else {
      const n = data.vaults.length;
      $('#login-hint').textContent =
        n === 1
          ? 'Ein Tresor gefunden — bitte Passwort eingeben.'
          : `${n} Tresore gefunden — bitte einen auswählen.`;
      data.vaults.forEach((v) => {
        const opt = document.createElement('option');
        opt.value = v.path;
        opt.textContent = `${v.filename}    —    ${v.path}`;
        if (v.is_default) opt.selected = true;
        select.appendChild(opt);
      });
      $('#unlock-btn').disabled = false;
      $('#password-input').disabled = false;
      select.disabled = false;
      $('#password-input').focus();
    }

    const nameInput = $('#new-vault-name');
    const dirInput = $('#new-vault-directory');
    if (nameInput && !nameInput.value) nameInput.value = data.suggested_new_name || 'main';
    if (dirInput && !dirInput.value) dirInput.value = data.suggested_new_directory || '';
    renderPathSuggestions(data.path_suggestions || []);
    updateVaultTargetPreview();
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
    const path = $('#vault-select').value;
    const password = $('#password-input').value;
    if (!path || !password) return;
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
    const openLink = document.createElement('a');
    openLink.className = 'card-open-link';
    openLink.href = `https://${device.host}:${device.port}/`;
    openLink.target = '_blank';
    openLink.title = 'OPNsense-Weboberfläche öffnen';
    openLink.innerHTML = `<svg width="12" height="12" viewBox="0 0 13 13" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
      <path d="M6.5 2H3a1 1 0 00-1 1v7a1 1 0 001 1h7a1 1 0 001-1V6.5"/>
      <path d="M8 1.5h3.5V5"/>
      <line x1="6" y1="7" x2="11.5" y2="1.5"/>
    </svg>`;
    openLink.addEventListener('click', (e) => {
      e.stopPropagation();  // Karte soll nicht das Detail-Modal oeffnen
      e.preventDefault();   // native Anchor-Navigation abloesen
      openWebUrl(`https://${device.host}:${device.port}/`);
    });
    row.appendChild(openLink);
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

    // Stats
    const stats = document.createElement('div');
    stats.className = 'card-stats';
    stats.innerHTML = `
      <span class="stat"><span class="stat-label">Port</span><span class="stat-value">${device.port}</span></span>
      <span class="stat"><span class="stat-value ${device.tls_verify ? 'tls-on' : 'tls-off'}">${device.tls_verify ? 'TLS ✓' : 'TLS AUS'}</span></span>
      <span class="stat"><span class="stat-value">${formatHeartbeatLabel(hb, reachability)}</span></span>
    `;
    article.appendChild(stats);

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

  function handleSessionLost() {
    stopHeartbeat();
    stopSessionTicker();
    stopRetryPolling();
    clearToken();
    state.devices = [];
    state.heartbeat = {};
    state.sessionInfo = null;
    showScreen('login');
    showLoginView('picker');
    fetchVaultsAndPopulate().catch(() => {});
  }

  async function doLock() {
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

  function openEditModal(device) {
    modalMode = 'edit';
    editingDeviceId = device.id;
    $('#ad-name').value = device.name;
    $('#ad-host').value = device.host;
    $('#ad-port').value = String(device.port);
    $('#ad-tags').value = (device.tags || []).join(', ');
    $('#ad-descr').value = device.descr || '';
    $('#ad-tls').checked = device.tls_verify;
    $('#ad-apikey').value = '';
    $('#ad-apisecret').value = '';
    $('#ad-apikey').placeholder = '(unverändert)';
    $('#ad-apisecret').placeholder = '(unverändert)';
    $('#ad-credentials-hint').hidden = false;
    $('#add-modal-title').textContent = `„${device.name}" bearbeiten`;
    $('#add-modal-confirm').textContent = 'Speichern';
    $('#add-modal-error').hidden = true;
    $('#add-modal').hidden = false;
    setTimeout(() => $('#ad-name').focus(), 0);
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
    const apiSecret = $('#ad-apisecret').value;

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
        if (apiKey) body.api_key = apiKey;
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
    $('#device-modal').hidden = false;
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

  function closePlanModal() {
    $('#plan-modal').hidden = true;
    planMode = 'route';
    planPhase = 'input';
    currentPlan = null;
    retryDeviceIds = null;
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
    $('#pl-route-descr').value = '';
    $('#pl-route-disabled').checked = false;
    $('#pl-alias-name').value = '';
    $('#pl-alias-type').value = 'host';
    $('#pl-alias-content').value = '';
    $('#pl-alias-descr').value = '';
    $('#pl-alias-merge').checked = false;
    $('#pl-confirm').checked = false;
    // F19: Chip-Listen bei Modal-Reset leeren — sonst zeigen sie Eintraege
    // vom vorigen Geraet auch wenn der User das Modal frisch oeffnet.
    const aliasChips = $('#pl-alias-chips');
    if (aliasChips) { aliasChips.innerHTML = ''; aliasChips.hidden = true; }
    const gwChips = $('#pl-gateway-chips');
    if (gwChips) { gwChips.innerHTML = ''; gwChips.hidden = true; }
    aliasSuggestionTypes = new Map();
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
    if (phase === 'input') {
      cancel.textContent = 'Abbrechen';
      back.hidden = true;
      next.hidden = false;
      next.textContent = 'Vorschau anzeigen';
      next.disabled = false;
      saveProfile.hidden = false;
    } else if (phase === 'preview') {
      cancel.textContent = 'Abbrechen';
      back.hidden = false;
      next.hidden = false;
      next.textContent = 'Aktivieren';
      next.disabled = !$('#pl-confirm').checked;
      saveProfile.hidden = true;
    } else if (phase === 'result') {
      cancel.textContent = 'Schließen';
      back.hidden = true;
      next.hidden = true;
      saveProfile.hidden = true;
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
    if (planMode === 'route') {
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
      url = '/api/plans/route';
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
    showPlanPhase('input');
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
      const wantedSubsystem = planMode === 'route' ? 'routes' : 'firewall_alias';
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
    if (planMode === 'route') {
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
      const chipItems = [];
      for (const g of data.gateways) {
        const opt = document.createElement('option');
        opt.value = g.name;
        opt.label = g.address ? `${g.name} — ${g.address} (${g.status})` : g.name;
        dl.appendChild(opt);
        chipItems.push({ value: g.name, meta: g.status || '' });
      }
      renderSuggestionChips('pl-gateway-chips', chipItems, (picked) => {
        const inp = $('#pl-route-gateway');
        inp.value = picked;
        inp.dispatchEvent(new Event('input', { bubbles: true }));
        inp.focus();
      });
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

  function renderSuggestionChips(containerId, items, onPick) {
    // F19: ergaenzendes UI zur datalist — klickbare Chips erlauben Re-Browse
    // nach Auswahl (datalist filtert nach value und blendet dann alles aus).
    const c = $(`#${containerId}`);
    if (!c) return;
    c.innerHTML = '';
    if (!items.length) {
      c.hidden = true;
      return;
    }
    c.hidden = false;
    for (const item of items) {
      const chip = document.createElement('button');
      chip.type = 'button';
      chip.className = 'suggestion-chip';
      chip.dataset.value = item.value;
      const name = document.createElement('span');
      name.textContent = item.value;
      chip.appendChild(name);
      if (item.meta) {
        const meta = document.createElement('span');
        meta.className = 'suggestion-chip-type';
        meta.textContent = item.meta;
        chip.appendChild(meta);
      }
      chip.addEventListener('click', () => {
        onPick(item.value);
        // active-Markierung
        for (const other of c.querySelectorAll('.suggestion-chip')) {
          other.classList.toggle('active', other.dataset.value === item.value);
        }
      });
      c.appendChild(chip);
    }
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
      const chipItems = [];
      for (const a of data.aliases) {
        const opt = document.createElement('option');
        opt.value = a.name;
        opt.label = a.type ? `${a.name} (${a.type})` : a.name;
        dl.appendChild(opt);
        if (a.type) aliasSuggestionTypes.set(a.name, String(a.type).toLowerCase());
        chipItems.push({ value: a.name, meta: a.type || '' });
      }
      renderSuggestionChips('pl-alias-chips', chipItems, (picked) => {
        const inp = $('#pl-alias-name');
        inp.value = picked;
        inp.dispatchEvent(new Event('input', { bubbles: true }));
        inp.focus();
      });
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
    $('#bk-vault-path').value = '';
    $('#bk-vault-pw').value = '';
    $('#bulk-modal-error').hidden = true;
    $('#bulk-parse-errors').hidden = true;
    updateBulkFormatHint();
    $('#bulk-modal').hidden = false;
  }

  async function submitVaultImport() {
    const errorBox = $('#bulk-modal-error');
    errorBox.hidden = true;
    const path = $('#bk-vault-path').value.trim();
    const password = $('#bk-vault-pw').value;
    if (!path || !password) {
      errorBox.textContent = 'Bitte Pfad und Master-Passwort des Quell-Tresors angeben.';
      errorBox.hidden = false;
      return;
    }
    const btn = $('#bulk-submit-btn');
    btn.disabled = true;
    btn.textContent = 'Importiere…';
    try {
      const response = await apiPost('/api/imports/vault', {
        source_path: path,
        source_password: password,
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
    // File-Felder anzeigen vs Vault-Felder
    $('#bulk-file-row').hidden = isVault;
    $('#bulk-vault-path-row').hidden = !isVault;
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
    $('#vs-pw-current').value = '';
    $('#vs-pw-new1').value = '';
    $('#vs-pw-new2').value = '';
    $('#vs-timeout-error').hidden = true;
    $('#vs-timeout-ok').hidden = true;
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
      }
    } catch (_e) { /* Modal kann auch mit leerem Feld bedient werden */ }
  }

  function closeVaultSettingsModal() {
    $('#vault-settings-modal').hidden = true;
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
    $('#ssw-pw').value = '';
    $('#ssw-new-path').value = '';
    $('#ssw-new-pw1').value = '';
    $('#ssw-new-pw2').value = '';
    $('#ssw-create-block').hidden = true;
    $('#ssw-error').hidden = true;
    $('#ssw-toggle-create').textContent = 'Stattdessen neuen Tresor anlegen…';
    $('#single-switch-modal').hidden = false;
    // Bekannte Tresore laden
    try {
      const response = await apiGet('/api/vaults');
      if (!response.ok) throw new Error('Vault-Liste nicht erreichbar.');
      const data = await response.json();
      const select = $('#ssw-vault-select');
      select.innerHTML = '';
      const currentName = state.sessionInfo?.vault_filename;
      let hasOther = false;
      for (const v of data.vaults || []) {
        if (v.filename === currentName) continue;
        const opt = document.createElement('option');
        opt.value = v.path;
        opt.textContent = `${v.filename} — ${v.path}`;
        select.appendChild(opt);
        hasOther = true;
      }
      if (!hasOther) {
        const opt = document.createElement('option');
        opt.value = '';
        opt.textContent = '(keine weiteren Tresore gefunden)';
        select.appendChild(opt);
        select.disabled = true;
      } else {
        select.disabled = false;
      }
      $('#ssw-new-path').value = data.suggested_new_path || '';
    } catch (err) {
      showSswError(err.message);
    }
    setTimeout(() => $('#ssw-pw').focus(), 0);
  }

  function closeSingleSwitchModal() {
    $('#single-switch-modal').hidden = true;
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
      const path = $('#ssw-vault-select').value;
      const pw = $('#ssw-pw').value;
      if (!path) return showSswError('Bitte einen Tresor auswaehlen oder einen neuen anlegen.');
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
    if (aboutLoaded) return;
    try {
      const response = await fetch('/api/about');
      if (!response.ok) return;
      const data = await response.json();
      $('#about-name').textContent = data.name || 'OPN-Cockpit';
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
    } catch (_err) {
      /* still show what's in the markup; nothing fatal */
    }
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

    // Setup-Wizard
    const setupAdminBtn = $('#setup-admin-btn');
    if (setupAdminBtn) {
      setupAdminBtn.addEventListener('click', doSetupAdmin);
      $('#su-pw2').addEventListener('keydown', (e) => {
        if (e.key === 'Enter') doSetupAdmin();
      });
    }
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
      $('#vs-timeout-save').addEventListener('click', saveInactivityTimeout);
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
    $('#plan-modal-close').addEventListener('click', closePlanModal);
    $('#plan-modal-cancel').addEventListener('click', closePlanModal);
    $('#plan-back-btn').addEventListener('click', planBack);
    $('#plan-next-btn').addEventListener('click', planNextOrApply);
    $('#pl-load-gateways').addEventListener('click', loadGatewaySuggestions);
    $('#pl-load-aliases').addEventListener('click', loadAliasSuggestions);
    // F17: Datalist-Auswahl feuert ein input-Event mit dem fertigen Wert.
    $('#pl-alias-name').addEventListener('input', syncAliasTypeFromSuggestion);
    $('#pl-alias-name').addEventListener('change', syncAliasTypeFromSuggestion);
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
})();
