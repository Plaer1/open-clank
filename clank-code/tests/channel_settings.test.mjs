import test from 'node:test';
import assert from 'node:assert/strict';
import { configBody, routeChoice, routeLabels, CHANNEL_FIELDS } from '../static/js/channelSettings.js';

test('route is sent only as a complete pair', () => {
  assert.deepEqual(routeChoice('ep1', 'm1'), { endpoint_id: 'ep1', model: 'm1' });
  assert.deepEqual(routeChoice('ep1', ''), {});
  assert.deepEqual(routeChoice('', 'm1'), {});
  assert.deepEqual(routeChoice(undefined, undefined), {});
});

test('configBody carries the chosen route and never leaves a half pair', () => {
  assert.deepEqual(configBody('telegram', true, { endpoint_id: 'ep1', model: 'm1' }).settings, { endpoint_id: 'ep1', model: 'm1' });
  assert.deepEqual(configBody('matrix', true, { endpoint_id: 'ep1' }).settings, {});
});

test('no channel lists the route as text fields', () => {
  for (const spec of Object.values(CHANNEL_FIELDS)) {
    assert.ok(!spec.settings.some(([k]) => k === 'endpoint_id' || k === 'model'));
  }
});

test('route selects have a name per channel and distinct empty choices', () => {
  assert.deepEqual(routeLabels('telegram'), { connection: 'Telegram connection', model: 'Telegram model', noConnection: 'Choose a connection', noModel: 'Choose a model' });
  for (const name of Object.keys(CHANNEL_FIELDS)) {
    const l = routeLabels(name);
    assert.notEqual(l.connection, l.model);
    assert.ok(l.connection.endsWith(' connection') && l.model.endsWith(' model') && l.connection.split(' ')[0] === l.model.split(' ')[0]);
  }
});
