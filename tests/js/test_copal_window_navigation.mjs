import assert from 'node:assert/strict';
import { createWindowNavigation } from '../../static/js/copal/navigation.js';
import {
  activateInputContext,
  disposeInputContextTree,
  getInputContextEntry,
  getInputContextStats,
  getActiveInputContext,
  registerInputContext,
  resetInputContextsForTests,
  resolveInputContext,
  setInputContextLifecycle,
  unregisterInputContext,
} from '../../static/js/copal/inputContext.js';

const root = (name = 'section') => {
  const element = {
    nodeName: name,
    hidden: false,
    style: { display: '' },
    classList: { contains: () => false },
    contains(node) { return node === this || node?.owner === this; },
  };
  return element;
};

const makeEvent = (target) => ({ target, composedPath: () => [target, target.owner] });

let restored = [];
const history = createWindowNavigation({
  scope: { account: 'a', workspace: 'w' },
  restore: async (entry) => {
    restored.push(entry.location);
    return entry.location !== 'denied';
  },
});
assert.equal(history.commit({ location: 'A', body: 'large body', scope: { account: 'a', workspace: 'w' } }), true);
assert.equal(history.commit({ location: 'B', presentation: { scrollTop: 20 }, scope: { account: 'a', workspace: 'w' } }), true);
assert.equal(history.commit({ location: 'C', scope: { account: 'a', workspace: 'w' } }), true);
assert.deepEqual(history.snapshot().entries[0], { location: 'A', scope: { account: 'a', workspace: 'w' } }, 'history omits document bodies');
assert.equal(history.canGoBack(), true);
assert.equal(await history.back(), true);
assert.equal(history.snapshot().cursor, 1);
assert.equal(await history.forward(), true);
assert.equal(history.snapshot().cursor, 2);

// A failed asynchronous restoration keeps the cursor in place.
const failed = createWindowNavigation({
  scope: { account: 'a', workspace: 'w' },
  restore: async () => false,
});
failed.commit({ location: 'A' });
failed.commit({ location: 'denied' });
assert.equal(await failed.back(), false);
assert.equal(failed.snapshot().cursor, 1);
const rejected = createWindowNavigation({
  scope: { account: 'a', workspace: 'w' },
  restore: async () => { throw new Error('load failed'); },
});
rejected.commit({ location: 'A' });
rejected.commit({ location: 'B' });
assert.equal(await rejected.back(), false, 'rejected restores are handled');
assert.equal(rejected.snapshot().cursor, 1, 'rejected restores do not move the cursor');

const cyclic = { location: 'cycle', body: 'omit', long: 'x'.repeat(5000) };
cyclic.self = cyclic;
cyclic.deep = { a: { b: { c: { d: { e: { f: { g: { h: { i: 'too deep' } } } } } } } } };
const captured = history.capture(cyclic);
assert.equal(captured.body, undefined);
assert.equal(captured.self, '[Circular]');
assert.equal(captured.long.length, 2048, 'history strings are bounded');
assert.equal(captured.deep.a.b.c.d.e.f.g, '[truncated]', 'history object depth is bounded');
const wide = Object.fromEntries(Array.from({ length: 130 }, (_, i) => [`key-${i}`, i]));
assert.equal(Object.keys(history.capture(wide)).length, 101, 'history object keys are bounded plus scope');

// Forward entries are truncated after a new successful commit, and the bound
// remains 100 even if a consumer records more locations.
for (let i = 0; i < 110; i += 1) history.commit({ location: `item-${i}`, scope: { account: 'a', workspace: 'w' } });
assert.equal(history.snapshot().length, 100);
assert.equal(history.snapshot().entries.at(-1).location, 'item-109');
history.setScope({ account: 'b', workspace: 'w' });
assert.equal(history.snapshot().length, 0, 'account changes clear local history');
const retained = createWindowNavigation({ scope: { account: 'a', workspace: 'w' } });
retained.commit({ location: 'A' });
retained.commit({ location: 'B' });
retained.setScope({ account: 'b', workspace: 'w' }, { clear: false });
assert.equal(retained.snapshot().length, 0, 'scope retention filters foreign history');
assert.equal(retained.canGoBack(), false);

// Exact close/reopen lifecycle: descendants survive owner unregister, then
// reattach to the replacement owner. Explicit ownership and nested panes use
// the same hierarchy and receive adapter changes after reopening.
resetInputContextsForTests();
const owner = root();
const pane = root(); pane.parentNode = owner;
const nested = root(); nested.parentNode = pane;
const ownerAdapter = { back() {}, forward() {}, canGoBack: () => true, canGoForward: () => true };
const reopenedAdapter = { back() {}, forward() {}, canGoBack: () => true, canGoForward: () => true };
registerInputContext(owner, { windowId: 'reopen', paneId: 'body', navigation: ownerAdapter });
registerInputContext(pane, { windowId: 'reopen', paneId: 'pane', ownerElement: owner, navigation: ownerAdapter });
registerInputContext(nested, { windowId: 'reopen', paneId: 'nested', ownerElement: pane, navigation: ownerAdapter });
unregisterInputContext(owner);
registerInputContext(owner, { windowId: 'reopen', paneId: 'body', navigation: reopenedAdapter });
assert.equal(activateInputContext(nested)?.paneId, 'nested');
assert.equal(getInputContextEntry(nested).context.navigation, reopenedAdapter);
setInputContextLifecycle(owner, { minimized: true });
assert.equal(activateInputContext(nested), null);
setInputContextLifecycle(owner, { minimized: false, visible: true, eligible: true });
assert.equal(activateInputContext(nested)?.paneId, 'nested');
disposeInputContextTree(owner);
assert.equal(getInputContextEntry(pane), null);
assert.equal(getInputContextEntry(nested), null);

