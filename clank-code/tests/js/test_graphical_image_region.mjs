import test from 'node:test';
import assert from 'node:assert/strict';
import {createImageRegion} from '../../static/js/graphics/imageRegion.js';
import {markLoadedStillImage,loadedStillImage,classifyStillBlob,forgetLoadedImage} from '../../static/js/graphics/mediaImageAuthority.js';
import {createSurfaceBudget} from '../../static/js/graphics/surfaceBudget.js';
import {createPresentationPolicy} from '../../static/js/graphics/presentationPolicy.js';
import {BITMAP_OUTSIDE_POSITION} from '../../static/js/graphics/mediaImageGeometry.js';
function fixture({ok=true,imageStyle={}}={}){
 const values=new Map(),priorities=new Map(),style={opacity:'',getPropertyValue:p=>values.get(p)||'',getPropertyPriority:p=>priorities.get(p)||'',setProperty(p,v,priority=''){values.set(p,v);priorities.set(p,priority);},removeProperty(p){values.delete(p);priorities.delete(p);}};
 const image={src:'blob:authorized',complete:true,naturalWidth:100,naturalHeight:80,isConnected:true,style,getBoundingClientRect:()=>({left:5,top:5,right:105,bottom:85,width:100,height:80})};
 markLoadedStillImage(image,{source:image.src,still:true,key:'owner:ref:revision'});
 const root={dataset:{},style:{position:''},isConnected:true,hidden:false,scrollLeft:0,scrollTop:0,clientWidth:200,clientHeight:150,children:[image],querySelectorAll:()=>[image],getBoundingClientRect:()=>({left:0,top:0,right:200,bottom:150,width:200,height:150}),addEventListener(){}};image.parentElement=root;
 const listeners={},draws=[];let draw,frameComplete,frameError,canvasChange,disposed=0,created=0,occluded=false,invalidations=0,collections=0,current=true;
 class Observer{observe(){}disconnect(){this.closed=true;}}
 const win={innerWidth:1000,innerHeight:800,devicePixelRatio:1,ResizeObserver:Observer,MutationObserver:Observer,addEventListener(k,f){listeners[k]=f;},getComputedStyle(el){return {position:'relative',display:'block',visibility:'visible',opacity:el.style.opacity||'1',transform:'none',objectFit:'fill',...(el===image?imageStyle:{}),...(el===image&&style.getPropertyValue('object-position')?{objectPosition:style.getPropertyValue('object-position')}:{})};}};
 const policy=createPresentationPolicy(),budget=createSurfaceBudget();
 const consumerFactory=options=>{created++;draw=options.draw;frameComplete=options.onFrameComplete;frameError=options.onFrameError;canvasChange=options.onCanvasChange;return {canvas:{style:{}},backendKind:'webgl2',backend:{clearImageSources(){}},resize(){},invalidate(){invalidations++;},dispose(){disposed++;}};};
 const query=root.querySelectorAll;root.querySelectorAll=()=>{collections++;return query();};
 const region=createImageRegion({root,win,doc:{elementFromPoint:()=>occluded?{}:image,addEventListener(k,f){listeners['doc:'+k]=f;}},isCurrent:()=>current,policy,budget,consumerFactory});
 const submit=()=>draw({backend:{drawImage:(...args)=>{draws.push(args);return ok;}}});
 return {region,root,image,policy,budget,draws,submit,emit:name=>listeners[name]?.(),occlude:value=>{occluded=value;},current:value=>{current=value;},get invalidations(){return invalidations;},get collections(){return collections;},complete:()=>frameComplete({backend:'webgl2'}),frameError:()=>frameError(),lose:()=>canvasChange(),paint:()=>{submit();frameComplete({backend:'webgl2'});},get created(){return created;},get disposed(){return disposed;}};
}
test('capture-phase visibility invalidation restores native immediately and rechecks after owner hides',async()=>{
 const f=fixture();f.paint();f.occlude(true);
 f.emit('animationend');assert.equal(f.image.style.getPropertyValue('object-position'),'');
 const before=f.collections;f.occlude(false);f.emit('openclank:presentation-visibility-changed');
 await Promise.resolve();assert.equal(f.collections,before+1);assert.equal(f.region.snapshot().images,1);
 assert.equal(f.image.style.getPropertyValue('object-position'),'','only a completed replacement frame may suppress');
 f.paint();assert.equal(f.image.style.getPropertyValue('object-position'),BITMAP_OUTSIDE_POSITION);f.region.dispose();
});
test('remaining or reopened overlay is not bypassed; later post-hide signal recovers without polling',async()=>{
 const f=fixture();f.paint();f.occlude(true);f.emit('odysseus:modal-closed');await Promise.resolve();
 assert.equal(f.region.snapshot().images,0);assert.equal(f.budget.snapshot().contexts,0);assert.equal(f.image.style.getPropertyValue('object-position'),'');
 const idle=f.collections;await Promise.resolve();await Promise.resolve();assert.equal(f.collections,idle);
 f.occlude(false);f.emit('openclank:presentation-visibility-changed');f.occlude(true);await Promise.resolve();assert.equal(f.region.snapshot().images,0);
 f.occlude(false);f.emit('openclank:presentation-visibility-changed');await Promise.resolve();assert.equal(f.region.snapshot().images,1);f.region.dispose();
});
test('visibility bursts coalesce, policy remains ordinary and disposal makes pending refresh inert',async()=>{
 const f=fixture();const before=f.collections;
 for(let i=0;i<20;i++)for(const event of ['animationend','odysseus:modal-opened','openclank:presentation-visibility-changed'])f.emit(event);
 await Promise.resolve();assert.equal(f.collections,before+1);
 const draws=f.draws.length,invalidations=f.invalidations;await Promise.resolve();assert.equal(f.draws.length,draws);assert.equal(f.invalidations,invalidations);
 f.policy.set('ordinary');f.emit('openclank:presentation-visibility-changed');await Promise.resolve();assert.equal(f.budget.snapshot().contexts,0);
 f.emit('animationend');const last=f.collections;f.region.dispose();await Promise.resolve();assert.equal(f.collections,last);assert.equal(f.budget.snapshot().mediaBytes,0);
});
test('revocation or ownership loss before queued refresh cannot recreate private pixels',async()=>{
 for(const change of [f=>f.current(false),f=>f.emit('doc:openclank:file-policy-changed'),f=>f.emit('doc:openclank:auth-user-ready')]){
  const f=fixture();f.paint();f.emit('openclank:presentation-visibility-changed');change(f);await Promise.resolve();
  assert.equal(f.created,1);assert.equal(f.region.snapshot().images,0);assert.equal(f.budget.snapshot().contexts,0);assert.equal(f.image.style.getPropertyValue('object-position'),'');f.region.dispose();
 }
});
test('ordinary semantic image stays exposed until upload signal; switch restores same element',()=>{
 const f=fixture();assert.equal(f.image.style.opacity,'');assert.equal(f.created,1);assert.equal(f.budget.snapshot().mediaBytes,32000);
 f.paint();assert.equal(f.image.style.getPropertyValue('object-position'),BITMAP_OUTSIDE_POSITION);assert.equal(f.image.style.opacity,'');assert.equal(f.root.dataset.graphicalBackend,'webgl2');
 f.policy.set('ordinary');assert.equal(f.image.style.opacity,'');assert.equal(f.budget.snapshot().contexts,0);assert.equal(f.disposed,1);
 f.region.dispose();assert.equal(f.budget.snapshot().mediaBytes,0);
});
test('upload refusal never hides content and releases resources once',async()=>{
 const f=fixture({ok:false});f.paint();assert.equal(f.image.style.opacity,'');await Promise.resolve();assert.equal(f.region.snapshot().failed,true);assert.equal(f.budget.snapshot().contexts,0);f.region.refresh();assert.equal(f.created,1);f.region.dispose();
});
test('draw acceptance is not enough: only completed frame hides pixels; loss restores synchronously',async()=>{
 const f=fixture();f.submit();assert.equal(f.image.style.getPropertyValue('object-position'),'');f.complete();assert.equal(f.image.style.getPropertyValue('object-position'),BITMAP_OUTSIDE_POSITION);
 f.lose();assert.equal(f.image.style.opacity,'');await Promise.resolve();assert.equal(f.budget.snapshot().contexts,0);assert.equal(f.region.snapshot().images,0);f.region.dispose();
 const g=fixture();g.paint();g.frameError();assert.equal(g.image.style.opacity,'');await Promise.resolve();assert.equal(g.budget.snapshot().mediaBytes,0);g.region.dispose();
});
test('production cover/radius/border uses original styled img and scroll refresh never adopts suppression as authored position',()=>{
 const f=fixture({imageStyle:{objectFit:'cover',objectPosition:'50% 50%',borderTopWidth:'1px',borderRightWidth:'1px',borderBottomWidth:'1px',borderLeftWidth:'1px',borderTopLeftRadius:'7px',borderTopRightRadius:'7px',borderBottomLeftRadius:'7px',borderBottomRightRadius:'7px'}});
 f.image.style.setProperty('object-position','50% 50%','important');
 f.region.refresh();f.paint();const first=f.draws.at(-1);assert.equal(first[6].clip.rx[0],6);assert.equal(first[3],98);assert.equal(first[4],78);
 f.paint();assert.equal(f.image.style.getPropertyValue('object-position'),BITMAP_OUTSIDE_POSITION);
 f.root.scrollTop=24;f.root.scrollLeft=12;f.region.refresh();assert.equal(f.image.style.getPropertyValue('object-position'),'50% 50%');f.paint();assert.deepEqual(f.draws.at(-1)[6],first[6]);assert.equal(f.created,1);
 f.policy.set('ordinary');assert.equal(f.image.style.getPropertyValue('object-position'),'50% 50%');assert.equal(f.image.style.getPropertyPriority('object-position'),'important');assert.equal(f.image.style.opacity,'');f.region.dispose();
});
test('changed image binding and revoked authority cannot retain hidden private pixels',()=>{
 const f=fixture();f.paint();f.image.src='blob:other';f.region.refresh();assert.equal(f.image.style.opacity,'');assert.equal(f.budget.snapshot().contexts,0);assert.equal(loadedStillImage(f.image),null);f.region.dispose();
 const g=fixture();g.paint();forgetLoadedImage(g.image);g.region.refresh();assert.equal(g.image.style.opacity,'');assert.equal(g.budget.snapshot().contexts,0);g.region.dispose();
});
test('oversized image and backing surface retain ordinary path',()=>{
 const f=fixture();f.image.naturalWidth=2000;f.region.refresh();assert.equal(f.budget.snapshot().contexts,0);assert.equal(f.image.style.opacity,'');f.region.dispose();
 const g=fixture();g.root.clientWidth=4000;g.root.clientHeight=4000;g.region.refresh();assert.equal(g.budget.snapshot().contexts,0);g.region.dispose();
});
const chunk=(type,data=[])=>{const bytes=new Uint8Array(12+data.length);new DataView(bytes.buffer).setUint32(0,data.length);bytes.set([...type].map(c=>c.charCodeAt(0)),4);bytes.set(data,8);return bytes;};
test('bounded existing Blob classification refuses animated/unknown images without fetching',async()=>{
 const signature=new Uint8Array([137,80,78,71,13,10,26,10]),ihdr=chunk('IHDR',Array(13).fill(0));
 assert.equal(await classifyStillBlob(new Blob([signature,ihdr,chunk('IDAT'),chunk('IEND')],{type:'image/png'})),true);
 assert.equal(await classifyStillBlob(new Blob([signature,ihdr,chunk('acTL',[1]),chunk('IEND')],{type:'image/png'})),false);
 assert.equal(await classifyStillBlob(new Blob([[1,2,3]],{type:'image/jpeg'})),false);
 assert.equal(await classifyStillBlob(new Blob([new Uint8Array([255,216,255])],{type:'image/jpeg'})),true);
 assert.equal(await classifyStillBlob(new Blob(['GIF89a'],{type:'image/gif'})),false);
 assert.equal(await classifyStillBlob(new Blob([new Uint8Array(4*1024*1024+1)],{type:'image/png'})),false);
});
