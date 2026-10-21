import {forceSimulation,forceManyBody,forceLink,forceCollide,forceX,forceY} from '../vendor/d3-force-3.0.0.js';
import { resolveGraphSettings } from './graphSettings.js';
// Deterministic bounded CPU layout, not a GPU simulation or evidence producer.
const MAX_NODES=2000,MAX_EDGES=5000,MAX_BYTES=4*1024*1024;
const bytes=v=>new TextEncoder().encode(JSON.stringify(v)).length;
function need(ok){if(!ok)throw Error('Invalid compact map layout');}
export function validateLayoutInput(input){
  need(input&&Object.keys(input).every(k=>['serial','namespace','nodes','edges','previous','anchor','viewport','presentation','settings'].includes(k))&&[7,8,9].includes(Object.keys(input).length)&&(!Object.hasOwn(input,'presentation')||['map','web','gravity'].includes(input.presentation))&&['serial','namespace','nodes','edges','previous','anchor','viewport'].every(k=>Object.hasOwn(input,k)));
  need(input&&Number.isSafeInteger(input.serial)&&input.serial>0&&typeof input.namespace==='string'&&input.namespace.length<=16384&&bytes(input)<=MAX_BYTES);
  need(Array.isArray(input.nodes)&&input.nodes.length<=MAX_NODES&&Array.isArray(input.edges)&&input.edges.length<=MAX_EDGES&&Array.isArray(input.previous)&&input.previous.length<=MAX_NODES);
  const exact=(v,fields)=>v&&Object.keys(v).length===fields.length&&fields.every(k=>Object.hasOwn(v,k));
  need(exact(input.viewport,['width','height'])&&[input.viewport.width,input.viewport.height].every(v=>Number.isFinite(v)&&v>=1&&v<=8192));
  const ids=new Set();for(const n of input.nodes){need(exact(n,['id','parent','kind'])&&typeof n.id==='string'&&n.id.length>0&&n.id.length<=256&&!ids.has(n.id)&&['repository','folder','file','symbol'].includes(n.kind)&&(n.parent===null||typeof n.parent==='string'&&n.parent.length<=256));ids.add(n.id);}
  for(const e of input.edges)need((exact(e,['from','to'])||exact(e,['from','to','relation']))&&ids.has(e.from)&&ids.has(e.to));
  const prior=new Set();for(const p of input.previous){need(exact(p,['id','x','y'])&&ids.has(p.id)&&!prior.has(p.id)&&Number.isFinite(p.x)&&Number.isFinite(p.y)&&Math.abs(p.x)<=1e7&&Math.abs(p.y)<=1e7);prior.add(p.id);}
  need(input.anchor===null||ids.has(input.anchor));return input;
}
export function layoutCodeMap(input){
  validateLayoutInput(input);
  if(input.presentation&&input.presentation!=='map')return networkLayout(input);
  const nodes=new Map(input.nodes.map(n=>[n.id,n])),children=new Map(),neighbors=new Map(),ranks=new Map();
  for(const id of nodes.keys())neighbors.set(id,[]);
  for(const e of input.edges){neighbors.get(e.from).push(e.to);neighbors.get(e.to).push(e.from);}
  const roots=[];for(const n of nodes.values()){
    const seen=new Set();let current=n.id;while(nodes.has(current)){need(!seen.has(current));seen.add(current);current=nodes.get(current).parent;}
    if(n.parent===null||!nodes.has(n.parent))roots.push(n.id);else{if(!children.has(n.parent))children.set(n.parent,[]);children.get(n.parent).push(n.id);}
  }
  const ordered=[...nodes.keys()].sort();ordered.forEach((id,i)=>ranks.set(id,i));
  // Two bounded barycentric ordering passes use actual adjacency while never
  // moving a node out of its declared containment branch.
  for(let pass=0;pass<2;pass++){
    const score=id=>{const adjacent=neighbors.get(id);return adjacent.length?adjacent.reduce((sum,n)=>sum+ranks.get(n),0)/adjacent.length:ranks.get(id);};
    for(const group of [roots,...children.values()])group.sort((a,b)=>score(a)-score(b)||a.localeCompare(b,'en'));
    let rank=0;const queue=[...roots];for(let i=0;i<queue.length;i++){const id=queue[i];ranks.set(id,rank++);queue.push(...(children.get(id)||[]));}
  }
  const positions=new Map(),boxes=new Map();
  // Pack REAL sibling subtrees in shelves rather than assigning every leaf a
  // global vertical row. A folder's reserved width leaves room for its fixed
  // screen-size label at a useful coarse fit; no grouping nodes/edges are added.
  // This is bounded internal worker metadata, never native/API authority.
  const aspect=Math.max(.1,Math.min(4,input.viewport.width/input.viewport.height));
  const availableShelfWidth=Math.max(190,(input.viewport.width-32)/.45-240);
  function pack(ids){
    if(!ids.length)return {width:0,height:0,slots:[]};
    const area=ids.reduce((sum,id)=>sum+boxes.get(id).width*boxes.get(id).height,0);
    const target=Math.max(...ids.map(id=>boxes.get(id).width),Math.min(availableShelfWidth,Math.sqrt(area*aspect)));
    let x=0,y=0,rowHeight=0,width=0;const slots=[];
    for(const id of ids){const b=boxes.get(id);if(x&&x+b.width>target){y+=rowHeight;x=0;rowHeight=0;}slots.push({id,x,y});x+=b.width;rowHeight=Math.max(rowHeight,b.height);width=Math.max(width,x);}
    return {width,height:y+rowHeight,slots};
  }
  // Iterative bottom-up rectangles avoid recursion on a deep containment tree.
  const stack=roots.slice().reverse().map(id=>({id,depth:0,done:false}));
  while(stack.length){const item=stack.pop(),kids=children.get(item.id)||[];
    if(!item.done&&kids.length){stack.push({...item,done:true});for(let i=kids.length-1;i>=0;i--)stack.push({id:kids[i],depth:item.depth+1,done:false});continue;}
    const siblings=kids.slice().sort((a,b)=>(nodes.get(a).kind==='file')-(nodes.get(b).kind==='file')||ranks.get(a)-ranks.get(b));
    const packed=pack(siblings);
    boxes.set(item.id,{width:kids.length?240+packed.width:190,height:Math.max(64,packed.height),slots:packed.slots});
  }
  const forest=pack(roots),pending=forest.slots.map(p=>({...p}));
  while(pending.length){const p=pending.pop(),b=boxes.get(p.id);positions.set(p.id,{id:p.id,x:p.x,y:p.y+32});for(const child of b.slots)pending.push({id:child.id,x:p.x+240+child.x,y:p.y+child.y});}
  // Existing positions are seeds/anchors, not a reason to restart a simulation.
  // Preserve them exactly on expansion; newly loaded neighbors use their own
  // deterministic hierarchy slots. No idle-time advancement is performed.
  const natural=new Map([...positions].map(([id,p])=>[id,{...p}]));
  for(const p of input.previous)positions.set(p.id,{id:p.id,x:p.x,y:p.y});
  const old=new Set(input.previous.map(p=>p.id)),occupied=new Map();
  const cell=p=>`${Math.floor(p.x/190)},${Math.floor(p.y/64)}`;
  const occupy=p=>{const key=cell(p);if(!occupied.has(key))occupied.set(key,[]);occupied.get(key).push(p);};
  const clashes=p=>{const x=Math.floor(p.x/190),y=Math.floor(p.y/64);for(let dx=-1;dx<=1;dx++)for(let dy=-1;dy<=1;dy++)for(const q of occupied.get(`${x+dx},${y+dy}`)||[])if(Math.abs(p.x-q.x)<190&&Math.abs(p.y-q.y)<64)return true;return false;};
  for(const p of input.previous)occupy(p);
  // Existing coordinates (including selected anchor) remain exact. Locate new
  // slots relative to a surviving parent's seed, then use a bounded outward
  // shelf search. Do not move the old scene or restart a force simulation.
  for(const [id,p] of positions)if(!old.has(id)){
    const parent=nodes.get(id).parent,base=parent&&positions.get(parent),original=parent&&natural.get(parent);
    if(base&&original){p.x+=base.x-original.x;p.y+=base.y-original.y;}
    const start={x:p.x,y:p.y};let attempt=0;
    // One retained point can block at most four candidate lattice slots.
    while(clashes(p)){attempt++;need(attempt<=MAX_NODES*4+1);p.x=start.x+(attempt%45)*190;p.y=start.y+Math.floor(attempt/45)*64;}
    occupy(p);
  }
  const spacing=resolveGraphSettings({common:input.settings||{}}).mapSpacing;for(const p of positions.values())if(!old.has(p.id)){p.x*=spacing;p.y*=spacing;}
  const result=[...positions.values()].sort((a,b)=>a.id.localeCompare(b.id,'en'));
  return {serial:input.serial,namespace:input.namespace,presentation:input.presentation||'map',positions:result,bounds:result.length?{minX:Math.min(...result.map(p=>p.x)),minY:Math.min(...result.map(p=>p.y)),maxX:Math.max(...result.map(p=>p.x)),maxY:Math.max(...result.map(p=>p.y))}:{minX:0,minY:0,maxX:0,maxY:0}};
}

