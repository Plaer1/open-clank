import assert from 'node:assert/strict';
import {
  providerDisplayName,
  sharedProviderLabel,
  sharedSecondaryLabel,
} from '../static/js/modelLabels.js';

// Provider identity comes from the catalog projection, never from a model ID.
assert.equal(providerDisplayName('OpenAI'), 'OpenAI');
assert.equal(providerDisplayName(''), 'Unknown provider');
assert.equal(providerDisplayName('Shared provider'), 'Unknown provider');
assert.equal(
  sharedProviderLabel('Anthropic'),
  'Shared Anthropic',
);
assert.equal(
  sharedProviderLabel(undefined, 'Fournisseur inconnu'),
  'Shared Fournisseur inconnu',
);
assert.equal(
  sharedSecondaryLabel({ label: 'Research pool', owner: 'alice' }),
  'Research pool · Shared by alice',
);
assert.equal(
  sharedSecondaryLabel({ owner: 'alice' }),
  'Shared by alice',
);

console.log('shared provider label checks passed');
