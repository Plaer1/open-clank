// Explicit human batch over existing revision-qualified preview/generate operations.
export function summaryBatch({selection, base, payload, current, signal}) {
  const panel = document.createElement('details');
  const heading = document.createElement('summary');
  heading.textContent = 'Workspace symbol summaries';
  const limit = document.createElement('input');
  Object.assign(limit, {type:'number', min:'1', max:'3000', value:'10'});
  const label = document.createElement('label');
  label.append('Maximum symbols (1–3,000) ', limit);
  const status = document.createElement('p');
  status.setAttribute('role','status');
  const preview = document.createElement('pre');
  preview.style.cssText = 'max-height:16rem;overflow:auto;white-space:pre-wrap';
  const button = text => {
    const element = document.createElement('button');
    element.type = 'button'; element.className = 'copal-btn'; element.textContent = text;
    return element;
  };
  const plan = button('Preview symbol list'), start = button('Generate listed summaries'), pause = button('Pause after current item');
  start.disabled = pause.disabled = true;
  let items = [], next = 0, running = false, paused = false, config = '';
  const saveManifest = button('Save batch progress'), loadManifest = document.createElement('input');
  loadManifest.type = 'file'; loadManifest.accept = '.json,application/json';
  const loadLabel = document.createElement('label');
  loadLabel.append('Restore batch progress ', loadManifest);
  const reuse = button('Read saved summaries');
  const matches = (a,b) => a && b && Object.keys(a).length === Object.keys(b).length && Object.keys(a).every(k=>a[k]===b[k]);
  saveManifest.onclick = () => {
    if (!items.length || !current()) return;
    const entries = items.map(({id,requestId,hash,original})=>({id,requestId,hash,original}));
    const blob = new Blob([JSON.stringify({version:1,recipe:JSON.parse(config),entries})],{type:'application/json'});
    const url=URL.createObjectURL(blob), link=document.createElement('a');
    link.href=url; link.download='open-clank-symbol-summaries.json'; link.click();
    setTimeout(()=>URL.revokeObjectURL(url),1000);
  };
  loadManifest.onchange = async () => {
    const file=loadManifest.files[0]; loadManifest.value='';
    if (!file || running || !current()) return;
    if (!items.length) {status.textContent='Preview the workspace symbol list first.';return;}
    try {
      if(file.size>2097152)throw Error();
      const backup=JSON.parse(await file.text()), recipe=JSON.parse(config);
      if(!current() || running || config!==JSON.stringify(payload()) || backup.version!==1 ||
          !matches(backup.recipe?.receipt,recipe.receipt) || !Array.isArray(backup.entries) ||
          backup.entries.length>3000 || !matches({...backup.recipe,receipt:null},{...recipe,receipt:null}))throw Error();
      const byId=new Map(items.map(item=>[item.id,item])), seen=new Set(), recovered=[];
      for(const row of backup.entries) {
        const item=byId.get(row?.id);
        if(!item)continue;
        if(seen.has(item.id) || !/^[a-f0-9-]{36}$/i.test(row.requestId) ||
            (row.hash!==undefined && !/^[a-f0-9]{64}$/.test(row.hash)))throw Error();
        const ref=row.original;
        if(ref && (!row.hash || ref.version!==1 || ref.request_id!==row.requestId ||
            typeof ref.operation_id!=='string' || ref.operation_id.length>256 ||
            !Number.isSafeInteger(ref.journal_revision) || ref.journal_revision<1))throw Error();
        seen.add(item.id); recovered.push([item,row]);
      }
      for(const [item,row] of recovered){
        item.requestId=row.requestId; item.hash=row.hash; item.original=row.original;
        item.state=row.original?'saved reference':'ready'; delete item.text;
      }
      next=0; draw(); start.disabled=false;
      status.textContent=recovered.length+' matching entries restored. Read saved summaries to verify their source and authorization.';
    } catch {status.textContent='This progress file does not match the current preview and model settings.';}
  };
  reuse.onclick = async () => {
    if(running || !current() || config!==JSON.stringify(payload()))return;
    running=true;plan.disabled=start.disabled=true;pause.disabled=false;paused=false;
    try {
      let loaded=0;
      for(const item of items){
        if(paused || !current())break;
        if(!item.original || item.text)continue;
        const result=await request('summary/reuse',{...JSON.parse(config),scope:'declaration',resource_id:item.id,
          original:item.original,request_id:item.requestId,preview_hash:item.hash,allow_reuse:true});
        if(!current())return;
        if(typeof result.interpretation!=='string')throw Error('Original summary unavailable.');
        item.text=result.interpretation;item.state='generated';loaded++;
      }
      if(current())status.textContent=loaded+' original summaries retrieved.';
    } catch(error){if(current())status.textContent=error.message;}
    finally{running=false;plan.disabled=false;start.disabled=!items.length;pause.disabled=true;if(current())draw();}
  };
  const request = async (route, body) => {
    const res = await fetch(base+'/api/memory/code/'+route, {method:'POST', signal, credentials:'same-origin',
      headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
    if (!res.ok) throw Error('Source, model or authorization changed.');
    return res.json();
  };
  let first = 0;
  const previous = button('Previous 20'), following = button('Next 20');
  previous.onclick = () => {first = Math.max(0, first - 20); draw();};
  following.onclick = () => {if (first + 20 < items.length) first += 20; draw();};
  function draw() {
    if (first >= items.length) first = 0;
    previous.disabled = first === 0;
    following.disabled = first + 20 >= items.length;
    preview.textContent = items.slice(first, first + 20).map(item => item.label+' · '+item.state+(item.text?'\n'+item.text:'')).join('\n');
  }
  function sameReceipt(actual) {
    const expected=selection.receipt;
    return actual && Object.keys(expected).every(k=>actual[k]===expected[k]);
  }
  plan.onclick = async () => {
    const cap = limit.valueAsNumber;
    if (running || !current() || !Number.isInteger(cap) || cap<1 || cap>3000) return;
    const recipe = payload();
    if (!recipe.model_route_id) {status.textContent='Choose a model for this batch.';return;}
    running=true;plan.disabled=start.disabled=true;items=[];next=0;draw();
    config=JSON.stringify(recipe);
    const ids=new Set(), cursors=new Set(); let cursor=null, partial=false;
    try {
      do {
        const page=await request('graph',{workspace_id:selection.workspaceId,query:{operation:'outline',
          expected_receipt:selection.receipt, filter:{kinds:['declaration']}, fields:['label'], channels:[],
          budgets:{rows:Math.min(cap,100),bytes:65536,tokens:8192}, ...(cursor?{cursor}:{})}});
        if (!current() || page.error || !sameReceipt(page.receipt)) throw Error('The published index changed.');
        for (const hit of page.hits || []) {
          if (items.length >= cap) { partial = true; break; }
          if (hit.kind!=='declaration' || !/^decl:[0-9a-f]{64}$/.test(hit.id) || ids.has(hit.id)) continue;
          ids.add(hit.id);
          items.push({id:hit.id,label:String(hit.label || hit.id),state:'ready',requestId:crypto.randomUUID()});
        }
        partial ||= Boolean(page.partial || page.degraded);
        cursor=page.next_cursor;
        if (cursor && cursors.has(cursor)) throw Error('Symbol outline cursor did not advance.');
        cursors.add(cursor);
      } while (cursor && items.length<cap && cursors.size<3000);
      status.textContent=items.length+' symbol(s) selected. '+(cursor || partial?'Coverage is partial. ':'')+
        'Generate sends one source excerpt per item to the selected model. Usage may be billed.';
      draw();start.disabled=!items.length;
    } catch(error) {items=[];if(current())status.textContent=error.message;}
    finally {running=false;plan.disabled=false;}
  };
  pause.onclick=()=>{paused=true;pause.disabled=true;};
  start.onclick=async()=>{
    if(running || !items.length || !current())return;
    if(config!==JSON.stringify(payload())){start.disabled=true;status.textContent='Model or summary settings changed. Preview the list again.';return;}
    running=true;paused=false;plan.disabled=start.disabled=true;pause.disabled=false;
    const recipe=JSON.parse(config);
    try {
      for(;next<items.length && current() && !paused;next++){
        if(config!==JSON.stringify(payload()))throw Error('Settings changed. Preview again.');
        const item=items[next], body={...recipe,scope:'declaration',resource_id:item.id};
        if (item.original) continue;
        const verified=await request('summary/preview',body);
        if(!current() || !sameReceipt(verified.receipt))throw Error('Source receipt changed.');
        if(paused)break;
        if(item.hash && item.hash!==verified.preview_hash)throw Error('Source or route changed. Preview a new list.');
        item.hash=verified.preview_hash;
        status.textContent='Generating '+(next+1)+' of '+items.length+' · '+item.label;
        const result=await request('summary/generate',{...body,preview_hash:item.hash,request_id:item.requestId,allow_reuse:true});
        if(!current())return;
        if(result.operation?.state!=='complete' || result.operation?.committed!==true || typeof result.interpretation!=='string'){
          item.state='outcome unknown';throw Error('Stopped at '+item.label+'; retry retains its operation identity.');
        }
        item.state='generated';item.text=result.interpretation;item.original=result.reuse_reference;
        if(next<20 || next%20===0)draw();
      }
      if(current())status.textContent=next+' of '+items.length+' generated.'+(paused?' Paused.':'');
    } catch(error) {if(current())status.textContent=error.message;}
    finally {running=false;pause.disabled=true;plan.disabled=false;start.disabled=next>=items.length;if(current())draw();}
  };
  panel.append(heading,label,plan,start,pause,status,previous,following,preview,saveManifest,loadLabel,reuse);
  return panel;
}
