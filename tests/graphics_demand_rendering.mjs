// Opt-in scheduler correctness only. Fake clocks/backends are not GPU or
// browser-performance evidence. Existing substrate tests stay independent.
import assert from 'node:assert/strict';
import test from 'node:test';
import {createSceneOwner,countSceneOwners,getSceneOwner} from '../static/js/graphics/scene-owner.js';
import {createGraphicsConsumer} from '../static/js/graphics/consumer.js';
import {createGraphicsScene} from '../static/js/graphics/scene-state.js';

function events(target={}) {
  const listeners=new Map();
  return Object.assign(target,{
    addEventListener(type,fn){if(!listeners.has(type))listeners.set(type,new Set());listeners.get(type).add(fn);},
    removeEventListener(type,fn){listeners.get(type)?.delete(fn);},
    emit(type,event={}){for(const fn of [...(listeners.get(type)||[])])fn(event);},
    listenerCount(){return [...listeners.values()].reduce((n,s)=>n+s.size,0);},
  });
}
function fixture({reduced=false,hidden=false,raf=true,cancel=true}={}) {
  let seq=0,now=0;const pending=new Map(),motion=events({matches:reduced});
  const win={matchMedia:()=>motion,performance:{now:()=>now}};
  if(raf)win.requestAnimationFrame=fn=>{const id=++seq;pending.set(id,fn);return id;};
  if(cancel)win.cancelAnimationFrame=id=>pending.delete(id);
  const doc=events({hidden,createElement:()=>canvas2d()});
  return {host:{},win,doc,motion,pending,
    pump(time=16){now=time;const callbacks=[...pending.values()];pending.clear();for(const fn of callbacks)fn(time);},
    visibility(value){doc.hidden=value;doc.emit('visibilitychange');},
    reduce(value){motion.matches=value;motion.emit('change');},
  };
}
function owner(f,options={}) {
  return createSceneOwner({id:'demand',host:f.host,win:f.win,doc:f.doc,renderMode:'demand',paint(){},...options});
}
function canvas2d() {
  const ctx={save(){},restore(){},clearRect(){},fillRect(){},setTransform(){},beginPath(){},moveTo(){},lineTo(){},stroke(){},arc(){},fill(){},setLineDash(){}};
  return events({style:{},dataset:{},attributes:[],width:1,height:1,
    getContext(kind){return kind==='2d'&&!this.tainted?ctx:null;},
    setAttribute(){},remove(){this.removed=true;},
  });
}
function mount() {
  return {children:[],appendChild(c){this.children.push(c);c.parentNode=this;},
    replaceChild(next,prev){const i=this.children.indexOf(prev);assert(i>=0);this.children[i]=next;next.parentNode=this;prev.parentNode=null;},
  };
}
// Deliberately implements only the GL protocol, not actual rasterization.
function fakeGL() {
  return new Proxy({}, {get:(_target,key)=>{
    if(/^[A-Z_0-9]+$/.test(String(key)))return key;
    if(key==='getParameter')return p=>p==='MAX_VIEWPORT_DIMS'?new Int32Array([2048,2048]):2048;
    if(key==='getShaderParameter'||key==='getProgramParameter')return()=>true;
    if(String(key).startsWith('create')||key==='getUniformLocation')return()=>({});
    return()=>{};
  }});
}

test('created real-DOM-like canvas supports getter-only dataset without assignment',()=>{
  const f=fixture(),surface=canvas2d(),dataset={};
  Object.defineProperty(surface,'dataset',{get:()=>dataset,configurable:true});
  f.doc.createElement=()=>surface;
  const consumer=createGraphicsConsumer({mount:mount(),id:'getter-only-dataset',win:f.win,doc:f.doc,ownerHost:f.host,renderMode:'demand',forceCanvas2D:true,draw(){}});
  assert.equal(dataset.graphicsSurface,'getter-only-dataset');f.pump();assert.equal(f.pending.size,0);consumer.dispose();assert.equal(countSceneOwners(f.host),0);
});