// A closed owner has no registry entry, but its detached pane tree remains
// discoverable by exact owner element until explicit destruction.
resetInputContextsForTests();
const closedOwner = root();
const closedPane = root(); closedPane.parentNode = closedOwner;
registerInputContext(closedOwner, { windowId: 'closed', paneId: 'body' });
registerInputContext(closedPane, { windowId: 'closed', paneId: 'pane', ownerElement: closedOwner });
unregisterInputContext(closedOwner);
assert.equal(getInputContextStats().registered, 1);
assert.equal(getInputContextStats().eligible, 0, 'detached panes are suspended while the owner is closed');
assert.equal(disposeInputContextTree(closedOwner), true);
assert.equal(getInputContextStats().registered, 0, 'close then destroy does not retain detached panes');

// An explicitly owned/portaled pane is detached from the owner DOM tree on
// close, so its eligibility must still follow the owner lifecycle. Removing
// that pane while closed must also remove it from the detached set; a later
// re-registration of the same element cannot be mistaken for the old entry.
resetInputContextsForTests();
const portalOwner = root();
const portalPane = root();
portalPane.parentNode = null;
registerInputContext(portalOwner, { windowId: 'same-window', paneId: 'body' });
registerInputContext(portalPane, { windowId: 'same-window', paneId: 'portal', ownerElement: portalOwner });
const unrelatedSameWindow = root();
registerInputContext(unrelatedSameWindow, { windowId: 'same-window', paneId: 'unrelated' });
unregisterInputContext(portalOwner);
assert.equal(getInputContextStats().registered, 2);
assert.equal(getInputContextStats().eligible, 1, 'explicit owner pane is suspended while detached');
unregisterInputContext(portalPane);
registerInputContext(portalOwner, { windowId: 'same-window', paneId: 'body' });
registerInputContext(portalPane, { windowId: 'same-window', paneId: 'portal', ownerElement: portalOwner });
assert.equal(getInputContextEntry(portalPane).context.eligible, true);
disposeInputContextTree(portalOwner);
assert.equal(getInputContextStats().registered, 1, 'destroying one closed tree preserves same-window registrations');
assert.equal(getInputContextEntry(unrelatedSameWindow).context.paneId, 'unrelated');
unregisterInputContext(unrelatedSameWindow);

// Teardown of a detached parent pane also clears its nested registrations and
// leaves no inaccessible descendant when the owner is subsequently destroyed.
resetInputContextsForTests();
const nestedOwner = root();
const nestedPane = root(); nestedPane.parentNode = nestedOwner;
const nestedChild = root(); nestedChild.parentNode = nestedPane;
registerInputContext(nestedOwner, { windowId: 'nested', paneId: 'body' });
registerInputContext(nestedPane, { windowId: 'nested', paneId: 'pane', ownerElement: nestedOwner });
registerInputContext(nestedChild, { windowId: 'nested', paneId: 'child', ownerElement: nestedPane });
unregisterInputContext(nestedOwner);
unregisterInputContext(nestedPane);
assert.equal(getInputContextStats().registered, 0);
disposeInputContextTree(nestedOwner);
assert.equal(getInputContextStats().registered, 0, 'detached parent teardown does not leave stale descendants');

// Cyclic explicit owner declarations are ignored deterministically. They
// cannot create recursive eligibility or adapter propagation walks.
resetInputContextsForTests();
const cycleA = root();
const cycleB = root();
registerInputContext(cycleA, { windowId: 'cycle', paneId: 'a', ownerElement: cycleB });
registerInputContext(cycleB, { windowId: 'cycle', paneId: 'b', ownerElement: cycleA });
assert.equal(getInputContextEntry(cycleA).parentEntry, null);
assert.equal(getInputContextEntry(cycleB).parentEntry, null);
const cycleAdapter = { back() {}, forward() {}, canGoBack: () => true, canGoForward: () => true };
assert.doesNotThrow(() => {
  registerInputContext(cycleA, { navigation: cycleAdapter });
  registerInputContext(cycleB, { navigation: cycleAdapter });
});
assert.equal(getInputContextStats().eligible, 2);
disposeInputContextTree(cycleA);
disposeInputContextTree(cycleB);
assert.equal(getInputContextStats().registered, 0);

resetInputContextsForTests();
const first = root();
const second = root();
const target = { nodeName: 'div', owner: first, closest: () => null };
registerInputContext(first, { windowId: 'first', paneId: 'body' });
registerInputContext(second, { windowId: 'second', paneId: 'body' });
activateInputContext(first);
assert.equal(resolveInputContext(makeEvent(target)).windowId, 'first');
setInputContextLifecycle(first, { minimized: true });
assert.equal(getActiveInputContext().windowId, 'second', 'minimized owners leave active lookup');
assert.equal(resolveInputContext(makeEvent(target)), null, 'hidden owners cannot resolve commands');
setInputContextLifecycle(second, { minimized: true });
assert.equal(getActiveInputContext(), null, 'all minimized owners leave no active owner');
setInputContextLifecycle(second, { minimized: false, visible: true, eligible: true });
setInputContextLifecycle(first, { minimized: false, visible: true, eligible: true });
activateInputContext(first);
first.isConnected = false;
assert.equal(getActiveInputContext().windowId, 'second', 'disconnected owners leave MRU lookup');
unregisterInputContext(first);
unregisterInputContext(second);

assert.deepEqual(restored, ['B', 'C']);
console.log('copal window navigation: ok');
