// OPN-Cockpit Frontend (Iter 2 — Login + Main-Platzhalter).
//
// State-Machine:
//   boot  -> versuche /api/auth/me mit gespeichertem Token
//             ok -> main
//             401 -> login
//   login -> Tresor-Auswahl + Passwort, oder Create-Vault-Inline-Dialog
//   main  -> einfacher Platzhalter (Inventar kommt in Iter 3)
//
// Token-Storage: sessionStorage (per Tab, beim Schliessen weg).

(function () {
  'use strict';

  const STATE_KEY = 'opn-cockpit-token';
  const THEME_KEY = 'opn-cockpit-theme';

  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => Array.from(document.querySelectorAll(sel));

  // -------------------- Theme --------------------

  function initTheme() {
    const saved = localStorage.getItem(THEME_KEY);
    if (saved === 'light' || saved === 'dark') {
      document.documentElement.setAttribute('data-theme', saved);
      return;
    }
    if (
      window.matchMedia &&
      window.matchMedia('(prefers-color-scheme: light)').matches
    ) {
      document.documentElement.setAttribute('data-theme', 'light');
    }
  }

  function toggleTheme() {
    const cur = document.documentElement.getAttribute('data-theme') || 'dark';
    const next = cur === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    try {
      localStorage.setItem(THEME_KEY, next);
    } catch (_) {}
  }

  // -------------------- Token --------------------

  function getToken() {
    try {
      return sessionStorage.getItem(STATE_KEY);
    } catch (_) {
      return null;
    }
  }

  function setToken(token) {
    try {
      sessionStorage.setItem(STATE_KEY, token);
    } catch (_) {}
  }

  function clearToken() {
    try {
      sessionStorage.removeItem(STATE_KEY);
    } catch (_) {}
  }

  // -------------------- API --------------------

  async function apiGet(path) {
    const headers = { Accept: 'application/json' };
    const token = getToken();
    if (token) headers.Authorization = `Bearer ${token}`;
    return await fetch(path, { headers });
  }

  async function apiPost(path, body) {
    const headers = {
      'Content-Type': 'application/json',
      Accept: 'application/json',
    };
    const token = getToken();
    if (token) headers.Authorization = `Bearer ${token}`;
    return await fetch(path, {
      method: 'POST',
      headers,
      body: body ? JSON.stringify(body) : undefined,
    });
  }

  // -------------------- Screen Switching --------------------

  function showScreen(name) {
    document.getElementById('app').setAttribute('data-state', name);
    $$('.screen').forEach((s) => {
      s.hidden = s.dataset.screen !== name;
    });
  }

  function showLoginView(name) {
    $$('.login-view').forEach((v) => {
      v.hidden = v.dataset.view !== name;
    });
    $('#login-error').hidden = true;
    $('#create-error').hidden = true;
  }

  // -------------------- Login --------------------

  async function fetchVaultsAndPopulate() {
    const response = await apiGet('/api/vaults');
    if (!response.ok) {
      throw new Error('Konnte Tresor-Liste nicht abrufen.');
    }
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

    // Default-Pfad fuer Create-Form vorbefuellen
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
      enterMain(data);
      startExpiryTicker();
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
    if (pw1.length < 12)
      return showCreateError('Passwort muss mindestens 12 Zeichen haben.');
    if (pw1 !== pw2)
      return showCreateError('Die beiden Passwörter stimmen nicht überein.');

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
      enterMain(data);
      startExpiryTicker();
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

  // -------------------- Main --------------------

  function enterMain(sessionInfo) {
    $('#current-vault').textContent = sessionInfo.vault_filename;
    $('#timeout-display').textContent = Math.round(
      sessionInfo.inactivity_timeout_s / 60
    );
    updateExpiry(sessionInfo.seconds_until_expiry);
    showScreen('main');
  }

  function updateExpiry(seconds) {
    const el = $('#expiry-display');
    if (!el) return;
    if (!seconds || seconds <= 0) {
      el.textContent = 'sofort';
      return;
    }
    const mins = Math.floor(seconds / 60);
    const secs = Math.floor(seconds % 60);
    el.textContent = `${String(mins).padStart(2, '0')}:${String(secs).padStart(2, '0')}`;
  }

  async function checkSession() {
    const response = await apiGet('/api/auth/me');
    if (response.status === 401) {
      clearToken();
      return null;
    }
    if (!response.ok) {
      throw new Error(`Backend-Fehler ${response.status}`);
    }
    return await response.json();
  }

  async function doLock() {
    await apiPost('/api/auth/lock');
    clearToken();
    showScreen('login');
    showLoginView('picker');
    await fetchVaultsAndPopulate();
  }

  let expiryTickerHandle = null;

  function startExpiryTicker() {
    if (expiryTickerHandle !== null) return;
    expiryTickerHandle = setInterval(async () => {
      try {
        const session = await checkSession();
        if (!session) {
          clearInterval(expiryTickerHandle);
          expiryTickerHandle = null;
          showScreen('login');
          showLoginView('picker');
          await fetchVaultsAndPopulate();
          return;
        }
        updateExpiry(session.seconds_until_expiry);
      } catch (_) {
        // Netzwerk-Hickup — ignorieren, naechster Tick versucht es wieder.
      }
    }, 30000);
  }

  // -------------------- Bootstrap --------------------

  async function bootstrap() {
    initTheme();
    bindStaticEvents();

    const status = $('#boot-status');

    // 1) Versionspruefung
    try {
      const v = await fetch('/api/version').then((r) => r.json());
      status.textContent = `Backend bereit · v${v.version}`;
    } catch (_) {
      status.textContent = 'Backend nicht erreichbar.';
      return;
    }

    // 2) Existierende Session?
    if (getToken()) {
      try {
        const session = await checkSession();
        if (session) {
          enterMain(session);
          startExpiryTicker();
          return;
        }
      } catch (_) {
        // Token kaputt — Login-Screen zeigen.
      }
    }

    // 3) Login
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
    $('#unlock-btn').addEventListener('click', doUnlock);
    $('#password-input').addEventListener('keydown', (e) => {
      if (e.key === 'Enter') doUnlock();
    });
    $('#create-vault-btn').addEventListener('click', () => {
      showLoginView('create');
      $('#new-vault-pw1').focus();
    });
    $('#create-back-btn').addEventListener('click', () => {
      showLoginView('picker');
    });
    $('#create-confirm-btn').addEventListener('click', doCreateVault);
    $('#theme-toggle-login').addEventListener('click', toggleTheme);
    $('#theme-toggle-main').addEventListener('click', toggleTheme);
    $('#lock-btn').addEventListener('click', doLock);
  }

  document.addEventListener('DOMContentLoaded', bootstrap);
})();
