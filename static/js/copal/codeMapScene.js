// Compact Code Map graphics adapter. Text editing stays DOM/CodeMirror-owned.
// This reports backend capability, never a hardware/GPU performance verdict.
import {createGraphicsConsumer} from '../graphics/consumer.js';
import {graphicalPresentationPolicy} from '../graphics/presentationPolicy.js';
import {graphicalSurfaceBudget} from '../graphics/surfaceBudget.js';
import {codeMapKey} from './codeMapModel.js';
import {createCodeMapLayoutOwner} from './codeMapLayout.js';
const MAX_BYTES=4*1024*1024;
const size=v=>new TextEncoder().encode(JSON.stringify(v)).length;
const need=ok=>{if(!ok)throw Error('Invalid Code Map scene');};
const clamp=(x,a,b)=>Math.max(a,Math.min(b,x));
const DEFAULT_PALETTE={background:'#121824',node:'#96a9c6',group:'#c6a5fa',selected:'#ffcd70',label:'#edf2ff',containment:'#65738c',exact:'#79b9fa',inferred:'#b6a1ed',import:'#74c9b4'};
const OWNER_KEY='__openClankCodeMapScenes';
const MIN_AUTOMATIC_SCALE=.45;
export function fitCodeMapCamera(positions,width,height,padding=36){
  if(!positions.length)return {x:width/2,y:height/2,scale:1};
  const xs=positions.map(p=>p.x),ys=positions.map(p=>p.y),minX=Math.min(...xs),maxX=Math.max(...xs),minY=Math.min(...ys),maxY=Math.max(...ys);
  const fitted=Math.min(Math.max(1,width-2*padding)/Math.max(160,maxX-minX+160),Math.max(1,height-2*padding)/Math.max(100,maxY-minY+100)),scale=clamp(fitted,MIN_AUTOMATIC_SCALE,2);
  // An impossible all-node fit must not merge fixed screen-space glyphs. Start
  // at the real branch's top/left and expose ordinary pan/Explorer overflow.
  return fitted<MIN_AUTOMATIC_SCALE?{scale,x:16-minX*scale,y:32-minY*scale}:{scale,x:width/2-(minX+maxX)/2*scale,y:height/2-(minY+maxY)/2*scale};
}
export function createCodeMapScene({mount,canvas,id='code-map',win=globalThis,doc=globalThis.document,ownerHost=globalThis,
  consumerFactory=createGraphicsConsumer,workerFactory=()=>typeof Worker==='function'?new Worker(new URL('./codeMapLayoutWorker.js',import.meta.url),{type:'module'}):null,
  ResizeObserverClass=globalThis.ResizeObserver,onChange=()=>{},onSelect=()=>{},onCanvasChange=()=>{},forceCanvas2D=false}={}){
  const registry=ownerHost[OWNER_KEY]||(ownerHost[OWNER_KEY]=new Map());
  registry.get(id)?.dispose();
  let disposed=false,namespace=null,serial=0,positions=new Map(),nodes=new Map(),edges=[],selected=null;
  let width=1,height=1,camera={x:0,y:0,scale:1},needsFit=true,layoutMode='pending',status='idle',fittedReset=false;
  let hitCells=new Map(),labels=[],drawn={nodes:0,edges:0,labels:0},palette={...DEFAULT_PALETTE},detach=()=>{},drag=null,observer=null;
  let paintNotificationPending=false,lastDraw=null,allocationDpr=null;
  let resolutionDpr=null,detachResolution=()=>{};
  let cameraMode='default',revealRequest=null,revealStatus=null,seedProtected=false,responsivePending=false;
  let density={mode:'pending',totalNodes:0,visibleNodes:0,offscreenNodes:0,totalGroups:0,visibleGroups:0,labeledGroups:0};
  let textContext=null;try{const measure=doc?.createElement?.('canvas');if(measure){measure.width=measure.height=1;textContext=measure.getContext?.('2d')||null;}}catch{}
  const textMetrics=new Map();
  function measureLabel(text,weight=400){
    const key=weight+'\0'+text;if(textMetrics.has(key))return textMetrics.get(key);
    let left=1,right=Array.from(text).length*10+1;
    if(textContext){textContext.font=`${weight} 12px sans-serif`;const m=textContext.measureText(text);if(Number.isFinite(m.width)){left=Math.ceil(Math.max(0,Number(m.actualBoundingBoxLeft)||0))+1;right=Math.ceil(Math.max(m.width,Number(m.actualBoundingBoxRight)||0))+1;}}
    const metrics={left,width:left+right};if(textMetrics.size>=512)textMetrics.delete(textMetrics.keys().next().value);textMetrics.set(key,metrics);return metrics;
  }
  function labelFor(label,maxWidth,weight){
    const chars=Array.from(label),limited=chars.slice(0,80).join('');let text=limited,truncated=chars.length>80,metrics=measureLabel(text,weight);
    if(metrics.width>maxWidth||truncated){let low=0,high=Math.min(80,chars.length);while(low<high){const mid=Math.ceil((low+high)/2);if(measureLabel(chars.slice(0,mid).join('')+'…',weight).width<=maxWidth)low=mid;else high=mid-1;}text=low?chars.slice(0,low).join('')+'…':'';metrics=measureLabel(text,weight);truncated=true;}
    return {text,...metrics,truncated};
  }
  const pointers=new Map();let pinch=null;
  const point=p=>({x:p.x*camera.scale+camera.x,y:p.y*camera.scale+camera.y});
  const notify=()=>onChange(snapshot());
  function resetPointers(target){for(const id of pointers.keys())if(target?.hasPointerCapture?.(id))target.releasePointerCapture?.(id);pointers.clear();pinch=null;drag=null;}
  function snapshot(){return {disposed,namespace,selected,camera:{...camera},cameraMode,revealStatus,layoutMode,status,fittedReset,pointerCount:pointers.size,drawn:{...drawn},density:{...density},labels:labels.map(l=>({...l})),backend:consumer?.backendKind||null,accelerated:consumer?.accelerated===true,fallbackReason:consumer?.fallbackReason||null,layout:layout.snapshot()};}
  function controlCamera(){cameraMode='user';needsFit=false;seedProtected=true;if(responsivePending&&positions.size)submitLayout(true);revealRequest=null;revealStatus=null;}
  function revealBounds(){const i=revealRequest.insets;return {left:Math.min(width,i.left),right:Math.max(0,width-i.right),top:Math.min(height,i.top),bottom:Math.max(0,height-i.bottom)};}
  function applyReveal(){
    if(!revealRequest)return false;
    if(revealRequest.id!==selected||revealRequest.namespace!==namespace){revealRequest=null;revealStatus=null;return false;}
    if(status!=='ready'||!positions.has(selected)){revealStatus='pending';return false;}
    const bounds=revealBounds(),availableW=bounds.right-bounds.left,availableH=bounds.bottom-bounds.top;
    if(availableW<52||availableH<32){revealStatus='insufficient-space';return false;}
    // Neighbor navigation may reveal real endpoints as a bounded context, but
    // an ordinary late layout/resize must never undo a user-controlled camera.
    if(revealRequest.contextIds.length&&cameraMode==='user'){revealStatus='user-camera';return false;}
    let contextChanged=false;
    if(revealRequest.contextIds.length){
      const fit=ids=>{const ps=ids.map(id=>positions.get(id));const minX=Math.min(...ps.map(p=>p.x)),maxX=Math.max(...ps.map(p=>p.x)),minY=Math.min(...ps.map(p=>p.y)),maxY=Math.max(...ps.map(p=>p.y));
        const label=Math.min(availableW-30,Math.max(...ids.map(id=>measureLabel(Array.from(nodes.get(id).label).slice(0,80).join(''),id===selected?600:400).width)));
        const scale=Math.min(camera.scale,2,(availableW-label-30)/Math.max(1,maxX-minX),(availableH-32)/Math.max(1,maxY-minY));
        return scale<MIN_AUTOMATIC_SCALE?null:{scale,x:bounds.left+12+(availableW-label-24)/2-(minX+maxX)/2*scale,y:(bounds.top+bounds.bottom)/2-(minY+maxY)/2*scale};};
      const ids=[selected];let next=null;
      for(const id of revealRequest.contextIds){if(id===selected||!positions.has(id)||!nodes.has(id)||!edges.some(e=>e.relation!=='containment'&&(e.from===selected&&e.to===id||e.to===selected&&e.from===id)))continue;const candidate=fit([...ids,id]);if(candidate){ids.push(id);next=candidate;}}
      if(next){contextChanged=Math.abs(next.x-camera.x)>1e-7||Math.abs(next.y-camera.y)>1e-7||Math.abs(next.scale-camera.scale)>1e-7;camera=next;}
    }
    const p=point(positions.get(selected)),labelWidth=measureLabel(Array.from(nodes.get(selected).label).slice(0,80).join(''),600).width;
    const shownWidth=Math.min(labelWidth,availableW-30),x=clamp(p.x,bounds.left+12,bounds.right-13-shownWidth),y=clamp(p.y,bounds.top+14,bounds.bottom-8);
    const dx=Math.abs(x-p.x)>1e-7?x-p.x:0,dy=Math.abs(y-p.y)>1e-7?y-p.y:0;
    const changed=contextChanged||dx!==0||dy!==0||revealStatus!=='revealed';camera.x+=dx;camera.y+=dy;revealStatus='revealed';return changed;
  }
  function draw({batch}){
    hitCells=new Map();labels=[];drawn={nodes:0,edges:0,labels:0};batch.rect(0,0,width,height,palette.background,1);
    const exposed=revealRequest?revealBounds():{left:0,right:width,top:0,bottom:height};
    const visible=new Map();for(const [key,p] of positions){const n=nodes.get(key);if(!n)continue;const q=point(p),radius=key===selected?9:n.kind==='file'?5:8;if(q.x>=exposed.left+radius&&q.x<=exposed.right-radius&&q.y>=exposed.top+radius&&q.y<=exposed.bottom-radius)visible.set(key,{...q,node:n});}
    for(const edge of edges){const a=positions.get(edge.from),b=positions.get(edge.to);if(!a||!b)continue;const p=point(a),q=point(b);
      if(Math.max(p.x,q.x)<0||Math.min(p.x,q.x)>width||Math.max(p.y,q.y)<0||Math.min(p.y,q.y)>height)continue;
      const color=edge.relation==='containment'?palette.containment:edge.relation==='import'?palette.import:edge.state==='inferred'?palette.inferred:palette.exact;
      if(edge.state==='inferred'){for(let i=0;i<3;i++){const a=i/3,b=(i+.55)/3;batch.line(p.x+(q.x-p.x)*a,p.y+(q.y-p.y)*a,p.x+(q.x-p.x)*b,p.y+(q.y-p.y)*b,color,1.5,.85);}}
      else batch.line(p.x,p.y,q.x,q.y,color,edge.relation==='containment'?1:1.7,edge.relation==='containment'?.5:.9);
      // Screen-space arrowheads preserve the actual from -> to direction.
      // Very short projected edges cannot fit a distinct arrow and node rim.
      const length=Math.hypot(q.x-p.x,q.y-p.y);
      if(length>24){const dx=(q.x-p.x)/length,dy=(q.y-p.y)/length,radius=edge.to===selected?10:nodes.get(edge.to)?.kind==='file'?7:10;
        const x=q.x-dx*radius,y=q.y-dy*radius;
        batch.line(x,y,x-dx*7-dy*3.5,y-dy*7+dx*3.5,color,1.5,.9);
        batch.line(x,y,x-dx*7+dy*3.5,y-dy*7-dx*3.5,color,1.5,.9);
      }
      drawn.edges++;
    }
    for(const [key,p] of visible){const radius=key===selected?9:p.node.kind==='file'?5:8;
      batch.circle(p.x,p.y,radius,key===selected?palette.selected:p.node.kind==='file'?palette.node:palette.group,1);
      const cell=`${Math.floor(p.x/32)},${Math.floor(p.y/32)}`;if(!hitCells.has(cell))hitCells.set(cell,[]);hitCells.get(cell).push({id:key,x:p.x,y:p.y,radius:Math.max(9,radius)});drawn.nodes++;
    }
    const kindRank={repository:0,folder:1,file:2};
    const hitsGlyph=(x,y,w)=>{const top=y-12,bottom=y+4;for(let cx=Math.floor((x-12)/32);cx<=Math.floor((x+w+12)/32);cx++)for(let cy=Math.floor((top-12)/32);cy<=Math.floor((bottom+12)/32);cy++)for(const p of hitCells.get(`${cx},${cy}`)||[]){const nearX=clamp(p.x,x,x+w),nearY=clamp(p.y,top,bottom);if(Math.hypot(p.x-nearX,p.y-nearY)<=p.radius+2)return true;}return false;};
    const priority=[...visible.entries()].sort(([a,p],[b,q])=>(b===selected)-(a===selected)||kindRank[p.node.kind]-kindRank[q.node.kind]||a.localeCompare(b,'en'));
    for(const [key,p] of priority){if(labels.length>=200)break;
      const bounded=exposed;
      if(width<40||height<28)continue;
      const fontSize=12,weight=key===selected?600:400,label=labelFor(p.node.label,bounded.right-bounded.left-(key===selected&&revealStatus==='revealed'?30:4),weight),labelWidth=label.width;
      if(!label.text)continue;
      const xs=[p.x+12,p.x-12-labelWidth].map(x=>clamp(x,bounded.left+2,Math.max(bounded.left+2,bounded.right-labelWidth-2))),baseY=clamp(p.y+5,bounded.top+14,Math.max(bounded.top+14,bounded.bottom-3));
      // Fixed screen-space font; bounded callout slots avoid shrinking an
      // otherwise useful coarse hierarchy into unreadable or missing labels.
      let x=null,y=null;for(const offset of [0,20,-20,40,-40,60,-60,80,-80]){const candidate=clamp(baseY+offset,bounded.top+14,Math.max(bounded.top+14,bounded.bottom-3));for(const candidateX of xs)if(!hitsGlyph(candidateX,candidate,labelWidth)&&!labels.some(l=>candidateX<l.x+l.width+5&&candidateX+labelWidth+5>l.x&&candidate-12<l.y+4&&candidate+4>l.y-12)){x=candidateX;y=candidate;break;}if(y!==null)break;}
      if(y===null)continue;
      if(Math.abs(y-baseY)>=14)batch.line(p.x,p.y,x,y-5,palette.containment,.7,.45);
      batch.glyph(label.text,x+label.left,y,fontSize,weight,palette.label,1,'left','alphabetic','sans-serif');labels.push({id:key,x,y,width:labelWidth,fontSize,text:label.text,truncated:label.truncated});
    }
    drawn.labels=labels.length;
    const group=n=>n.kind!=='file';density={mode:drawn.nodes<nodes.size?'overflow':'fit',totalNodes:nodes.size,visibleNodes:drawn.nodes,offscreenNodes:nodes.size-drawn.nodes,totalGroups:[...nodes.values()].filter(group).length,visibleGroups:[...visible.values()].filter(p=>group(p.node)).length,labeledGroups:labels.filter(l=>group(nodes.get(l.id))).length};
    consumer?.canvas?.setAttribute?.('aria-description',density.mode==='overflow'?`${density.visibleNodes} of ${density.totalNodes} nodes visible; pan or use Explore for the rest.`:'All current map nodes fit in this viewport.');
    lastDraw={serial,namespace};
    if(!paintNotificationPending){paintNotificationPending=true;queueMicrotask(()=>{
      paintNotificationPending=false;
      // Runs after the consumer's synchronous batch flush/endFrame, never
      // invalidates the owner or queues another animation frame.
      if(!disposed&&lastDraw?.serial===serial&&lastDraw.namespace===namespace)notify();
    });}
  }
  function local(event){const r=consumer.canvas.getBoundingClientRect();return {x:(event.clientX-r.left)*width/Math.max(1,r.width),y:(event.clientY-r.top)*height/Math.max(1,r.height)};}
  function pick(x,y){let best=null,distance=Infinity;const cx=Math.floor(x/32),cy=Math.floor(y/32);for(let a=-1;a<=1;a++)for(let b=-1;b<=1;b++)for(const p of hitCells.get(`${cx+a},${cy+b}`)||[]){const d=Math.hypot(p.x-x,p.y-y);if(d<=p.radius&&d<distance){best=p.id;distance=d;}}return best;}
  function attach(next,previous=null){
    detach();if(next.style)next.style.pointerEvents='auto';next.removeAttribute?.('aria-hidden');next.setAttribute?.('role','img');next.setAttribute?.('aria-label','Code repository map; arrow keys pan, plus and minus zoom; use companion explorer for source actions');next.tabIndex=0;
    const listeners=[];const listen=(type,fn,options)=>{next.addEventListener?.(type,fn,options);listeners.push(()=>next.removeEventListener?.(type,fn,options));};
    const pair=()=>{const [a,b]=[...pointers.values()];return {distance:Math.max(1,Math.hypot(b.x-a.x,b.y-a.y)),mid:{x:(a.x+b.x)/2,y:(a.y+b.y)/2}};};
    listen('pointerdown',event=>{if(event.button!==0||pointers.size>=2||pointers.has(event.pointerId))return;const p=local(event);pointers.set(event.pointerId,p);next.setPointerCapture?.(event.pointerId);
      if(pointers.size===2){pinch={...pair(),camera:{...camera}};drag=null;}else drag={id:event.pointerId,start:p,last:p,moved:false};});
    listen('pointermove',event=>{if(!pointers.has(event.pointerId))return;const p=local(event);pointers.set(event.pointerId,p);
      if(pointers.size===2&&pinch){controlCamera();const current=pair(),scale=clamp(pinch.camera.scale*current.distance/pinch.distance,.001,8),wx=(pinch.mid.x-pinch.camera.x)/pinch.camera.scale,wy=(pinch.mid.y-pinch.camera.y)/pinch.camera.scale;camera={x:current.mid.x-wx*scale,y:current.mid.y-wy*scale,scale};consumer.invalidate();return;}
      if(!drag||drag.id!==event.pointerId)return;if(Math.hypot(p.x-drag.start.x,p.y-drag.start.y)>4)drag.moved=true;if(drag.moved){controlCamera();camera.x+=p.x-drag.last.x;camera.y+=p.y-drag.last.y;consumer.invalidate();}drag.last=p;});
    const endPointer=(event,cancelled=false)=>{if(!pointers.has(event.pointerId))return;const wasPinch=pointers.size===2,p=local(event),click=!cancelled&&!wasPinch&&drag?.id===event.pointerId&&!drag.moved;pointers.delete(event.pointerId);pinch=null;drag=null;if(next.hasPointerCapture?.(event.pointerId))next.releasePointerCapture?.(event.pointerId);
      if(pointers.size===1){const [id,point]=[...pointers.entries()][0];drag={id,start:point,last:point,moved:true};}if(click)select(pick(p.x,p.y),{notify:true});};
    listen('pointerup',event=>endPointer(event));
    listen('pointercancel',event=>endPointer(event,true));
    listen('lostpointercapture',event=>endPointer(event,true));
    listen('wheel',event=>{event.preventDefault();const p=local(event);zoom(Math.exp(-clamp(event.deltaY,-200,200)*.002),p);},{passive:false});
    listen('keydown',event=>{if(['ArrowLeft','ArrowRight','ArrowUp','ArrowDown'].includes(event.key)){event.preventDefault();controlCamera();camera.x+=event.key==='ArrowLeft'?32:event.key==='ArrowRight'?-32:0;camera.y+=event.key==='ArrowUp'?32:event.key==='ArrowDown'?-32:0;consumer.invalidate();}else if(event.key==='+'||event.key==='='){event.preventDefault();zoom(1.2);}else if(event.key==='-'){event.preventDefault();zoom(1/1.2);}else if(event.key==='0'){event.preventDefault();fit();}else if(event.key==='Escape')select(null,{notify:true});});
    detach=()=>{for(const remove of listeners)remove();resetPointers(next);};onCanvasChange(next,previous);
  }
  let consumer=null;
  const layout=createCodeMapLayoutOwner({workerFactory,changed:(result,mode)=>{
    if(disposed)return;layoutMode=mode;
    if(!result){status='layout-error';consumer?.invalidate();notify();return;}
    if(result.serial!==serial||result.namespace!==namespace)return;
    try{need(Array.isArray(result.positions)&&result.positions.length===nodes.size&&size(result)<=MAX_BYTES);const next=new Map();for(const p of result.positions){need(nodes.has(p.id)&&!next.has(p.id)&&Number.isFinite(p.x)&&Number.isFinite(p.y)&&Math.abs(p.x)<=1e7&&Math.abs(p.y)<=1e7);next.set(p.id,{id:p.id,x:p.x,y:p.y});}positions=next;
      responsivePending=false;if(needsFit){camera=fitCodeMapCamera([...positions.values()],width,height);needsFit=false;fittedReset=true;}status='ready';applyReveal();consumer?.invalidate();notify();
    }catch{status='layout-error';positions.clear();consumer?.invalidate();notify();}
  }});
  const presentationPolicy=graphicalPresentationPolicy(),surfaceBudget=graphicalSurfaceBudget();
  let contextLease=null;
  function mountConsumer(initialCanvas){
    const ordinary=forceCanvas2D||presentationPolicy.mode==='ordinary';
    if(!ordinary)contextLease=surfaceBudget.acquire(`map:${id}`);
    consumer=consumerFactory({mount,canvas:initialCanvas,id,win,doc,ownerHost,renderMode:'demand',forceCanvas2D:ordinary||!contextLease,draw,onCanvasChange:attach});allocationDpr=null;attach(consumer.canvas);
    if(consumer.backendKind!=='webgl2'){contextLease?.release();contextLease=null;}
  }
  mountConsumer(canvas);
  const unsubscribePresentation=presentationPolicy.subscribe(()=>{
    if(disposed)return;
    detach();consumer.dispose();contextLease?.release();contextLease=null;
    mountConsumer(null);if(!measureMount())resize(width,height,readDpr());consumer.invalidate();
  });
  function submitLayout(preserve){
    serial++;status='layout-pending';responsivePending=!preserve;
    layout.submit({serial,namespace,nodes:[...nodes.values()].map(({id,parent,kind})=>({id,parent,kind})),edges:edges.map(({from,to})=>({from,to})),previous:preserve?[...positions.values()].filter(p=>nodes.has(p.id)):[],anchor:selected,viewport:{width,height}});
  }
  function resize(w,h,dpr=win.devicePixelRatio||1){
    if(disposed)return;need(Number.isFinite(w)&&Number.isFinite(h)&&w>0&&h>0);const oldWidth=width,oldHeight=height;width=clamp(w,1,8192);height=clamp(h,1,8192);const ratio=Math.min(2,Math.max(1,dpr)),geometryChanged=width!==oldWidth||height!==oldHeight;
    if(!geometryChanged&&allocationDpr===ratio)return;
    consumer.resize(width,height,ratio);allocationDpr=ratio;
    // Pixel-density changes are allocation only: no camera/layout/reveal reset.
    if(!geometryChanged)return;
    if(nodes.size&&cameraMode==='default'&&!seedProtected&&(width!==oldWidth||height!==oldHeight)){needsFit=true;submitLayout(false);}
    else if(positions.size&&(needsFit||cameraMode==='default')){camera=fitCodeMapCamera([...positions.values()],width,height);needsFit=false;}
    else if(cameraMode==='user'){camera.x+=(width-oldWidth)/2;camera.y+=(height-oldHeight)/2;}
    applyReveal();consumer.invalidate();
  }
  function fit(){if(disposed)return;cameraMode='default';revealRequest=null;revealStatus=null;camera=fitCodeMapCamera([...positions.values()],width,height);needsFit=false;fittedReset=true;consumer.invalidate();notify();}
  function zoom(factor,p={x:width/2,y:height/2}){if(disposed)return;need(Number.isFinite(factor)&&factor>0&&Number.isFinite(p.x)&&Number.isFinite(p.y));controlCamera();const scale=clamp(camera.scale*factor,.001,8),ratio=scale/camera.scale;camera={x:p.x-(p.x-camera.x)*ratio,y:p.y-(p.y-camera.y)*ratio,scale};consumer.invalidate();}
  function select(id,{notify:notifySelection=false}={}){if(disposed||id!==null&&!nodes.has(id))return false;if(selected!==id){revealRequest=null;revealStatus=null;}selected=id;if(id!==null){seedProtected=true;if(responsivePending&&positions.size){needsFit=false;submitLayout(true);}}consumer.invalidate();if(notifySelection)onSelect(id===null?null:{id,authorityNamespace:namespace});notify();return true;}
  function revealSelected({insets={},contextIds=[]}={}){if(disposed||selected===null)return false;need(insets&&Object.keys(insets).every(k=>['left','right','top','bottom'].includes(k)));need(Array.isArray(contextIds)&&contextIds.length<=32&&contextIds.every(id=>typeof id==='string'&&id.length>0&&id.length<=256)&&new Set(contextIds).size===contextIds.length);const normalized={left:0,right:0,top:0,bottom:0,...insets};for(const v of Object.values(normalized))need(Number.isFinite(v)&&v>=0&&v<=8192);const request={id:selected,namespace,insets:normalized,contextIds:[...contextIds]},changedRequest=codeMapKey(revealRequest)!==codeMapKey(request);revealRequest=request;const changed=applyReveal();if(changedRequest||changed)consumer.invalidate();return true;}
  function clear(){if(disposed)return;namespace=null;serial++;layout.invalidate();positions.clear();nodes.clear();edges=[];hitCells.clear();textMetrics.clear();resetPointers(consumer.canvas);selected=null;needsFit=true;seedProtected=false;cameraMode='default';revealRequest=null;revealStatus=null;density={mode:'pending',totalNodes:0,visibleNodes:0,offscreenNodes:0,totalGroups:0,visibleGroups:0,labeledGroups:0};status='idle';consumer.invalidate();notify();}
  function update(value){if(disposed)return false;
    if(!value?.authority||!['ready','loading'].includes(value.status)){clear();return false;}
    need(['structure_preview','published'].includes(value.authority.variant)&&Array.isArray(value.nodes)&&value.nodes.length<=2000&&Array.isArray(value.edges)&&value.edges.length<=5000&&size(value)<=MAX_BYTES);
    const nextNamespace=codeMapKey(value.authority),nextNodes=new Map();for(const n of value.nodes){need(typeof n.id==='string'&&!nextNodes.has(n.id)&&typeof n.label==='string'&&Array.from(n.label).length<=160&&['repository','folder','file'].includes(n.kind));nextNodes.set(n.id,{id:n.id,label:n.label,kind:n.kind,parent:n.parent});}
    const nextEdges=value.edges.map(e=>{need(nextNodes.has(e.from)&&nextNodes.has(e.to)&&['containment','import','call'].includes(e.relation)&&['structure','exact','inferred'].includes(e.state));return {from:e.from,to:e.to,relation:e.relation,state:e.state};});
    if(nextNamespace!==namespace){positions.clear();hitCells.clear();textMetrics.clear();resetPointers(consumer.canvas);selected=null;needsFit=true;seedProtected=false;cameraMode='default';revealRequest=null;revealStatus=null;fittedReset=false;layout.invalidate();}
    else if(positions.size)seedProtected=true;
    namespace=nextNamespace;nodes=nextNodes;edges=nextEdges;if(selected!==null&&!nodes.has(selected))selected=null;
    submitLayout(true);consumer.invalidate();return true;
  }
  function readDpr(){const n=Number(win.devicePixelRatio);return Number.isFinite(n)&&n>0?n:1;}
  function measureMount(){if(disposed||!mount)return false;const r=mount.getBoundingClientRect();if(r.width>0&&r.height>0){const logicalWidth=mount.clientWidth||r.width;resize(logicalWidth,mount.clientHeight||r.height,Math.min(2,readDpr()*r.width/logicalWidth));return true;}return false;}
  function watchResolution(){
    const dpr=readDpr();if(resolutionDpr===dpr)return;detachResolution();detachResolution=()=>{};resolutionDpr=dpr;
    if(typeof win.matchMedia!=='function')return;const query=win.matchMedia(`(resolution: ${dpr}dppx)`);
    if(query.addEventListener){query.addEventListener('change',refreshPixels);detachResolution=()=>query.removeEventListener('change',refreshPixels);}
    else if(query.addListener){query.addListener(refreshPixels);detachResolution=()=>query.removeListener(refreshPixels);}
  }
  function refreshPixels(){if(disposed)return;watchResolution();measureMount();}
  if(ResizeObserverClass&&mount){observer=new ResizeObserverClass(measureMount);observer.observe(mount);}
  win.addEventListener?.('resize',refreshPixels);watchResolution();if(mount)measureMount();
  const api={update,clear,select,revealSelected,fit,resize,snapshot,pick,zoom,
    setPalette(patch){if(disposed)return;for(const [key,color] of Object.entries(patch))if(Object.hasOwn(DEFAULT_PALETTE,key)&&typeof color==='string'&&/^#[0-9a-f]{6}$/i.test(color))palette[key]=color;consumer.invalidate();},
    suspend(){consumer.suspend();},resume(){consumer.resume();},
    dispose(){if(disposed)return;disposed=true;serial++;layout.dispose();observer?.disconnect();win.removeEventListener?.('resize',refreshPixels);detachResolution();detach();consumer.dispose();contextLease?.release();unsubscribePresentation();nodes.clear();positions.clear();textMetrics.clear();edges=[];hitCells.clear();labels=[];if(registry.get(id)===api)registry.delete(id);},
    get canvas(){return consumer.canvas;},
  };
  registry.set(id,api);return api;
}
