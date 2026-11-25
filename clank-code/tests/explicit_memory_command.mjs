import assert from 'node:assert/strict';
import { test } from 'node:test';
import { explicitMemoryCommandText, saveExplicitMemory, memoryAddMessage } from '../static/js/explicitMemory.js';

test('explicit chat commands preserve the typed fact and share the guarded Add receipt', async () => {
  const text = 'Design  A\nuses <green>';
  for (const prefix of ['/remember ', '/memory add ', '/m echo ']) {
    assert.equal(explicitMemoryCommandText(prefix + text), text);
  }
  assert.equal(explicitMemoryCommandText('remember what I said?'), '');
  globalThis.document = {body: {dataset: {accountId: 'first'}}};
  const requests = [];
  globalThis.fetch = async (url, init) => {
    assert.equal(url, '/api/memory/add');
    assert.equal(init.credentials, 'same-origin');
    requests.push(JSON.parse(init.body));
    if (requests.length === 1) throw new Error('response lost');
    return {ok: true, json: async () => ({state: 'applied'})};
  };
  await saveExplicitMemory(text, 'fact', () => false);
  assert.equal(requests.length, 0);
  await assert.rejects(saveExplicitMemory(text, 'fact', () => true), /response lost/);
  assert.equal(memoryAddMessage(await saveExplicitMemory(text, 'fact', () => true)), 'Memory added');
  assert.equal(requests.length, 2);
  assert.ok(requests[0].request_key);
  assert.deepEqual(requests[1], {...requests[0], action: 'status'});
  assert.equal(requests[0].text, text);
  assert.equal(requests[0].action, 'submit');
});

test('confirmed absence permits another explicit submit using the original key', async () => {
  const actions = [];
  globalThis.document = {body: {dataset: {accountId: 'second'}}};
  globalThis.fetch = async (_url, init) => {
    actions.push(JSON.parse(init.body));
    if (actions.length === 1) throw new Error('connection failed');
    return {ok: true, json: async () => ({state: actions.length === 2 ? 'absent' : 'staged'})};
  };
  const save = () => saveExplicitMemory('Review this', 'fact', () => true);
  await assert.rejects(save(), /connection failed/);
  await assert.rejects(save(), /Retry/);
  assert.equal(memoryAddMessage(await save()), 'Sent to Candidates for review');
  assert.deepEqual(actions.map(item => item.action), ['submit', 'status', 'submit']);
  assert.equal(new Set(actions.map(item => item.request_key)).size, 1);
});
