// Bounded navigation over an already-authorized loaded document projection.
export function documentNeighborhood(projection, selected, { direction='both', kinds=['link','embed'], depth=1, maxNodes=200, maxEdges=400 } = {}) {
  if (!['incoming','outgoing','both'].includes(direction) || ![1,2].includes(depth)) throw new TypeError('Invalid neighborhood');
  const allowed = new Set(kinds.filter(kind => kind === 'link' || kind === 'embed'));
  const nodeMap = new Map((projection?.nodes || []).map(node => [node.id,node]));
  if (!nodeMap.has(selected)) return { nodes:[],edges:[],notices:[],coverage:{scope:'loaded',complete:false,omittedNodes:0,omittedEdges:0} };
  const all = (projection.edges || []).filter(edge => allowed.has(edge.type));
  const reached = new Set([selected]), selectedEdges = new Map(); let frontier = new Set([selected]);
  for (let level=0;level<depth;level++) {
    const next = new Set();
    for (const edge of all) {
      const outgoing = direction !== 'incoming' && frontier.has(edge.from);
      const incoming = direction !== 'outgoing' && frontier.has(edge.to);
      if (!outgoing && !incoming) continue;
      const other = outgoing ? edge.to : edge.from;
      if (!nodeMap.has(other)) continue;
      selectedEdges.set(edge.id, edge);
      if (!reached.has(other)) { reached.add(other); next.add(other); }
    }
    frontier = next;
    if (!frontier.size) break;
  }
  const nodeLimit = Math.max(1,Math.min(200,Number(maxNodes)||200));
  const edgeLimit = Math.max(0,Math.min(400,Number.isFinite(maxEdges)?maxEdges:400));
  const ids = [...reached], kept = new Set(ids.slice(0,nodeLimit));
  const eligibleEdges = [...selectedEdges.values()].filter(edge => kept.has(edge.from) && kept.has(edge.to));
  const edges = eligibleEdges.slice(0,edgeLimit);
  return { nodes:[...kept].map(id => nodeMap.get(id)), edges,
    notices:(projection.notices || []).filter(item => kept.has(item.sourceNodeId)),
    coverage:{scope:'loaded',complete:false,omittedNodes:ids.length-kept.size,
      omittedEdges:selectedEdges.size-edges.length,filteredEdges:(projection.edges || []).length-all.length} };
}

// Each direction owns its cursor and only requests data while its panel is live.
export function relationPanel({h,direction,read,open,current}) {
  const list=h('div'), status=h('p',{role:'status'}), button=h('button',{type:'button',text:'Load connections'});
  const panel=h('details',{class:'copal-wiki-backlinks'},h('summary',{text:direction==='incoming'?'Backlinks':'Outgoing links'}),status,list,button);
  const live=()=>panel.isConnected && current();
  let cursor=null, count=0, started=false, loading=false;
  async function load() {
    if (!live() || loading) return;
    started=loading=true;
    button.disabled=true;
    try {
      const page=await read({direction,...(cursor?{cursor}:{})});
      if (!live()) return;
      for (const row of page.links) {
        const endpoint=direction==='incoming'?row.source:row.target;
        const run=async structure=>{
          try {await open(row,direction,structure,live);}
          catch (error) {if(live()) status.textContent=error.message;}
        };
        list.append(h('div',{},h('button',{type:'button',text:endpoint.name+(row.fragment?' · #'+row.fragment:''),onclick:()=>run(false)}),
          h('button',{type:'button',text:'Structure',onclick:()=>run(true)})));
      }
      count+=page.links.length;
      const unresolved=Object.entries(page.unresolved).map(([reason,n])=>n+' '+reason.replaceAll('_',' '));
      status.textContent=count+' shown of '+page.available+' available'+(unresolved.length?' · '+unresolved.join(', '):'');
      cursor=page.cursor;
      button.hidden=!cursor;
      button.textContent='More connections';
    } catch(error) {
      if(live()) {list.replaceChildren(); count=0; cursor=null; button.hidden=false; button.textContent='Reload connections'; status.textContent=error.message;}
    } finally {button.disabled=false; loading=false;}
  }
  button.onclick=load;
  panel.addEventListener('toggle',()=>{if(panel.open && !started) load();});
  return panel;
}
