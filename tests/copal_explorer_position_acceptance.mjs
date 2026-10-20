import assert from 'node:assert/strict';
import fs from 'node:fs';
import { withCopalBrowser } from './helpers/copal_browser_fixture.mjs';

const source = fs.readFileSync('static/js/copal.js', 'utf8');
const page = `<!doctype html><link rel="stylesheet" href="/static/style.css"><script type="module">
import Copal, { explorerFixture } from '/static/js/copal.js'; window.Copal = Copal; window.fixture = explorerFixture;
window.probe = () => {
 const body = document.querySelector('[data-note-panel=files]'), rect = body.getBoundingClientRect();
 const rows = [...body.querySelectorAll('[data-note-tree-key]')].filter(n => n.getClientRects().length);
 const first = rows.find(n => n.getBoundingClientRect().bottom > rect.top);
 return { key:first.dataset.noteTreeKey, offset:first.getBoundingClientRect().top-rect.top, scroll:body.scrollTop, height:body.clientHeight, max:body.scrollHeight-body.clientHeight };
};
window.offset = key => { const body = document.querySelector('[data-note-panel=files]'); return body.querySelector('[data-note-tree-key="'+CSS.escape(key)+'"]').getBoundingClientRect().top-body.getBoundingClientRect().top; };
</script>`;
const request = async (req, res) => {
 if (!req.url.startsWith('/api/')) return false;
 res.setHeader('content-type','application/json');
 if(req.url.includes('/status')) res.end(JSON.stringify({account_id:'account-a', storage_namespace:'user:a'}));
 else if(req.url.includes('/events')) {res.setHeader('content-type','text/event-stream');res.write(': fixture\n\n');}
 else res.end(JSON.stringify({value:{},tracks:[],floatingTodos:[]}));
 return true;
};
await withCopalBrowser({page,request,overrides:{'/static/js/copal.js':source+'\nexport const explorerFixture = {state,notesFeature};\n'}}, async ({evaluate,until,cdp}) => {
 await cdp('Emulation.setDeviceMetricsOverride',{width:1500,height:1000,deviceScaleFactor:1,mobile:false});
 await cdp('Emulation.setEmulatedMedia',{features:[{name:'prefers-reduced-motion',value:'reduce'}]});
 await until('Boolean(window.fixture)');
 await evaluate('Copal.init()');
 await evaluate(`fixture.state.docs = Array.from({length:200},(_,i)=>({id:'doc-'+i,name:'Folder'+String(Math.floor(i/2)).padStart(3,'0')+'/Note'+i+'.md',kind:'markdown',text:'fixture',readOnly:true,properties:{},relations:[],links:[]})); Copal.open('notes',false)`);
 await evaluate(`const context=fixture.state.windows.get('notes'); context.window.content.style.width='1300px'; context.window.content.style.height='800px'; context.noteWorkspace.left.expanded=Array.from({length:100},(_,i)=>'Folder'+String(i).padStart(3,'0')); fixture.notesFeature.render()`);
 await until('document.querySelectorAll(".copal-folder-row").length === 100');
 await evaluate('document.fonts.ready;');
 await evaluate('document.getAnimations().forEach(animation=>animation.finish())');
 await evaluate('document.querySelector("[data-note-panel=files]").scrollTop=600');
 const before = await evaluate('window.probe()');
 assert(before.scroll > 500 && before.height > 0 && before.max > 1000, JSON.stringify(before));
 const target = await evaluate(`(() => {const body=document.querySelector('[data-note-panel=files]'),r=body.getBoundingClientRect();const row=[...body.querySelectorAll('.copal-folder-row')].find(n=>n.getBoundingClientRect().top>r.top+100);const p=row.getBoundingClientRect();return {key:row.dataset.noteTreeKey,x:p.left+60,y:p.top+p.height/2};})()`);
 await cdp('Input.dispatchMouseEvent',{type:'mousePressed',x:target.x,y:target.y,button:'left',buttons:1,clickCount:1});
 await cdp('Input.dispatchMouseEvent',{type:'mouseReleased',x:target.x,y:target.y,button:'left',buttons:0,clickCount:1});
 assert(Math.abs(await evaluate(`window.offset(${JSON.stringify(before.key)})`)-before.offset)<=1,'visible disclosure displaced the viewport anchor');
 assert.equal(await evaluate('document.activeElement?.dataset.noteTreeKey'),target.key,'disclosure loses keyboard focus');
 const above = await evaluate('window.probe()');
 await evaluate(`document.querySelector('[data-note-tree-key="folder:Folder000"]').click()`);
 const collapsedOffset = await evaluate(`window.offset(${JSON.stringify(above.key)})`);
 assert(Math.abs(collapsedOffset-above.offset)<=1,JSON.stringify({case:'collapse above viewport',above,collapsedOffset,after:await evaluate('window.probe()')}));
 const background = await evaluate('window.probe()');
 await evaluate(`fixture.state.docs.unshift({id:'new',name:'AAAA/new.md',kind:'markdown',text:'new',readOnly:true,properties:{},links:[],relations:[]});fixture.notesFeature.render()`);
 assert(Math.abs(await evaluate(`window.offset(${JSON.stringify(background.key)})`)-background.offset)<=1,'background insertion displaced surviving row');
 const focusParent = await evaluate(`(() => {const row=document.querySelector('[data-note-tree-key="document:doc-20"]');row.focus({preventScroll:true});const key=row.dataset.noteParent;document.querySelector('[data-note-tree-key="folder:'+key+'"]').click();return key;})()`);
 assert.equal(await evaluate('document.activeElement?.dataset.noteTreeKey'),`folder:${focusParent}`,'collapsed focused descendant must fall back to parent');
 for (const side of ['right','left']) for (const tab of ['tags','bookmarks','recent']) {
   const expected = tab === 'tags' ? 'doc-8' : tab === 'bookmarks' ? 'doc-4' : 'doc-6';
   await evaluate(`(() => { fixture.state.docs.find(d=>d.id==='doc-8').tags=['fixture-tag']; const w=fixture.state.windows.get('notes').noteWorkspace; w.bookmarks=['doc-4'];w.recent=['doc-6']; fixture.notesFeature.updateNotesPanel(${JSON.stringify(tab)},{side:${JSON.stringify(side)}});w[${JSON.stringify(side)}].tab=${JSON.stringify(tab)};w[${JSON.stringify(side)}].open=true;fixture.notesFeature.render(); })()`);
   if(tab==='tags') await evaluate(`document.querySelector('#copal-notes-${side}-sidebar .copal-tag-group').open=true`);
   await evaluate(`document.querySelector('#copal-notes-${side}-sidebar [data-note-tree-key="document:${expected}"]').click()`);
   assert.equal(await evaluate('fixture.state.windows.get("notes").selected'),expected,`${tab} on ${side} must open its source`);
 }
 for (const scale of [.8,1.25]) {
   await evaluate(`document.documentElement.style.zoom=${scale};fixture.state.windows.get('notes').noteWorkspace.left.tab='files';fixture.notesFeature.render();document.querySelector('[data-note-panel=files]').scrollTop=600`);
   const scaled = await evaluate('window.probe()');
   await evaluate(`document.querySelector('[data-note-tree-key="folder:Folder001"]').click()`);
   assert(Math.abs(await evaluate(`window.offset(${JSON.stringify(scaled.key)})`)-scaled.offset)<=1,`zoom ${scale} displaced the visible anchor`);
 }
 console.log(JSON.stringify({passed:['trusted scrolled disclosure keeps anchor/focus','collapse above viewport','background insertion','collapsed descendant focus','Tags/Bookmarks/Recent work on both sides','anchor at CSS zoom .8/1.25'],baseline:before},null,2));
});
