'use strict';

/**
 * User settings for the desktop app, and the bridge to engine configuration.
 *
 * The end user never edits YAML: the setup wizard writes this object, and
 * `toOverrides` turns it into the dotted configuration overrides the control API
 * accepts (`--settings <file>`). Everything is validated here first, so a typo in
 * the wizard cannot stop the engine from starting.
 */

const fs = require('node:fs');
const path = require('node:path');

const MODES = ['auto', 'external', 'simulated'];

const DEFAULTS = Object.freeze({
  version: 1,
  mode: 'auto',
  torBinary: '',
  socksPort: 9150,
  bridges: [],
  persona: 'balanced',
  llmUrl: 'http://127.0.0.1:11434',
  llmModel: 'qwen2.5:1.5b',
  startWithWindows: false,
  completed: false,
});

const MODE_LABELS = {
  auto: 'Automatic - use Tor when it is installed, otherwise the built-in lab',
  external: 'Always use the Tor binary I choose',
  simulated: 'Always use the built-in lab simulation',
};

function coercePort(value, fallback) {
  const port = Number(value);
  if (!Number.isFinite(port)) return fallback;
  const rounded = Math.trunc(port);
  if (rounded < 1024 || rounded > 65535) return fallback;
  return rounded;
}

function splitBridges(value) {
  const list = Array.isArray(value) ? value : String(value || '').split(/\r?\n/);
  return list
    .map((entry) => String(entry).trim())
    .filter((entry) => entry.length > 0 && !entry.startsWith('#'));
}

/** Clean any incoming object into a complete, valid settings object. */
function normalize(raw = {}) {
  const source = raw && typeof raw === 'object' ? raw : {};
  const mode = MODES.includes(source.mode) ? source.mode : DEFAULTS.mode;
  return {
    version: 1,
    mode,
    torBinary: String(source.torBinary || '').trim(),
    socksPort: coercePort(source.socksPort, DEFAULTS.socksPort),
    bridges: splitBridges(source.bridges),
    persona: String(source.persona || DEFAULTS.persona).trim() || DEFAULTS.persona,
    llmUrl: String(source.llmUrl || DEFAULTS.llmUrl).trim() || DEFAULTS.llmUrl,
    llmModel: String(source.llmModel || DEFAULTS.llmModel).trim() || DEFAULTS.llmModel,
    startWithWindows: Boolean(source.startWithWindows),
    completed: Boolean(source.completed),
  };
}

/**
 * Check a settings object.
 * @returns {{ok: boolean, errors: string[]}}
 */
function validate(raw) {
  const settings = normalize(raw);
  const errors = [];
  if (settings.mode === 'external' && !settings.torBinary) {
    errors.push('Choose a Tor binary, or let UQAB start the lab simulation instead.');
  }
  if (settings.torBinary && !fs.existsSync(settings.torBinary)) {
    errors.push(`The Tor binary does not exist: ${settings.torBinary}`);
  }
  if (Number(raw && raw.socksPort) !== settings.socksPort && raw && raw.socksPort !== undefined) {
    errors.push('The SOCKS port must be between 1024 and 65535.');
  }
  if (!/^https?:\/\//i.test(settings.llmUrl)) {
    errors.push('The model endpoint must start with http:// or https://');
  }
  if (!/^[A-Za-z0-9_-]+$/.test(settings.persona)) {
    errors.push('Persona names may only contain letters, digits, "-" and "_".');
  }
  return { ok: errors.length === 0, errors };
}

/** Dotted configuration overrides for the engine (`--settings` file). */
function toOverrides(raw) {
  const settings = normalize(raw);
  const overrides = {
    'tor.mode': settings.mode,
    'tor.socks_port': settings.socksPort,
    'tor.bridges': settings.bridges,
    'persona.default': settings.persona,
    'llm.url': settings.llmUrl,
    'llm.model': settings.llmModel,
    'pt.log_slots': true,
  };
  if (settings.torBinary) overrides['tor.binary'] = settings.torBinary;
  return overrides;
}

function load(filePath) {
  try {
    const parsed = JSON.parse(fs.readFileSync(filePath, 'utf8'));
    return normalize(parsed);
  } catch {
    return { ...DEFAULTS, bridges: [] };
  }
}

function save(filePath, raw) {
  const settings = normalize(raw);
  const directory = path.dirname(filePath);
  fs.mkdirSync(directory, { recursive: true });
  const tmp = `${filePath}.tmp`;
  fs.writeFileSync(tmp, `${JSON.stringify(settings, null, 2)}\n`, 'utf8');
  fs.renameSync(tmp, filePath);
  return settings;
}

/** Write the override file the control API reads; returns its path. */
function writeOverrides(filePath, raw) {
  const overrides = toOverrides(raw);
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  const tmp = `${filePath}.tmp`;
  fs.writeFileSync(tmp, `${JSON.stringify(overrides, null, 2)}\n`, 'utf8');
  fs.renameSync(tmp, filePath);
  return { path: filePath, overrides };
}

/** Human sentence for the wizard's summary line. */
function describe(settings, tor = null) {
  const mode = normalize(settings).mode;
  if (mode === 'simulated') return 'Lab simulation (no Tor needed)';
  if (mode === 'external') {
    const ready = tor && tor.found ? ` (${tor.version || 'Tor'})` : ' (not found yet)';
    return `Tor binary${ready}`;
  }
  if (tor && tor.found) return `Automatic - using Tor${tor.version ? ` (${tor.version})` : ''}`;
  return 'Automatic - Tor not found, so the lab simulation will be used';
}

module.exports = {
  DEFAULTS,
  MODES,
  MODE_LABELS,
  normalize,
  validate,
  toOverrides,
  load,
  save,
  writeOverrides,
  describe,
  splitBridges,
};
