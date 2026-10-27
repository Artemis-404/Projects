'use strict';

/**
 * UQAB renderer: tabs, address bar, history and the 1 Hz session panel.
 *
 * The renderer holds no privileges. Every privileged action goes through
 * `window.uqab` (preload -> main), and every page is drawn by a <webview> whose
 * session the main process gates on the tunnel state.
 */

const ui = {
  tabs: document.getElementById('tabs'),
  newTab: document.getElementById('new-tab'),
  viewport: document.getElementById('viewport'),
  blocked: document.getElementById('blocked'),
  blockedStart: document.getElementById('blocked-start'),
  addressForm: document.getElementById('address-form'),
  address: document.getElementById('address'),
  back: document.getElementById('back'),
  forward: document.getElementById('forward'),
  reload: document.getElementById('reload'),
  bookmark: document.getElementById('bookmark'),
  lock: document.getElementById('lock'),
  historyToggle: document.getElementById('history-toggle'),
  history: document.getElementById('history'),
  historyList: document.getElementById('history-list'),
  historySearch: document.getElementById('history-search'),
  historyClear: document.getElementById('history-clear'),
  historyClose: document.getElementById('history-close'),
  dashboard: document.getElementById('open-dashboard'),
  state: document.getElementById('state-pill'),
  modeBadge: document.getElementById('mode-badge'),
  bootstrapWrap: document.getElementById('bootstrap-wrap'),
  bootstrapBar: document.getElementById('bootstrap-bar'),
  bootstrapText: document.getElementById('bootstrap-text'),
  persona: document.getElementById('persona'),
  personaHint: document.getElementById('persona-hint'),
  toggle: document.getElementById('toggle'),
  sessionDetail: document.getElementById('session-detail'),
  realUp: document.getElementById('real-up'),
  realDown: document.getElementById('real-down'),
  coverUp: document.getElementById('cover-up'),
  coverDown: document.getElementById('cover-down'),
  overhead: document.getElementById('overhead'),
  period: document.getElementById('period'),
  uptime: document.getElementById('uptime'),
  consentPill: document.getElementById('consent-pill'),
  consentText: document.getElementById('consent-text'),
  consentMeta: document.getElementById('consent-meta'),
  consentAccept: document.getElementById('consent-accept'),
  consentWithdraw: document.getElementById('consent-withdraw'),
  appMeta: document.getElementById('app-meta'),
  modal: document.getElementById('modal'),
  modalText: document.getElementById('modal-text'),
  modalAccept: document.getElementById('modal-accept'),
  modalCancel: document.getElementById('modal-cancel'),
  toast: document.getElementById('toast'),
  // setup wizard
  openSetup: document.getElementById('open-setup'),
  setupSummary: document.getElementById('setup-summary'),
  wizard: document.getElementById('wizard'),
  wizardSteps: Array.from(document.querySelectorAll('.wizard-body')),
  wizardDots: Array.from(document.querySelectorAll('#wizard-dots .dot')),
  wizardNext: document.getElementById('wizard-next'),
  wizardBack: document.getElementById('wizard-back'),
  wizardSkip: document.getElementById('wizard-skip'),
  wizardError: document.getElementById('wizard-error'),
  torDetect: document.getElementById('tor-detect'),
  torPill: document.getElementById('tor-pill'),
  torText: document.getElementById('tor-text'),
  torBinaryField: document.getElementById('tor-binary-field'),
  torBinary: document.getElementById('tor-binary'),
  torBrowse: document.getElementById('tor-browse'),
  socksPort: document.getElementById('socks-port'),
  setupPersona: document.getElementById('setup-persona'),
  bridges: document.getElementById('bridges'),
  startWithWindows: document.getElementById('start-with-windows'),
  consentPillWizard: document.getElementById('wizard-consent-pill'),
  sumMode: document.getElementById('sum-mode'),
  sumPort: document.getElementById('sum-port'),
  sumPersona: document.getElementById('sum-persona'),
  sumBridges: document.getElementById('sum-bridges'),
};

const bridge = window.uqab || null;
const DASHBOARD_URL = 'http://127.0.0.1:8501/';