// Finite presentation geometry only. Springs never create evidence edges.
function networkLayout(input){
  const settings=resolveGraphSettings({common:input.settings||{}},input.presentation);
  const ids=input.nodes.map(n=>n.id).sort(),byId=new Map(input.nodes.map(n=>[n.id,n])),prior=new Map(input.previous.map(p=>[p.id,p]));
  const degree=new Map(ids.map(id=>[id,0]));for(const e of input.edges){degree.set(e.from,degree.get(e.from)+1);degree.set(e.to,degree.get(e.to)+1);}
  const radius=id=>settings.nodeRadius*(settings.nodeSizeBasis==='connections'?1+Math.min(2,Math.log2(1+degree.get(id))*.3):byId.get(id).kind==='repository'?1.5:byId.get(id).kind==='folder'?1.3:1);
  const groupFor=id=>{if(settings.grouping==='free')return 'all';let n=byId.get(id),chain=[],seen=new Set();while(n&&!seen.has(n.id)){seen.add(n.id);if(n.kind==='folder')chain.unshift(n.id);n=byId.get(n.parent);}return chain.slice(0,settings.groupDepth).at(-1)||'root';};
  const groupIds=[...new Set(ids.map(groupFor))].sort(),groups=new Map(groupIds.map((id,i)=>[id,{x:(i%Math.ceil(Math.sqrt(groupIds.length)))*settings.clusterSpacing,y:Math.floor(i/Math.ceil(Math.sqrt(groupIds.length)))*settings.clusterSpacing}]));
  const points=ids.map((id,i)=>{const previous=prior.get(id),g=groups.get(groupFor(id)),angle=i*2.399963229728653,r=20*Math.sqrt(i+1);return {id,x:previous?.x??g.x+Math.cos(angle)*r,y:previous?.y??g.y+Math.sin(angle)*r};});
  const links=input.edges.filter(e=>e.from!==e.to).map(e=>({source:e.from,target:e.to,relation:e.relation||'containment'}));
  // Stop the maintained timer immediately. Only this finite worker job ticks;
  // D3 mutates this fresh local geometry, never native nodes or evidence.
  const simulation=forceSimulation(points).stop().alpha(1).alphaMin(.002)
    .alphaDecay(1-Math.pow(.002,1/settings.iterations)).velocityDecay(1-settings.damping)
    .force('charge',forceManyBody().strength(-settings.repulsion).theta(.9))
    .force('links',forceLink(links).id(p=>p.id).distance(e=>settings.linkDistance*(e.relation==='containment'?.8:1)).strength(e=>settings.linkStrength*(e.relation==='containment'?settings.containmentStrength:1)))
    .force('collision',forceCollide(p=>radius(p.id)+settings.collisionPadding+Math.min(45,settings.labelSize*2)).iterations(2))
    .force('x',forceX(p=>input.presentation==='web'?groups.get(groupFor(p.id)).x:0).strength(input.presentation==='web'?settings.gravity*2:settings.gravity))
    .force('y',forceY(p=>input.presentation==='web'?groups.get(groupFor(p.id)).y:0).strength(input.presentation==='web'?settings.gravity*2:settings.gravity));
  if(input.anchor&&prior.has(input.anchor)){const p=points.find(p=>p.id===input.anchor);p.fx=p.x;p.fy=p.y;}
  for(let step=0;step<settings.iterations;step++){simulation.tick();if(simulation.alpha()<simulation.alphaMin())break;}
  simulation.stop();
  const positions=points.map(({id,x,y})=>{need(Number.isFinite(x)&&Number.isFinite(y)&&Math.abs(x)<=1e7&&Math.abs(y)<=1e7);return {id,x,y};});
  return {serial:input.serial,namespace:input.namespace,presentation:input.presentation,positions,
    bounds:positions.length?{minX:Math.min(...positions.map(p=>p.x)),minY:Math.min(...positions.map(p=>p.y)),maxX:Math.max(...positions.map(p=>p.x)),maxY:Math.max(...positions.map(p=>p.y))}:{minX:0,minY:0,maxX:0,maxY:0}};
}

