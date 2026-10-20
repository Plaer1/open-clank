/* Original bounded learning models and native DOM renderer.
 * Authoring workflow adapted from OpenIntelligentUI response playbook;
 * initial SVG template prototype omitted after visual review because tables
 * show the actual state more clearly. Source pattern and workflow inspected at
 * pinned upstream commit 8391a94c1c3939c679e40ddd7bfc08a9ef690e70.
 * The MIT License — Copyright (c) Atai Barkai
 * Permission is hereby granted, free of charge, to any person obtaining a copy
 * of this software and associated documentation files (the "Software"), to deal
 * in the Software without restriction, including without limitation the rights
 * to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
 * copies of the Software, and to permit persons to whom the Software is
 * furnished to do so, subject to the following conditions:
 * The above copyright notice and this permission notice shall be included in
 * all copies or substantial portions of the Software.
 * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
 * IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
 * FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
 * AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
 * LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
 * OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
 * SOFTWARE.
 */

const MODELS = new Set(['cs-state-trace', 'games-puzzle-states', 'ai-context-budget', 'cs-graph-search', 'cs-boundary-search', 'cs-box-layout', 'cs-finite-probability', 'cs-feedback-trace', 'cs-compile-map', 'cs-binary-evidence', 'cs-finite-frontier', 'games-network-replay', 'games-generator-check', 'games-payoff-table', 'games-shader-ramp', 'games-asset-handoff', 'ai-model-axes', 'ai-memory-cache', 'ai-delegation-cost', 'ai-native-availability', 'ai-save-revisions', 'ai-tokenization-trace']);
const START = [0, 0, 2, 1];
const DIRECTIONS = [[0, -1, 'up'], [0, 1, 'down'], [-1, 0, 'left'], [1, 0, 'right']];

function finite(value, min, max, label) {
  if (value === '' || value === null || value === undefined) throw new Error(`${label} needs a number.`);
  const number = Number(value);
  if (!Number.isFinite(number) || number < min || number > max) throw new Error(`${label} must be between ${min} and ${max}.`);
  return number;
}

export function puzzleValid(state) {
  if (!Array.isArray(state) || state.length !== 4 || !state.every(Number.isInteger)) return false;
  const [ax, ay, bx, by] = state;
  return ax >= 0 && ax <= 1 && ay >= 0 && ay <= 2 && bx >= 0 && bx <= 2 && by >= 0 && by <= 2 && !(by === ay && (bx === ax || bx === ax + 1));
}

export function puzzleMoves(state, slides = false) {
  if (!puzzleValid(state)) throw new Error('Puzzle state is outside the board or overlaps.');
  const moves = [];
  for (const piece of [0, 2]) for (const [dx, dy, label] of DIRECTIONS) {
    for (let distance = 1; distance <= (slides ? 2 : 1); distance += 1) {
      const next = [...state]; next[piece] += dx * distance; next[piece + 1] += dy * distance;
      if (!puzzleValid(next)) break;
      moves.push({state: next, label: `${piece === 0 ? 'A' : 'B'} ${label}${distance > 1 ? ` ${distance} cells` : ''}`});
    }
  }
  return moves;
}

export function puzzleSearch(slides = false, cap = 256) {
  if (!Number.isInteger(cap) || cap < 1 || cap > 256) throw new Error('Search cap must be 1–256.');
  const queue = [{state: START, path: []}]; const visited = new Set([START.join(',')]); let expanded = 0;
  while (queue.length && expanded < cap) {
    const current = queue.shift(); expanded += 1;
    if (current.state[0] === 1 && current.state[1] === 2) return {status: 'found', path: current.path, visited: visited.size, expanded};
    for (const move of puzzleMoves(current.state, slides)) {
      const key = move.state.join(','); if (visited.has(key)) continue;
      visited.add(key); queue.push({state: move.state, path: [...current.path, move.label]});
    }
  }
  return {status: queue.length ? 'bound reached' : 'exhausted', path: [], visited: visited.size, expanded};
}

