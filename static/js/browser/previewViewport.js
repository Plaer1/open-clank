// Size the existing browsing context; changing a choice never navigates it.
const SIZES=Object.freeze({fit:null,phone:[390,844],tablet:[768,1024],desktop:[1280,800]});
export function createPreviewViewport() {
  let mode='fit', frame=null, viewport=null;
  const select=document.createElement('select'), label=document.createElement('label');
  select.setAttribute('aria-label','Preview viewport size');
  for(const [value,text] of [['fit','Fit available space'],['phone','Phone · 390 × 844'],['tablet','Tablet · 768 × 1024'],['desktop','Desktop · 1280 × 800']]) {const option=document.createElement('option');option.value=value;option.textContent=text;select.append(option);}
  label.append(document.createTextNode('View: '),select);select.disabled=true;
  function apply(){
    if(!frame || !viewport)return;
    const size=SIZES[mode];viewport.style.overflow='auto';
    frame.style.display='block';frame.style.boxSizing='content-box';frame.style.minWidth='0';frame.style.minHeight='0';frame.style.maxWidth='none';frame.style.maxHeight='none';frame.style.border='0';
    frame.style.width=size?`${size[0]}px`:'100%';frame.style.height=size?`${size[1]}px`:'100%';
  }
  select.addEventListener('change',()=>{if(!Object.hasOwn(SIZES,select.value))return;mode=select.value;apply();});
  return Object.freeze({control:label,attach(nextFrame,nextViewport){frame=nextFrame;viewport=nextViewport;select.disabled=false;apply();},detach(){frame=null;viewport=null;select.disabled=true;},get mode(){return mode;}});
}