const fmt = {
  bytes(value) {
    const bytes = Number(value) || 0;
    if (bytes < 1024) return `${bytes} B`;
    const units = ['KiB', 'MiB', 'GiB'];
    let size = bytes / 1024;
    let index = 0;
    while (size >= 1024 && index < units.length - 1) {
      size /= 1024;
      index += 1;
    }
    return `${size.toFixed(size >= 100 ? 0 : 1)} ${units[index]}`;
  },
  duration(seconds) {
    const total = Math.max(0, Math.floor(Number(seconds) || 0));
    const minutes = Math.floor(total / 60);
    return `${minutes}:${String(total % 60).padStart(2, '0')}`;
  },
  overhead(value) {
    const ratio = Number(value) || 0;
    return ratio > 0 ? `${ratio.toFixed(2)}x` : '-';
  },
};

const STATE_LABELS = {
  stopped: ['Disconnected', 'idle'],
  connecting: ['Connecting', 'pending'],
  connected: ['Protected', 'good'],
  blocked: ['Blocked', 'bad'],
};

const state = {
  session: 'stopped',
  socksPort: 0,
  persona: '',
  firstRecordAt: 0,
  tabs: [],
  activeTab: 0,
  consentAccepted: false,
  wizardStep: 1,
  settings: null,
  tor: null,
  personaIds: [],
};

// ------------------------------------------------------------------- helpers

function unwrap(result, fallbackMessage = 'the engine did not answer') {
  if (!result) throw new Error(fallbackMessage);
  if (result.ok) return result.value;
  const detail = result.detail && result.detail !== result.error ? ` (${result.detail})` : '';
  throw new Error(`${result.error || fallbackMessage}${detail}`);
}

let toastTimer = null;
function toast(message, tone = 'bad') {
  ui.toast.textContent = message;
  ui.toast.dataset.tone = tone;
  ui.toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    ui.toast.hidden = true;
  }, 5000);
}

function setText(node, text) {
  if (node) node.textContent = text;
}

// ---------------------------------------------------------------------- tabs

/**
 * The URL a <webview> shows. Its methods only exist once it is attached and
 * dom-ready, so fall back to the src attribute before that.
 */
function urlOf(view) {
  if (!view) return '';
  try {
    return view.getURL() || '';
  } catch {
    return view.getAttribute('src') || '';
  }
}

/** Call a <webview> navigation predicate that may not be ready yet. */
function viewCan(view, method) {
  try {
    return Boolean(view && view[method] && view[method]());
  } catch {
    return false;
  }
}

function tabIndex(id) {
  return state.tabs.findIndex((tab) => tab.id === id);
}

let nextTabId = 0;

function activeContents() {
  const tab = state.tabs[state.activeTab];
  return tab ? tab.view : null;
}

function createTab(url = 'about:blank', { activate = true } = {}) {
  const id = nextTabId++;
  const view = document.createElement('webview');
  view.setAttribute('partition', 'persist:uqab-tabs');
  view.setAttribute('allowpopups', 'false');
  view.setAttribute('webpreferences', 'contextIsolation=yes,nodeIntegration=no,spellcheck=no');
  view.dataset.tab = String(id);
  view.src = url === 'about:blank' ? 'about:blank' : url;

  const element = document.createElement('div');
  element.className = 'tab';
  element.dataset.active = 'false';
  element.innerHTML = '<span class="dot"></span><span class="title">New tab</span><button class="close" aria-label="Close tab">&#10005;</button>';
  element.addEventListener('click', (event) => {
    if (event.target.classList.contains('close')) {
      closeTab(id);
      return;
    }
    activateTab(id);
  });
  ui.tabs.appendChild(element);
  ui.viewport.appendChild(view);

  const tab = { id, view, element };
  state.tabs.push(tab);
  wireView(tab);

  if (activate) activateTab(id);
  return tab;
}

