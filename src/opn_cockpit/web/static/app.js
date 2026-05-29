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
    $('#login-error').hidden = true;
    $('#create-error').hidden = true;
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

    const newPath = $('#new-vault-path');
    if (newPath && !newPath.value) newPath.value = data.suggested_new_path;
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
    const path = $('#new-vault-path').value.trim();
    const pw1 = $('#new-vault-pw1').value;
    const pw2 = $('#new-vault-pw2').value;
    const errorBox = $('#create-error');
    errorBox.hidden = true;

    if (!path) return showCreateError('Pfad fehlt.');
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
    await loadInventory();
    startHeartbeat();
    startSessionTicker();
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
    renderSidebar();
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
      return;
    }

    empty.hidden = true;
    const visible = state.devices.filter(deviceMatchesFilter);

    grid.innerHTML = '';
    for (const device of visible) {
      grid.appendChild(renderCard(device));
    }

    renderStatusSummary(state.devices);
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
    openLink.rel = 'noopener noreferrer';
    openLink.title = 'OPNsense-Weboberfläche öffnen';
    openLink.innerHTML = `<svg width="12" height="12" viewBox="0 0 13 13" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
      <path d="M6.5 2H3a1 1 0 00-1 1v7a1 1 0 001 1h7a1 1 0 001-1V6.5"/>
      <path d="M8 1.5h3.5V5"/>
      <line x1="6" y1="7" x2="11.5" y2="1.5"/>
    </svg>`;
    openLink.addEventListener('click', (e) => {
      e.stopPropagation();  // Karte soll nicht das Detail-Modal oeffnen
    });
    row.appendChild(openLink);
    article.appendChild(row);

    // Name
    const name = document.createElement('div');
    name.className = 'card-name';
    name.textContent = device.name;
    article.appendChild(name);

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
    $('#theme-toggle-login').addEventListener('click', toggleTheme);

    // Main: top bar
    $('#theme-toggle-main').addEventListener('click', toggleTheme);
    $('#lock-btn').addEventListener('click', doLock);

    // Main: search
    $('#search-input').addEventListener('input', (e) => {
      state.search = e.target.value.trim().toLowerCase();
      renderGrid();
    });

    // Sidebar actions
    $('#add-device-btn').addEventListener('click', openAddModal);
    $('#empty-add-btn').addEventListener('click', openAddModal);

    // Add-Modal
    $('#add-modal-close').addEventListener('click', closeAddModal);
    $('#add-modal-cancel').addEventListener('click', closeAddModal);
    $('#add-modal-confirm').addEventListener('click', doAddOrEditDevice);
    $('#add-modal').addEventListener('click', (e) => {
      if (e.target.id === 'add-modal') closeAddModal();
    });

    // Device-Modal
    $('#device-modal-close').addEventListener('click', closeDeviceModal);
    $('#device-modal-cancel').addEventListener('click', closeDeviceModal);
    $('#device-modal-delete').addEventListener('click', doDeleteDevice);
    $('#device-test-btn').addEventListener('click', doTestConnection);
    // #device-open-web-btn ist ein echter <a target="_blank">,
    // braucht keinen JS-Handler — Browser navigiert nativ.
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
