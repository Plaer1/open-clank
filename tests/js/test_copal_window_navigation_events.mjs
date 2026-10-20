import assert from 'node:assert/strict';

const listeners = new Map();
globalThis.document = {
  body: {},
  documentElement: {},
  addEventListener(type, handler) { listeners.set(type, handler); },
};
const {
  activateInputContext,
  getActiveInputContext,
  registerInputContext,
  resetInputContextsForTests,
  setInputContextLifecycle,
  setInputContextNavigation,
} = await import('../../static/js/copal/inputContext.js?event-test');

const root = (id) => ({ id, nodeType: 1, hidden: false, style: { display: '' }, classList: { contains: () => false }, contains(node) { return node === this; } });
const ownerRoot = root('owner');
const target = { owner: ownerRoot, nodeType: 1 };
let calls = 0;
const navigation = { back() { calls += 1; }, forward() { calls += 10; }, canGoBack: () => true, canGoForward: () => true };
resetInputContextsForTests();
registerInputContext(ownerRoot, { windowId: 'owner', paneId: 'body', navigation });
activateInputContext(ownerRoot);
const ordinaryRoot = root('ordinary-tool');
const ordinaryTarget = { owner: ordinaryRoot, nodeType: 1 };
registerInputContext(ordinaryRoot, { windowId: 'ordinary', paneId: 'body' });

const fire = (type, button, targetValue = target, timeStamp = 10) => {
  const event = { type, button, target: targetValue, timeStamp, prevented: false, preventDefault() { this.prevented = true; }, stopPropagation() { this.stopped = true; }, composedPath: () => [targetValue, targetValue.owner] };
  listeners.get(type)(event);
  return event;
};
const pointer = fire('pointerdown', 3);
assert.equal(calls, 1);
assert.equal(pointer.prevented, true);
const compatibility = fire('auxclick', 3, target, 11);
assert.equal(calls, 1, 'pointer/auxclick sequence executes once');
assert.equal(compatibility.prevented, true);
const next = fire('pointerdown', 4, target, 2000);
assert.equal(calls, 11);
assert.equal(next.prevented, true);

// A visible ordinary tool owns its event path even when it has no local
// navigation adapter. Files must not steal the side button.
activateInputContext(ordinaryRoot);
const ordinaryEvent = fire('pointerdown', 3, ordinaryTarget, 3000);
assert.equal(calls, 11, 'ordinary tool target does not invoke stale Files navigation');
assert.equal(ordinaryEvent.prevented, false, 'ordinary tool leaves unsupported navigation to its own/browser policy');

activateInputContext(ownerRoot);
const bodyEvent = fire('pointerdown', 3, document.body, 4000);
assert.equal(calls, 12, 'body target uses the active eligible owner');
assert.equal(bodyEvent.prevented, true);

ownerRoot.hidden = true;
const outside = fire('pointerdown', 3, ordinaryTarget, 6000);
assert.equal(calls, 12, 'hidden owner cannot steal navigation');
assert.equal(outside.prevented, false);

// A registered pane inherits its window lifecycle and navigation adapter.
ownerRoot.hidden = false;
const pane = root('pane');
pane.parentNode = ownerRoot;
const firstAdapter = { back() { calls += 100; }, forward() {}, canGoBack: () => true, canGoForward: () => true };
const secondAdapter = { back() { calls += 1000; }, forward() {}, canGoBack: () => true, canGoForward: () => true };
setInputContextNavigation(ownerRoot, firstAdapter);
registerInputContext(pane, { windowId: 'owner', paneId: 'pane', navigation: firstAdapter });
activateInputContext(pane);
setInputContextNavigation(ownerRoot, secondAdapter);
fire('pointerdown', 3, { owner: pane, nodeType: 1 }, 7000);
assert.equal(calls, 1012, 'navigation adapter swaps propagate to registered panes');
setInputContextLifecycle(ownerRoot, { minimized: true });
assert.equal(getActiveInputContext().windowId, 'ordinary', 'minimizing a window excludes its registered pane');
const minimizedBody = fire('pointerdown', 3, document.body, 8000);
assert.equal(calls, 1012, 'body fallback does not reach a pane under a minimized owner');

let rejectedErrors = 0;
const rejectedAdapter = {
  back: () => Promise.reject(new Error('expected navigation failure')),
  forward() {}, canGoBack: () => true, canGoForward: () => true,
  onError() { rejectedErrors += 1; },
};
setInputContextLifecycle(ownerRoot, { minimized: false });
setInputContextNavigation(ownerRoot, rejectedAdapter);
activateInputContext(ownerRoot);
fire('pointerdown', 3, target, 9000);
await Promise.resolve();
await Promise.resolve();
assert.equal(rejectedErrors, 1, 'rejected adapter promises are caught');
console.log('copal auxiliary navigation events: ok');
