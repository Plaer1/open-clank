// Pure CSS replaced-image geometry. No image acquisition or resource authority.
export const BITMAP_OUTSIDE_POSITION='-1000000px -1000000px';
const px=value=>{if(value==null||value==='')return 0;return /^\d+(?:\.\d+)?px$/.test(value)?Number.parseFloat(value):NaN;};
const radius=(value,width,height)=>{
  const parts=String(value||'0px').trim().split(/\s+/);if(parts.length>2)return null;
  const length=(v,extent)=>/^\d+(?:\.\d+)?%$/.test(v)?Number.parseFloat(v)*extent/100:px(v);
  const result=[length(parts[0],width),length(parts[1]||parts[0],height)];return result.every(Number.isFinite)?result:null;
};
function position(value){
  const aliases={left:'0%',top:'0%',center:'50%',right:'100%',bottom:'100%'};
  let tokens=String(value||'50% 50%').trim().split(/\s+/);
  if(tokens.length===1)tokens=['top','bottom'].includes(tokens[0])?['50%',tokens[0]]:[tokens[0],'50%'];
  if(['top','bottom'].includes(tokens[0])&&['left','right','center'].includes(tokens[1]))tokens.reverse();
  if(tokens.length!==2)return null;
  return tokens.map(token=>{const text=aliases[token]||token;if(!/^-?\d+(?:\.\d+)?(?:%|px)$/.test(text))return null;return {n:Number.parseFloat(text),percent:text.endsWith('%')};});
}
export function mediaImageGeometry({x,y,width,height,naturalWidth,naturalHeight,style={}}){
  if(![x,y,width,height,naturalWidth,naturalHeight].every(Number.isFinite)||width<=0||height<=0||width>8192||height>8192||naturalWidth<=0||naturalHeight<=0)return null;
  if(style.transform&&style.transform!=='none'||style.filter&&style.filter!=='none'||style.mixBlendMode&&style.mixBlendMode!=='normal'||Number(style.opacity??1)!==1)return null;
  if(style.transitionDuration&&style.transitionDuration.split(',').some(value=>Number.parseFloat(value)!==0))return null;
  if(style.animationName&&style.animationName!=='none')return null;
  const sides=['Left','Top','Right','Bottom'];
  const insets=sides.map(side=>px(style[`border${side}Width`])+px(style[`padding${side}`]));
  if(!insets.every(Number.isFinite))return null;
  const [left,top,right,bottom]=insets,cw=width-left-right,ch=height-top-bottom;if(cw<=0||ch<=0)return null;
  const corners=['TopLeft','TopRight','BottomRight','BottomLeft'].map(corner=>radius(style[`border${corner}Radius`],width,height));
  if(corners.some(value=>!value))return null;
  // CSS normalizes overlapping outer corner radii before border/padding inset.
  const factor=Math.min(1,width/Math.max(1,corners[0][0]+corners[1][0]),width/Math.max(1,corners[3][0]+corners[2][0]),height/Math.max(1,corners[0][1]+corners[3][1]),height/Math.max(1,corners[1][1]+corners[2][1]));
  const rx=corners.map((r,i)=>Math.max(0,r[0]*factor-([0,3].includes(i)?left:right)));
  const ry=corners.map((r,i)=>Math.max(0,r[1]*factor-(i<2?top:bottom)));
  const fit=style.objectFit||'fill',pos=position(style.objectPosition);if(!pos||pos.some(value=>!value)||!['fill','contain','cover'].includes(fit))return null;
  let dw=cw,dh=ch;
  if(fit!=='fill'){const scale=(fit==='cover'?Math.max:Math.min)(cw/naturalWidth,ch/naturalHeight);dw=naturalWidth*scale;dh=naturalHeight*scale;}
  const offset=(space,p)=>p.percent?space*p.n/100:p.n;
  const startX=x+left+offset(cw-dw,pos[0]),startY=y+top+offset(ch-dh,pos[1]);
  const x0=Math.max(x+left,startX),y0=Math.max(y+top,startY),x1=Math.min(x+left+cw,startX+dw),y1=Math.min(y+top+ch,startY+dh);
  if(x1<=x0||y1<=y0)return null;
  return {x:x0,y:y0,width:x1-x0,height:y1-y0,uv:[(x0-startX)/dw,(y0-startY)/dh,(x1-startX)/dw,(y1-startY)/dh],clip:{x:x+left,y:y+top,width:cw,height:ch,rx,ry}};
}
export function suppressBitmap(image){
  const style=image.style;
  const old={value:style.getPropertyValue('object-position'),priority:style.getPropertyPriority('object-position')};
  style.setProperty('object-position',BITMAP_OUTSIDE_POSITION,'important');
  // CSSOM may serialize large numeric lengths in exponent form. Compare the
  // actual applied serialization, not the authored spelling, when restoring.
  old.applied=style.getPropertyValue('object-position');return old;
}
export function restoreBitmap(image,old){
  const style=image.style;
  // An external authored update wins; never restore over a newer inline edit.
  if(style.getPropertyValue('object-position')!==old.applied||style.getPropertyPriority('object-position')!=='important')return;
  if(old.value)style.setProperty('object-position',old.value,old.priority);else style.removeProperty('object-position');
}
