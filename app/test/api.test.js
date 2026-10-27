'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');

const { ControlAPI, ApiError } = require('../src/api');

const ORIGIN = 'https://app.uqab';

/** Start a throwaway control-API stand-in; `routes` maps "METHOD /path". */
function stubApi(routes) {
  const requests = [];
  const server = http.createServer((req, res) => {
    const chunks = [];
    req.on('data', (chunk) => chunks.push(chunk));
    req.on('end', () => {
      const raw = Buffer.concat(chunks).toString('utf8');
      const body = raw ? JSON.parse(raw) : null;
      requests.push({ method: req.method, url: req.url, origin: req.headers.origin, body });
      const key = `${req.method} ${req.url}`;
      const handler = routes[key];
      if (!handler) {
        res.writeHead(404, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: 'not_found' }));
        return;
      }
      const result = handler(req, body) || {};
      if (result.hang) return; // deliberately never answer
      const { status = 200, payload = {} } = result;
      res.writeHead(status, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify(payload));
    });
  });
  return new Promise((resolve) => {
    server.listen(0, '127.0.0.1', () => {
      const { port } = server.address();
      resolve({ server, port, requests });
    });
  });
}

test('requests carry the app origin and parse JSON', async () => {
  const { server, port, requests } = await stubApi({
    'GET /health': () => ({ payload: { ok: true, version: '1.0' } }),
  });
  try {
    const api = new ControlAPI({ port, origin: ORIGIN });
    const health = await api.health();
    assert.equal(health.ok, true);
    assert.equal(requests[0].origin, ORIGIN);
    assert.equal(api.baseUrl, `http://127.0.0.1:${port}`);
  } finally {
    server.close();
  }
});

test('start posts the persona and returns the status', async () => {
  const { server, port, requests } = await stubApi({
    'POST /start': (_req, body) => ({ status: 202, payload: { state: 'connecting', persona: body.persona } }),
  });
  try {
    const api = new ControlAPI({ port, origin: ORIGIN });
    const status = await api.start('high');
    assert.equal(status.state, 'connecting');
    assert.deepEqual(requests[0].body, { persona: 'high' });
  } finally {
    server.close();
  }
});

test('error responses become ApiError with the server detail', async () => {
  const { server, port } = await stubApi({
    'POST /start': () => ({ status: 409, payload: { error: 'busy', detail: 'engine is connecting' } }),
  });
  try {
    const api = new ControlAPI({ port, origin: ORIGIN });
    await assert.rejects(api.start('high'), (error) => {
      assert.ok(error instanceof ApiError);
      assert.equal(error.status, 409);
      assert.match(error.message, /engine is connecting/);
      return true;
    });
  } finally {
    server.close();
  }
});

test('timeouts surface as a 504 ApiError', async () => {
  const { server, port } = await stubApi({
    'GET /status': () => ({ hang: true }),
  });
  try {
    const api = new ControlAPI({ port, origin: ORIGIN, timeoutMs: 200 });
    await assert.rejects(api.status(), (error) => {
      assert.equal(error.status, 504);
      assert.equal(error.timedOut, true);
      return true;
    });
  } finally {
    server.close();
  }
});

test('personas unwraps the list and tolerates a bad body', async () => {
  const { server, port } = await stubApi({
    'GET /personas': () => ({ payload: { personas: ['balanced', 'high'] } }),
  });
  try {
    const api = new ControlAPI({ port, origin: ORIGIN });
    assert.deepEqual(await api.personas(), ['balanced', 'high']);
  } finally {
    server.close();
  }
});

test('consent helpers hit the right verbs', async () => {
  const { server, port, requests } = await stubApi({
    'GET /consent': () => ({ payload: { opted_in: false, text: '...', current_text_version: '1.1' } }),
    'POST /consent': () => ({ payload: { opted_in: true } }),
    'DELETE /consent': () => ({ payload: { opted_in: false, profile_deleted: true } }),
  });
  try {
    const api = new ControlAPI({ port, origin: ORIGIN });
    assert.equal((await api.consent()).opted_in, false);
    assert.equal((await api.acceptConsent('1.1')).opted_in, true);
    assert.equal((await api.withdrawConsent()).profile_deleted, true);
    assert.deepEqual(
      requests.map((item) => `${item.method} ${item.url}`),
      ['GET /consent', 'POST /consent', 'DELETE /consent'],
    );
    assert.deepEqual(requests[1].body, { accept: true, text_version: '1.1' });
  } finally {
    server.close();
  }
});

test('counters and mode helpers', async () => {
  const { server, port } = await stubApi({
    'GET /counters': () => ({ payload: { real_down: 10, cover_down: 20, overhead: 2 } }),
    'GET /mode': () => ({ payload: { mode: 'auto' } }),
  });
  try {
    const api = new ControlAPI({ port, origin: ORIGIN });
    assert.equal((await api.counters()).overhead, 2);
    assert.equal((await api.mode()).mode, 'auto');
  } finally {
    server.close();
  }
});

test('a closed port raises a connection error', async () => {
  const api = new ControlAPI({ port: 1, origin: ORIGIN, timeoutMs: 500 });
  await assert.rejects(api.status(), (error) => error instanceof ApiError || error.code !== undefined);
});
