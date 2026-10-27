'use strict';

/**
 * UQAB desktop app - Electron main process.
 *
 * Responsibilities, and nothing more:
 *  1. own the window and the browser tabs (<webview>, Min-style);
 *  2. start and supervise the Python control API (app_api/server.py);
 *  3. route **every** tab request through the SOCKS5 port the engine reports,
 *     and refuse to load anything at all while no tunnel is up;
 *  4. expose a tiny, validated IPC surface to the renderer.
 *
 * The browser shell follows Min's architecture (tabs, address bar, history) and
 * vendors Min's Apache-2.0 licence; see NOTICE for the attribution and exactly
 * what was and was not carried over.
 */

const { app, BrowserWindow, dialog, ipcMain, session, shell, Menu } = require('electron');
const path = require('node:path');
const fs = require('node:fs');

const { ControlAPI } = require('./src/api');
const { EngineManager } = require('./src/engine');
const { HistoryStore } = require('./src/history');
const { toUrl, isActive } = require('./src/format');
const settingsStore = require('./src/settings');

const APP_ORIGIN = 'https://app.uqab';
const TAB_PARTITION = 'persist:uqab-tabs';
const DEFAULT_SOCKS_PORT = 9150;
const USER_AGENT_SUFFIX = ' UQAB/1.0';

/** Locate the Python package root: bundled next to the app, or the dev checkout. */
function resolveRoot() {
  const appPath = app.getAppPath();
  const candidates = [
    process.env.UQAB_ROOT,
    // Installer layout: resources/engine next to app.asar.
    path.join(process.resourcesPath || '', 'engine'),
    path.join(appPath, 'engine'),
    appPath,
    // Development: `npm start` in app/ makes app/ the app path; the repo root
    // (app_api/, .venv/) is its parent.
    path.dirname(appPath),
    path.join(path.dirname(appPath), '..', '..'),
  ].filter(Boolean);
  for (const candidate of candidates) {
    try {
      if (fs.existsSync(path.join(candidate, 'app_api', 'server.py'))) return candidate;
    } catch {
      /* ignore */
    }
  }
  return appPath;
}

const ROOT = resolveRoot();
const RESOURCES = process.resourcesPath || path.join(ROOT, 'resources');
const USER_DATA = app.getPath('userData');

const SETTINGS_FILE = path.join(USER_DATA, 'settings.json');
const OVERRIDES_FILE = path.join(USER_DATA, 'engine-settings.json');

/** The user's choices; the setup wizard writes this, the engine reads overrides. */
let userSettings = settingsStore.load(SETTINGS_FILE);

/** Engine arguments derived from the settings; rebuilt on every restart. */
function engineArgs() {
  settingsStore.writeOverrides(OVERRIDES_FILE, userSettings);
  return ['--settings', OVERRIDES_FILE];
}

let engine = new EngineManager({
  rootDir: ROOT,
  resourcesPath: fs.existsSync(path.join(RESOURCES, 'python')) ? RESOURCES : '',
  extraArgs: engineArgs(),
  log: (line) => console.log(`[engine] ${line}`),
});

/** Restart the control API so a settings change takes effect. */
async function restartEngine() {
  await engine.stop();
  engine = new EngineManager({
    rootDir: ROOT,
    resourcesPath: fs.existsSync(path.join(RESOURCES, 'python')) ? RESOURCES : '',
    extraArgs: engineArgs(),
    log: (line) => console.log(`[engine] ${line}`),
  });
  await onTunnelDown('restarting the engine');
  const api = await engine.ensureStarted();
  const status = await api.status();
  await setTunnel(status);
  return status;
}

let historyStore = null;
let mainWindow = null;
/** Tunnel gate shared by every tab request. */
const tunnel = { active: false, socksPort: 0, state: 'stopped' };

// --------------------------------------------------------------------- proxies

/** Apply the SOCKS5 proxy to the tab session (or fall back to direct). */
async function applyProxy(socksPort) {
  const target = session.fromPartition(TAB_PARTITION);
  if (socksPort) {
    await target.setProxy({
      // Chromium hands the *host name* to a SOCKS5 proxy, so DNS never happens
      // locally (FR-26, remote DNS).
      proxyRules: `socks5://127.0.0.1:${socksPort}`,
      // Even loopback destinations go through the proxy: nothing bypasses it.
      proxyBypassRules: '<-loopback>',
    });
  } else {
    await target.setProxy({ mode: 'direct' });
  }
}

/** Open the gate: tabs may load only while the engine reports a live session. */
async function setTunnel(status) {
  const state = String((status && status.state) || 'stopped');
  const socksPort = Number((status && status.socks_port) || DEFAULT_SOCKS_PORT);
  const active = isActive(state) && state === 'connected';
  tunnel.state = state;
  tunnel.active = active;
  tunnel.socksPort = active ? socksPort : 0;
  await applyProxy(tunnel.socksPort);
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send('tunnel:state', { ...tunnel });
  }
  return { ...tunnel };
}

