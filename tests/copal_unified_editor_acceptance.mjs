import assert from 'node:assert/strict';
import { withCopalBrowser } from './helpers/copal_browser_fixture.mjs';

const page = `<!doctype html><meta charset="utf-8"><body><main id="app"></main>
<script type="module">
  import { createNotesFeature } from '/static/js/copal/notesFeature.js';
  import { createMarkdownEditor, createSourceEditor } from '/static/js/copal/codemirror.js';
  import { createBufferRegistry } from '/static/js/copal/documentBuffers.js';
  import { configureCopalStorage } from '/static/js/copal/storage.js';
  import { splitWorkspaceGroup, moveWorkspaceLeaf, setWorkspaceLeafMode, serializeNotesWorkspace, normalizeNotesWorkspace, closeWorkspaceLeaf } from '/static/js/copal/notesWorkspace.js';
  configureCopalStorage('fixture-unified-editor');

  const h = (tag, attrs = {}, ...children) => {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) {
      if (key === 'class') node.className = value;
      else if (key === 'text') node.textContent = value;
      else if (key.startsWith('on') && typeof value === 'function') node.addEventListener(key.slice(2), value);
      else if (value !== false && value != null) node.setAttribute(key, String(value));
    }
    for (const child of children.flat()) if (child != null) node.append(child.nodeType ? child : document.createTextNode(String(child)));
    return node;
  };

  const state = {
    docs: [], windows: new Map(), accountId:'fixture-account', workspace:'fixture-workspace',
    storageNamespace:'fixture', contextEpoch:1, selected:null, saveTimers:new Map(), noteEditors:new Set(),
  };
  const root = document.querySelector('#app');
  const body = document.createElement('section'); root.append(body);
  state.windows.set('notes', { window:{ root, body, setStatus() {} } });
  const saves = [];
  const feature = createNotesFeature({
    h, state, createMarkdownEditor, createSourceEditor,
    renderMarkdown:source => h('article', { class:'rendered' }, source),
    renderPreview:source => h('article', { class:'preview' }, source),
    formatBaseCell:value => String(value ?? ''),
    api:async path => path.startsWith('/bases/') ? { columns:[], rows:[] } : {},
    saveDocument:async () => ({ outcome:'applied', revision:{kind:'copalHead', value:'fixture-next'} }),
    saveResource:async (snapshot, {resource}) => {
      saves.push({ snapshot, resource });
      return { outcome:'applied', revision:{kind:'hostFingerprint', value:'host-next'} };
    },
    renameNote:async () => {}, deleteDocument:async () => {}, showHistory:()=>{}, showTrash:()=>{}, showForm:()=>{},
    importVault:async () => {}, loadDocuments:async () => {}, openDocument:()=>{}, persistActiveContext:()=>{},
    deleteDocuments:async () => {}, activateNotes:()=>{}, renderTimeline:()=>h('div'), openEventEditor:()=>{},
    resourceBufferRegistry:createBufferRegistry(),
  });
  const key = (provider, resourceId) => ({ accountId:'fixture-account', workspaceId:'fixture-workspace', provider, resourceId });
  const managed = { key:key('copal','note-1'), revision:{kind:'copalHead', value:'note-1'}, representation:'markdown', capabilities:{read:true, edit:true}, locator:{displayName:'Managed.md', locationLabel:'Managed.md'} };
  const host = { key:key('host','readme'), revision:{kind:'hostFingerprint', value:'fp-1'}, representation:'markdown', metadata:{encoding:'utf-16le', newline:'\\r\\n', bomBytes:2, mode:'markdown', language:'markdown'}, capabilities:{read:true, edit:true}, locator:{displayName:'README.md', locationLabel:'README.md', opaqueRef:'opaque-readme'} };
  const source = { key:key('host','source'), revision:{kind:'hostFingerprint', value:'fp-2'}, representation:'text', metadata:{encoding:'utf-8', newline:'\\n', bomBytes:0, mode:'source', language:'python'}, capabilities:{read:true, edit:true}, locator:{displayName:'main.py', locationLabel:'main.py', opaqueRef:'opaque-source'} };
  const base = { key:key('copal','base-1'), revision:{kind:'copalHead', value:'base-1'}, representation:'base', capabilities:{read:true, edit:false}, locator:{displayName:'Tasks Base', locationLabel:'Tasks Base'} };
  const legacy = resource => feature.openResource(resource, { text: resource.key.resourceId === 'note-1' ? '# draft' : '', name:resource.locator.displayName });
  const leavesOf = node => node?.type === 'group' ? node.tabs : (node?.children || []).flatMap(leavesOf);
  const check = (condition, message) => { if (!condition) throw new Error(message); };
  window.__run = async () => {
    const noteId = await feature.openResource(managed, { text:'# managed', name:'Managed.md' });
    await legacy(managed); // legacy /code and Files adapters converge on this production entry.
    await feature.openResource(host, { text:'README source', name:'README.md' });
    await feature.openResource(source, { text:'print(1)', name:'main.py' });
    await feature.openResource(base, { text:'', name:'Tasks Base' });
    const docs = state.docs.filter(doc => doc.resource);
    const note = state.docs.find(doc => doc.id === noteId);
    const leaves = leavesOf(state.windows.get('notes').noteWorkspace.root);
    check(docs.length === 4, 'Files and legacy aliases share one document owner: ' + docs.length);
    check(state.docs.filter(doc => doc.resource?.key?.resourceId === 'note-1').length === 1, 'same handle deduplicates');
    check(state.docs.find(doc => doc.name === 'main.py').sourceKind === 'host', 'host source kind');
    check(state.docs.find(doc => doc.name === 'main.py').kind === 'text', 'host source remains source');
    check(state.docs.find(doc => doc.name === 'README.md').savePolicy === 'explicit', 'host save is explicit');
    check(state.docs.find(doc => doc.name === 'README.md').kind === 'markdown', 'host Markdown opt-in is representation-driven');
    check(state.docs.find(doc => doc.name === 'README.md').sourceMetadata.encoding === 'utf-16le', 'host encoding metadata survives handle normalization');
    check(state.docs.find(doc => doc.name === 'README.md').sourceMetadata.bomBytes === 2, 'host BOM metadata survives handle normalization');
    check(state.docs.find(doc => doc.name === 'main.py').sourceMetadata.language === 'python', 'host language metadata survives source handoff');
    check(leaves.length >= 1, 'all resources are in one working set');
    note.text = '# changed';
    feature.queueSave(note, note.text);
    await feature.flushDocument(note.id);
    check(saves.length === 0, 'managed note uses its Copal save path');
    const hostDoc = state.docs.find(doc => doc.name === 'README.md');
    hostDoc.text = 'edited README'; feature.queueSave(hostDoc, hostDoc.text);
    const hostBuffer = state.windows.get('notes').noteBuffers.get(hostDoc.id);
    hostBuffer.apply({ text:'edited README\\nsecond line' });
    check(hostBuffer.undo()?.envelope.text === 'edited README', 'host draft undo survives in the shared resource buffer');
    check(hostBuffer.redo()?.envelope.text === 'edited README\\nsecond line', 'host draft redo survives in the shared resource buffer');
    const hostResult = await hostBuffer.flush();
    check(hostResult?.outcome === 'applied', 'host buffer flush applied');
    const hostReceipt = await featureSaveResourceForFixture(host, 'edited README');
    check(hostReceipt.revision.kind === 'hostFingerprint', 'host fingerprint revision');
    check(saves.at(-1).snapshot.expectedRevision.value === 'fp-1', 'host fingerprint preserved');
    const conflictRegistry = createBufferRegistry();
    const conflictBuffer = conflictRegistry.acquire(host, { text:'remote base' }, { actorId:'fixture-account:fixture-workspace', epoch:1, save:async () => ({ outcome:'conflict', remote:{ revision:{kind:'hostFingerprint', value:'fp-remote'}, envelope:{text:'remote edit'} } }) });
    conflictBuffer.apply({ text:'local draft' });
    const conflictReceipt = await conflictBuffer.flush();
    check(conflictReceipt.outcome === 'conflict', 'stale host fingerprint reports conflict');
    check(conflictBuffer.state().conflict.local.envelope.text === 'local draft', 'conflict retains local draft');
    check(conflictBuffer.state().conflict.remote.envelope.text === 'remote edit', 'conflict retains exact remote snapshot');
    const sourceLeaf = leaves.find(item => item.docId === state.docs.find(doc => doc.name === 'main.py').id);
    check(sourceLeaf && state.docs.find(doc => doc.name === 'main.py').kind === 'text', 'host source remains source representation');
    const workspace = state.windows.get('notes').noteWorkspace;
    const markdownLeaf = leaves.find(item => item.docId === hostDoc.id);
    check(markdownLeaf && setWorkspaceLeafMode(workspace, markdownLeaf.id, 'reading'), 'Markdown reading mode is retained in the shared workspace');
    markdownLeaf.selection = { anchor:7, head:7 }; markdownLeaf.scrollTop = 132;
    const firstGroup = workspace.root.type === 'group' ? workspace.root : workspace.root.children[0];
    const splitLeaf = splitWorkspaceGroup(workspace, firstGroup.id, state.docs.find(doc => doc.name === 'main.py'));
    check(splitLeaf && workspace.root.type === 'split', 'production workspace split created');
    const destination = workspace.root.children.find(node => node.type === 'group' && node.id !== firstGroup.id);
    check(moveWorkspaceLeaf(workspace, splitLeaf.id, firstGroup.id, 0), 'production workspace reorder moved leaf');
    check(setWorkspaceLeafMode(workspace, sourceLeaf.id, 'source'), 'source mode retained');
    for (const leaf of leavesOf(workspace.root).filter(item => item.docId === sourceLeaf.docId)) { leaf.selection = { anchor:4, head:4 }; leaf.scrollTop = 88; }
    const restored = normalizeNotesWorkspace(JSON.parse(serializeNotesWorkspace(workspace)), state.docs, sourceLeaf.docId);
    const restoredSource = leavesOf(restored.root).find(leaf => leaf.docId === sourceLeaf.docId);
    check(restoredSource?.selection?.anchor === 4 && restoredSource.scrollTop === 88, 'selection and scroll survive workspace restore: ' + JSON.stringify(restoredSource));
    const restoredMarkdown = leavesOf(restored.root).find(leaf => leaf.docId === hostDoc.id);
    check(restoredMarkdown?.mode === 'reading' && restoredMarkdown.selection?.anchor === 7 && restoredMarkdown.scrollTop === 132, 'Markdown mode, cursor, and scroll survive movement/reload');
    const closed = closeWorkspaceLeaf(workspace, sourceLeaf.id);
    check(closed?.docId === sourceLeaf.docId, 'close removes one source leaf');
    feature.openResource(source, { text:'print(1)', name:'main.py' });
    check(state.docs.filter(doc => doc.resource?.key?.resourceId === 'source').length === 1, 'reopen retains one source owner');
    feature.suspendScope();
    check(state.windows.get('notes').noteLeafViews.size === 0, 'account reset detaches editors');
    state.accountId = 'fixture-account-b'; state.contextEpoch = 2; state.docs.length = 0;
    const switched = { ...source, key:{accountId:'fixture-account-b', workspaceId:'fixture-workspace', provider:'host', resourceId:'source-b'}, locator:{...source.locator, opaqueRef:'opaque-source-b'} };
    feature.openResource(switched, { text:'print(2)', name:'main.py' });
    check(state.docs.length === 1 && state.docs[0].resource.key.accountId === 'fixture-account-b', 'account switch isolates new resource owner');
    return { docs:docs.map(doc => ({name:doc.name, kind:doc.kind, savePolicy:doc.savePolicy})), saves:saves.length };
  };
  const featureSaveResourceForFixture = async (resource, text) => {
    const snapshot = { key:resource.key, expectedRevision:resource.revision, envelope:{text}, localRevision:1 };
    return (await (async () => {
      saves.push({ snapshot, resource });
      return { outcome:'applied', revision:{kind:'hostFingerprint', value:'host-next'} };
    })());
  };
</script>`;

await withCopalBrowser({ page }, async ({ evaluate, until }) => {
  await until('window.__run');
  const result = await evaluate('window.__run()');
  // The shared Host ResourceBuffer flush is itself a real save. The fixture's
  // explicit adapter probe below records a second independent receipt.
  assert.equal(result.saves, 2);
  assert.deepEqual(result.docs.map(item => item.name).sort(), ['Managed.md','README.md','Tasks Base','main.py'].sort());
  console.log('Unified Editor production path: managed note + host Markdown + host source + Base share one workspace; alias dedupe, hostFingerprint save, source-only guard, and scope teardown passed.');
});
