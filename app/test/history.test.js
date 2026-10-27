'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const { HistoryStore, MAX_ENTRIES } = require('../src/history');

function storePath() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'uqab-history-'));
  return path.join(dir, 'nested', 'history.json');
}

test('adds entries newest first and persists them', () => {
  const file = storePath();
  const store = new HistoryStore(file);
  store.add('https://demo.uqab/', 'Demo home', { at: 1000 });
  store.add('https://demo.uqab/docs', 'Docs', { at: 2000 });
  assert.equal(store.list().length, 2);
  assert.equal(store.list()[0].url, 'https://demo.uqab/docs');

  const reopened = new HistoryStore(file);
  assert.equal(reopened.list().length, 2);
  assert.equal(reopened.list()[1].title, 'Demo home');
});

test('non-http urls are refused', () => {
  const store = new HistoryStore(storePath());
  assert.equal(store.add('javascript:alert(1)'), null);
  assert.equal(store.add('about:blank'), null);
  assert.equal(store.list().length, 0);
});

test('repeat visits within two seconds collapse', () => {
  const store = new HistoryStore(storePath());
  store.add('https://demo.uqab/', 'Demo', { at: 1000 });
  store.add('https://demo.uqab/', 'Demo again', { at: 1500 });
  assert.equal(store.list().length, 1);
  assert.equal(store.list()[0].visits, 2);
  assert.equal(store.list()[0].title, 'Demo again');
  store.add('https://demo.uqab/', 'Later', { at: 9000 });
  assert.equal(store.list().length, 2);
});

test('search matches url and title', () => {
  const store = new HistoryStore(storePath());
  store.add('https://demo.uqab/docs', 'Documentation', { at: 1 });
  store.add('https://demo.uqab/news', 'Headlines', { at: 2 });
  assert.equal(store.search('doc').length, 1);
  assert.equal(store.search('HEAD').length, 1);
  assert.equal(store.search('demo.uqab').length, 2);
  assert.equal(store.search('').length, 2);
});

test('history is capped', () => {
  const store = new HistoryStore(storePath(), { maxEntries: 3 });
  for (let i = 0; i < 10; i += 1) store.add(`https://demo.uqab/${i}`, `p${i}`, { at: i * 5000 });
  assert.equal(store.list().length, 3);
  assert.equal(store.list()[0].url, 'https://demo.uqab/9');
  assert.ok(MAX_ENTRIES >= 3);
});

test('bookmarks toggle and survive clearing history', () => {
  const file = storePath();
  const store = new HistoryStore(file);
  store.add('https://demo.uqab/', 'Demo', { at: 1 });
  assert.equal(store.toggleBookmark('https://demo.uqab/', 'Demo'), true);
  assert.equal(store.isBookmarked('https://demo.uqab/'), true);
  store.clearHistory();
  assert.equal(store.list().length, 0);
  assert.equal(store.bookmarks().length, 1);
  assert.equal(new HistoryStore(file).bookmarks()[0].url, 'https://demo.uqab/');
  assert.equal(store.toggleBookmark('https://demo.uqab/', 'Demo'), false);
  assert.equal(store.bookmarks().length, 0);
});

test('a corrupt file is treated as empty', () => {
  const file = storePath();
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, '{not json');
  const store = new HistoryStore(file);
  assert.equal(store.list().length, 0);
  store.add('https://demo.uqab/', 'Demo', { at: 1 });
  assert.equal(new HistoryStore(file).list().length, 1);
});

test('clearAll removes history and bookmarks', () => {
  const store = new HistoryStore(storePath());
  store.add('https://demo.uqab/', 'Demo', { at: 1 });
  store.toggleBookmark('https://demo.uqab/', 'Demo');
  store.clearAll();
  assert.equal(store.list().length, 0);
  assert.equal(store.bookmarks().length, 0);
});
