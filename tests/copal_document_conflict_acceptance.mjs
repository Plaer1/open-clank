import assert from 'node:assert/strict';
import fs from 'node:fs';
import { withCopalBrowser } from './helpers/copal_browser_fixture.mjs';

const resource = { key:{ accountId:'account-a', workspaceId:'default', provider:'copal', resourceId:'note-a' }, revision:{ kind:'copalHead', value:'head-original' }, locator:{ displayName:'Note', locationLabel:'Note' }, representation:'nativeNote', capabilities:{ read:true, edit:true } };
let stored = { id:'note-a', name:'Note', kind:'note', text:'remote body', properties:{ side:'remote' }, relations:[{ target:'Remote', origin:'explicit', kind:'link' }], links:[], head:'head-remote', resource:{ ...resource, revision:{ kind:'copalHead', value:'head-remote' } } };
const requests = [];
let holdResponse = false;
let releaseResponse = null;
const page = `<!doctype html><link rel="stylesheet" href="/static/style.css"><script type="module">
import Copal, { conflictFixture } from '/static/js/copal.js';
window.Copal = Copal; window.fixture = conflictFixture;
</script>`;
const overrides = { '/static/js/copal.js':fs.readFileSync('static/js/copal.js', 'utf8') + '\nexport const conflictFixture = {state, notesFeature, loadDocuments, showDocumentConflict};\n' };
const request = async (req, res) => {
  if (!req.url.startsWith('/api/')) return false;
  const pathname = new URL(req.url, 'http://fixture').pathname;
  const json = value => { res.setHeader('content-type', 'application/json'); res.end(JSON.stringify(value)); };
  if (pathname.endsWith('/status')) json({ account_id:'account-a', storage_namespace:'user:a' });
  else if (pathname.endsWith('/copal_entry_visibility')) json({ value:{} });
  else if (pathname.endsWith('/events')) { res.writeHead(200, { 'content-type':'text/event-stream' }); res.write(': fixture\n\n'); }
  else if (pathname.endsWith('/planning')) json({ tracks:[], floatingTodos:[] });
  else if (pathname.endsWith('/documents')) json({ docs:[stored] });
  else if (pathname.endsWith('/documents/note-a') && req.method === 'PUT') {
    let body = ''; for await (const part of req) body += part;
    const payload = JSON.parse(body); requests.push(payload);
    if (payload.base !== stored.head) { res.statusCode = 409; json({ detail:{ outcome:'stale', doc:stored } }); }
    else {
      stored = { ...stored, text:payload.content, properties:payload.properties, relations:payload.relations, head:`head-saved-${requests.length}`, resource:{ ...resource, revision:{ kind:'copalHead', value:`head-saved-${requests.length}` } } };
      const receipt = { outcome:'committed', actionId:payload.actionId, doc:stored };
      if (holdResponse) await new Promise(resolve => { releaseResponse = resolve; });
      json(receipt);
    }
  } else if (pathname.endsWith('/documents/note-a')) json(stored);
  else json({});
  return true;
};

await withCopalBrowser({ page, overrides, request }, async ({ evaluate, until }) => {
  await until('Boolean(window.fixture)');
  await evaluate('Copal.init();');
  await evaluate('fixture.loadDocuments(false);');
  await evaluate(`window.remote = structuredClone(fixture.state.docs[0]); fixture.notesFeature.getSettings(); fixture.state.docs[0].properties = {side:'local'}; fixture.notesFeature.queueSave(fixture.state.docs[0], 'local body'); fixture.showDocumentConflict(fixture.state.docs[0], 'local body', window.remote, 'notes')`);
  await evaluate(`fixture.state.docs[0].properties = {side:'newer'}; fixture.notesFeature.queueSave(fixture.state.docs[0], 'newer body'); [...document.querySelectorAll('dialog[open] button')].find(b=>b.textContent==='Save mine over latest').click()`);
  assert.equal(requests.length, 0, 'changed draft needs a new comparison before writing');
  assert((await evaluate('document.querySelector("dialog[open]").innerText')).includes('newer body'));
  await evaluate(`[...document.querySelectorAll('dialog[open] button')].find(b=>b.textContent==='Save mine over latest').click()`);
  await until('fixture.state.windows.get("notes").noteDrafts.size === 0');
  assert.equal(requests.length, 1);
  assert.equal(requests[0].content, 'newer body');
  assert.deepEqual(requests[0].properties, { side:'newer' });
  assert.equal(requests[0].base, 'head-remote');
  assert.equal(await evaluate('fixture.state.windows.get("notes").noteBuffers.get("note-a").state().dirty'), false);

  stored = { ...stored, head:'remote-again', text:'remote again', properties:{ side:'remote-again' }, relations:[{ target:'Elsewhere', origin:'explicit', kind:'embed' }], resource:{ ...resource, revision:{ kind:'copalHead', value:'remote-again' } } };
  await evaluate(`window.remote = ${JSON.stringify(stored)}; fixture.state.docs[0].properties = {side:'discard-me'}; fixture.notesFeature.queueSave(fixture.state.docs[0], 'discard local'); fixture.showDocumentConflict(fixture.state.docs[0], 'discard local', window.remote, 'notes'); [...document.querySelectorAll('dialog[open] button')].find(b=>b.textContent==='Load latest').click()`);
  await until('fixture.state.windows.get("notes").noteDrafts.size === 0');
  const latest = await evaluate('({doc:fixture.state.docs[0],envelope:fixture.state.windows.get("notes").noteBuffers.get("note-a").envelope})');
  assert.equal(latest.envelope.text, 'remote again');
  assert.deepEqual(latest.envelope.properties, { side:'remote-again' });
  assert.deepEqual(latest.envelope.relations, stored.relations);
  assert.deepEqual(latest.doc.properties, { side:'remote-again' });
  assert.equal(requests.length, 1, 'Load latest must not write');
  holdResponse = true;
  await evaluate(`window.remote = ${JSON.stringify(stored)};fixture.notesFeature.queueSave(fixture.state.docs[0], 'reviewed overwrite');fixture.showDocumentConflict(fixture.state.docs[0], 'reviewed overwrite', window.remote, 'notes');[...document.querySelectorAll('dialog[open] button')].find(b=>b.textContent==='Save mine over latest').click()`);
  for(let attempt=0;!releaseResponse&&attempt<100;attempt++) await new Promise(resolve=>setTimeout(resolve,10));
  assert(releaseResponse, 'reviewed write did not reach the fixture');
  await evaluate(`fixture.state.docs[0].properties={side:'edited during save'};fixture.notesFeature.queueSave(fixture.state.docs[0], 'newer edit while saving')`);
  await new Promise(resolve=>setTimeout(resolve,800));
  assert.equal(requests.length,2,'autosave must wait for the reviewed overwrite receipt');
  holdResponse=false;releaseResponse();
  await until('fixture.state.windows.get("notes").noteDrafts.size === 0');
  assert.equal(requests.length,3);
  assert.equal(requests[2].base,'head-saved-2');
  assert.equal(stored.text,'newer edit while saving');
  assert.deepEqual(stored.properties,{side:'edited during save'});
  console.log('Copal full-envelope conflict comparison and resolution browser checks passed');
});
