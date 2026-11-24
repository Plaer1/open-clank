// Portable, allowlisted projection only. Raw provider objects never enter an artifact.
import { CODE_GRAPH_DISPLAY } from './graphSettings.js';
const KINDS = new Set(['repository','folder','file','symbol']);
import { MAP_RELATIONS } from './codeMapModel.js';
const RELATIONS = new Set(MAP_RELATIONS);
const STATES = new Set(['exact','inferred','unresolved','structure']);
const safeLabel = (value) => {
  const text = Array.from(String(value ?? '').normalize('NFC').replace(/[\uD800-\uDFFF\uFFFE\uFFFF]/gu,'�')).slice(0,256).join('');
  return !text || /[\u0000-\u001f\u007f]/.test(text) || /(?:^|[\\/])\.\.(?:[\\/]|$)/.test(text) || /(?:^|[^a-z0-9_.-])[\\/]|[a-z]:[\\/]|\\\\|[a-z][a-z0-9+.-]*:\/\//i.test(text) ? '[redacted]' : text;
};
const compare = (a,b) => a < b ? -1 : a > b ? 1 : 0;
const count = (value) => Number.isSafeInteger(value) && value >= 0 ? value : null;
const token = value => typeof value === 'string' && /^[a-z0-9][a-z0-9._:-]{0,127}$/i.test(value) ? value : null;
function settledLayout(ids, positions) {
  if (!Array.isArray(positions) || !ids.size || positions.length !== ids.size) return null;
  const found = new Map();
  for (const point of positions) {
    const id = ids.get(String(point?.id));
    if (!id || found.has(id) || ![point.x,point.y].every(value=>Number.isFinite(value)&&Math.abs(value)<=1e7)) return null;
    found.set(id,point);
  }
  // Normalize the layout in export coordinates. Never emit a source identity
  // or browser camera with the positions.
  const xs=[...found.values()].map(p=>p.x),ys=[...found.values()].map(p=>p.y);
  const left=Math.min(...xs),top=Math.min(...ys);
  const scale=Math.min(1,4000/Math.max(1,Math.max(...xs)-left,Math.max(...ys)-top));
  return {basis:'settled',points:[...ids.values()].map(id=>{
    const point=found.get(id);
    return {id,x:Math.round((point.x-left)*scale)+28,y:Math.round((point.y-top)*scale)+44};
  })};
}
export function codeGraphExportSnapshot(scene, metadata = {}) {
  if (!scene || !Array.isArray(scene.nodes) || !Array.isArray(scene.edges) || scene.nodes.length > CODE_GRAPH_DISPLAY.nodes || scene.edges.length > CODE_GRAPH_DISPLAY.edges) throw new Error('Graph projection is unavailable or exceeds export limits.');
  const nodes = [...scene.nodes].sort((a,b)=>compare(String(a.id),String(b.id)));
  if (new Set(nodes.map(n=>String(n.id))).size !== nodes.length) throw new Error('Graph projection contains duplicate identities.');
  const ids = new Map(nodes.map((node,index)=>[String(node.id),`n${index}`]));
  const exportedNodes = nodes.map(node=>({id:ids.get(String(node.id)),kind:KINDS.has(node.kind)?node.kind:'other',label:safeLabel(node.label),parent:ids.get(String(node.parent)) || null}));
  const exportedEdges = scene.edges.filter(edge=>ids.has(String(edge.from)) && ids.has(String(edge.to))).map(edge=>({from:ids.get(String(edge.from)),to:ids.get(String(edge.to)),relation:RELATIONS.has(edge.relation)?edge.relation:'other',state:STATES.has(edge.state)?edge.state:'unknown'})).sort((a,b)=>compare(JSON.stringify(a),JSON.stringify(b)));
  const receipt=scene.authority?.variant === 'published' ? scene.authority.receipt : null;
  const omittedEdges=count(metadata.omittedEdges);
  const layout=settledLayout(ids,metadata.positions);
  return {schema:'openclank.code-graph-export.v1',view:{family:'code',presentation:['map','web','gravity'].includes(metadata.presentation)?metadata.presentation:'map',scope:'displayed',direction:['incoming','outgoing','both'].includes(metadata.direction)?metadata.direction:'both',relations:(metadata.relations||[]).filter(value=>RELATIONS.has(value)).sort(),nodeScope:['mixed','files','symbols'].includes(metadata.nodeScope)?metadata.nodeScope:'mixed',symbolTypes:{functions:metadata.showFunctions === true,classes:metadata.showClasses === true,other:metadata.showOtherSymbols === true},nodeLimit:count(metadata.nodeLimit),edgeLimit:count(metadata.edgeLimit),fileLimit:count(metadata.fileLimit),symbolLimit:count(metadata.symbolLimit)},receipt:{headGeneration:token(receipt?.head_generation),configFingerprint:token(receipt?.config_fingerprint),freshness:['current','stale','partial','unknown'].includes(metadata.freshness)?metadata.freshness:'unknown'},nodes:exportedNodes,edges:exportedEdges,...(layout ? {layout} : {}),omissions:{nodes:count(metadata.omittedNodes),edges:omittedEdges === null ? null : omittedEdges+scene.edges.length-exportedEdges.length,remainingNodes:count(metadata.remainingNodes),remainingEdges:count(metadata.remainingEdges),incomplete:metadata.incomplete !== false,sourceText:true,absolutePaths:true,ownerAndActivity:true}};
}
export function codeGraphExportJSON(snapshot) { return JSON.stringify(snapshot,null,2)+'\n'; }
const escapeHTML = value => String(value).replace(/[&<>"']/g, char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
export function codeGraphExportHTML(snapshot) {
  const nodes=snapshot.nodes.map(node=>`<li><strong>${escapeHTML(node.label)}</strong> <small>${escapeHTML(node.kind)} · ${escapeHTML(node.id)}</small></li>`).join('');
  const edges=snapshot.edges.map(edge=>`<li>${escapeHTML(edge.from)} → ${escapeHTML(edge.to)} · ${escapeHTML(edge.relation)} (${escapeHTML(edge.state)})</li>`).join('');
  return `<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Code graph snapshot</title><style>body{font:16px system-ui;max-width:72rem;margin:2rem auto;padding:0 1rem}small{color:#555}li{overflow-wrap:anywhere}</style><h1>Code graph snapshot</h1><p>Read-only offline snapshot. ${snapshot.nodes.length} nodes, ${snapshot.edges.length} edges; ${snapshot.omissions.nodes ?? "unknown"} loaded nodes and ${snapshot.omissions.edges ?? "unknown"} loaded edges omitted. Coverage ${snapshot.omissions.incomplete?"is incomplete or unknown":"is complete"}.</p><p>Presentation: ${escapeHTML(snapshot.view.presentation)}; freshness: ${escapeHTML(snapshot.receipt.freshness)}; head: ${escapeHTML(snapshot.receipt.headGeneration || "unavailable")}.</p><h2>Nodes</h2><ul>${nodes}</ul><h2>Relationships</h2><ul>${edges}</ul></html>`;
}
export function codeGraphExportGraphML(snapshot) {
  const nodes=snapshot.nodes.map(n=>`<node id="${escapeHTML(n.id)}"><data key="label">${escapeHTML(n.label)}</data><data key="kind">${escapeHTML(n.kind)}</data></node>`).join('');
  const edges=snapshot.edges.map((e,i)=>`<edge id="e${i}" source="${escapeHTML(e.from)}" target="${escapeHTML(e.to)}"><data key="relation">${escapeHTML(e.relation)}</data><data key="state">${escapeHTML(e.state)}</data></edge>`).join('');
  return `<?xml version="1.0" encoding="UTF-8"?><graphml xmlns="http://graphml.graphdrawing.org/xmlns"><key id="label" for="node" attr.name="label" attr.type="string"/><key id="kind" for="node" attr.name="kind" attr.type="string"/><key id="relation" for="edge" attr.name="relation" attr.type="string"/><key id="state" for="edge" attr.name="state" attr.type="string"/><key id="scope" for="graph" attr.name="scope" attr.type="string"/><key id="receipt" for="graph" attr.name="receipt" attr.type="string"/><key id="omissions" for="graph" attr.name="omissions" attr.type="string"/><graph id="snapshot" edgedefault="directed"><data key="scope">${escapeHTML(JSON.stringify(snapshot.view))}</data><data key="receipt">${escapeHTML(JSON.stringify(snapshot.receipt))}</data><data key="omissions">${escapeHTML(JSON.stringify(snapshot.omissions))}</data>${nodes}${edges}</graph></graphml>`;
}
export function codeGraphExportSVG(snapshot) {
  const columns=8, cellWidth=190, cellHeight=65, rows=Math.max(1,Math.ceil(snapshot.nodes.length/columns));
  const actual=snapshot.layout?.basis==='settled';
  const positions=actual ? new Map(snapshot.layout.points.map(p=>[p.id,p]))
    : new Map(snapshot.nodes.map((n,i)=>[n.id,{x:10+(i%columns)*cellWidth,y:32+Math.floor(i/columns)*cellHeight}]));
  const width=actual ? Math.max(150,...snapshot.layout.points.map(p=>p.x+180)) : columns*cellWidth;
  const height=actual ? Math.max(100,...snapshot.layout.points.map(p=>p.y+30)) : rows*cellHeight+40;
  const edges=snapshot.edges.map(e=>{const a=positions.get(e.from),b=positions.get(e.to);const dx=actual?0:85,dy=actual?0:18;return a&&b?`<line x1="${a.x+dx}" y1="${a.y+dy}" x2="${b.x+dx}" y2="${b.y+dy}" stroke="#777"/>`:'';}).join('');
  const nodes=snapshot.nodes.map(n=>{
    const p=positions.get(n.id),label=escapeHTML(Array.from(n.label).slice(0,24).join(''));
    if(actual)return `<g><circle cx="${p.x}" cy="${p.y}" r="5" fill="#333"/><text x="${p.x+9}" y="${p.y+4}" font-size="12">${label}</text></g>`;
    return `<g><rect x="${p.x}" y="${p.y}" width="170" height="36" fill="#fff" stroke="#333"/><text x="${p.x+5}" y="${p.y+23}" font-size="12">${label}</text></g>`;
  }).join('');
  const metadata=escapeHTML(JSON.stringify({view:snapshot.view,receipt:snapshot.receipt,omissions:snapshot.omissions,layout:actual?'settled presentation':'schematic export grid',labels:'shortened to 24 characters'}));
  return `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}"><title>Code graph snapshot, ${actual?'settled layout':'schematic export grid'}</title><desc>${metadata}</desc><rect width="100%" height="100%" fill="#fff"/>${edges}${nodes}</svg>`;
}
export function codeGraphExportCanvas(snapshot) {
  const markdownText = value => String(value).replace(/[\\`*_{}\[\]<>!#|]/g, '\\$&');
  const positions=new Map((snapshot.layout?.points||[]).map(p=>[p.id,p]));
  const nodes=snapshot.nodes.map((n,i)=>{
    const point=positions.get(n.id);
    return {id:n.id,type:'text',text:markdownText(n.label),x:point?.x??(i%8)*240,y:point?.y??Math.floor(i/8)*110,width:210,height:80};
  });
  const edges=snapshot.edges.map((e,i)=>({id:`e${i}`,fromNode:e.from,toNode:e.to,fromSide:'right',toSide:'left',label:`${e.relation} (${e.state})`}));
  return JSON.stringify({nodes,edges,'x-openclank':{schema:snapshot.schema,view:snapshot.view,receipt:snapshot.receipt,omissions:snapshot.omissions,layout:positions.size?'settled presentation':'schematic export grid'}},null,2)+'\n';
}
const cypherString = value => "'"+String(value).replace(/\\/g,'\\\\').replace(/'/g,"\\'").replace(/[\r\n\u0000-\u001f\u007f]/g,' ')+"'";
export function codeGraphExportCypher(snapshot) {
  const metadata=JSON.stringify({view:snapshot.view,receipt:snapshot.receipt,omissions:snapshot.omissions});
  const clauses=snapshot.nodes.map(n=>`(${n.id}:OpenClankNode {label: ${cypherString(n.label)}, kind: ${cypherString(n.kind)}})`);
  clauses.push(...snapshot.edges.map(e=>`(${e.from})-[:OPENCLANK_LINK {relation: ${cypherString(e.relation)}, state: ${cypherString(e.state)}}]->(${e.to})`));
  return `// OpenClank portable graph data; review before executing.\n// ${metadata}\n${clauses.length?'CREATE '+clauses.join(',\n')+';':''}\n`;
}
export function codeGraphExportMarkdown(snapshot) {
  const text = value => String(value).replace(/[\\`*_{}\[\]<>!#|]/g,'\\$&');
  const nodes=snapshot.nodes.map(n=>`- ${text(n.id)}: ${text(n.label)} (${text(n.kind)})`).join('\n');
  const edges=snapshot.edges.map(e=>`- ${text(e.from)} → ${text(e.to)}: ${text(e.relation)} (${text(e.state)})`).join('\n');
  return `# Code graph snapshot\n\nGenerated projection for review; it is not an endorsement of the source. Scope: displayed ${text(snapshot.view.presentation)}. Freshness: ${text(snapshot.receipt.freshness)}. Head: ${text(snapshot.receipt.headGeneration || 'unavailable')}. Coverage: ${snapshot.omissions.incomplete?'incomplete or unknown':'complete'}.\n\nScope, receipt and omissions: ${text(JSON.stringify({view:snapshot.view,receipt:snapshot.receipt,omissions:snapshot.omissions}))}\n\n## Nodes\n\n${nodes}\n\n## Relationships\n\n${edges}\n`;
}
