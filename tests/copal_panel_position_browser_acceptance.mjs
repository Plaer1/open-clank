#!/usr/bin/env node

// Focused browser acceptance for panel anchor/focus restoration. The geometry
// counter proves a surviving first anchor avoids a visibility read per row.
import assert from 'node:assert/strict';
import { withCopalBrowser } from './helpers/copal_browser_fixture.mjs';

const page = `<!doctype html><meta charset="utf-8"><body><main id="app"></main>
<script type="module">
  import { capturePanelPositions, restorePanelPositions } from '/static/js/copal/panelPosition.js';
  const root = document.querySelector('#app');
  const panel = document.createElement('div');
  panel.dataset.notePanel = 'places';
  panel.style.height = '200px';
  panel.style.overflow = 'auto';
  Object.defineProperty(panel, 'offsetHeight', { value:200 });
  panel.getBoundingClientRect = () => ({ top:100, bottom:300, left:0, right:240, width:240, height:200 });
  const row = (key, parent, top, tag='button') => {
    const node = document.createElement(tag);
    node.dataset.noteTreeKey = key;
    node.dataset.noteParent = parent;
    node.tabIndex = 0;
    node.style.display = 'block';
    node.style.height = '20px';
    node.style.minHeight = '20px';
    node._testTop = top;
    node.getClientRects = () => node.hidden ? [] : [{ top:node._testTop, bottom:node._testTop + 20, left:0, right:100, width:100, height:20 }];
    node.getBoundingClientRect = () => ({ top:node._testTop, bottom:node._testTop + 20, left:0, right:100, width:100, height:20 });
    return node;
  };
  const folder = row('folder:Projects', '', 50, 'div');
  panel.append(folder);
  const rows = [];
  for (let index=0; index<500; index++) {
    const top = index < 10 ? 50 : 110 + (index - 10) * 20;
    const item = row('document:doc-' + index, 'Projects', top);
    rows.push(item);
    panel.append(item);
  }
  root.append(panel);
  let rowGeometryReads = 0;
  for (const node of panel.querySelectorAll('[data-note-tree-key]')) {
    const getRects = node.getClientRects;
    node.getClientRects = () => { rowGeometryReads++; return getRects(); };
  }
  window.__run = () => {
    rows[20].focus();
    panel.scrollTop = 42;
    const saved = capturePanelPositions(root, new Map());
    const savedAnchor = saved.get('places').anchors[0].key;
    const savedFocus = saved.get('places').focusKey;

    // The nearest saved anchor and the focused row are now hidden. Restore
    // should inspect the next saved anchor, then its exact focus parent.
    rows[10].hidden = true;
    rows[20].hidden = true;
    rows[11]._testTop = 150; // 20px layout shift; compensate scroll by 20px.
    panel.scrollTop = 0;
    rowGeometryReads = 0;
    restorePanelPositions(root, saved);
    return {
      rowGeometryReads,
      scrollTop:panel.scrollTop,
      activeKey:document.activeElement?.dataset?.noteTreeKey || null,
      mappedRows:panel.querySelectorAll('[data-note-tree-key]').length,
      savedAnchor,
      savedFocus,
    };
  };
</script>`;

await withCopalBrowser({ page }, async ({ evaluate, until }) => {
  await until('window.__run');
  const result = await evaluate('window.__run()');
  assert.equal(result.scrollTop, 62);
  assert.equal(result.activeKey, 'folder:Projects');
  assert.equal(result.mappedRows, 501);
  assert.equal(result.savedAnchor, 'document:doc-10');
  assert.equal(result.savedFocus, 'document:doc-20');
  assert.ok(result.rowGeometryReads <= 4, `expected at most four lazy row checks, got ${result.rowGeometryReads}`);
  console.log(`Panel position production path: ${result.mappedRows} rows, ${result.rowGeometryReads} row geometry reads, preserved anchor scroll and parent focus.`);
});
