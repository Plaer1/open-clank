// Interleave branches and root continuation. Only published maps ask the
// planner to sample declarations and relationships.
export function createCodeMapProgression({anchor, initialDepth, readBudget, fileAtlas, wantSymbols, wantRelationships, canEnter=()=>true}) {
  const lanes = [];
  const seen = new Set();
  const width = Math.max(1, Math.min(6, Math.floor(readBudget / 8)));
  let hasRootPage = false, sinceRootPage = 0, lastLane = null;
  const sampleFiles = wantSymbols || wantRelationships;
  const hierarchyDepth=fileAtlas?64:initialDepth;
  const pending = (id, operation, phase, depth=0) => ({id,operation,phase,depth});

  function discover(parent, depth, nodes, lane=null) {
    const children = nodes.filter(node=>node.parent===parent && ['folder','file'].includes(node.kind));
    children.sort((a,b)=>b.source_files-a.source_files || a.label.localeCompare(b.label) || a.id.localeCompare(b.id));
    for (const node of children) {
      if (seen.has(node.id)) continue;
      seen.add(node.id);
      if (!lane) {
        const branch = {folders:[], files:[], followUps:[], attempts:0, sampled:!sampleFiles};
        if (node.kind==='folder' || sampleFiles) lanes.push(branch);
        add(branch,node,depth);
      } else add(lane,node,depth);
    }
  }

  function add(lane,node,depth) {
    if (node.kind==='folder') lane.folders.push(pending(node.id,'children','folder',depth));
    else if (sampleFiles && node.source_files>0 && node.resource_id && !lane.sampled) {
      lane.files.push(pending(node.id,wantSymbols?'children':'neighbors',wantSymbols?'declarations':'file-neighbors'));
    }
  }

  function next() {
    function pick(lane) {
      for (const queue of [lane.followUps, lane.files]) {
        const task = queue.find(task=>canEnter(task.id));
        if (task) return {...task,lane,queue};
      }
      const task = lane.folders.filter(task=>canEnter(task.id) && (!lane.sampled || task.depth<hierarchyDepth))
        .reduce((best,task)=>{
          if(!best)return task;
          if(lane.sampled&&task.depth!==best.depth)return task.depth<best.depth?task:best;
          if(!lane.sampled&&task.depth!==best.depth)return task.depth>best.depth?task:best;
          return best.more&&!task.more?task:best;
        },null);
      return task && {...task,lane,queue:lane.folders};
    }
    if (hasRootPage && sinceRootPage>=width && canEnter(anchor)) {
      return {...pending(anchor,'overview','root'),more:true};
    }
    const start=lastLane?lanes.indexOf(lastLane)+1:0;
    for(let i=0;i<lanes.length;i++){
      const task=pick(lanes[(start+i)%lanes.length]);
      if(task)return {...task,more:task.more===true};
    }
    return hasRootPage && canEnter(anchor) ? {...pending(anchor,'overview','root'),more:true} : null;
  }

  function accept(task,nodes,page,ok=true) {
    if (task.phase==='root') {
      sinceRootPage = 0;
      hasRootPage = ok && Boolean(page?.next_cursor);
      if (ok) discover(anchor,1,nodes);
      return;
    }
    sinceRootPage++;
    const {lane,queue} = task;
    lastLane=lane;
    const slot = queue.findIndex(job=>job.phase===task.phase && job.id===task.id);
    if (slot!==-1) queue.splice(slot,1);

    if (task.phase==='folder') {
      if (ok) {
        discover(task.id,task.depth+1,nodes,lane);
        if (page?.next_cursor) lane.folders.push({...pending(task.id,'children','folder',task.depth),more:true});
      }
    } else if (task.phase==='declarations') {
      const representative = ok && nodes.find(node=>node.parent===task.id && node.kind==='symbol' && node.resource_id);
      lane.attempts++;
      lane.sampled=Boolean(representative)||lane.attempts>=2;
      if(lane.sampled)lane.files.length=0;
      if (wantRelationships) {
        lane.followUps.push(pending(task.id,'neighbors','file-neighbors'));
        if (representative) lane.followUps.push(pending(representative.id,'neighbors','symbol-neighbors'));
      }
    } else if (task.phase==='file-neighbors'&&!wantSymbols) {
      lane.files.length=0;
      lane.sampled=true;
    }
  }

  return {
    begin:(nodes,page)=>accept({phase:'root'},nodes,page),
    next,
    accept,
  };
}
