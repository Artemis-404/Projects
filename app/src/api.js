'use strict';

/**
 * Minimal client for the UQAB control API (SDD 5.7).
 *
 * Every request carries the Origin header the API expects, so the app is the only
 * caller that can drive the engine. The module uses `http` directly (no fetch
 * polyfills, no dependencies) and is injectable for tests.
 */

const http = require('node:http');

class ApiError extends Error {
  constructor(message, { status = 0, body = null } = {}) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.body = body;
  }

  /** True when the failure is a timeout rather than a rejection. */
  get timedOut() {
    return this.status === 504;
  }
}

class ControlAPI {
  /**
   * @param {{host?: string, port: number, origin: string, timeoutMs?: number}} options
   */
  constructor({ host = '127.0.0.1', port, origin = 'https://app.uqab', timeoutMs = 20000 }) {
    this.host = host;
    this.port = port;
    this.origin = origin;
    this.timeoutMs = timeoutMs;
  }

  get baseUrl() {
    return `http://${this.host}:${this.port}`;
  }

  /**
   * Perform one request against the control API.
   * @returns {Promise<{status: number, body: any}>}
   */
  request(method, path, payload = null, { timeoutMs = this.timeoutMs } = {}) {
    const body = payload === null ? null : Buffer.from(JSON.stringify(payload), 'utf8');
    const headers = { Origin: this.origin, Accept: 'application/json' };
    if (body) {
      headers['Content-Type'] = 'application/json';
      headers['Content-Length'] = String(body.length);
    }
    return new Promise((resolve, reject) => {
      const req = http.request(
        { host: this.host, port: this.port, path, method, headers, timeout: timeoutMs },
        (res) => {
          const chunks = [];
          res.on('data', (chunk) => chunks.push(chunk));
          res.on('end', () => {
            const text = Buffer.concat(chunks).toString('utf8');
            let parsed = null;
            try {
              parsed = text ? JSON.parse(text) : null;
            } catch {
              parsed = { raw: text };
            }
            const status = res.statusCode || 0;
            if (status >= 400) {
              const detail = (parsed && (parsed.detail || parsed.error)) || `HTTP ${status}`;
              reject(new ApiError(String(detail), { status, body: parsed }));
              return;
            }
            resolve({ status, body: parsed });
          });
        },
      );
      req.on('timeout', () => {
        req.destroy(new ApiError('the engine did not answer in time', { status: 504 }));
      });
      req.on('error', (error) => {
        reject(error instanceof ApiError ? error : new ApiError(error.message, { status: 0 }));
      });
      if (body) req.write(body);
      req.end();
    });
  }

  /** GET /health — cheap liveness probe used while the API starts. */
  async health() {
    return (await this.request('GET', '/health', null, { timeoutMs: 2000 })).body;
  }

  /** GET /status — engine state, counters, consent state and mode label. */
  async status() {
    return (await this.request('GET', '/status')).body;
  }

  /** GET /personas — selectable persona ids. */
  async personas() {
    const body = (await this.request('GET', '/personas')).body || {};
    return Array.isArray(body.personas) ? body.personas : [];
  }

  /** POST /start with the chosen persona; resolves with the new status. */
  async start(persona) {
    return (await this.request('POST', '/start', { persona })).body;
  }

  /** POST /stop. */
  async stop() {
    return (await this.request('POST', '/stop', {})).body;
  }

  /** GET /counters — the 1 Hz panel data. */
  async counters() {
    return (await this.request('GET', '/counters')).body;
  }

  /** GET /consent — text, version and the current opt-in state. */
  async consent() {
    return (await this.request('GET', '/consent')).body;
  }

  /** POST /consent — record the opt-in and its text version. */
  async acceptConsent(textVersion) {
    return (await this.request('POST', '/consent', { accept: true, text_version: textVersion }))
      .body;
  }

  /** DELETE /consent — withdraw and delete the learned profile. */
  async withdrawConsent() {
    return (await this.request('DELETE', '/consent')).body;
  }

  /** GET /mode — configured tor.mode (auto | external | simulated). */
  async mode() {
    return (await this.request('GET', '/mode')).body;
  }
}

module.exports = { ControlAPI, ApiError };
