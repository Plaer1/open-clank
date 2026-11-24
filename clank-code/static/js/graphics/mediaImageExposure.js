// Conservative paint ownership. No text/source data is inspected.
const overlaps=(a,b)=>a.left<b.right&&a.right>b.left&&a.top<b.bottom&&a.bottom>b.top;
const visible=s=>s.display!=='none'&&s.visibility!=='hidden'&&Number(s.opacity??1)>0;
export function translationOnlyTransform(value){
  if(!value||value==='none')return true;
  const match=/^matrix\(([^)]+)\)$/.exec(value);if(!match)return false;
  const values=match[1].split(',').map(Number);
  return values.length===6&&values.every(Number.isFinite)&&values[0]===1&&values[1]===0&&values[2]===0&&values[3]===1;
}
function existingStackingContext(node,style,doc,win){
  if(node===doc?.documentElement)return true;
  if(['fixed','sticky'].includes(style.position))return true;
  const numericZ=style.zIndex!==''&&style.zIndex!=='auto'&&Number.isFinite(Number(style.zIndex));
  if(numericZ&&['absolute','relative'].includes(style.position))return true;
  if(numericZ&&node.parentElement&&/^(?:inline-)?(?:flex|grid)$/.test(win.getComputedStyle(node.parentElement).display))return true;
  return Number(style.opacity??1)<1||style.transform&&style.transform!=='none'
    ||style.perspective&&style.perspective!=='none'||style.isolation==='isolate'
    ||style.mixBlendMode&&style.mixBlendMode!=='normal'
    ||/(?:^|\s)(?:paint|layout|strict|content)(?:\s|$)/.test(style.contain||'')
    ||/(?:^|,\s*)(?:transform|opacity|perspective|filter)(?:\s*,|$)/.test(style.willChange||'');
}
function identicalRoundedParentClip(image,parent,imageStyle,parentStyle){
  if(image.parentElement!==parent||imageStyle.transform!=='none'||parentStyle.transform!=='none')return false;
  if(!['hidden','clip'].includes(parentStyle.overflowX)||!['hidden','clip'].includes(parentStyle.overflowY))return false;
  const a=image.getBoundingClientRect(),b=parent.getBoundingClientRect();
  if(!['left','top','right','bottom','width','height'].every(k=>a[k]===b[k]))return false;
  for(const node of [image,parent]){
    if(node.scrollLeft!==0||node.scrollTop!==0||node.scrollWidth>node.clientWidth||node.scrollHeight>node.clientHeight)return false;
  }
  for(const style of [imageStyle,parentStyle])for(const side of ['Left','Top','Right','Bottom']){
    if(!/^0(?:\.0+)?px$/.test(style[`border${side}Width`])||!/^0(?:\.0+)?px$/.test(style[`padding${side}`]))return false;
  }
  // Equal boxes/raw circular px radii normalize identically in CSS. No
  // percentage/elliptical solver, inset approximation or epsilon widening.
  return ['TopLeft','TopRight','BottomLeft','BottomRight'].every(corner=>{
    const value=imageStyle[`border${corner}Radius`];return /^\d+(?:\.\d+)?px$/.test(value)&&value===parentStyle[`border${corner}Radius`];
  });
}
export function mediaImageExposed({image,root,doc,win,maxElements=4096,work={remaining:maxElements}}){
  const r=image.getBoundingClientRect();
  if(!root||!doc?.elementFromPoint)return false;
  let owns=false;const ancestors=new Set();
  for(let current=image;current;current=current.parentElement){if(--work.remaining<0||ancestors.has(current))return false;ancestors.add(current);if(current===root){owns=true;break;}}
  if(!owns)return false;
  let boundary=null;
  for(let current=root;current;current=current.parentElement){if(--work.remaining<0)return false;if(existingStackingContext(current,win.getComputedStyle(current),doc,win)){boundary=current;break;}}
  // No recognized context: retain the old full sibling walk, never invent a
  // traversal shortcut or mutate isolation/z-index to manufacture a fence.
  const imageStyle=win.getComputedStyle(image);
  if(imageStyle.clipPath&&imageStyle.clipPath!=='none'||imageStyle.maskImage&&imageStyle.maskImage!=='none')return false;
  for(const [u,v]of [[.05,.05],[.5,.05],[.95,.05],[.05,.5],[.5,.5],[.95,.5],[.05,.95],[.5,.95],[.95,.95]]){
    if(--work.remaining<0)return false;
    const hit=doc.elementFromPoint(r.left+r.width*u,r.top+r.height*v);
    if(hit!==image&&!image.contains?.(hit))return false;
  }
  const visited=new Set();
  let insideRoot=true,insideContext=true;
  for(let current=image;current?.parentElement;current=current.parentElement){
    if(--work.remaining<0)return false;
    const parent=current.parentElement,s=win.getComputedStyle(parent),ar=parent.getBoundingClientRect();
    if(!visible(s)||!translationOnlyTransform(s.transform)||s.clipPath&&s.clipPath!=='none'||s.maskImage&&s.maskImage!=='none'||s.filter&&s.filter!=='none')return false;
    if(parent===root)insideRoot=false;
    // The canvas is a root child, not inside this image-only effect wrapper.
    if(insideRoot&&(Number(s.opacity??1)!==1||s.mixBlendMode&&s.mixBlendMode!=='normal'))return false;
    if(/hidden|auto|scroll|clip/.test(`${s.overflow} ${s.overflowX} ${s.overflowY}`)){
      const radius=Math.max(0,...['TopLeft','TopRight','BottomLeft','BottomRight'].map(c=>Number.parseFloat(s[`border${c}Radius`])||0));
      if((r.left<ar.left+radius||r.right>ar.right-radius||r.top<ar.top+radius||r.bottom>ar.bottom-radius)&&!identicalRoundedParentClip(image,parent,imageStyle,s))return false;
    }
    if(current===boundary)insideContext=false;
    // External stacking branches compose above/below BOTH native image and
    // root canvas together. Still inspect their shared ancestors' geometry.
    if(!insideContext)continue;
    const siblings=parent.children||[];
    if(siblings.length>Math.min(maxElements,work.remaining))return false;
    for(const sibling of siblings){
      if(sibling===current||sibling.dataset?.graphicsSurface)continue;
      // Inspect bounded sibling subtrees so a small corner menu cannot hide
      // between hit-test samples. Ambiguous overlap stays ordinary.
      const stack=[sibling];
      while(stack.length){
        const node=stack.pop();if(visited.has(node))continue;visited.add(node);if(--work.remaining<0||visited.size>maxElements)return false;
        if(node.dataset?.graphicsSurface)continue;
        const ns=win.getComputedStyle(node);if(!visible(ns))continue;
        if(Number(ns.zIndex)<0)continue;
        if(overlaps(r,node.getBoundingClientRect()))return false;
        const children=node.children||[];
        if(children.length>Math.min(maxElements-visited.size,work.remaining-stack.length))return false;
        for(let index=0;index<children.length;index++)stack.push(children[index]);
      }
    }
  }
  return true;
}
