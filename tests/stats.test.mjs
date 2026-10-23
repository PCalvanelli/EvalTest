// static/js/stats.js: pass-rate intervals and the paired comparison. Run: node --test tests/
import assert from 'node:assert/strict';
import {test} from 'node:test';

import {pairedCompare, passRateCI, quantile} from '../static/js/stats.js';

const prompts = (n, right) => Array.from({length: n}, (_, i) => [i < right ? 1 : 0]);

test('an all-pass setup has a point interval', () => {
  assert.deepEqual(passRateCI(Array.from({length: 22}, () => [1, 1, 1])), {rate: [1, 1], passK: [1, 1]});
});

test('no scored prompt gives no interval', () => {
  assert.equal(passRateCI([[], []]), null);
});

test('22 prompts give a wide interval around the observed rate', () => {
  const [lo, hi] = passRateCI(prompts(22, 15)).rate;
  assert.ok(lo < 15 / 22 && 15 / 22 < hi);
  assert.ok(hi - lo > 0.25 && hi - lo < 0.5);
});

test('a setup against itself shows no difference', () => {
  const a = prompts(22, 15);
  const c = pairedCompare(a, a);
  assert.equal(c.diff, 0);
  assert.equal(c.p, 1);
  assert.equal(c.clear, false);
});

test('seven extra prompts right is a clear difference, with the exact McNemar p', () => {
  const c = pairedCompare(prompts(22, 15), prompts(22, 8));
  assert.equal(c.aOnly, 7);
  assert.equal(c.bOnly, 0);
  assert.ok(Math.abs(c.p - 2 / 2 ** 7) < 1e-12);
  assert.ok(c.clear && c.ci[0] > 0);
});

test('swapping A and B mirrors the interval', () => {
  const ab = pairedCompare(prompts(22, 15), prompts(22, 8)).ci;
  const ba = pairedCompare(prompts(22, 8), prompts(22, 15)).ci;
  assert.deepEqual(ba.map((x) => +x.toFixed(9)), ab.map((x) => +(-x).toFixed(9)).reverse());
});

test('a one-prompt edge is not clear', () => {
  assert.equal(pairedCompare(prompts(22, 9), prompts(22, 8)).clear, false);
});

test('prompts only one setup scored are left out of the pairing', () => {
  assert.equal(pairedCompare([[1], []], [[1], [0]]).n, 1);
});

test('quantile interpolates', () => {
  assert.equal(quantile([1, 2, 3, 4], 0.5), 2.5);
});
