// Pure, bounded presentation state. These are play points, never work progress.
// A revision is an opaque content digest; incompatible duplicates cannot be ordered.
export function canonicalUsage(usage) {
  const contributions = new Map(), conflicts = new Set();
  const contributionValue = (item) => JSON.stringify([item.revision,item.attribution,
    item.actorRef?.sessionId ?? null,item.actorRef?.actorId ?? null,item.actorRef?.executionRevision ?? null,
    ...['input_tokens','output_tokens','cache_read_tokens','cache_write_tokens','reasoning_tokens'].map((key)=>[item.metrics?.[key]?.state ?? null,item.metrics?.[key]?.value ?? null])]);
  for (const item of usage || []) {
    if (typeof item?.identity !== 'string' || !item.identity || item.identity.length>256) continue;
    const key=item.identity;
    if (conflicts.has(key)) continue;
    if (typeof item.revision !== 'string' || !/^[0-9a-f]{64}$/.test(item.revision)) { contributions.delete(key); conflicts.add(key); continue; }
    const prior=contributions.get(key);
    if (!prior) contributions.set(key,item);
    else if (contributionValue(prior)!==contributionValue(item)) { contributions.delete(key); conflicts.add(key); }
  }
  return {items:[...contributions.values()], unavailableUsageCount:conflicts.size};
}

export function derivePlay(events, usage) {
  const actions = new Map();
  for (const e of events || []) {
    const r = e?.sourceRef;
    if (!r || e.coverage !== 'qualified_terminal') continue;
    const key = JSON.stringify([r.session_handle, r.actor_id, r.message_id, r.part_id]);
    if (!actions.has(key) || e.sourceRevision > actions.get(key).sourceRevision) actions.set(key, e);
  }
  const canonical=canonicalUsage(usage);
  let tokens = 0, reported = false, unassignedTokens = 0;
  const actors = new Map();
  const entry = (key) => { if (!actors.has(key)) actors.set(key,{actions:0,tokens:0,reported:false}); return actors.get(key); };
  for (const e of actions.values()) { const key = JSON.stringify([e.sessionId,e.actorId,e.executionRevision]); entry(key).actions++; }
  for (const item of canonical.items) {
    let actorTokens = 0, hasTokens = false;
    for (const metric of ['input_tokens','output_tokens']) {
      const value = item.metrics?.[metric]?.value;
      if (Number.isSafeInteger(value) && value >= 0) { tokens += value; actorTokens += value; reported = true; hasTokens = true; }
    }
    if (item.attribution === 'actor' && item.actorRef) {
      const a = item.actorRef, key = JSON.stringify([a.sessionId,a.actorId,a.executionRevision]), target=entry(key);
      target.tokens += actorTokens; target.reported ||= hasTokens;
    } else unassignedTokens += actorTokens;
  }
  return { version:1, points: actions.size * 10 + Math.floor(tokens / 1000), actionCount:actions.size,
    tokens: reported ? tokens : null, unassignedTokens, unavailableUsageCount:canonical.unavailableUsageCount, actors, actions:[...actions.values()].slice(-128) };
}

// An original 10×12 toy board: each canonical terminal action yields one
// deterministic multi-cell piece. Candidate ordering favours clears, then
// fewer holes, then lower stack. No state here controls real work.
const SHAPES = [ [[0,0],[1,0],[2,0],[3,0]], [[0,0],[1,0],[0,1],[1,1]],
  [[0,0],[1,0],[2,0],[1,1]], [[0,0],[0,1],[1,1],[2,1]], [[1,0],[2,0],[0,1],[1,1]] ];
