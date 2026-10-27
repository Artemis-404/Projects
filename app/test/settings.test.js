'use strict';

/**
 * Settings tests: the wizard's data layer. These matter because the wizard is the
 * only way an end user (who never sees a YAML file) configures Tor.
 */

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const settings = require('../src/settings');

function tmpFile(name = 'settings.json') {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'uqab-settings-'));
  return path.join(dir, name);
}

test('defaults are complete and usable', () => {
  const defaults = settings.normalize({});
  assert.equal(defaults.mode, 'auto');
  assert.equal(defaults.socksPort, 9150);
  assert.equal(defaults.persona, 'balanced');
  assert.deepEqual(defaults.bridges, []);
  assert.equal(defaults.completed, false);
  assert.equal(settings.normalize(null).mode, 'auto');
});

test('normalize cleans hostile input', () => {
  const cleaned = settings.normalize({
    mode: 'bogus',
    socksPort: 'abc',
    bridges: '  \n # comment \n 192.0.2.1:9443 fp \n\n',
    persona: '  high  ',
    llmUrl: '   ',
    completed: 1,
  });
  assert.equal(cleaned.mode, 'auto');
  assert.equal(cleaned.socksPort, 9150);
  assert.deepEqual(cleaned.bridges, ['192.0.2.1:9443 fp']);
  assert.equal(cleaned.persona, 'high');
  assert.equal(cleaned.llmUrl, settings.DEFAULTS.llmUrl);
  assert.equal(cleaned.completed, true);
});

test('ports outside the allowed range fall back', () => {
  assert.equal(settings.normalize({ socksPort: 80 }).socksPort, 9150);
  assert.equal(settings.normalize({ socksPort: 70000 }).socksPort, 9150);
  assert.equal(settings.normalize({ socksPort: 9400.7 }).socksPort, 9400);
});

test('validate catches the mistakes the wizard can make', () => {
  const ok = settings.validate({ mode: 'auto', socksPort: 9150 });
  assert.equal(ok.ok, true);

  const noBinary = settings.validate({ mode: 'external', torBinary: '' });
  assert.equal(noBinary.ok, false);
  assert.match(noBinary.errors.join(' '), /Choose a Tor binary/);

  const missing = settings.validate({ mode: 'external', torBinary: '/definitely/not/tor' });
  assert.equal(missing.ok, false);
  assert.match(missing.errors.join(' '), /does not exist/);

  const realFile = path.join(os.tmpdir(), 'fake-tor');
  fs.writeFileSync(realFile, '');
  assert.equal(settings.validate({ mode: 'external', torBinary: realFile }).ok, true);

  assert.match(settings.validate({ socksPort: 12 }).errors.join(' '), /1024 and 65535/);
  assert.match(settings.validate({ llmUrl: 'ftp://x' }).errors.join(' '), /http/);
  assert.match(settings.validate({ persona: 'bad name!' }).errors.join(' '), /Persona names/);
});

test('overrides use the dotted keys the engine understands', () => {
  const overrides = settings.toOverrides({
    mode: 'external',
    torBinary: '/usr/bin/tor',
    socksPort: 9400,
    bridges: ['192.0.2.1:9443 abc'],
    persona: 'high',
    llmUrl: 'http://127.0.0.1:11434',
    llmModel: 'qwen2.5:1.5b',
  });
  assert.equal(overrides['tor.mode'], 'external');
  assert.equal(overrides['tor.binary'], '/usr/bin/tor');
  assert.equal(overrides['tor.socks_port'], 9400);
  assert.deepEqual(overrides['tor.bridges'], ['192.0.2.1:9443 abc']);
  assert.equal(overrides['persona.default'], 'high');
  assert.equal(overrides['llm.url'], 'http://127.0.0.1:11434');
  assert.equal(overrides['llm.model'], 'qwen2.5:1.5b');
});

test('an empty binary never overrides a working configuration', () => {
  const overrides = settings.toOverrides({ mode: 'auto', torBinary: '' });
  assert.equal('tor.binary' in overrides, false);
});

test('save and load roundtrip, and a broken file means defaults', () => {
  const file = tmpFile();
  const saved = settings.save(file, {
    mode: 'simulated',
    socksPort: 9300,
    persona: 'low',
    completed: true,
  });
  assert.equal(saved.mode, 'simulated');
  const loaded = settings.load(file);
  assert.equal(loaded.mode, 'simulated');
  assert.equal(loaded.socksPort, 9300);
  assert.equal(loaded.completed, true);
  assert.equal(fs.existsSync(`${file}.tmp`), false, 'the temp file must be renamed away');

  fs.writeFileSync(file, '{ not json');
  assert.equal(settings.load(file).mode, 'auto');
  assert.equal(settings.load(path.join(os.tmpdir(), 'does-not-exist.json')).mode, 'auto');
});

test('writeOverrides produces the file the control API reads', () => {
  const file = tmpFile('engine-settings.json');
  const written = settings.writeOverrides(file, { mode: 'simulated', socksPort: 9150 });
  assert.equal(written.path, file);
  const parsed = JSON.parse(fs.readFileSync(file, 'utf8'));
  assert.equal(parsed['tor.mode'], 'simulated');
  assert.equal(parsed['tor.socks_port'], 9150);
  assert.deepEqual(parsed['tor.bridges'], []);
  assert.equal(parsed['persona.default'], 'balanced');
});

test('describe explains the mode in plain words', () => {
  assert.match(settings.describe({ mode: 'simulated' }), /Lab simulation/);
  assert.match(settings.describe({ mode: 'auto' }, { found: true, version: 'Tor 0.4.8' }), /using Tor/);
  assert.match(settings.describe({ mode: 'auto' }, { found: false }), /lab simulation/);
  assert.match(settings.describe({ mode: 'external' }, { found: false }), /not found yet/);
});

test('splitBridges accepts strings, arrays and comments', () => {
  assert.deepEqual(settings.splitBridges('a\n# b\n\nc '), ['a', 'c']);
  assert.deepEqual(settings.splitBridges(['x', '', 'y']), ['x', 'y']);
  assert.deepEqual(settings.splitBridges(undefined), []);
});
