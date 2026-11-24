import assert from 'node:assert/strict';
import {
  activateInputContext,
  canHandleInput,
  clearActiveModal,
  dispatchCommand,
  getActiveInputContext,
  isComposingEvent,
  isEditableTarget,
  registerInputContext,
  resetInputContextsForTests,
  resolveInputContext,
  setActiveModal,
  unregisterInputContext,
} from '../../static/js/copal/inputContext.js';

const makeElement = (name) => ({
  nodeName: name,
  contains(node) { return node === this || node?.owner === this; },
  closest() { return null; },
});
const rootA = makeElement('section');
const rootB = makeElement('section');
const paneA = makeElement('main'); paneA.owner = rootA;
const paneB = makeElement('main'); paneB.owner = rootA;
const portal = makeElement('dialog');
const targetA = { owner: rootA, nodeName: 'div', closest() { return null; } };
const targetB = { owner: rootB, nodeName: 'div', closest() { return null; } };
const event = (target, extra = {}) => ({
  target,
  composedPath: () => [target, target.owner],
  defaultPrevented: false,
  isComposing: false,
  preventDefault() { this.defaultPrevented = true; },
  stopPropagation() { this.stopped = true; },
  ...extra,
});

resetInputContextsForTests();
const contextA = { windowId: 'window-a', paneId: 'timeline', modalId: 'modal-a' };
const contextB = { windowId: 'window-b', paneId: 'editor', modalId: 'modal-b', blockingModal:true };
registerInputContext(rootA, contextA);
registerInputContext(rootB, contextB);
activateInputContext(contextA);
assert.equal(getActiveInputContext().windowId, 'window-a');
assert.equal(resolveInputContext(event(targetA)).paneId, 'timeline');
assert.equal(resolveInputContext(event(targetB)), null, 'background window cannot resolve commands');
assert.equal(canHandleInput(event(targetA), contextA), true);
assert.equal(canHandleInput(event(targetA, { defaultPrevented: true }), contextA), false);
assert.equal(canHandleInput(event(targetA, { isComposing: true }), contextA), false);
assert.equal(canHandleInput(event(targetA), { ...contextA, composing:true }), false, 'context composition state blocks commands');

const input = { nodeName: 'textarea', owner: rootA, closest() { return null; } };
assert.equal(isEditableTarget(input), true);
assert.equal(canHandleInput(event(input), contextA), false, 'editable controls retain arrow/text behavior');
assert.equal(isComposingEvent({ keyCode: 229 }), true);

setActiveModal('modal-b', contextB);
assert.equal(resolveInputContext(event(targetB)).windowId, 'window-b', 'active modal gets priority');
assert.equal(resolveInputContext(event(targetA)), null, 'modal blocks an underlying window');
let calls = 0;
const modalEvent = event(targetB);
assert.equal(dispatchCommand(modalEvent, () => { calls += 1; return true; }, { stopPropagation: true }), true);
assert.equal(calls, 1);
assert.equal(modalEvent.defaultPrevented, true);
assert.equal(modalEvent.stopped, true);

resetInputContextsForTests();
const parent = makeElement('section');
const child = makeElement('main');
const childTarget = { owner: child, nodeName:'div', closest() { return null; } };
registerInputContext(parent, { windowId:'window-c', paneId:'parent' });
registerInputContext(child, { windowId:'window-c', paneId:'child' });
activateInputContext({ windowId:'window-c', paneId:'child' });
assert.equal(resolveInputContext({ target:childTarget, composedPath:() => [childTarget, child, parent] }).paneId, 'child', 'deepest pane wins');
unregisterInputContext(parent);
assert.equal(getActiveInputContext().paneId, 'child', 'removing sibling pane preserves active pane');

registerInputContext(portal, { windowId:'portal-window', paneId:'portal-pane', modalId:'portal-modal', blockingModal:true });
activateInputContext({ windowId:'window-c', paneId:'child' });
setActiveModal('portal-modal');
assert.equal(resolveInputContext(event({ owner:parent, nodeName:'div', closest() { return null; } })), null, 'active modal excludes underlying window');
const portalTarget = { owner:portal, nodeName:'div', closest() { return null; } };
const portalEvent = event(portalTarget);
assert.equal(canHandleInput(portalEvent, { windowId:'portal-window', paneId:'portal-pane', modalId:'portal-modal', blockingModal:true }), true, 'active portal modal can consume input');
activateInputContext({ windowId:'window-c', paneId:'child' });
assert.equal(resolveInputContext(event(portalTarget)).windowId, 'portal-window', 'ordinary activation does not clear blocking modal');

const outer = makeElement('dialog');
const inner = makeElement('dialog');
const outerTarget = { owner:outer, nodeName:'div', closest() { return null; } };
const innerTarget = { owner:inner, nodeName:'div', closest() { return null; } };
registerInputContext(outer, { windowId:'modal-window', paneId:'outer', modalId:'outer-modal', blockingModal:true });
registerInputContext(inner, { windowId:'modal-window', paneId:'inner', modalId:'inner-modal', blockingModal:true });
setActiveModal('outer-modal');
setActiveModal('inner-modal');
assert.equal(resolveInputContext(event(innerTarget)).modalId, 'inner-modal', 'nested modal takes top priority');
assert.equal(resolveInputContext(event(outerTarget)), null, 'underlying nested modal is blocked');
setActiveModal('outer-modal');
clearActiveModal('outer-modal');
assert.equal(resolveInputContext(event(innerTarget)).modalId, 'inner-modal', 'closing outer restores inner blocker');
clearActiveModal('inner-modal');
assert.equal(resolveInputContext(event(outerTarget)), null, 'closing final modal releases blocker');

resetInputContextsForTests();
console.log('copal input context: ok');