export function evaluateLearningAsset(id, input = {}, step = 0) {
  if (!MODELS.has(id)) throw new Error('Unknown learning asset.');
  const at = finite(step, 0, 20, 'Step');
  const n = (key, fallback, min, max) => finite(input[key] ?? fallback, min, max, key);
  const table = (headers, rows, summary, extra = {}) => ({headers, rows, summary, ...extra});
  if (id === 'cs-graph-search') {
    const graphs = {cycle:{A:['B','C'],B:['D'],C:['D'],D:['A'],E:[]}, disconnected:{A:['B'],B:[],C:['D'],D:[],E:[]}, diamond:{A:['B','C'],B:['D'],C:['D'],D:[],E:[]}};
    const graph = graphs[input.graph || 'cycle']; if(!graph)throw new Error('Unknown graph preset.');
    const start = input.start || 'A'; if (!(start in graph)) throw new Error('Unknown start node.');
    const dfs = input.method === 'dfs'; const queue = [{node: start, distance: 0, parent: '—'}]; const discovered = new Set([start]); const trace = [];
    while (queue.length && trace.length < 12) {
      const current = dfs ? queue.pop() : queue.shift(); trace.push([current.node, current.parent, current.distance, queue.map(x => x.node).join(', ') || 'empty']);
      for (const node of graph[current.node]) if (!discovered.has(node)) {discovered.add(node); queue.push({node, parent: current.node, distance: current.distance + 1});}
    }
    return table(['Visit', 'Parent', 'Tree depth', 'Queue/stack before expansion'], trace.slice(0, at + 1), `${dfs ? 'DFS stack' : 'BFS queue'}; ${Object.entries(graph).map(([node,neighbors])=>`${node}→${neighbors.join(',')||'none'}`).join('; ')}. Mark on discovery. ${dfs ? 'DFS tree depth need not be shortest.' : 'BFS minimizes number of edges.'}`, {maxSteps: trace.length - 1});
  }
  if (id === 'cs-boundary-search') {
    const data = input.preset === 'empty' ? [] : input.preset === 'duplicates' ? [1, 2, 2, 2, 5, 8] : [1, 3, 5, 7, 9];
    const target = n('target', 2, -20, 20); let lo = 0; let hi = data.length; const trace = [];
    while (lo < hi) {const mid = Math.floor((lo + hi) / 2); trace.push([lo, hi, mid, data[mid], data[mid] < target ? 'lo = m + 1' : 'hi = m']); if (data[mid] < target) lo = mid + 1; else hi = mid;}
    trace.push([lo, hi, '—', '—', `first position ≥ ${target}: ${lo}`]);
    return table(['lo', 'hi exclusive', 'm', 'a[m]', 'Update'], trace.slice(0, at + 1), `Sorted input [${data}]. Lower bound ${lo}; ${lo < data.length && data[lo] === target ? 'target exists' : 'target absent'}. Empty input returns 0.`, {maxSteps: trace.length - 1});
  }
  if (id === 'cs-box-layout') {
    const width = n('width', 200, 0, 400); const padding = n('padding', 16, 0, 64); const border = n('border', 2, 0, 16); const borderBox = input.sizing === 'border';
    const outer = borderBox ? width : width + 2 * padding + 2 * border; const content = borderBox ? Math.max(0, width - 2 * padding - 2 * border) : width;
    return table(['Quantity', 'CSS pixels'], [['Declared width', width], ['Each side padding', padding], ['Each side border', border], ['Content width', content], ['Outer width', Math.max(outer, 2 * padding + 2 * border)]], `${borderBox ? 'border-box includes padding/border' : 'content-box adds padding/border'}. Content cannot have negative width.`);
  }
  if (id === 'cs-finite-probability') {
    const weights = [n('w0', 1, 0, 12), n('w1', 2, 0, 12), n('w2', 1, 0, 12)]; const indices = input.condition === 'positive' ? [1, 2] : [0, 1, 2];
    const mass = indices.reduce((sum, i) => sum + weights[i], 0); if (!mass) throw new Error('Selected condition has zero probability mass.');
    const expected = indices.reduce((sum, i) => sum + i * weights[i], 0) / mass;
    return table(['Outcome / payoff', 'Weight', 'Conditional probability'], indices.map(i => [i, weights[i], `${weights[i]}/${mass}`]), `Expectation = ${expected}. Renormalize only outcomes permitted by the condition.`);
  }
  if (id === 'cs-feedback-trace') {
    const factor = n('factor', 0.5, -2, 2); let x = n('initial', 8, -100, 100); const rows = [[0, x]];
    for (let i = 1; i <= Math.floor(at); i++) {x *= factor; if (!Number.isFinite(x) || Math.abs(x) > 1000000) return table(['Step', 'x'], rows, 'Stopped at the magnitude bound.'); rows.push([i, x]);}
    return table(['Step', 'x'], rows, 'x_next = a × x. A negative factor flips sign; |a|<1 shrinks magnitude. This scalar toy omits delay and disturbances.', {maxSteps: 20});
  }
  if (id === 'cs-compile-map') {
    const stage = input.stage || 'source'; const stages = {source: ['Source', 'Human-written text', 'Preprocess/compile → object'], object: ['Object', 'Machine instructions, symbols and relocations', 'Link → executable'], executable: ['Executable', 'Linked file with entry and loader metadata', 'Load → process'], process: ['Process', 'Running state, mappings and memory', 'Execution can change memory without changing source']};
    if (!stages[stage]) throw new Error('Unknown artifact stage.');
    return table(['Artifact', 'Contents', 'Next operation'], [stages[stage]], 'These are conceptual artifact relationships. No compiler or program runs in this asset.');
  }
  if (id === 'cs-binary-evidence') {
    const items = {bytes: ['Hand-authored instruction input', 'Given SP+12 contains 2. ldr w8,[sp,#0xc] loads it; add w0,w8,#1 adds 1; ret returns w0=3. No disassembler or program was run.', 'observation'], intent: ['Claim about original intent', 'Rejecting invalid players is a possible intent; this supplied excerpt does not establish the author’s purpose.', 'inference'], original: ['Exact original source', 'The original variable name and comments are recoverable from this excerpt.', 'unknown']}; const key = input.excerpt || 'bytes'; const item = items[key]; if (!item) throw new Error('Unknown excerpt.');
    return table(['Evidence', 'Statement', 'Bounded classification'], [[item[0], item[1], item[2]]], input.category === item[2] ? 'Classification agrees with the evidence boundary. A response still needs your reasoning.' : `Compare your ${input.category || 'observation'} label with the evidence shown; observation, inference and unknown mean different things.`);
  }
  if (id === 'cs-finite-frontier') {
    const options = [['Light', 2, 3, 4], ['Guard', 4, 5, 3], ['Heavy', 6, 7, 1], ['Wasteful', 5, 3, 2], ['Twin', 4, 5, 3]]; const budget = n('budget', 6, 0, 12); const eligible = options.filter(x => x[1] <= budget);
    return table(['Option', 'Cost ≤ budget', 'Protection ↑', 'Speed ↑', 'Status'], eligible.map(x => [...x, eligible.some(y => y[2] >= x[2] && y[3] >= x[3] && (y[2] > x[2] || y[3] > x[3])) ? 'dominated' : 'frontier']), 'Toy objectives maximize protection and speed under the cost constraint. Equal outcomes do not strictly dominate one another.');
  }
  if (id === 'games-network-replay') {
    const generated = n('ack', 2, 0, 20), authority = n('authority', 1, -20, 20), delay = n('delay', 0, 0, 5);
    if (!Number.isInteger(generated) || !Number.isInteger(delay)) throw new Error('Generated tick and delay must be integers.');
    const now = Math.max(Math.floor(at), generated);
    const total = tick => Array.from({length: tick}, (_, i) => i % 3 === 2 ? -1 : 1).reduce((a,b) => a+b, 0);
    const offset = authority - total(generated);
    const events = [{id:'snapshot-0',tick:0,position:0,arrival:0,status:'received'}];
    const incoming = Array.from({length:generated}, (_,i) => ({id:`snapshot-${i+1}`,tick:i+1,position:total(i+1)+offset,arrival:i+1+delay,dropped:input.drop==='third'&&(i+1)%3===0}));
    if (input.packet === 'duplicate') incoming.push({id:`snapshot-${generated}`,tick:generated,position:authority,arrival:generated+delay});
    if (input.packet === 'old') incoming.push({id:'late-old',tick:0,position:99,arrival:generated+delay});
    const accepted = new Map([[0,0]]), seen = new Set(['snapshot-0']); let confirmed = 0;
    for (const packet of incoming.sort((a,b) => a.arrival-b.arrival || a.tick-b.tick)) {
      let status;
      if (packet.dropped) status = 'dropped';
      else if (packet.arrival > now) status = 'pending';
      else if (seen.has(packet.id)) status = 'duplicate ignored';
      else if (packet.tick <= confirmed) status = 'old tick ignored';
      else {status='received';seen.add(packet.id);accepted.set(packet.tick,packet.position);confirmed=packet.tick;}
      events.push({...packet,status});
    }
    const position = accepted.get(confirmed), pending = Array.from({length:now-confirmed}, (_,i) => (confirmed+i)%3===2?-1:1);
    const predicted = position + total(now)-total(confirmed), rawPrediction=total(now), renderTime=Math.max(0,now-1);
    const times = [...accepted.keys()].sort((a,b)=>a-b); let mode, shown, reason;
    if (accepted.has(renderTime)) {mode='exact';shown=accepted.get(renderTime);reason='accepted timestamp';}
    else if(times.some(t=>t>renderTime)) {
      const lo=Math.max(...times.filter(t=>t<renderTime)),hi=Math.min(...times.filter(t=>t>renderTime));
      mode='interpolate';shown=accepted.get(lo)+(accepted.get(hi)-accepted.get(lo))*(renderTime-lo)/(hi-lo);reason='two accepted samples bracket display time';
    } else {mode='hold';shown=accepted.get(times.at(-1));reason=events.some(p=>p.status==='pending')?'later packets pending':events.some(p=>p.status==='dropped')?'known dropped packet':'no newer accepted sample';}
    const receipt=events.map(p=>[p.id,p.tick,p.position,p.arrival,p.status]);
    return table(['Packet identity','Source tick','Source position','Arrival tick','Receipt'],receipt,`Now tick ${now}; accepted ack ${confirmed}: ${position}; pending inputs [${pending}]; restore/replay ${predicted}; correction delta ${predicted-rawPrediction} from local ${rawPrediction}. Remote display at tick ${renderTime}: ${shown} (${mode}; ${reason}). Rendering never changes authority.`,{maxSteps:20,replay:{confirmed,position,pending,predicted,correctionDelta:predicted-rawPrediction,receipt,events},display:{time:renderTime,value:shown,mode,reason,accepted:Object.fromEntries(accepted)}});
  }
  if (id === 'games-generator-check') {
    const seed = n('seed', 0, 0, 15); if (!Number.isInteger(seed)) throw new Error('Seed must be an integer.');
    let random = seed; const grid = Array.from({length:5}, () => Array.from({length:5}, () => {random=(5*random+1)%16;return random<5?'#':'.';}));
    // Original v1 recipe: even seeds carve top+right corridor; odd seeds separate the exit.
    if (seed % 2 === 0) {for(let i=0;i<5;i++){grid[0][i]='.';grid[i][4]='.';}} else {grid[3][4]='#';grid[4][3]='#';}
    grid[0][0]='S';grid[4][4]='E';if(input.repair==='yes')grid[3][4]='.';
    const queue=[[0,0]];const seen=new Set(['0,0']);while(queue.length&&seen.size<=25){const [x,y]=queue.shift();for(const [dx,dy]of DIRECTIONS){const nx=x+dx,ny=y+dy,key=`${nx},${ny}`;if(nx<0||ny<0||nx>=5||ny>=5||grid[ny][nx]==='#'||seen.has(key))continue;seen.add(key);queue.push([nx,ny]);}}
    const validation={valid:seen.has('4,4'),reachable:seen.size};
    const comparisons=input.compare==='yes'?[0,1,2,3].map(seed=>{const result=evaluateLearningAsset(id,{seed,repair:input.repair,compare:'no'});return `seed ${seed}: ${result.validation.valid?'reachable':'invalid'} (${result.validation.reachable} cells)`;}):[];
    return table(['Row y', 'Cells x0–4', 'Reachable coordinates'], grid.map((row,y)=>[y,row.join(' '),[...seen].filter(k=>k.endsWith(','+y)).join('; ')]), `Toy generator v1; seed ${seed}. Ordered recipe: 25 row-major draws s=(5s+1)%16; wall if s<5. Even seeds carve top row/right column; odd seeds block (4,3)/(3,4). Place S(0,0), E(4,4); selected repair opens (4,3) last. Four-neighbor validation; ${validation.valid?'exit reachable':'INVALID: exit unreachable'}; ${seen.size}/25 cells reachable. Seed alone is not a complete recipe. ${comparisons.join('; ')}`,{validation,comparisons});
  }
  if (id === 'games-payoff-table') {
    const payoffs = Array.from({length:4},(_,i)=>[n('a'+i,[3,0,5,1][i],-5,5),n('b'+i,[3,5,0,1][i],-5,5)]);const p=n('probability',0.5,0,1);
    if(!payoffs.flat().every(Number.isInteger))throw new Error('Payoffs must be integers from −5 to 5.');
    const rows=payoffs.map((pair,i)=>{const row=Math.floor(i/2),col=i%2;const bestA=pair[0]>=payoffs[(1-row)*2+col][0],bestB=pair[1]>=payoffs[row*2+(1-col)][1];return [row?'A1':'A0',col?'B1':'B0',...pair,bestA?'A best response':'—',bestB?'B best response':'—',bestA&&bestB?'pure equilibrium':'—'];});
    return table(['A choice','B choice','A payoff','B payoff','A comparison','B comparison','Equilibrium'],rows,`When B0 is chosen with probability ${p}, A0 expected payoff=${p*payoffs[0][0]+(1-p)*payoffs[1][0]}; A1=${p*payoffs[2][0]+(1-p)*payoffs[3][0]}. Ties count as best responses. This is not a general mixed-equilibrium solver.`);
  }
  if (id === 'games-shader-ramp') {
    const u=n('sample',0.5,0,1),strength=n('strength',0.5,0,1),surround1=n('surround1',0.2,0,1),surround2=n('surround2',0.8,0,1);const level=(0.2+0.6*u)*strength;
    return table(['Quantity','Stored numeric value'],[['u',u],['strength',strength],['patch in surround 1',level],['patch in surround 2',level],['surround 1',surround1],['surround 2',surround2]],'level=(0.2+0.6u)×strength. Surround changes context, never the stored patch value. This computes a toy grayscale value; no shader is executed.',{swatches:[{surround:surround1,value:level},{surround:surround2,value:level}]});
  }
  if (id === 'games-asset-handoff') {
    const format=input.contract||'mesh'; const rows=[['Dimensions 2×1×1','retained'],['Marked front face','needs import orientation test'],['UV coordinates',format==='mesh'?'retained':'needs test'],['Baked base-color texture',format==='baked'?'retained':'not in this delivery contract'],['Editable procedural nodes','lost'],['Original rig/clip',format==='animated'?'retained':'lost']];
    return table(['Original toy property','Declared delivery result'],rows,`Toy contract ${format}; these labels are not claims about glTF/FBX/USD vendor exporters. Retain editable source and verify an actual import separately.`);
  }
  if (id === 'ai-model-axes') {
    const cards=[['Linear predictor',['linear'],['supervised'],['regression'],['numbers']],['Decision tree',['tree'],['supervised'],['classification','regression'],['tabular']],['Image CNN',['convolution'],['supervised'],['classification'],['image']],['Sequence RNN',['recurrent'],['self-supervised'],['prediction'],['text']],['Encoder transformer',['attention'],['self-supervised','supervised'],['embedding','classification'],['text']],['Decoder transformer',['attention'],['self-supervised'],['generation','prediction'],['text']],['Diffusion model',['denoising','attention'],['self-supervised'],['generation'],['image']],['Graph network',['message passing'],['supervised'],['prediction'],['graph']]]; const index={architecture:1,training:2,task:3,modality:4}[input.axis||'architecture'];if(!index)throw new Error('Unknown model axis.');
    const selected=n('card',0,0,7);if(!Number.isInteger(selected))throw new Error('Choose one of the eight cards.');const choice=input.category||'linear',matches=cards[selected][index].includes(choice);
    return table(['Synthetic card',input.axis||'architecture','Your selected card'],cards.map((row,i)=>[row[0],row[index].join('; '),i===selected?'selected':'—']),`${cards[selected][0]}: your ${choice} category ${matches?'matches one declared label':'does not match this declared axis; compare the shown labels'}. Overlapping labels are intentional; axis choice and family name do not determine a product capability. This is local feedback, not a grade.`,{feedback:{matches,expected:cards[selected][index]}});
  }
  if (id === 'ai-memory-cache') {
    const stored=input.stored==='yes',supplied=input.supplied==='yes',cached=input.cached==='yes';
    return table(['Layer','Toy state'],[['Persistent preference',stored?'stored':'absent'],['Preference supplied this request',supplied?'present':'absent'],['Prompt prefix cache',cached?'reused computation':'not reused'],['Information available to this request',supplied?'preference supplied':'no preference supplied']], 'Cache reuse saves eligible computation. It does not add a missing preference or expand context; stored information still needs retrieval/supply.');
  }
  if (id === 'ai-delegation-cost') {
    const workers=n('workers',3,1,4);if(!Number.isInteger(workers))throw new Error('Workers must be an integer.');const lead=n('lead',1000,0,100000),worker=n('worker',1200,0,100000),summary=n('summary',200,0,10000),leadTime=n('leadtime',3,0,120),workerTime=n('workertime',5,0,120);
    return table(['Synthetic receipt','Toy value'],[['Aggregate tokens',lead+worker*workers],['Lead return context',summary*workers],['Parallel elapsed assumption',Math.max(leadTime,workerTime)],['Serial elapsed assumption',leadTime+workerTime*workers],['Worker receipts included',workers]],'Equal illustrative workers run concurrently under this simplifying assumption. Faster elapsed time does not imply fewer aggregate tokens; overlap/scheduling and omitted receipts change the result.');
  }
  if (id === 'ai-native-availability') {
    const cards={editor:['Editor','editor.open','available','writable content'],reference:['Official reference','wiki.reference','available','read-only'],missing:['Unavailable tool','none','unavailable: no destination','not writable'],host:['Host-equivalent label','none','unavailable: label alone supplies no route','not writable']};const row=cards[input.card||'editor'];if(!row)throw new Error('Unknown availability card.');
    return table(['Label','Synthetic destination','Availability / reason','Write capability'],[row],'Discovery is an inventory, not proof that a route works. This asset executes no native command.');
  }
  if (id === 'ai-save-revisions') {
    const changed=input.remote==='yes',action=input.action||'cancel';let buffer='Edited B',disk=changed?'Remote C':'Original A',status='Both revisions preserved.';
    if(action==='save'){if(changed)status='Conflict: expected A, disk C. Preserve buffer B and disk C; reload/compare before another save.';else{disk=buffer;status='Explicit Save copies B to disk in this toy.';}}else if(action==='discard'){buffer=disk;status='Discard chooses current disk for buffer.';}else if(action==='view')status='Switching view writes nothing.';
    return table(['Revision','Synthetic text'],[['Expected base','Original A'],['Editor buffer',buffer],['Disk',disk]],status+' No actual file is written.');
  }
  if (id === 'ai-tokenization-trace') {
    const texts={a:'cats =1;',b:'cat s=1;',unknown:'cats=2;'}, version=input.vocabulary||'v1';
    const source=texts[input.text||'a']; if(source===undefined || !['v1','v2'].includes(version))throw new Error('Choose a supplied input and vocabulary.');
    const vocabulary=[['cat',1],['s',2],[' ',3],['=',4],['1',5],[';',6],...version==='v1'?[['cats',7]]:[],['c',8],['a',9],['t',10]].sort((a,b)=>b[0].length-a[0].length);
    const trace=[],ids=[];let cursor=0,unknown=null;
    while(cursor<source.length && trace.length<8){
      const match=vocabulary.find(([literal])=>source.startsWith(literal,cursor));
      if(!match){unknown=source[cursor];trace.push([cursor,unknown,'unknown; stop',source.slice(cursor)]);break;}
      const [literal,id]=match;trace.push([`${cursor}–${cursor+literal.length}`,literal===' '?'␠ (U+0020)':literal,id,source.slice(cursor+literal.length)||'empty']);ids.push(id);cursor+=literal.length;
    }
    const count=Math.min(Math.floor(at),trace.length),complete=count===trace.length;
    return table(['Codepoint span','Longest literal','Token ID','Remainder'],trace.slice(0,count),`ToyGreedy-${version}, case-sensitive, no normalization/fallback. Vocabulary ${vocabulary.map(([literal,id])=>`${id}:${literal===' '?'␠':literal}`).join(', ')}. ${complete?(unknown?`Unknown ${unknown}; stopped with prefix IDs [${ids}], full count unknown.`:`IDs [${ids}]; ${ids.length} tokens from ${source.length} codepoints.`):`Input ${JSON.stringify(source)}; step ${count}/${trace.length}. Step to inspect each longest match.`} This is not a provider tokenizer or bill.`,{maxSteps:trace.length,tokenization:{ids,unknown,count:unknown?null:ids.length,source,version}});
  }
  if (id === 'ai-context-budget') {
    const total = finite(input.total ?? 10000, 1, 100000, 'Total toy budget');
    const instructions = finite(input.instructions ?? 2000, 0, 100000, 'Instructions and tools');
    const evidence = finite(input.evidence ?? 4000, 0, 100000, 'Selected evidence');
    const reserve = finite(input.reserve ?? 1500, 0, 100000, 'Output reserve');
    const remaining = total - instructions - evidence - reserve;
    return {headers: ['Quantity', 'Toy units'], rows: [['Total', total], ['Instructions/tools', instructions], ['Evidence', evidence], ['Output reserve', reserve], ['Remaining', remaining]],
      summary: remaining < 0 ? `Overflow: reduce allocations by ${-remaining} toy units.` : `${remaining} toy units remain. This is illustrative, not a provider quota.`};
  }
  if (id === 'cs-state-trace') {
    const copy = input.mode === 'copy'; const stage = Math.min(3, Math.floor(at));
    const a = stage >= 2 && !copy ? '[1, 2]' : '[1]';
    const b = stage >= 3 ? '[9]' : stage >= 2 ? '[1, 2]' : '[1]';
    return {headers: ['Name', 'Object identity', 'Value'], rows: [['a', 'object 1', a], ...(stage ? [['b', stage >= 3 ? 'object 3' : copy ? 'object 2' : 'object 1', b]] : [])],
      summary: ['Create a = [1].', copy ? 'b receives a separate shallow copy.' : 'b = a shares object 1.', 'Append 2 through b; mutation changes its object.', 'Rebind b = [9]; a keeps its reference.'][stage]};
  }
  const slides = input.mode === 'slide'; const result = puzzleSearch(slides, Number(input.cap ?? 256));
  const state = input.state ?? START;
  if (!puzzleValid(state)) throw new Error('Invalid board.');
  const rows = Array.from({length: 3}, (_, y) => [y, ...Array.from({length: 3}, (_, x) => y === state[1] && (x === state[0] || x === state[0] + 1) ? 'A' : x === state[2] && y === state[3] ? 'B' : '.')]);
  return {headers: ['y / x', '0', '1', '2'], rows, moves: puzzleMoves(state, slides),
    summary: `${result.status}; ${result.expanded} expanded; ${result.visited} discovered. Shortest route from initial board: ${result.path.length ? result.path.join(' → ') : 'none within bound'}. Each ${slides ? 'unobstructed slide' : 'one-cell move'} costs 1.`};
}

