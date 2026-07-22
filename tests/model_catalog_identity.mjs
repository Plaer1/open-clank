import assert from 'node:assert/strict';
import {
  modelChoiceKey,
  resolveStoredModelChoices,
} from '../static/js/modelCatalog.js';

const first = { mid: 'shared-model', endpointId: 'endpoint-a', url: 'https://a.invalid/v1' };
const second = { mid: 'shared-model', endpointId: 'endpoint-b', url: 'https://b.invalid/v1' };
const repeated = { ...first };

assert.notEqual(modelChoiceKey(first), modelChoiceKey(second));
assert.equal(modelChoiceKey(first), modelChoiceKey(repeated));
assert.deepEqual(
  resolveStoredModelChoices(['shared-model'], [first, second]),
  [modelChoiceKey(first)],
);
assert.deepEqual(
  resolveStoredModelChoices([modelChoiceKey(second)], [first, second]),
  [modelChoiceKey(second)],
);

console.log('model catalog identity checks passed');