const clone = (grid) => grid.map((row)=>row.slice());
function rotations(shape) {
  const found=[], seen=new Set(); let cells=shape;
  for(let n=0;n<4;n++) {
    const minX=Math.min(...cells.map(c=>c[0])), minY=Math.min(...cells.map(c=>c[1]));
    const normalized=cells.map(([x,y])=>[x-minX,y-minY]).sort((a,b)=>a[1]-b[1]||a[0]-b[0]);
    const id=JSON.stringify(normalized); if(!seen.has(id)){seen.add(id);found.push(normalized);}
    cells=normalized.map(([x,y])=>[-y,x]);
  }
  return found;
}
function shapeFor(event) {
  const ref=event.sourceRef || {}; let hash=2166136261;
  for(const ch of JSON.stringify([ref.session_handle,ref.actor_id,ref.message_id,ref.part_id])) { hash^=ch.codePointAt(0); hash=Math.imul(hash,16777619)>>>0; }
  return SHAPES[hash%SHAPES.length];
}
function quality(grid) {
  let holes=0,maxHeight=0,total=0;
  for(let x=0;x<10;x++) { let seen=false,height=0;
    for(let y=0;y<12;y++) { if(grid[y][x]) {if(!seen)height=12-y; seen=true;} else if(seen) holes++; }
    maxHeight=Math.max(maxHeight,height);total+=height;
  }
  return {holes,maxHeight,total};
}
function choose(grid,shape,owner) {
  let best=null;
  for(const cells of rotations(shape)) {
    const width=1+Math.max(...cells.map(c=>c[0]));
    for(let x=0;x<=10-width;x++) {
      const fits=(y)=>cells.every(([dx,dy])=>y+dy<12 && y+dy>=0 && !grid[y+dy][x+dx]);
      if(!fits(0))continue; let y=0;while(fits(y+1))y++;
      const work=clone(grid); for(const [dx,dy] of cells) work[y+dy][x+dx]=owner;
      const cleared=[];for(let row=11;row>=0;row--)if(work[row].every(Boolean)){cleared.push(row);work.splice(row,1);work.unshift(Array(10).fill(''));row++;}
      const q=quality(work), rank=[-cleared.length,q.holes,q.maxHeight,q.total,x];
      if(!best || rank.some((v,i)=>v<best.rank[i] && rank.slice(0,i).every((n,j)=>n===best.rank[j])))
        best={grid:work,cells:cells.map(([dx,dy])=>({x:x+dx,y:y+dy})),cleared,rank};
    }
  }
  return best;
}
export function blockBoard(actions) {
  let grid=Array.from({length:12},()=>Array(10).fill('')), clears=0,resets=0,last=null;
  for(const event of (actions || []).slice(-128)) {
    const owner=JSON.stringify([event.sessionId,event.actorId,event.executionRevision]), shape=shapeFor(event);
    let before=clone(grid), choice=choose(grid,shape,owner), reset=false;
    if(!choice) {grid=Array.from({length:12},()=>Array(10).fill(''));before=clone(grid);resets++;reset=true;choice=choose(grid,shape,owner);}
    grid=choice.grid; clears+=choice.cleared.length; last={before,cells:choice.cells,cleared:choice.cleared,owner,reset};
  }
  return {grid,clears,resets,last};
}

export const ACTIVE_MODES = Object.freeze(['workshop', 'drift', 'idle']);
export const activeMode = (value) => ACTIVE_MODES.includes(value) ? value : 'workshop';
export const actorRunKey = (row) => JSON.stringify([row.accountId ?? null, row.chatId ?? null, row.sessionId, row.actorId, row.executionRevision]);
export const PRESS_CAPACITY = 4;
const MAX_CHARGE = 12;
const PRESS_CYCLE = 1.15;
const PRESS_COOLDOWN = 1.6;
const COAST_SECONDS = 13;
const UNKNOWN_USAGE = Object.freeze({ input: null, output: null });
export const PREVIEW_DEFAULT = Object.freeze({ actors: 4, input: 120, output: 48, usageGap: false });
export const PREVIEW_CALLS_MAX = 14;
const PREVIEW_WORKERS = Object.freeze([
  ['Aster','read',3.5], ['Soot','write',2], ['Copper','bash',1.25], ['Mica','grep',0],
  ['Opal','edit',2.5], ['Basil','glob',.75], ['Pip','skill',1.5], ['Wren','webfetch',.5],
]);
export const PREVIEW_PACE_MAX = PREVIEW_CALLS_MAX * PREVIEW_WORKERS.length;

export function createPreviewWorkers() {
  return PREVIEW_WORKERS.map(([name,category,rate],index) => {
    const worker = { actorId:`demo-${index+1}`, sessionId:'preview', executionRevision:1,
      name, category, rate, offset:.2 + (index * .29) % .65, until:Infinity, state:null, lastEvent:null };
    resetPreviewCadence(worker);
    return worker;
  });
}

