#!/usr/bin/env node
import assert from 'node:assert/strict';
import { withCopalBrowser } from './helpers/copal_browser_fixture.mjs';

const page = `<!doctype html><meta charset="utf-8"><style>body{margin:0}.copal-tool-modal{width:640px;height:360px}</style><main id="fixture"></main><script>window.addEventListener('error',event=>window.auditError=event.message);window.addEventListener('unhandledrejection',event=>window.auditError=String(event.reason));</script><script type="module" src="/fixture.js"></script>`;
const fixture = `
const listenerTypes = new Set(['pointerdown','mousedown','auxclick']);
let listenerAdds = 0;
const listenerByType = {};
const nativeAdd = document.addEventListener.bind(document);
document.addEventListener = (type, handler, options) => { if (listenerTypes.has(type)) { listenerAdds += 1; listenerByType[type] = (listenerByType[type] || 0) + 1; } return nativeAdd(type, handler, options); };
const IC = await import('/static/js/copal/inputContext.js');
const { createOpenClankWindow } = await import('/static/js/copal/windows.js');
const Modals = await import('/static/js/modalManager.js');
const root = document.getElementById('fixture');
const adapter1 = { back(){ window.audit.back1 += 1; }, forward(){}, canGoBack:()=>true, canGoForward:()=>true };
const adapter2 = { back(){ window.audit.back2 += 1; }, forward(){}, canGoBack:()=>true, canGoForward:()=>true };
const win = createOpenClankWindow({ id:'s00-lifecycle-window', label:'S00 lifecycle', accountScope:'acct-a', workspaceScope:'workspace-a' });
win.show();
const pane = document.createElement('section'); pane.dataset.pane = 'persistent'; win.body.append(pane);
win.registerPane(pane, { paneId:'persistent-pane' });
// This pane lives in a portal outside the window DOM. Its explicit owner and
// nested child must still inherit the window lifecycle and adapter.
const portalPane = document.createElement('section'); portalPane.dataset.pane = 'portal'; document.body.append(portalPane);
const portalNested = document.createElement('div'); portalPane.append(portalNested);
win.registerPane(portalPane, { paneId:'portal-pane', ownerElement:win.root });
win.registerPane(portalNested, { paneId:'portal-nested', ownerElement:portalPane });
win.setNavigationAdapter(adapter1);
window.audit = { win, pane, portalPane, portalNested, adapter1, adapter2, back1:0, back2:0, listenerAdds, IC };
window.audit.lifecycle = () => {
  const before = IC.getInputContextEntry(pane);
  win.requestClose();
  const closed = IC.getInputContextStats();
  win.show();
  const after = IC.getInputContextEntry(pane);
  const portalAfter = IC.getInputContextEntry(portalPane);
  const nestedAfter = IC.getInputContextEntry(portalNested);
  const activated = IC.activateInputContext(pane)?.paneId;
  win.setNavigationAdapter(adapter2);
  const propagated = after?.context.navigation === adapter2
    && portalAfter?.context.navigation === adapter2
    && nestedAfter?.context.navigation === adapter2;
  Modals.minimize(win.id);
  const minimized = IC.activateInputContext(pane) === null && IC.activateInputContext(portalNested) === null;
  Modals.restore(win.id);
  const restored = IC.activateInputContext(portalNested)?.paneId;
  for (let i=0; i<3; i += 1) { win.requestClose(); win.show(); IC.activateInputContext(pane); }
  const cycled = IC.getInputContextEntry(pane)?.context.navigation === adapter2;
  const invalidated = IC.invalidateInputContexts({ accountScope:'acct-a' });
  const revoked = IC.getInputContextStats();
  win.destroy();
  const disposed = IC.getInputContextStats();
  const makeDetached = (id, onClosed = null) => {
    let detachedWindow;
    detachedWindow = createOpenClankWindow({ id, label:id, onClosed });
    detachedWindow.show();
    const detachedPane = document.createElement('section');
    detachedWindow.body.append(detachedPane);
    detachedWindow.registerPane(detachedPane, { paneId:id+'-pane' });
    return { detachedWindow, detachedPane };
  };
  const outside = makeDetached('s00-close-destroy');
  const outsideSibling = document.createElement('div'); document.body.append(outsideSibling);
  IC.registerInputContext(outsideSibling, { windowId:'s00-close-destroy', paneId:'unrelated-sibling' });
  outside.detachedWindow.requestClose();
  outside.detachedWindow.destroy();
  const closeDestroy = IC.getInputContextStats();
  const siblingSurvives = IC.getInputContextEntry(outsideSibling)?.context.paneId;
  IC.unregisterInputContext(outsideSibling); outsideSibling.remove();
  const closeDestroyCleaned = IC.getInputContextStats();
  let callbackWindow;
  const callback = makeDetached('s00-onclosed-destroy', () => callbackWindow?.destroy());
  callbackWindow = callback.detachedWindow;
  const callbackSibling = document.createElement('div'); document.body.append(callbackSibling);
  IC.registerInputContext(callbackSibling, { windowId:'s00-onclosed-destroy', paneId:'unrelated-sibling' });
  callbackWindow.requestClose();
  const callbackDestroy = IC.getInputContextStats();
  const callbackSiblingSurvives = IC.getInputContextEntry(callbackSibling)?.context.paneId;
  IC.unregisterInputContext(callbackSibling); callbackSibling.remove();
  const callbackDestroyCleaned = IC.getInputContextStats();
  return { before:!!before, closed, activated, propagated, minimized, restored, cycled, invalidated, revoked, disposed, closeDestroy, siblingSurvives, closeDestroyCleaned, callbackDestroy, callbackSiblingSurvives, callbackDestroyCleaned };
};
window.audit.performance = () => {
  const run = (count) => {
    IC.resetInputContextsForTests();
    const nodes = [];
    for (let i=0; i<count; i += 1) {
      const node = document.createElement('div');
      node.dataset.owner = String(i); document.body.append(node); nodes.push(node);
      IC.registerInputContext(node, { windowId:'bench-'+i, paneId:'body' });
    }
    IC.activateInputContext(nodes.at(-1));
    const samples = [];
    for (let i=0; i<1000; i += 1) {
      const started = performance.now();
      nodes.at(-1).dispatchEvent(new PointerEvent('pointerdown', { bubbles:true, button:0 }));
      samples.push(performance.now() - started);
    }
    samples.sort((a,b)=>a-b);
    const result = { count, p50:samples[499], p95:samples[949], max:samples.at(-1), stats:IC.getInputContextStats() };
    for (const node of nodes) { IC.unregisterInputContext(node); node.remove(); }
    return result;
  };
  return [run(1), run(10), run(50)];
};
window.audit.sidePath = async () => {
  IC.resetInputContextsForTests();
  const side = createOpenClankWindow({ id:'s00-side-window', label:'S00 side path' });
  side.show(); side.setNavigationAdapter({ back(){ window.audit.sideCalls += 1; }, forward(){}, canGoBack:()=>true, canGoForward:()=>true });
  window.audit.sideCalls = 0;
  const longTasks = [];
  const observer = new PerformanceObserver(list => longTasks.push(...list.getEntries()));
  try { observer.observe({ type:'longtask', buffered:false }); } catch (_) {}
  const samples = [];
  for (let i=0; i<200; i += 1) {
    const started = performance.now();
    side.root.dispatchEvent(new PointerEvent('pointerdown', { bubbles:true, cancelable:true, button:3 }));
    samples.push(performance.now() - started);
  }
  await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  observer.disconnect();
  const result = { calls:window.audit.sideCalls, max:Math.max(...samples), longTasks:longTasks.map(entry=>entry.duration), listeners:IC.getInputContextStats().navigationListenersInstalled, listenerCount:listenerAdds, listenerByType };
  side.destroy();
  IC.resetInputContextsForTests();
  return result;
};
window.audit.focusOwnership = () => {
  const nativeRaf = window.requestAnimationFrame;
  const queued = [];
  window.requestAnimationFrame = (callback) => { queued.push(callback); return queued.length; };
  const focused = createOpenClankWindow({ id:'s00-focus-generation', label:'Focus generation' });
  const competing = createOpenClankWindow({ id:'s00-focus-competing', label:'Competing focus' });
  const external = document.createElement('button'); external.textContent = 'External focus'; document.body.append(external);
  const input = document.createElement('input'); input.value = 'descendant'; focused.body.append(input);
  focused.show(); input.focus();
  const firstCallback = queued.shift(); firstCallback?.(performance.now());
  const descendantPreserved = document.activeElement === input;
  external.focus(); focused.show(); competing.show();
  queued.shift()?.(performance.now());
  const competingPreserved = document.activeElement === external && window.audit.IC.getActiveInputContext()?.windowId === competing.id;
  for (const callback of queued.splice(0)) callback(performance.now());
  external.focus(); focused.show(); const oldShowCallback = queued.shift();
  focused.requestClose(); focused.show(); const newShowCallback = queued.shift();
  oldShowCallback?.(performance.now());
  const staleReopenPreserved = document.activeElement === external;
  newShowCallback?.(performance.now());
  const ordinaryWindowFocused = document.activeElement === focused.body;
  competing.destroy(); focused.destroy(); input.remove(); external.remove(); window.requestAnimationFrame = nativeRaf;
  return { descendantPreserved, competingPreserved, staleReopenPreserved, ordinaryWindowFocused };
};
window.auditReady = true;
`;

