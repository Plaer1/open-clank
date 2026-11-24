import test from 'node:test';
import assert from 'node:assert/strict';
import { colourFor, BODY_COLOURS, chooseChats, chatUnits, sceneUnits, placeUnits, unitState, MAX_CLANKS, liveLine } from '../static/js/copal/clankshopModel.js';
import { sceneSvg, unitsSvg } from '../static/js/copal/clankshopArt.js';

const chat = (id, extra = {}) => ({ id, name: `Chat ${id}`, updated_at: `2026-11-${String(10 + Number(id.slice(1)) % 18).padStart(2, '0')}`, ...extra });
const actor = (id, status, outcome, label) => ({ sourceRef: { kind: 'native_actor', sessionId: 's', nativeId: id }, nativeStatus: status, ...(outcome ? { nativeOutcome: outcome } : {}), title: label || id, ...(label ? { actorDisplay: { label } } : {}) });
const observation = (...items) => ({ sources: { native_actor: { items } } });

test('colour is stable per id and comes from the palette', () => {
  assert.equal(colourFor('c1/main'), colourFor('c1/main'));
  for (const id of ['a', 'b', 'c1/main', 'c2/helper', '']) assert.ok(BODY_COLOURS.includes(colourFor(id)));
  assert.ok(new Set(Array.from({ length: 40 }, (_, i) => colourFor(`id-${i}`))).size > 1);
});

test('chats: private, archived, assistant and nameless-incognito chats never appear; favourites first; cap of 8', () => {
  const rows = [chat('c1'), chat('c2', { archived: true }), chat('c3', { incognito: true }), chat('c4', { folder: 'Assistant' }), chat('c5', { name: 'Incognito' }),
    chat('c6'), chat('c7', { is_important: true }), chat('c8'), chat('c9'), chat('c10'), chat('c11'), chat('c12'), chat('c13'), chat('c14'), chat('c15'), { id: '' }, null];
  const { shown, more } = chooseChats(rows, { incognitoIds: ['c8'] });
  const ids = shown.map((c) => c.id);
  assert.equal(shown.length, MAX_CLANKS);
  for (const hidden of ['c2', 'c3', 'c4', 'c5', 'c8']) assert.ok(!ids.includes(hidden), hidden);
  assert.equal(ids[0], 'c7');
  assert.equal(shown.length + more, 10);
});

test('state mapping from the observation and companion shapes', () => {
  assert.equal(unitState({ status: 'running' }), 'working');
  assert.equal(unitState({ status: 'pending' }), 'starting');
  assert.equal(unitState({ status: 'idle' }), 'idle');
  assert.equal(unitState({ status: 'idle', outcome: 'success' }), 'done');
  assert.equal(unitState({ status: 'idle', outcome: 'failure' }), 'failed');
  assert.equal(unitState({ status: 'running', companionState: 'error' }), 'failed');
  assert.equal(unitState({ status: 'running', question: true }), 'question');
  assert.equal(unitState({ status: 'running', question: true, permission: true }), 'permission');
  assert.equal(unitState({}), 'unavailable');
});

test('live units are named by the actor display name; a failed read or no data is one resting clank carrying the chat title', () => {
  const c = chat('c1', { name: 'My chat' });
  const units = chatUnits(c, { actors: observation(actor('main', 'running', null, 'Chief Clank'), actor('a2', 'idle', 'success', 'Rivet')),
    companion: { actors: [{ sourceRef: { nativeId: 'a2' }, state: 'idle' }], requests: { items: [{ observedState: 'inputneeded', actorRef: { nativeId: 'main' } }] } } });
  assert.deepEqual(units.map((u) => [u.name, u.state]), [['Chief Clank', 'question'], ['Rivet', 'done']]);
  const off = chatUnits(c, {});
  assert.deepEqual(off.map((u) => [u.name, u.state, u.live]), [['My chat', 'unavailable', false]]);
  assert.equal(chatUnits(c, { permission: { request_id: 'r' } })[0].state, 'permission');
  assert.equal(chatUnits(c, { actors: observation(actor('main', 'idle')), permission: { request_id: 'r', actor_id: 'main' } })[0].state, 'permission');
});

test('scene cap: at most 8 drawn, those needing the owner first, the rest counted; every unit stays in the list', () => {
  const units = Array.from({ length: 11 }, (_, i) => ({ id: `u${i}`, name: `U${i}`, state: i === 10 ? 'permission' : i === 9 ? 'failed' : 'idle', colour: '#F6BE48', chatId: 'c', chatTitle: 'c' }));
  const { drawn, extra, all } = sceneUnits(units);
  assert.equal(drawn.length, 8); assert.equal(extra, 3); assert.equal(all.length, 11);
  assert.deepEqual(drawn.slice(0, 2).map((u) => u.id), ['u10', 'u9']);
  const placed = placeUnits(drawn);
  assert.equal(placed.find((u) => u.id === 'u10').room, 'lamp');
  assert.equal(placed.find((u) => u.id === 'u9').room, 'fault');
  assert.deepEqual(placed.filter((u) => u.room === 'cot').map((u) => u.slot), [0, 1, 2, 3, 4, 5]);
});

test('scene text: names are escaped and the off-state line says where to turn live states on', () => {
  const svg = unitsSvg(placeUnits([{ id: 'x', name: '<img onerror=1>"', state: 'working', colour: '#FF776E' }]));
  assert.ok(!svg.includes('<img') && svg.includes('&lt;img'));
  assert.ok(sceneSvg([], { extra: 3 }).includes('+3 more'));
  assert.match(liveLine({ enabled: false, loaded: false, failures: 0 }), /switch beside Refresh/);
});
