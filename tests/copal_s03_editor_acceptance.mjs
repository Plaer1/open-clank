#!/usr/bin/env node
import assert from 'node:assert/strict';
import { withCopalBrowser } from './helpers/copal_browser_fixture.mjs';

const page = `<!doctype html><meta charset="utf-8"><link rel="stylesheet" href="/static/style.css"><body><main id="app"></main>
<script type="module">
  import { createNotesFeature } from '/static/js/copal/notesFeature.js';
  import { createMarkdownEditor, createSourceEditor } from '/static/js/copal/codemirror.js';
  import { createBufferRegistry } from '/static/js/copal/documentBuffers.js';
  import { configureCopalStorage } from '/static/js/copal/storage.js';
  configureCopalStorage('s03-editor-resize');
  const h = (tag, attrs = {}, ...children) => {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) {
      if (key === 'class') node.className = value;
      else if (key === 'text') node.textContent = value;
      else if (key.startsWith('on') && typeof value === 'function') node.addEventListener(key.slice(2).toLowerCase(), value);
      else if (value != null) node.setAttribute(key, String(value));
    }
    for (const child of children.flat()) if (child != null) node.append(child.nodeType ? child : document.createTextNode(String(child)));
    return node;
  };
  const root = document.querySelector('#app');
  const state = { docs:[], windows:new Map(), accountId:'s03-account', workspace:'s03-workspace', storageNamespace:'s03', contextEpoch:1, selected:null, saveTimers:new Map(), noteEditors:new Set() };
  const body = document.createElement('section'); root.append(body);
  state.windows.set('notes', { window:{ root, body, setStatus() {} } });
  window.__run = async () => {
    const feature = createNotesFeature({ h, state, createMarkdownEditor, createSourceEditor,
      renderMarkdown:source => h('article', { class:'rendered', text:source }), renderPreview:source => h('article', { class:'preview', text:source }), formatBaseCell:value => String(value ?? ''),
      api:async () => ({}), saveDocument:async () => ({ outcome:'applied', revision:{kind:'copalHead', value:'next'} }), saveResource:async () => ({ outcome:'applied', revision:{kind:'hostFingerprint', value:'next'} }),
      renameNote:async () => {}, deleteDocument:async () => {}, showHistory:()=>{}, showTrash:()=>{}, showForm:()=>{}, importVault:async () => {}, loadDocuments:async () => {}, openDocument:()=>{}, persistActiveContext:()=>{}, deleteDocuments:async () => {}, activateNotes:()=>{}, renderTimeline:()=>h('div'), openEventEditor:()=>{}, resourceBufferRegistry:createBufferRegistry(),
    });
    await feature.openResource({ key:{accountId:'s03-account', workspaceId:'s03-workspace', provider:'copal', resourceId:'resize-note'}, revision:{kind:'copalHead', value:'r1'}, representation:'markdown', capabilities:{read:true, edit:true}, locator:{displayName:'Resize.md', locationLabel:'Resize.md'} }, { text:'# draft', name:'Resize.md' });
    state.windows.get('notes').noteWorkspace.left.open = true;
    state.windows.get('notes').noteWorkspace.right.open = true;
    state.windows.get('notes').noteShellCache = null;
    feature.render();
    const handle = document.querySelector('.copal-sidebar-resize.left');
    const pane = document.querySelector('.copal-notes-explorer');
    const requestAnimationFrameOriginal = window.requestAnimationFrame;
    let frames = 0;
    window.requestAnimationFrame = callback => { frames += 1; return requestAnimationFrameOriginal(callback); };
    const before = pane.getBoundingClientRect().width;
    handle.focus(); handle.dispatchEvent(new KeyboardEvent('keydown', { key:'ArrowRight', bubbles:true }));
    await new Promise(resolve => requestAnimationFrame(resolve));
    const keyboard = pane.getBoundingClientRect().width;
    handle.dispatchEvent(new PointerEvent('pointerdown', { bubbles:true, pointerId:11, isPrimary:true, button:0, clientX:100 }));
    handle.dispatchEvent(new PointerEvent('pointermove', { bubbles:true, pointerId:11, isPrimary:true, clientX:160 }));
    handle.dispatchEvent(new PointerEvent('pointerup', { bubbles:true, pointerId:11, isPrimary:true, clientX:160 }));
    await new Promise(resolve => requestAnimationFrame(resolve));
    const pointer = pane.getBoundingClientRect().width;
    handle.focus(); handle.dispatchEvent(new KeyboardEvent('keydown', { key:'Home', bubbles:true }));
    await new Promise(resolve => requestAnimationFrame(resolve));
    const minimum = pane.getBoundingClientRect().width;
    handle.dispatchEvent(new KeyboardEvent('keydown', { key:'End', bubbles:true }));
    await new Promise(resolve => requestAnimationFrame(resolve));
    const maximum = pane.getBoundingClientRect().width;
    const persisted = JSON.parse(localStorage.getItem(Object.keys(localStorage).find(key => key.includes('odysseus-copal-notes-layout')) || '') || '{}');
    window.requestAnimationFrame = requestAnimationFrameOriginal;
    feature.destroy();
    return { before, keyboard, pointer, minimum, maximum, frames, persisted, keys:Object.keys(localStorage), handles:document.querySelectorAll('.copal-sidebar-resize').length };
  };
</script>`;

await withCopalBrowser({ page }, async ({ cdp, evaluate, until }) => {
  await cdp('Emulation.setDeviceMetricsOverride', { width:1280, height:900, deviceScaleFactor:1, mobile:false });
  await until('window.__run');
  const result = await evaluate('window.__run()');
  assert.equal(Math.round(result.keyboard - result.before), 12, 'ArrowRight uses the 12px resize step');
  assert(result.pointer > result.keyboard, 'pointer drag changes the pane');
  assert.equal(Math.round(result.minimum), 150, 'Home clamps to minimum');
  assert.equal(Math.round(result.maximum), 420, 'End clamps to maximum');
  assert(result.frames >= 1, 'pointer movement is coalesced through requestAnimationFrame');
  assert.equal(result.persisted.left.width, 420, 'owner/workspace width persists');
  assert.equal(result.handles, 2, 'destroy leaves one left and one right resize handle');
  console.log(JSON.stringify({ passed:'S03 mounted Notes resize lifecycle', ...result }));
});