/** Close the gate (used when the engine stops or restarts). */
async function onTunnelDown(reason) {
  tunnel.active = false;
  tunnel.socksPort = 0;
  tunnel.state = 'stopped';
  await applyProxy(0);
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send('tunnel:state', { ...tunnel, reason });
  }
}

function wireTabSession() {
  const target = session.fromPartition(TAB_PARTITION);

  // Single hard gate: nothing leaves the machine without an active tunnel.
  target.webRequest.onBeforeRequest((details, callback) => {
    const scheme = String(details.url || '').split(':')[0].toLowerCase();
    if (scheme !== 'http' && scheme !== 'https' && scheme !== 'ws' && scheme !== 'wss') {
      callback({ cancel: true });
      return;
    }
    if (!tunnel.active) {
      callback({ cancel: true });
      return;
    }
    callback({ cancel: false });
  });

  // Deny every permission prompt: no camera, microphone, geolocation, notifications.
  target.setPermissionRequestHandler((_webContents, _permission, callback) => callback(false));
  target.setPermissionCheckHandler(() => false);

  target.setUserAgent(`${app.userAgentFallback}${USER_AGENT_SUFFIX}`, 'en-US,en');
}

// ---------------------------------------------------------------------- window

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1280,
    height: 820,
    minWidth: 940,
    minHeight: 620,
    backgroundColor: '#0e1117',
    title: 'UQAB',
    show: false,
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
      webviewTag: true,
      spellcheck: false,
    },
  });

  mainWindow.once('ready-to-show', () => mainWindow.show());
  mainWindow.loadFile(path.join(__dirname, 'renderer', 'index.html'));

  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.webContents.send('tab:open', { url });
    }
    return { action: 'deny' };
  });

  mainWindow.webContents.on('will-navigate', (event, url) => {
    // The UI itself never navigates away from the packaged page.
    if (!url.startsWith('file://')) event.preventDefault();
  });

  // Harden every <webview> as it attaches: no WebRTC outside the tunnel, no
  // new windows, no permissions, and navigation goes through our URL policy.
  mainWindow.webContents.on('did-attach-webview', (_event, contents) => {
    contents.setWindowOpenHandler(({ url }) => {
      mainWindow.webContents.send('tab:open', { url });
      return { action: 'deny' };
    });
    contents.session.setPermissionRequestHandler((_wc, _p, callback) => callback(false));
    if (typeof contents.setWebRTCIPHandlingPolicy === 'function') {
      contents.setWebRTCIPHandlingPolicy('disable_non_proxied_udp');
    }
    contents.on('will-navigate', (event, url) => {
      const scheme = String(url).split(':')[0].toLowerCase();
      if (scheme !== 'http' && scheme !== 'https') event.preventDefault();
    });
  });

  mainWindow.on('closed', () => {
    mainWindow = null;
  });
}

