const TYPES = [['duplicate','Possible duplicate'],['conflict','Potential conflict'],['lesson','Lesson or correction']];
const BASIS = {
  same_normalized_claim:'Same claim wording',shared_source_and_wording:'Shared source and similar wording',
  matching_native_conflict:'Matching native conflict proposal',different_values_for_single_claim:'Different values for one claim',
  successive_claim_revisions:'Successive validity periods for one claim',shared_source_with_guidance:'Shared source with guidance'
};
const requestOptions = {credentials:'same-origin',cache:'no-store'};
const digest = value => typeof value === 'string' && /^[0-9a-f]{64}$/.test(value);
const element = (tag,text) => {
  const item=document.createElement(tag);
  if (text!=null) item.textContent=text;
  return item;
};

export function memoryRelationReview({id, choices, isCurrent, onSettled}) {
  const panel=element('details');
  const target=element('select');
  target.setAttribute('aria-label','Earlier memory');
  const blank=element('option','Choose earlier memory');
  blank.value='';
  target.append(blank);
  for (const choice of choices) {
    const option=element('option',choice.label);
    option.value=choice.id;
    target.append(option);
  }
  const kind=element('select');
  kind.setAttribute('aria-label','Proposed relationship');
  for (const [value,label] of TYPES) {
    const option=element('option',label);
    option.value=value;
    kind.append(option);
  }
  const compare=element('button','Compare evidence');
  compare.type='button';
  const evidence=element('div');
  const status=element('p');
  status.setAttribute('role','status');
  const suggestions=element('div');
  const find=element('button','Find suggested pairs');
  find.type='button';
  panel.append(element('summary','Review relationship'),find,suggestions,target,kind,compare,evidence,status);
  const url=`/api/memory/${encodeURIComponent(id)}`;
  const valid=()=>panel.isConnected && isCurrent();
  let sequence=0;
  const reset=()=>{++sequence;compare.disabled=!target.value;evidence.replaceChildren();status.textContent='';};
  target.addEventListener('change',reset);
  kind.addEventListener('change',reset);
  compare.disabled=true;

  const read=async (path,options={})=>{
    const response=await fetch(url+path,{...requestOptions,...options});
    if(!response.ok)throw Error('Review unavailable or changed');
    return response.json();
  };
  const send=(path,body)=>read(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const post=body=>send('/supersede',body);
  let suggestionRequest=0;
  find.addEventListener('click',async()=>{
    if(!valid())return;
    const request=++suggestionRequest, owns=()=>valid()&&request===suggestionRequest;
    find.disabled=true;
    suggestions.replaceChildren(element('p','Reading scoped Memory candidates…'));
    try {
      const page=await read('/relation-review/suggestions');
      if(!owns())return;
      if(page?.version!==1||page.coverage!=='bounded_native_candidates'||!Array.isArray(page.items)||page.items.length>8)throw Error();
      suggestions.replaceChildren();
      const list=element('ul');
      for(const item of page.items){
        const label=TYPES.find(([value])=>value===item?.kind)?.[1], basis=BASIS[item?.basis];
        if(!label||!basis||typeof item?.target_id!=='string'||!item.target_id||item.target_id.length>512||
            typeof item.label!=='string'||!Number.isSafeInteger(item.revision))continue;
        const row=element('li',`${label} · ${basis} · ${item.label}`);
        const button=element('button',item.decision?.state==='declined'?'Review declined pair':'Compare suggested pair');
        button.type='button';
        button.addEventListener('click',()=>{
          if(!valid()||target.disabled)return;
          if(![...target.options].some(option=>option.value===item.target_id)){
            const option=element('option',item.label);
            option.value=item.target_id;
            target.append(option);
          }
          target.value=item.target_id;
          kind.value=item.kind;
          reset();
          compare.click();
        });
        row.append(' ',button);
        list.append(row);
      }
      suggestions.append(list.childNodes.length?list:element('p','No suggested pairs in the current candidate window.'));
    } catch {
      if(owns())suggestions.replaceChildren(element('p','Suggested pairs are unavailable.'));
    } finally {
      if(owns())find.disabled=false;
    }
  });
  compare.addEventListener('click',async()=>{
    if(!target.value||!valid()) return;
    const attempt=++sequence, owns=()=>valid()&&sequence===attempt;
    const selectedTarget=target.value, selectedKind=kind.value;
    const query=new URLSearchParams({target_id:selectedTarget,kind:selectedKind});
    evidence.replaceChildren();
    compare.disabled=true;
    status.textContent='Reading current evidence…';
    try {
      const result=await read(`/relation-review?${query}`);
      if(!owns())return;
      const sides=[result?.left,result?.right];
      if(result?.version!==1||result.kind!==selectedKind||!digest(result.evidence_sha256)||sides.some(side=>
          !Number.isSafeInteger(side?.revision)||typeof side.claim!=='string'||!Array.isArray(side.evidence)||side.evidence.length>4))throw Error();
      for(const [index,side] of sides.entries()){
        const section=element('section');
        section.append(element('h4',`${index===0?'Selected':'Earlier'} memory · revision ${side.revision}`),element('p',side.claim||'Claim text unavailable'));
        if(side.validity?.valid_from||side.validity?.valid_to) section.append(element('p',`Valid ${side.validity.valid_from||'from unknown'} to ${side.validity.valid_to||'open'}`));
        if(side.claim_truncated) section.append(element('p','Claim excerpt is abbreviated.'));
        for(const item of side.evidence){
          if(!item||!digest(item.source_sha256)||typeof item.excerpt!=='string')throw Error();
          if(item.excerpt) section.append(element('blockquote',item.excerpt));
        }
        if(!side.evidence.length) section.append(element('p','Original evidence unavailable.'));
        evidence.append(section);
      }
      status.textContent='Choose whether the selected memory supersedes the earlier revision.';
      const previewButton=element('button','Preview supersession');
      previewButton.type='button';
      const decline=element('button','Decline pair');
      decline.type='button';
      if(result.decision?.state==='declined'){
        decline.disabled=true;
        status.textContent='Decline recorded for these revisions.';
      }
      evidence.append(previewButton,decline);
      decline.addEventListener('click',async()=>{
        if(!owns())return;
        decline.disabled=previewButton.disabled=true;
        try {
          const decision=await send('/relation-review/decline',{target_id:selectedTarget,kind:selectedKind,evidence_sha256:result.evidence_sha256});
          if(!owns())return;
          if(decision?.state!=='declined'||typeof decision.receipt_id!=='string')throw Error();
          status.textContent='Decline recorded for these revisions.';
          suggestions.replaceChildren();
        } catch {
          if(owns()){
            status.textContent='Decline unavailable. Compare the current evidence again.';
            decline.disabled=previewButton.disabled=false;
          }
        }
      });
      previewButton.addEventListener('click',async()=>{
        if(!owns())return;
        previewButton.disabled=decline.disabled=true;
        try {
          const ctx=await read('/mirror-context');
          if(!owns())return;
          if(ctx?.mode!=='guarded'||ctx.expected_revision!==result.left.revision)throw Error();
          const base={target_id:selectedTarget,comparison:{kind:selectedKind,evidence_sha256:result.evidence_sha256},
            candidate_id:ctx.candidate_id,origin_request_key:ctx.origin_request_key,prior_receipt_id:ctx.prior_receipt_id,
            expected_revision:ctx.expected_revision,request_key:crypto.randomUUID(),reason:'Reviewed relationship'};
          const preview=await post({...base,phase:'preview'});
          if(!owns())return;
          if(preview?.target_revision!==result.right.revision||typeof preview.operation_json!=='string'||!digest(preview.operation_sha256))throw Error();
          status.textContent='Both original revisions will remain in History. You can later detach this relationship.';
          const apply=element('button','Confirm supersession');
          apply.type='button';
          previewButton.replaceWith(apply);
          apply.addEventListener('click',async()=>{
            if(!owns())return;
            apply.disabled=true;
            target.disabled=kind.disabled=compare.disabled=true;
            try {
              const receipt=await post({...base,phase:'apply',operation_json:preview.operation_json,operation_sha256:preview.operation_sha256});
              if(!owns())return;
              if(receipt?.ok!==true)throw Error();
              status.textContent='Supersession recorded.';
              await onSettled();
            } catch {
              if(owns()){
                apply.disabled=false;
                apply.textContent='Check original review';
                status.textContent='Outcome unavailable. Check the original review using the same receipt.';
              }
            }
          });
        } catch {
          if(owns()) {previewButton.disabled=false;decline.disabled=result.decision?.state==='declined';status.textContent='The evidence or review context changed. Compare again.';}
        }
      });
    } catch {
      if(owns()) {evidence.replaceChildren();status.textContent='Two-sided evidence is unavailable.';}
    } finally {
      if(owns())compare.disabled=false;
    }
  });
  return panel;
}
