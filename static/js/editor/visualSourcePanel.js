import { parseVisualSvg, patchVisualSvg, svgPaintFallback } from './visualSourceEdit.js';

export function mountVisualSourcePanel(root, {capture, current, transact}) {
  let disposed = false, serial = 0;
  const el = (name,text) => { const n = document.createElement(name); if (text != null) n.textContent = text; return n; };
  async function refresh() {
    const ticket=++serial; root.replaceChildren(el('p','Loading visual Properties…'));
    try {
      const snapshot=capture();
      const model=await parseVisualSvg(snapshot.source);
      if (disposed || ticket !== serial || !current(snapshot)) return;
      const title=el('strong','SVG visual Properties'), note=el('p','Supported source shapes only. Role colors use their declared fallback in this preview; they are not the current theme. Edits change the original source and can be undone.');
      const svg=document.createElementNS('http://www.w3.org/2000/svg','svg'); svg.setAttribute('viewBox',model.viewBox); svg.setAttribute('aria-label',model.title || 'SVG visual selection'); svg.style.cssText='display:block;width:100%;max-width:600px;max-height:400px;border:1px solid currentColor';
      const list=el('div'), properties=el('div'); list.setAttribute('aria-label','Groups and shapes');
      const select=index => {
        properties.replaceChildren(el('strong',`Properties: ${model.items[index].kind} ${index+1}`));
        for (const [name,span] of Object.entries(model.items[index].attrs)) {
          const label=el('label',`${name} `), input=el('input'); input.value=span.value; input.setAttribute('aria-label',name); label.append(input);
          const apply=el('button','Apply'); apply.type='button'; apply.addEventListener('click',()=>{try { if (!current(snapshot)) throw Error('Source or account changed. Reopen visual Properties.'); const next=patchVisualSvg(model,index,name,input.value); const result=transact(snapshot,next); if (result?.outcome === 'failed') throw Error(result.message); void refresh(); } catch(error) { status.textContent=error.message; }}); label.append(apply); properties.append(label);
        }
      };
      const status=el('p'); status.setAttribute('role','status');
      const indices=new Map(model.items.map((item,index)=>[item,index]));
      function draw(item,parent){const index=indices.get(item),node=document.createElementNS('http://www.w3.org/2000/svg',item.kind);for(const [k,v] of Object.entries(item.attrs))node.setAttribute(k,(k==='fill'||k==='stroke') ? svgPaintFallback(v.value) : v.value);node.addEventListener('click',event=>{event.stopPropagation();select(index);});parent.append(node);for(const child of item.children)draw(child,node);}
      for(const item of model.nodes)draw(item,svg);
      model.items.forEach((item,i)=>{const button=el('button',`${item.kind==='g'?'group':item.kind} ${i+1}`);button.type='button';button.addEventListener('click',()=>select(i));list.append(button);});
      root.replaceChildren(title,note,svg,list,properties,status); select(0);
    } catch(error) { if (!disposed && ticket===serial) root.replaceChildren(el('p',error.message || 'Visual Properties unavailable. Use source mode.')); }
  }
  void refresh(); return {dispose(){disposed=true;serial++;root.replaceChildren();}};
}
