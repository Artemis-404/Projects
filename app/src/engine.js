'use strict';

/**
 * Owns the Python control-API process that drives the UQAB engine.
 *
 * Packaged builds ship a self-contained CPython under `resources/python`, so the
 * app finds its interpreter through `locatePython`. In development the system
 * interpreter is used. Nothing here imports Electron, which keeps the module
 * unit-testable with a stubbed `spawn`.
 */

const net = require('node:net');
const path = require('node:path');
const fs = require('node:fs');

const DEFAULT_PORT = 9170;
const READY_TIMEOUT_MS = 30000;

/** Split a command line like `"C:\\Program Files\\py\\python.exe" -u` into argv. */
function splitCommand(command) {
  const parts = [];
  let current = '';
  let quote = null;
  for (const char of String(command)) {
    if (quote) {
      if (char === quote) quote = null;
      else current += char;
      continue;
    }
    if (char === '"' || char === "'") {
      quote = char;
      continue;
    }
    if (char === ' ') {
      if (current) {
        parts.push(current);
        current = '';
      }
      continue;
    }
    current += char;
  }
  if (current) parts.push(current);
  return parts;
}

/**
 * Find a Python interpreter.
 * Order: UQAB_PYTHON env var, bundled runtime, then the platform default.
 * @returns {{command: string, args: string[], source: string}}
 */
function locatePython({ resourcesPath = '', rootDir = process.cwd(), env = process.env, platform = process.platform } = {}) {
  if (env.UQAB_PYTHON) {
    const parts = splitCommand(env.UQAB_PYTHON);
    return { command: parts[0], args: parts.slice(1), source: 'UQAB_PYTHON' };
  }
  const candidates = [];
  if (resourcesPath) {
    if (platform === 'win32') {
      candidates.push(path.join(resourcesPath, 'python', 'python.exe'));
      candidates.push(path.join(resourcesPath, 'python', 'pythonw.exe'));
    } else {
      candidates.push(path.join(resourcesPath, 'python', 'bin', 'python3'));
    }
  }
  if (platform === 'win32') {
    candidates.push(path.join(rootDir, '.venv', 'Scripts', 'python.exe'));
  } else {
    candidates.push(path.join(rootDir, '.venv', 'bin', 'python3'));
  }
  for (const candidate of candidates) {
    if (fs.existsSync(candidate)) {
      return { command: candidate, args: [], source: candidate };
    }
  }
  const fallback = platform === 'win32' ? 'python' : 'python3';
  return { command: fallback, args: [], source: 'PATH' };
}

/** Ask the OS for a free TCP port on loopback. */
function freePort(host = '127.0.0.1') {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.unref();
    server.on('error', reject);
    server.listen(0, host, () => {
      const { port } = server.address();
      server.close(() => resolve(port));
    });
  });
}

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

class EngineManager {
  /**
   * @param {object} options
   * @param {string} options.rootDir      repository root (contains app_api/)
   * @param {string} [options.resourcesPath]
   * @param {Function} [options.spawn]    injected child_process.spawn
   * @param {Function} [options.makeApi]  injected ControlAPI factory
   * @param {string[]} [options.extraArgs]
   * @param {Function} [options.log]
   */
  constructor({
    rootDir,
    resourcesPath = '',
    spawn = require('node:child_process').spawn,
    makeApi,
    extraArgs = [],
    log = () => {},
    platform = process.platform,
    env = process.env,
  }) {
    this.rootDir = rootDir;
    this.resourcesPath = resourcesPath;
    this.spawn = spawn;
    this.makeApi = makeApi;
    this.extraArgs = extraArgs;
    this.log = log;
    this.platform = platform;
    this.env = env;
    this.child = null;
    this.api = null;
    this.starting = null;
    this.port = null;
    this.lastError = '';
    this.stdout = [];
    this.stderr = [];
  }

  /** True when a control-API process is running. */
  get running() {
    return Boolean(this.child && this.child.exitCode === null);
  }

  /**
   * Start the control API and wait until `/health` answers.
   * Idempotent: returns the existing client when it is already up.
   */
  async ensureStarted(options = {}) {
    // Concurrent callers (main-process startup and the renderer's first IPC)
    // share one start-up instead of racing a half-ready API.
    if (this.starting) return this.starting;
    if (this.api && this.running) return this.api;
    this.starting = this.start(options).finally(() => {
      this.starting = null;
    });
    return this.starting;
  }

  async start({ port = 0, waitMs = READY_TIMEOUT_MS } = {}) {
    const python = locatePython({
      resourcesPath: this.resourcesPath,
      rootDir: this.rootDir,
      env: this.env,
      platform: this.platform,
    });
    const chosen = port || (await freePort());
    const args = [
      ...python.args,
      '-u',
      '-m',
      'app_api.server',
      '--host',
      '127.0.0.1',
      '--port',
      String(chosen),
      ...this.extraArgs,
    ];
    this.log(`starting control API: ${python.command} ${args.join(' ')} (${python.source})`);
    const child = this.spawn(python.command, args, {
      cwd: this.rootDir,
      env: { ...this.env, PYTHONUNBUFFERED: '1', PYTHONPATH: this.rootDir },
      windowsHide: true,
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    this.child = child;
    this.port = chosen;
    const collect = (buffer, line) => {
      const text = String(line).trimEnd();
      if (!text) return;
      buffer.push(text);
      if (buffer.length > 200) buffer.shift();
      this.log(text);
    };
    if (child.stdout) child.stdout.on('data', (chunk) => String(chunk).split('\n').forEach((line) => collect(this.stdout, line)));
    if (child.stderr) child.stderr.on('data', (chunk) => String(chunk).split('\n').forEach((line) => collect(this.stderr, line)));
    child.on('exit', (code) => {
      this.log(`control API exited with code ${code}`);
      this.child = null;
      this.api = null;
    });

    this.api = this.makeApi
      ? this.makeApi({ host: '127.0.0.1', port: chosen })
      : new (require('./api').ControlAPI)({ host: '127.0.0.1', port: chosen });

    const deadline = Date.now() + waitMs;
    while (Date.now() < deadline) {
      if (!this.running && !this.makeApi) {
        this.lastError = this.stderr.slice(-3).join(' | ') || 'the control API exited early';
        throw new Error(this.lastError);
      }
      try {
        await this.api.health();
        this.lastError = '';
        return this.api;
      } catch (error) {
        this.lastError = error.message;
        await delay(200);
      }
    }
    await this.stop();
    throw new Error(`the control API did not become ready: ${this.lastError}`);
  }

  /** Stop the control API (and therefore any session it is running). */
  async stop() {
    const child = this.child;
    this.child = null;
    this.api = null;
    if (!child || child.exitCode !== null) return;
    try {
      child.kill();
    } catch {
      /* already gone */
    }
    await new Promise((resolve) => {
      const timer = setTimeout(resolve, 3000);
      child.once('exit', () => {
        clearTimeout(timer);
        resolve();
      });
    });
  }
}

module.exports = { EngineManager, locatePython, splitCommand, freePort, DEFAULT_PORT };
