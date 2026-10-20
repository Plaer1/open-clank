import assert from 'node:assert/strict';
import {withCopalBrowser} from './helpers/copal_browser_fixture.mjs';

const page=`<!doctype html><html><head><link rel="stylesheet" href="/static/style.css"></head><body>
<div class="msg"><div class="role"><span class="role-timestamp">12:34</span></div><div class="thinking-section">Reasoning</div></div>
<div class="agent-thread-node" id="good-tool"><pre class="agent-thread-cmd">echo test</pre><details class="agent-tool-output" open>Output</details><details class="agent-tool-diff agent-tool-output" open><pre class="diff-pre"><span>long diff line</span></pre></details></div>
<div class="agent-thread-node error" id="bad-tool"><details class="agent-tool-output" open>Failure</details></div>
<script type="module">
  const source=await fetch('/static/app.js').then(r=>r.text());
  const start=source.indexOf('  function applyUIVis(state) {');
  const end=source.indexOf('  // Rearrange toggles',start);
  if(start<0||end<0)throw new Error('Missing shipped visibility consumer');
  const storage=await import('/static/js/storage.js');
  const key=source.match(/const UI_VIS_KEY = '([^']+)'/)[1];
  window.loadUIVis=()=>storage.getJSON(key,{});
  window.saveUIVis=state=>storage.setJSON(key,state);
  window.UI_VIS_DEFAULT_OFF=new Set(['wrap-diffs']);
  // Invoke the actual shipped consumer, isolated from unrelated app startup.
  window.applyUIVis=new Function('UI_VIS_MAP','UI_VIS_DEFAULT_OFF','applyTextEmojis',source.slice(start,end)+';return applyUIVis;')({},window.UI_VIS_DEFAULT_OFF,()=>{});
  window.applyUIVis(window.loadUIVis());
  window._isAdmin=false;
  const parsed=new DOMParser().parseFromString(await fetch('/static/index.html').then(r=>r.text()),'text/html');
  document.body.append(parsed.getElementById('settings-modal'));
  const module=await import('/static/js/settings.js?transcript-preferences-fixture');
  window.settingsModule=module.default;
  module.open('appearance');
  await window.__agentSettingsReady;
  window.fixtureReady=true;
</script></body></html>`;
await withCopalBrowser({page,cdpTimeoutMs:60000,request:async(req,res)=>{
  if(new URL(req.url,'http://fixture').pathname==='/api/auth/settings'){
    res.setHeader('content-type','application/json');res.end(JSON.stringify({agent_settings:{},agent_settings_override:false}));return true;
  }
  return false;
}},async({evaluate,until})=>{
  await until('window.fixtureReady');
  const change=async(key,value)=>evaluate(`{const input=document.querySelector('[data-ui-key="${key}"]');input.checked=${value};input.dispatchEvent(new Event('change'));}`);
  const display=selector=>evaluate(`getComputedStyle(document.querySelector(${JSON.stringify(selector)})).display`);
  await change('show-timestamps',false);
  assert.equal(await display('.role-timestamp'),'none');
  await evaluate(`{const m=document.createElement('div');m.className='msg';m.innerHTML='<span class="role-timestamp" id="late-time">12:35</span>';document.body.append(m);}`);
  assert.equal(await display('#late-time'),'none');
  await change('show-timestamps',true);
  assert.notEqual(await display('#late-time'),'none');
  await change('show-thinking',false);
  assert.equal(await display('.thinking-section'),'none');
  await change('show-tool-details',false);
  assert.equal(await display('.agent-thread-cmd'),'none');
  await change('show-tool-output',false);
  assert.equal(await display('#good-tool .agent-tool-output:not(.agent-tool-diff)'),'none');
  assert.notEqual(await display('#good-tool .agent-tool-diff'),'none');
  assert.notEqual(await display('#bad-tool .agent-tool-output'),'none');
  await change('wrap-diffs',true);
  assert.equal(await evaluate(`getComputedStyle(document.querySelector('.diff-pre span')).whiteSpace`),'pre-wrap');
  await change('wrap-diffs',false);
  assert.equal(await evaluate(`getComputedStyle(document.querySelector('.diff-pre span')).whiteSpace`),'pre');
  await evaluate(`window.settingsModule.openSettings('appearance show-timestamps')`);
  await until(`document.activeElement.dataset.uiKey==='show-timestamps'`);
  const entry=await evaluate(`window.settingsModule.getCommandCatalogEntries().find(e=>e.identity==='setting:appearance:show-timestamps')`);
  assert.equal(entry.scope,'browser-local');
  assert.deepEqual(entry.destination,{panel:'appearance',field:'show-timestamps'});
  await evaluate(`window.applyUIVis(window.loadUIVis())`);
  assert.equal(await display('.agent-thread-cmd'),'none');
});
console.log('transcript preferences mounted acceptance passed: shipped consumer, CSS, persistence, late nodes, errors, diffs and deep focus');
