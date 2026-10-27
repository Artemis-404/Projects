'use strict';

/**
 * The only bridge between the sandboxed renderer and the main process.
 *
 * The renderer has no Node access, so every privileged action goes through this
 * narrow, explicit surface: control-API calls, history, and a few app helpers.
 * Results are always `{ok, value}` / `{ok:false, error}` envelopes so the UI never
 * has to guess whether an IPC call threw.
 */

const { contextBridge, ipcRenderer } = require('electron');

const invoke = (channel, payload) => ipcRenderer.invoke(channel, payload);

contextBridge.exposeInMainWorld('uqab', {
  api: {
    /** Raw control-API call: {method, path, payload}. */
    request: (method, path, payload = null) => invoke('api:request', { method, path, payload }),
    status: () => invoke('api:request', { method: 'GET', path: '/status' }),
    counters: () => invoke('api:request', { method: 'GET', path: '/counters' }),
    personas: () => invoke('api:request', { method: 'GET', path: '/personas' }),
    consent: () => invoke('api:request', { method: 'GET', path: '/consent' }),
    acceptConsent: (textVersion) =>
      invoke('api:request', {
        method: 'POST',
        path: '/consent',
        payload: { accept: true, text_version: textVersion },
      }),
    withdrawConsent: () => invoke('api:request', { method: 'DELETE', path: '/consent' }),
    mode: () => invoke('api:request', { method: 'GET', path: '/mode' }),
  },
  session: {
    start: (persona) => invoke('session:start', { persona }),
    stop: () => invoke('session:stop'),
    status: () => invoke('session:status'),
  },
  engine: {
    ensure: () => invoke('engine:ensure'),
    shutdown: () => invoke('engine:shutdown'),
  },
  history: {
    add: (url, title) => invoke('history:add', { url, title }),
    list: (limit) => invoke('history:list', { limit }),
    search: (query) => invoke('history:search', { query }),
    clear: () => invoke('history:clear'),
    bookmarks: () => invoke('history:bookmarks'),
    toggleBookmark: (url, title) => invoke('history:toggleBookmark', { url, title }),
  },
  app: {
    info: () => invoke('app:info'),
    openExternal: (url) => invoke('app:openExternal', { url }),
    quit: () => invoke('app:quit'),
  },
  /** Main-process push events. Each returns an unsubscribe function. */
  on: (channel, listener) => {
    const allowed = new Set(['tunnel:state', 'tab:open', 'engine:failure']);
    if (!allowed.has(channel)) return () => {};
    const wrapped = (_event, payload) => listener(payload);
    ipcRenderer.on(channel, wrapped);
    return () => ipcRenderer.removeListener(channel, wrapped);
  },
});
