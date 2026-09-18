#!/usr/bin/env node

import assert from 'node:assert/strict';
import { withCopalBrowser } from './helpers/copal_browser_fixture.mjs';

const page = `<!doctype html><meta charset="utf-8"><body>
<main id="a"></main><main id="b"></main>
<script type="module">
  import { initCustomContextMenu } from '/static/js/custom-context-menu.js';
  import { createTableWidget, parseTable, applyTableEdit } from '/static/js/copal/tableModel.js?table-menu';
  initCustomContextMenu();
  const source = new Map([['a', '| A | B |\\n| --- | --- |\\n| one | two |'], ['b', '| X | Y |\\n| --- | --- |\\n| left | right |']]);
  const calls = [];
  const mount = (id) => {
    const host = document.querySelector('#' + id);
    const render = () => {
      const model = parseTable(source.get(id));
      const widget = createTableWidget(model, edit => {
        calls.push({ id, edit });
        const next = applyTableEdit(source.get(id), model, edit);
        source.set(id, next.newText);
        host.dataset.source = next.newText;
      });
      host.replaceChildren(widget);
      host.dataset.source = source.get(id);
    };
    render();
  };
  window.__run = () => { mount('a'); mount('b'); };
  window.__state = () => ({ calls, source:Object.fromEntries(source) });
</script></body>`;

await withCopalBrowser({ page }, async ({ evaluate, until }) => {
  await until('window.__run');
  await evaluate('window.__run()');
  await until('document.querySelectorAll(".copal-table-widget").length === 2');

  const open = async selector => {
    await evaluate(`document.querySelector(${JSON.stringify(selector)}).dispatchEvent(new MouseEvent('contextmenu', { bubbles:true, cancelable:true, clientX:20, clientY:20 }))`);
    await until('!!document.querySelector("#openclank-context-menu")');
  };

  await open('#b td[data-row="1"][data-col="0"]');
  assert.equal(await evaluate('[...document.querySelectorAll("#openclank-context-menu button")].some(b => b.dataset.command === "table-insert-row-below")'), true);
  assert.equal(await evaluate('[...document.querySelectorAll("#openclank-context-menu button")].some(b => b.dataset.command === "table-insert-column-right")'), false);
  await evaluate('document.querySelector("#openclank-context-menu button[data-command=table-insert-row-below]").click()');
  assert.deepEqual((await evaluate('window.__state()')).calls, [{ id:'b', edit:{ type:'insertRow', afterRow:1 } }]);
  assert.equal((await evaluate('window.__state()')).source.a, '| A | B |\n| --- | --- |\n| one | two |');

  await open('#a th[data-row="0"][data-col="1"]');
  assert.equal(await evaluate('[...document.querySelectorAll("#openclank-context-menu button")].some(b => b.dataset.command === "table-insert-column-left")'), true);
  assert.equal(await evaluate('[...document.querySelectorAll("#openclank-context-menu button")].some(b => b.dataset.command === "table-insert-row-below")'), false);
  await evaluate('document.querySelector("#openclank-context-menu button[data-command=table-insert-column-left]").click()');
  const afterHeader = await evaluate('window.__state()');
  assert.equal(afterHeader.calls.at(-1).id, 'a');
  assert.equal(afterHeader.calls.at(-1).edit.type, 'insertColumn');
  assert.equal(afterHeader.source.b, '| X    | Y     |\n| :--- | :--- |\n| left | right |\n|      |       |');

  await evaluate('localStorage.setItem("odysseus-custom-context-menu", "off")');
  // Dispatch from a real table cell and observe that no app-owned menu or
  // preventDefault remains when the shared setting is disabled.
  const native = await evaluate(`(() => { let prevented = null; const cell=document.querySelector('#a td[data-row="1"][data-col="0"]'); const event=new MouseEvent('contextmenu',{bubbles:true,cancelable:true}); cell.dispatchEvent(event); return { prevented:event.defaultPrevented, menu:!!document.querySelector('#openclank-context-menu') }; })()`);
  assert.deepEqual(native, { prevented:false, menu:false });
  console.log('Mounted table context ownership: two production widgets isolate captured mutations, header/body commands are contextual, and disabled shared menus leave the browser menu untouched.');
});
