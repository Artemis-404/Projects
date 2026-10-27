'use strict';

/**
 * Pure formatting helpers shared by the main process, the renderer and the tests.
 * No Electron or Node APIs are used here.
 */

/** Human-readable byte count using binary units. */
function formatBytes(value) {
  const bytes = Number(value) || 0;
  if (bytes < 1024) return `${bytes} B`;
  const units = ['KiB', 'MiB', 'GiB', 'TiB'];
  let size = bytes / 1024;
  let index = 0;
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024;
    index += 1;
  }
  return `${size.toFixed(size >= 100 ? 0 : 1)} ${units[index]}`;
}

/** Byte counter split into a compact value and its unit (for the panel). */
function splitBytes(value) {
  const text = formatBytes(value);
  const space = text.indexOf(' ');
  return { value: text.slice(0, space), unit: text.slice(space + 1) };
}

/** Seconds as mm:ss, or h:mm:ss past an hour. */
function formatDuration(seconds) {
  const total = Math.max(0, Math.floor(Number(seconds) || 0));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  const pad = (n) => String(n).padStart(2, '0');
  return hours > 0 ? `${hours}:${pad(minutes)}:${pad(secs)}` : `${minutes}:${pad(secs)}`;
}

/** Latency-like millisecond value. */
function formatMs(value) {
  const ms = Number(value) || 0;
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)} s` : `${Math.round(ms)} ms`;
}

const STATES = {
  stopped: { label: 'Disconnected', tone: 'idle' },
  connecting: { label: 'Connecting', tone: 'pending' },
  connected: { label: 'Protected', tone: 'good' },
  blocked: { label: 'Blocked', tone: 'bad' },
};

/** Map an engine state to its UI label and colour tone. */
function stateInfo(state) {
  return STATES[state] || { label: 'Unknown', tone: 'idle' };
}

/** True when the session is in a state where only Stop makes sense. */
function isActive(state) {
  return state === 'connecting' || state === 'connected';
}

/** Tor vs lab label; never claims Tor when the engine is simulating. */
function modeLabel(mode) {
  if (mode === 'tor') return 'Tor';
  if (mode === 'simulated' || mode === 'lab') return 'Lab simulation';
  return '';
}

/** Overhead as "2.4x". */
function formatOverhead(value) {
  const ratio = Number(value) || 0;
  return ratio > 0 ? `${ratio.toFixed(2)}x` : '-';
}

const SEARCH_URL = 'https://duckduckgo.com/?q=';

/**
 * Turn whatever the user typed into a navigation URL.
 * Refuses script-bearing schemes so a pasted `javascript:` cannot run.
 */
function toUrl(input) {
  const text = String(input || '').trim();
  if (!text) return '';
  if (/^[a-zA-Z][a-zA-Z0-9+.-]*:/.test(text)) {
    const scheme = text.slice(0, text.indexOf(':')).toLowerCase();
    if (scheme === 'http' || scheme === 'https') return text;
    if (scheme === 'about' && text === 'about:blank') return text;
    return `${SEARCH_URL}${encodeURIComponent(text)}`;
  }
  if (text.includes(' ') || !text.includes('.')) {
    return `${SEARCH_URL}${encodeURIComponent(text)}`;
  }
  return `https://${text}`;
}

/** The origin of a URL, or an empty string when it cannot be parsed. */
function originOf(url) {
  try {
    return new URL(String(url)).origin;
  } catch {
    return '';
  }
}

/** Trim a title to something that fits a tab. */
function shorten(text, limit = 28) {
  const value = String(text || '').replace(/\s+/g, ' ').trim();
  return value.length > limit ? `${value.slice(0, limit - 1)}\u2026` : value;
}

module.exports = {
  formatBytes,
  splitBytes,
  formatDuration,
  formatMs,
  stateInfo,
  isActive,
  modeLabel,
  formatOverhead,
  toUrl,
  originOf,
  shorten,
  SEARCH_URL,
};