test('demand construction defers first paint, coalesces and becomes entirely idle',()=>{
  const f=fixture(),calls=[],advances=[];let ready=false;
  const o=owner(f,{scene:{advance(...args){advances.push(args);},markPainted(){}},paint(time,reduced){assert(ready,'No constructor re-entry');calls.push({time,reduced});}});
  o.start();o.start();o.invalidate();o.invalidate();assert.equal(calls.length,0);assert.equal(f.pending.size,1);assert.equal(o.running,true);ready=true;
  f.pump(50);assert.deepEqual(calls,[{time:0,reduced:false}]);assert.equal(f.pending.size,0);assert.equal(o.running,false);
  f.pump(100000);o.invalidate();f.pump(200000);assert.equal(calls.at(-1).time,0);assert.deepEqual(advances,[],'Idle wall time never advances demand simulation');
  o.dispose();assert.equal(countSceneOwners(f.host),0);assert.equal(f.doc.listenerCount(),0);assert.equal(f.motion.listenerCount(),0);
});

test('omitted renderMode retains the existing continuous default',()=>{
  const f=fixture();let paints=0;const o=createSceneOwner({id:'default',host:f.host,win:f.win,doc:f.doc,paint(){paints++;}}).start();
  assert.equal(o.renderMode,'continuous');f.pump(16);assert.equal(paints,1);assert.equal(f.pending.size,1);f.pump(32);assert.equal(paints,2);assert.equal(f.pending.size,1);o.dispose();assert.equal(f.pending.size,0);
});

test('invalidation during paint preserves dirty state and coalesces one follow-up',()=>{
  const f=fixture(),scene=createGraphicsScene({documentState:{value:1}});let paints=0,o;
  o=owner(f,{scene,paint(){if(++paints===1){o.invalidate();o.invalidate();}}}).start();
  f.pump();assert.equal(paints,1);assert.equal(f.pending.size,1);assert.equal(scene.invalidated,true);
  f.pump();assert.equal(paints,2);assert.equal(f.pending.size,0);assert.equal(scene.invalidated,false);o.dispose();
});

test('hidden start and hidden updates paint nothing; restore coalesces one redraw',()=>{
  const f=fixture({hidden:true});let paints=0;const o=owner(f,{paint(){paints++;}}).start();o.invalidate();assert.equal(f.pending.size,0);
  f.visibility(false);o.invalidate();assert.equal(f.pending.size,1);f.pump();assert.equal(paints,1);assert.equal(f.pending.size,0);
  o.invalidate();f.visibility(true);assert.equal(f.pending.size,0);f.pump();assert.equal(paints,1);
  f.visibility(false);f.pump();assert.equal(paints,2);assert.equal(f.pending.size,0);o.dispose();
});

test('suspended updates and host signals stay stopped; resume redraws same owner once',()=>{
  const f=fixture();let paints=0;const o=owner(f,{paint(){paints++;}}).start();o.suspend();o.invalidate();o.onHostSignal();assert.equal(f.pending.size,0);
  o.resume();o.resume();assert.equal(getSceneOwner('demand',f.host),o);assert.equal(f.pending.size,1);f.pump();assert.equal(paints,1);assert.equal(f.pending.size,0);o.dispose();
});

test('reduced motion toggles redraw once without demand simulation or a loop',()=>{
  const f=fixture(),scene=createGraphicsScene({documentState:{selected:'stable'}});scene.update({phase:123});const calls=[];
  const o=owner(f,{scene,paint(time,reduced){calls.push({time,reduced});}}).start();f.pump();
  f.reduce(true);f.reduce(true);assert.equal(f.pending.size,1);f.pump(10000);assert.equal(f.pending.size,0);assert.deepEqual(calls.at(-1),{time:0,reduced:true});assert.equal(scene.phase,123);
  f.reduce(false);f.pump(20000);assert.deepEqual(calls.at(-1),{time:0,reduced:false});assert.equal(scene.phase,123);assert.equal(f.pending.size,0);o.dispose();
});