export function resetPreviewCadence(worker) {
  worker.until = worker.rate > 0 ? worker.offset / worker.rate : Infinity;
}

export function advancePreviewCadence(worker, elapsed) {
  if (worker.state || worker.rate === 0) return 0;
  worker.until -= elapsed;
  if (worker.until > 0) return 0;
  const arrivals = 1 + Math.floor(-worker.until * worker.rate);
  worker.until += arrivals / worker.rate;
  return arrivals;
}
export const TOOL_CATEGORIES = Object.freeze(['read', 'write', 'edit', 'grep', 'glob', 'bash', 'skill', 'question', 'webfetch']);
const clamp = (n, min, max) => Math.max(min, Math.min(n, max));

export function sampleUsage(previous, current, elapsed) {
  if (!previous || !current || elapsed <= 0) return UNKNOWN_USAGE;
  const before = canonicalUsage(previous), next = canonicalUsage(current);
  if (before.unavailableUsageCount || next.unavailableUsageCount) return UNKNOWN_USAGE;
  const prior = new Map(before.items.map(item => [item.identity, item.revision]));
  const keys = new Set(next.items.map(item=>item.identity));
  if (next.items.some(item => prior.has(item.identity) && prior.get(item.identity) !== item.revision)
      || before.items.some(item => !keys.has(item.identity))) return UNKNOWN_USAGE;
  const added = next.items.filter(item => !prior.has(item.identity));
  const rate = key => {
    const reported = item => item.metrics?.[key]?.state === 'reported'
      && Number.isSafeInteger(item.metrics[key].value) && item.metrics[key].value >= 0;
    if (!next.items.some(reported) || added.some(item => !reported(item))) return null;
    return added.reduce((sum, item) => sum + item.metrics[key].value, 0) / elapsed;
  };
  return { input: rate('input_tokens'), output: rate('output_tokens') };
}

export const createDrift = () => ({
  distance: 0, speed: 0, lane: 1, targetLane: 1, yaw: 0, nextGate: .85, gateIndex: 0,
  gates: [], pickups: 0, pickupCue: 'chip', dodges: 0, maneuver: 'Parked', glint: 0,
  actions:0, curviness:.4, driftTime:0, driftDuration:0, driftCount:0, driftCooldown:0, driftDirection:0,
});

// Smooth deterministic value noise with independent broad and short bends.
function courseNoise(position, salt) {
  const index=Math.floor(position);
  const x=position-index;
  const smooth=x*x*x*(x*(x*6-15)+10);
  const value=i=>{
    let hash=Math.imul(i^salt,0x9e3779b1);
    hash=Math.imul(hash^(hash>>>16),0x85ebca6b);
    hash=Math.imul(hash^(hash>>>13),0xc2b2ae35);
    return ((hash^(hash>>>16))>>>0)/0xffffffff*2-1;
  };
  return value(index)+(value(index+1)-value(index))*smooth;
}

export function driftCourse(distance) {
  return .72*courseNoise(distance*.92,27)+.28*courseNoise(distance*1.85,103);
}

