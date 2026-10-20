import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { withCopalBrowser } from './helpers/copal_browser_fixture.mjs';

const page = `<!doctype html><meta charset="utf-8"><link rel="stylesheet" href="/static/style.css">
<style>body{margin:0;background:#20242a;color:#eee}#graph{width:1000px;height:720px;margin:12px;--panel:#26292f;--bg:#171a1f;--fg:#eee;--border:#535863;--accent:#edb54a}.fixture-canvas{flex:1;background:#30343b;min-height:180px}</style>
<main id="graph" class="copal-graph-wrap"><div id="filters"></div><div class="fixture-canvas">Graph viewport</div></main><button id="behind">Behind overlay</button><script type="module" src="/fixture.js"></script>`;
const fixture = `
import { mountGraphFilters } from '/static/js/copal/graphFilterControls.js';
import { createFacetCatalog } from '/static/js/copal/graphFacetCatalog.js';
import { propertyFacetOption } from '/static/js/copal/graphModel.js';
const folders=[{value:'',count:1},...Array.from({length:5301},(_,i)=>({value:'Folder/'+String(i).padStart(4,'0'),count:1}))];
const properties={likes:[true,'true',1,'1',' padded '].map(value=>({...propertyFacetOption(value),count:1})),
 ...Object.fromEntries(['__proto__','constructor'].map(key=>[key,[{...propertyFacetOption('safe'),count:1}]])),
 ...Object.fromEntries(Array.from({length:220},(_,i)=>['metadata-'+i,[{...propertyFacetOption('value'),count:1}]]))};
const facets={version:2,generation:'g1',kinds:[{value:'note',count:406},{value:'wiki',count:2}],folders:folders.slice(0,200),tags:[{value:'tag',count:3}],properties,propertyKeys:Object.keys(properties),incomplete:true};
const graphState={searchQuery:'',activeKinds:new Set(['note','wiki']),activeFolders:new Set(),activeTags:new Set(),activeProperties:{orphan:new Set([{type:'legacy-string',value:'unknown'}])},includeOfficial:false};
window.audit={graphState,facets,calls:[],changes:[],behind:0,hold:false,pending:[],current:true};
audit.popupSubscriptions=[];
const addListener=EventTarget.prototype.addEventListener;
EventTarget.prototype.addEventListener=function(type,listener,options){
 if(listener?.name==='boundPopup')audit.popupSubscriptions.push({type,signal:options?.signal});
 return addListener.call(this,type,listener,options);
};
document.querySelector('#behind').onclick=()=>audit.behind++;
const catalog=createFacetCatalog({getScope:()=>({account:'a',workspace:'w'}),getDocuments:()=>[],request:async params=>{
 audit.calls.push(params); if(audit.hold)await new Promise(resolve=>audit.pending.push(resolve));
 const source=params.category==='properties'?properties[params.property_key]||[]:params.category==='folders'?folders:facets[params.category]||[];
 const values=source.filter(item=>String(item.label??item.value).toLowerCase().includes(params.query.toLowerCase()));
 const part={values:values.slice(params.offset,params.offset+params.limit),offset:params.offset,total:values.length,hasMore:params.offset+params.limit<values.length};
 return {version:2,generation:'g1',facets:params.category==='properties'?{properties:{[params.property_key]:part}}:{[params.category]:part}};
}});
audit.controls=mountGraphFilters({element:document.querySelector('#filters'),graphState,facets,catalog,onChange:event=>audit.changes.push(event||{}),isCurrent:()=>audit.current});
window.ready=true;
`;