export function createCodeMapLayoutOwner({workerFactory,changed=()=>{}}={}){
  let worker=null,disposed=false,inflight=null,pending=null,latest=0,namespace=null;
  try{worker=workerFactory?.()||null;}catch{worker=null;}
  function dispatch(job){
    if(disposed)return;
    if(!worker){try{const result=layoutCodeMap(job);if(job.serial===latest&&job.namespace===namespace)changed(result,'main-thread-fallback');}catch{changed(null,'layout-error');}return;}
    inflight=job;try{worker.postMessage(job);}catch{fallback();}
  }
  function fallback(){worker?.terminate();worker=null;const job=pending||inflight;pending=null;inflight=null;if(job&&job.serial===latest)dispatch(job);}
  if(worker){worker.onmessage=event=>{
    const job=inflight;if(disposed||!job)return;
    const r=event.data;
    // Unexpected replies do not release the in-flight slot or authorize data.
    if(r?.serial!==job.serial||r?.namespace!==job.namespace)return;
    inflight=null;
    if(job.serial===latest&&job.namespace===namespace){
      if(r.error)changed(null,'layout-error');else changed(r,'worker');
    }
    const next=pending;pending=null;if(next)dispatch(next);
  };worker.onerror=()=>{if(!disposed)fallback();};}
  return {
    submit(input){if(disposed)return false;validateLayoutInput(input);need(input.serial>latest);latest=input.serial;namespace=input.namespace;const job=structuredClone(input);if(inflight)pending=job;else dispatch(job);return true;},
    invalidate(){namespace=null;pending=null;},
    dispose(){disposed=true;latest++;pending=null;inflight=null;if(worker){worker.onmessage=null;worker.onerror=null;worker.terminate();worker=null;}},
    snapshot(){return {disposed,worker:!!worker,inflight:inflight?1:0,pending:pending?1:0,latest,namespace};},
  };
}
