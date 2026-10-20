import assert from 'node:assert/strict';
import { withCopalBrowser } from './helpers/copal_browser_fixture.mjs';

const page = `<!doctype html><meta charset="utf-8">
<button id="workspace-indicator-btn"><span id="workspace-indicator-name"></span></button>
<button id="overflow-workspace-btn"></button><div id="toast"></div><script type="module" src="/fixture.js"></script>`;
const fixture = `
import * as workspace from '/static/js/workspace.js';
window.audit = { workspace, calls: [], statuses: [], pending: new Map(), incognito: false, sessionId: '' };
window.sessionModule = { getCurrentSessionId: () => audit.sessionId, isCurrentSessionIncognito: () => audit.incognito };
window.addEventListener('workspace-index-status', event => audit.statuses.push(event.detail));
window.fetch = async (input, options = {}) => {
  const url = new URL(String(input), location.origin);
  const body = options.body ? JSON.parse(options.body) : {};
  audit.calls.push({ path: url.pathname, body });
  const json = (data, status = 200) => new Response(JSON.stringify(data), { status, headers:{'Content-Type':'application/json'} });
  const match = url.pathname.match(/workspaces\\/([^/]+)\\/index\\/([^/]+)/);
  if (match) {
    if (match[2] === 'open' && match[1] === 'workspace-rejected') return json({detail:{code:'index_request_rejected',message:'Erased index requires explicit admission'}},409);
    if (match[2] === 'open' && match[1] === 'workspace-unavailable') return json({detail:{code:'index_unavailable',message:'Index owner unavailable'}},503);
    if (match[2] === 'open' && match[1] === 'workspace-a') return new Promise(resolve => audit.pending.set(body.subscriber_id, () => resolve(json({index:{workspace_id:match[1],lifecycle:'ready'}}))));
    return json({index:{workspace_id:match[1],lifecycle:'queued'}});
  }
  return json({workspace:{id:'workspace-a',path:'/allowed/a'}});
};
window.auditReady = true;
`;

await withCopalBrowser({ page, overrides: { '/fixture.js': fixture } }, async ({ evaluate, until }) => {
  await until('window.auditReady');
  // Registration and resolution do not emit opened-workspace events.
  await evaluate(`(async () => { await audit.workspace.bindWorkspacePath('/allowed/a'); await audit.workspace.resolveWorkspaceId('workspace-a'); })()`);
  assert.equal(await evaluate(`audit.calls.filter(call => call.path.includes('/index/')).length`), 0);
  // A held native acknowledgement cannot hold the workspace selection UI.
  await evaluate(`audit.workspace.setWorkspace('/allowed/a','workspace-a',{persist:false})`);
  assert.equal(await evaluate(`document.getElementById('workspace-indicator-name').textContent`), 'a');
  await until('audit.pending.size === 1');
  await evaluate(`audit.workspace.setWorkspace('/allowed/b','workspace-b',{persist:false})`);
  await until(`audit.statuses.some(status => status.workspaceId === 'workspace-b')`);
  await evaluate(`audit.pending.values().next().value()`);
  await until(`audit.calls.filter(call => call.path.endsWith('/index/release')).length >= 2`);
  assert.deepEqual(await evaluate('audit.statuses.map(status => status.workspaceId)'), ['workspace-b']);
  assert.equal(await evaluate('audit.workspace.getWorkspaceId()'), 'workspace-b');
  await evaluate('audit.workspace.clearWorkspace({persist:false})');
  await until(`audit.calls.some(call => call.path.includes('workspace-b/index/release'))`);
  assert.equal(await evaluate(`document.getElementById('workspace-indicator-btn').style.display`), 'none');
  // Incognito selection neither opens nor appends any code evidence.
  const opens = await evaluate(`audit.calls.filter(call => call.path.endsWith('/index/open')).length`);
  await evaluate(`audit.incognito = true; audit.workspace.setWorkspace('/allowed/private','workspace-private',{persist:false})`);
  assert.equal(await evaluate(`audit.calls.filter(call => call.path.endsWith('/index/open')).length`), opens);
  assert.equal(await evaluate(`audit.statuses.length`), 1);
  // Reopening A owns a fresh subscriber; the previous release cannot cancel it.
  await evaluate(`audit.incognito = false; audit.workspace.setWorkspace('/allowed/a','workspace-a',{persist:false})`);
  await until('audit.pending.size === 2');
  assert.equal(await evaluate(`new Set(audit.calls.filter(call=>call.path.endsWith('/index/open')).map(call=>call.body.subscriber_id)).size`), 3);
  await evaluate('window.dispatchEvent(new Event("pagehide"))');
  await until(`audit.calls.filter(call => call.path.endsWith('/index/release')).length >= 4`);
  await evaluate(`audit.workspace.setWorkspace('/allowed/rejected','workspace-rejected',{persist:false})`);
  await until(`audit.statuses.some(status=>status.workspaceId==='workspace-rejected')`);
  assert.deepEqual(await evaluate(`audit.statuses.find(status=>status.workspaceId==='workspace-rejected').index`), {
    lifecycle:'failed',error:{code:'index_request_rejected',message:'Erased index requires explicit admission'},
  });
  await evaluate(`audit.workspace.setWorkspace('/allowed/unavailable','workspace-unavailable',{persist:false})`);
  await until(`audit.statuses.some(status=>status.workspaceId==='workspace-unavailable')`);
  assert.equal(await evaluate(`audit.statuses.find(status=>status.workspaceId==='workspace-unavailable').index.lifecycle`),'unavailable');
  console.log('Code index workspace acceptance: 8 mounted browser groups passed');
});