function wireView(tab) {
  const view = tab.view;
  const title = tab.element.querySelector('.title');

  view.addEventListener('dom-ready', () => {
    const current = urlOf(view);
    if (state.tabs[state.activeTab] !== tab) return;
    ui.address.value = current === 'about:blank' ? '' : current;
    updateNavButtons();
  });

  view.addEventListener('did-navigate', () => {
    if (state.tabs[state.activeTab] !== tab) return;
    ui.address.value = urlOf(view);
    updateNavButtons();
  });

  view.addEventListener('did-navigate-in-page', () => {
    if (state.tabs[state.activeTab] !== tab) return;
    ui.address.value = urlOf(view);
    updateNavButtons();
  });

  view.addEventListener('page-title-updated', (event) => {
    title.textContent = (event.title || 'Untitled').slice(0, 40);
    view.dataset.title = event.title || '';
  });

  view.addEventListener('page-favicon-updated', () => {
    /* favicons are deliberately ignored: no extra network fetches */
  });

  view.addEventListener('did-start-loading', () => {
    tab.element.dataset.loading = 'true';
  });

  view.addEventListener('did-stop-loading', () => {
    tab.element.dataset.loading = 'false';
  });

  view.addEventListener('did-fail-load', (event) => {
    if (!event.isMainFrame || event.errorCode === -3) return;
    if (state.session !== 'connected') {
      toast('No tunnel: start a session before loading a page.');
    } else {
      toast(`Could not load that page (${event.errorDescription || event.errorCode}).`);
    }
  });

  view.addEventListener('new-window', (event) => {
    event.preventDefault();
    openUrl(event.url, { newTab: true });
  });

  view.addEventListener('will-navigate', (event) => {
    const url = String(event.url || '');
    if (!/^https?:/i.test(url) && !url.startsWith('about:')) event.preventDefault();
  });
}

function activateTab(id) {
  const index = tabIndex(id);
  if (index < 0) return;
  state.activeTab = index;
  for (const tab of state.tabs) {
    const active = tab.id === id;
    tab.element.dataset.active = active ? 'true' : 'false';
    tab.view.dataset.active = active ? 'true' : 'false';
  }
  const current = urlOf(state.tabs[index].view);
  ui.address.value = current === 'about:blank' ? '' : current;
  updateLock();
  updateNavButtons();
}

function closeTab(id) {
  const index = tabIndex(id);
  if (index < 0) return;
  const [tab] = state.tabs.splice(index, 1);
  tab.view.remove();
  tab.element.remove();
  if (!state.tabs.length) {
    createTab('about:blank');
    return;
  }
  if (state.activeTab === index) {
    activateTab(state.tabs[Math.max(0, index - 1)].id);
  } else if (state.activeTab > index) {
    state.activeTab -= 1;
  }
}

function updateNavButtons() {
  const view = activeContents();
  const canGoBack = Boolean(view && view.canGoBack && view.canGoBack());
  const canGoForward = Boolean(view && view.canGoForward && view.canGoForward());
  ui.back.disabled = !canGoBack;
  ui.forward.disabled = !canGoForward;
  const current = urlOf(view);
  ui.bookmark.textContent = current && window.__bookmarks && window.__bookmarks.has(current) ? '\u2605' : '\u2606';
}

function navigate(url) {
  const target = String(url || '');
  if (!target) return;
  if (state.session !== 'connected') {
    toast('Start a session first: nothing may leave this machine unprotected.');
    return;
  }
  if (!/^https?:/i.test(target)) {
    toast('Only http and https pages can be opened.');
    return;
  }
  let view = activeContents();
  if (!view) {
    createTab(target);
    return;
  }
  if (urlOf(view) === 'about:blank') {
    view.src = target;
  } else {
    view.loadURL(target);
  }
  ui.address.value = target;
}

function openUrl(url, { newTab = false } = {}) {
  if (newTab || !activeContents() || urlOf(activeContents()) === 'about:blank') {
    createTab(url);
    if (state.session === 'connected') {
      ui.address.value = url;
    }
    return;
  }
  navigate(url);
}

// ----------------------------------------------------------------- address bar

ui.addressForm.addEventListener('submit', (event) => {
  event.preventDefault();
  const raw = ui.address.value.trim();
  if (!raw) return;
  if (/^[a-zA-Z][a-zA-Z0-9+.-]*:/.test(raw)) {
    if (!/^https?:/i.test(raw)) {
      toast('Only http and https addresses are supported.');
      return;
    }
    navigate(raw);
    return;
  }
  if (raw.includes(' ') || !raw.includes('.')) {
    navigate(`https://duckduckgo.com/?q=${encodeURIComponent(raw)}`);
    return;
  }
  navigate(`https://${raw}`);
});

ui.address.addEventListener('focus', () => ui.address.select());

ui.back.addEventListener('click', () => {
  const view = activeContents();
  if (viewCan(view, 'canGoBack')) view.goBack();
  updateNavButtons();
});

ui.forward.addEventListener('click', () => {
  const view = activeContents();
  if (viewCan(view, 'canGoForward')) view.goForward();
  updateNavButtons();
});

