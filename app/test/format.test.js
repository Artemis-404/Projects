'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');

const fmt = require('../src/format');

test('formatBytes uses binary units', () => {
  assert.equal(fmt.formatBytes(0), '0 B');
  assert.equal(fmt.formatBytes(999), '999 B');
  assert.equal(fmt.formatBytes(1024), '1.0 KiB');
  assert.equal(fmt.formatBytes(1536), '1.5 KiB');
  assert.equal(fmt.formatBytes(1024 * 1024 * 3.25), '3.3 MiB');
  assert.equal(fmt.formatBytes(undefined), '0 B');
});

test('splitBytes separates value and unit', () => {
  assert.deepEqual(fmt.splitBytes(2048), { value: '2.0', unit: 'KiB' });
  assert.deepEqual(fmt.splitBytes(12), { value: '12', unit: 'B' });
});

test('formatDuration prints mm:ss and h:mm:ss', () => {
  assert.equal(fmt.formatDuration(0), '0:00');
  assert.equal(fmt.formatDuration(65), '1:05');
  assert.equal(fmt.formatDuration(3725), '1:02:05');
  assert.equal(fmt.formatDuration(-4), '0:00');
});

test('stateInfo covers the four engine states', () => {
  assert.equal(fmt.stateInfo('connected').label, 'Protected');
  assert.equal(fmt.stateInfo('connected').tone, 'good');
  assert.equal(fmt.stateInfo('blocked').tone, 'bad');
  assert.equal(fmt.stateInfo('connecting').label, 'Connecting');
  assert.equal(fmt.stateInfo('stopped').label, 'Disconnected');
  assert.equal(fmt.stateInfo('nonsense').tone, 'idle');
});

test('isActive is true only while a session runs', () => {
  assert.equal(fmt.isActive('connecting'), true);
  assert.equal(fmt.isActive('connected'), true);
  assert.equal(fmt.isActive('stopped'), false);
  assert.equal(fmt.isActive('blocked'), false);
});

test('modeLabel never calls the lab Tor', () => {
  assert.equal(fmt.modeLabel('tor'), 'Tor');
  assert.equal(fmt.modeLabel('simulated'), 'Lab simulation');
  assert.equal(fmt.modeLabel(''), '');
});

test('formatOverhead', () => {
  assert.equal(fmt.formatOverhead(2.456), '2.46x');
  assert.equal(fmt.formatOverhead(0), '-');
});

test('toUrl keeps http(s), searches everything else', () => {
  assert.equal(fmt.toUrl('https://example.com/x'), 'https://example.com/x');
  assert.equal(fmt.toUrl('http://demo.uqab/docs'), 'http://demo.uqab/docs');
  assert.equal(fmt.toUrl('example.com'), 'https://example.com');
  assert.equal(fmt.toUrl('how to bake bread'), `https://duckduckgo.com/?q=how%20to%20bake%20bread`);
  assert.match(fmt.toUrl('javascript:alert(1)'), /^https:\/\/duckduckgo\.com/);
  assert.equal(fmt.toUrl('   '), '');
});

test('originOf and shorten', () => {
  assert.equal(fmt.originOf('https://demo.uqab/docs'), 'https://demo.uqab');
  assert.equal(fmt.originOf('not a url'), '');
  assert.equal(fmt.shorten('a short title'), 'a short title');
  assert.equal(fmt.shorten('x'.repeat(40), 10).length, 10);
});
