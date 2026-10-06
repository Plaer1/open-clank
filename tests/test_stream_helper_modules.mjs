import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { applyModelMetricsState, applyModelRouteEventState, inheritModelRouteState } from '../static/js/chatModelProvenance.js';
import { createTerminalStreamError, isRecoverableStreamError } from '../static/js/chatStreamErrors.js';
import { createLiveThinkingThrottle } from '../static/js/liveThinkingThrottle.js';

test('route provenance keeps round-tagged fallback and actual models on the active round', () => {
  const first = { _requestedModel: 'selected', _actualModel: 'first-actual' };
  const round = {};
  applyModelRouteEventState({ type: 'fallback', round: 2, selected_model: 'selected', answered_by: 'fallback' }, first, round, 'default');
  applyModelRouteEventState({ type: 'model_actual', round: 2, requested_model: 'selected', model: 'round-actual' }, first, round, 'default');
  assert.deepEqual(first, { _requestedModel: 'selected', _actualModel: 'first-actual' });
  assert.deepEqual(round, { _requestedModel: 'selected', _actualModel: 'round-actual' });
  const direct = {};
  assert.equal(
    applyModelRouteEventState({ type: 'fallback', selected_model: 'direct', answered_by: 'direct-fallback' }, direct, null, 'default'),
    direct,
  );
  assert.deepEqual(direct, { _requestedModel: 'direct', _actualModel: 'direct-fallback' });
  const continuation = {};
  inheritModelRouteState(first, round, continuation, 'other');
  assert.deepEqual(continuation, round);
  applyModelMetricsState({ requested_model: 'selected', model: 'final', round_models: ['round-final'] }, first, round, 'default');
  assert.deepEqual(round, { _requestedModel: 'selected', _actualModel: 'round-final' });
});

test('terminal errors preserve provider text and cannot auto-recover', () => {
  const error = createTerminalStreamError({ status: 401, error: { message: 'Repair credentials' } });
  assert.equal(error.name, 'TerminalStreamError');
  assert.equal(error.message, 'Repair credentials');
  assert.equal(error.status, 401);
  assert.equal(
    createTerminalStreamError(
      { status: 401, error: { message: 'Repair credentials' } },
      'Repair credentials Repair the endpoint credentials in Settings → Added Models.',
    ).message,
    'Repair credentials Repair the endpoint credentials in Settings → Added Models.',
  );
  assert.equal(isRecoverableStreamError(error), false);
  assert.equal(isRecoverableStreamError(new TypeError('network failed')), true);
  assert.equal(isRecoverableStreamError(new Error('HTTP 503 unavailable')), false);
});

test('thinking throttle commits only its latest pending value and cancels cleanly', () => {
  const commits = [];
  const scheduled = [];
  const throttle = createLiveThinkingThrottle((value) => commits.push(value), {
    prepare: ({ value }) => value.toUpperCase(),
    schedule(callback) { const timer = () => callback(); scheduled.push(timer); return timer; },
    cancel(timer) { const index = scheduled.indexOf(timer); if (index >= 0) scheduled.splice(index, 1); },
  });
  throttle.update({ value: 'first' });
  const firstTimer = scheduled[0];
  throttle.update({ value: 'latest' });
  assert.equal(scheduled.length, 1);
  assert.notEqual(scheduled[0], firstTimer);
  scheduled.shift()();
  assert.deepEqual(commits, ['LATEST']);
  throttle.update({ value: 'discarded' });
  throttle.cancel();
  assert.equal(throttle.flush(), false);
  assert.deepEqual(commits, ['LATEST']);
});

test('terminal SSE reader cancellation reaches the shared non-retry policy', async () => {
  const source = await readFile(new URL('../static/js/chat.js', import.meta.url), { encoding: 'utf8' });
  const terminalStart = source.indexOf('if (_nextIsError || json.status >= 400)');
  const terminalEnd = source.indexOf("if (json.delta || json.type === 'agent_prep'", terminalStart);
  const terminal = source.slice(terminalStart, terminalEnd);
  assert.match(terminal, /_streamTerminalError = createTerminalStreamError\(json, errMsg\)/);
  assert.match(terminal, /await reader\.cancel\(\)/);
  assert.match(terminal, /break streamReadLoop/);
  assert.match(source, /if \(_streamTerminalError\) \{\s*throw _streamTerminalError;/);
  assert.match(source, /isRecoverableStreamError\(err\) && _tryAutoRecover/);
  assert.doesNotMatch(source, /function _isRecoverableStreamErr/);
});

test('detached stream controllers retain and return the server run identity', async () => {
  const source = await readFile(new URL('../static/js/chat.js', import.meta.url), { encoding: 'utf8' });
  assert.match(source, /const _resumingStreams = new Map\(\)/);
  assert.match(source, /const runId = res\.headers\.get\('X-Agent-Run-ID'\) \|\| '';/);
  assert.match(source, /if \(active\) active\.runId = runId;/);
  assert.match(source, /_resumingStreams\.set\(sessionId, res\.headers\.get\('X-Agent-Run-ID'\) \|\| ''\)/);

  const stopHelperStart = source.indexOf('function _stopDetachedRun(sessionId)');
  const stopHelperEnd = source.indexOf('function _syncForegroundStreamGlobals()', stopHelperStart);
  const stopHelper = source.slice(stopHelperStart, stopHelperEnd);
  assert.match(stopHelper, /_resumingStreams\.get\(sessionId\)/);
  assert.match(stopHelper, /'X-Agent-Run-ID': runId/);
  assert.match(source, /if \(streamSessionId\) _stopDetachedRun\(streamSessionId\)/);
  assert.match(source, /if \(_sid\) \{\s*_stopDetachedRun\(_sid\);/);
});

test('service worker precaches every helper imported by the chat shell', async () => {
  const worker = await readFile(new URL('../static/sw.js', import.meta.url), { encoding: 'utf8' });
  for (const helper of ['chatModelProvenance.js', 'chatStreamErrors.js', 'liveThinkingThrottle.js']) {
    assert.match(worker, new RegExp(`/static/js/${helper.replace('.', '\\.')}`));
  }
});
