#!/usr/bin/env node
import assert from 'node:assert/strict';
import { withCopalBrowser } from './helpers/copal_browser_fixture.mjs';

const page = `<!doctype html><meta charset="utf-8"><link rel="stylesheet" href="/static/style.css"><body><main id="app"></main>
<script type="module">
  import { createNotesFeature } from '/static/js/copal/notesFeature.js';
  import { createMarkdownEditor, createSourceEditor } from '/static/js/copal/codemirror.js';
  import { createBufferRegistry } from '/static/js/copal/documentBuffers.js';
  import { configureCopalStorage } from '/static/js/copal/storage.js';
  configureCopalStorage('copal-notes-lifecycle');
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
  const body = document.createElement('section'); body.style.cssText = 'height:720px;width:100%'; root.append(body);
  const owner = { window:{ id:'wb00-editor', root, body, visible:true, setStatus() {} } };
  const state = { docs:[], windows:new Map([['notes', owner]]), accountId:'lifecycle-account', workspace:'lifecycle-workspace', storageNamespace:'lifecycle', contextEpoch:1, selected:null, saveTimers:new Map(), noteEditors:new Set() };
  const deferred = {};
  deferred.open = new Promise(resolve => { deferred.resolve = resolve; });
  window.__filesStub = { openCalled:false, createCount:0, disposeCount:0, cancelCount:0, currentDirectory:null, deferred };
  const feature = createNotesFeature({ h, state, createMarkdownEditor, createSourceEditor,
    renderMarkdown:source => h('article', { text:source }), renderPreview:source => h('article', { text:source }), formatBaseCell:value => String(value ?? ''),
    api:async () => ({}), saveDocument:async () => ({ outcome:'applied', revision:{kind:'copalHead', value:'next'} }), saveResource:async () => ({ outcome:'applied', revision:{kind:'hostFingerprint', value:'next'} }),
    renameNote:async () => {}, deleteDocument:async () => {}, showHistory:()=>{}, showTrash:()=>{}, showForm:()=>{}, importVault:async () => {}, loadDocuments:async () => {}, openDocument:()=>{}, persistActiveContext:()=>{}, deleteDocuments:async () => {}, activateNotes:()=>{}, renderTimeline:()=>h('div'), openEventEditor:()=>{}, resourceBufferRegistry:createBufferRegistry(),
  });
  window.__feature = feature;
  window.__owner = owner;
  window.__state = state;
  window.__openResource = async (id, name) => feature.openResource({ key:{accountId:state.accountId, workspaceId:state.workspace, provider:'copal', resourceId:id}, revision:{kind:'copalHead', value:'r1'}, representation:'markdown', capabilities:{read:true, edit:true}, locator:{displayName:name, locationLabel:name} }, { text:'# ' + name, name });
  window.__runLifecycle = async () => {
    await window.__openResource('one', 'Projects/One.md');
    owner.noteWorkspace.left.open = true;
    feature.render();
    for (let i = 0; i < 200 && !window.__filesStub.openCalled; i += 1) await new Promise(resolve => setTimeout(resolve, 10));
    const started = window.__filesStub.openCalled;
    const explorer = owner.noteFilesExplorer;
    owner.noteWorkspace.left.open = false; feature.render();
    const hidden = !document.querySelector('.copal-editor-files-explorer');
    owner.noteWorkspace.left.open = true; feature.render();
    const reopenedSame = owner.noteFilesExplorer === explorer;
    owner.window.visible = false; feature.render();
    owner.window.visible = true; feature.render();
    const restoredSame = owner.noteFilesExplorer === explorer;
    await window.__openResource('two', 'Projects/Two.md');
    const switchedSame = owner.noteFilesExplorer === explorer;
    const oldScope = explorer?.scope || null;
    state.workspace = 'retired-workspace'; state.contextEpoch += 1;
    feature.render();
    const retired = Boolean(explorer && owner.noteFilesExplorer !== explorer && explorer.element.isConnected === false);
    window.__filesStub.deferred.resolve(true);
    await new Promise(resolve => setTimeout(resolve, 40));
    return { started, hidden, reopenedSame, restoredSame, switchedSame, retired, oldScope, newScope:owner.noteFilesExplorer?.scope || null, disposeCount:window.__filesStub.disposeCount, cancelCount:window.__filesStub.cancelCount, retiredReady:explorer?.ready || false, retiredDirectoryReady:explorer?.directoryReady || false, createCount:window.__filesStub.createCount, workspace:owner.noteWorkspace?.left?.tab, explorerExists:Boolean(explorer) };
  };
</script>`;

const filesModule = `export function createFilesBrowser() {
  const state = window.__filesStub;
  state.createCount += 1;
  return {
    async open() { state.openCalled = true; await state.deferred.open; return true; },
    cancelReveal() { state.cancelCount += 1; },
    dispose() { state.disposeCount += 1; },
    getCurrentDirectory() { return state.currentDirectory; },
  };
}`;

await withCopalBrowser({ page, overrides:{ '/static/js/files.js':filesModule } }, async ({ cdp, evaluate, until }) => {
  await cdp('Emulation.setDeviceMetricsOverride', { width:1280, height:900, deviceScaleFactor:1, mobile:false });
  await until('window.__runLifecycle');
  const result = await evaluate('window.__runLifecycle()');
  assert.equal(result.started, true, `the actual Notes Explorer reaches its delayed Files browser open: ${JSON.stringify(result)}`);
  assert.equal(result.hidden, true, 'same-scope hide detaches the Explorer view');
  assert.equal(result.reopenedSame, true, 'same-scope show retains the live Explorer');
  assert.equal(result.restoredSame, true, 'minimize/restore retains the live Explorer');
  assert.equal(result.switchedSame, true, 'switching documents in scope retains the live Explorer');
  assert.equal(result.retired, true, 'changing authenticated workspace disposes the prior scope');
  assert.notEqual(result.oldScope, result.newScope, 'retirement changes the Files owner scope');
  assert.equal(result.disposeCount, 1, 'the pending browser is disposed once at scope retirement');
  assert.equal(result.retiredReady, false, 'late open completion does not mark the retired Explorer ready');
  assert.equal(result.retiredDirectoryReady, false, 'late open completion does not publish a directory into the retired scope');
  assert(result.createCount >= 2, 'scope retirement may start a new browser owned by the new scope');
  console.log(JSON.stringify({ passed:'bounded Notes Files lifecycle', ...result }, null, 2));
});