ui.reload.addEventListener('click', () => {
  const view = activeContents();
  if (view && view.reload) view.reload();
});

ui.newTab.addEventListener('click', () => createTab('about:blank'));

ui.bookmark.addEventListener('click', async () => {
  const view = activeContents();
  const url = urlOf(view);
  if (!url || !/^https?:/i.test(url)) return;
  try {
    const marked = unwrap(
      await bridge.history.toggleBookmark(url, view.dataset.title || ''),
      'bookmarks are unavailable',
    );
    window.__bookmarks = window.__bookmarks || new Set();
    if (marked) window.__bookmarks.add(url);
    else window.__bookmarks.delete(url);
    updateNavButtons();
    toast(marked ? 'Bookmarked.' : 'Bookmark removed.', 'good');
  } catch (error) {
    toast(error.message);
  }
});

// ---------------------------------------------------------------------- panel

function humanState(value) {
  return STATE_LABELS[value] || ['Unknown', 'idle'];
}

function renderSession(status) {
  if (!status) return;
  state.session = status.state || 'stopped';
  state.socksPort = Number(status.socks_port) || 0;
  const [label, tone] = humanState(state.session);
  setText(ui.state, label);
  ui.state.dataset.tone = tone;

  const active = state.session === 'connecting' || state.session === 'connected';
  ui.toggle.textContent = active ? 'Stop' : 'Start';
  ui.toggle.classList.toggle('stop', active);
  ui.persona.disabled = active;

  const mode = status.mode === 'tor' ? 'Tor' : state.session !== 'stopped' && status.mode ? 'Lab simulation' : '';
  ui.modeBadge.hidden = !mode;
  setText(ui.modeBadge, mode);

  if (state.session === 'connecting') {
    ui.bootstrapWrap.hidden = false;
    const percent = Math.max(5, Math.min(99, Number(status.bootstrap) || 5));
    ui.bootstrapBar.style.width = `${percent}%`;
    setText(
      ui.bootstrapText,
      status.mode === 'tor' ? `bootstrapping ${percent}%` : 'starting the lab stack',
    );
  } else {
    ui.bootstrapWrap.hidden = true;
  }

  setText(ui.sessionDetail, status.reason || status.detail || '');
  if (status.persona) {
    state.persona = status.persona;
    if (ui.persona.value !== status.persona) ui.persona.value = status.persona;
  }
  if (state.session === 'connected' && !state.firstRecordAt) state.firstRecordAt = Date.now();
  if (state.session === 'stopped' || state.session === 'blocked') state.firstRecordAt = 0;

  const counters = status.counters || {};
  setText(ui.realDown, fmt.bytes(counters.real_down));
  setText(ui.realUp, fmt.bytes(counters.real_up));
  setText(ui.coverDown, fmt.bytes(counters.cover_down));
  setText(ui.coverUp, fmt.bytes(counters.cover_up));
  setText(ui.overhead, fmt.overhead(counters.overhead));
  setText(ui.period, counters.period || '-');
  if (state.firstRecordAt) {
    setText(ui.uptime, fmt.duration((Date.now() - state.firstRecordAt) / 1000));
  } else {
    setText(ui.uptime, fmt.duration(0));
  }
  if (counters.stale && state.session === 'connected') {
    setText(ui.sessionDetail, 'the engine has not reported for a few seconds');
  }

  updateLock();
  const consent = status.consent;
  if (consent) renderConsent(consent);
}

function updateLock() {
  const open = state.session === 'connected';
  ui.lock.dataset.open = open ? 'true' : 'false';
  ui.lock.title = open
    ? `All traffic leaves through SOCKS5 127.0.0.1:${state.socksPort || 9150} (remote DNS)`
    : 'Blocked until a session is protected';
  ui.blocked.hidden = open || state.tabs.some((tab) => urlOf(tab.view) !== 'about:blank');
}

function renderConsent(consent) {
  state.consentAccepted = Boolean(consent.opted_in);
  ui.consentPill.textContent = state.consentAccepted ? 'on' : 'off';
  ui.consentPill.className = `pill ${state.consentAccepted ? 'on' : 'off'}`;
  const parts = [];
  if (consent.accepted_at) parts.push(`accepted ${String(consent.accepted_at).slice(0, 19)}`);
  if (consent.text_version) parts.push(`text v${consent.text_version}`);
  parts.push(consent.profile_present ? 'profile on disk' : 'no profile on disk');
  setText(ui.consentMeta, parts.join(' \u00b7 '));
  ui.consentAccept.disabled = state.consentAccepted;
}

