import {forceSimulation,forceManyBody,forceLink,forceCollide,forceX,forceY} from '../vendor/d3-force-3.0.0.js';
import { CODE_GRAPH_DISPLAY, resolveGraphSettings } from './graphSettings.js';
// Deterministic bounded CPU layout, not a GPU simulation or evidence producer.
const {nodes:MAX_NODES,edges:MAX_EDGES,sceneBytes:MAX_BYTES}=CODE_GRAPH_DISPLAY;
const MAX_SYNCHRONOUS_NODES=2000;
const bytes=v=>new TextEncoder().encode(JSON.stringify(v)).length;
function need(ok){if(!ok)throw Error('Invalid compact map layout');}
export function validateLayoutInput(input){
  need(input&&Object.keys(input).every(k=>['serial','namespace','nodes','edges','previous','anchor','viewport','presentation','settings','relayout','pinned'].includes(k))&&[7,8,9,10,11].includes(Object.keys(input).length)&&(!Object.hasOwn(input,'presentation')||['map','web','gravity'].includes(input.presentation))&&['serial','namespace','nodes','edges','previous','anchor','viewport'].every(k=>Object.hasOwn(input,k)));
  need(!Object.hasOwn(input,'relayout') || (input.presentation === 'gravity' && typeof input.relayout === 'boolean'));
  need(input&&Number.isSafeInteger(input.serial)&&input.serial>0&&typeof input.namespace==='string'&&input.namespace.length<=16384&&bytes(input)<=MAX_BYTES);
  need(Array.isArray(input.nodes)&&input.nodes.length<=MAX_NODES&&Array.isArray(input.edges)&&input.edges.length<=MAX_EDGES&&Array.isArray(input.previous)&&input.previous.length<=MAX_NODES);
  const exact=(v,fields)=>v&&Object.keys(v).length===fields.length&&fields.every(k=>Object.hasOwn(v,k));
  need(exact(input.viewport,['width','height'])&&[input.viewport.width,input.viewport.height].every(v=>Number.isFinite(v)&&v>=1&&v<=8192));
  const ids=new Set();for(const n of input.nodes){need(exact(n,['id','parent','kind'])&&typeof n.id==='string'&&n.id.length>0&&n.id.length<=256&&!ids.has(n.id)&&['repository','folder','file','symbol'].includes(n.kind)&&(n.parent===null||typeof n.parent==='string'&&n.parent.length<=256));ids.add(n.id);}
  for(const e of input.edges)need((exact(e,['from','to'])||exact(e,['from','to','relation']))&&ids.has(e.from)&&ids.has(e.to));
  const prior=new Set();for(const p of input.previous){need(exact(p,['id','x','y'])&&ids.has(p.id)&&!prior.has(p.id)&&Number.isFinite(p.x)&&Number.isFinite(p.y)&&Math.abs(p.x)<=1e7&&Math.abs(p.y)<=1e7);prior.add(p.id);}
  const pins=input.pinned||[];
  need(Array.isArray(pins)&&pins.length<=64&&new Set(pins).size===pins.length&&pins.every(id=>prior.has(id)));
  need(input.anchor===null||ids.has(input.anchor));return input;
}
export function layoutCodeMap(input){
  validateLayoutInput(input);
  if(input.presentation==='gravity' && input.previous.length && !input.relayout) return settleGravityArrivals(input);
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

// Membership comes from linked containment in the current projection.
export function codeMapTerritory(id, nodes, {grouping,groupDepth}) {
  if (grouping === 'free') return null;
  const folders = [], seen = new Set();
  let current = nodes.get(id);
  while (current && !seen.has(current.id) && seen.size < 64) {
    seen.add(current.id);
    if (current.kind === 'repository') return grouping === 'workspaces'
      ? current.id : folders.reverse().slice(0,groupDepth).at(-1) || null;
    if (current.kind === 'folder') folders.push(current.id);
    current = nodes.get(current.parent);
  }
  return null;
}

function networkResult(input, points) {
  const positions = points.map(({id,x,y}) => {
    need(Number.isFinite(x) && Number.isFinite(y) && Math.abs(x) <= 1e7 && Math.abs(y) <= 1e7);
    return {id,x,y};
  });
  return {serial:input.serial,namespace:input.namespace,presentation:input.presentation,positions,
    bounds:positions.length?{minX:Math.min(...positions.map(p=>p.x)),minY:Math.min(...positions.map(p=>p.y)),maxX:Math.max(...positions.map(p=>p.x)),maxY:Math.max(...positions.map(p=>p.y))}:{minX:0,minY:0,maxX:0,maxY:0}};
}

function networkNodeRadius(kind, degree, settings) {
  const factor = settings.nodeSizeBasis === 'connections'
    ? 1 + Math.min(2,Math.log2(1 + degree) * .3)
    : kind === 'repository' ? 1.5 : kind === 'folder' ? 1.3 : 1;
  return settings.nodeRadius * factor;
}

function phaseFor(id) {
  let hash = 2166136261;
  for (let i = 0; i < id.length; i++) hash = Math.imul(hash ^ id.charCodeAt(i), 16777619);
  return (hash >>> 0) / 4294967296 * 2 * Math.PI;
}

// Expand a settled neighborhood in place. Each position reserves room for
// glyphs and its arrival motion before the next newcomer chooses a slot.
function settleGravityArrivals(input) {
  const settings = resolveGraphSettings({common:input.settings || {}}, 'gravity');
  const nodes = new Map(input.nodes.map(node => [node.id,node]));
  const ids = [...nodes.keys()].sort();
  const positioned = new Map(input.previous.map(point => [point.id,point]));
  if (positioned.size === ids.length) return {...networkResult(input, ids.map(id => positioned.get(id))),arrivals:[]};

  const neighbors = new Map(ids.map(id => [id, new Set()]));
  const children = new Map(ids.map(id => [id, []]));
  const degrees = new Map(ids.map(id => [id, 0]));
  for (const edge of input.edges) {
    degrees.set(edge.from, degrees.get(edge.from) + 1);
    degrees.set(edge.to, degrees.get(edge.to) + 1);
    if (edge.from === edge.to) continue;
    neighbors.get(edge.from).add(edge.to);
    neighbors.get(edge.to).add(edge.from);
  }
  for (const id of ids) children.get(nodes.get(id).parent)?.push(id);
  const clearance = settings.collisionPadding + Math.min(45,settings.labelSize * 2);
  const footprint = new Map(ids.map(id => [id,networkNodeRadius(nodes.get(id).kind,degrees.get(id),settings) + clearance]));
  const gridStep = Math.max(...footprint.values()) * 2;
  const cell = (x,y) => `${Math.floor(x / gridStep)},${Math.floor(y / gridStep)}`;
  const occupied = new Map();
  function occupy(id, point) {
    const key = cell(point.x,point.y);
    if (!occupied.has(key)) occupied.set(key,[]);
    occupied.get(key).push({...point, radius:footprint.get(id)});
  }
  function available(point, radius) {
    if (Math.abs(point.x) > 1e7 || Math.abs(point.y) > 1e7) return false;
    for (let dx = -1; dx <= 1; dx++) for (let dy = -1; dy <= 1; dy++) {
      for (const old of occupied.get(cell(point.x + dx * gridStep, point.y + dy * gridStep)) || []) {
        const distance = radius + old.radius;
        if ((point.x - old.x) ** 2 + (point.y - old.y) ** 2 < distance ** 2) return false;
      }
    }
    return true;
  }
  const membership = new Map(ids.map(id => [id, codeMapTerritory(id,nodes,settings)]));
  const groupCenters = new Map();
  const settleCenter = (id, point) => {
    const group = membership.get(id);
    const center = groupCenters.get(group) || {x:0,y:0,count:0};
    center.x += point.x; center.y += point.y; center.count++;
    groupCenters.set(group,center);
  };
  const queue = [...positioned.keys()].sort();
  const queued = new Set(queue), arrivals = [], orbitCursor = new Map();
  for (const id of queue) {
    const point = positioned.get(id);
    occupy(id,point);
    settleCenter(id,point);
  }
  const roots = ids.filter(id => !nodes.has(nodes.get(id).parent));
  const fallbackNodes = [...roots, ...ids];
  const orbitStep = Math.max(gridStep * .8,Math.sqrt(settings.repulsion));
  let disconnected = 0, fallbackSlot = 0;
  const origin = positioned.get(roots[0]) || {x:0,y:0};
  const goldenAngle = Math.PI * (3 - Math.sqrt(5));
  for (let index = 0; index < ids.length; index++) {
    if (index === queue.length) {
      while (queued.has(fallbackNodes[disconnected])) disconnected++;
      const next = fallbackNodes[disconnected++];
      queue.push(next);
      queued.add(next);
    }
    const id = queue[index], parent = nodes.get(id).parent;
    const related = [...neighbors.get(id)].sort();
    if (!positioned.has(id)) {
      const anchor = positioned.has(parent) ? parent : related.find(candidate => positioned.has(candidate));
      const group = membership.get(id);
      const totals = groupCenters.get(group);
      const center = positioned.get(anchor) || (totals ? {x:totals.x / totals.count,y:totals.y / totals.count} : {x:0,y:0});
      const radius = footprint.get(id);
      const orbitKey = anchor || group || null;
      const start = orbitCursor.get(orbitKey) || 0;
      const phase = phaseFor(orbitKey || id);
      const baseDistance = Math.max(gridStep, anchor ? settings.linkDistance * (anchor === parent ? .8 : 1) : settings.clusterSpacing);
      let point;
      for (let slot = start; slot < start + 64; slot++) {
        const distance = Math.hypot(baseDistance,orbitStep * Math.sqrt(slot));
        const angle = phase + slot * goldenAngle;
        const candidate = {id,x:center.x + Math.cos(angle) * distance,y:center.y + Math.sin(angle) * distance};
        if (!available(candidate,radius)) continue;
        orbitCursor.set(orbitKey,slot + 1);
        point = candidate;
        break;
      }
      if (!point) orbitCursor.set(orbitKey,start + 64);
      // Overflow grows around the repository in every direction. One
      // shared cursor bounds placement work across crowded neighborhoods.
      while (!point) {
        need(fallbackSlot <= MAX_NODES * 4 + 1);
        const slot = ++fallbackSlot;
        const distance = gridStep * Math.sqrt(slot);
        const angle = slot * goldenAngle;
        const candidate = {id,x:origin.x+distance*Math.cos(angle),y:origin.y+distance*Math.sin(angle)};
        if (available(candidate,radius)) point = candidate;
      }
      const distance = Math.hypot(center.x - point.x,center.y - point.y);
      const motion = distance > 0 ? Math.min(.28,clearance * .65 / distance) : 0;
      arrivals.push({id,x:point.x + (center.x - point.x) * motion,y:point.y + (center.y - point.y) * motion});
      positioned.set(id,point);
      occupy(id,point);
      settleCenter(id,point);
    }
    for (const next of [...children.get(id), parent, ...related]) {
      if (!nodes.has(next) || queued.has(next)) continue;
      queued.add(next);
      queue.push(next);
    }
  }
  const result = {...networkResult(input,ids.map(id => positioned.get(id))),arrivals};
  // Large projections can settle immediately when optional origins exceed
  // the same byte budget as the rest of the scene message.
  return bytes(result)>MAX_BYTES ? {...result,arrivals:[]} : result;
}

// Finite presentation geometry. Springs never create evidence edges.
function networkLayout(input){
  const settings=resolveGraphSettings({common:input.settings||{}},input.presentation);
  const ids=input.nodes.map(n=>n.id).sort(),byId=new Map(input.nodes.map(n=>[n.id,n])),prior=new Map(input.previous.map(p=>[p.id,p]));
  const degree=new Map(ids.map(id=>[id,0]));for(const e of input.edges){degree.set(e.from,degree.get(e.from)+1);degree.set(e.to,degree.get(e.to)+1);}
  const radius=id=>networkNodeRadius(byId.get(id).kind,degree.get(id),settings);
  const membership=new Map(ids.map(id=>[id,codeMapTerritory(id,byId,settings)]));
  const groupIds=[...new Set(membership.values())].filter(Boolean).sort();
  const columns=Math.max(1,Math.ceil(Math.sqrt(groupIds.length)));
  const groups=new Map(groupIds.map((id,i)=>[id,{x:(i%columns)*settings.clusterSpacing,y:Math.floor(i/columns)*settings.clusterSpacing}]));
  const origin={x:0,y:0},center=id=>groups.get(membership.get(id))||origin;
  const points=ids.map((id,i)=>{const previous=prior.get(id),g=center(id),angle=i*2.399963229728653,r=20*Math.sqrt(i+1);return {id,x:previous?.x??g.x+Math.cos(angle)*r,y:previous?.y??g.y+Math.sin(angle)*r};});
  const links=input.edges.filter(e=>e.from!==e.to).map(e=>({source:e.from,target:e.to,relation:e.relation||'containment'}));
  // Stop the maintained timer immediately. Only this finite worker job ticks;
  // D3 mutates this fresh local geometry, never native nodes or evidence.
  const simulation=forceSimulation(points).stop().alpha(1).alphaMin(.002)
    .alphaDecay(1-Math.pow(.002,1/settings.iterations)).velocityDecay(1-settings.damping)
    .force('charge',forceManyBody().strength(-settings.repulsion).theta(.9))
    .force('links',forceLink(links).id(p=>p.id).distance(e=>settings.linkDistance*(e.relation==='containment'?.8:1)).strength(e=>settings.linkStrength*(e.relation==='containment'?settings.containmentStrength:1)))
    .force('collision',forceCollide(p=>radius(p.id)+settings.collisionPadding+Math.min(45,settings.labelSize*2)).iterations(2))
    .force('x',forceX(p=>center(p.id).x).strength(input.presentation==='web'?settings.gravity*2:settings.gravity))
    .force('y',forceY(p=>center(p.id).y).strength(input.presentation==='web'?settings.gravity*2:settings.gravity));
  const pinned=new Set(input.pinned||[]);
  if(input.anchor&&prior.has(input.anchor))pinned.add(input.anchor);
  for(const p of points)if(pinned.has(p.id)){p.fx=p.x;p.fy=p.y;}
  for(let step=0;step<settings.iterations;step++){simulation.tick();if(simulation.alpha()<simulation.alphaMin())break;}
  simulation.stop();
  return networkResult(input,points);
}

export function createCodeMapLayoutOwner({workerFactory,changed=()=>{}}={}){
  let worker=null,disposed=false,inflight=null,pending=null,latest=0,namespace=null;
  try{worker=workerFactory?.()||null;}catch{worker=null;}
  function dispatch(job){
    if(disposed)return;
    if(!worker){try{if(job.nodes.length>MAX_SYNCHRONOUS_NODES){changed(null,'worker-required');return;}const result=layoutCodeMap(job);if(job.serial===latest&&job.namespace===namespace)changed(result,'main-thread-fallback');}catch{changed(null,'layout-error');}return;}
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
