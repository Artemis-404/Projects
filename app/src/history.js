'use strict';

/**
 * Local browsing history and bookmarks.
 *
 * The store is a single JSON file inside the app's user-data directory. Nothing
 * is ever sent anywhere: this is the app's own record of what the user opened, in
 * the spirit of FR-26 (no network calls that Tor does not carry). Writes are
 * atomic (write-then-rename) and the file is capped so it cannot grow forever.
 */

const fs = require('node:fs');
const path = require('node:path');

const MAX_ENTRIES = 5000;
const MAX_TITLE = 200;

function emptyState() {
  return { version: 1, entries: [], bookmarks: [] };
}

class HistoryStore {
  /** @param {string} filePath absolute path of the JSON store */
  constructor(filePath, { maxEntries = MAX_ENTRIES } = {}) {
    this.filePath = filePath;
    this.maxEntries = maxEntries;
    this.state = this.load();
  }

  load() {
    try {
      const raw = fs.readFileSync(this.filePath, 'utf8');
      const parsed = JSON.parse(raw);
      if (!parsed || typeof parsed !== 'object') return emptyState();
      return {
        version: 1,
        entries: Array.isArray(parsed.entries) ? parsed.entries : [],
        bookmarks: Array.isArray(parsed.bookmarks) ? parsed.bookmarks : [],
      };
    } catch {
      return emptyState();
    }
  }

  /** Atomically persist the current state. */
  save() {
    const directory = path.dirname(this.filePath);
    fs.mkdirSync(directory, { recursive: true });
    const tmp = `${this.filePath}.tmp`;
    fs.writeFileSync(tmp, JSON.stringify(this.state), 'utf8');
    fs.renameSync(tmp, this.filePath);
  }

  /**
   * Record a visit. Re-visiting the same URL in a row does not duplicate the row;
   * instead the visit count and timestamp are updated.
   */
  add(url, title = '', { at = Date.now() } = {}) {
    const value = String(url || '');
    if (!/^https?:/i.test(value)) return null;
    const clean = String(title || '').slice(0, MAX_TITLE);
    const head = this.state.entries[0];
    if (head && head.url === value && at - head.visited <= 2000) {
      head.visits = (head.visits || 1) + 1;
      head.visited = at;
      if (clean) head.title = clean;
      this.save();
      return head;
    }
    const entry = { url: value, title: clean, visited: at, visits: 1 };
    this.state.entries.unshift(entry);
    if (this.state.entries.length > this.maxEntries) {
      this.state.entries.length = this.maxEntries;
    }
    this.save();
    return entry;
  }

  /** Most recent entries, newest first. */
  list(limit = 100) {
    return this.state.entries.slice(0, Math.max(0, limit));
  }

  /** Case-insensitive search over URL and title. */
  search(query, limit = 20) {
    const needle = String(query || '').trim().toLowerCase();
    if (!needle) return this.list(limit);
    return this.state.entries
      .filter(
        (entry) =>
          entry.url.toLowerCase().includes(needle) ||
          String(entry.title || '').toLowerCase().includes(needle),
      )
      .slice(0, limit);
  }

  /** Toggle a bookmark and return whether the URL is now bookmarked. */
  toggleBookmark(url, title = '') {
    const value = String(url || '');
    const index = this.state.bookmarks.findIndex((item) => item.url === value);
    if (index >= 0) {
      this.state.bookmarks.splice(index, 1);
      this.save();
      return false;
    }
    this.state.bookmarks.push({ url: value, title: String(title || '').slice(0, MAX_TITLE), added: Date.now() });
    this.save();
    return true;
  }

  bookmarks() {
    return this.state.bookmarks.slice();
  }

  isBookmarked(url) {
    return this.state.bookmarks.some((item) => item.url === url);
  }

  /** Remove history entries but keep bookmarks. */
  clearHistory() {
    this.state.entries = [];
    this.save();
  }

  /** Remove everything (used by "withdraw clearnet learning" and by tests). */
  clearAll() {
    this.state = emptyState();
    this.save();
  }
}

module.exports = { HistoryStore, MAX_ENTRIES };
