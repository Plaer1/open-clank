import assert from 'node:assert/strict';
import { test } from 'node:test';
import { providerAuthMethodsForBillingLane } from '../static/js/providerControl.js';

const subscriptionKeyFamilies = [
  'xiaomi',
  'opencode-go',
  'kimi-code-plan-cn',
  'kimi-code-plan-global',
  'llmgateway-devpass',
  'zai-coding-plan',
];

test('subscription API-key visibility follows explicit method billing lanes', () => {
  for (const id of subscriptionKeyFamilies) {
    const methods = providerAuthMethodsForBillingLane({
      id,
      kinds: ['subscription'],
      auth_methods: [{
        id: 'api_key',
        type: 'api',
        credential_kinds: ['api'],
        billing_lanes: ['subscription'],
      }],
    }, 'subscription');
    assert.equal(methods.length, 1, `${id} plan key should be offered`);
  }

  for (const id of ['openai', 'anthropic', 'google']) {
    const methods = providerAuthMethodsForBillingLane({
      id,
      kinds: ['official', 'subscription'],
      auth_methods: [{
        id: 'api_key',
        type: 'api',
        credential_kinds: ['api'],
        billing_lanes: ['metered_api', 'custom'],
      }],
    }, 'subscription');
    assert.deepEqual(methods, [], `${id} metered key must not appear as a subscription method`);
  }

  assert.deepEqual(providerAuthMethodsForBillingLane({
    id: 'legacy-unknown',
    kinds: ['subscription'],
    auth_methods: [{ id: 'api_key', type: 'api', credential_kinds: ['api'] }],
  }, 'subscription'), [], 'subscription use requires an explicit API-key billing-lane declaration');
});

test('OAuth methods stay in their declared lane while manual keys retain metered and custom lanes', () => {
  const family = {
    id: 'openai',
    kinds: ['official', 'subscription', 'custom_gateway'],
    auth_methods: [
      {
        id: 'codex-login',
        type: 'oauth',
        credential_kinds: ['oauth'],
        billing_lanes: ['subscription'],
      },
      {
        id: 'api_key',
        type: 'api',
        credential_kinds: ['api'],
        billing_lanes: ['metered_api', 'custom'],
      },
    ],
  };
  assert.deepEqual(
    providerAuthMethodsForBillingLane(family, 'subscription').map(method => method.id),
    ['codex-login'],
  );
  assert.deepEqual(
    providerAuthMethodsForBillingLane(family, 'metered_api').map(method => method.id),
    ['api_key'],
  );
  assert.deepEqual(
    providerAuthMethodsForBillingLane(family, 'custom').map(method => method.id),
    ['api_key'],
  );
  assert.deepEqual(providerAuthMethodsForBillingLane({
    id: 'anthropic',
    kinds: ['official', 'subscription'],
    auth_methods: [{
      id: 'claude-login',
      type: 'oauth',
      credential_kinds: ['oauth'],
      billing_lanes: ['subscription'],
    }],
  }, 'metered_api'), []);
});
