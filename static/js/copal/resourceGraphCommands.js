// Shared command semantics, not a registry. Surface owners retain immutable
// captures and evaluate these helpers through their existing descriptors.
export const CODE_GRAPH_LABEL='Open in Code Graph';
const integer=n=>Number.isSafeInteger(n)&&n>=0;
const denied=code=>Object.assign(new Error({stale:'The captured resource or access changed. Reopen its menu.',unsupported:'Code Graph requires one authorized Host file or folder.',parent:'The authorized containing folder is unavailable.',cancelled:'Code Graph navigation cancelled.',unavailable:'Code Graph navigation is unavailable. Refresh and try again.'}[code]||'Code Graph navigation is unavailable.'),{code});
export function graphResource(value){
  if(!value||typeof value!=='object'||Array.isArray(value))return null;
  const id=value.resource_id||value.resourceId||value.id,ref=value.resource_ref||value.resourceRef||value.ref;
  const capabilities=Array.isArray(value.capabilities)?value.capabilities:Object.keys(value.capabilities||{}).filter(key=>value.capabilities[key]===true);
  const folder=['folder','directory','recursive_directory'].includes(value.kind);
  if(value.provider!=='host'||!['file','folder','directory','recursive_directory'].includes(value.kind)||typeof id!=='string'||!id||typeof ref!=='string'||!ref)return null;
  return {id,ref,provider:'host',kind:folder?'folder':'file',name:typeof value.name==='string'?value.name:'',capabilities};
}
export function graphDisabledReason(entries){
  if(!Array.isArray(entries)||entries.length!==1)return 'Select exactly one Host file or folder.';
  const resource=graphResource(entries[0]);
  if(!resource)return 'Code Graph is available only for Host files and folders.';
  if(!resource.capabilities.includes(resource.kind==='folder'?'children':'open'))return 'This resource does not allow Code Graph navigation.';
  return '';
}
export async function executeCodeGraphNavigation({resource:input,knownWorkspace=null,generation,dirty=false,isCurrent,acceptGeneration,client,confirmParent,handoff,signal=null}){
  const resource=graphResource(input);
  try{
    if(!resource||graphDisabledReason([input]))throw denied('unsupported');
    if(!integer(generation)||typeof isCurrent!=='function'||typeof acceptGeneration!=='function'||typeof handoff!=='function')throw denied('stale');
    const current=g=>!signal?.aborted&&isCurrent(g)===true;
    const check=g=>{if(!current(g))throw denied('stale');};check(generation);
    if(knownWorkspace&&(!knownWorkspace.id||!integer(knownWorkspace.revision)||knownWorkspace.revision<1))throw denied('stale');
    if(resource.kind==='file'&&!knownWorkspace){
      const revealed=await client.reveal(resource.ref,{signal});check(generation);
      const target=graphResource(revealed?.resource),parent=graphResource(revealed?.parent);
      if(revealed?.resource?.policy_generation!==generation||revealed?.parent?.policy_generation!==generation||target?.id!==resource.id||parent?.kind!=='folder'||!parent.capabilities.includes('children')||!parent.name)throw denied('parent');
      // Only the authority-issued parent label appears; no dirname/path hunt.
      if(!await confirmParent?.({name:parent.name,dirty:dirty===true}))return {ok:false,code:'cancelled',message:'Code Graph navigation cancelled.'};
      check(generation);
    }
    const binding=await client.codeGraphTarget(resource.ref,{...(knownWorkspace?{workspaceId:knownWorkspace.id,expectedWorkspaceRevision:knownWorkspace.revision}:{}),signal});
    const returned=binding?.generation;
    if(!integer(returned)||returned<generation||!(current(generation)||current(returned)))throw denied('stale');
    if(!binding.workspace?.id||binding.workspace.archived||typeof binding.open_relative!=='string')throw denied('stale');
    if(resource.kind==='file'&&!binding.open_relative||resource.kind==='folder'&&!knownWorkspace&&binding.open_relative)throw denied('stale');
    if(knownWorkspace&&(binding.workspace.id!==knownWorkspace.id||binding.workspace.revision!==knownWorkspace.revision))throw denied('stale');
    const issued=await client.workspaceResource(binding.workspace.id,binding.open_relative,{signal});
    const exact=graphResource(issued?.resource);
    if(issued?.generation!==returned||issued?.workspace?.id!==binding.workspace.id||issued?.workspace?.revision!==binding.workspace.revision||exact?.id!==resource.id||exact.kind!==resource.kind||!(current(generation)||current(returned)))throw denied('stale');
    if(await acceptGeneration({before:generation,after:returned,resource:exact,binding})!==true)throw denied('stale');
    check(returned);
    const handoffCurrent=()=>current(returned);
    const opened=await handoff(binding,{isCurrent:handoffCurrent,dirty:dirty===true,resource:exact});
    check(returned);
    if(opened===false)throw denied('unavailable');
    return {ok:true,generation:returned,message:dirty?'Code Graph uses saved/on-disk source. Your unsaved buffer was not saved or indexed.':'Opened saved/on-disk source in Code Graph.'};
  }catch(error){
    const code=['stale','unsupported','parent','cancelled','unavailable'].includes(error?.code)?error.code:signal?.aborted?'cancelled':'unavailable';
    return {ok:false,code,message:denied(code).message};
  }
}
