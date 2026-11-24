import assert from 'node:assert/strict';
import {
  bindModelStateOwner,
  catalogHasModelChoice,
  modelChoiceKey,
  modelStateKey,
  resolveStoredModelChoices,
} from '../static/js/modelCatalog.js';

const first = { mid: 'shared-model', endpointId: 'endpoint-a', url: 'https://a.invalid/v1' };
const second = { mid: 'shared-model', endpointId: 'endpoint-b', url: 'https://b.invalid/v1' };
const repeated = { ...first };

assert.notEqual(modelChoiceKey(first), modelChoiceKey(second));
assert.equal(modelChoiceKey(first), modelChoiceKey(repeated));
assert.deepEqual(
  resolveStoredModelChoices(['shared-model'], [first, second]),
  [],
);
assert.deepEqual(resolveStoredModelChoices(['shared-model'], [first]), [modelChoiceKey(first)]);
assert.deepEqual(resolveStoredModelChoices([modelChoiceKey(first)], [first, repeated]), [modelChoiceKey(first)]);
assert.deepEqual(resolveStoredModelChoices([modelChoiceKey(first)], [first, {...first, providerModelId:'different'}]), []);
assert.deepEqual(
  resolveStoredModelChoices([modelChoiceKey(second)], [first, second]),
  [modelChoiceKey(second)],
);
assert.deepEqual(
  resolveStoredModelChoices(['endpoint:gone:model', 'gone-model'], [first, second]),
  [],
);

const sharedRoute = {
  endpoint_id: 'shared:grant',
  url: 'mimo://acp',
  models: ['xiaomi/mimo-v2.5-pro/high'],
};
const personalRoute = {
  endpoint_id: 'mimo:xiaomi',
  url: 'mimo://acp',
  models: ['xiaomi/mimo-v2.5-pro/high'],
};
assert.equal(
  catalogHasModelChoice(
    [personalRoute],
    'xiaomi/mimo-v2.5-pro/high',
    'shared:grant',
    'mimo://acp',
  ),
  false,
  'a personal route cannot impersonate a removed shared route with the same model and URL',
);
assert.equal(
  catalogHasModelChoice(
    [sharedRoute, personalRoute],
    'xiaomi/mimo-v2.5-pro/high',
    'shared:grant',
    'mimo://acp',
  ),
  true,
);
assert.equal(
  catalogHasModelChoice(
    [personalRoute],
    'xiaomi/mimo-v2.5-pro/high',
    'mimo:auto',
    'mimo://acp',
  ),
  true,
  'the automatic MiMo selector resolves through its provider-scoped catalogue row',
);
assert.equal(
  catalogHasModelChoice(
    [sharedRoute],
    'xiaomi/mimo-v2.5-pro/high',
    'mimo:auto',
    'mimo://acp',
  ),
  false,
  'the automatic MiMo selector cannot silently adopt a shared route',
);

const state = new Map([['openclank-model-favorites', '["shared-model"]']]);
globalThis.localStorage = {
  getItem: key => state.has(key) ? state.get(key) : null,
  setItem: (key, value) => state.set(key, String(value)),
  removeItem: key => state.delete(key),
};
bindModelStateOwner('Alice', 'alice');
assert.equal(state.has(modelStateKey('openclank-model-favorites', 'alice')), false);
assert.equal(state.get('openclank-model-favorites'), '["shared-model"]');
bindModelStateOwner('Bob', 'alice');
assert.notEqual(
  modelStateKey('openclank-model-favorites', 'alice'),
  modelStateKey('openclank-model-favorites', 'bob'),
);
assert.equal(state.has(modelStateKey('openclank-model-favorites', 'bob')), false);

console.log('model catalog identity checks passed');
