import {openSharedFeedback} from './sharedFeedback.js';

/** A read-only region selection over bytes captured by the shared Files owner. */
export async function openSharedImageFeedback({data, name, publicationID, isCurrent}) {
  if (!data || data.version!==1 || !['viewer','commenter','editor'].includes(data.role) ||
      !['image/png','image/jpeg'].includes(data.mime) || !Number.isSafeInteger(data.width) || !Number.isSafeInteger(data.height) ||
      data.width<1 || data.height<1 || data.width>4096 || data.height>4096 || data.width*data.height>16777216 ||
      typeof data.bytes!=='string' || data.bytes.length>5592408 || !/^[a-f0-9]{64}$/.test(data.sha256 || '') ||
      data.revision?.kind!=='hostFingerprint' || data.revision.value!==`sha256:${data.sha256}:${Math.floor(data.bytes.length*3/4)-(data.bytes.endsWith('==')?2:data.bytes.endsWith('=')?1:0)}` ||
      typeof isCurrent!=='function' || !isCurrent()) throw Error('Shared image unavailable.');
  let raw; try { raw=atob(data.bytes); } catch (_) { throw Error('Shared image unavailable.'); }
  const bytes=Uint8Array.from(raw, char=>char.charCodeAt(0));
  const sha=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',bytes)),v=>v.toString(16).padStart(2,'0')).join('');
  if(sha!==data.sha256 || !isCurrent()) throw Error('Shared image changed.');
  const url=URL.createObjectURL(new Blob([bytes],{type:data.mime}));
  const dialog=document.createElement('dialog'), title=document.createElement('h2'), status=document.createElement('p');
  const image=document.createElement('img'), frame=document.createElement('div'), region=document.createElement('div'), feedback=document.createElement('button'), close=document.createElement('button');
  title.textContent=String(name || 'Shared image');status.setAttribute('role','status');status.textContent='Drag on the image to select a region.';
  image.alt='Shared image; drag to select a feedback region';image.src=url;image.style.cssText='display:block;max-width:100%;max-height:60vh;touch-action:none;user-select:none;';
  feedback.type=close.type='button';feedback.textContent='Feedback on selected region';feedback.disabled=true;close.textContent='Close';
  frame.style.cssText='position:relative;display:inline-block;max-width:100%;';region.style.cssText='position:absolute;display:none;box-sizing:border-box;border:2px solid #fff;outline:1px solid #111;background:rgba(100,160,255,.2);pointer-events:none;';frame.append(image,region);
  dialog.append(title,status,frame,feedback,close);document.body.append(dialog);
  let closed=false,start=null,anchor=null,generation=0,child=null;
  const current=()=>!closed && dialog.isConnected && isCurrent()===true;
  const observer=new MutationObserver(()=>{if(!current())done();});
  function done(){if(closed)return;closed=true;generation++;observer.disconnect();for(const target of [window,document]){target.removeEventListener('openclank:auth-context-changed',done);target.removeEventListener('openclank-account-changed',done);target.removeEventListener('openclank-window-closed',check);}child?.close();URL.revokeObjectURL(url);if(dialog.open)dialog.close();dialog.remove();}
  function check(){if(!current())done();}
  function point(event){const box=image.getBoundingClientRect();if(!box.width || !box.height)return null;return {x:Math.max(0,Math.min(1,(event.clientX-box.left)/box.width)),y:Math.max(0,Math.min(1,(event.clientY-box.top)/box.height))};}
  image.addEventListener('load',()=>{if(!current() || image.naturalWidth!==data.width || image.naturalHeight!==data.height){status.textContent='Shared image dimensions changed.';feedback.disabled=true;}});
  image.addEventListener('error',()=>{status.textContent='Shared image unavailable.';feedback.disabled=true;});
  function show(a,b){const x=Math.min(a.x,b.x),y=Math.min(a.y,b.y),w=Math.abs(b.x-a.x),h=Math.abs(b.y-a.y);region.style.display='block';region.style.left=`${x*100}%`;region.style.top=`${y*100}%`;region.style.width=`${w*100}%`;region.style.height=`${h*100}%`;}
  image.addEventListener('pointerdown',event=>{if(!current() || image.naturalWidth!==data.width || image.naturalHeight!==data.height)return;start=point(event);anchor=null;generation++;feedback.disabled=true;region.style.display='none';image.setPointerCapture(event.pointerId);});
  image.addEventListener('pointermove',event=>{if(start && current()){const next=point(event);if(next)show(start,next);}});
  image.addEventListener('pointercancel',()=>{start=null;anchor=null;generation++;feedback.disabled=true;region.style.display='none';});
  image.addEventListener('pointerup',event=>{if(!current() || !start)return;const end=point(event),first=start;start=null;if(!end)return;const x=Math.min(first.x,end.x),y=Math.min(first.y,end.y),width=Math.abs(end.x-first.x),height=Math.abs(end.y-first.y);if(width<=0 || height<=0){region.style.display='none';return;}show(first,end);anchor={kind:'image',x,y,width,height};generation++;feedback.disabled=!['commenter','editor'].includes(data.role);status.textContent='Region selected. Open feedback to save a comment.';});
  feedback.addEventListener('click',async()=>{if(!current() || !anchor)return;const selected=anchor,revision=generation;feedback.disabled=true;try{const opened=await openSharedFeedback({publicationID,expectedRevision:data.revision,anchor:selected,canComment:true,isCurrent:()=>current() && revision===generation && anchor===selected});if(!current() || revision!==generation){opened?.close();return;}child=opened;}catch(_){if(current())status.textContent='Shared feedback unavailable.';}finally{if(current() && revision===generation)feedback.disabled=false;}});
  close.addEventListener('click',done);dialog.addEventListener('close',done,{once:true});
  for(const target of [window,document]){target.addEventListener('openclank:auth-context-changed',done);target.addEventListener('openclank-account-changed',done);target.addEventListener('openclank-window-closed',check);}
  observer.observe(document.body,{childList:true,subtree:true});dialog.showModal();return Object.freeze({close:done});
}
