// Sanitized mapPages[0].result from real-corpus-ui-01/result.json, SHA256
// 73b262ec9fcc4af0b34158a2b8f93d8d970c101887bc6a3a401a0383f2a53a11.
// Preserve exact labels, kinds, parent topology and containment state. Replace
// opaque native IDs/authority with test-local IDs retaining their lexical rank
// (the layout tie-breaker); no native authority claim.
const folders=['companion','config','contracts','core','docker','docs','execution','harness','integrations','licenses','mcp_servers','native','packages','routes','scripts','services','specs','src','static','swift','tests'];
const files=['.dockerignore','.env.example','.gitattributes','.gitignore','.gitleaks.toml','.gitleaksignore','ACKNOWLEDGMENTS.md','AGENTS.md','CONTRIBUTING.md','Dockerfile','LICENSE','MEATBAGS.md','Odysseus.spec','README.md','ROADMAP.md','SECURITY.md','THREAT_MODEL.md','app.py','build-macos-app.sh','build-windows-portable.ps1','bun.lock','docker-compose.gpu-amd.yml','docker-compose.gpu-nvidia.yml','docker-compose.yml','install-service.sh','launch-windows.ps1','launcher.py','odysseus-ui.service','open-clank-policy-worker.service','open-clank.service','open.sh','openclank_entry.py','package-lock.json','package.json','pyproject.toml','requirements-grpc-codegen.txt','requirements-optional.txt','requirements.txt','setup.py','start-macos.sh','update_windows.bat','uv.lock'];
const nativeIdRanks=[0,1,19,31,36,37,48,51,55,57,2,3,4,5,7,8,9,10,14,15,20,22,23,24,32,52,56,58,61,62,63,6,11,12,13,16,17,18,21,25,26,27,28,29,30,33,34,35,38,39,40,41,42,43,44,45,46,47,49,50,53,54,59,60];
export function coldStructure(){
 const children=[...folders.map(label=>({label,kind:'folder'})),...files.map(label=>({label,kind:'file'}))].sort((a,b)=>a.label<b.label?-1:a.label>b.label?1:0);
 const id=i=>`cold-${String(nativeIdRanks[i]).padStart(3,'0')}`;
 const nodes=[{id:id(0),label:'Repository',kind:'repository',parent:null},...children.map((n,i)=>({...n,id:id(i+1),parent:id(0)}))];
 return {nodes,edges:nodes.slice(1).map(n=>({from:id(0),to:n.id,relation:'containment',state:'structure'}))};
}
