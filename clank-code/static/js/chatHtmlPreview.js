/** Explicit, current-scope HTML preview of one independently qualified History action. */
export function createCapturedHtmlPreview(raw) {
  const metadata=raw?.rawOutput?.metadata, locator=metadata?.history?.managed_action_ref;
  if (raw?.status!=='completed' || metadata?.file?.media_type!=='text/html' || typeof locator!=='string' || !locator.startsWith('oc-history-action:v1:') || locator.length>4096) return null;
  const scope=window.chatModule?.ratingAccountScope?.(), sessions=window.sessionModule, navigation=sessions?.getSessionNavigationToken?.();
  const root=document.createElement('div'); root.className='copal-inline-preview';
  const open=document.createElement('button');open.type='button';open.className='copal-btn';open.textContent='Preview saved HTML';
  const editor=document.createElement('button');editor.type='button';editor.className='copal-btn';editor.textContent='Open in Editor';editor.hidden=true;
  const exportButton=document.createElement('button');exportButton.type='button';exportButton.className='copal-btn';exportButton.textContent='Export source bundle (partial static)';exportButton.title='Only declared static resources are included; runtime dependencies may be missing.';exportButton.hidden=true;
  const widget=document.createElement('button');widget.type='button';widget.className='copal-btn';widget.textContent='Widget state…';widget.hidden=true;
  const close=document.createElement('button');close.type='button';close.className='copal-btn';close.textContent='Close preview';close.hidden=true;
  const status=document.createElement('span');status.setAttribute('role','status');
  const slot=document.createElement('div');slot.className='copal-inline-preview-viewport';slot.style.display='block';slot.style.width='100%';slot.style.height='320px';
  root.append(open,editor,exportButton,widget,close,status,slot);
  let mount=null, target=null, observer=null, timer=null, guard=null, busy=false;
  const current=()=>{const now=window.chatModule?.ratingAccountScope?.();return root.isConnected && !!scope?.sid && !!scope?.owner && navigation!==undefined && now?.sid===scope.sid && now.owner===scope.owner && now.authEpoch===scope.authEpoch && document.body.dataset.accountId===scope.owner && sessions?.getCurrentSessionId?.()===scope.sid && sessions?.getSessionNavigationToken?.()===navigation;};
  function retire(message='') {
    guard?.abort();guard?.dispose();guard=null;observer?.disconnect();observer=null;if(timer)clearInterval(timer);timer=null;
    const previous=mount;mount=null;target=null;if(previous){previous.viewportControl.remove();void previous.dispose();}slot.replaceChildren();
    editor.hidden=true;exportButton.hidden=true;widget.hidden=true;close.hidden=true;open.hidden=false;open.disabled=false;busy=false;if(message && root.isConnected)status.textContent=message;
  }
  function monitor() {
    if(observer)return;
    observer=new MutationObserver(()=>{if(!current())retire('Preview closed because the chat or account changed.');});observer.observe(document.body,{childList:true,subtree:true});
    timer=setInterval(()=>{if(!current())retire('Preview closed because the chat or account changed.');},1000);
  }
  open.addEventListener('click',async event=>{
    event.stopPropagation();if(busy || !current())return;busy=true;open.disabled=true;monitor();status.textContent='Checking saved HTML…';
    try {
      const response=await fetch('/api/history/captured-html-resource',{method:'POST',credentials:'same-origin',cache:'no-store',headers:{'Content-Type':'application/json'},body:JSON.stringify({locator,chat:scope.sid})});
      if(!response.ok || !current())throw Error('Saved HTML is unavailable.');
      const result=await response.json();
      if(!current() || result?.version!==1 || typeof result.resourceRef!=='string' || !result.resourceRef || typeof result.resourceID!=='string' || !result.resourceID || result?.revision?.kind!=='hostFingerprint' || !/^sha256:[a-f0-9]{64}:(0|[1-9][0-9]*)$/.test(result.revision.value || '') || typeof result.workspaceScope!=='string' || !result.workspaceScope)throw Error('Saved HTML is unavailable.');
      target=Object.freeze({resourceRef:result.resourceRef,resourceID:result.resourceID,revision:Object.freeze({...result.revision}),workspaceScope:result.workspaceScope,accountScope:scope.owner,name:String(result.title || 'HTML preview'),isCurrent:current});
      const [{createPreviewFrameMount},{acquireInlineDependencies},{createDependencyAcquisitionGuard}]=await Promise.all([import('./browser/previewMount.js'),import('./browser/inlineDependencies.js'),import('./browser/dependencyManifest.js')]);
      if(!current())return;guard=createDependencyAcquisitionGuard({isCurrent:current});let acquired;
      try {acquired=await acquireInlineDependencies({target,signal:guard.signal,isCurrent:current});}
      catch(error) {if(current() && guard.timedOut)acquired={dependencies:[],warnings:['Dependency acquisition timed out; only self-contained content may work.']};else throw error;}
      finally {guard?.dispose();guard=null;}
      if(!current())return;
      mount=createPreviewFrameMount({owner:root,getViewport:()=>slot,isCurrent:current,frameClass:'copal-inline-preview-frame',
        onStatus:message=>{if(current())status.textContent=message;},
        onRetired:()=>{if(current())retire('Preview moved to another view.');return true;}});
      root.insertBefore(mount.viewportControl,slot);
      const opened=await mount.open({resourceRef:target.resourceRef,revision:target.revision,title:target.name,accountScope:target.accountScope,workspaceScope:target.workspaceScope,dependencies:acquired.dependencies,dependencyWarnings:acquired.warnings,isCurrent:current},{handoff:false});
      if(!opened || !current())throw Error('Preview unavailable or another preview is active.');
      editor.hidden=false;exportButton.hidden=false;widget.hidden=!mount.session?.widget_state;close.hidden=false;open.hidden=true;status.textContent=acquired.warnings.join(' ') || 'HTML preview opened.';
    } catch(error) {retire(error?.message || 'Saved HTML is unavailable.');}
    finally {busy=false;if(root.isConnected)open.disabled=false;}
  });
  editor.addEventListener('click',async event=>{event.stopPropagation();if(!target || !current())return;try{const editorModule=await import('./copal.js');if(current())await editorModule.openResource(target.resourceRef,{expectedRevision:target.revision,isCurrent:current});}catch{if(current())status.textContent='Editor source is unavailable or changed.';}});
  exportButton.addEventListener('click',async event=>{event.stopPropagation();const captured=mount;if(!captured || !current())return;exportButton.disabled=true;try{await captured.downloadSourceBundle();if(current() && mount===captured)status.textContent='Source bundle downloaded; static dependency coverage is partial.';}catch{if(current())status.textContent='Source bundle is unavailable or changed.';}finally{if(root.isConnected)exportButton.disabled=false;}});
  widget.addEventListener('click',async event=>{event.stopPropagation();const captured=mount;if(!captured || !target || !current())return;try{const {openOwnedWidgetState}=await import('./browser/widgetStateOwner.js');if(current() && mount===captured)openOwnedWidgetState({mount:captured,model:target,isCurrent:()=>current() && mount===captured});}catch{if(current())status.textContent='Widget state is unavailable.';}});
  close.addEventListener('click',event=>{event.stopPropagation();retire('Preview closed.');});
  return root;
}