const editorFixture = `
import * as workspace from '/static/js/workspace.js';
window.audit = { workspace, calls: [], statuses: [], pending: [], selected: 'editor-refresh', activeSubscribers: new Set() };
window.addEventListener('editor-workspace-index-status', event => audit.statuses.push(event.detail));
window.fetch = async (input, options = {}) => {
  const url = new URL(String(input), location.origin), body = options.body ? JSON.parse(options.body) : {};
  const json = value => new Response(JSON.stringify(value), {status:200,headers:{'Content-Type':'application/json'}});
  audit.calls.push({path:url.pathname,body});
  if (url.pathname === '/api/auth/status') return json({ok:true,username:'alice',is_admin:false});
  if (url.pathname === '/api/files-v1/workspace') return json({workspace:{id:audit.selected},open_relative:''});
  if (url.pathname.includes('/resolve')) return json({workspace:{id:audit.selected,path:'/allowed/' + audit.selected}});
  if (url.pathname === '/api/files-v1/workspace-resource') return json({workspace:{id:audit.selected},parent:{ref:'rr1.folder',kind:'folder'},resource:{ref:'rr1.folder',kind:'folder'}});
  if (url.pathname === '/api/files-v1/children') return json({entries:[],policy_generation:1,page:{next_cursor:null}});
  if (url.pathname.endsWith('/index/open') && body.purpose === 'app_folder') return new Promise(resolve => audit.pending.push(() => {
    audit.activeSubscribers.add(body.subscriber_id);
    resolve(json({index:{lifecycle:'ready'}}));
  }));
  if (url.pathname.includes('/index/')) {
    if (url.pathname.endsWith('/open')) audit.activeSubscribers.add(body.subscriber_id);
    if (url.pathname.endsWith('/release')) audit.activeSubscribers.delete(body.subscriber_id);
    return json({index:{lifecycle:'queued'}});
  }
  return json({});
};
await workspace.setWorkspace('/allowed/chat', 'chat-workspace', {persist:false});
const code = await import('/static/js/codeEditor.js');
audit.code = code;
window.auditReady = true;
`;

await withCopalBrowser({ page, overrides: { '/fixture.js': editorFixture } }, async ({ evaluate, until }) => {
  await until('window.auditReady');
  await evaluate(`audit.code.openResource('rr1.folder')`);
  await until('audit.pending.length === 1');
  const reads = await evaluate(`audit.calls.filter(call=>call.path==='/api/files-v1/children').length`);
  await evaluate(`document.querySelector('button[title=Refresh]').click()`);
  await until(`audit.calls.filter(call=>call.path==='/api/files-v1/children').length > ${reads}`);
  await evaluate(`audit.pending[0]()`);
  await until(`audit.statuses.length === 1`);
  assert.equal(await evaluate('audit.statuses[0].workspaceId'),'editor-refresh');
  assert.equal(await evaluate(`audit.calls.some(call=>call.path.includes('editor-refresh/index/release'))`),false);
  assert.equal(await evaluate('audit.activeSubscribers.size'),2,'same-root refresh preserves Editor and chat subscribers');
  await evaluate(`audit.selected = 'editor-a'; audit.code.openResource('rr1.folder')`);
  await until('audit.pending.length === 2');
  assert.equal(await evaluate(`document.querySelector('[data-code-root]').textContent`), '/allowed/editor-a');
  assert.equal(await evaluate(`audit.workspace.getWorkspaceId()`), 'chat-workspace');
  assert.equal(await evaluate(`audit.calls.some(call => call.path.endsWith('/index/open') && call.body.purpose === 'app_folder')`), true);
  await evaluate(`audit.selected = 'editor-b'; audit.code.openResource('rr1.folder')`);
  await until('audit.pending.length === 3');
  assert.equal(await evaluate(`document.querySelector('[data-code-root]').textContent`), '/allowed/editor-b');
  await evaluate(`audit.pending[1]()`);
  await until(`audit.calls.filter(call => call.path.includes('editor-a/index/release')).length >= 2`);
  assert.equal(await evaluate('audit.statuses.length'), 1);
  await evaluate('audit.code.default.close()');
  await until(`audit.calls.some(call => call.path.includes('editor-b/index/release'))`);
  await evaluate(`audit.pending[2]()`);
  await until(`audit.calls.filter(call => call.path.includes('editor-b/index/release')).length >= 2`);
  assert.equal(await evaluate('audit.activeSubscribers.size'), 1, 'late native registration must be removed while chat remains subscribed');
  assert.equal(await evaluate('audit.statuses.length'), 1);
  assert.equal(await evaluate(`audit.calls.some(call => call.path.includes('chat-workspace/index/release'))`), false);
  assert.equal(await evaluate('audit.workspace.getWorkspaceId()'), 'chat-workspace');
  console.log('Code index Editor acceptance: 4 mounted browser groups passed');
});