export function mountLearningAsset({host, assetId, unitId, registry, theme}) {
  const records = Array.isArray(registry) ? registry : Object.values(registry || {});
  const record = records.find(item => item.id === assetId);
  const document = host.ownerDocument; const container = document.createElement('section');
  container.className = 'treehouse-learning-asset'; container.dataset.theme = String(theme || 'inherit');
  const message = document.createElement('p'); message.textContent = 'Loading optional practice…'; container.append(message); host.append(container);
  const abort = new AbortController(); let alive = true; const listeners = [];
  function on(node, event, action) { node.addEventListener(event, action); listeners.push(() => node.removeEventListener(event, action)); }
  function destroy() { alive = false; abort.abort(); listeners.splice(0).forEach(remove => remove()); container.remove(); }
  if (!record || !MODELS.has(assetId) || !record.unitIds?.includes(unitId) || !/^static\/treehouse\/assets\/[a-z][a-z0-9-]*\.json$/.test(record.path) || !/^sha256:[a-f0-9]{64}$/.test(record.digest || '')) {
    message.textContent = 'Optional practice unavailable. The written exercise remains usable.'; return {destroy};
  }
  async function load() {
    try {
      const response = await fetch('/' + record.path, {signal: abort.signal, credentials: 'same-origin'});
      if (!response.ok) throw new Error('Asset could not be loaded.');
      const bytes = await response.arrayBuffer(); if (bytes.byteLength > 32768) throw new Error('Asset exceeds its size bound.');
      const hash = await crypto.subtle.digest('SHA-256', bytes);
      const actual = 'sha256:' + [...new Uint8Array(hash)].map(x=>x.toString(16).padStart(2,'0')).join('');
      if (actual !== record.digest) throw new Error('Asset changed. Reload the curriculum.');
      const raw = new TextDecoder('utf-8', {fatal:true}).decode(bytes);
      const config = JSON.parse(raw);
      if (config.id !== assetId || typeof config.title !== 'string' || !Array.isArray(config.fields) || config.fields.length > 12) throw new Error('Invalid asset data.');
      const keys = new Set();
      for (const field of config.fields) {
        if (!field || !/^[a-z][a-z0-9-]*$/.test(field.key) || keys.has(field.key) || typeof field.label !== 'string') throw new Error('Invalid field data.');
        keys.add(field.key);
        if (field.type === 'select') {if(!Array.isArray(field.options)||!field.options.length||field.options.length>24||!field.options.every(x=>x&&typeof x.value==='string'&&typeof x.label==='string')||!field.options.some(x=>x.value===field.default))throw new Error('Invalid choice data.');}
        else if(field.type!=='number'||!Number.isFinite(field.min)||!Number.isFinite(field.max)||field.min>field.max||!Number.isFinite(Number(field.default))||Number(field.default)<field.min||Number(field.default)>field.max)throw new Error('Invalid number field.');
      }
      if (!alive) return;
      container.replaceChildren();
      const heading = document.createElement('h4'); heading.textContent = config.title; container.append(heading);
      const note = document.createElement('p'); note.textContent = 'Illustrative offline practice. Your response is submitted separately.'; container.append(note);
      const controls = document.createElement('div'); controls.className = 'treehouse-learning-asset-controls'; container.append(controls);
      const inputs = {}; let step = 0; let board = [...START]; const moveListeners = [];
      listeners.push(()=>moveListeners.splice(0).forEach(remove=>remove()));
      const output = document.createElement('div'); const status = document.createElement('p'); status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite');
      function values() { return Object.fromEntries(Object.entries(inputs).map(([key, input]) => [key, input.value])); }
      function button(label, action, parent = controls) { const node = document.createElement('button'); node.type = 'button'; node.textContent = label; on(node, 'click', action); parent.append(node); return node; }
      function render() {
        moveListeners.splice(0).forEach(remove => remove());
        try {
          const result = evaluateLearningAsset(assetId, {...values(), state: board}, step);
          status.textContent = result.summary; output.replaceChildren();
          const table = document.createElement('table'); const caption = document.createElement('caption'); caption.textContent = 'Current illustrative state'; table.append(caption);
          const head = document.createElement('tr'); result.headers.forEach(label => { const th = document.createElement('th'); th.scope = 'col'; th.textContent = label; head.append(th); }); table.append(head);
          for (const row of result.rows) { const tr = document.createElement('tr'); row.forEach((value, index) => { const cell = document.createElement(index === 0 ? 'th' : 'td'); if (index === 0) cell.scope = 'row'; cell.textContent = String(value); tr.append(cell); }); table.append(tr); } output.append(table);
          if(result.swatches){const samples=document.createElement('div');samples.className='treehouse-learning-asset-controls';result.swatches.forEach((sample,i)=>{const outer=document.createElement('div');outer.className='treehouse-learning-asset-swatch';outer.style.backgroundColor=`rgb(${Math.round(sample.surround*255)} ${Math.round(sample.surround*255)} ${Math.round(sample.surround*255)})`;const patch=document.createElement('span');patch.style.backgroundColor=`rgb(${Math.round(sample.value*255)} ${Math.round(sample.value*255)} ${Math.round(sample.value*255)})`;patch.setAttribute('aria-label',`Patch ${i+1}: stored value ${sample.value}, surround ${sample.surround}`);outer.append(patch);samples.append(outer);});output.append(samples);}
          if (result.moves) { const moves = document.createElement('div'); moves.className = 'treehouse-learning-asset-controls'; result.moves.forEach(move => {const node = document.createElement('button'); node.type = 'button'; node.textContent = move.label; const act = () => {board = [...move.state]; render();}; node.addEventListener('click', act); moveListeners.push(() => node.removeEventListener('click', act)); moves.append(node);}); output.append(moves); }
        } catch (error) { output.replaceChildren(); status.textContent = error.message; }
      }
      for (const field of config.fields) {
        if (!/^[a-z][a-z0-9-]*$/.test(field.key)) throw new Error('Invalid field.');
        const label = document.createElement('label'); label.append(document.createTextNode(field.label + ' '));
        let input;
        if (field.type === 'select') { input = document.createElement('select'); for (const option of field.options) { const child = document.createElement('option'); child.value = option.value; child.textContent = option.label; input.append(child); } }
        else { input = document.createElement('input'); input.type = 'number'; input.min = field.min; input.max = field.max; input.step = field.step || 1; }
        input.value = field.default; inputs[field.key] = input; label.append(input); controls.append(label); on(input, 'change', render);
      }
      if (['cs-state-trace','cs-graph-search','cs-boundary-search','cs-feedback-trace','games-network-replay','ai-tokenization-trace'].includes(assetId)) button('Step', () => {step = Math.min(assetId==='cs-state-trace'?3:20, step + 1); render();});
      button('Reset', () => {step = 0; board = [...START]; for (const field of config.fields) inputs[field.key].value = field.default; render();});
      container.append(status, output); render();
    } catch (error) { if (alive) {listeners.splice(0).forEach(remove=>remove());message.textContent = `${error.message} The written exercise remains usable.`;container.replaceChildren(message);} }
  }
  load(); return {destroy};
}