test('no-rAF demand fallback is deferred, coalesced and cancellable on dispose',async()=>{
  const f=fixture({raf:false,cancel:false});let paints=0,ready=false;
  const o=owner(f,{paint(){assert(ready);paints++;}}).start();o.invalidate();o.invalidate();assert.equal(paints,0);ready=true;
  await Promise.resolve();assert.equal(paints,1);assert.equal(o.running,false);o.invalidate();o.dispose();await Promise.resolve();assert.equal(paints,1);
});

test('no-rAF fallback cancellation respects hide, suspend and replacement',async()=>{
  const f=fixture({raf:false,cancel:false});let paints=0;
  const first=owner(f,{paint(){paints++;}}).start();f.visibility(true);await Promise.resolve();assert.equal(paints,0);
  f.visibility(false);first.suspend();await Promise.resolve();assert.equal(paints,0);first.resume();
  const second=owner(f,{paint(){paints+=10;}}).start();assert.equal(first.disposed,true);await Promise.resolve();assert.equal(paints,10);assert.equal(countSceneOwners(f.host),1);second.dispose();
});

test('uncancellable rAF callback cannot steal or paint a newer demand request',()=>{
  const f=fixture({cancel:false});let paints=0;const o=owner(f,{paint(){paints++;}}).start();const stale=[...f.pending.values()][0];
  o.suspend();o.resume();assert.equal(f.pending.size,2);stale(16);assert.equal(paints,0);assert.equal(o.running,true);f.pump(32);assert.equal(paints,1);assert.equal(f.pending.size,0);o.dispose();
});

test('demand replacement coexists with one unchanged continuous owner',()=>{
  const f=fixture();let demandPaints=0,continuousPaints=0;
  const continuous=owner(f,{id:'animated',renderMode:'continuous',paint(){continuousPaints++;}}).start();
  const first=owner(f,{paint(){demandPaints++;}}).start();f.pump();assert.equal(continuousPaints,1);assert.equal(demandPaints,1);assert.equal(f.pending.size,1);assert.equal(continuous.renderMode,'continuous');
  first.invalidate();const second=owner(f,{paint(){demandPaints+=10;}}).start();assert.equal(first.disposed,true);f.pump();assert.equal(demandPaints,11);assert.equal(continuousPaints,2);assert.equal(f.pending.size,1);assert.equal(countSceneOwners(f.host),2);
  second.dispose();continuous.dispose();assert.equal(f.pending.size,0);assert.equal(countSceneOwners(f.host),0);
});

test('invalid modes fail before replacing an existing owner or creating consumer DOM',()=>{
  const f=fixture(),o=owner(f).start(),m=mount();assert.throws(()=>owner(f,{renderMode:'almost-demand'}),/invalid graphics render mode/);assert.equal(getSceneOwner('demand',f.host),o);
  assert.throws(()=>createGraphicsConsumer({mount:m,renderMode:'unknown',draw(){}}),/invalid graphics render mode/);assert.equal(m.children.length,0);o.dispose();
});