function buildMenu() {
  const template = [
    {
      label: 'UQAB',
      submenu: [
        { role: 'reload' },
        { role: 'toggleDevTools' },
        { type: 'separator' },
        { role: 'quit' },
      ],
    },
    {
      label: 'View',
      submenu: [{ role: 'resetZoom' }, { role: 'zoomIn' }, { role: 'zoomOut' }, { role: 'togglefullscreen' }],
    },
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

// ------------------------------------------------------------------------- IPC

function apiOf() {
  if (!engine.api) throw new Error('the engine is not running yet');
  return engine.api;
}

function wrap(handler) {
  return async (event, payload) => {
    try {
      const value = await handler(payload, event);
      return { ok: true, value };
    } catch (error) {
      return {
        ok: false,
        status: error.status || 0,
        error: error.message || String(error),
        detail: (error.body && (error.body.detail || error.body.error)) || '',
      };
    }
  };
}

function registerIpc() {
  ipcMain.handle(
    'engine:ensure',
    wrap(async () => {
      const api = await engine.ensureStarted();
      return { port: engine.port, baseUrl: api.baseUrl };
    }),
  );

  ipcMain.handle(
    'engine:shutdown',
    wrap(async () => {
      try {
        await apiOf().stop();
      } catch {
        /* the API may already be gone */
      }
      await engine.stop();
      return true;
    }),
  );

  ipcMain.handle(
    'api:request',
    wrap(async ({ method, path: route, payload }) => {
      const allowed = ['GET', 'POST', 'DELETE'];
      const verb = String(method || 'GET').toUpperCase();
      if (!allowed.includes(verb)) throw new Error(`method ${verb} is not allowed`);
      if (typeof route !== 'string' || !route.startsWith('/')) {
        throw new Error('path must start with /');
      }
      const response = await apiOf().request(verb, route, payload ?? null);
      return response.body;
    }),
  );

  ipcMain.handle(
    'session:start',
    wrap(async ({ persona }) => {
      if (typeof persona !== 'string' || !persona) throw new Error('persona is required');
      const status = await apiOf().start(persona);
      await setTunnel(status);
      return status;
    }),
  );

  ipcMain.handle(
    'session:stop',
    wrap(async () => {
      const status = await apiOf().stop();
      await setTunnel(status);
      return status;
    }),
  );

  ipcMain.handle(
    'session:status',
    wrap(async () => {
      const status = await apiOf().status();
      await setTunnel(status);
      return status;
    }),
  );

  ipcMain.handle(
    'history:add',
    wrap(async ({ url, title }) => historyStore.add(url, title || '')),
  );
  ipcMain.handle('history:list', wrap(async ({ limit } = {}) => historyStore.list(limit || 100)));
  ipcMain.handle('history:search', wrap(async ({ query } = {}) => historyStore.search(query || '')));
  ipcMain.handle('history:clear', wrap(async () => {
    historyStore.clearHistory();
    return true;
  }));
  ipcMain.handle('history:bookmarks', wrap(async () => historyStore.bookmarks()));
  ipcMain.handle(
    'history:toggleBookmark',
    wrap(async ({ url, title }) => historyStore.toggleBookmark(url, title || '')),
  );

  ipcMain.handle(
    'settings:get',
    wrap(async () => ({
      settings: userSettings,
      defaults: settingsStore.DEFAULTS,
      modes: settingsStore.MODE_LABELS,
    })),
  );

  ipcMain.handle(
    'settings:save',
    wrap(async (payload) => {
      const incoming = (payload && payload.settings) || {};
      const check = settingsStore.validate(incoming);
      if (!check.ok) throw new Error(check.errors.join(' '));
      const next = settingsStore.save(SETTINGS_FILE, { ...incoming, completed: true });
      userSettings = next;
      const status = await restartEngine();
      let tor = null;
      try {
        tor = (await engine.api.request('GET', '/tor')).body;
      } catch {
        tor = null;
      }
      return { settings: next, status, tor };
    }),
  );

  ipcMain.handle(
    'settings:detect',
    wrap(async () => {
      if (!engine.api) await engine.ensureStarted();
      const tor = (await engine.api.request('GET', '/tor')).body;
      const setup = (await engine.api.request('GET', '/setup')).body;
      return { tor, setup };
    }),
  );

  ipcMain.handle(
    'settings:pickBinary',
    wrap(async () => {
      const isWindows = process.platform === 'win32';
      const result = await dialog.showOpenDialog(mainWindow, {
        title: 'Choose your Tor binary',
        properties: ['openFile'],
        filters: isWindows
          ? [{ name: 'Tor', extensions: ['exe'] }]
          : [{ name: 'Executables', extensions: ['*'] }],
      });
      if (result.canceled || !result.filePaths.length) return '';
      return result.filePaths[0];
    }),
  );

  ipcMain.handle(
    'app:info',
    wrap(async () => ({
      version: app.getVersion(),
      electron: process.versions.electron,
      chrome: process.versions.chrome,
      platform: process.platform,
      mode: 'desktop',
      origin: APP_ORIGIN,
      userData: USER_DATA,
    })),
  );

  ipcMain.handle(
    'app:openExternal',
    wrap(async ({ url }) => {
      const target = toUrl(url);
      if (!/^https?:\/\//i.test(target)) throw new Error('only http(s) links can be opened');
      await shell.openExternal(target);
      return true;
    }),
  );

  ipcMain.handle('app:quit', wrap(async () => {
    app.quit();
    return true;
  }));
}

// ---------------------------------------------------------------------- startup

const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on('second-instance', () => {
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.focus();
    }
  });

  // WebRTC must never send UDP outside the tunnel (FR-26).
  app.commandLine.appendSwitch('force-webrtc-ip-handling-policy', 'disable_non_proxied_udp');
  app.commandLine.appendSwitch('webrtc-ip-handling-policy', 'disable_non_proxied_udp');
  app.commandLine.appendSwitch('disable-features', 'WebRtcAllowInputVolumeAdjustment');

  app.whenReady().then(async () => {
    app.setName('UQAB');
    historyStore = new HistoryStore(path.join(USER_DATA, 'history.json'));
    wireTabSession();
    await applyProxy(0);
    registerIpc();
    buildMenu();
    createWindow();

    try {
      await engine.ensureStarted();
      const status = await engine.api.status();
      await setTunnel(status);
      if (mainWindow && !mainWindow.isDestroyed()) {
        mainWindow.webContents.send('settings:loaded', { settings: userSettings });
      }
    } catch (error) {
      console.error('[engine] startup failed:', error.message);
      if (mainWindow && !mainWindow.isDestroyed()) {
        mainWindow.webContents.send('engine:failure', { message: error.message });
      }
    }

    app.on('activate', () => {
      if (BrowserWindow.getAllWindows().length === 0) createWindow();
    });
  });

  app.on('window-all-closed', async () => {
    await engine.stop();
    app.quit();
  });

  app.on('before-quit', async () => {
    await engine.stop();
  });
}

module.exports = { resolveRoot, applyProxy, setTunnel, engineArgs };
