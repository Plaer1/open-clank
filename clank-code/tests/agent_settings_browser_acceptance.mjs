import assert from 'node:assert/strict';
import { withCopalBrowser } from './helpers/copal_browser_fixture.mjs';
import { setTimeout as delay } from 'node:timers/promises';

const page = `<!doctype html><html><body><script type="module">
  window._isAdmin = false;
  const parsed = new DOMParser().parseFromString(await fetch('/static/index.html').then(r => r.text()), 'text/html');
  parsed.querySelector('#set-agentMaxTools')?.remove();
  parsed.querySelector('#set-agentMaxRounds')?.remove();
  parsed.querySelector('#set-agentMsg')?.remove();
  document.body.replaceChildren(parsed.getElementById('settings-modal'));
  const originalFetch = window.fetch;
  window.fetch = (input, options = {}) => {
    const headers = new Headers(options.headers || {});
    if (window.__agentRace) headers.set('X-Agent-Race', '1');
    return originalFetch(input, {...options, headers});
  };
  const module = await import('/static/js/settings.js?agent-settings-fixture');
  window.settingsModule = module.default;
  await module.open('ai');
  await window.__agentSettingsReady;
</script></body></html>`;

let reads = 0;
let writes = [];
let raceReads = 0;
const response = (res, value, status = 200) => {
  res.writeHead(status, {'content-type': 'application/json'});
  res.end(JSON.stringify(value));
  return true;
};

await withCopalBrowser({ page, cdpTimeoutMs:60000, request: async (req, res) => {
  const url = new URL(req.url, 'http://fixture');
  if (url.pathname === '/api/auth/settings' && req.method === 'GET') {
    reads += 1;
    if (req.headers['x-agent-race']) {
      raceReads += 1;
      if (raceReads === 1) await delay(100);
      return response(res, { agent_settings: {}, agent_settings_override: false, agent_settings_effective: {status:'available', value:{context:{hard:100000 + raceReads, usable:80000 + raceReads}, compaction:{tailTurns: raceReads === 1 ? 111 : 222, reserved:13000}}} });
    }
    const state = reads === 1
      ? { agent_settings: {}, agent_settings_override: false, agent_settings_effective: {status:'pending', reason:'CHAT_ROUTE_UNAVAILABLE'} }
      : { agent_settings: {compaction:{tail_turns: reads}}, agent_settings_override: reads < 4, agent_settings_effective: {status:'available', value:{context:{hard:100000, usable:80000}, compaction:{tailTurns:reads, reserved:13000}}} };
    return response(res, state);
  }
  if (url.pathname === '/api/auth/settings' && req.method === 'POST') {
    let body = '';
    for await (const chunk of req) body += chunk;
    writes.push(JSON.parse(body || '{}'));
    return response(res, {ok:true});
  }
  if (url.pathname === '/api/models/endpoints') return response(res, {items:[]});
  return false;
}}, async ({ evaluate, until }) => {
  await until(`document.querySelector('#set-agentNativeSave')`);
  await until(`/pending|Inherited/.test(document.querySelector('#set-agentNativeStatus').textContent)`);
  assert.match(await evaluate(`document.querySelector('#set-agentNativeStatus').textContent`), /pending|Inherited/);
  await evaluate(`document.querySelector('#set-agentTailTurns').value = '7'`);
  await evaluate(`document.querySelector('#set-agentNativeSave').click()`);
  await until(`document.querySelector('#set-agentNativeEffective').textContent.includes('80000')`);
  assert.equal(writes.at(-1).agent_settings.compaction.tail_turns, 7);
  assert.deepEqual(Object.keys(writes.at(-1).agent_settings.compaction), ['tail_turns']);
  assert.deepEqual(writes.at(-1).agent_settings.checkpoint, {});
  await evaluate(`document.querySelector('#set-agentNativeReset').click()`);
  await until(`document.querySelector('#set-agentNativeStatus').textContent.includes('Inherited')`);
  const before = reads;
  await evaluate(`window.__agentRace = true; window.dispatchEvent(new CustomEvent('openclank:default-chat-changed'))`);
  for (let attempt = 0; attempt < 40 && raceReads < 1; attempt += 1) await delay(5);
  assert.equal(raceReads, 1);
  await evaluate(`window.dispatchEvent(new CustomEvent('openclank:default-chat-changed'))`);
  await until(`document.querySelector('#set-agentNativeEffective').textContent.includes('222 tail turns')`);
  assert.ok(reads > before);
  assert.equal(await evaluate(`document.querySelector('#set-agentMaxTools') === null`), true);
});

