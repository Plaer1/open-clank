import {createGraphicsConsumer} from './consumer.js';
import {graphicalPresentationPolicy} from './presentationPolicy.js';
import {graphicalSurfaceBudget, GRAPHICAL_SURFACE_LIMITS as L} from './surfaceBudget.js';
import {loadedStillImage,forgetLoadedImage} from './mediaImageAuthority.js';
import {mediaImageGeometry,suppressBitmap,restoreBitmap} from './mediaImageGeometry.js';
import {mediaImageExposed} from './mediaImageExposure.js';

let sequence=0;
export function createImageRegion({root,selector='img',isCurrent=()=>true,policy=graphicalPresentationPolicy(),budget=graphicalSurfaceBudget(),consumerFactory=createGraphicsConsumer,win=globalThis.window,doc=globalThis.document,onStatus=()=>{}}={}) {
  if(!root)throw new TypeError('Image region requires a root');
  const id=`graphical-media-${++sequence}`;
  let disposed=false,consumer=null,lease=null,items=[],bytes=0,failed=false,submitted=false;
  const hidden=new Map(),lifetime=new AbortController();
  const oldPosition=root.style.position;let ownedPosition=false;
  const restore=()=>{if(consumer?.canvas)consumer.canvas.style.visibility='hidden';for(const [image,position]of hidden)restoreBitmap(image,position);hidden.clear();};
  const status=(backend,reason=null)=>{root.dataset.graphicalBackend=backend;root.dataset.graphicalFallback=reason||'';onStatus({kind:'image-region',backend,hardware:'unknown',reason,images:items.length,textureBytes:bytes});};
  const drop=(reason=null)=>{restore();consumer?.dispose();consumer=null;lease?.release();lease=null;bytes=0;items=[];if(ownedPosition){root.style.position=oldPosition;ownedPosition=false;}status('ordinary',reason);};
  const reject=reason=>{failed=true;drop(reason);};
  const collect=()=>{
    const r=root.getBoundingClientRect(),style=win.getComputedStyle(root);
    if(!isCurrent()||!root.isConnected||root.hidden||style.display==='none'||style.visibility==='hidden'||r.width<=0||r.height<=0||r.bottom<=0||r.right<=0||r.top>=win.innerHeight||r.left>=win.innerWidth)return [];
    const output=[],exposureWork={remaining:4096};
    for(const image of root.querySelectorAll(selector)){
      const metadata=loadedStillImage(image),ir=image.getBoundingClientRect(),s=win.getComputedStyle(image);
      if(!metadata||!image.complete||!image.naturalWidth||!image.naturalHeight||s.display==='none'||s.visibility==='hidden'||(!hidden.has(image)&&Number(s.opacity)===0))continue;
      const pixels=image.naturalWidth*image.naturalHeight;
      if(image.naturalWidth>1024||image.naturalHeight>1024||pixels>L.imagePixels||ir.width<=0||ir.height<=0||ir.right<=Math.max(0,r.left)||ir.left>=Math.min(win.innerWidth,r.right)||ir.bottom<=Math.max(0,r.top)||ir.top>=Math.min(win.innerHeight,r.bottom))continue;
      let clipped=false;
      for(let ancestor=image.parentElement;ancestor&&ancestor!==root;ancestor=ancestor.parentElement){
        const a=win.getComputedStyle(ancestor);
        if(/hidden|auto|scroll|clip/.test(`${a.overflow} ${a.overflowX} ${a.overflowY}`)){
          const ar=ancestor.getBoundingClientRect();
          if(ir.left<ar.left||ir.right>ar.right||ir.top<ar.top||ir.bottom>ar.bottom){clipped=true;break;}
        }
      }
      if(clipped)continue; // Partially clipped imagery keeps native clipping.
      if(!mediaImageExposed({image,root,doc,win,work:exposureWork}))continue;
      const sx=r.width/Math.max(1,root.clientWidth),sy=r.height/Math.max(1,root.clientHeight);
      const shape=mediaImageGeometry({x:(ir.left-r.left)/sx-(root.clientLeft||0),y:(ir.top-r.top)/sy-(root.clientTop||0),width:ir.width/sx,height:ir.height/sy,naturalWidth:image.naturalWidth,naturalHeight:image.naturalHeight,style:s});
      if(!shape)continue;
      output.push({image,key:metadata.key,source:metadata.source,...shape,alpha:1,bytes:pixels*4});
      if(output.length>=L.images)break;
    }
    return output;
  };
  function refresh(){
    if(disposed)return;
    restore();
    if(policy.mode==='ordinary'||!isCurrent()){items=[];drop(policy.mode==='ordinary'?'ordinary-policy':'authority-changed');return;}
    if(failed)return;
    const next=collect();
    if(!next.length){items=[];drop('no-eligible-visible-still-image');return;}
    const r=root.getBoundingClientRect(),dpr=Math.max(1,Math.min(L.dpr,(win.devicePixelRatio||1)*r.width/Math.max(1,root.clientWidth)));
    if(Math.ceil(root.clientWidth*dpr)*Math.ceil(root.clientHeight*dpr)>L.canvasPixels){drop('backing-pixel-limit');return;}
    const nextBytes=next.reduce((sum,i)=>sum+i.bytes,0);
    if(!lease)lease=budget.acquire(id);
    if(!lease||!lease.reserve(nextBytes)){drop('resource-limit');return;}
    const changed=items.length!==next.length||items.some((item,i)=>item.image!==next[i].image||item.source!==next[i].source||item.key!==next[i].key);
    items=next;bytes=nextBytes;
    if(!consumer){
      if(win.getComputedStyle(root).position==='static'){root.style.position='relative';ownedPosition=true;}
      try{consumer=consumerFactory({mount:root,id,renderMode:'demand',directImages:true,win,doc,
        onFrameError:()=>{restore();failed=true;queueMicrotask(()=>{if(!disposed)drop('image-frame-failed');});},
        onFrameComplete:({backend})=>{
          if(backend!=='webgl2'||!submitted||failed||disposed||!isCurrent()||policy.mode==='ordinary')return;
          for(const item of items){if(!hidden.has(item.image))hidden.set(item.image,suppressBitmap(item.image));}
          if(consumer?.canvas)consumer.canvas.style.visibility='visible';
          submitted=false;status('webgl2');
        },
        onCanvasChange:()=>{restore();failed=true;queueMicrotask(()=>{if(!disposed)drop('context-lost');});},
        draw:({backend})=>{
          submitted=false;
          if(disposed||failed||!isCurrent()||policy.mode==='ordinary'){restore();return;}
          if(consumer?.backendKind!=='webgl2'){restore();queueMicrotask(()=>{if(!disposed)reject('webgl-unavailable');});return;}
          try{
            for(const item of items){
              if(!item.image.isConnected||item.image.src!==item.source||!loadedStillImage(item.image)||backend.drawImage(item.image,item.x,item.y,item.width,item.height,item.alpha,{uv:item.uv,clip:item.clip})!==true){restore();queueMicrotask(()=>{if(!disposed)reject('image-upload-failed');});return;}
            }
            // Native pixels remain until consumer flush/endFrame succeeds.
            submitted=true;
          }catch(_){restore();queueMicrotask(()=>{if(!disposed)reject('image-draw-failed');});}
        }});
        if(consumer.canvas){consumer.canvas.style.pointerEvents='none';consumer.canvas.style.visibility='hidden';}
      }catch(_){reject('graphics-init-failed');return;}
    }else if(changed)consumer.backend?.clearImageSources?.();
    if(consumer.canvas){consumer.canvas.style.left=`${root.scrollLeft}px`;consumer.canvas.style.top=`${root.scrollTop}px`;}
    consumer.resize(root.clientWidth,root.clientHeight,dpr);consumer.invalidate();
  }
  let visibilityRefreshPending=false;
  const visibilityChanged=()=>{
    if(disposed)return;
    // Never retain accelerated pixels while visibility/stacking changes.
    restore();
    if(visibilityRefreshPending)return;
    visibilityRefreshPending=true;
    // A capture-phase animation/lifecycle event can precede the owner's DOM
    // mutation. Recheck after dispatch; this is one invalidation, not polling.
    queueMicrotask(()=>{visibilityRefreshPending=false;if(!disposed)refresh();});
  };
  const resize=new win.ResizeObserver(refresh);resize.observe(root);
  const mutation=new win.MutationObserver(refresh);mutation.observe(root,{childList:true,subtree:true,attributes:true,attributeFilter:['src','hidden']});
  root.addEventListener('load',refresh,{capture:true,signal:lifetime.signal});
  root.addEventListener('openclank:still-image-ready',refresh,{signal:lifetime.signal});
  root.addEventListener('error',()=>{restore();refresh();},{capture:true,signal:lifetime.signal});
  win.addEventListener('scroll',refresh,{capture:true,signal:lifetime.signal});
  win.addEventListener('resize',refresh,{signal:lifetime.signal});
  win.addEventListener('animationend',visibilityChanged,{capture:true,signal:lifetime.signal});
  for(const name of ['odysseus:modal-minimized','odysseus:modal-opened','odysseus:modal-closed','openclank:presentation-visibility-changed'])win.addEventListener(name,visibilityChanged,{signal:lifetime.signal});
  const unsubscribe=policy.subscribe(()=>{failed=false;refresh();});
  const revoke=()=>{for(const image of root.querySelectorAll(selector))forgetLoadedImage(image);failed=true;drop('authority-changed');};
  doc?.addEventListener?.('openclank:file-policy-changed',revoke,{signal:lifetime.signal});
  doc?.addEventListener?.('openclank:auth-user-ready',revoke,{signal:lifetime.signal});
  refresh();
  return {refresh,snapshot:()=>({disposed,backend:consumer?.backendKind||'ordinary',images:items.length,textureBytes:bytes,failed}),dispose(){if(disposed)return;disposed=true;restore();drop();items=[];resize.disconnect();mutation.disconnect();lifetime.abort();unsubscribe();root.style.position=oldPosition;delete root.dataset.graphicalBackend;delete root.dataset.graphicalFallback;}};
}

export function mountImageRegion(options) {
  if(typeof globalThis.window?.ResizeObserver!=='function'||typeof globalThis.window?.MutationObserver!=='function')return {refresh(){},dispose(){}};
  return createImageRegion(options);
}
