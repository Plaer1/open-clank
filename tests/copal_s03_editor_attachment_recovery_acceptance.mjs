#!/usr/bin/env node

import assert from 'node:assert/strict';
import test from 'node:test';
import { withCopalBrowser } from './helpers/copal_browser_fixture.mjs';

const page = `<!doctype html><meta charset="utf-8"><body><main id="app"></main>
<script type="module">
  window.__run = async () => {
    const copal = await import('/static/js/copal.js?attachment-recovery');
    const files = await import('/static/js/files.js?attachment-recovery');
    window.__copal = copal.default;
    window.__files = files.default;
    try {
      await copal.init(location.origin);
      await copal.default.open('notes');
      await copal.default.openResource('rr-target');
      await files.default.open();
      return true;
    } catch (error) {
      window.__runError = String(error?.stack || error);
      throw error;
    }
  };
</script></body>`;

const targetResource = {
  ref:'rr-target', key:{ accountId:'account-owner', workspaceId:'default', provider:'copal', resourceId:'target-1' },
  revision:{ kind:'copalHead', value:'target-1' }, representation:'markdown',
  locator:{ opaqueRef:'rr-target', displayName:'Draft.md', locationLabel:'default' },
  capabilities:{ read:true, write:true, edit:true },
};

test('S03 recovers a committed Files attachment after the prepare response is lost', async () => {
    const state = { prepareRequests:0, receiptRequests:0, insertionRequests:0, payload:null, preparation:null, committed:false, receiptMode:'complete', operationIds:[] };
  const json = (res, value, status = 200) => { res.writeHead(status, {'content-type':'application/json'}); res.end(JSON.stringify(value)); return true; };
  const readJson = async req => { const chunks=[]; for await (const chunk of req) chunks.push(chunk); return chunks.length ? JSON.parse(Buffer.concat(chunks).toString()) : {}; };
  await withCopalBrowser({ page, request:async (req, res) => {
    const url = new URL(req.url, 'http://fixture');
    if (url.pathname === '/api/auth/status') return json(res, { username:'account-owner', is_admin:true });
    if (url.pathname === '/api/copal/status') return json(res, { storage_namespace:'attachment-recovery', account_id:'account-owner' });
    if (url.pathname === '/api/prefs/copal_entry_visibility') return json(res, { value:{} });
    if (url.pathname === '/api/copal/planning') return json(res, { tracks:[], floatingTodos:[] });
    if (url.pathname === '/api/copal/documents' && req.method === 'GET') return json(res, { docs:[] });
    if (url.pathname === '/api/copal/events') { res.writeHead(200, {'content-type':'text/event-stream'}); res.end(': fixture\n\n'); return true; }
    if (url.pathname === '/api/files-v1/roots') return json(res, {
      version:1, policy_generation:7,
      entries:[{ id:'root-1', ref:'rr-root', provider:'host', kind:'provider_root', name:'Host', capabilities:['children','stat'], sort_keys:['name'] }],
    });
    if (url.pathname === '/api/files-v1/children') return json(res, {
      entries:[{ id:'source-1', ref:'rr-source', provider:'host', kind:'file', name:'source.txt', capabilities:['stat','read','open','copy'], revision:{ kind:'hostFingerprint', value:'source-1' }, mime_type:'text/plain' }],
      next_cursor:null,
    });
    if (url.pathname === '/api/files-v1/places' || url.pathname === '/api/files-v1/workspaces') return json(res, { entries:[] });
    if (url.pathname === '/api/files-v1/open-resource' && req.method === 'POST') return json(res, {
      resource:targetResource, payload:{ name:'Draft.md', kind:'markdown', corpus:'copal', text:'Hello ', representation:'markdown', resource:targetResource },
    });
    if (url.pathname === '/api/files-v1/stat' && req.method === 'POST') return json(res, {
      id:'source-1', ref:'rr-source', provider:'host', kind:'file', name:'source.txt', capabilities:['stat','read','open','copy'], revision:{ kind:'hostFingerprint', value:'source-1' }, mime_type:'text/plain',
    });
    if (url.pathname === '/api/files-v1/attachments/prepare' && req.method === 'POST') {
      state.prepareRequests += 1; state.payload = await readJson(req); state.operationIds.push(state.payload.operation_id);
      state.preparation = {
        operation_id:state.payload.operation_id, generation:state.payload.generation,
        preparation_receipt_id:'prep-recovered-1', source_revision:state.payload.source.expected_revision,
        target_identity:{ resource_ref:'rr-target', kind:'copal_document' }, target_revision:state.payload.target.expected_revision,
        insertion:{ format:'markdown', link_target:'asset-recovered-1', label:'source.txt', media_kind:'text/plain' },
        asset:{ resource_key:{ provider:'copal', account_id:'account-owner', workspace_id:'default', resource_id:'asset-recovered-1' }, mime_type:'text/plain', name:'asset-recovered-1' },
        history:{ action_id:'action-recovered-1', status:'complete', phase:'complete' },
      };
      state.committed = true;
      // The provider has committed the preparation, then the transport drops
      // before a response reaches fetch. The Editor must recover only through
      // the typed GET endpoint; no synthetic 503 body is available to parse.
      // Flush an error status first so the browser cannot transparently retry
      // the POST, then truncate the response body to model a lost payload.
      res.writeHead(503, {'content-type':'application/json', connection:'close'});
      res.flushHeaders();
      res.destroy();
      return true;
    }
    if (url.pathname.startsWith('/api/files-v1/attachments/') && req.method === 'GET') {
      state.receiptRequests += 1;
      if (state.receiptMode === 'malformed') return json(res, {
        operation_id:url.pathname.split('/').pop(), generation:state.payload?.generation ?? 7, state:'complete',
        preparation:{ ...state.preparation, insertion:{ format:'legacy', text:'must never be consumed' } },
      });
      return json(res, { operation_id:url.pathname.split('/').pop(), generation:state.payload?.generation ?? 7, state:'complete', preparation:state.preparation });
    }
    if (url.pathname === '/fixture/receipt-mode') { state.receiptMode = url.searchParams.get('mode') || 'complete'; return json(res, { ok:true }); }
    if (url.pathname === '/api/copal/documents/note-1') { state.insertionRequests += 1; return json(res, {}); }
    return false;
  }}, async ({ evaluate, until }) => {
    await evaluate('void window.__run()');
    await until('window.__runError || (window.__copal && window.__files)');
    assert.equal(await evaluate('window.__runError || null'), null);
    await until('!!document.querySelector(".cm-content")');
    await new Promise(resolve => setTimeout(resolve, 1000));
    assert.equal(await evaluate('!!document.querySelector("[data-files-selection-key]")'), true, 'Files rows must be mounted');
    await evaluate(`(() => {
      const source = document.querySelector('[data-file-name="source.txt"]') || [...document.querySelectorAll('[data-files-selection-key]')].find(row => row.dataset.resourceRef === 'rr-source');
      const dataTransfer = new DataTransfer();
      source.dispatchEvent(new DragEvent('dragstart', { bubbles:true, cancelable:true, dataTransfer, altKey:true }));
      window.__dragContext = window.__openClankFilesTransferContext();
      window.__attachmentDataTransfer = dataTransfer;
      const host = document.querySelector('.copal-notes-window:not(.hidden) .copal-codemirror-host');
      host.dispatchEvent(new DragEvent('drop', { bubbles:true, cancelable:true, dataTransfer }));
    })()`);
    await new Promise(resolve => setTimeout(resolve, 2000));
    assert.equal(state.prepareRequests, 1, 'one preparation POST');
    assert.equal(state.committed, true, 'the preparation was committed before transport loss');
    assert.equal(new Set(state.operationIds).size, 1, 'transport retries reuse one stable preparation operation');
    await new Promise(resolve => setTimeout(resolve, 2000));
    assert.equal(state.receiptRequests, 1, 'one typed recovery GET');
    await until('document.querySelector(".cm-content")?.textContent.includes("asset-recovered-1")');
    assert.equal(await evaluate('window.__dragContext && Object.isFrozen(window.__dragContext)'), true);
    assert.equal(await evaluate('window.__dragContext?.workspace'), 'default');
    assert.equal(state.prepareRequests, 1, 'one preparation POST');
    assert.equal(state.receiptRequests, 1, 'one typed preparation recovery GET');
    assert.equal(await evaluate('document.querySelectorAll(".cm-content").length'), 1, 'one Editor buffer');
    assert.equal(await evaluate('document.querySelector(".cm-content").textContent.match(/asset-recovered-1/g)?.length'), 1, 'one insertion');
    await evaluate(`document.querySelector('.copal-notes-window:not(.hidden) .copal-codemirror-host').dispatchEvent(new DragEvent('drop', { bubbles:true, cancelable:true, dataTransfer:window.__attachmentDataTransfer }))`);
    await new Promise(resolve => setTimeout(resolve, 250));
    assert.equal(await evaluate('document.querySelector(".cm-content").textContent.match(/asset-recovered-1/g)?.length'), 1, 'repeated drop cannot insert twice');
    assert.equal(state.prepareRequests, 1, 'repeated drop cannot prepare twice');
    await evaluate("fetch('/fixture/receipt-mode?mode=malformed')");
    const beforeHostile = await evaluate('JSON.stringify(window.__openClankFilesTransferContext()?.selectedKeys || [])');
    await evaluate(`(() => {
      const source = document.querySelector('[data-file-name="source.txt"]') || [...document.querySelectorAll('[data-files-selection-key]')].find(row => row.dataset.resourceRef === 'rr-source');
      const dataTransfer = new DataTransfer();
      source.dispatchEvent(new DragEvent('dragstart', { bubbles:true, cancelable:true, dataTransfer, altKey:true }));
      document.querySelector('.copal-notes-window:not(.hidden) .copal-codemirror-host').dispatchEvent(new DragEvent('drop', { bubbles:true, cancelable:true, dataTransfer }));
    })()`);
    await new Promise(resolve => setTimeout(resolve, 1200));
    assert.equal(state.receiptRequests, 2, 'hostile recovery still uses the typed GET once');
    assert.equal(await evaluate('document.querySelector(".cm-content").textContent.match(/asset-recovered-1/g)?.length'), 1, 'malformed recovery cannot insert');
    assert.equal(await evaluate('JSON.stringify(window.__openClankFilesTransferContext()?.selectedKeys || [])'), beforeHostile, 'malformed recovery preserves Files selection');
    console.log('S03 Files→Editor lost preparation response recovered through typed receipt with one insertion.');
  });
});