for(const [renderMode,reduced]of [['continuous',false],['continuous',true],['demand',false],['demand',true]]){
 test(`isAlive false in scheduled frame disposes ${renderMode}, reduced=${reduced}`,()=>{
  const f=fixture({reduced});let alive=true,paints=0;
  const o=owner(f,{renderMode,isAlive:()=>alive,paint(){paints++;}}).start();assert.equal(f.pending.size,1);
  alive=false;assert.doesNotThrow(()=>f.pump());assert.equal(paints,0);assert.equal(o.disposed,true);assert.equal(countSceneOwners(f.host),0);assert.equal(f.pending.size,0);assert.equal(f.doc.listenerCount(),0);assert.equal(f.motion.listenerCount(),0);
 });
}
for(const [renderMode,reduced]of [['continuous',true],['demand',false],['demand',true]]){
 test(`isAlive false before one-shot scheduling disposes ${renderMode}, reduced=${reduced}`,()=>{
  const f=fixture({reduced});let paints=0;
  const o=owner(f,{renderMode,isAlive:()=>false,paint(){paints++;}});
  assert.doesNotThrow(()=>o.start());assert.equal(paints,0);assert.equal(o.disposed,true);assert.equal(countSceneOwners(f.host),0);assert.equal(f.pending.size,0);assert.equal(f.doc.listenerCount(),0);assert.equal(f.motion.listenerCount(),0);
 });
}

test('consumer forwards demand through initial Canvas2D fallback and updates/resize',()=>{
  const f=fixture(),m=mount(),calls=[];let consumer;
  consumer=createGraphicsConsumer({mount:m,id:'fallback-demand',ownerHost:f.host,win:f.win,doc:f.doc,renderMode:'demand',forceCanvas2D:true,
    draw({backend,scene,time,batch}){assert(consumer,'Draw must be deferred until construction returns');calls.push({backend:backend.kind,scene,time});batch.rect(0,0,2,2,'#fff');},
  });
  const o=consumer.owner;assert.equal(o.renderMode,'demand');assert.equal(calls.length,0);consumer.resize(100,80,2);consumer.update({documentState:{selection:'saved'}});assert.equal(f.pending.size,1);f.pump();
  assert.equal(calls.length,1);assert.equal(calls[0].backend,'canvas2d');assert.equal(calls[0].time,0);assert.equal(f.pending.size,0);assert.equal(consumer.scene.width,100);assert.equal(consumer.scene.dpr,2);assert.equal(consumer.scene.documentState.selection,'saved');
  consumer.suspend();consumer.update({style:{alpha:.5}});assert.equal(f.pending.size,0);consumer.resume();f.pump(99999);assert.equal(calls.length,2);assert.equal(consumer.owner,o);assert.equal(countSceneOwners(f.host),1);consumer.dispose();assert.equal(f.pending.size,0);assert.equal(countSceneOwners(f.host),0);
});

test('consumer context loss keeps same demand owner/state and repaints fallback once',()=>{
  const f=fixture(),m=mount(),canvas=canvas2d(),gl=fakeGL(),calls=[],changes=[];m.appendChild(canvas);
  const c=createGraphicsConsumer({mount:m,canvas,id:'loss-demand',ownerHost:f.host,win:f.win,doc:f.doc,renderMode:'demand',
    createWebGL2(target){target.tainted=true;return gl;},onCanvasChange(next,prev){changes.push({next,prev});},
    draw({backend,scene,batch}){calls.push({kind:backend.kind,scene});batch.rect(0,0,1,1,'#fff');},
  });
  assert.equal(c.backendKind,'webgl2');const o=c.owner,scene=c.scene;c.resize(120,90,2);c.update({documentState:{selected:'retained'}});f.pump();assert.equal(calls.length,1);assert.equal(f.pending.size,0);
  f.visibility(true);canvas.emit('webglcontextlost',{preventDefault(){}});assert.equal(c.backendKind,'canvas2d');assert.equal(f.pending.size,0);assert.equal(c.owner,o);assert.equal(c.scene,scene);assert.equal(changes.length,1);assert.notEqual(c.canvas,canvas);assert.equal(c.canvas.parentNode,m);assert.equal(c.scene.documentState.selected,'retained');assert.equal(c.scene.width,120);assert.equal(c.scene.dpr,2);
  f.visibility(false);c.invalidate();assert.equal(f.pending.size,1);f.pump();assert.equal(calls.length,2);assert.equal(calls.at(-1).kind,'canvas2d');assert.equal(f.pending.size,0);assert.equal(countSceneOwners(f.host),1);c.dispose();
});
