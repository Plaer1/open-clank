import test from 'node:test';
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {withCopalBrowser} from './helpers/copal_browser_fixture.mjs';

const page = `<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/static/treehouse/learning-assets.css"></head><body><main id="host"></main><script type="module">
import {mountLearningAsset} from '/static/js/copal/treehouseLearningAssets.js';
window.registry = await fetch('/static/treehouse/assets/registry.json').then(r=>r.json());
window.openAsset = id => {window.current?.destroy(); const record=registry.find(x=>x.id===id); window.current=mountLearningAsset({host:document.querySelector('#host'),assetId:id,unitId:record.unitIds[0],registry,theme:window.assetTheme||'light'});};
window.openAsset('cs-state-trace');
</script></body></html>`;

test('pilot native controls, text equivalence, narrow themes and disposal', async () => {
  await withCopalBrowser({page}, async ({cdp,evaluate,until}) => {
    await until("document.querySelector('table')");
    await cdp('Page.bringToFront');
    await evaluate("[...document.querySelectorAll('button')].find(b=>b.textContent==='Step').focus()");
    assert.equal(await evaluate("document.activeElement.textContent"),'Step');
    await cdp('Input.dispatchKeyEvent',{type:'keyDown',key:'Enter',code:'Enter',windowsVirtualKeyCode:13,text:'\r'});
    await cdp('Input.dispatchKeyEvent',{type:'keyUp',key:'Enter',code:'Enter',windowsVirtualKeyCode:13});
    assert.match(await evaluate("document.querySelector('[role=status]').textContent"),/shares/);
    await evaluate("[...document.querySelectorAll('button')].find(b=>b.textContent==='Step').click()");
    assert.match(await evaluate("document.querySelector('table').textContent"),/\[1, 2\]/);
    await evaluate("window.openAsset('games-puzzle-states')");
    await until("document.querySelector('h4')?.textContent==='A tiny sliding puzzle' && document.querySelector('table')");
    assert.match(await evaluate("document.querySelector('[role=status]').textContent"),/A down → A down → A right/);
    await evaluate("document.querySelector('select').value='slide'; document.querySelector('select').dispatchEvent(new Event('change'))");
    assert.match(await evaluate("document.querySelector('[role=status]').textContent"),/A down 2 cells → A right/);
    await evaluate("[...document.querySelectorAll('button')].find(b=>b.textContent==='A down 2 cells').click()");
    assert.match(await evaluate("document.querySelector('table').textContent"),/A/);
    await evaluate("window.openAsset('ai-context-budget')");
    await until("document.querySelector('h4')?.textContent==='Reserve context for work' && document.querySelector('table')");
    assert.match(await evaluate("document.querySelector('[role=status]').textContent"),/2500/);
    await evaluate("document.querySelectorAll('input')[2].value='9000';document.querySelectorAll('input')[2].dispatchEvent(new Event('change'))");
    assert.match(await evaluate("document.querySelector('[role=status]').textContent"),/Overflow/);
    await evaluate("document.querySelectorAll('input')[2].value='';document.querySelectorAll('input')[2].dispatchEvent(new Event('change'))");
    assert.match(await evaluate("document.querySelector('[role=status]').textContent"),/needs a number/);
    assert.equal(await evaluate("document.querySelectorAll('table').length"),0);
    await evaluate("[...document.querySelectorAll('button')].find(b=>b.textContent==='Reset').click()");
    assert.match(await evaluate("document.querySelector('[role=status]').textContent"),/2500/);
    await cdp('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:false});
    assert.equal(await evaluate('document.documentElement.scrollWidth <= window.innerWidth'),true);
    await cdp('Emulation.setEmulatedMedia',{features:[{name:'prefers-reduced-motion',value:'reduce'}]});
    await evaluate("document.body.style.background='#151515';document.body.style.color='#fafafa';document.querySelector('button').focus()");
    assert.equal(await evaluate("getComputedStyle(document.activeElement).outlineStyle"),'solid');
    assert.equal(await evaluate("document.querySelectorAll('svg').length"),0);
    for(const id of await evaluate('registry.map(x=>x.id)')) {
      await evaluate(`window.openAsset(${JSON.stringify(id)})`);
      await until(`document.querySelector('table') && document.querySelector('h4')`);
      assert.equal(await evaluate('document.documentElement.scrollWidth <= window.innerWidth'),true,id);
      assert.equal(await evaluate("document.querySelectorAll('[role=status]').length"),1,id);
    }
    await evaluate("window.openAsset('ai-model-axes')");await until("document.querySelector('h4')?.textContent==='Compare separate model axes' && document.querySelector('table')");
    await evaluate("const selects=document.querySelectorAll('select');selects[0].value='modality';selects[1].value='4';selects[2].value='image';selects[2].dispatchEvent(new Event('change'))");
    assert.match(await evaluate("document.querySelector('[role=status]').textContent"),/does not match/);
    await evaluate("document.querySelectorAll('select')[2].value='text';document.querySelectorAll('select')[2].dispatchEvent(new Event('change'))");
    assert.match(await evaluate("document.querySelector('[role=status]').textContent"),/matches one declared label/);
    await evaluate("window.openAsset('games-payoff-table')");await until("document.querySelector('h4')?.textContent==='Compare payoffs and best responses' && document.querySelector('table')");
    await evaluate("document.querySelector('input').value='0.5';document.querySelector('input').dispatchEvent(new Event('change'))");
    assert.match(await evaluate("document.querySelector('[role=status]').textContent"),/integers/);
    assert.equal(await evaluate("document.querySelectorAll('table').length"),0);
    await evaluate("window.openAsset('ai-tokenization-trace')");
    await until("document.querySelector('h4')?.textContent==='Inspect a toy tokenizer' && document.querySelector('table')");
    await evaluate("for(let i=0;i<8;i++)[...document.querySelectorAll('button')].find(b=>b.textContent==='Step').click()");
    assert.match(await evaluate("document.querySelector('[role=status]').textContent"),/5 tokens from 8 codepoints/);
    await evaluate("document.querySelector('select').value='unknown';document.querySelector('select').dispatchEvent(new Event('change'))");
    assert.match(await evaluate("document.querySelector('[role=status]').textContent"),/Unknown 2.*full count unknown/);
    await evaluate("[...document.querySelectorAll('button')].find(b=>b.textContent==='Reset').click()");
    assert.equal(await evaluate("document.querySelector('table').rows.length"),1);
    await evaluate("for(let i=0;i<12;i++)window.openAsset('cs-state-trace')");
    await until("document.querySelector('table')");
    assert.equal(await evaluate("document.querySelectorAll('.treehouse-learning-asset').length"),1);
    await evaluate("window.current.destroy()");
    assert.equal(await evaluate("document.querySelectorAll('.treehouse-learning-asset').length"),0);
  });
});

test('bad fetch, digest mismatch and malformed fields remain visibly recoverable',async()=>{
  const malformed=JSON.stringify({id:'cs-state-trace',title:'Broken test data',fields:[{key:'mode',label:'Mode',type:'select',options:null}]});
  const digest='sha256:'+createHash('sha256').update(malformed).digest('hex');let state='bad-fetch';
  await withCopalBrowser({page,request:async(req,res)=>{
    if(req.url==='/static/treehouse/assets/cs-state-trace.json'){
      if(state==='bad-fetch'){res.writeHead(503);res.end('unavailable');}else{res.setHeader('content-type','application/json');res.end(malformed);}return true;
    }return false;
  }},async({evaluate,until})=>{
    await until("document.querySelector('#host').textContent.includes('written exercise remains usable')");
    assert.match(await evaluate("document.querySelector('#host').textContent"),/could not be loaded/);
    state='digest-mismatch';await evaluate("window.openAsset('cs-state-trace')");
    await until("document.querySelector('#host').textContent.includes('Asset changed')");
    state='malformed';await evaluate(`registry.find(x=>x.id==='cs-state-trace').digest=${JSON.stringify(digest)};window.openAsset('cs-state-trace')`);
    await until("document.querySelector('#host').textContent.includes('Invalid choice data')");
    assert.match(await evaluate("document.querySelector('#host').textContent"),/written exercise remains usable/);
    assert.equal(await evaluate("document.querySelectorAll('input,select').length"),0);
  });
});


test('all22 assets have readable labelled narrow light/dark controls and cleanup',async()=>{
  await withCopalBrowser({page},async({cdp,evaluate,until})=>{
    await until("window.registry?.length===22 && document.querySelector('table')");
    await cdp('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:false});
    await cdp('Emulation.setEmulatedMedia',{features:[{name:'prefers-reduced-motion',value:'reduce'}]});
    const ids=await evaluate('registry.map(x=>x.id)');
    for(const theme of ['light','dark'])for(const id of ids){
      await evaluate(`window.assetTheme=${JSON.stringify(theme)};document.body.style.color=${JSON.stringify(theme==='dark'?'#fafafa':'#151515')};document.body.style.background=${JSON.stringify(theme==='dark'?'#151515':'#ffffff')};window.openAsset(${JSON.stringify(id)})`);
      await until(`document.querySelector('.treehouse-learning-asset')?.dataset.theme===${JSON.stringify(theme)} && document.querySelector('table')`);
      const state=await evaluate(`(()=>{const root=document.querySelector('.treehouse-learning-asset');const button=[...root.querySelectorAll('button')].find(x=>x.textContent==='Reset');button.focus();return {overflow:document.documentElement.scrollWidth>innerWidth,labels:[...root.querySelectorAll('input,select')].every(x=>x.closest('label')?.textContent.trim()),caption:root.querySelector('caption')?.textContent,headers:root.querySelectorAll('th[scope=col]').length,status:root.querySelectorAll('[role=status]').length,focus:getComputedStyle(button).outlineStyle,svg:root.querySelectorAll('svg').length,text:root.textContent}})()`);
      assert.equal(state.overflow,false,`${id}/${theme}`);assert.equal(state.labels,true,`${id}/${theme}`);
      assert.equal(state.caption,'Current illustrative state');assert.ok(state.headers>0);assert.equal(state.status,1);
      assert.equal(state.focus,'solid');assert.equal(state.svg,0);assert.doesNotMatch(state.text,/NaN|Infinity/);
      await cdp('Input.dispatchKeyEvent',{type:'keyDown',key:'Enter',code:'Enter',windowsVirtualKeyCode:13,text:'\r'});
      await cdp('Input.dispatchKeyEvent',{type:'keyUp',key:'Enter',code:'Enter',windowsVirtualKeyCode:13});
      assert.equal(await evaluate("document.activeElement.textContent"),'Reset');
      await evaluate('window.current.destroy()');assert.equal(await evaluate("document.querySelectorAll('.treehouse-learning-asset').length"),0);
    }
  });
});
