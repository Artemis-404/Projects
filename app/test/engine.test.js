'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const { EngineManager, locatePython, splitCommand, freePort } = require('../src/engine');

/** A stand-in child process with the pipes EngineManager expects. */
function fakeChild() {
  const child = new EventEmitter();
  child.exitCode = null;
  child.stdout = new EventEmitter();
  child.stderr = new EventEmitter();
  child.killed = false;
  child.kill = () => {
    child.killed = true;
    child.exitCode = 0;
    child.emit('exit', 0);
  };
  return child;
}

function tempRoot() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'uqab-engine-'));
  fs.mkdirSync(path.join(dir, 'app_api'), { recursive: true });
  fs.writeFileSync(path.join(dir, 'app_api', 'server.py'), '# stub\n');
  return dir;
}

test('splitCommand respects quoting', () => {
  assert.deepEqual(splitCommand('python3 -u'), ['python3', '-u']);
  assert.deepEqual(splitCommand('"C:\\Program Files\\Py\\python.exe" -u'), [
    'C:\\Program Files\\Py\\python.exe',
    '-u',
  ]);
});

test('locatePython prefers UQAB_PYTHON', () => {
  const found = locatePython({ env: { UQAB_PYTHON: '/opt/py/bin/python3 -u' } });
  assert.equal(found.command, '/opt/py/bin/python3');
  assert.deepEqual(found.args, ['-u']);
  assert.equal(found.source, 'UQAB_PYTHON');
});

test('locatePython finds a bundled Windows runtime', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'uqab-py-'));
  const resources = path.join(dir, 'resources');
  fs.mkdirSync(path.join(resources, 'python'), { recursive: true });
  const exe = path.join(resources, 'python', 'python.exe');
  fs.writeFileSync(exe, '');
  const found = locatePython({ resourcesPath: resources, platform: 'win32', env: {} });
  assert.equal(found.command, exe);
  assert.equal(found.source, exe);
});

test('locatePython falls back to the PATH name', () => {
  const found = locatePython({ platform: 'win32', rootDir: path.join(os.tmpdir(), 'nope'), env: {} });
  assert.equal(found.command, 'python');
  assert.equal(found.source, 'PATH');
});

test('freePort returns a usable port', async () => {
  const port = await freePort();
  assert.ok(port > 0 && port < 65536);
});

test('EngineManager starts the API and waits for health', async () => {
  const root = tempRoot();
  const calls = [];
  let spawnArgs = null;
  const ready = { ok: true };
  const makeApi = ({ port }) => ({
    baseUrl: `http://127.0.0.1:${port}`,
    health: async () => {
      calls.push(port);
      return ready;
    },
    status: async () => ({ state: 'stopped' }),
  });
  const manager = new EngineManager({
    rootDir: root,
    makeApi,
    spawn: (command, args) => {
      spawnArgs = { command, args };
      return fakeChild();
    },
    env: {},
    log: () => {},
  });
  const api = await manager.ensureStarted({ waitMs: 2000 });
  assert.ok(api);
  assert.equal(manager.running, true);
  assert.ok(calls.length >= 1);
  assert.equal(spawnArgs.args.slice(0, 3).join(' '), '-u -m app_api.server');
  const portFlag = spawnArgs.args[spawnArgs.args.indexOf('--port') + 1];
  assert.equal(Number(portFlag), manager.port);
  await manager.stop();
  assert.equal(manager.running, false);
});

test('EngineManager reports a crash with the stderr tail', async () => {
  const root = tempRoot();
  const manager = new EngineManager({
    rootDir: root,
    makeApi: ({ port }) => ({
      baseUrl: `http://127.0.0.1:${port}`,
      health: async () => {
        throw new Error('ECONNREFUSED');
      },
    }),
    spawn: () => {
      const child = fakeChild();
      setImmediate(() => {
        child.stderr.emit('data', 'Traceback: no module named app_api\n');
        child.exitCode = 1;
        child.emit('exit', 1);
      });
      return child;
    },
    env: {},
    log: () => {},
  });
  await assert.rejects(manager.ensureStarted({ waitMs: 800 }), /control API/);
  assert.equal(manager.running, false);
});

test('EngineManager is idempotent while running', async () => {
  const root = tempRoot();
  let spawned = 0;
  const manager = new EngineManager({
    rootDir: root,
    makeApi: ({ port }) => ({ baseUrl: `http://127.0.0.1:${port}`, health: async () => ({ ok: true }) }),
    spawn: () => {
      spawned += 1;
      return fakeChild();
    },
    env: {},
    log: () => {},
  });
  await manager.ensureStarted({ waitMs: 1000 });
  await manager.ensureStarted({ waitMs: 1000 });
  assert.equal(spawned, 1);
  await manager.stop();
});

test('EngineManager shares one start-up between concurrent callers', async () => {
  const root = tempRoot();
  let spawned = 0;
  let healthy = false;
  setTimeout(() => {
    healthy = true;
  }, 300);
  const manager = new EngineManager({
    rootDir: root,
    makeApi: ({ port }) => ({
      baseUrl: `http://127.0.0.1:${port}`,
      health: async () => {
        if (!healthy) throw new Error('not ready');
        return { ok: true };
      },
    }),
    spawn: () => {
      spawned += 1;
      return fakeChild();
    },
    env: {},
    log: () => {},
  });
  const [first, second] = await Promise.all([
    manager.ensureStarted({ waitMs: 2000 }),
    manager.ensureStarted({ waitMs: 2000 }),
  ]);
  assert.equal(spawned, 1);
  assert.equal(first, second);
  assert.ok(healthy, 'the second caller must not get a half-ready API');
  await manager.stop();
});