async function refreshStatus() {
  try {
    const result = await bridge.session.status();
    renderSession(unwrap(result, 'the engine is not reachable'));
  } catch (error) {
    setText(ui.sessionDetail, error.message);
  }
}

async function loadPersonas() {
  try {
    const result = await bridge.api.personas();
    const body = unwrap(result, 'personas are unavailable') || {};
    // /personas returns {id, source, description} objects; the selects want ids.
    const ids = (Array.isArray(body.personas) ? body.personas : [])
      .map((entry) => (typeof entry === 'string' ? entry : entry && entry.id))
      .filter(Boolean);
    state.personaIds = ids;
    fillPersonaSelect(ui.persona, ids);
    if (ui.setupPersona) fillPersonaSelect(ui.setupPersona, ids);
    if (state.persona) ui.persona.value = state.persona;
    setText(
      ui.personaHint,
      ids.length ? `${ids.length} personas available; locked while a session runs.` : 'No personas found.',
    );
  } catch (error) {
    setText(ui.personaHint, `could not load personas: ${error.message}`);
  }
}

async function toggleSession() {
  const running = state.session === 'connecting' || state.session === 'connected';
  ui.toggle.disabled = true;
  try {
    if (running) {
      const status = unwrap(await bridge.session.stop(), 'could not stop the session');
      renderSession(status);
      toast('Session stopped.', 'good');
    } else {
      const persona = ui.persona.value;
      if (!persona) throw new Error('choose a persona first');
      state.persona = persona;
      const pending = unwrap(await bridge.session.start(persona), 'could not start the session');
      renderSession(pending);
      setText(ui.sessionDetail, 'starting the tunnel...');
      for (let attempt = 0; attempt < 60; attempt += 1) {
        await new Promise((resolve) => setTimeout(resolve, 500));
        const status = unwrap(await bridge.session.status(), 'the engine stopped answering');
        renderSession(status);
        if (status.state === 'connected' || status.state === 'blocked') break;
      }
      if (state.session === 'connected') {
        toast('Protected: every tab now goes through UQAB.', 'good');
        ensureBlankTabReady();
      } else if (state.session === 'blocked') {
        toast('The engine could not open the tunnel. See the panel for the reason.');
      }
    }
  } catch (error) {
    toast(error.message);
  } finally {
    ui.toggle.disabled = false;
    await refreshStatus();
  }
}

function ensureBlankTabReady() {
  if (!state.tabs.length) createTab('about:blank');
  updateLock();
}

ui.toggle.addEventListener('click', toggleSession);
ui.blockedStart.addEventListener('click', toggleSession);
ui.persona.addEventListener('change', () => {
  state.persona = ui.persona.value;
});

// -------------------------------------------------------------------- consent

ui.consentAccept.addEventListener('click', async () => {
  try {
    const consent = unwrap(await bridge.api.consent(), 'consent state is unavailable');
    ui.modalText.textContent = consent.text || 'No consent text available.';
    ui.modalText.dataset.version = consent.current_text_version || '1.1';
    ui.modal.hidden = false;
  } catch (error) {
    toast(error.message);
  }
});

ui.modalCancel.addEventListener('click', () => {
  ui.modal.hidden = true;
});

ui.modalAccept.addEventListener('click', async () => {
  ui.modal.hidden = true;
  try {
    const version = ui.modalText.dataset.version || '1.1';
    const consent = unwrap(await bridge.api.acceptConsent(version), 'could not record consent');
    renderConsent(consent);
    toast('Clearnet learning enabled on this machine.', 'good');
  } catch (error) {
    toast(error.message);
  }
});

ui.consentWithdraw.addEventListener('click', async () => {
  try {
    const consent = unwrap(await bridge.api.withdrawConsent(), 'could not withdraw consent');
    renderConsent(consent);
    toast('Consent withdrawn; the learned profile was deleted.', 'good');
  } catch (error) {
    toast(error.message);
  }
});

// -------------------------------------------------------------------- history

const bookmarkSet = new Set();
window.__bookmarks = bookmarkSet;

