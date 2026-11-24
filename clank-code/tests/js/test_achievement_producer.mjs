import assert from 'node:assert/strict';

// Importing Notes' model helpers in a DOM-free process must not install an
// account owner, send receipts, or require a fake document just to load them.
assert.equal(typeof document, 'undefined');
assert.equal(typeof window, 'undefined');
const headless = await import('../../static/js/achievementProducer.js');
assert.equal(headless.achievementOwner(), '');
assert.equal(await headless.flushAchievementReceipts(), false);
assert.equal(await headless.queueReceipt('unowned', {}), false);

// The guarded listeners still bind and work in the browser environment.
const globals = ['document', 'window', 'localStorage', 'fetch'];
const prior = globals.map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]);
const storage = new Map();
const requests = [];
try {
  globalThis.document = new EventTarget();
  globalThis.window = new EventTarget();
  globalThis.localStorage = {
    getItem:key => storage.get(key) ?? null,
    setItem:(key, value) => storage.set(key, value),
  };
  globalThis.fetch = async (url, options) => {
    requests.push({ url, options });
    return { ok:true, status:200 };
  };
  const browser = await import('../../static/js/achievementProducer.js?browser-listeners');
  const dispatch = (name, accountId) => document.dispatchEvent(new CustomEvent(name, { detail:{ accountId } }));
  dispatch('openclank:auth-context-changed', 'synthetic-alice');
  await browser.flushAchievementReceipts();
  assert.equal(browser.achievementOwner(), 'synthetic-alice');
  assert.equal(await browser.queueReceipt('receipt-1', { synthetic:true }), true);
  assert.equal(requests.filter(item => item.url.endsWith('/events')).length, 1);
  assert.equal(requests.at(-1).options.body, JSON.stringify({ synthetic:true }));
  storage.set('openclank:achievement-pending:synthetic-alice', '[{"id":"reset-me"}]');
  dispatch('openclank:achievements-reset', 'synthetic-alice');
  assert.equal(storage.get('openclank:achievement-pending:synthetic-alice'), '[]');
  const beforeOnline = requests.length;
  window.dispatchEvent(new Event('online'));
  await browser.flushAchievementReceipts();
  assert.ok(requests.length > beforeOnline);
  dispatch('openclank:auth-context-changed', '');
  assert.equal(browser.achievementOwner(), '');
  assert.equal(await browser.queueReceipt('late', {}, { accountId:'synthetic-alice' }), false);
} finally {
  for (const [key, descriptor] of prior) {
    if (descriptor) Object.defineProperty(globalThis, key, descriptor);
    else delete globalThis[key];
  }
}
console.log('achievement producer DOM-free import and browser listener tests passed');
