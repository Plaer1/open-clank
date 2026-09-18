#!/usr/bin/env node

import assert from 'node:assert/strict';
import { withCopalBrowser } from './helpers/copal_browser_fixture.mjs';

const page = `<!doctype html><meta charset="utf-8"><body><main id="tasks"></main>
<script type="module">
  const h = (tag, attrs = {}, ...children) => {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs || {})) {
      if (key === 'class') node.className = value;
      else if (key === 'text') node.textContent = value;
      else if (key.startsWith('on') && typeof value === 'function') node.addEventListener(key.slice(2), value);
      else if (value !== false && value != null) node.setAttribute(key, String(value));
    }
    for (const child of children.flat()) if (child) node.append(child.nodeType ? child : document.createTextNode(String(child)));
    return node;
  };
  const data = { tracks:[], floatingTodos:[] };
  const firstPage = Array.from({ length:100 }, (_, index) => ({ id:'task-'+index, source:'vault', text:'ordinary '+index, checked:false, label:'note-'+index+'.md', task:{ text:'ordinary '+index, done:false }, doc:{ id:'doc-'+index, name:'note-'+index+'.md' } }));
  const match = { id:'task-4321', source:'vault', text:'needle after page one', checked:false, label:'needle.md', task:{ text:'needle after page one', done:false }, doc:{ id:'doc-4321', name:'needle.md' } };
  window.__state = { items:firstPage, calls:[], sourceDocumentsRead:0 };
  window.__setup = async () => {
    const { createPlanningFeature } = await import('/static/js/copal/planning.js?tasks-5k-browser');
    let planning;
    const redraw = () => planning.renderTodo(document.querySelector('#tasks'), window.__state.items, { total:5000, indexedTotal:5000, matchedTotal:window.__state.items.length, totalExact:true, query:window.__state.query || {}, onSelect:() => {} });
    planning = createPlanningFeature({ h, api:async () => ({}), getPlanning:() => data, refresh:async () => {}, setStatus:() => {}, projectionChanged:() => {}, openDocument:() => {}, openMarkdownTask:() => {}, createMarkdownTask:() => {}, patchMarkdownTask:async () => {}, queryMarkdownTasks:async options => { window.__state.calls.push(options); window.__state.query = options; window.__state.items = options.query ? [match] : firstPage; redraw(); } });
    redraw();
  };
</script></body>`;

await withCopalBrowser({ page }, async ({ evaluate, until }) => {
  await until('window.__setup');
  await evaluate('window.__setup()');
  await until('document.querySelectorAll(".copal-task-row").length === 100');
  assert.equal(await evaluate('window.__state.sourceDocumentsRead'), 0);
  await evaluate(`(() => { const search=document.querySelector('[aria-label="Search tasks"]'); search.value='needle after page one'; search.dispatchEvent(new Event('input',{bubbles:true})); })()`);
  await until('document.querySelectorAll(".copal-task-row").length === 1');
  assert.equal(await evaluate('document.querySelector(".copal-task-title").textContent'), 'needle after page one');
  assert.equal(await evaluate('window.__state.calls.at(-1).query'), 'needle after page one');
  assert.equal(await evaluate('window.__state.sourceDocumentsRead'), 0);
  console.log('5k task browser/model path: first page stayed at 100 rows, a filtered match beyond row 100 was reached by server query, and no source documents were loaded.');
});
