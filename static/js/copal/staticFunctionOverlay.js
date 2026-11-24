// A separate, non-indexed view: never feeds the canonical Code Graph model.
let overlayMenuSequence=0;
export function mountStaticFunctionOverlay({container,load,isCurrent,watchRoot,onDispose=()=>{}}) {
  const controller=new AbortController(),targets=[document,globalThis.window].filter(Boolean);
  const authEvents=['openclank:auth-context-changed','openclank-account-changed','openclank-files-policy-changed','openclank-policy-changed','openclank:file-policy-changed','workspace-change','chat-workspace-change'];
  const scopeEvents=['odysseus:session-selected','openclank-window-closed'];
  let disposed=false,observer;const menuID=++overlayMenuSequence;
  const current=()=>!disposed&&container.isConnected&&isCurrent();
  const dispose=()=>{if(disposed)return;disposed=true;controller.abort();observer?.disconnect();for(const target of targets){for(const event of authEvents)target.removeEventListener(event,dispose);for(const event of scopeEvents)target.removeEventListener(event,check);}for(const event of ['click','keydown','openclank:window-placement'])watchRoot?.removeEventListener(event,check);container.replaceChildren();onDispose();};
  const check=()=>{if(!current())dispose();};for(const target of targets){for(const event of authEvents)target.addEventListener(event,dispose);for(const event of scopeEvents)target.addEventListener(event,check);}
  for(const event of ['click','keydown','openclank:window-placement'])watchRoot?.addEventListener(event,check);
  if(watchRoot && typeof MutationObserver==='function'){observer=new MutationObserver(check);observer.observe(watchRoot,{subtree:true,childList:true,attributes:true,attributeFilter:['class','aria-selected','hidden','style']});if(watchRoot.parentNode)observer.observe(watchRoot.parentNode,{childList:true});}
  const title=document.createElement('h3');title.textContent='Static functions · derived, non-indexed';
  const status=document.createElement('p');status.textContent='Loading verified dossier…';status.setAttribute('aria-live','polite');
  const metadata=document.createElement('pre'),details=document.createElement('p'),viewport=document.createElement('div');viewport.className='code-workbench-viewport';viewport.style.overflow='auto';
  container.replaceChildren(title,status,metadata,details,viewport);
  (async()=>{try{
    if(!current())return dispose();
    const value=await load({signal:controller.signal});if(!current())return dispose();
    if(value?.schema!=='openclank.static-function-overlay.v1'||value.state!=='verified'||!['current','historical','unchecked'].includes(value.sourceState)||value.coverage!=='partial_static_analysis'||value.architecture!=='unknown'||!Number.isSafeInteger(value.omitted)||value.omitted<0||!Number.isSafeInteger(value.ambiguousRows)||value.ambiguousRows<0||!Array.isArray(value.functions)||value.functions.length>256||!Array.isArray(value.edges)||value.edges.length!==0||!value.resultIdentity||new TextEncoder().encode(JSON.stringify(value)).length>262144)throw new Error('Static function overlay unavailable.');
    const seen=new Set();for(const row of value.functions){if(!row||Object.keys(row).sort().join(',')!=='address,addressSpace,id,name,size'||!/^overlay:[a-f0-9]{64}$/.test(row.id)||seen.has(row.id)||typeof row.name!=='string'||row.addressSpace!=='provider_virtual'||!Number.isSafeInteger(row.address)||row.address<0||!Number.isSafeInteger(row.size)||row.size<0||!Number.isSafeInteger(row.address+row.size))throw new Error('Static function overlay unavailable.');seen.add(row.id);}
    status.textContent=`Source at read: ${value.sourceState} (not continuously monitored) · partial static evidence · ${value.omitted} omitted · ${value.ambiguousRows} ambiguous rows excluded. No indexed nodes, edges or runtime evidence.`;
    metadata.textContent=JSON.stringify({dossierRevision:value.dossierRevision,...value.resultIdentity,architecture:value.architecture,unsupported:value.unsupported},null,2);
    details.textContent='Select a function to inspect its provider virtual address. Address navigation, disassembly, cross-references and traces are unavailable.';
    const height=Math.max(160,Math.ceil(value.functions.length/4)*80+40),svg=document.createElementNS('http://www.w3.org/2000/svg','svg');svg.setAttribute('viewBox',`0 0 1200 ${height}`);svg.style.height=`${height}px`;svg.setAttribute('role','img');svg.setAttribute('aria-label','Derived static functions; no edges');
    value.functions.forEach((row,index)=>{const group=document.createElementNS(svg.namespaceURI,'g');group.setAttribute('transform',`translate(${30+(index%4)*290} ${40+Math.floor(index/4)*80})`);group.setAttribute('tabindex','0');group.setAttribute('role','button');group.setAttribute('class','code-point');const rect=document.createElementNS(svg.namespaceURI,'rect');rect.setAttribute('width','270');rect.setAttribute('height','54');rect.setAttribute('rx','5');rect.setAttribute('fill','currentColor');rect.setAttribute('opacity','0.18');const label=document.createElementNS(svg.namespaceURI,'text');label.setAttribute('x','8');label.setAttribute('y','30');label.textContent=(row.name||'(unnamed)').slice(0,32);group.append(rect,label);const inspect=()=>{if(!current())return dispose();details.textContent=`${row.name||'(unnamed)'} · provider virtual 0x${row.address.toString(16)} · size ${row.size}. This is partial static interpretation, not a file offset or runtime fact.`;};group.addEventListener('click',inspect);group.addEventListener('keydown',event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();inspect();}});svg.append(group);});
    viewport.replaceChildren(svg);
  }catch(error){if(current())status.textContent='Verified static functions are unavailable or the dossier changed.';}})();
  const menuCapture=()=>({kind:'static_function_overlay',id:menuID});
  const menuScopeCurrent=value=>Boolean(current()&&value&&typeof value==='object'&&!Array.isArray(value)&&Object.keys(value).sort().join(',')==='id,kind'&&value.kind==='static_function_overlay'&&value.id===menuID);
  return {dispose,isCurrent:current,isMounted:()=>current(),setVisible:()=>{},menuCapture,menuScopeCurrent,menuCommands:()=>[]};
}
