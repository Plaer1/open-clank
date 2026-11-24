import {CODE_GRAPH_DISPLAY,resolveGraphSettings} from './graphSettings.js';
// Compact Code Map graphics adapter. Text editing stays DOM/CodeMirror-owned.
// This reports backend capability, never a hardware/GPU performance verdict.
import {createGraphicsConsumer} from '../graphics/consumer.js';
import {graphicalPresentationPolicy} from '../graphics/presentationPolicy.js';
import {graphicalSurfaceBudget} from '../graphics/surfaceBudget.js';
import {codeMapKey, MAP_RELATIONS} from './codeMapModel.js';
import {codeMapTerritory,createCodeMapLayoutOwner} from './codeMapLayout.js';
const MAX_BYTES=CODE_GRAPH_DISPLAY.sceneBytes;
const size=v=>new TextEncoder().encode(JSON.stringify(v)).length;
const need=ok=>{if(!ok)throw Error('Invalid Code Map scene');};
const clamp=(x,a,b)=>Math.max(a,Math.min(b,x));
const DEFAULT_PALETTE={background:'#121824',node:'#96a9c6',group:'#c6a5fa',selected:'#ffcd70',label:'#edf2ff',containment:'#65738c',exact:'#79b9fa',inferred:'#b6a1ed',import:'#74c9b4'};
const OWNER_KEY='__openClankCodeMapScenes';
const MIN_AUTOMATIC_SCALE=.45;
export function fitCodeMapCamera(positions,width,height,padding=36,minimumScale=MIN_AUTOMATIC_SCALE,maximumScale=2){
  if(!positions.length)return {x:width/2,y:height/2,scale:1};
  const xs=positions.map(p=>p.x),ys=positions.map(p=>p.y),minX=Math.min(...xs),maxX=Math.max(...xs),minY=Math.min(...ys),maxY=Math.max(...ys);
  const fitted=Math.min(Math.max(1,width-2*padding)/Math.max(160,maxX-minX+160),Math.max(1,height-2*padding)/Math.max(100,maxY-minY+100)),scale=clamp(fitted,minimumScale,maximumScale);
  // An impossible all-node fit must not merge fixed screen-space glyphs. Start
  // at the real branch's top/left and expose ordinary pan/Explorer overflow.
  return fitted<minimumScale?{scale,x:16-minX*scale,y:32-minY*scale}:{scale,x:width/2-(minX+maxX)/2*scale,y:height/2-(minY+maxY)/2*scale};
}
export function createCodeMapScene({mount,canvas,id='code-map',win=globalThis,doc=globalThis.document,ownerHost=globalThis,
  consumerFactory=createGraphicsConsumer,workerFactory=()=>typeof Worker==='function'?new Worker(new URL('./codeMapLayoutWorker.js',import.meta.url),{type:'module'}):null,
  settings:initialSettings={},ResizeObserverClass=globalThis.ResizeObserver,onChange=()=>{},onSelect=()=>{},onCanvasChange=()=>{},onCameraTakeover=()=>{},forceCanvas2D=false}={}){
  const registry=ownerHost[OWNER_KEY]||(ownerHost[OWNER_KEY]=new Map());
  registry.get(id)?.dispose();
  let settings=resolveGraphSettings({common:initialSettings}),positionTween=null,cameraTween=null;const effects=new Map(),degrees=new Map();
  const pendingArrivals=new Set(),nodeArrivals=new Map();
  let lastNodeReveal=-Infinity;
  const revealIntervalMs=28;
  const now=()=>win.performance?.now?.()??Date.now();
  const canAnimate=()=>settings.motion==='system'&&settings.transitionMs>0&&consumer?.owner?.reducedMotion!==true;
  let placement='expanded',requestedBackend='auto',acquisitionReason=null;
  const requestedFps=()=>{const override=placement==='docked'?settings.dockedFps:settings.expandedFps;return override==='inherit'?settings.fps:override;};
  const frameRate=()=>{const requested=requestedFps(),rate=requested==='unlocked'?0:requested==='auto'?(placement==='docked'?30:60):Number(requested);return consumer?.backendKind==='canvas2d'?rate?Math.min(30,rate):30:rate;};
  const wake=duration=>consumer?.owner?.animate(duration,{fps:frameRate(),reduced:settings.motion!=='system'});
  let presentation='map',active=true,layoutPaused=false,layoutDeferred=false,layoutRelayout=false;
  let layoutPausedAt=0;
  const settledGeometry=()=>positionTween?.to || positions;
  const presentationCache=new Map();
  const layoutKey=(mode=presentation,config=settings)=>mode==='map'?mode:JSON.stringify([mode,config.grouping,config.groupDepth]);
  let disposed=false,namespace=null,serial=0,positions=new Map(),nodes=new Map(),edges=[],selected=null;
  let pins=new Map(),pathRelations=new Set(),observedActors=[];
  const relationKey=edge=>JSON.stringify([edge.from,edge.to,edge.relation]);
  let territoryByNode=new Map();const visited=new Set();let dimUnvisited=false,activityTrail=[];
  function updateTerritories(){
    territoryByNode=new Map();
    if(presentation==='map'||settings.grouping==='free')return;
    for(const id of nodes.keys()){
      const territory=codeMapTerritory(id,nodes,settings);
      if(territory)territoryByNode.set(id,territory);
    }
  }
  let width=1,height=1,camera={x:0,y:0,scale:1},needsFit=true,layoutMode='pending',status='idle',fittedReset=false;
  let hitCells=new Map(),labels=[],drawn={nodes:0,edges:0,labels:0},palette={...DEFAULT_PALETTE},detach=()=>{},drag=null,observer=null;
  let paintNotificationPending=false,lastDraw=null,lastAcknowledgedPaint=null,paintSequence=0,allocationDpr=null,effectiveDpr=null;
  let lastPaintNoticeAt=-Infinity,witnessedNamespace=null,canvasDescription='';
  let resolutionDpr=null,detachResolution=()=>{};
  let cameraMode='default',revealRequest=null,revealStatus=null,seedProtected=false,responsivePending=false;
  // Network overviews must frame the radial bounds, not Map's empty top-left
  // overflow corner. Manual pan/zoom cancels every pending automatic fit.
  const zoomScale=value=>clamp(value,settings.codeZoomMin/100,settings.codeZoomMax/100);
  const fittedCamera=(geometry=positions)=>fitCodeMapCamera([...geometry.values()],width,height,36,
    Math.max(settings.codeZoomMin/100,presentation==='map'?MIN_AUTOMATIC_SCALE:0),
    Math.min(2,settings.codeZoomMax/100));
  function constrainCamera(value) {
    const scale=zoomScale(value.scale);
    if(scale===value.scale)return value;
    const ratio=scale/value.scale;
    return {scale,x:width/2-(width/2-value.x)*ratio,y:height/2-(height/2-value.y)*ratio};
  }
  let density={mode:'pending',totalNodes:0,visibleNodes:0,offscreenNodes:0,totalGroups:0,visibleGroups:0,labeledGroups:0};
  let textContext=null;try{const measure=doc?.createElement?.('canvas');if(measure){measure.width=measure.height=1;textContext=measure.getContext?.('2d')||null;}}catch{}
  const textMetrics=new Map();
  function measureLabel(text,weight=400){
    const key=weight+'\0'+text;if(textMetrics.has(key))return textMetrics.get(key);
    let left=1,right=Array.from(text).length*settings.labelSize*.8+1;
    if(textContext){textContext.font=`${weight} ${settings.labelSize}px sans-serif`;const m=textContext.measureText(text);if(Number.isFinite(m.width)){left=Math.ceil(Math.max(0,Number(m.actualBoundingBoxLeft)||0))+1;right=Math.ceil(Math.max(m.width,Number(m.actualBoundingBoxRight)||0))+1;}}
    const metrics={left,width:left+right};if(textMetrics.size>=512)textMetrics.delete(textMetrics.keys().next().value);textMetrics.set(key,metrics);return metrics;
  }
  function labelFor(label,maxWidth,weight){
    const chars=Array.from(label),limited=chars.slice(0,settings.labelLength).join('');let text=limited,truncated=chars.length>settings.labelLength,metrics=measureLabel(text,weight);
    if(metrics.width>maxWidth||truncated){let low=0,high=Math.min(settings.labelLength,chars.length);while(low<high){const mid=Math.ceil((low+high)/2);if(measureLabel(chars.slice(0,mid).join('')+'…',weight).width<=maxWidth)low=mid;else high=mid-1;}text=low?chars.slice(0,low).join('')+'…':'';metrics=measureLabel(text,weight);truncated=true;}
    return {text,...metrics,truncated};
  }
  const pointers=new Map();let pinch=null;
  const point=p=>({x:p.x*camera.scale+camera.x,y:p.y*camera.scale+camera.y});
  const notify=()=>onChange(snapshot());
  function resetPointers(target){for(const id of pointers.keys())if(target?.hasPointerCapture?.(id))target.releasePointerCapture?.(id);pointers.clear();pinch=null;drag=null;}
  function snapshot(){return {disposed,namespace,presentation,selected,camera:{...camera},cameraMode,revealStatus,layoutMode,status,fittedReset,pointerCount:pointers.size,drawn:{...drawn},paint:lastAcknowledgedPaint?.serial===serial?{...lastAcknowledgedPaint}:null,density:{...density},labels:labels.map(l=>({...l})),backend:consumer?.backendKind||null,requestedBackend,accelerated:consumer?.accelerated===true,fallbackReason:acquisitionReason||consumer?.fallbackReason||null,renderPolicy:{placement,requestedFps:requestedFps(),effectiveAnimatedFps:frameRate()||'unlocked',requestedDpr:settings.dpr,effectiveDpr,reducedMotion:consumer?.owner?.reducedMotion===true,idle:'demand'},layout:layout.snapshot()};}
  function controlCamera(){if(cameraMode==='follow')onCameraTakeover();cameraTween=null;cameraMode='user';needsFit=false;seedProtected=true;revealRequest=null;revealStatus=null;}
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
        return scale<Math.max(MIN_AUTOMATIC_SCALE,settings.codeZoomMin/100)?null:{scale,x:bounds.left+12+(availableW-label-24)/2-(minX+maxX)/2*scale,y:(bounds.top+bounds.bottom)/2-(minY+maxY)/2*scale};};
      const ids=[selected];let next=null;
      for(const id of revealRequest.contextIds){if(id===selected||!positions.has(id)||!nodes.has(id)||!edges.some(e=>e.relation!=='containment'&&(e.from===selected&&e.to===id||e.to===selected&&e.from===id)))continue;const candidate=fit([...ids,id]);if(candidate){ids.push(id);next=candidate;}}
      if(next){contextChanged=Math.abs(next.x-camera.x)>1e-7||Math.abs(next.y-camera.y)>1e-7||Math.abs(next.scale-camera.scale)>1e-7;camera=next;}
    }
    const p=point(positions.get(selected)),labelWidth=measureLabel(Array.from(nodes.get(selected).label).slice(0,80).join(''),600).width;
    const shownWidth=Math.min(labelWidth,availableW-30),x=clamp(p.x,bounds.left+12,bounds.right-13-shownWidth),y=clamp(p.y,bounds.top+14,bounds.bottom-8);
    const dx=Math.abs(x-p.x)>1e-7?x-p.x:0,dy=Math.abs(y-p.y)>1e-7?y-p.y:0;
    const changed=contextChanged||dx!==0||dy!==0||revealStatus!=='revealed';camera.x+=dx;camera.y+=dy;revealStatus='revealed';return changed;
  }
  function moveCamera(target){
    if(canAnimate()&&settings.cameraMotion&&active){cameraTween={from:{...camera},to:{...target},start:now(),duration:settings.transitionMs};wake(settings.transitionMs);}else{cameraTween=null;camera=target;}
  }
  function radiusFor(id,n=nodes.get(id)){
    const count=settings.nodeSizeBasis==='connections'?degrees.get(id)||0:0;
    return settings.nodeRadius*(settings.nodeSizeBasis==='connections'?1+Math.min(2,Math.log2(1+count)*.3):n?.kind==='repository'?1.5:n?.kind==='folder'?1.3:1)+(id===selected?2:0);
  }
  function arrivalInView(position,viewCamera=camera){
    if(!position)return false;
    const bounds=revealRequest?revealBounds():{left:0,right:width,top:0,bottom:height};
    const x=position.x*viewCamera.scale+viewCamera.x,y=position.y*viewCamera.scale+viewCamera.y;
    return x>=bounds.left&&x<=bounds.right&&y>=bounds.top&&y<=bounds.bottom;
  }
  function startNodeArrivals() {
    if (canAnimate() && settings.layoutMotion && !doc?.hidden) {
      const duration=Math.min(settings.transitionMs,350);
      for (const id of pendingArrivals) nodeArrivals.set(id,{start:null,duration});
      if (pendingArrivals.size) wake(duration);
    }
    pendingArrivals.clear();
  }
  function wakeLayoutMotion() {
    if(layoutPaused)return;
    let remaining=0;
    const time=now();
    const extend=track=>{remaining=Math.max(remaining,track.start+track.duration-time);};
    if(positionTween?.arrivals){
      for(const track of positionTween.arrivals.values())extend(track);
    }else if(positionTween)extend(positionTween);
    for(const track of nodeArrivals.values())extend(track);
    if(nodeArrivals.size)remaining=Math.max(remaining,revealIntervalMs*2);
    if(remaining>0)wake(remaining);
  }
  function paintLayoutMotion(clock, settle) {
    if (!positionTween || (layoutPaused && !settle)) return;
    const {arrivals,to,from,start,duration} = positionTween;
    if (settle) {
      positions=to;positionTween=null;
    } else if (arrivals) {
      for (const [id,track] of arrivals) {
        if(track.start===null)continue;
        const fraction=clamp((clock-track.start)/track.duration,0,1);
        const point=to.get(id);
        const eased=1-(1-fraction)**track.damping;
        positions.set(id,fraction===1?point:{id,x:track.from.x+(point.x-track.from.x)*eased,y:track.from.y+(point.y-track.from.y)*eased});
        if (fraction===1) arrivals.delete(id);
      }
      if (!arrivals.size) positionTween=null;
    } else {
      const fraction=clamp((clock-start)/duration,0,1),eased=1-(1-fraction)**3;
      positions=new Map([...to].map(([id,p])=>{const a=from.get(id)||p;return [id,{id,x:a.x+(p.x-a.x)*eased,y:a.y+(p.y-a.y)*eased}];}));
      if (fraction===1) positionTween=null;
    }
  }
  function draw({batch,time=0,reduced=false}){
    const started=now();let witnessedEdges=0,firstRelation=null;
    const clock=time||started,ease=t=>1-Math.pow(1-t,3);for(const [id,e]of effects)if(clock-e.start>=e.duration)effects.delete(id);
    const noLayoutMotion=reduced||!canAnimate()||!settings.layoutMotion;
    // Pages share one reveal queue. A late frame admits one node even
    // when several pages arrived while the renderer was busy.
    if(!noLayoutMotion&&!layoutPaused&&clock-lastNodeReveal>=revealIntervalMs){
      for(const [id,track] of nodeArrivals){
        if(track.start!==null)continue;
        const movement=positionTween?.arrivals?.get(id);
        const target=settledGeometry().get(id);
        // A camera change can move a queued node away before its first frame.
        if(!arrivalInView(target)){
          nodeArrivals.delete(id);
          if(movement){positions.set(id,target);positionTween.arrivals.delete(id);}
          continue;
        }
        track.start=clock;
        lastNodeReveal=clock;
        if(movement)movement.start=clock;
        const duration=Math.max(track.duration,movement?.duration||0,revealIntervalMs*2);
        queueMicrotask(()=>{if(active&&!disposed&&!layoutPaused&&nodeArrivals.has(id))wake(duration);});
        break;
      }
    }
    paintLayoutMotion(clock,noLayoutMotion);
    if(noLayoutMotion)nodeArrivals.clear();
    const arrivalOpacity=new Map();
    for(const [id,track] of nodeArrivals){
      if(track.start===null){arrivalOpacity.set(id,0);continue;}
      const elapsed=(layoutPaused?layoutPausedAt:clock)-track.start;
      if(elapsed>=track.duration)nodeArrivals.delete(id);
      else arrivalOpacity.set(id,.12+.88*ease(clamp(elapsed/track.duration,0,1)));
    }
    const opacityFor=id=>!noLayoutMotion&&pendingArrivals.has(id)?0:(arrivalOpacity.get(id)??1);
    const scaleFor=id=>.5+.5*opacityFor(id);
    const paintedRadius=(id,node)=>radiusFor(id,node)*scaleFor(id);
    if(cameraTween){const t=reduced||!canAnimate()?1:clamp((clock-cameraTween.start)/cameraTween.duration,0,1),u=ease(t),a=cameraTween.from,b=cameraTween.to;camera={x:a.x+(b.x-a.x)*u,y:a.y+(b.y-a.y)*u,scale:a.scale+(b.scale-a.scale)*u};if(t>=1)cameraTween=null;}

    hitCells=new Map();labels=[];drawn={nodes:0,edges:0,labels:0};if(settings.background)batch.rect(0,0,width,height,palette.background,1);
    const exposed=revealRequest?revealBounds():{left:0,right:width,top:0,bottom:height};
    const visible=new Map();for(const [key,p] of positions){const n=nodes.get(key);if(!n||!opacityFor(key))continue;const q=point(p),radius=paintedRadius(key,n);if(q.x>=exposed.left+radius&&q.x<=exposed.right-radius&&q.y>=exposed.top+radius&&q.y<=exposed.bottom-radius)visible.set(key,{...q,node:n,radius});}
    const protectedNodes=new Set([selected,...pins.keys(),...observedActors]);
    if(pathRelations.size)for(const e of edges)if(pathRelations.has(relationKey(e))){protectedNodes.add(e.from);protectedNodes.add(e.to);}
    const grouped=new Set(), bins=[];
    if(settings.densityBins&&camera.scale*100<=settings.densityZoom&&visible.size>=50){
      const cells=new Map();
      for(const [id,p] of visible){
        if(protectedNodes.has(id)||['repository','folder'].includes(p.node.kind)||effects.has(id)||arrivalOpacity.has(id))continue;
        const key=`${Math.floor(p.x/36)},${Math.floor(p.y/36)}`;
        const cell=cells.get(key)||{x:0,y:0,ids:[]};
        cell.x+=p.x;cell.y+=p.y;cell.ids.push(id);cells.set(key,cell);
      }
      for(const cell of cells.values())if(cell.ids.length>=3){
        cell.ids.sort((a,b)=>(degrees.get(b)||0)-(degrees.get(a)||0)||a.localeCompare(b));
        for(const id of cell.ids)grouped.add(id);
        bins.push({id:cell.ids[0],x:cell.x/cell.ids.length,y:cell.y/cell.ids.length,count:cell.ids.length});
      }
    }
    const territories=new Map();
    for(const [id,p] of visible){
      const group=territoryByNode.get(id);
      if(!group||(presentation==='gravity'&&settings.grouping==='workspaces'))continue;
      const box=territories.get(group);
      if(box){box.left=Math.min(box.left,p.x);box.right=Math.max(box.right,p.x);box.top=Math.min(box.top,p.y);box.bottom=Math.max(box.bottom,p.y);}
      else territories.set(group,{left:p.x,right:p.x,top:p.y,bottom:p.y});
    }
    for(const [id,bounds] of territories){
      const left=clamp(bounds.left-14,0,width),top=clamp(bounds.top-16,0,height);
      const right=clamp(bounds.right+14,0,width),bottom=clamp(bounds.bottom+14,0,height);
      if(right>left&&bottom>top){
        const arrival=opacityFor(id);
        batch.rect(left,top,right-left,bottom-top,palette.group,(id===selected?.13:.055)*arrival);
        const label=nodes.get(id)?.label;
        if(label&&right-left>90&&bottom-top>35&&settings.labels!=='off'){
          const title=labelFor(label,right-left-12,600);
          if(title.text)batch.glyph(title.text,left+6+title.left,top+13,11,600,palette.group,.82*arrival,'left','alphabetic','sans-serif');
        }
      }
    }
    for(const edge of edges){
      const arrival=Math.min(opacityFor(edge.from),opacityFor(edge.to));
      if(!arrival)continue;
      const a=positions.get(edge.from),b=positions.get(edge.to);if(!a||!b)continue;const p=point(a),q=point(b);
      if(Math.max(p.x,q.x)<0||Math.min(p.x,q.x)>width||Math.max(p.y,q.y)<0||Math.min(p.y,q.y)>height)continue;
      const path=pathRelations.size>0&&pathRelations.has(relationKey(edge));
      if(!path&&selected!==edge.from&&selected!==edge.to&&(grouped.has(edge.from)||grouped.has(edge.to)))continue;
      const color=path?palette.selected:edge.relation==='containment'?palette.containment:edge.relation==='import'?palette.import:edge.state==='inferred'?palette.inferred:palette.exact;
      const emphasized=path || !settings.selectedEmphasis || !selected || edge.from===selected || edge.to===selected;
      const opacity=(path?1:settings.edgeOpacity*(emphasized?.95:.25))*arrival;
      const edgeWidth=path?settings.edgeWidth+1.25:settings.edgeWidth;
      if(edge.state==='inferred'){for(let i=0;i<3;i++){const a=i/3,b=(i+.55)/3;batch.line(p.x+(q.x-p.x)*a,p.y+(q.y-p.y)*a,p.x+(q.x-p.x)*b,p.y+(q.y-p.y)*b,color,edgeWidth,opacity);}}
      else batch.line(p.x,p.y,q.x,q.y,color,edgeWidth,opacity);
      // Screen-space arrowheads preserve the actual from -> to direction.
      // Very short projected edges cannot fit a distinct arrow and node rim.
      const length=Math.hypot(q.x-p.x,q.y-p.y);
      if(settings.arrows&&length>24){const dx=(q.x-p.x)/length,dy=(q.y-p.y)/length,radius=paintedRadius(edge.to)+2;
        const x=q.x-dx*radius,y=q.y-dy*radius;
        const arrowOpacity=path?opacity:Math.min(.9*arrival,opacity+.15*arrival);
        batch.line(x,y,x-dx*7-dy*3.5,y-dy*7+dx*3.5,color,1.5,arrowOpacity);
        batch.line(x,y,x-dx*7+dy*3.5,y-dy*7-dx*3.5,color,1.5,arrowOpacity);
      }
      drawn.edges++;
      if(edge.witnessed&&opacity>0&&edgeWidth>0&&visible.has(edge.from)&&visible.has(edge.to)){
        witnessedEdges++;
        firstRelation??=edge.relation;
      }
    }
    if(settings.activityTrail&&canAnimate()&&(consumer?.backendKind!=='canvas2d'||visible.size<=600)){
      for(let index=1;index<activityTrail.length;index++){
        const a=visible.get(activityTrail[index-1]),b=visible.get(activityTrail[index]);
        if(a&&b)batch.line(a.x,a.y,b.x,b.y,palette.exact,2,.32*Math.min(opacityFor(activityTrail[index-1]),opacityFor(activityTrail[index])));
      }
    }
    for(const bin of bins){
      batch.circle(bin.x,bin.y,12,palette.group,.88);
      batch.glyph(String(bin.count),bin.x,bin.y+3,9,600,palette.background,1,'center','alphabetic','sans-serif');
      const key=`${Math.floor(bin.x/32)},${Math.floor(bin.y/32)}`;
      if(!hitCells.has(key))hitCells.set(key,[]);
      hitCells.get(key).push({id:bin.id,x:bin.x,y:bin.y,radius:13});
    }
    for(const [key,p] of visible){if(grouped.has(key))continue;const {radius}=p,arrival=opacityFor(key);
      const color=key===selected?palette.selected:p.node.kind==='symbol'?palette.exact:p.node.kind==='file'?palette.node:palette.group;
      const opacity=(dimUnvisited&&p.node.kind==='file'&&!visited.has(key)&&key!==selected?.35:1)*arrival;
      if(pins.has(key))batch.circle(p.x,p.y,radius+3,palette.group,.65*arrival);
      const watching=observedActors.reduce((count,id)=>count+Number(id===key),0);
      if(watching)batch.circle(p.x,p.y,radius+3+Math.min(4,watching),palette.import,.55*arrival);
      if(key===selected&&settings.nodeGlow)batch.circle(p.x,p.y,radius+5,palette.selected,.22*arrival);
      const isType=p.node.kind==='symbol'&&['class','interface','type'].includes(p.node.symbol_kind);
      const square=isType||p.node.kind==='file'&&settings.fileNodeShape==='square';
      if(square)batch.rect(p.x-radius,p.y-radius,radius*2,radius*2,color,opacity);
      else {
        batch.circle(p.x,p.y,radius,color,opacity);
        if(p.node.kind==='file'&&settings.fileNodeShape==='ring')batch.circle(p.x,p.y,Math.max(1,radius-2.5),palette.background,.9*arrival);
      }
      const effect=effects.get(key);
      if(effect&&settings.nodeShimmer&&(consumer?.backendKind!=='canvas2d'||visible.size<=600)){const age=clock-effect.start;if(age>=effect.duration)effects.delete(key);else{batch.circle(p.x,p.y,radius+4+(reduced?0:age/effect.duration*12),effect.kind==='agent_read'?palette.exact:palette.selected,Math.max(.1,1-age/effect.duration)*arrival);}}

      const cell=`${Math.floor(p.x/32)},${Math.floor(p.y/32)}`;if(!hitCells.has(cell))hitCells.set(cell,[]);hitCells.get(cell).push({id:key,x:p.x,y:p.y,radius:Math.max(9*scaleFor(key),radius),square});drawn.nodes++;
    }
    const kindRank={repository:0,folder:1,file:2,symbol:3};
    const hitsGlyph=(x,y,w)=>{const top=y-12,bottom=y+4;for(let cx=Math.floor((x-12)/32);cx<=Math.floor((x+w+12)/32);cx++)for(let cy=Math.floor((top-12)/32);cy<=Math.floor((bottom+12)/32);cy++)for(const p of hitCells.get(`${cx},${cy}`)||[]){const nearX=clamp(p.x,x,x+w),nearY=clamp(p.y,top,bottom);if(Math.hypot(p.x-nearX,p.y-nearY)<=p.radius+2)return true;}return false;};
    const priority=[...visible.entries()].sort(([a,p],[b,q])=>(b===selected)-(a===selected)||kindRank[p.node.kind]-kindRank[q.node.kind]||a.localeCompare(b,'en'));
    for(const [key,p] of priority){if(settings.labels==='off')break;if(grouped.has(key))continue;if(settings.labels==='selected'&&key!==selected)continue;if(settings.labels==='automatic'&&labels.length>=Math.max(8,Math.round(settings.labelDensity*clamp(camera.scale,.25,1)))&&key!==selected)continue;
      const bounded=exposed;
      if(width<40||height<28)continue;
      const fontSize=settings.labelSize,weight=key===selected?600:400,label=labelFor(p.node.label,bounded.right-bounded.left-(key===selected&&revealStatus==='revealed'?30:4),weight),labelWidth=label.width;
      if(!label.text)continue;
      const xs=[p.x+12,p.x-12-labelWidth].map(x=>clamp(x,bounded.left+2,Math.max(bounded.left+2,bounded.right-labelWidth-2))),baseY=clamp(p.y+5,bounded.top+14,Math.max(bounded.top+14,bounded.bottom-3));
      // Fixed screen-space font; bounded callout slots avoid shrinking an
      // otherwise useful coarse hierarchy into unreadable or missing labels.
      let x=null,y=null;for(const offset of [0,20,-20,40,-40,60,-60,80,-80]){const candidate=clamp(baseY+offset,bounded.top+14,Math.max(bounded.top+14,bounded.bottom-3));for(const candidateX of xs)if(!hitsGlyph(candidateX,candidate,labelWidth)&&!labels.some(l=>candidateX<l.x+l.width+5&&candidateX+labelWidth+5>l.x&&candidate-12<l.y+4&&candidate+4>l.y-12)){x=candidateX;y=candidate;break;}if(y!==null)break;}
      if(y===null)continue;
      const arrival=opacityFor(key);
      if(Math.abs(y-baseY)>=14)batch.line(p.x,p.y,x,y-5,palette.containment,.7,.45*arrival);
      batch.glyph(label.text,x+label.left,y,fontSize,weight,palette.label,arrival,'left','alphabetic','sans-serif');labels.push({id:key,x,y,width:labelWidth,fontSize,text:label.text,truncated:label.truncated});
    }
    drawn.labels=labels.length;
    const group=n=>['repository','folder'].includes(n.kind);density={mode:bins.length?'binned':drawn.nodes<nodes.size?'overflow':'fit',totalNodes:nodes.size,visibleNodes:drawn.nodes,offscreenNodes:nodes.size-visible.size,bins:bins.length,groupedNodes:grouped.size,totalGroups:[...nodes.values()].filter(group).length,visibleGroups:[...visible.values()].filter(p=>group(p.node)).length,labeledGroups:labels.filter(l=>group(nodes.get(l.id))).length};
    lastDraw={serial,namespace,sceneCpuMs:Math.max(0,now()-started),witnessedEdges,firstRelation};
    const moving=(!layoutPaused&&(nodeArrivals.size||positionTween))||cameraTween||effects.size;
    if(!paintNotificationPending&&(!moving||clock-lastPaintNoticeAt>=250||witnessedEdges>0&&witnessedNamespace!==namespace)){
      paintNotificationPending=true;queueMicrotask(()=>{
      paintNotificationPending=false;
      // Runs after the consumer's synchronous batch flush/endFrame, never
      // invalidates the owner or queues another animation frame.
      if(disposed||lastDraw?.serial!==serial||lastDraw.namespace!==namespace)return;
      lastPaintNoticeAt=clock;
      if(lastDraw.witnessedEdges>0&&lastAcknowledgedPaint?.serial===serial)witnessedNamespace=namespace;
      const description=density.mode==='binned'?`${density.groupedNodes} items grouped into ${density.bins} bins. Zoom in or open Graph table for individual items.`:density.mode==='overflow'?`${density.visibleNodes} of ${density.totalNodes} nodes visible; pan or use Explore for the rest.`:'All current map nodes fit in this viewport.';
      if(description!==canvasDescription){
        canvasDescription=description;
        consumer?.canvas?.setAttribute?.('aria-description',description);
      }
      notify();
    });}
  }
  function acknowledgePaint({backend}){
    if(disposed||!lastDraw||lastDraw.serial!==serial||lastDraw.namespace!==namespace)return;
    lastAcknowledgedPaint={...lastDraw,backend,sequence:++paintSequence,acknowledgedAt:now()};
  }
  function local(event){const r=consumer.canvas.getBoundingClientRect();return {x:(event.clientX-r.left)*width/Math.max(1,r.width),y:(event.clientY-r.top)*height/Math.max(1,r.height)};}
  function pick(x,y){
    let best=null,distance=Infinity;
    const cx=Math.floor(x/32),cy=Math.floor(y/32),reach=Math.max(1,Math.ceil((settings.nodeRadius*3+2)/32));
    for(let a=-reach;a<=reach;a++)for(let b=-reach;b<=reach;b++)for(const p of hitCells.get(`${cx+a},${cy+b}`)||[]){
      if(!nodes.has(p.id))continue;
      const dx=Math.abs(p.x-x),dy=Math.abs(p.y-y),d=Math.hypot(dx,dy);
      if((p.square?Math.max(dx,dy):d)<=p.radius&&d<distance){best=p.id;distance=d;}
    }
    return best;
  }
  function attach(next,previous=null){
    canvasDescription='';
    lastPaintNoticeAt=-Infinity;
    detach();if(next.style)next.style.pointerEvents='auto';next.removeAttribute?.('aria-hidden');next.setAttribute?.('role','img');next.setAttribute?.('aria-label','Code repository map; arrow keys pan, plus and minus zoom; use companion explorer for source actions');next.tabIndex=0;
    const listeners=[];const listen=(type,fn,options)=>{next.addEventListener?.(type,fn,options);listeners.push(()=>next.removeEventListener?.(type,fn,options));};
    const pair=()=>{const [a,b]=[...pointers.values()];return {distance:Math.max(1,Math.hypot(b.x-a.x,b.y-a.y)),mid:{x:(a.x+b.x)/2,y:(a.y+b.y)/2}};};
    listen('pointerdown',event=>{if(event.button!==0||pointers.size>=2||pointers.has(event.pointerId))return;const p=local(event);pointers.set(event.pointerId,p);next.setPointerCapture?.(event.pointerId);
      if(pointers.size===2){pinch={...pair(),camera:{...camera}};drag=null;}else drag={id:event.pointerId,start:p,last:p,moved:false};});
    listen('pointermove',event=>{if(!pointers.has(event.pointerId))return;const p=local(event);pointers.set(event.pointerId,p);
      if(pointers.size===2&&pinch){controlCamera();const current=pair(),scale=zoomScale(pinch.camera.scale*current.distance/pinch.distance),wx=(pinch.mid.x-pinch.camera.x)/pinch.camera.scale,wy=(pinch.mid.y-pinch.camera.y)/pinch.camera.scale;camera={x:current.mid.x-wx*scale,y:current.mid.y-wy*scale,scale};consumer.invalidate();return;}
      if(!drag||drag.id!==event.pointerId)return;if(Math.hypot(p.x-drag.start.x,p.y-drag.start.y)>4)drag.moved=true;if(drag.moved){controlCamera();camera.x+=p.x-drag.last.x;camera.y+=p.y-drag.last.y;consumer.invalidate();}drag.last=p;});
    const endPointer=(event,cancelled=false)=>{if(!pointers.has(event.pointerId))return;const wasPinch=pointers.size===2,p=local(event),click=!cancelled&&!event.defaultPrevented&&!wasPinch&&drag?.id===event.pointerId&&!drag.moved;pointers.delete(event.pointerId);pinch=null;drag=null;if(next.hasPointerCapture?.(event.pointerId))next.releasePointerCapture?.(event.pointerId);
      if(pointers.size===1){const [id,point]=[...pointers.entries()][0];drag={id,start:point,last:point,moved:true};}if(click)select(pick(p.x,p.y),{notify:true});};
    listen('pointerup',event=>endPointer(event));
    listen('pointercancel',event=>endPointer(event,true));
    listen('lostpointercapture',event=>endPointer(event,true));
    listen('wheel',event=>{event.preventDefault();const p=local(event);zoom(Math.exp(-clamp(event.deltaY,-200,200)*.002*settings.zoomSensitivity),p);},{passive:false});
    listen('keydown',event=>{if(['ArrowLeft','ArrowRight','ArrowUp','ArrowDown'].includes(event.key)){event.preventDefault();controlCamera();camera.x+=event.key==='ArrowLeft'?32:event.key==='ArrowRight'?-32:0;camera.y+=event.key==='ArrowUp'?32:event.key==='ArrowDown'?-32:0;consumer.invalidate();}else if(event.key==='+'||event.key==='='){event.preventDefault();zoom(1.2);}else if(event.key==='-'){event.preventDefault();zoom(1/1.2);}else if(event.key==='0'){event.preventDefault();fit();}else if(event.key==='Escape')select(null,{notify:true});});
    detach=()=>{for(const remove of listeners)remove();resetPointers(next);};onCanvasChange(next,previous);
  }
  function applyGravityArrivals(from, next) {
    const previous = settledGeometry();
    const arrivals = new Map();
    const existing = positionTween?.arrivals;
    const drawn = new Map(next);
    for (const [id,target] of next) {
      const track = existing?.get(id), previousTarget = previous.get(id);
      if (track && previousTarget?.x===target.x && previousTarget?.y===target.y) {
        arrivals.set(id,track);
        drawn.set(id,positions.get(id)||track.from);
      } else if (pendingArrivals.has(id) && from.has(id)) {
        const origin = from.get(id);
        // A later page joins the motion without restarting an earlier page.
        arrivals.set(id,{from:origin,start:null,duration:settings.transitionMs,damping:2+2*settings.damping});
        drawn.set(id,origin);
      }
    }
    positions=drawn;
    positionTween=arrivals.size ? {to:next,arrivals} : null;

  }
  function validateLayoutPoints(points, allowedCount) {
    need(Array.isArray(points) && points.length<=allowedCount);
    const result = new Map();
    for (const {id,x,y} of points) {
      need(nodes.has(id) && !result.has(id) && Number.isFinite(x) && Number.isFinite(y) && Math.abs(x)<=1e7 && Math.abs(y)<=1e7);
      result.set(id,{id,x,y});
    }
    return result;
  }
  let consumer=null;
  const layout=createCodeMapLayoutOwner({workerFactory,changed:(result,mode)=>{
    if(disposed||!active||layoutPaused)return;
    layoutMode=mode;
    // Manual takeover keeps the already visible world. The resize job owns
    // only a proposed repack, so its reply can be discarded without a new job.
    if (responsivePending && cameraMode==='user' && positions.size===nodes.size && nodes.size) {
      responsivePending=false;status='ready';consumer?.invalidate();notify();return;
    }
    if(!result){layoutRelayout=false;status=mode==='worker-required'?'layout-worker-required':'layout-error';consumer?.invalidate();notify();return;}
    if(result.serial!==serial||result.namespace!==namespace||result.presentation!==presentation)return;
    layoutRelayout=false;
    try {
      need(result.positions?.length===nodes.size && size(result)<=MAX_BYTES);
      const next = validateLayoutPoints(result.positions,nodes.size);
      const incremental = presentation==='gravity' && result.arrivals!==undefined;
      const origins = incremental ? validateLayoutPoints(result.arrivals,nodes.size) : null;
      const arrivalCamera=needsFit?fittedCamera(next):camera;
      for(const id of pendingArrivals)if(!arrivalInView(next.get(id),arrivalCamera))pendingArrivals.delete(id);
      if (canAnimate() && settings.layoutMotion && positions.size) {
        if (incremental) applyGravityArrivals(origins,next);
        else {
          positionTween={from:new Map(positions),to:next,start:now(),duration:settings.transitionMs};
          positions=new Map([...next].map(([id,p])=>[id,positions.get(id)||p]));
          wake(settings.transitionMs);
        }
      } else { positionTween=null;positions=next; }
      startNodeArrivals();
      responsivePending=false;
      if(needsFit){camera=fittedCamera();needsFit=false;fittedReset=true;}
      status='ready';
      if (incremental && cameraMode==='user' && revealRequest) revealStatus='user-camera';
      else applyReveal();
      consumer?.invalidate();notify();
    } catch {
      status='layout-error';positionTween=null;positions.clear();nodeArrivals.clear();consumer?.invalidate();notify();
    }
  }});
  const presentationPolicy=graphicalPresentationPolicy(),surfaceBudget=graphicalSurfaceBudget();
  let contextLease=null;
  function mountConsumer(initialCanvas){
    const ordinary=forceCanvas2D||presentationPolicy.mode==='ordinary';
    requestedBackend=ordinary?'ordinary':'auto';
    if(!ordinary)contextLease=surfaceBudget.acquire(`map:${id}`);
    acquisitionReason=ordinary?'ordinary-selected':contextLease?null:'webgl2-lease-unavailable';
    consumer=consumerFactory({mount,canvas:initialCanvas,id,win,doc,ownerHost,renderMode:'demand',forceCanvas2D:ordinary||!contextLease,draw,onFrameComplete:acknowledgePaint,onFrameError:()=>{lastDraw=null;},
      onCanvasChange:(next,previous)=>{attach(next,previous);queueMicrotask(()=>{
        if(!disposed&&consumer?.canvas===next&&consumer.backendKind!=='webgl2'){contextLease?.release();contextLease=null;notify();}
      });}
    });allocationDpr=null;attach(consumer.canvas);
    if(consumer.backendKind!=='webgl2'){contextLease?.release();contextLease=null;}
  }
  mountConsumer(canvas);
  const unsubscribePresentation=presentationPolicy.subscribe(()=>{
    if(disposed)return;
    detach();consumer.dispose();contextLease?.release();contextLease=null;
    mountConsumer(null);if(!measureMount())resize(width,height,readDpr());if(!active)consumer.suspend();else{wakeLayoutMotion();consumer.invalidate();}
  });
  function submitLayout(preserve, relayout=false, responsive=false){
    if(presentation==='gravity' && relayout) layoutRelayout=true;
    if(!active||layoutPaused){layoutDeferred=true;return;}layoutDeferred=false;
    serial++;status='layout-pending';responsivePending=responsive;
    const seeds=new Map(preserve ? (presentation==='gravity'&&!layoutRelayout ? settledGeometry() : positions) : []);
    for(const [id,point] of pins)if(nodes.has(id))seeds.set(id,point);
    const job={serial,namespace,presentation,nodes:[...nodes.values()].map(({id,parent,kind})=>({id,parent,kind})),
      edges:edges.map(({from,to,relation})=>({from,to,relation})),settings,
      previous:[...seeds.values()].filter(p=>nodes.has(p.id)),pinned:[...pins.keys()].filter(id=>nodes.has(id)),anchor:selected,viewport:{width,height}};
    if (presentation==='gravity' && layoutRelayout) job.relayout=true;
    try{layout.submit(job);}catch{layoutRelayout=false;status='layout-error';notify();}
  }
  function resize(w,h,dpr=win.devicePixelRatio||1){
    if(disposed)return;need(Number.isFinite(w)&&Number.isFinite(h)&&w>0&&h>0);const oldWidth=width,oldHeight=height;width=clamp(w,1,8192);height=clamp(h,1,8192);const ratio=Math.min(settings.dpr,Math.max(.5,dpr)),geometryChanged=width!==oldWidth||height!==oldHeight;
    if(!geometryChanged&&allocationDpr===ratio)return;
    const allocation=consumer.resize(width,height,ratio);allocationDpr=ratio;effectiveDpr=allocation?{x:allocation.width/width,y:allocation.height/height}:ratio;
    // Pixel-density changes are allocation only: no camera/layout/reveal reset.
    if(!geometryChanged)return;
    if(settings.preserveCamera&&positions.size&&!needsFit){
      camera.x+=(width-oldWidth)/2;camera.y+=(height-oldHeight)/2;
    }else if(presentation!=='gravity'&&nodes.size&&cameraMode==='default'&&!seedProtected){needsFit=true;submitLayout(false,false,true);}
    else if(positions.size&&(needsFit||cameraMode==='default')){camera=fittedCamera();needsFit=false;}
    else if(cameraMode==='user'){camera.x+=(width-oldWidth)/2;camera.y+=(height-oldHeight)/2;}
    applyReveal();consumer.invalidate();
  }
  function fit(){if(disposed)return;if(cameraMode==='follow')onCameraTakeover();cameraMode='default';revealRequest=null;revealStatus=null;moveCamera(fittedCamera());needsFit=status!=='ready';fittedReset=true;consumer.invalidate();notify();}
  function zoom(factor,p={x:width/2,y:height/2}){if(disposed)return;need(Number.isFinite(factor)&&factor>0&&Number.isFinite(p.x)&&Number.isFinite(p.y));controlCamera();const scale=zoomScale(camera.scale*factor),ratio=scale/camera.scale;camera={x:p.x-(p.x-camera.x)*ratio,y:p.y-(p.y-camera.y)*ratio,scale};consumer.invalidate();}
  function select(id,{notify:notifySelection=false}={}){if(disposed||id!==null&&!nodes.has(id))return false;if(selected!==id){revealRequest=null;revealStatus=null;if(id&&settings.selectionMotion&&canAnimate()){effects.set(id,{start:now(),duration:settings.transitionMs,kind:'selection'});while(effects.size>settings.activityCount)effects.delete(effects.keys().next().value);wake(settings.transitionMs);}}selected=id;if(id!==null){seedProtected=true;if(responsivePending&&positions.size){needsFit=false;submitLayout(true);}}consumer.invalidate();if(notifySelection)onSelect(id===null?null:{id,authorityNamespace:namespace});notify();return true;}
  function revealSelected({insets={},contextIds=[]}={}){if(disposed||selected===null)return false;need(insets&&Object.keys(insets).every(k=>['left','right','top','bottom'].includes(k)));need(Array.isArray(contextIds)&&contextIds.length<=32&&contextIds.every(id=>typeof id==='string'&&id.length>0&&id.length<=256)&&new Set(contextIds).size===contextIds.length);const normalized={left:0,right:0,top:0,bottom:0,...insets};for(const v of Object.values(normalized))need(Number.isFinite(v)&&v>=0&&v<=8192);const request={id:selected,namespace,insets:normalized,contextIds:[...contextIds]},changedRequest=codeMapKey(revealRequest)!==codeMapKey(request);revealRequest=request;const before={...camera},changed=applyReveal();if(changed&&canAnimate()&&settings.cameraMotion){const target={...camera};camera=before;moveCamera(target);}if(changedRequest||changed)consumer.invalidate();return true;}
  function clear(){if(disposed)return;pins.clear();pathRelations.clear();observedActors=[];layoutRelayout=false;positionTween=null;cameraTween=null;effects.clear();pendingArrivals.clear();nodeArrivals.clear();consumer?.owner?.finishAnimation();presentationCache.clear();namespace=null;serial++;layout.invalidate();positions.clear();nodes.clear();territoryByNode.clear();visited.clear();dimUnvisited=false;activityTrail=[];edges=[];degrees.clear();hitCells.clear();textMetrics.clear();resetPointers(consumer.canvas);selected=null;needsFit=true;seedProtected=false;cameraMode='default';revealRequest=null;revealStatus=null;density={mode:'pending',totalNodes:0,visibleNodes:0,offscreenNodes:0,totalGroups:0,visibleGroups:0,labeledGroups:0};status='idle';consumer.invalidate();notify();}
  function update(value,{saved=[],pinned=[],discovered=[]}={}){if(disposed)return false;
    if(!value?.authority||!['ready','loading'].includes(value.status)){clear();return false;}
    need(['structure_preview','published'].includes(value.authority.variant)&&Array.isArray(value.nodes)&&value.nodes.length<=CODE_GRAPH_DISPLAY.nodes&&Array.isArray(value.edges)&&value.edges.length<=CODE_GRAPH_DISPLAY.edges&&size(value)<=MAX_BYTES);
    const nextNamespace=codeMapKey(value.authority),nextNodes=new Map();for(const n of value.nodes){need(typeof n.id==='string'&&!nextNodes.has(n.id)&&typeof n.label==='string'&&Array.from(n.label).length<=160&&['repository','folder','file','symbol'].includes(n.kind));nextNodes.set(n.id,{id:n.id,label:n.label,kind:n.kind,parent:n.parent,symbol_kind:n.symbol_kind});}
    const nextEdges=value.edges.map(e=>{need(nextNodes.has(e.from)&&nextNodes.has(e.to)&&MAP_RELATIONS.includes(e.relation)&&['structure','exact','inferred'].includes(e.state));return {from:e.from,to:e.to,relation:e.relation,state:e.state,witnessed:e.witnessed===true&&e.relation!=='containment'&&e.state==='exact'};});
    if(nextNamespace!==namespace){
      layoutRelayout=false;cameraTween=null;effects.clear();visited.clear();pendingArrivals.clear();
      if(!value.retainPresentation){
        positionTween=null;positions.clear();nodeArrivals.clear();presentationCache.clear();cameraMode='default';
      }else if(!positionTween?.arrivals){
        positions=settledGeometry();positionTween=null;
      }
      hitCells.clear();textMetrics.clear();resetPointers(consumer.canvas);selected=null;needsFit=!value.retainPresentation;seedProtected=value.retainPresentation===true;revealRequest=null;revealStatus=null;fittedReset=false;layout.invalidate();
    }else if(positions.size)seedProtected=true;
    for(const id of positionTween?.to.keys()||[])if(!nextNodes.has(id)){
      positionTween.to.delete(id);
      positionTween.arrivals?.delete(id);
    }
    for(const id of pendingArrivals)if(!nextNodes.has(id))pendingArrivals.delete(id);
    for(const id of nodeArrivals.keys())if(!nextNodes.has(id))nodeArrivals.delete(id);
    namespace=nextNamespace;nodes=nextNodes;edges=nextEdges;updateTerritories();degrees.clear();for(const e of edges){degrees.set(e.from,(degrees.get(e.from)||0)+1);degrees.set(e.to,(degrees.get(e.to)||0)+1);}for(const id of positions.keys())if(!nodes.has(id))positions.delete(id);for(const cache of presentationCache.values())cache.positions=cache.positions.filter(p=>nodes.has(p.id));if(selected!==null&&!nodes.has(selected))selected=null;
    for(const point of saved)if(nodes.has(point.id)&&!positions.has(point.id)&&[point.x,point.y].every(n=>Number.isFinite(n)&&Math.abs(n)<=1e7)){
      const position={id:point.id,x:point.x,y:point.y};
      positions.set(point.id,position);
      positionTween?.to.set(point.id,position);
    }
    setPins(pinned,false);
    if(active&&!doc?.hidden&&namespace!==null){
      for(const id of discovered)if(nodes.has(id)&&nodes.get(id).kind!=='repository'&&!positions.has(id)&&!pins.has(id))pendingArrivals.add(id);
    }
    submitLayout(true);consumer.invalidate();return true;
  }
  function setPins(points, relayout=true){
    pins=new Map(points.filter(point=>nodes.has(point.id)&&[point.x,point.y].every(n=>Number.isFinite(n)&&Math.abs(n)<=1e7))
      .slice(0,64).map(({id,x,y})=>[id,{id,x,y}]));
    if(relayout&&namespace)submitLayout(true,true);
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
  function rememberLayout(key){
    presentationCache.delete(key);
    presentationCache.set(key,{positions:[...(positionTween?.to||positions).values()].map(p=>({...p})),camera:{...camera},
      cameraMode:cameraMode==='follow'?'user':cameraMode});
    while(presentationCache.size>12)presentationCache.delete(presentationCache.keys().next().value);
  }
  function restoreLayout(key){
    const cached=presentationCache.get(key);
    positionTween=null;cameraTween=null;nodeArrivals.clear();pendingArrivals.clear();
    positions=new Map((cached?.positions||[]).filter(p=>nodes.has(p.id)).map(p=>[p.id,{...p}]));
    if(cached){camera={...cached.camera};cameraMode=cached.cameraMode;needsFit=!positions.size;}
    else{cameraMode='default';needsFit=true;}
    seedProtected=!!positions.size;
    revealRequest=null;revealStatus=null;hitCells.clear();updateTerritories();
  }
  const api={
    setSettings(value,mode=presentation){
      if(disposed||!['map','web','gravity'].includes(mode))return;
      const previous=settings,oldKey=layoutKey(),next=resolveGraphSettings({common:value}),newKey=layoutKey(mode,next);
      if(oldKey!==newKey)rememberLayout(oldKey);
      settings=next;presentation=mode;
      if(oldKey!==newKey){layoutRelayout=false;restoreLayout(newKey);}
      camera=constrainCamera(camera);
      if(cameraTween){
        cameraTween.from=constrainCamera(cameraTween.from);
        cameraTween.to=constrainCamera(cameraTween.to);
      }
      textMetrics.clear();for(const [id,e]of effects)if(e.kind==='activity'&&!settings.activity||e.kind==='agent_read'&&!settings.agentActivity||e.kind==='selection'&&!settings.selectionMotion)effects.delete(id);if(effects.size||positionTween||cameraTween||nodeArrivals.size)wake(0);else consumer.owner?.finishAnimation();
      if(settings.motion!=='system'){if(positionTween)positions=positionTween.to;positionTween=null;if(cameraTween)camera=cameraTween.to;cameraTween=null;effects.clear();consumer.owner?.animate(0,{reduced:true});}
      const layoutKeys=['mapSpacing','nodeRadius','nodeSizeBasis','grouping','groupDepth','clusterSpacing','linkDistance','linkStrength','containmentStrength','repulsion','gravity','damping','collisionPadding','iterations','labelSize'];
      if(namespace&&(oldKey!==newKey||layoutKeys.some(k=>settings[k]!==previous[k])))submitLayout(oldKey!==newKey||presentation!=='map'||settings.mapSpacing===previous.mapSpacing, oldKey===newKey);
      resize(width,height,resolutionDpr||win.devicePixelRatio||1);consumer.invalidate();},
    setLayoutPaused(value){
      const paused=value===true;
      if (paused===layoutPaused) return;
      layoutPaused=paused;
      if (paused) {
        layoutPausedAt=now();
        serial++;layout.invalidate();
        layoutDeferred ||= status==='layout-pending';
        consumer.owner?.finishAnimation();
      } else {
        const elapsed=now()-layoutPausedAt;
        if (positionTween?.arrivals) {
          for (const track of positionTween.arrivals.values()) if(track.start!==null)track.start+=elapsed;
        } else if (positionTween) positionTween.start+=elapsed;
        for(const track of nodeArrivals.values())if(track.start!==null)track.start+=elapsed;
        wakeLayoutMotion();
        if (layoutDeferred&&active&&namespace) submitLayout(true);
      }
    },
    relayout(){if(namespace&&active)submitLayout(true,true);},
    activity(ids,{agentRead=false}={}){if(!settings.nodeShimmer||(agentRead?!settings.agentActivity:!settings.activity)||!active||doc?.hidden||(agentRead&&(settings.motion!=='system'||consumer?.owner?.reducedMotion===true)))return;for(const id of ids)if(nodes.has(id)){effects.delete(id);effects.set(id,{start:now(),duration:settings.activityMs,kind:agentRead?'agent_read':'activity'});while(effects.size>settings.activityCount)effects.delete(effects.keys().next().value);}wake(settings.activityMs);consumer.invalidate();},
    clearAgentActivity(){for(const [id,e] of effects)if(e.kind==='agent_read')effects.delete(id);consumer.invalidate();},
    positions:()=>[...positions.values()].map(p=>({...p})),
    settledPositions:()=>status==='ready'?[...(positionTween?.to||positions).values()].map(p=>({...p})):null,
    remapPositions(mapID){
      const remap=values=>values.flatMap(p=>{const id=mapID(p.id);return id?[{...p,id}]:[];});
      const remapGeometry=geometry=>new Map(remap([...geometry.values()]).map(p=>[p.id,p]));
      if(positionTween?.arrivals){
        const arrivals=new Map();
        for(const [id,track] of positionTween.arrivals){
          const next=mapID(id);
          if(next)arrivals.set(next,{...track,from:{...track.from,id:next}});
        }
        const to=remapGeometry(positionTween.to);
        positionTween=arrivals.size?{to,arrivals}:null;
        positions=positionTween?remapGeometry(positions):to;
      }else{
        positions=remapGeometry(settledGeometry());
        positionTween=null;
      }
      const remappedArrivals=[...nodeArrivals].map(([id,track])=>[mapID(id),track]);
      nodeArrivals.clear();
      for(const [id,track] of remappedArrivals)if(id)nodeArrivals.set(id,track);
      seedProtected=true;
      for(const cache of presentationCache.values())cache.positions=remap(cache.positions);
    },
    seedPositions:seeds=>{positions=new Map(seeds.map(p=>[p.id,{...p}]));seedProtected=true;},
    update,clear,select,revealSelected,fit,resize,snapshot,pick,zoom,setPins,
    pickClient(event){
      if(disposed || drag?.moved || pinch || event.target!==consumer.canvas || !Number.isFinite(event.clientX) || !Number.isFinite(event.clientY))return null;
      const point=local(event),id=pick(point.x,point.y);
      return nodes.has(id)?{id,authorityNamespace:namespace}:null;
    },
    setObservedActors(rows){
      if(rows.length===observedActors.length&&rows.every((id,index)=>id===observedActors[index]))return;
      observedActors=rows.slice(0,12);consumer.invalidate();
    },
    setPath(rows){
      pathRelations=new Set(rows.map(relationKey));
      consumer.invalidate();
    },
    setPlacement(value){
      const next=value==='docked'?'docked':'expanded';
      if(next===placement)return;
      placement=next;
      if(positionTween||cameraTween||effects.size||nodeArrivals.size)wake(0);
      notify();
    },
    setPresentation(value){if(disposed||!['map','web','gravity'].includes(value)||value===presentation)return false;api.setSettings(settings,value);return true;},
    setActivityCoverage(ids,dim){
      const next=new Set(ids.filter(id=>nodes.has(id))),nextDim=dim===true&&next.size>0;
      if(nextDim===dimUnvisited&&next.size===visited.size&&[...next].every(id=>visited.has(id)))return;
      visited.clear();for(const id of next)visited.add(id);dimUnvisited=nextDim;consumer.invalidate();
    },
    setActivityTrail(ids){
      const next=ids.filter(id=>nodes.has(id)).slice(-3);
      if(next.length===activityTrail.length&&next.every((id,index)=>id===activityTrail[index]))return;
      activityTrail=next;consumer.invalidate();
    },
    followNode(id){const p=positions.get(id);if(!p||!active||status!=='ready')return false;
      cameraMode='follow';needsFit=false;revealRequest=null;moveCamera({x:width/2-p.x*camera.scale,y:height/2-p.y*camera.scale,scale:camera.scale});consumer.invalidate();return true;},
    stopFollowing(){if(cameraMode==='follow'){cameraTween=null;cameraMode='user';consumer.invalidate();}},
    setPalette(patch){if(disposed)return;for(const [key,color] of Object.entries(patch))if(Object.hasOwn(DEFAULT_PALETTE,key)&&typeof color==='string'&&/^#[0-9a-f]{6}$/i.test(color))palette[key]=color;consumer.invalidate();},
    suspend(){if(!active)return;active=false;if(positionTween)positions=positionTween.to;if(cameraTween)camera=cameraTween.to;positionTween=null;cameraTween=null;effects.clear();pendingArrivals.clear();nodeArrivals.clear();serial++;layout.invalidate();layoutDeferred=true;consumer.suspend();},resume(){if(active)return;active=true;if(layoutDeferred&&namespace)submitLayout(true);consumer.resume();},
    dispose(){if(disposed)return;disposed=true;serial++;layout.dispose();observer?.disconnect();win.removeEventListener?.('resize',refreshPixels);detachResolution();detach();consumer.dispose();contextLease?.release();unsubscribePresentation();presentationCache.clear();nodes.clear();positions.clear();pendingArrivals.clear();nodeArrivals.clear();textMetrics.clear();edges=[];hitCells.clear();labels=[];if(registry.get(id)===api)registry.delete(id);},
    get canvas(){return consumer.canvas;},
  };
  registry.set(id,api);return api;
}
