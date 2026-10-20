#!/usr/bin/env node

import assert from 'node:assert/strict';
import { withCopalBrowser } from './helpers/copal_browser_fixture.mjs';

const page = `<!doctype html><meta charset="utf-8"><body>
<main id="a"></main><main id="b"></main><main id="typed"></main><main id="locked"></main>
<script type="module">
  import { initCustomContextMenu } from '/static/js/custom-context-menu.js';
  import { createTableWidget, parseTable, applyTableEdit } from '/static/js/copal/tableModel.js?table-menu';
  initCustomContextMenu();
  const source = new Map([
    ['a', '| A | B |\\n| --- | --- |\\n| one | two |'],
    ['b', '| X | Y |\\n| --- | --- |\\n| left | right |'],
    ['typed', '<!-- clank-table v=1 id=tbl-typed\\ncolumn id=col-when type=date format=iso\\ncolumn id=col-amt type=currency format=code\\n-->\\n| When | Amt | Note |\\n| :--- | ---: | :--- |\\n| 2026-09-22 | USD 12.50 | mid |\\n| 2026-01-05 | USD 4.00 | early |\\n| 2026-12-31 | USD 9.25 | late |\\n| | =SUM(B1:B3) | total |'],
    ['locked', '| L | M |\\n| --- | --- |\\n| keep | me |'],
  ]);
  const calls = [];
  const mount = (id, editable = true) => {
    const host = document.querySelector('#' + id);
    const render = () => {
      const model = parseTable(source.get(id));
      const widget = createTableWidget(model, edit => {
        calls.push({ id, edit });
        const next = applyTableEdit(source.get(id), model, edit);
        source.set(id, next.newText);
        host.dataset.source = next.newText;
        render();
      }, { editable });
      host.replaceChildren(widget);
      host.dataset.source = source.get(id);
    };
    render();
  };
  window.__run = () => { mount('a'); mount('b'); mount('typed'); mount('locked', false); };
  window.__state = () => ({ calls, source:Object.fromEntries(source) });
</script></body>`;

await withCopalBrowser({ page }, async ({ evaluate, until }) => {
  await until('window.__run');
  await evaluate('window.__run()');
  await until('document.querySelectorAll(".copal-table-widget").length === 4');

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

  // ── S18 typed table: header menu exposes sort, type and move commands ──
  await open('#typed th[data-row="0"][data-col="0"]');
  const headerCommands = await evaluate('[...document.querySelectorAll("#openclank-context-menu button")].map(b => b.dataset.command)');
  for (const id of ['table-sort-asc', 'table-sort-desc', 'table-type-date', 'table-type-currency', 'table-move-column-right', 'table-align-center']) {
    assert.equal(headerCommands.includes(id), true, `header menu should expose ${id}`);
  }
  await evaluate('document.querySelector("#openclank-context-menu button[data-command=table-sort-asc]").click()');
  const afterSort = await evaluate('window.__state()');
  assert.equal(afterSort.calls.at(-1).edit.type, 'sort');
  assert.deepEqual(afterSort.calls.at(-1).edit, { type:'sort', col:0, direction:'asc' });

  // ISO date order: 2026-01-05 before 2026-09-22 before 2026-12-31.
  const early = afterSort.source.typed.indexOf('2026-01-05');
  const mid = afterSort.source.typed.indexOf('2026-09-22');
  const late = afterSort.source.typed.indexOf('2026-12-31');
  assert.ok(early >= 0 && mid >= 0 && late >= 0, 'all three dates still present');
  assert.ok(early < mid && mid < late, 'dates sorted chronologically');

  // ── S18 typed table: body menu exposes row ops and typed sort ──
  await open('#typed td[data-row="1"][data-col="2"]');
  const bodyCommands = await evaluate('[...document.querySelectorAll("#openclank-context-menu button")].map(b => b.dataset.command)');
  for (const id of ['table-insert-row-above', 'table-insert-row-below', 'table-delete-row', 'table-move-row-up', 'table-move-row-down', 'table-sort-desc']) {
    assert.equal(bodyCommands.includes(id), true, `body menu should expose ${id}`);
  }
  await evaluate('document.querySelector("#openclank-context-menu button[data-command=table-insert-row-below]").click()');
  const afterRowInsert = await evaluate('window.__state()');
  assert.equal(afterRowInsert.calls.at(-1).edit.type, 'insertRow');

  // ── S18: formula source is inspectable in the formula bar ──
  await evaluate(`(() => {
    const cells = [...document.querySelectorAll('#typed td.copal-table-cell-formula, #typed td[title^="="]')];
    const target = cells.find((td) => (td.title || td.textContent || '').includes('=SUM(')) || cells[0];
    target?.click();
  })()`);
  const formulaBar = await evaluate('document.querySelector("#typed .copal-table-widget .copal-table-formula-bar")?.textContent');
  // The total row formula should be visible as source text.
  assert.ok(String(formulaBar).includes('=SUM('), `formula bar should show source formula, got ${JSON.stringify(formulaBar)}`);

  // ── S18: typed display — currency/date cells carry status classes ──
  const cellStatus = await evaluate(`(() => {
    const td = document.querySelector('#typed td[data-row="1"][data-col="0"]');
    return td ? { status: td.dataset.cellStatus, type: td.closest('table')?.querySelector('th[data-col="0"]')?.dataset.columnType } : null;
  })()`);
  assert.ok(cellStatus, 'typed cell exists');
  assert.equal(cellStatus.type, 'date', 'column 0 is typed date');
  assert.equal(cellStatus.status, 'valid');

  // ── S18: read-only widgets refuse mutation ──
  await open('#locked td[data-row="1"][data-col="0"]');
  const lockedCommands = await evaluate('[...document.querySelectorAll("#openclank-context-menu button")].filter(b => b.dataset.command === "table-delete-row").map(b => b.disabled)');
  assert.deepEqual(lockedCommands, [true], 'delete row is disabled on a read-only widget');
  const lockedInsert = await evaluate('[...document.querySelectorAll("#openclank-context-menu button")].filter(b => b.dataset.command === "table-insert-row-below").map(b => b.disabled)');
  assert.deepEqual(lockedInsert, [true], 'insert row is disabled on a read-only widget');
  // The disabled command must not mutate the source when activated programmatically.
  await evaluate('window.__openClankTableContextCommand("table-insert-row-below", document.querySelector("#locked td"))');
  const afterLocked = await evaluate('window.__state()');
  assert.equal(afterLocked.source.locked, '| L | M |\n| --- | --- |\n| keep | me |', 'read-only table source unchanged');
  await evaluate('document.getElementById("openclank-context-menu")?.remove()');

  await evaluate('localStorage.setItem("odysseus-custom-context-menu", "off")');
  // Dispatch from a real table cell and observe that no app-owned menu or
  // preventDefault remains when the shared setting is disabled.
  const native = await evaluate(`(() => { let prevented = null; const cell=document.querySelector('#a td[data-row="1"][data-col="0"]'); const event=new MouseEvent('contextmenu',{bubbles:true,cancelable:true}); cell.dispatchEvent(event); return { prevented:event.defaultPrevented, menu:!!document.querySelector('#openclank-context-menu') }; })()`);
  assert.deepEqual(native, { prevented:false, menu:false });
  console.log('Mounted table context ownership: two production widgets isolate captured mutations, header/body commands are contextual, typed sort/type/move commands are discoverable, formula source is inspectable, read-only widgets refuse mutation, and disabled shared menus leave the browser menu untouched.');
});
