import { replayRecordedInvestigation } from './copal/codeInvestigation.js';

// A journal page declares membership; each History action is reauthorized on use.
export function createHistoryChangeSets({context, render, comparator}) {
  let scope=context, selectedAction=null, noteID='', head='', title='', description='', members=[], selected=0, page=0;
  let busy=false, dirty=false, pendingCreate=null, pendingUpdate=null, message='', metadata=null, serial=0, actionEpoch=0;
  const current=()=>!!scope && scope.isCurrent() && document.body.dataset.accountId===scope.account;
  const sameScope=(a,b)=>!!a&&!!b&&a.account===b.account&&a.workspace===b.workspace&&a.epoch===b.epoch&&a.api===b.api;
  const valid=(original,ticket)=>ticket===serial&&sameScope(scope,original)&&original.isCurrent()&&current();
  const purge=()=>{serial++;actionEpoch++;selectedAction=null;noteID=head=title=description='';members=[];selected=page=0;busy=dirty=false;pendingCreate=pendingUpdate=metadata=null;message='';scope=null;};
  const authChanged=()=>{purge();render();};
  const authEvents=['openclank:auth-context-changed','openclank-account-changed'];
  for(const target of [document,window])for(const event of authEvents)target.addEventListener(event,authChanged);
  const bind=next=>{selectedAction=null;actionEpoch++;if(!sameScope(scope,next)){purge();scope=next||null;}else scope=next;};
  const selectAction=(locator,epoch)=>{if(epoch===actionEpoch && current() && typeof locator==='string' && locator.startsWith('oc-history-action:v1:')){selectedAction=locator;message='Recorded action selected for this set.';render();}};
  const request=async(path,options={})=>{if(!current())throw Error('Copal scope unavailable');const original=scope,ticket=serial;const value=await original.api(path,{cache:'no-store',...options},original.workspace);if(!valid(original,ticket))throw Error('Copal scope changed');return value;};
  const send=async(fn)=>{if(busy||!current())return;busy=true;const own=++serial;render();try{await fn(own);}catch{if(current()&&own===serial)message='Request unavailable; keep the original draft and inspect its status.';}finally{if(own===serial){busy=false;render();}}};
  const value=()=>({version:1,title,description,members:members.map(locator=>({locator}))});
  const applyDoc=result=>{if(result?.version!==1||typeof result.document_id!=='string'||typeof result.head!=='string'||!result.head||result.set?.version!==1||typeof result.set.title!=='string'||typeof result.set.description!=='string'||!Array.isArray(result.set.members)||result.set.members.length>100||result.set.members.some(x=>!x||typeof x.locator!=='string'))throw Error('Invalid set');noteID=result.document_id;head=result.head;title=result.set.title;description=result.set.description;members=result.set.members.map(x=>x.locator);selected=page=0;dirty=false;metadata=null;};
  const create=()=>send(async()=>{if(pendingCreate||pendingUpdate)return;if(!title.trim()){message='Enter a title.';return;}const action_id=crypto.randomUUID().replaceAll('-','');pendingCreate={version:1,workspace:scope.workspace,name:title,title,description,members:value().members,action_id};message='Creating the declared note…';await createStatus();});
  const createStatus=async()=>{const result=await request('/logical-change-sets',{method:'POST',body:JSON.stringify(pendingCreate)});if(result?.state==='complete'||result?.state==='status_only'&&result.receipt?.state==='complete'){const receipt=result.receipt;if(!receipt?.document_id||!receipt?.head)throw Error('Invalid receipt');noteID=receipt.document_id;head=receipt.head;pendingCreate=null;dirty=false;message='Set saved as a journal page.';}else message='Original create is pending or unavailable; check its status without creating another.';};
  const open=id=>send(async()=>{if(dirty||pendingCreate||pendingUpdate){message='Keep or save the current draft before opening another note.';return;}const result=await request('/logical-change-sets/'+encodeURIComponent(id));applyDoc(result);message='Opened the current declared set.';});
  const save=()=>send(async()=>{if(!noteID||!head||pendingCreate||pendingUpdate)return;const action_id=crypto.randomUUID().replaceAll('-','');pendingUpdate={version:1,workspace:scope.workspace,base:head,action_id,...value()};const result=await request('/logical-change-sets/'+encodeURIComponent(noteID),{method:'PUT',body:JSON.stringify(pendingUpdate)});if(result?.outcome!=='committed'||!result?.doc?.head||result.doc.id!==noteID||result.actionId!==action_id)throw Error('Unknown save');head=result.doc.head;pendingUpdate=null;dirty=false;message='Set saved at its new note revision.';});
  const check=()=>send(async()=>{const original=scope,ticket=serial,start=page*20,locators=members.slice(start,start+20);if(!locators.length)return;const response=await fetch('/api/history/captured-change-metadata',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({version:1,locators}),cache:'no-store'});const result=await response.json();if(!valid(original,ticket)||!response.ok||result?.version!==1||!Array.isArray(result.items)||result.items.length!==locators.length||result.items.some(x=>!x||!['available','unavailable'].includes(x.state)))throw Error('Metadata unavailable');metadata={start,items:result.items};message='Current authorized evidence; unavailable members have no displayed identity.';});
  const node=(tag,text)=>{const n=document.createElement(tag);if(text!=null)n.textContent=text;return n;};
  const button=(label,fn,disabled=false)=>{const b=node('button',label);b.type='button';b.disabled=disabled||busy;b.addEventListener('click',fn);return b;};
  const field=(label,val,oninput,multiline=false)=>{const box=node('label',label),input=node(multiline?'textarea':'input');input.value=val;input.disabled=busy||!!pendingCreate||!!pendingUpdate;input.maxLength=multiline?16384:200;input.addEventListener('input',()=>{oninput(input.value);dirty=true;});box.append(input);return box;};
  const renderSection=()=>{if(!current())return null;const box=node('section');box.className='history-change-groups';box.setAttribute('aria-label','Local change set');box.append(node('h3','Local change set'),node('p','An editable note declaration, not a complete turn, PR or push.'));
    const choices=node('select');choices.setAttribute('aria-label','Open a loaded journal page');const blank=node('option','Choose a loaded note');blank.value='';choices.append(blank);for(const choice of scope.choices.slice(0,100)){const opt=node('option',choice.name);opt.value=choice.id;choices.append(opt);}box.append(choices,button('Open',()=>{if(choices.value)open(choices.value);},!!pendingCreate||!!pendingUpdate));if(!scope.choices.length)box.append(node('p','Open Notes to load existing note choices; a new set can still be created.'));
    const investigation=replayRecordedInvestigation(value());
    if(investigation){
      const format=tick=>tick===null||!Number.isFinite(new Date(Number(tick)).valueOf())?'Unknown':new Date(Number(tick)).toLocaleString();
      const report=node('section');
      report.append(node('h4','Recorded investigation'),
        node('p',`${investigation.observations} records across ${investigation.windows} saved windows. ${investigation.changesets} distinct changesets. Partial coverage.`),
        node('p',`${format(investigation.earliest)} – ${format(investigation.latest)}`));
      if(investigation.cochanges.length)report.append(node('h5','Recurring recorded co-change'));
      for(const pair of investigation.cochanges){
        const details=node('details');
        details.append(node('summary',`${pair.witnesses.length} of ${investigation.changesets} changesets`));
        for(const witness of pair.witnesses){
          const row=node('p');
          row.append(button(`Compare member ${witness.leftMember+1}`,()=>comparator(members[witness.leftMember])));
          if(witness.rightMember!==witness.leftMember)row.append(button(`Compare member ${witness.rightMember+1}`,()=>comparator(members[witness.rightMember])));
          details.append(row);
        }
        report.append(details);
      }
      if(investigation.omitted)report.append(node('p',`${investigation.omitted} additional correlations in the saved windows.`));
      box.append(report);
    }
    box.append(field('Title',title,v=>{title=v;}));
    if(!investigation)box.append(field('Description',description,v=>{description=v;},true));
    const picker=node('select');picker.setAttribute('aria-label','Set member');members.forEach((_,i)=>{const option=node('option',`Member ${i+1}`);option.value=String(i);picker.append(option);});picker.value=String(selected);picker.addEventListener('change',()=>{selected=Number(picker.value)||0;render();});box.append(picker);
    const action=node('select');action.setAttribute('aria-label','Local set action');for(const [id,label] of [['add','Add selected recorded action'],['remove','Remove member'],['up','Move member up'],['down','Move member down'],['check','Check member page'],['compare','Compare selected member']]){if(investigation&&['add','remove','up','down'].includes(id))continue;const option=node('option',label);option.value=id;action.append(option);}box.append(action,button('Apply',()=>{if(pendingCreate||pendingUpdate)return;const op=action.value;if(investigation&&['add','remove','up','down'].includes(op))return;if(op==='add'&&selectedAction&&members.length<100&&!members.includes(selectedAction)){members.push(selectedAction);selected=members.length-1;dirty=true;metadata=null;}else if(op==='remove'&&members.length){members.splice(selected,1);selected=Math.max(0,Math.min(selected,members.length-1));dirty=true;metadata=null;}else if(op==='up'&&selected>0){[members[selected-1],members[selected]]=[members[selected],members[selected-1]];selected--;dirty=true;metadata=null;}else if(op==='down'&&selected+1<members.length){[members[selected+1],members[selected]]=[members[selected],members[selected+1]];selected++;dirty=true;metadata=null;}else if(op==='check'){void check();return;}else if(op==='compare'&&members[selected]){comparator(members[selected]);return;}render();}));
    box.append(node('p',`Members: ${members.length}. ${selectedAction?'A recorded action is selected.':'Choose an authorized recorded action below.'}`));
    if(metadata){for(const item of metadata.items){box.append(node('p',item.state==='available'?`${item.current_name} · ${item.change?.operation||'action'} · ${item.change?.timestamp_millis||'time unknown'}`:'Unavailable'));}}
    box.append(button('Previous members',()=>{page=Math.max(0,page-1);metadata=null;render();},page===0),node('span',` Page ${page+1} `),button('Next members',()=>{page++;metadata=null;render();},(page+1)*20>=members.length));
    if(pendingCreate)box.append(button('Check original create',()=>send(createStatus)));else if(pendingUpdate)box.append(node('p','Save outcome is unknown. Keep this draft and inspect the note and History before another edit.'));else box.append(button(noteID?'Save set':'Create set',noteID?save:create));
    if(message)box.append(node('p',message));return box;};
  return {bind,purge,dispose(){for(const target of [document,window])for(const event of authEvents)target.removeEventListener(event,authChanged);purge();},selectAction,actionToken(){return actionEpoch;},clearAction(){selectedAction=null;actionEpoch++;},renderSection};
}