async function showHistory(query = '') {
  ui.history.hidden = false;
  try {
    const result = query
      ? await bridge.history.search(query)
      : await bridge.history.list(200);
    const entries = unwrap(result, 'history is unavailable') || [];
    ui.historyList.innerHTML = '';
    if (!entries.length) {
      const empty = document.createElement('div');
      empty.className = 'empty';
      empty.textContent = 'No history yet.';
      ui.historyList.appendChild(empty);
      return;
    }
    for (const entry of entries) {
      const item = document.createElement('div');
      item.className = 'history-item';
      const title = document.createElement('div');
      title.className = 'title';
      title.textContent = entry.title || entry.url;
      const url = document.createElement('div');
      url.className = 'url';
      url.textContent = entry.url;
      item.append(title, url);
      item.addEventListener('click', () => {
        ui.history.hidden = true;
        openUrl(entry.url, { newTab: true });
      });
      ui.historyList.appendChild(item);
    }
  } catch (error) {
    toast(error.message);
  }
}

ui.historyToggle.addEventListener('click', () => {
  if (ui.history.hidden) showHistory();
  else ui.history.hidden = true;
});
ui.historyClose.addEventListener('click', () => {
  ui.history.hidden = true;
});
ui.historySearch.addEventListener('input', () => showHistory(ui.historySearch.value));
ui.historyClear.addEventListener('click', async () => {
  try {
    unwrap(await bridge.history.clear(), 'could not clear history');
    await showHistory();
  } catch (error) {
    toast(error.message);
  }
});

ui.dashboard.addEventListener('click', async () => {
  try {
    unwrap(await bridge.app.openExternal(DASHBOARD_URL), 'could not open the dashboard');
    toast(`Dashboard opened at ${DASHBOARD_URL} - start it with: python -m dashboard.server`, 'good');
  } catch (error) {
    toast(`${error.message}. Start it with: python -m dashboard.server`);
  }
});


// ------------------------------------------------------------- setup wizard

function fillPersonaSelect(select, ids) {
  if (!select) return;
  const previous = select.value;
  select.innerHTML = '';
  const list = ids && ids.length ? ids : ['balanced', 'high', 'constant', 'low'];
  for (const id of list) {
    const option = document.createElement('option');
    option.value = id;
    option.textContent = id;
    select.appendChild(option);
  }
  if (previous && list.includes(previous)) select.value = previous;
}

function selectedMode() {
  const checked = document.querySelector('input[name="mode"]:checked');
  return checked ? checked.value : 'auto';
}

function setMode(mode) {
  for (const input of document.querySelectorAll('input[name="mode"]')) {
    input.checked = input.value === mode;
  }
  updateModeFields();
}

function updateModeFields() {
  const mode = selectedMode();
  ui.torBinaryField.hidden = mode !== 'external';
  const tor = state.tor;
  if (mode === 'simulated') {
    ui.torPill.textContent = 'simulation';
    ui.torPill.className = 'pill warn';
    ui.torText.textContent = 'UQAB will use its own demo stack. No Tor needed.';
    return;
  }
  if (!tor) {
    ui.torPill.textContent = 'checking';
    ui.torPill.className = 'pill';
    ui.torText.textContent = 'looking for a Tor binary...';
    return;
  }
  if (tor.found) {
    ui.torPill.textContent = 'Tor found';
    ui.torPill.className = 'pill ok';
    ui.torText.textContent = `${tor.version || 'Tor'} at ${tor.path}`;
  } else {
    ui.torPill.textContent = mode === 'external' ? 'not found' : 'Tor not found';
    ui.torPill.className = 'pill warn';
    ui.torText.textContent =
      mode === 'external'
        ? 'Choose the tor binary below, or switch to Automatic / Lab simulation.'
        : 'No Tor on this machine, so UQAB will use its lab simulation.';
  }
}

async function detectTor() {
  if (!bridge || !bridge.settings) return;
  try {
    const result = await bridge.settings.detect();
    const value = unwrap(result, 'Tor detection failed');
    state.tor = value.tor;
    if (value.setup && value.setup.persona_default && ui.setupPersona) {
      ui.setupPersona.value = value.setup.persona_default;
    }
  } catch (error) {
    state.tor = null;
    ui.torText.textContent = `could not look for Tor: ${error.message}`;
  }
  updateModeFields();
}