await withCopalBrowser({page,overrides:{'/fixture.js':fixture}}, async ({evaluate,until,cdp}) => {
  await until('window.ready');
  assert.equal(await evaluate(`document.querySelectorAll('#filters input[type=checkbox]').length`),0,'closed controls have no checkbox wall');
  assert.ok(await evaluate(`document.querySelectorAll('#filters button').length < 12`));
  assert.equal(await evaluate('audit.calls.length'),0,'closed pickers do not fetch');
  assert.ok(await evaluate(`document.querySelector('.copal-graph-active-filters').textContent.includes('legacy string match')`));
  await evaluate(`document.querySelector('[data-facet-trigger=kinds]').click()`);
  await until(`document.querySelectorAll('[data-facet-group=Kinds] input').length===2`);
  assert.equal(await evaluate('audit.changes.length'),0,'opening does not filter');
  await evaluate(`[...document.querySelectorAll('.copal-graph-filter-popup button')].find(x=>x.textContent==='None').click()`);
  assert.equal(await evaluate('audit.graphState.activeKinds.size'),0,'empty kinds intentionally matches nothing');
  await evaluate(`document.activeElement.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}))`);
  assert.equal(await evaluate(`document.activeElement.dataset.facetTrigger`),'kinds');
  await evaluate(`document.querySelector('[aria-label="Reset graph filters"]').click()`);
  assert.equal(await evaluate('audit.graphState.activeKinds.size'),2);
  await evaluate(`document.querySelector('[data-facet-trigger=folders]').click()`);
  await until(`document.querySelectorAll('[data-facet-group=Folders] input').length===80`);
  await evaluate(`document.querySelector('[data-facet-value=""]').focus();audit.controls.updateFacets(audit.facets)`);
  await until(`document.querySelectorAll('[data-facet-group=Folders] input').length===80`);
  assert.equal(await evaluate('document.activeElement.dataset.facetValue'),'','root-folder focus survives refinement');
  await evaluate(`[...document.querySelectorAll('.copal-graph-filter-popup button')].find(x=>x.textContent==='Load more values').click()`);
  await until(`document.querySelectorAll('[data-facet-group=Folders] input').length===160`);
  await evaluate(`{const x=document.querySelector('[aria-label="Find Folders values"]');x.value='5300';x.dispatchEvent(new Event('input'));}`);
  await until(`document.querySelectorAll('[data-facet-group=Folders] input').length===1 && document.querySelector('[data-facet-group=Folders]').textContent.includes('5300')`);
  await evaluate(`document.querySelector('[data-facet-group=Folders] input').click()`);
  assert.deepEqual(await evaluate('[...audit.graphState.activeFolders]'),['Folder/5300'],'queried value beyond canonical cap remains selectable');
  await evaluate(`document.querySelector('[aria-label="Close Filter Folders"]').click(); document.querySelector('[data-facet-trigger=properties]').click()`);
  await evaluate(`{const x=document.querySelector('[aria-label="Find graph filter property"]');x.value='likes';x.dispatchEvent(new Event('input'));}`);
  await evaluate(`document.querySelector('[data-property-key=likes]').click()`);
  await until(`document.querySelectorAll('[data-facet-group=likes] input').length===5`);
  await evaluate(`{const inputs=[...document.querySelectorAll('[data-facet-group=likes] input')];inputs.find(x=>x.dataset.facetValue.startsWith('boolean:')).click();inputs.find(x=>x.dataset.facetValue==='string:'+JSON.stringify('true')).click();}`);
  assert.deepEqual(await evaluate(`[...audit.graphState.activeProperties.likes].map(x=>x.type).sort()`),['boolean','string']);
  await evaluate(`document.querySelector('[aria-label="Close Filter likes"]').click()`);
  for(const key of ['__proto__','constructor']) {
    await evaluate(`{document.querySelector('[data-facet-trigger=properties]').click();const x=document.querySelector('[aria-label="Find graph filter property"]');x.value='${key}';x.dispatchEvent(new Event('input'));document.querySelector('[data-property-key="${key}"]').click();}`);
    await until(`document.querySelectorAll('[data-facet-group="${key}"] input').length===1`);
    await evaluate(`document.querySelector('[data-facet-group="${key}"] input').click()`);
    assert.equal(await evaluate(`Object.hasOwn(audit.graphState.activeProperties,'${key}') && audit.graphState.activeProperties['${key}'] instanceof Set && audit.graphState.activeProperties['${key}'].size===1`),true);
    await evaluate(`document.querySelector('.copal-graph-filter-popup header button').click()`);
  }
  await evaluate(`document.querySelector('[data-facet-trigger=folders]').click()`);
  await until(`document.querySelectorAll('[data-facet-group=Folders] input').length===80`);
  await evaluate(`audit.hold=true; const x=document.querySelector('[aria-label="Find Folders values"]');x.value='Folder/01';x.focus();x.setSelectionRange(4,7);x.dispatchEvent(new Event('input'));`);
  await until('audit.pending.length===1');
  await evaluate(`audit.controls.updateFacets({...audit.facets,incomplete:false})`);
  await until('audit.pending.length===2');
  assert.deepEqual(await evaluate(`({label:document.activeElement.getAttribute('aria-label'),value:document.activeElement.value,start:document.activeElement.selectionStart,end:document.activeElement.selectionEnd})`),{label:'Find Folders values',value:'Folder/01',start:4,end:7},'refinement keeps search caret and focus');
  await evaluate(`document.querySelector('[aria-label="Close Filter Folders"]').click();audit.hold=false;for(const release of audit.pending)release()`);
  await evaluate('new Promise(resolve=>setTimeout(resolve,30))');
  assert.equal(await evaluate(`document.querySelectorAll('[data-graph-filter-popup]').length`),0,'late response never resurrects popup');
  await evaluate(`document.querySelector('[data-facet-trigger=folders]').click()`);
  await evaluate('new Promise(resolve=>setTimeout(resolve,30))');
  await evaluate(`document.querySelector('#behind').click()`);
  assert.equal(await evaluate('audit.behind'),0,'outside dismissal suppresses click-through');
  assert.equal(await evaluate(`document.querySelectorAll('[data-graph-filter-popup]').length`),0);
  for(const width of [320,390,1000]) {
    await evaluate(`document.querySelector('#graph').style.width='${width}px'`);
    if(width<560) await evaluate(`document.querySelector('.copal-graph-mobile-filters').click()`);
    else await evaluate(`document.querySelector('[data-facet-trigger=properties]').click()`);
    await until(`Boolean(document.querySelector('[data-graph-filter-popup]'))`);
    const bounds=await evaluate(`(()=>{const root=document.querySelector('#graph').getBoundingClientRect(),pop=document.querySelector('[data-graph-filter-popup]').getBoundingClientRect(),toolbar=document.querySelector('#filters').getBoundingClientRect();return {left:pop.left>=root.left,right:pop.right<=root.right+1,height:toolbar.height,canvas:document.querySelector('.fixture-canvas').getBoundingClientRect().height};})()`);
    assert.ok(bounds.left&&bounds.right,`popup bounded at ${width}: ${JSON.stringify(bounds)}`);assert.ok(bounds.height<250);assert.ok(bounds.canvas>=180);
    if(process.env.GRAPH_QOL_SCREENSHOTS){const image=await cdp('Page.captureScreenshot',{format:'png'});fs.writeFileSync(path.join(process.env.GRAPH_QOL_SCREENSHOTS,`filters-${width}.png`),Buffer.from(image.data,'base64'));}
    await evaluate(`document.querySelector('.copal-graph-filter-popup header button').click()`);
  }
  for(const scale of [1,2]) for(const theme of ['dark','light']) {
    await evaluate(`{const graph=document.querySelector('#graph');graph.style.width='390px';graph.style.height='480px';graph.style.zoom='${scale}';graph.style.setProperty('--panel','${theme==='light'?'#fafafa':'#26292f'}');graph.style.setProperty('--fg','${theme==='light'?'#111':'#eee'}');graph.style.setProperty('--bg','${theme==='light'?'#fff':'#171a1f'}');document.querySelector('.copal-graph-mobile-filters').click();}`);
    await evaluate('new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))');
    const bounds=await evaluate(`(()=>{const root=document.querySelector('#graph').getBoundingClientRect(),popup=document.querySelector('.copal-graph-filter-popup').getBoundingClientRect();return {bottom:popup.bottom<=root.bottom+1,right:popup.right<=root.right+1,left:popup.left>=root.left};})()`);
    assert.ok(bounds.bottom&&bounds.right&&bounds.left,`short applet bounds ${theme} zoom${scale}: ${JSON.stringify(bounds)}`);
    await evaluate(`document.querySelector('.copal-graph-filter-popup header button').click()`);
  }
  // Reproduce the actual short applet: header/scroll offset consumes most of
  // the viewport, unlike a toy 480px graph positioned at the page origin.
  for(const width of [320,390]) for(const scale of [1,2]) {
    await cdp('Emulation.setDeviceMetricsOverride',{width,height:480,deviceScaleFactor:1,mobile:false});
    await evaluate(`{let host=document.querySelector('#clip-host');if(!host){host=document.createElement('section');host.id='clip-host';document.body.append(host);host.append(document.querySelector('#graph'));}Object.assign(host.style,{position:'fixed',top:'235px',left:'16px',width:'${width-32}px',height:'257px',overflow:'auto'});const graph=document.querySelector('#graph');Object.assign(graph.style,{margin:'0',width:'${(width-32)/scale}px',height:'${257/scale}px',zoom:'${scale}'});document.querySelector('.copal-graph-mobile-filters').click();}`);
    await evaluate('new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))');
    const inspect=`(()=>{const p=document.querySelector('.copal-graph-filter-popup'),r=p.getBoundingClientRect(),g=document.querySelector('#graph').getBoundingClientRect(),h=document.querySelector('#clip-host').getBoundingClientRect();return {top:r.top,bottom:r.bottom,left:r.left,right:r.right,limitTop:Math.max(0,g.top,h.top),limitBottom:Math.min(innerHeight,g.bottom,h.bottom),limitLeft:Math.max(0,g.left,h.left),limitRight:Math.min(innerWidth,g.right,h.right),height:r.height};})()`;
    const verify=async label=>{const b=await evaluate(inspect);assert(b.top>=b.limitTop-1&&b.bottom<=b.limitBottom+1&&b.left>=b.limitLeft-1&&b.right<=b.limitRight+1,label+': '+JSON.stringify(b));assert(b.height>100,'Form has usable scrollable height');};
    await verify(`visible applet intersection ${width} zoom${scale}`);
    await evaluate(`{const p=document.querySelector('.copal-graph-filter-popup'),o=p.querySelector('[aria-label="Include official documentation"]');p.scrollTop=p.scrollHeight;const a=p.getBoundingClientRect(),b=o.getBoundingClientRect();p.scrollTop+=(b.top+b.height/2-a.top-a.height/2)/${scale};}`);
    await evaluate('new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))');
    const last=await evaluate(`(()=>{const p=document.querySelector('.copal-graph-filter-popup'),r=p.getBoundingClientRect(),o=p.querySelector('[aria-label="Include official documentation"]').getBoundingClientRect();return {top:r.top,bottom:r.bottom,controlTop:o.top,controlBottom:o.bottom,scrollTop:p.scrollTop,scrollHeight:p.scrollHeight,clientHeight:p.clientHeight};})()`);
    assert(last.controlTop>=last.top&&last.controlBottom<=last.bottom,'Last control reachable by scrolling: '+JSON.stringify({width,scale,...last}));
    await evaluate(`document.querySelector('#clip-host').scrollTop=20`);await evaluate('new Promise(resolve=>requestAnimationFrame(resolve))');await verify('scroll while open');
    await cdp('Emulation.setDeviceMetricsOverride',{width:width-10,height:460,deviceScaleFactor:1,mobile:false});await evaluate('new Promise(resolve=>requestAnimationFrame(resolve))');await verify('resize while open');
    await evaluate(`document.querySelector('.copal-graph-filter-popup').scrollTop=0`);
    const close=await evaluate(`(()=>{const r=document.querySelector('.copal-graph-filter-popup header button').getBoundingClientRect();return{x:r.x+r.width/2,y:r.y+r.height/2};})()`);
    await cdp('Input.dispatchMouseEvent',{type:'mousePressed',button:'left',clickCount:1,...close});await cdp('Input.dispatchMouseEvent',{type:'mouseReleased',button:'left',clickCount:1,...close});
    assert.equal(await evaluate(`document.querySelectorAll('[data-graph-filter-popup]').length`),0,'Close reachable by real pointer');
    assert(await evaluate(`audit.popupSubscriptions.every(item=>item.signal?.aborted)`),'Closing aborts owned resize and capture-scroll listeners');
  }
  await evaluate(`document.querySelector('.copal-graph-mobile-filters').click();document.querySelector('#clip-host').style.height='24px';document.querySelector('#clip-host').dispatchEvent(new Event('scroll'))`);
  await evaluate('new Promise(resolve=>requestAnimationFrame(resolve))');
  assert.equal(await evaluate(`document.querySelectorAll('[data-graph-filter-popup]').length`),0,'Unusable positive intersection dismisses, never exposes a padding-floor sliver');
  assert(await evaluate(`document.activeElement.matches('.copal-graph-mobile-filters')`),'Tiny-intersection dismissal restores opener focus');
  assert(await evaluate(`audit.popupSubscriptions.every(item=>item.signal?.aborted)`),'Tiny-intersection dismissal cleans listeners');
  await evaluate(`document.querySelector('#clip-host').style.height='257px'`);
  await evaluate(`document.querySelector('.copal-graph-mobile-filters').click();audit.controls.dispose()`);
  assert.equal(await evaluate(`document.querySelectorAll('[data-graph-filter-popup]').length`),0);
  assert(await evaluate(`audit.popupSubscriptions.every(item=>item.signal?.aborted)`),'Disposal aborts all popup subscriptions');
  console.log('Graph compact filter acceptance: closed layout, typed choices, 5301-value search, paging, focus/refinement, disposal, outside click, and 320/390/1000px applet bounds passed');
});
