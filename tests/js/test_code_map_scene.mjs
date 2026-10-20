import test from 'node:test';
import assert from 'node:assert/strict';
import {createCodeMapScene,fitCodeMapCamera} from '../../static/js/copal/codeMapScene.js';
import {layoutCodeMap} from '../../static/js/copal/codeMapLayout.js';
import {countSceneOwners} from '../../static/js/graphics/scene-owner.js';
import {graphicalPresentationPolicy} from '../../static/js/graphics/presentationPolicy.js';
import {coldStructure} from '../fixtures/code_map_cold_structure.mjs';
import {selectedRelation} from '../fixtures/code_map_selected_relation.mjs';
function events(target={}){const handlers=new Map();return Object.assign(target,{addEventListener(t,f){if(!handlers.has(t))handlers.set(t,new Set());handlers.get(t).add(f);},removeEventListener(t,f){handlers.get(t)?.delete(f);},emit(t,e={}){for(const f of [...handlers.get(t)||[]])f(e);},count(){return [...handlers.values()].reduce((s,x)=>s+x.size,0);}});}
function canvas(){const ctx=new Proxy({measureText:t=>({width:t.length*7})},{get:(o,k)=>k in o?o[k]:()=>{}});return events({width:1,height:1,style:{},attributes:[],dataset:{},setAttribute(name,value){this.attributes=this.attributes.filter(a=>a.name!==name);this.attributes.push({name,value});},removeAttribute(name){this.attributes=this.attributes.filter(a=>a.name!==name);},getContext:type=>type==='2d'?ctx:null,getBoundingClientRect:()=>({left:0,top:0,width:800,height:600}),setPointerCapture(){},releasePointerCapture(){},remove(){this.removed=true;}});}
function map(variant='structure_preview'){return {status:'ready',authority:variant==='published'?{variant,receipt:{workspace_id:'w',head_generation:'head'},summary_id:'summary'}:{variant,inventory_id:'inventory'},nodes:[{id:`${variant}-repo`,kind:'repository',label:'Repository',parent:null},{id:`${variant}-file`,kind:'file',label:'caller.py',parent:`${variant}-repo`},{id:`${variant}-target`,kind:'file',label:'target.py',parent:`${variant}-repo`}],edges:[{from:`${variant}-repo`,to:`${variant}-file`,relation:'containment',state:'structure'},{from:`${variant}-file`,to:`${variant}-target`,relation:variant==='published'?'call':'containment',state:variant==='published'?'exact':'structure'}]};}
function fixture({worker=false,real=false,host={},textContext=null}={}){
 const pending=new Map();let seq=0;const win=events({devicePixelRatio:3,requestAnimationFrame:f=>{pending.set(++seq,f);return seq;},cancelAnimationFrame:id=>pending.delete(id),matchMedia:()=>events({matches:false})}),doc=events({hidden:false}),c=canvas(),mount={clientWidth:800,clientHeight:600,getBoundingClientRect:c.getBoundingClientRect},sent=[],selections=[],changes=[],primitives=[];
 if(textContext)doc.createElement=()=>({getContext:()=>textContext});
 let options,invalidates=0,disposes=0,attached;const fakeConsumer=o=>{options=o;attached=o.onCanvasChange;return {canvas:c,backendKind:'canvas2d',accelerated:false,fallbackReason:'test-only',resize(){},invalidate(){invalidates++;},suspend(){},resume(){},dispose(){disposes++;}};};
 const w={postMessage:j=>sent.push(j),terminate(){this.stopped=true;}};
 const scene=createCodeMapScene({mount,canvas:c,win,doc,ownerHost:host,workerFactory:()=>worker?w:null,ResizeObserverClass:null,forceCanvas2D:true,...(real?{}:{consumerFactory:fakeConsumer}),onSelect:v=>selections.push(v),onChange:v=>changes.push(v)});
 return {scene,c,win,doc,pending,sent,w,selections,changes,primitives,get invalidates(){return invalidates;},get disposes(){return disposes;},swap:next=>attached(next,c),draw(){primitives.length=0;options.draw({batch:{rect:(...a)=>primitives.push(['rect',...a]),line:(...a)=>primitives.push(['line',...a]),circle:(...a)=>primitives.push(['circle',...a]),glyph:(...a)=>primitives.push(['glyph',...a])}});},pump(){const frames=[...pending.values()];pending.clear();for(const f of frames)f(16);}};
}
test('retained real13-node relation reveals both endpoints outside inspector with fixed readable labels',()=>{
 const f=fixture({worker:true}),data=structuredClone(selectedRelation),target=data.nodes.find(n=>n.label==='code_query.py').id;f.scene.resize(data.viewport.width,data.viewport.height);f.scene.update(data);const request=f.sent[0];f.w.onmessage({data:{serial:request.serial,namespace:request.namespace,positions:data.positions}});f.scene.select(data.root);f.scene.revealSelected({insets:data.insets,contextIds:[target]});f.draw();const s=f.scene.snapshot();
 for(const id of [data.root,target]){const l=s.labels.find(l=>l.id===id);assert(l,'Actual selected relation endpoint label must be drawn');assert.equal(l.fontSize,12);assert(l.x>=data.insets.left&&l.x+l.width<=data.viewport.width-data.insets.right,'Endpoint label must not hide behind inspector');assert(l.y-12>=data.insets.top&&l.y+4<=data.viewport.height-data.insets.bottom);}
 for(const l of s.labels)assert(l.x+l.width<=data.viewport.width-data.insets.right,'Other labels cannot claim exposed paint under inspector');const before=f.invalidates;f.scene.revealSelected({insets:data.insets,contextIds:[target]});assert.equal(f.invalidates,before,'Same relation/insets cannot seed an idle paint loop');assert(s.density.offscreenNodes>0,'Partial exposed projection remains honestly incomplete');f.scene.dispose();
});
test('folder loading layout then smaller selected neighborhood settles the latest Worker projection',()=>{
 const f=fixture({worker:true}),data=structuredClone(selectedRelation),target=data.nodes.find(n=>n.label==='code_query.py').id;
 const folder={...data,status:'loading',nodes:[...data.nodes,...Array.from({length:150},(_,i)=>({id:`sibling-${i}`,kind:'file',label:`sibling-${i}.py`,parent:null}))]};
 f.scene.resize(data.viewport.width,data.viewport.height);f.scene.update(folder);
 f.w.onmessage({data:layoutCodeMap(f.sent[0])});f.draw();assert.equal(f.scene.snapshot().density.totalNodes,folder.nodes.length);
 f.scene.select(data.root);f.scene.update(folder);f.scene.revealSelected({insets:data.insets,contextIds:[target]});
 f.scene.update(data);f.scene.select(data.root);f.scene.revealSelected({insets:data.insets,contextIds:[target]});
 const old=f.sent[1];f.w.onmessage({data:layoutCodeMap(old)});assert.equal(f.scene.snapshot().status,'layout-pending');
 f.w.onmessage({data:layoutCodeMap(f.sent[2])});f.draw();const s=f.scene.snapshot();
 assert.equal(s.status,'ready');assert.equal(s.selected,data.root);assert.equal(s.density.totalNodes,data.nodes.length);
 assert(s.labels.some(l=>l.id===data.root));assert(s.labels.some(l=>l.id===target));f.scene.dispose();
});
test('late relation/context reveal cannot undo user pan/zoom and context is bounded to real endpoints',()=>{
 const f=fixture({worker:true}),data=structuredClone(selectedRelation),target=data.nodes.find(n=>n.label==='code_query.py').id;f.scene.resize(data.viewport.width,data.viewport.height);f.scene.update(data);const request=f.sent[0];f.w.onmessage({data:{serial:request.serial,namespace:request.namespace,positions:data.positions}});f.scene.select(data.root);f.scene.zoom(1.2);const camera=f.scene.snapshot().camera;f.scene.revealSelected({insets:data.insets,contextIds:[target]});f.draw();assert.deepEqual(f.scene.snapshot().camera,camera);assert.equal(f.scene.snapshot().revealStatus,'user-camera');
 assert.throws(()=>f.scene.revealSelected({contextIds:Array(33).fill(target)}));assert.throws(()=>f.scene.revealSelected({contextIds:[target,target]}));assert.throws(()=>f.scene.revealSelected({contextIds:[{}]}));f.scene.dispose();
});
test('pending relation reveal is discarded by newer camera or namespace before worker reply',()=>{
 const f=fixture({worker:true}),data=structuredClone(selectedRelation),target=data.nodes.find(n=>n.label==='code_query.py').id;f.scene.resize(data.viewport.width,data.viewport.height);f.scene.update(data);f.scene.select(data.root);f.scene.revealSelected({insets:data.insets,contextIds:[target]});assert.equal(f.scene.snapshot().revealStatus,'pending');f.scene.zoom(1.1);const camera=f.scene.snapshot().camera,request=f.sent[0];f.w.onmessage({data:{serial:request.serial,namespace:request.namespace,positions:data.positions}});assert.deepEqual(f.scene.snapshot().camera,camera);assert.equal(f.scene.snapshot().revealStatus,null);f.scene.update(map());assert.equal(f.scene.snapshot().revealStatus,null);f.scene.dispose();
});
test('browser-local rendering switch preserves map namespace, camera, selection and layout without source action',()=>{
 const policy=graphicalPresentationPolicy();policy.set('auto');const f=fixture();f.scene.update(map());f.scene.select('structure_preview-file');f.scene.zoom(1.1);f.draw();const before=f.scene.snapshot();
 policy.set('ordinary');const after=f.scene.snapshot();assert.deepEqual(after.camera,before.camera);assert.equal(after.namespace,before.namespace);assert.equal(after.selected,before.selected);assert.equal(f.disposes,1);assert.equal(f.selections.length,0);
 policy.set('auto');assert.equal(f.disposes,2);f.scene.dispose();
});
test('real geometry batches and labels stay at 12 CSS px; camera fits and picking uses scoped ID',()=>{
 const f=fixture();f.scene.update(map());f.draw();const s=f.scene.snapshot();assert.equal(s.status,'ready');assert.equal(s.layoutMode,'main-thread-fallback');assert.equal(s.drawn.nodes,3);assert.equal(s.drawn.edges,2);assert(s.drawn.labels>0);assert(f.primitives.some(p=>p[0]==='line'));assert(f.primitives.filter(p=>p[0]==='glyph').every(p=>p[4]>=11));assert(s.labels.every(l=>l.fontSize===12&&l.x>=0&&l.x+l.width<=800));
 const circle=f.primitives.find(p=>p[0]==='circle');const picked=f.scene.pick(circle[1],circle[2]);assert(picked);f.scene.select(picked,{notify:true});assert.equal(f.selections[0].id,picked);assert.equal(f.selections[0].authorityNamespace,s.namespace);f.scene.dispose();
});
test('same-authority data keeps camera/selection; preview publication clears old namespace',()=>{
 const f=fixture();f.scene.update(map());f.scene.select('structure_preview-file');f.scene.zoom(2);const camera=f.scene.snapshot().camera;f.scene.update(map());assert.deepEqual(f.scene.snapshot().camera,camera);assert.equal(f.scene.snapshot().selected,'structure_preview-file');f.scene.update(map('published'));const s=f.scene.snapshot();assert.equal(s.selected,null);assert(s.namespace.includes('published'));f.draw();assert(s.fittedReset);assert(f.scene.snapshot().labels.every(l=>l.id.startsWith('published-')));f.scene.clear();assert.equal(f.scene.snapshot().namespace,null);f.scene.dispose();
});
test('one worker coalesces data; stale layout cannot paint after authority change or dispose',()=>{
 const f=fixture({worker:true});f.scene.update(map());f.scene.update(map('published'));assert.equal(f.sent.length,1);f.w.onmessage({data:layoutCodeMap(f.sent[0])});assert.equal(f.sent.length,2);assert.equal(f.scene.snapshot().status,'layout-pending');f.w.onmessage({data:layoutCodeMap(f.sent[1])});assert.equal(f.scene.snapshot().status,'ready');const callback=f.w.onmessage;f.scene.dispose();callback({data:layoutCodeMap(f.sent[1])});assert(f.w.stopped);assert.equal(f.scene.snapshot().disposed,true);
});
test('physical canvas replacement detaches old pointers and disposal owns all listeners',()=>{
 const f=fixture();assert(f.c.count()>0);const next=canvas();f.swap(next);assert.equal(f.c.count(),0);assert(next.count()>0);f.scene.dispose();assert.equal(next.count(),0);assert.equal(f.disposes,1);f.scene.dispose();assert.equal(f.disposes,1);
});
test('actual demand graphics consumer schedules once and stops idle; hidden restore and dispose',()=>{
 const f=fixture({real:true});f.scene.update(map());assert.equal(f.pending.size,1);f.pump();assert.equal(f.pending.size,0);f.scene.zoom(1.2);assert.equal(f.pending.size,1);f.pump();assert.equal(f.pending.size,0);f.doc.hidden=true;f.doc.emit('visibilitychange');f.scene.fit();f.pump();assert.equal(f.pending.size,0);f.doc.hidden=false;f.doc.emit('visibilitychange');assert.equal(f.pending.size,1);f.pump();f.scene.dispose();assert.equal(f.pending.size,0);assert.equal(f.c.count(),0);assert.equal(f.doc.count(),0);
});
test('invalid worker coordinates fail closed, oversized updates refuse, no synthetic topology',()=>{
 const f=fixture({worker:true});f.scene.update(map());const r=layoutCodeMap(f.sent[0]);r.positions[0].x=Infinity;f.w.onmessage({data:r});assert.equal(f.scene.snapshot().status,'layout-error');f.draw();assert.equal(f.scene.snapshot().drawn.nodes,0);assert.throws(()=>f.scene.update({...map(),nodes:Array(2001).fill(map().nodes[0])}));f.scene.dispose();
 const empty=fixture();empty.scene.update({...map(),nodes:[],edges:[]});empty.draw();assert.equal(empty.scene.snapshot().drawn.edges,0);empty.scene.dispose();assert.deepEqual(fitCodeMapCamera([],800,600),{x:400,y:300,scale:1});
});
test('replacement disposes complete prior graph adapter, including worker and pointers',()=>{
 const host={},first=fixture({worker:true,host});first.scene.update(map());const next=fixture({host});assert(first.scene.snapshot().disposed);assert(first.w.stopped);assert.equal(first.c.count(),0);assert.equal(first.disposes,1);next.scene.dispose();assert.equal(host.__openClankCodeMapScenes.size,0);
 const actual=fixture({real:true,host});assert.equal(countSceneOwners(host),1);actual.scene.dispose();assert.equal(countSceneOwners(host),0);
});
test('physical pan/zoom keys and pointer picking redraw without extra owner or evidence action',()=>{
 const f=fixture();f.scene.update(map());f.draw();const before=f.scene.snapshot().camera;f.c.emit('keydown',{key:'ArrowRight',preventDefault(){}});assert.notDeepEqual(f.scene.snapshot().camera,before);let prevented=false;f.c.emit('wheel',{clientX:400,clientY:300,deltaY:-100,preventDefault(){prevented=true;}});assert(prevented);assert(f.scene.snapshot().camera.scale>before.scale);f.scene.fit();f.draw();const point=f.primitives.find(p=>p[0]==='circle');f.c.emit('pointerdown',{button:0,pointerId:1,clientX:point[1],clientY:point[2]});f.c.emit('pointerup',{pointerId:1,clientX:point[1],clientY:point[2]});assert.equal(f.selections.length,1);assert.deepEqual(Object.keys(f.selections[0]).sort(),['authorityNamespace','id']);assert.throws(()=>f.scene.zoom(NaN));f.scene.dispose();
});
test('inferred dependencies are dashed, containment distinct; no per-node source payload in worker',()=>{
 const f=fixture(),v=map('published');v.edges[1].state='inferred';f.scene.update(v);f.draw();const relations=f.primitives.filter(p=>p[0]==='line'&&p[6]>=1);assert.equal(relations.length,8,'Three inferred dashes + solid containment + four arrow sides; separate .7px callouts are not relations');assert.equal(f.scene.snapshot().drawn.edges,2);f.scene.dispose();
 const w=fixture({worker:true});const rich=map();rich.nodes[0].body='must not reach layout';w.scene.update(rich);assert(!JSON.stringify(w.sent[0]).includes('must not'));assert(w.sent[0].nodes.every(n=>Object.keys(n).length===3));w.scene.dispose();
});
test('programmatic selection is silent in a controlled owner; physical selection notifies once',()=>{
 const f=fixture();f.scene.update(map());f.scene.select('structure_preview-file');assert.equal(f.selections.length,0);f.scene.select(null);assert.equal(f.selections.length,0);
 let scene,calls=0;scene=createCodeMapScene({canvas:canvas(),workerFactory:()=>null,ResizeObserverClass:null,consumerFactory:o=>({canvas:canvas(),invalidate(){},resize(){},dispose(){}}),ownerHost:{},onSelect:value=>{calls++;scene.select(value?.id??null);}});scene.resize(800,600);scene.update(map());assert(scene.select('structure_preview-file',{notify:true}));assert.equal(calls,1);assert.equal(scene.snapshot().selected,'structure_preview-file');scene.dispose();f.scene.dispose();
});
test('direction arrow tips stop at actual target rim and do not create pickable edge IDs',()=>{
 const f=fixture();f.scene.update(map('published'));f.draw();const lines=f.primitives.filter(p=>p[0]==='line'&&p[6]>=1);assert.equal(lines.length,6);const circles=f.primitives.filter(p=>p[0]==='circle');for(let i=0;i<lines.length;i+=3){const main=lines[i],arrow=lines[i+1];const target=circles.find(p=>p[1]===main[3]&&p[2]===main[4]);assert(target);assert(Math.hypot(arrow[1]-target[1],arrow[2]-target[2])>=target[3]);assert(f.scene.pick(target[1],target[2]));}f.scene.dispose();
});
test('first actual demand paint reports backend and draw state asynchronously without idle RAF',async()=>{
 const f=fixture({real:true});f.scene.update(map());assert.equal(f.pending.size,1);f.pump();assert.equal(f.pending.size,0);await Promise.resolve();const painted=f.changes.at(-1);assert.equal(painted.backend,'canvas2d');assert.equal(painted.drawn.nodes,3);assert.equal(painted.drawn.edges,2);assert(painted.drawn.labels>0);assert(painted.labels.length>0);assert.equal(f.pending.size,0);const calls=f.changes.length;await Promise.resolve();assert.equal(f.changes.length,calls);assert.equal(f.pending.size,0);f.scene.dispose();
});
test('focusable actual consumer canvas is not aria-hidden, including adopted fallback',()=>{
 const f=fixture({real:true});assert.equal(f.c.attributes.find(a=>a.name==='aria-hidden'),undefined);assert.equal(f.c.attributes.find(a=>a.name==='role').value,'img');assert.equal(f.c.tabIndex,0);f.scene.dispose();
 const g=fixture();const next=canvas();next.setAttribute('aria-hidden','true');g.swap(next);assert.equal(next.attributes.find(a=>a.name==='aria-hidden'),undefined);assert.equal(next.tabIndex,0);g.scene.dispose();
});
test('bounded two-pointer pinch preserves midpoint world anchor and cancels without selecting',()=>{
 const f=fixture();f.scene.update(map());const before=f.scene.snapshot().camera;
 f.c.emit('pointerdown',{button:0,pointerId:1,clientX:300,clientY:300});f.c.emit('pointerdown',{button:0,pointerId:2,clientX:500,clientY:300});f.c.emit('pointerdown',{button:0,pointerId:3,clientX:0,clientY:0});assert.equal(f.scene.snapshot().pointerCount,2);
 f.c.emit('pointermove',{pointerId:2,clientX:600,clientY:300});const after=f.scene.snapshot().camera;assert.equal(after.scale,before.scale*1.5);assert(Math.abs((450-after.x)/after.scale-(400-before.x)/before.scale)<1e-8);
 f.c.emit('pointercancel',{pointerId:2,clientX:600,clientY:300});assert.equal(f.scene.snapshot().pointerCount,1);f.c.emit('pointerup',{pointerId:1,clientX:300,clientY:300});assert.equal(f.scene.snapshot().pointerCount,0);assert.equal(f.selections.length,0);
 f.c.emit('pointerdown',{button:0,pointerId:4,clientX:400,clientY:300});f.c.emit('lostpointercapture',{pointerId:4,clientX:400,clientY:300});assert.equal(f.scene.snapshot().pointerCount,0);f.scene.dispose();assert.equal(f.c.count(),0);
});
test('publication cancels old pointer gestures and pending draw reports cannot revive disposed scene',async()=>{
 const f=fixture();f.scene.update(map());f.draw();f.c.emit('pointerdown',{button:0,pointerId:1,clientX:400,clientY:300});assert.equal(f.scene.snapshot().pointerCount,1);f.scene.update(map('published'));assert.equal(f.scene.snapshot().pointerCount,0);f.c.emit('pointerup',{pointerId:1,clientX:400,clientY:300});assert.equal(f.selections.length,0);f.draw();f.scene.dispose();const count=f.changes.length;await Promise.resolve();assert.equal(f.changes.length,count);
});
test('default camera refits on shrink; user camera preserves world center and zoom',()=>{
 const f=fixture();f.scene.update(map());const wide=f.scene.snapshot().camera;f.scene.resize(390,400);f.draw();const small=f.scene.snapshot();assert.equal(small.cameraMode,'default');assert.notDeepEqual(small.camera,wide);assert.equal(small.drawn.nodes,3);assert(small.labels.length>=3);
 f.scene.zoom(1.4);const user=f.scene.snapshot().camera;f.scene.resize(500,450);const next=f.scene.snapshot();assert.equal(next.cameraMode,'user');assert.equal(next.camera.scale,user.scale);assert(Math.abs((250-next.camera.x)/next.camera.scale-(195-user.x)/user.scale)<1e-8);assert(Math.abs((225-next.camera.y)/next.camera.scale-(200-user.y)/user.scale)<1e-8);f.scene.dispose();
});
test('explicit reveal minimally pans glyph and label into insets and is idempotent',()=>{
 const f=fixture();f.scene.update(map());f.scene.select('structure_preview-file');const before=f.scene.snapshot().camera;f.scene.revealSelected({insets:{left:320,right:220,top:20,bottom:30}});f.draw();const s=f.scene.snapshot(),label=s.labels.find(l=>l.id==='structure_preview-file');assert.equal(s.revealStatus,'revealed');assert.equal(s.camera.scale,before.scale);assert(label);assert(label.x>=320&&label.x+label.width<=580);assert(label.y-12>=20&&label.y<=570);const count=f.invalidates;f.scene.revealSelected({insets:{left:320,right:220,top:20,bottom:30}});assert.equal(f.invalidates,count,'Repeated caller notification cannot seed an idle redraw loop');
 f.scene.revealSelected({insets:{left:790}});assert.equal(f.scene.snapshot().revealStatus,'insufficient-space');assert.throws(()=>f.scene.revealSelected({insets:{left:-1}}));assert.throws(()=>f.scene.revealSelected({insets:{owner:1}}));f.scene.dispose();
});
test('reveal waits for pending layout and is cancelled by scope or deliberate camera change',()=>{
 const f=fixture({worker:true});f.scene.update(map());f.scene.select('structure_preview-file');f.scene.revealSelected({insets:{left:300,right:240}});assert.equal(f.scene.snapshot().revealStatus,'pending');f.w.onmessage({data:layoutCodeMap(f.sent[0])});assert.equal(f.scene.snapshot().revealStatus,'revealed');f.scene.update(map());f.scene.revealSelected({insets:{left:300,right:240}});f.scene.zoom(1.2);const camera=f.scene.snapshot().camera;f.w.onmessage({data:layoutCodeMap(f.sent[1])});assert.deepEqual(f.scene.snapshot().camera,camera);assert.equal(f.scene.snapshot().revealStatus,null);f.scene.update(map('published'));assert.equal(f.scene.snapshot().revealStatus,null);f.scene.dispose();
});
test('153 CSS-pixel portrait map keeps coarse hierarchy labels readable using bounded callouts',()=>{
 const f=fixture();f.scene.update(map());f.scene.resize(153,198);f.draw();const s=f.scene.snapshot();assert.equal(s.drawn.nodes,3);assert.equal(s.drawn.labels,3);assert.equal(s.labels[0].id,'structure_preview-repo');for(const l of s.labels){assert.equal(l.fontSize,12);assert(l.x>=0&&l.x+l.width<=153&&l.y>=14&&l.y<=198);for(const circle of f.primitives.filter(p=>p[0]==='circle')){const nearX=Math.max(l.x,Math.min(l.x+l.width,circle[1])),nearY=Math.max(l.y-12,Math.min(l.y+4,circle[2]));assert(Math.hypot(circle[1]-nearX,circle[2]-nearY)>circle[3]+2,'Label box must not cross any node glyph');}}for(let i=0;i<s.labels.length;i++)for(let j=i+1;j<s.labels.length;j++){const a=s.labels[i],b=s.labels[j];assert(!(a.x<b.x+b.width&&a.x+a.width>b.x&&a.y-12<b.y+4&&a.y+4>b.y-12));}f.scene.dispose();
});
test('reported 64-node cold hierarchy exposes all root/folder labels and nonoverlapping glyphs at desktop fit',()=>{
 const f=fixture(),data={...map(),...coldStructure()},folder=data.nodes.find(n=>n.kind==='folder').id;
 f.scene.resize(1280,700);f.scene.update(data);f.draw();const s=f.scene.snapshot(),circles=f.primitives.filter(p=>p[0]==='circle');assert.equal(s.drawn.nodes,64);assert.equal(s.drawn.edges,63);
 const labeled=new Set(s.labels.map(l=>l.id));for(const n of data.nodes.filter(n=>n.kind!=='file'))assert(labeled.has(n.id),`Missing required group label ${n.id}`);
 const xs=circles.map(c=>c[1]),ys=circles.map(c=>c[2]);assert(Math.max(...xs)-Math.min(...xs)>700);assert(Math.max(...ys)-Math.min(...ys)>300);
 for(let i=0;i<circles.length;i++)for(let j=i+1;j<circles.length;j++)assert(Math.hypot(circles[i][1]-circles[j][1],circles[i][2]-circles[j][2])>circles[i][3]+circles[j][3]+2);
 for(const l of s.labels){assert.equal(l.fontSize,12);for(const c of circles){const x=Math.max(l.x,Math.min(l.x+l.width,c[1])),y=Math.max(l.y-12,Math.min(l.y+4,c[2]));assert(Math.hypot(c[1]-x,c[2]-y)>c[3]+2);}}
 f.scene.select(folder);f.scene.zoom(1.2);const camera=f.scene.snapshot().camera;data.nodes.push({id:'nested',parent:folder,kind:'file',label:'nested.py'});data.edges.push({from:folder,to:'nested',relation:'containment',state:'structure'});f.scene.update(data);assert.deepEqual(f.scene.snapshot().camera,camera);assert.equal(f.scene.snapshot().selected,folder);f.scene.dispose();
});
test('same cold-page group coverage oracle rejects the former global leaf-column geometry',()=>{
 const f=fixture({worker:true}),data={...map(),...coldStructure()};f.scene.resize(1280,700);f.scene.update(data);
 // Former layout's star topology: depth * 240, global leaf row * 64,
 // repository at its children's mean. No invented relation or hidden nodes.
 const positions=data.nodes.map((n,i)=>({id:n.id,x:i?240:0,y:i?(i-1)*64:31*64}));
 f.w.onmessage({data:{serial:f.sent[0].serial,namespace:f.sent[0].namespace,positions,bounds:{minX:0,minY:0,maxX:240,maxY:3968}}});f.draw();const s=f.scene.snapshot(),labeled=new Set(s.labels.map(l=>l.id));
 assert(s.drawn.nodes<64);assert.equal(s.density.mode,'overflow');assert(s.density.offscreenNodes>0);assert(data.nodes.some(n=>n.kind!=='file'&&!labeled.has(n.id)),'Any-label/nonempty draw cannot certify useful group coverage');
 const circles=f.primitives.filter(p=>p[0]==='circle'),xs=circles.map(c=>c[1]);assert(Math.max(...xs)-Math.min(...xs)<200,'Old layout still fails useful horizontal spread; new camera refuses to compress it into an all-node fit');f.scene.dispose();
});
test('responsive automatic packing exposes honest overflow with separated glyphs and readable group focus',()=>{
 const measure={measureText:text=>({width:Array.from(text).length*6.5,actualBoundingBoxLeft:0,actualBoundingBoxRight:Array.from(text).length*6.5})},f=fixture({textContext:measure}),data={...map(),...coldStructure()};
 f.scene.update(data);for(const [w,h]of [[390,600],[155,198]]){f.scene.resize(w,h);f.draw();const s=f.scene.snapshot(),circles=f.primitives.filter(p=>p[0]==='circle');assert.equal(s.density.mode,'overflow');assert.equal(s.density.totalNodes,64);assert(s.drawn.nodes<64);assert.equal(s.density.visibleNodes,s.drawn.nodes);assert.equal(s.density.offscreenNodes,64-s.drawn.nodes);assert(s.camera.scale>=.45);assert(s.labels.some(l=>l.id==='cold-000'&&!l.truncated));assert(s.labels.filter(l=>data.nodes.find(n=>n.id===l.id).kind==='folder'&&!l.truncated).length>=2);
  for(let i=0;i<circles.length;i++)for(let j=i+1;j<circles.length;j++)assert(Math.hypot(circles[i][1]-circles[j][1],circles[i][2]-circles[j][2])>circles[i][3]+circles[j][3]+2);for(const l of s.labels){assert.equal(l.fontSize,12);for(const c of circles){const x=Math.max(l.x,Math.min(l.x+l.width,c[1])),y=Math.max(l.y-12,Math.min(l.y+4,c[2]));assert(Math.hypot(c[1]-x,c[2]-y)>c[3]+2);}}
 }f.scene.dispose();
});
test('responsive relayout never repositions protected expansion seeds or changes user zoom',()=>{
 const f=fixture({worker:true});f.scene.update(map());f.w.onmessage({data:layoutCodeMap(f.sent[0])});f.scene.resize(390,600);assert.equal(f.sent.length,2);assert.deepEqual(f.sent[1].viewport,{width:390,height:600});assert.equal(f.sent[1].previous.length,0);const packed=layoutCodeMap(f.sent[1]);f.w.onmessage({data:packed});
 const expanded=map();expanded.nodes.push({id:'new',kind:'file',label:'new.py',parent:'structure_preview-repo'});f.scene.select('structure_preview-file');f.scene.update(expanded);assert.deepEqual(f.sent[2].previous.slice().sort((a,b)=>a.id.localeCompare(b.id)),packed.positions);f.w.onmessage({data:layoutCodeMap(f.sent[2])});const before=f.sent.length;f.scene.resize(155,198);assert.equal(f.sent.length,before,'Resize cannot discard expansion/selection seeds');f.scene.zoom(1.1);const user=f.scene.snapshot().camera;f.scene.resize(200,250);assert.equal(f.scene.snapshot().camera.scale,user.scale);assert.equal(f.sent.length,before);f.scene.dispose();
});
test('physical camera or selection during queued responsive layout protects already visible geometry',()=>{
 for(const action of ['zoom','selection']){const f=fixture({worker:true});f.scene.update(map());const old=layoutCodeMap(f.sent[0]);f.w.onmessage({data:old});f.scene.resize(155,198);const responsive=f.sent[1];if(action==='zoom')f.scene.zoom(1.1);else f.scene.select('structure_preview-file');const camera=f.scene.snapshot().camera;f.w.onmessage({data:layoutCodeMap(responsive)});assert.equal(f.sent.length,3);assert.deepEqual(f.sent[2].previous.slice().sort((a,b)=>a.id.localeCompare(b.id)),old.positions);f.w.onmessage({data:layoutCodeMap(f.sent[2])});assert.deepEqual(f.scene.snapshot().camera,camera);f.scene.dispose();}
});
test('label collision boxes use real weight-aware text advance/ink, not character estimates',()=>{
 const context={measureText:text=>{const width=Array.from(text).reduce((sum,c)=>sum+(c==='W'?19:4),0);return {width,actualBoundingBoxLeft:3,actualBoundingBoxRight:width+5};}},f=fixture({textContext:context}),data=map();data.nodes[0].label='WWWWWW';data.nodes[1].label='iiiiiiii';f.scene.update(data);f.draw();
 for(const l of f.scene.snapshot().labels){const actual=context.measureText(l.text),glyph=f.primitives.find(p=>p[0]==='glyph'&&p[1]===l.text);assert(l.width>=actual.width+actual.actualBoundingBoxLeft+5);assert(glyph[2]-actual.actualBoundingBoxLeft>=l.x);assert(glyph[2]+actual.actualBoundingBoxRight<=l.x+l.width);}
 assert(context.font.includes('12px sans-serif'));f.scene.dispose();
});
test('automatic resize scales backing DPR for CSS zoom, preserving existing cap of two',()=>{
 let callback;const calls=[],surface=canvas(),mount={clientWidth:153,clientHeight:198,getBoundingClientRect:()=>({width:306,height:396})},win={devicePixelRatio:1};
 const scene=createCodeMapScene({mount,canvas:surface,win,doc:null,ownerHost:{},workerFactory:()=>null,ResizeObserverClass:class{constructor(fn){callback=fn;}observe(){}disconnect(){}},consumerFactory:()=>({canvas:surface,resize:(...args)=>calls.push(args),invalidate(){},dispose(){}})});
 assert.deepEqual(calls.at(-1),[153,198,2]);win.devicePixelRatio=3;callback();assert.deepEqual(calls.at(-1),[153,198,2]);scene.dispose();
});
test('fixed CSS mount follows DPR1→2→1 events without camera/layout refit or leaked owner listeners',()=>{
 const calls=[],media=[],surface=canvas(),win=events({devicePixelRatio:1}),mount={clientWidth:800,clientHeight:600,getBoundingClientRect:()=>({width:800,height:600})};let suspended=false,resumes=0;
 win.matchMedia=query=>{const m=events({media:query,matches:false});media.push(m);return m;};
 const create=()=>createCodeMapScene({mount,canvas:surface,id:'dpr-owned',win,doc:null,ownerHost:host,workerFactory:()=>null,ResizeObserverClass:null,consumerFactory:()=>({canvas:surface,resize:(...args)=>calls.push(args),invalidate(){},suspend(){suspended=true;},resume(){suspended=false;resumes++;},dispose(){}})}),host={},scene=create();
 scene.update(map());scene.select('structure_preview-file');scene.zoom(1.2);const before=scene.snapshot();assert.deepEqual(calls.at(-1),[800,600,1]);
 win.devicePixelRatio=2;win.emit('resize');assert.deepEqual(calls.at(-1),[800,600,2]);assert.deepEqual(scene.snapshot().camera,before.camera);assert.equal(scene.snapshot().namespace,before.namespace);assert.equal(scene.snapshot().selected,before.selected);assert.deepEqual(scene.snapshot().layout,before.layout);
 const count=calls.length;win.emit('resize');assert.equal(calls.length,count,'Repeated unchanged signals must not reallocate');const oldMedia=media.find(m=>m.media==='(resolution: 1dppx)');oldMedia.emit('change');assert.equal(calls.length,count,'Window and media events coalesce by effective allocation');
 scene.suspend();win.devicePixelRatio=1;media.findLast(m=>m.media==='(resolution: 2dppx)').emit('change');assert.deepEqual(calls.at(-1),[800,600,1]);assert(suspended);assert.equal(resumes,0);assert.deepEqual(scene.snapshot().camera,before.camera);
 const replaced=create();assert(scene.snapshot().disposed);const afterReplacement=calls.length;oldMedia.emit('change');assert.equal(calls.length,afterReplacement,'Disposed media callbacks cannot touch a replacement');replaced.dispose();assert.equal(win.count(),0);assert(media.every(m=>m.count()===0));win.devicePixelRatio=2;win.emit('resize');assert.equal(calls.length,afterReplacement);
});
test('actual software demand owner reallocates DPR while suspended without scheduling hidden frames',()=>{
 const f=fixture({real:true});f.scene.update(map());f.pump();const before=f.scene.snapshot();f.win.devicePixelRatio=1;f.win.emit('resize');assert.equal(f.c.width,800);assert.equal(f.c.height,600);assert.deepEqual(f.scene.snapshot().camera,before.camera);assert.equal(f.pending.size,1);f.pump();assert.equal(f.pending.size,0);
 f.scene.suspend();f.win.devicePixelRatio=2;f.win.emit('resize');assert.equal(f.c.width,1600);assert.equal(f.c.height,1200);assert.equal(f.pending.size,0);assert.deepEqual(f.scene.snapshot().camera,before.camera);f.scene.resume();assert.equal(f.pending.size,1);f.pump();assert.equal(f.pending.size,0);f.scene.dispose();assert.equal(f.win.count(),0);
});
test('presentation remount uses current transformed DPR and retained namespace/camera with allocation cap',()=>{
 const policy=graphicalPresentationPolicy();policy.set('auto');const calls=[],surface=canvas(),win=events({devicePixelRatio:1}),mount={clientWidth:800,clientHeight:600,getBoundingClientRect:()=>({width:1600,height:1200})};
 const scene=createCodeMapScene({mount,canvas:surface,win,doc:null,ownerHost:{},workerFactory:()=>null,ResizeObserverClass:null,consumerFactory:()=>({canvas:surface,resize:(...args)=>calls.push(args),invalidate(){},dispose(){}})});scene.update(map());scene.select('structure_preview-file');scene.zoom(1.2);const before=scene.snapshot();assert.deepEqual(calls.at(-1),[800,600,2]);policy.set('ordinary');assert.deepEqual(calls.at(-1),[800,600,2]);assert.deepEqual(scene.snapshot().camera,before.camera);assert.equal(scene.snapshot().namespace,before.namespace);assert.equal(scene.snapshot().selected,before.selected);const count=calls.length;win.devicePixelRatio=3;win.emit('resize');assert.equal(calls.length,count,'Already capped effective DPR does not reallocate');scene.dispose();policy.set('auto');
});