function showWizardStep(step) {
  state.wizardStep = Math.min(3, Math.max(1, step));
  for (const section of ui.wizardSteps) {
    section.hidden = Number(section.dataset.step) !== state.wizardStep;
  }
  ui.wizardDots.forEach((dot, index) => {
    dot.classList.toggle('active', index + 1 <= state.wizardStep);
  });
  ui.wizardBack.disabled = state.wizardStep === 1;
  ui.wizardNext.textContent = state.wizardStep === 3 ? 'Finish' : 'Next';
  ui.wizardError.hidden = true;
  if (state.wizardStep === 3) renderWizardSummary();
}

function wizardValues() {
  return {
    mode: selectedMode(),
    torBinary: ui.torBinary.value.trim(),
    socksPort: Number(ui.socksPort.value),
    bridges: ui.bridges.value,
    persona: ui.setupPersona.value || 'balanced',
    startWithWindows: ui.startWithWindows.checked,
    completed: true,
  };
}

function renderWizardSummary() {
  const values = wizardValues();
  const modeText =
    values.mode === 'simulated'
      ? 'Lab simulation (no Tor needed)'
      : values.mode === 'external'
        ? `Tor binary: ${values.torBinary || 'not chosen'}`
        : state.tor && state.tor.found
          ? `Automatic - uses Tor at ${state.tor.path}`
          : 'Automatic - Tor not found, lab simulation will be used';
  setText(ui.sumMode, modeText);
  setText(ui.sumPort, String(values.socksPort || 9150));
  setText(ui.sumPersona, values.persona);
  const bridges = String(values.bridges || '')
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);
  setText(ui.sumBridges, bridges.length ? `${bridges.length} configured` : 'none');
}

function openWizard() {
  if (!bridge) {
    toast('Settings are only available inside the desktop app.');
    return;
  }
  const settings = state.settings || {};
  setMode(settings.mode || 'auto');
  ui.torBinary.value = settings.torBinary || '';
  ui.socksPort.value = settings.socksPort || 9150;
  ui.bridges.value = (settings.bridges || []).join('\n');
  ui.startWithWindows.checked = Boolean(settings.startWithWindows);
  fillPersonaSelect(ui.setupPersona, state.personaIds);
  ui.setupPersona.value = settings.persona || state.persona || 'balanced';
  ui.wizard.hidden = false;
  showWizardStep(1);
  detectTor();
}

function closeWizard() {
  ui.wizard.hidden = true;
  ui.wizardError.hidden = true;
}

async function saveWizard() {
  const values = wizardValues();
  ui.wizardNext.disabled = true;
  ui.wizardError.hidden = true;
  try {
    const result = await bridge.settings.save({ ...(state.settings || {}), ...values });
    const saved = unwrap(result, 'could not save the settings');
    state.settings = saved.settings;
    state.tor = saved.tor;
    renderSetupSummary();
    renderSession(saved.status);
    closeWizard();
    toast('Setup saved. UQAB is ready.', 'good');
    await loadPersonas();
  } catch (error) {
    ui.wizardError.textContent = error.message;
    ui.wizardError.hidden = false;
  } finally {
    ui.wizardNext.disabled = false;
  }
}

function renderSetupSummary() {
  const settings = state.settings;
  if (!settings) {
    setText(ui.setupSummary, 'not configured yet');
    return;
  }
  const labels = {
    auto: 'Automatic',
    external: 'Your Tor binary',
    simulated: 'Lab simulation',
  };
  let text = labels[settings.mode] || settings.mode;
  if (settings.mode === 'auto' && state.tor) {
    text += state.tor.found ? ' (Tor found)' : ' (no Tor, lab mode)';
  }
  if (settings.bridges && settings.bridges.length) text += `, ${settings.bridges.length} bridges`;
  setText(ui.setupSummary, `${text} - SOCKS ${settings.socksPort}, ${settings.persona}`);
}

async function loadSettings(openOnFirstRun = true) {
  if (!bridge || !bridge.settings) return;
  try {
    const result = await bridge.settings.get();
    const value = unwrap(result, 'settings are unavailable');
    state.settings = value.settings;
    renderSetupSummary();
    if (openOnFirstRun && value.settings && !value.settings.completed) {
      openWizard();
    }
  } catch (error) {
    setText(ui.setupSummary, `settings unavailable: ${error.message}`);
  }
}

