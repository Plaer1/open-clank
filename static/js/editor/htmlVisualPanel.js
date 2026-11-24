import {parseHtmlOutline,patchHtmlOutline,htmlFeedbackLocator} from './htmlVisualSource.js';

export function mountHtmlVisualPanel(root,{capture,current,transact,onFeedback,onReveal}) {
  let disposed=false,serial=0;
  const el=(tag,text)=>{const node=document.createElement(tag);if(text!=null)node.textContent=String(text);return node;};
  function invalidate(){if(disposed)return;serial++;root.replaceChildren(el('p','Source changed. Close and reopen HTML source Properties.'));}
  async function refresh(){
    const ticket=++serial;root.replaceChildren(el('p','Mapping source elements…'));
    try {
      const snapshot=capture(), model=await parseHtmlOutline(snapshot.source);
      if(disposed || ticket!==serial)return;if(!current(snapshot)){invalidate();return;}
      const heading=el('strong','HTML source Properties'), note=el('p','Static source outline; this is not a live DOM selection. Values are specified inline styles; the saved preview and runtime cascade may differ.'), list=el('div'), properties=el('div'), status=el('p');
      list.setAttribute('aria-label','Source elements');status.setAttribute('role','status');
      const select=index=>{
        const item=model.nodes[index];properties.replaceChildren(el('strong',`${item.tag}: ${item.label}`));
        if (typeof onReveal==='function') { const reveal=el('button','Reveal source');reveal.type='button';reveal.addEventListener('click',async()=>{try{if(!current(snapshot))throw Error('Source changed.');await onReveal(snapshot,item,model);}catch(error){status.textContent=error.message;}});properties.append(reveal); }
        if (typeof onFeedback==='function') { const feedback=el('button','Feedback on source element…');feedback.type='button';feedback.addEventListener('click',async()=>{try{if(!current(snapshot))throw Error('Source changed.');const locator=await htmlFeedbackLocator(model,index,snapshot.resourceID);if(!current(snapshot))throw Error('Source changed.');await onFeedback(snapshot,locator,model);}catch(error){status.textContent=error.message || 'Static source feedback is unavailable.';}});properties.append(feedback); }
        for(const [name,span] of Object.entries(item.properties)){
          const label=el('label',`${name} `),input=el('input'),apply=el('button','Apply');input.value=span.value;input.setAttribute('aria-label',name);apply.type='button';
          apply.addEventListener('click',()=>{try{if(!current(snapshot))throw Error('Source or account changed. Reopen Properties.');const next=patchHtmlOutline(model,index,name,input.value);const result=transact(snapshot,next,model.sourceSHA256);if(result?.outcome==='failed')throw Error(result.message);void refresh();}catch(error){status.textContent=error.message;}});label.append(input,apply);properties.append(label);
        }
      };
      model.nodes.forEach((item,index)=>{const b=el('button',`${item.tag}: ${item.label}`);b.type='button';b.addEventListener('click',()=>select(index));list.append(b);});
      root.replaceChildren(heading,note,list,properties,status);select(0);
    }catch(error){if(!disposed && ticket===serial)root.replaceChildren(el('p',error.message || 'Mapping unavailable. Continue in source mode.'));}
  }
  void refresh();return {invalidate,dispose(){disposed=true;serial++;root.replaceChildren();}};
}
