#!/usr/bin/env node

import assert from 'node:assert/strict';
import { withCopalBrowser } from './helpers/copal_browser_fixture.mjs';

const page = `<!doctype html><meta charset="utf-8"><body><main id="tasks"></main>
<script type="module">
  const h = (tag, attrs = {}, ...children) => {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) {
      if (key === 'class') node.className = value;
      else if (key === 'text') node.textContent = value;
      else if (key.startsWith('on') && typeof value === 'function') node.addEventListener(key.slice(2), value);
      else if (value !== false && value != null) node.setAttribute(key, String(value));
    }
    for (const child of children.flat()) if (child) node.append(child.nodeType ? child : document.createTextNode(String(child)));
    return node;
  };
  const data = { tracks:[], floatingTodos:[] };
  const item = { type:'markdown', id:'note-1:block-1', source:'vault', text:'Write release notes', checked:false, label:'Projects/Release.md', sourceRevision:'head-1', resourceKey:{ accountId:'a', workspaceId:'w', provider:'copal', resourceId:'r1' }, anchor:{ blockId:'block-1', sourceRange:{ from:0, to:25 }, expectedTextHash:'hash-1', expectedText:'- [ ] Write release notes' }, document:{ id:'note-1', name:'Projects/Release.md', head:'head-1', kind:'note' }, doc:{ id:'note-1', name:'Projects/Release.md', head:'head-1', kind:'note' }, task:{ id:'note-1:block-1', blockId:'block-1', line:1, text:'Write release notes', done:false } };
  const calls = { patch:[], creates:0, opens:[], refreshes:0 };
  window.__taskSetup = async () => {
    const { createPlanningFeature } = await import('/static/js/copal/planning.js?task-browser');
    const planning = createPlanningFeature({
      h, api:async () => ({}), getPlanning:() => data, refresh:async () => { calls.refreshes += 1; }, setStatus:() => {}, projectionChanged:() => {}, openDocument:() => {},
      openMarkdownTask:(value) => calls.opens.push(value.doc.id),
      createMarkdownTask:() => { calls.creates += 1; },
      patchMarkdownTask:async (value, checked) => { calls.patch.push({ id:value.id, checked }); value.checked = checked; value.task.done = checked; },
    });
    window.__render = () => planning.renderTodo(document.querySelector('#tasks'), [item]);
    window.__render();
  };
  window.__snapshot = () => ({ rows:[...document.querySelectorAll('.copal-task-row')].map(row => ({ text:row.textContent, checked:row.querySelector('input')?.checked })), calls, sourceFilter:document.querySelector('[aria-label="Source filter"]')?.value });
</script></body>`;

await withCopalBrowser({ page }, async ({ evaluate, until }) => {
  await until('window.__taskSetup');
  await evaluate('window.__taskSetup()');
  await until('document.querySelectorAll(".copal-task-row").length === 1');
  const before = await evaluate('window.__snapshot()');
  assert.equal(before.rows[0].checked, false);
  await evaluate('document.querySelector(".copal-task-row input").click()');
  await until('window.__snapshot().calls.patch.length === 1');
  const toggled = await evaluate('window.__snapshot()');
  assert.deepEqual(toggled.calls.patch, [{ id:'note-1:block-1', checked:true }]);
  assert.equal(toggled.rows[0].checked, true);
  await evaluate('document.querySelector(".copal-task-title").click()');
  assert.deepEqual(await evaluate('window.__snapshot().calls.opens'), ['note-1']);
  await evaluate('document.querySelector(".copal-meatbag-tasks .primary").click()');
  assert.equal((await evaluate('window.__snapshot().calls.creates')), 1);
  await evaluate('(() => { const source=document.querySelector("[aria-label=\\"Source filter\\"]"); source.value="timeline"; source.dispatchEvent(new Event("change",{bubbles:true})); })()');
  assert.equal((await evaluate('document.querySelectorAll(".copal-task-row").length')), 0);
  await evaluate('(() => { const source=document.querySelector("[aria-label=\\"Source filter\\"]"); source.value="vault"; source.dispatchEvent(new Event("change",{bubbles:true})); window.__render(); })()');
  assert.equal((await evaluate('document.querySelectorAll(".copal-task-row").length')), 1);
  assert.equal((await evaluate('document.querySelector(".copal-task-row input").checked')), true);
  console.log('Production task browser path: text/completed state, one toggle mutation, exact source navigation, create-in-note hook, filters, and refresh selection passed.');
});