// Exercise the shipped form against schema/revision semantics, not a duplicate
// implementation of its parser. Native consumer parity has separate tests.
const targetedPage = page.replace('window._isAdmin = false;', "window._isAdmin = true; localStorage.setItem('currentSessionId', 'settings-fixture'); window.confirm = () => true;");
let targetReads = 0, revision = 1;
let rejectTargetReads = true;
let stored = {compaction:{max_context:{'*':'50%'},preserve_recent_tokens:12000,auto:false},checkpoint:{fork:false,push_caps:{recent_user:0}},formatter:false,lsp:{typescript:{command:['typescript-language-server','--stdio'],extensions:['.ts']}},command:{review:{template:'Review $ARGUMENTS'}},watcher:{ignore:[]},instructions:['/legacy/account-instruction.md']};
let targetedWrites = [];
let holdTargetWrite=false, releaseTargetWrite=null;
let mcpEnabled=true, catalogRefreshes=[];
const schemaFields = [
  {path:'compaction.auto',type:'boolean'},
  {path:'compaction.max_context',type:'quantity'},
  {path:'compaction.preserve_recent_tokens',type:'integer',min:0,max:8000000},
  {path:'checkpoint.fork',type:'boolean'},
  {path:'checkpoint.push_caps.recent_user',type:'integer',min:0},
  {path:'formatter',type:'json'},
  {path:'lsp',type:'json'},
  {path:'command',type:'object'},
  {path:'watcher.ignore',type:'string_array'},
  {path:'instructions',type:'string_array',available:false,resettable:true,reason:'Instruction sources require the Files owner'},
].map(field=>({...field,available:field.available!==false,scope:'account',apply:'next_admission',help:field.path}));
await withCopalBrowser({ page:targetedPage, cdpTimeoutMs:60000, request:async(req,res)=>{
  const url=new URL(req.url,'http://fixture');
  if(url.pathname==='/api/auth/settings' && req.method==='GET') {
    targetReads++;
    if(rejectTargetReads)return response(res,{detail:'Temporary read failure'},503);
    return response(res,{agent_settings_saved:stored,agent_settings:stored,agent_settings_override:true,agent_settings_revision:String(revision),agent_settings_schema:{version:1,fields:schemaFields},agent_settings_effective:{status:'pending',reason:'SUPERVISOR_UNAVAILABLE'}});
  }
  if(url.pathname==='/api/auth/settings' && req.method==='POST') {
    let body='';for await(const chunk of req)body+=chunk;
    const patch=JSON.parse(body);targetedWrites.push(patch);
    if(holdTargetWrite)await new Promise(resolve=>{releaseTargetWrite=resolve;});
    if(patch.agent_settings_expected_revision!==String(revision))return response(res,{detail:'Settings changed in another window. Reload before saving.'},409);
    stored={...stored,...patch.agent_settings};
    for(const path of patch.agent_settings_reset || []) {
      const parts=path.split('.');let node=stored;
      for(const key of parts.slice(0,-1))node=node?.[key];
      if(node)delete node[parts.at(-1)];
    }
    revision++;
    return response(res,{ok:true});
  }
  if(url.pathname==='/api/models/endpoints')return response(res,{items:[]});
  if(url.pathname==='/api/mcp/servers' && req.method==='GET')return response(res,[{id:'fixture-mcp',name:'Fixture MCP',is_enabled:mcpEnabled,status:'connected',tool_count:0,enabled_tool_count:0}]);
  if(url.pathname==='/api/mcp/servers/fixture-mcp' && req.method==='PATCH') {
    for await(const chunk of req){};mcpEnabled=!mcpEnabled;return response(res,{ok:true});
  }
  if(url.pathname==='/api/session/settings-fixture/mimo-operation' && req.method==='POST') {
    let body='';for await(const chunk of req)body+=chunk;catalogRefreshes.push(JSON.parse(body));return response(res,{commands:[],generation:1});
  }
  return false;
}},async({evaluate,until})=>{
  await until(`document.querySelector('#set-agentNativeStatus').textContent.includes('read failed')`);
  assert.equal(await evaluate(`document.querySelector('#set-agentNativeSave').disabled`),true);
  rejectTargetReads=false;
  await evaluate(`document.querySelector('#set-agentNativeReload').click()`);
  await until(`!document.querySelector('#set-agentNativeSave').disabled`);
  assert.equal(await evaluate(`document.querySelector('#set-agentPreserveRecent').value`),'12000');
  assert.deepEqual(JSON.parse(await evaluate(`document.querySelector('#set-agentMaxContext').value`)),{'*':'50%'});
  assert.equal(await evaluate(`document.querySelector('input[data-native-setting="instructions"]').disabled`),true);
  assert.equal(await evaluate(`document.querySelector('.settings-row[data-native-setting="instructions"] [data-native-inherit]').disabled`),false);
  await evaluate(`window.settingsModule.openSettings('ai instructions')`);
  await until(`document.activeElement.hasAttribute('data-native-inherit')`);
  const instructionEntry = await evaluate(`window.settingsModule.getCommandCatalogEntries().find(entry=>entry.identity==='setting:instructions')`);
  assert.equal(instructionEntry.availability,'available');
  assert.equal(instructionEntry.underlyingAvailability,'unavailable');
  assert.equal(instructionEntry.writable,false);
  await evaluate(`document.activeElement.click()`);
  await evaluate(`document.querySelector('#set-agentPreserveRecent').value='0'; document.querySelector('#set-agentNativeSave').click()`);
  await until(`document.querySelector('#set-agentPreserveRecent').value==='0' && !document.querySelector('#set-agentNativeSave').disabled`);
  const saved=targetedWrites.at(-1);
  assert.deepEqual(saved.agent_settings.compaction.max_context,{'*':'50%'});
  assert.equal(saved.agent_settings.compaction.preserve_recent_tokens,0);
  assert.equal(saved.agent_settings.compaction.auto,false);
  assert.equal(saved.agent_settings.checkpoint.fork,false);
  assert.equal(saved.agent_settings.checkpoint.push_caps.recent_user,0);
  assert.equal(saved.agent_settings_expected_revision,'1');
  assert.equal(saved.agent_settings.formatter,false);
  assert.deepEqual(saved.agent_settings.lsp,{typescript:{command:['typescript-language-server','--stdio'],extensions:['.ts']}});
  assert.deepEqual(saved.agent_settings.command,{review:{template:'Review $ARGUMENTS'}});
  assert.deepEqual(saved.agent_settings.watcher.ignore,[]);
  assert.equal(saved.agent_settings.instructions,undefined);
  assert.deepEqual(saved.agent_settings_reset,['instructions']);
  assert.equal(stored.instructions,undefined);
  await evaluate(`window.settingsModule.openSettings('ai reference:server.port')`);
  await until(`document.activeElement.closest('[data-native-reference-path]')?.dataset.nativeReferencePath==='server.port'`);
  assert.equal(await evaluate(`document.activeElement.closest('[data-native-reference-path]').textContent.includes('operator')`),true);
  assert.equal(await evaluate(`document.activeElement.closest('[data-native-reference-path]').querySelectorAll('input,select,textarea').length`),0);
  const reference = await evaluate(`window.settingsModule.getCommandCatalogEntries().find(row=>row.token==='settings ai reference:server.port')`);
  assert.equal(reference.writable,false);
  assert.equal(reference.destination.field,'reference:server.port');
  holdTargetWrite=true;
  const beforeWrites=targetedWrites.length;
  await evaluate(`document.querySelector('#set-agentNativeSave').click()`);
  for(let attempt=0;attempt<100 && targetedWrites.length===beforeWrites;attempt++)await delay(10);
  assert.ok(releaseTargetWrite,'held mutation began');
  const beforeReads=targetReads;
  await evaluate(`document.dispatchEvent(new CustomEvent('odysseus:model-picked'))`);
  for(let attempt=0;attempt<100 && targetReads===beforeReads;attempt++)await delay(10);
  await delay(30);
  assert.equal(await evaluate(`document.querySelector('#set-agentPreserveRecent').disabled`),true);
  assert.equal(await evaluate(`document.querySelector('#set-agentNativeSave').disabled`),true);
  holdTargetWrite=false;releaseTargetWrite();releaseTargetWrite=null;
  await until(`!document.querySelector('#set-agentNativeSave').disabled`);
  await evaluate(`window.settingsModule.openSettings('permissions')`);
  assert.equal(await evaluate(`document.querySelector('[data-settings-panel="file-access"]').classList.contains('hidden')`),false);
  await evaluate(`window.settingsModule.openSettings('ai compaction.max_context')`);
  await until(`document.activeElement.id==='set-agentMaxContext'`);
  await evaluate(`document.querySelector('#settings-finder').value='checkpoint.push_caps'; document.querySelector('#settings-finder').dispatchEvent(new Event('input'))`);
  assert.equal(await evaluate(`document.querySelector('[data-settings-tab="ai"]').classList.contains('settings-finder-hidden')`),false);
  revision++;
  await evaluate(`document.querySelector('#set-agentPreserveRecent').value='13000'; document.dispatchEvent(new CustomEvent('odysseus:model-picked'))`);
  await until(`document.querySelector('#set-agentNativeStatus').textContent.includes('Unsaved changes preserved')`);
  assert.equal(await evaluate(`document.querySelector('#set-agentPreserveRecent').value`),'13000');
  await evaluate(`document.querySelector('#set-agentNativeSave').click()`);
  await until(`document.querySelector('#set-agentNativeStatus').textContent.includes('Settings not saved')`);
  assert.equal(await evaluate(`document.querySelector('#set-agentPreserveRecent').value`),'13000');
  await evaluate(`window._isAdmin=true;window.settingsModule.openSettings('integrations')`);
  await until(`document.querySelector('.intg-card[data-intg-type="mcp"]')`);
  await evaluate(`document.querySelector('.intg-card[data-intg-type="mcp"]').click()`);
  await until(`document.querySelector('#uf-mcp-toggle')`);
  await evaluate(`document.querySelector('#uf-mcp-toggle').click()`);
  for(let attempt=0;attempt<100 && !catalogRefreshes.length;attempt++)await delay(10);
  assert.equal(mcpEnabled,false);
  assert.deepEqual(catalogRefreshes,[{operation:'commands-refresh',reload_skills:false}]);
});

console.log('agent settings mounted acceptance passed: save/reset/races, schema fidelity, retry, navigation and conflicts');
