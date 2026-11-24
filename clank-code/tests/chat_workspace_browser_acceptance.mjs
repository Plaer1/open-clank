#!/usr/bin/env node
import assert from 'node:assert/strict';
import { withCopalBrowser } from './helpers/copal_browser_fixture.mjs';

const page = `<!doctype html><meta charset="utf-8"><link rel="stylesheet" href="/static/style.css"><style>
body{margin:0;background:#17202a;color:#eee}.chat-container{height:300px}.chat-history{height:80px!important;max-height:80px;overflow:auto!important}.chat-history>div{height:640px!important;min-height:640px}.copal-tool-modal{position:fixed;display:flex}.copal-modal-content{width:420px;height:260px}
</style><main id="chat-container" class="chat-container"><div class="chat-meta-overlay"><span id="current-meta">Chat</span><span class="export-dropdown-wrap"><button id="export-dl-btn" type="button">More</button><div id="export-dropdown-menu" class="export-dropdown-menu"></div></span></div><div id="chat-history" class="chat-history"><div></div></div><textarea id="message"></textarea></main><script>window.addEventListener('error',e=>window.auditError=e.message);window.addEventListener('unhandledrejection',e=>window.auditError=String(e.reason));</script><script type="module" src="/fixture.js"></script>`;

const fixture = `
import chatWorkspace from '/static/js/chatWorkspace.js?workspace-fixture';
import { createOpenClankWindow } from '/static/js/copal/windows.js?workspace-fixture';
import { applyEdgeDock } from '/static/js/modalSnap.js?workspace-fixture';
window.audit = { chatWorkspace, createOpenClankWindow, applyEdgeDock };
window.auditReady = true;
`;

await withCopalBrowser({ page, overrides: { '/fixture.js': fixture } }, async ({ evaluate, until }) => {
  await until('Boolean(window.auditReady || window.auditError)');
  assert.equal(await evaluate('window.auditError || null'), null);
  assert.equal(await evaluate('document.querySelectorAll("#chat-minimize-btn").length'), 1);
  assert.equal(await evaluate('document.querySelectorAll("#chat-close-btn").length'), 1);

  const before = await evaluate(`(() => {
    const input = document.getElementById('message');
    const history = document.getElementById('chat-history');
    history.style.cssText = 'height:80px!important;max-height:80px!important;overflow:auto!important';
    history.innerHTML = '<div style="height:1000px!important;min-height:1000px!important"></div>';
    input.value = 'draft survives'; history.scrollTop = history.scrollHeight - history.clientHeight; input.focus();
    return { draft: input.value, scroll: history.scrollTop };
  })()`);
  await evaluate('document.getElementById("chat-minimize-btn").click()');
  const minimized = await evaluate(`(() => ({
    hidden: document.body.classList.contains('chat-workspace-hidden'),
    inert: document.getElementById('chat-container').inert === true,
    display: getComputedStyle(document.getElementById('chat-container')).display,
    focus: document.activeElement?.id,
    restore: document.getElementById('chat-workspace-restore')?.textContent,
    draft: document.getElementById('message').value,
    scroll: document.getElementById('chat-history').scrollTop,
    menuOpen: document.getElementById('export-dropdown-menu').classList.contains('open'),
  }))()`);
  assert.equal(minimized.hidden, true);
  assert.equal(minimized.inert, true);
  assert.equal(minimized.display, 'none');
  assert.equal(minimized.focus, 'chat-workspace-restore');
  assert.equal(minimized.restore, 'Restore chat');
  assert.equal(minimized.draft, before.draft);
  assert.equal(minimized.menuOpen, false);

  await evaluate('document.getElementById("chat-workspace-restore").click()');
  assert.deepEqual(await evaluate(`({
    hidden: document.body.classList.contains('chat-workspace-hidden'),
    inert: document.getElementById('chat-container').inert === true,
    draft: document.getElementById('message').value,
    scroll: document.getElementById('chat-history').scrollTop,
  })`), { hidden: false, inert: false, draft: before.draft, scroll: before.scroll });

  await evaluate('document.getElementById("chat-close-btn").click()');
  assert.equal(await evaluate('window.audit.chatWorkspace.chatWorkspaceMode()'), 'closed');
  await evaluate('document.dispatchEvent(new CustomEvent("odysseus:session-selected", { detail:{sessionId:"chat-reopened"} }))');
  assert.equal(await evaluate('document.body.classList.contains("chat-workspace-hidden")'), false);

  await evaluate('window.audit.chatWorkspace.minimizeChat()');
  const docked = await evaluate(`(() => {
    const left = window.audit.createOpenClankWindow({id:'workspace-left',label:'Editor',minWidth:280,minHeight:200}).show();
    const right = window.audit.createOpenClankWindow({id:'workspace-right',label:'Wiki',minWidth:280,minHeight:200}).show();
    window.audit.applyEdgeDock(left.root, 'left');
    window.audit.applyEdgeDock(right.root, 'right');
    const l = left.content.getBoundingClientRect(); const r = right.content.getBoundingClientRect();
    return { leftDock: left.root.classList.contains('modal-left-docked'), rightDock: right.root.classList.contains('modal-right-docked'), leftWidth:l.width, rightWidth:r.width, gap:r.left-l.right, chatHidden:document.body.classList.contains('chat-workspace-hidden') };
  })()`);
  assert.equal(docked.leftDock, true);
  assert.equal(docked.rightDock, true);
  assert.ok(docked.leftWidth > 0 && docked.rightWidth > 0, JSON.stringify(docked));
  assert.ok(docked.gap >= -1, `Copal docks overlap: ${JSON.stringify(docked)}`);
  assert.equal(docked.chatHidden, true);
  console.log(`Chat workspace browser acceptance: ${JSON.stringify({ before, minimized, docked })}`);
});