export function advanceDrift(road, pace, dt) {
  const target = pace > 0.05 ? clamp((pace * 0.044) + 0.094, 0, 0.75) : 0;
  road.speed += (target - road.speed) * (1 - Math.exp(-dt * 2.1));
  if (!target && road.speed < .008) road.speed = 0;
  road.glint = Math.max(0, road.glint - dt);
  road.curviness += (.4 + .85*(1-Math.exp(-road.actions/90)) - road.curviness) * (1-Math.exp(-dt*1.25));
  road.driftCooldown = Math.max(0,road.driftCooldown-dt);
  road.driftTime = Math.max(0,road.driftTime-dt);
  if (!road.speed) { road.maneuver = 'Parked'; road.yaw = 0; road.driftTime=0; return; }
  road.distance += road.speed * dt;
  const near=driftCourse(road.distance), middle=driftCourse(road.distance+.19), far=driftCourse(road.distance+.38);
  const heading=Math.atan((middle-near)*road.curviness*1.8);
  const upcoming=Math.atan((far-middle)*road.curviness*1.8);
  const turn=(upcoming-heading)*180/Math.PI;
  if (pace>=5 && road.speed>.29 && !road.driftTime && !road.driftCooldown && Math.abs(turn)>Math.max(11,25-pace)) {
    road.driftDirection=Math.sign(turn);
    road.driftDuration=clamp(.75+Math.abs(turn)*.013+pace*.025,.9,1.52);
    road.driftTime=road.driftDuration;
    road.driftCooldown=road.driftDuration+.55;
    road.driftCount++;
  }
  while (road.nextGate < road.distance + 1.25) {
    const n = road.gateIndex++;
    const lanes = [1, 0, 1, 2, 2, 1, 0, 0];
    const pickup = lanes[n % lanes.length];
    const hazard = n % 3 === 0 ? (pickup + 1 + (n % 2)) % 3 : null;
    const palette = road.speed > .58 ? ['chip','star','coil','cell','star']
      : road.speed > .35 ? ['chip','coil','cell','coil'] : ['chip','coil','chip'];
    const kind = palette[n % palette.length];
    road.gates.push({ id: n, at: road.nextGate, pickup, kind, hazard, passed: false });
    road.nextGate += .48 - road.speed * .18 + (n % 3) * .023;
  }
  const next = road.gates.find(gate => !gate.passed && gate.at - road.distance < .42);
  road.targetLane = next?.pickup ?? road.targetLane;
  const sideways = road.targetLane - road.lane;
  road.lane += sideways * (1 - Math.exp(-dt * 5.7));
  const roadAngle=clamp(heading*180/Math.PI,-13,13);
  const slide=road.driftTime>0 ? road.driftDirection*(20+10*Math.min(1,road.driftTime/.24)) : 0;
  const yaw=clamp(roadAngle+sideways*(slide ? 13 : 32)+slide,-42,42);
  road.yaw += (yaw-road.yaw) * (1-Math.exp(-dt*5.9));
  if (!road.glint) road.maneuver = !target ? 'Coasting to rest'
    : Math.abs(road.yaw) > 6 ? 'Sliding toward a pickup' : next?.hazard != null ? 'Passing a hazard' : 'Following the course';
  for (const gate of road.gates) {
    if (gate.passed || road.distance < gate.at) continue;
    gate.passed = true;
    if (Math.abs(road.lane - gate.pickup) < .47) {
      road.pickups++;
      road.pickupCue = gate.kind;
      road.glint = .55;
      road.maneuver = 'Pickup collected';
    }
    if (gate.hazard !== null && Math.abs(road.lane - gate.hazard) >= .5) { road.dodges++; road.glint = .55; road.maneuver = 'Hazard avoided'; }
  }
  road.gates = road.gates.filter(gate => gate.at > road.distance - .1);
  if (road.driftTime>0) road.maneuver='Drifting';
}

