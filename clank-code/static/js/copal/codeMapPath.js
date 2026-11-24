// Resolve only the full ordered hierarchy returned by one map authority.
// Never derive visual/canonical ids from filenames or select a suffix match.
export async function resolveCodeMapPath(path,{read,snapshot,isCurrent,maxPages=32,maxSegments=64,initialOverview=null}){
  const parts=typeof path==='string'?path.split('/'):[];
  if(!parts.length||parts.length>maxSegments||parts.some(p=>!p||p==='.'||p==='..'||p.includes('\\')))return {state:'invalid'};
  // Native labels are bounded presentation strings, not lossless locators.
  if(parts.some(p=>new TextEncoder().encode(p).length>=160||/[\u0000-\u001f\u007f]/u.test(p)))return {state:'label_limit'};
  let pages=0,authority=null,parent=null;
  const key=value=>JSON.stringify(value);
  // One-use handoff from the workbench's just-completed overview, not a cache.
  // The caller fences request/scope identity; recheck current model completeness
  // and authority here. Any missing proof keeps the original fetch path.
  const complete=value=>value?.status==='ready'&&['published','structure_preview'].includes(value.authority?.variant)&&value.evicted===false&&value.page?.complete===true&&value.page.truncated===false&&value.page.next_cursor===null;
  for(let i=0;i<parts.length;i++){
    if(!isCurrent())return {state:'stale'};
    if(++pages>maxPages)return {state:'limited'};
    let data=i===0&&initialOverview?snapshot():null;
    const reuse=i===0&&initialOverview?.operation==='overview'&&complete(initialOverview)&&complete(data)&&key(initialOverview.authority)===key(data.authority)&&initialOverview.root===data.root;
    if(!reuse){if(!await read(i===0?'overview':'children',i===0?null:{kind:'visual',id:parent},false))return {state:'unavailable'};data=snapshot();}
    const first=key(data.authority);
    if(authority!==null&&first!==authority)return {state:'stale'};
    authority=first;
    parent=data.root??data.nodes?.find(node=>node.kind==='repository')?.id??null;
    const matches=new Map(),cursors=new Set();
    while(true){
      if(!isCurrent()||data.status!=='ready'||key(data.authority)!==authority)return {state:'stale'};
      for(const node of data.nodes||[])if(node.parent===parent&&node.label===parts[i])matches.set(node.id,node);
      const cursor=data.page?.next_cursor;
      if(!cursor)break;
      if(cursors.has(cursor)||++pages>maxPages)return {state:'limited'};
      cursors.add(cursor);
      if(!await read(i===0?'overview':'children',i===0?null:{kind:'visual',id:parent},true))return {state:'unavailable'};
      data=snapshot();
    }
    if(data.page?.truncated||data.page?.complete!==true)return {state:'limited'};
    if(matches.size!==1)return {state:matches.size?'ambiguous':'not_found'};
    const node=[...matches.values()][0];
    if(i===parts.length-1)return {state:'found',node,authority:data.authority,pages};
    if(!['folder','repository'].includes(node.kind))return {state:'not_found'};
    parent=node.id;
  }
  return {state:'not_found'};
}