await withCopalBrowser({ page, overrides: { '/fixture.js': fixture } }, async ({ evaluate, until }) => {
  await until('Boolean(window.auditReady || window.auditError)');
  assert.equal(await evaluate('window.auditError || null'), null);
  const lifecycle = await evaluate('window.audit.lifecycle()');
  assert.equal(lifecycle.before, true);
  assert.equal(lifecycle.activated, 'persistent-pane');
  assert.equal(lifecycle.propagated, true);
  assert.equal(lifecycle.minimized, true);
  assert.equal(lifecycle.restored, 'portal-nested');
  assert.equal(lifecycle.cycled, true);
  assert.equal(lifecycle.invalidated, 4, 'account invalidation revokes the owner and all registered panes');
  assert.equal(lifecycle.revoked.eligible, 0);
  assert.equal(lifecycle.disposed.registered, 0, 'destroy disposes persistent pane registrations');
  assert.equal(lifecycle.disposed.eligible, 0);
  assert.equal(lifecycle.closeDestroy.registered, 1, 'close then destroy preserves unrelated same-window registrations');
  assert.equal(lifecycle.siblingSurvives, 'unrelated-sibling');
  assert.equal(lifecycle.closeDestroyCleaned.registered, 0, 'close then destroy reaches zero after sibling cleanup');
  assert.equal(lifecycle.closeDestroyCleaned.eligible, 0);
  assert.equal(lifecycle.closeDestroyCleaned.mru, 0);
  assert.equal(lifecycle.callbackDestroy.registered, 1, 'onClosed then destroy preserves unrelated same-window registrations');
  assert.equal(lifecycle.callbackSiblingSurvives, 'unrelated-sibling');
  assert.equal(lifecycle.callbackDestroyCleaned.registered, 0, 'onClosed then destroy reaches zero after sibling cleanup');
  assert.equal(lifecycle.callbackDestroyCleaned.eligible, 0);
  assert.equal(lifecycle.callbackDestroyCleaned.mru, 0);

  const performance = await evaluate('window.audit.performance()');
  for (const sample of performance) {
    assert(sample.p95 <= 1, `p95 dispatch budget exceeded for ${sample.count}: ${sample.p95}`);
    assert.equal(sample.stats.registered, sample.count);
    assert.equal(sample.stats.eligible, sample.count);
  }
  const sidePath = await evaluate('window.audit.sidePath()');
  assert(sidePath.calls > 0, 'mounted side-button path invokes the adapter');
  assert.equal(sidePath.listeners, true);
  assert.equal(sidePath.listenerCount, 4, 'auxiliary routing keeps a fixed listener count');
  assert(sidePath.listenerByType.pointerdown >= 1 && sidePath.listenerByType.mousedown >= 1 && sidePath.listenerByType.auxclick >= 1);
  assert(sidePath.longTasks.every(duration => duration <= 50), `ownership long task exceeded 50ms: ${sidePath.longTasks}`);
  const focusOwnership = await evaluate('window.audit.focusOwnership()');
  assert.deepEqual(focusOwnership, { descendantPreserved:true, competingPreserved:true, staleReopenPreserved:true, ordinaryWindowFocused:true }, 'delayed show focus preserves newer descendants, competing windows, and reopen generations');
  console.log(JSON.stringify({ lifecycle, performance, sidePath, focusOwnership }, null, 2));
});