export function createPresentation({ paceCeiling = 14 } = {}) {
  const tracks = new Map(), seenEvents = new Set(), cues = [], ambientCues = new Set();
  const road = createDrift();
  let pace = null, age = Infinity, usage = UNKNOWN_USAGE;
  function remember(key) {
    if (seenEvents.has(key)) return false;
    if (seenEvents.size>=4096) seenEvents.delete(seenEvents.values().next().value);
    seenEvents.add(key);
    return true;
  }
  function trackFor(key) {
    let track = tracks.get(key);
    if (!track && tracks.size < 128) {
      track = { charge: 0, elapsed: Infinity, cooldown: 0, cause: '', releaseCause: '', batch: 0, calls: 0, releases: 0 };
      tracks.set(key, track);
    }
    return track;
  }
  function receiptKey(event, run) {
    const kind = ['completion','failure'].includes(event.cue) ? 'terminal' : event.cue || 'call';
    return JSON.stringify([run, event.effectIdentity || event.callId, kind]);
  }
  function record(event) {
    const run = actorRunKey(event), track = trackFor(run);
    if (!track || !remember(receiptKey(event,run))) return false;
    if (event.cue) {
      if (cues.length < 16) cues.push({ kind: event.cue, label: event.label });
      return false;
    }
    if (track.charge===0) ambientCues.add('recharge');
    ambientCues.add('tool');
    track.charge = Math.min(MAX_CHARGE, track.charge + 1);
    track.calls++;
    road.actions++;
    track.cause = event.label;
    return true;
  }
  function apply(events, elapsed, measured = UNKNOWN_USAGE) {
    const arrivals = events.reduce((n, event) => n + Number(record(event)), 0);
    pace = elapsed > 0 ? clamp(arrivals / elapsed, 0, paceCeiling) : null;
    age = 0;
    usage = measured;
  }
  function advance(dt, intensity, drifting, paceMultiplier = 1) {
    age += dt;
    for (const track of tracks.values()) {
      track.elapsed += dt;
      track.cooldown = Math.max(0, track.cooldown - dt);
      if (track.charge < PRESS_CAPACITY || track.cooldown > 0) continue;
      track.batch = track.charge;
      track.charge = 0;
      track.releaseCause = track.cause;
      track.elapsed = 0;
      track.cooldown = PRESS_COOLDOWN;
      track.releases++;
      if (cues.length < 16) cues.push({ kind: 'stamp', label: track.releaseCause });
    }
    const fade = Math.max(0, 1 - Math.max(0, age - 2) / (COAST_SECONDS - 2));
    const motion = intensity === 'quiet' ? .65 : intensity === 'busy' ? 1.2 : 1;
    if (drifting) advanceDrift(road, (pace || 0) * fade * motion * clamp(paceMultiplier,.25,3), dt);
    else road.speed = 0;
  }
  return {
    tracks, road, record, apply, advance,
    setUsage: measured => { usage = measured; },
    hasReceipt: event => seenEvents.has(receiptKey(event,actorRunKey(event))),
    cues: () => cues.splice(0),
    ambientCues() {
      const arrivals=Array.from(ambientCues,kind=>({kind}));
      ambientCues.clear();
      return arrivals;
    },
    reading: () => ({ pace, usage, coasting: age >= COAST_SECONDS, age }),
    active: () => !!cues.length || ambientCues.size>0 || road.speed > 0 || pace > 0 && age < COAST_SECONDS
      || [...tracks.values()].some(track => track.elapsed < PRESS_CYCLE || track.charge >= PRESS_CAPACITY),
    suspend() {
      pace = null;
      age = Infinity;
      usage = UNKNOWN_USAGE;
      road.speed = road.yaw = road.glint = road.driftTime = road.driftCooldown = 0;
      road.maneuver = 'Parked';
      cues.length = 0;
      ambientCues.clear();
      for (const track of tracks.values()) { track.charge = 0; track.elapsed = Infinity; track.cooldown = 0; }
    },
    forget(knownRuns) {
      for (const [run, track] of tracks) {
        if (!knownRuns.has(run) && track.elapsed >= PRESS_CYCLE) tracks.delete(run);
      }
    },
  };
}

export function pressPhase(track) {
  if (!track) return 'settled';
  if (track.elapsed < .28) return 'stamp';
  if (track.elapsed < .78) return 'landing';
  if (track.elapsed < PRESS_CYCLE) return 'settling';
  return track.charge > 0 ? 'charge' : 'settled';
}

// Historical Blocks presentation retained for saved versions. The active
// picker never calls this renderer or emits a Blocks visit.
export function renderBlockBoard(grid, board, { colour, animate = false } = {}) {
  const doc = grid.ownerDocument;
  grid.classList.add('cs-block-grid');
  grid.setAttribute('role','img');
  grid.setAttribute('aria-label',`Toy board, ${board.clears} rows cleared, ${board.resets} toy resets`);
  const tint = actor => colour?.(actor) || '#B7A7E8';
  const shown = animate && board.last ? board.last.before : board.grid;
  const nodes = shown.flatMap((row, y) => row.map((actor, x) => {
    const tile = doc.createElement('span');
    tile.className = actor ? 'cs-block cs-block-base filled' : 'cs-block cs-block-base';
    tile.style.gridColumn=String(x+1); tile.style.gridRow=String(y+1);
    if (actor) tile.style.background=tint(actor);
    return tile;
  }));
  if (animate && board.last) for (const cell of board.last.cells) {
    const tile=doc.createElement('span');
    tile.className='cs-block filled cs-falling';
    tile.style.gridColumn=String(cell.x+1); tile.style.gridRow=String(cell.y+1);
    tile.style.background=tint(board.last.owner); tile.style.setProperty('--cell-row',String(cell.y+1));
    nodes.push(tile);
  }
  grid.replaceChildren(...nodes);
}