if (ui.openSetup) ui.openSetup.addEventListener('click', openWizard);
if (ui.wizardNext) {
  ui.wizardNext.addEventListener('click', () => {
    if (state.wizardStep === 3) {
      saveWizard();
      return;
    }
    if (state.wizardStep === 1) {
      const mode = selectedMode();
      if (mode === 'external' && !ui.torBinary.value.trim()) {
        ui.wizardError.textContent = 'Choose your tor binary, or pick Automatic / Lab simulation.';
        ui.wizardError.hidden = false;
        return;
      }
    }
    showWizardStep(state.wizardStep + 1);
  });
}
if (ui.wizardBack) ui.wizardBack.addEventListener('click', () => showWizardStep(state.wizardStep - 1));
if (ui.wizardSkip) {
  ui.wizardSkip.addEventListener('click', async () => {
    // "Skip" keeps the defaults and marks setup done, so it never nags again.
    const defaults = { ...(state.settings || {}), completed: true };
    try {
      const result = await bridge.settings.save(defaults);
      const saved = unwrap(result, 'could not save the defaults');
      state.settings = saved.settings;
      state.tor = saved.tor;
      renderSetupSummary();
      renderSession(saved.status);
    } catch (error) {
      toast(error.message);
    }
    closeWizard();
  });
}
for (const input of document.querySelectorAll('input[name="mode"]')) {
  input.addEventListener('change', updateModeFields);
}
if (ui.torBrowse) {
  ui.torBrowse.addEventListener('click', async () => {
    try {
      const result = await bridge.settings.pickBinary();
      const path = unwrap(result, 'could not open the file picker');
      if (path) {
        ui.torBinary.value = path;
        await detectTor();
      }
    } catch (error) {
      toast(error.message);
    }
  });
}

// ---------------------------------------------------------------- keyboard

document.addEventListener('keydown', (event) => {
  const mod = event.ctrlKey || event.metaKey;
  if (mod && event.key === 't') {
    event.preventDefault();
    createTab('about:blank');
  } else if (mod && event.key === 'w') {
    event.preventDefault();
    if (state.tabs[state.activeTab]) closeTab(state.tabs[state.activeTab].id);
  } else if (mod && event.key === 'r') {
    event.preventDefault();
    const view = activeContents();
    if (view && view.reload) view.reload();
  } else if (mod && event.key === 'l') {
    event.preventDefault();
    ui.address.focus();
  } else if (mod && event.key === 'h') {
    event.preventDefault();
    showHistory();
  } else if (mod && event.key === 'Tab') {
    event.preventDefault();
    const next = (state.activeTab + (event.shiftKey ? -1 : 1) + state.tabs.length) % state.tabs.length;
    activateTab(state.tabs[next].id);
  }
});

// ------------------------------------------------------------------- startup

async function boot() {
  if (!bridge) {
    toast('This page must run inside the UQAB desktop app.');
    return;
  }
  // Push events first: the main process may announce settings before the
  // awaits below finish.
  bridge.on('tunnel:state', (payload) => {
    if (!payload) return;
    state.socksPort = payload.socksPort || state.socksPort;
  });
  bridge.on('tab:open', ({ url }) => {
    if (url) openUrl(url, { newTab: true });
  });
  bridge.on('settings:loaded', ({ settings }) => {
    state.settings = settings;
    renderSetupSummary();
    if (!settings.completed && ui.wizard.hidden) openWizard();
  });
  bridge.on('engine:failure', ({ message }) => {
    toast(`The engine failed to start: ${message}`);
  });

  createTab('about:blank');
  try {
    // Wait for the control API (shared with the main process's own start-up).
    unwrap(await bridge.engine.ensure(), 'the engine did not start');
  } catch (error) {
    toast(`The engine failed to start: ${error.message}`);
  }
  await loadPersonas();
  await refreshStatus();
  await loadSettings(true);
  try {
    const info = unwrap(await bridge.app.info(), 'app information is unavailable');
    setText(ui.appMeta, `UQAB ${info.version} \u00b7 Electron ${info.electron}`);
  } catch {
    setText(ui.appMeta, 'UQAB');
  }
  try {
    const bookmarks = unwrap(await bridge.history.bookmarks(), 'bookmarks are unavailable') || [];
    for (const item of bookmarks) bookmarkSet.add(item.url);
  } catch {
    /* bookmarks are optional */
  }

  // The single 1 Hz panel tick (FR-29).
  setInterval(refreshStatus, 1000);
}

boot();
