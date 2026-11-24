import { buildMenu, menuPrompt, pickOperation, pickProfile, needsProfileQuestion, profilePrompt, NEUTRAL_OPERATIONS } from './mediaMenuModel.js';
// Explicit, single-user media review. All authority is revalidated by Files.
export async function runSpriteCropsFlow({client,sessionID,current,sourceRef,sourceID,sourceName,chooseDestination,status}) {
  const check=()=>{if(!current())throw new Error('The Files or chat scope changed.');};check();
  const destinationRef=await chooseDestination();check();if(!destinationRef)return;
  const dialog=document.createElement('dialog');dialog.className='copal-dialog sprite-sheet-dialog';const title=document.createElement('h2');title.textContent='Select sprite frames';dialog.append(title);
  const fields=document.createElement('div');fields.className='modal-body';dialog.append(fields);
  const input=(label,value,type='number')=>{const row=document.createElement('label'),element=document.createElement('input');row.textContent=label;element.type=type;element.value=value;row.append(element);fields.append(row);return element;};
  const side=input('Square frame size (pixels)','32'),rects=Array.from({length:4},(_,i)=>({x:input(`Frame ${i+1} source X`,'0'),y:input(`Frame ${i+1} source Y`,'0'),px:input('Pivot X','0'),py:input('Pivot Y','0')}));
  const fps=input('Frames per second','6'),facing=input('Facing label (optional)','','text'),palette=input('Palette label (optional)','','text'),style=input('Style label (optional)','','text'),name=input('Destination filename','sprite-sheet.png','text');
  const detail=document.createElement('pre');detail.setAttribute('aria-live','polite');detail.textContent='Choose four distinct non-overlapping equal square regions from the untagged 8-bit RGB/RGBA PNG. Source geometry is never inferred.';dialog.append(detail);
  const footer=document.createElement('div');footer.className='modal-footer';const cancel=document.createElement('button'),advance=document.createElement('button');cancel.type=advance.type='button';cancel.textContent='Cancel';advance.textContent='Review';cancel.className='confirm-btn confirm-btn-secondary';advance.className='confirm-btn confirm-btn-primary';footer.append(cancel,advance);dialog.append(footer);document.body.append(dialog);
  let prepared=null,requestParameters=null,busy=false,closed=false;
  return await new Promise(resolve=>{const finish=()=>{if(closed)return;closed=true;dialog.close();dialog.remove();resolve();};cancel.addEventListener('click',finish);dialog.addEventListener('cancel',event=>{event.preventDefault();finish();});
    advance.addEventListener('click',async()=>{if(busy||closed)return;busy=true;advance.disabled=true;try{check();if(!prepared){
      const size=Number(side.value),rectangles=rects.map(r=>({x:Number(r.x.value),y:Number(r.y.value),width:size,height:size}));
      const annotations={fps:Number(fps.value),facing:facing.value.trim()||null,pivots:rects.map(r=>({x:Number(r.px.value),y:Number(r.py.value)})),palette:palette.value.trim()||null,style:style.value.trim()||null};
      if(!Number.isSafeInteger(size)||size<1||size>512||rectangles.some(r=>!Number.isSafeInteger(r.x)||!Number.isSafeInteger(r.y)||r.x<0||r.y<0)||!Number.isSafeInteger(annotations.fps)||annotations.fps<1||annotations.fps>60||annotations.pivots.some(p=>!Number.isSafeInteger(p.x)||!Number.isSafeInteger(p.y)||p.x<0||p.y<0||p.x>size||p.y>size))throw new Error('Enter valid integer rectangles, pivots and FPS.');
      const destinationName=name.value.trim();if(!destinationName)throw new Error('Enter a destination filename.');requestParameters={annotations,rectangles};
      const value=await client.prepareSpriteCrops({sessionID,sourceRef,destinationRef,destinationName,parameters:annotations,rectangles});check();if(closed)return;
      if(value?.state!=='prepared'||value.source?.resourceID!==sourceID||value.destination?.name!==destinationName||value.cellSize!==size||!Number.isSafeInteger(value.sourceWidth)||!Number.isSafeInteger(value.sourceHeight)||JSON.stringify(value.rectangles)!==JSON.stringify(rectangles)||JSON.stringify(value.parameters)!==JSON.stringify(requestParameters))throw new Error('The source or reviewed frames changed.');
      prepared=value;for(const el of fields.querySelectorAll('input'))el.disabled=true;
      detail.textContent=`Source: ${String(sourceName||'PNG').slice(0,120)} · ${value.source.resourceID} · ${value.source.sha256}\nActual source: ${value.sourceWidth}×${value.sourceHeight}\nOrdered source rectangles: ${JSON.stringify(value.rectangles)}\nOutput: 2×2, ${size}×${size} cells; no resampling · ${annotations.fps} fps\nDestination: ${value.destination.folder} / ${destinationName}`;advance.textContent='Create';
    }else{let result;try{result=await client.runMedia({jobID:prepared.jobID,revision:prepared.revision,parameters:requestParameters});}catch{result={state:'unknown_outcome'};}check();if(!closed)status(`Media job ${prepared.jobID}: ${result.state}. Open Media jobs to refresh or collect; do not repeat an unknown operation.`);finish();}
    }catch(error){if(!closed)detail.textContent=String(error?.message||'Sprite sheet unavailable.')+' Check source bounds, non-overlapping rectangles and current Files access.';}finally{busy=false;if(!closed)advance.disabled=false;}});dialog.showModal();});
}

export async function runSpriteSheetFlow({client,ui,sessionID,current,chooseDestination,chooseFrame,status}) {
  const check=()=>{if(!current())throw new Error('The Files or chat scope changed.');};
  const sources=[]; for(let i=0;i<4;i++) { const selected=await chooseFrame(i+1); check(); if(!selected)return; sources.push(selected); }
  const destinationRef=await chooseDestination(); check(); if(!destinationRef)return;
  const dialog=document.createElement('dialog'); dialog.className='copal-dialog sprite-sheet-dialog';
  const title=document.createElement('h2'); title.textContent='Create sprite sheet'; dialog.append(title);
  const fields=document.createElement('div'); fields.className='modal-body'; dialog.append(fields);
  const requirements=document.createElement('p'); requirements.textContent='Use four equal square, untagged 8-bit RGB or RGBA PNGs. Indexed, high-bit-depth, tagged and animated PNGs are unsupported.'; fields.append(requirements);
  const input=(label,value,type='text')=>{const row=document.createElement('label'); row.textContent=label; const element=document.createElement('input'); element.type=type; element.value=value; row.append(element); fields.append(row); return element;};
  const pivots=sources.map((source,i)=>({x:input(`Frame ${i+1}: ${String(source.name).slice(0,120)} · pivot X`,'0','number'),y:input('Pivot Y','0','number')}));
  const fps=input('Frames per second','6','number'), facing=input('Facing label (optional)',''), palette=input('Palette label (optional)',''), style=input('Style label (optional)',''), name=input('Destination filename','sprite-sheet.png');
  const detail=document.createElement('pre'); detail.setAttribute('aria-live','polite'); dialog.append(detail);
  const footer=document.createElement('div'); footer.className='modal-footer'; const cancel=document.createElement('button'), advance=document.createElement('button');
  cancel.type=advance.type='button'; cancel.textContent='Cancel'; advance.textContent='Review'; cancel.className='confirm-btn confirm-btn-secondary'; advance.className='confirm-btn confirm-btn-primary'; footer.append(cancel,advance); dialog.append(footer); document.body.append(dialog);
  let prepared=null, parameters=null, busy=false, closed=false;
  return await new Promise(resolve=>{
    const finish=()=>{if(closed)return; closed=true; dialog.close(); dialog.remove(); resolve();};
    cancel.addEventListener('click',finish); dialog.addEventListener('cancel',event=>{event.preventDefault();finish();});
    advance.addEventListener('click',async()=>{
      if(busy || closed)return; busy=true; advance.disabled=true;
      try {
        check();
        if(!prepared) {
          parameters={fps:Number(fps.value),facing:facing.value.trim()||null,pivots:pivots.map(p=>({x:Number(p.x.value),y:Number(p.y.value)})),palette:palette.value.trim()||null,style:style.value.trim()||null};
          if(!Number.isSafeInteger(parameters.fps) || parameters.fps<1 || parameters.fps>60 || parameters.pivots.some(p=>!Number.isSafeInteger(p.x)||!Number.isSafeInteger(p.y)||p.x<0||p.y<0))throw new Error('Enter a valid FPS and nonnegative integer pivots.');
          const destinationName=name.value.trim(); if(!destinationName)throw new Error('Enter a destination filename.');
          const candidate=await client.prepareSpriteSheet({sessionID,sourceRefs:sources.map(v=>v.ref),destinationRef,destinationName,parameters}); check(); if(closed)return;
          if(candidate?.state!=='prepared' || candidate.destination?.name!==destinationName || candidate.sources?.length!==4 || !Number.isSafeInteger(candidate.cellSize) || candidate.cellSize<1 || candidate.cellSize>512)throw new Error('The sheet review is unavailable.');
          if(candidate.parameters?.fps!==parameters.fps || candidate.parameters?.facing!==parameters.facing || candidate.parameters?.palette!==parameters.palette || candidate.parameters?.style!==parameters.style || !Array.isArray(candidate.parameters?.pivots) || candidate.parameters.pivots.length!==4 || candidate.parameters.pivots.some((p,i)=>p.x!==parameters.pivots[i].x || p.y!==parameters.pivots[i].y))throw new Error('The sheet parameters changed.');
          prepared=candidate; for(const el of fields.querySelectorAll('input'))el.disabled=true;
          detail.textContent=candidate.sources.map((v,i)=>`${i+1}. ${sources[i].name} · ${v.resourceID} · ${v.sha256}`).join('\n')+`\n\n2×2 · ${candidate.cellSize}×${candidate.cellSize} cells · no resampling · ${parameters.fps} fps\nDestination: ${candidate.destination.folder} / ${destinationName}`;
          advance.textContent='Create';
        } else {
          let result; try {result=await client.runMedia({jobID:prepared.jobID,revision:prepared.revision,parameters});} catch (_) {result={state:'unknown_outcome'};}
          check(); if(!closed)status(`Media job ${prepared.jobID}: ${result.state}. Open Media jobs to refresh or collect; do not repeat an unknown operation.`); finish();
        }
      } catch(error) {if(!closed)detail.textContent=String(error?.message || 'Sprite sheet unavailable.')+' Check the untagged 8-bit RGB/RGBA PNG requirements and current Files access.';}
      finally {busy=false; if(!closed)advance.disabled=false;}
    });
    dialog.showModal();
  });
}

export async function runNeutralMediaFlow({client, ui, sessionID, current, chooseDestination, chooseRecipe, chooseImage, status}) {
  const check=()=>{if(!current())throw new Error('The Files or chat scope changed.');};
  const catalog=await client.mediaProfiles({sessionID,operation:'*'});check();
  const menu=buildMenu(catalog,NEUTRAL_OPERATIONS);
  if(!menu.length)throw new Error('Media operations are unavailable.');
  const choice=await ui.styledPrompt(menuPrompt(menu),{title:'Choose media operation',defaultValue:'1'});check();
  if(choice==null)return;
  const item=pickOperation(menu,choice);
  const operation=item.operation;
  const index=needsProfileQuestion(item)?await ui.styledPrompt(profilePrompt(item),{title:'Choose a compatible profile',defaultValue:'1'}):null;check();
  if(needsProfileQuestion(item)&&index==null)return;
  const profile=pickProfile(item,index);
  if (profile.input_kinds?.includes('image') && profile.output_kinds?.includes('mesh') && profile.output_mime==='model/gltf-binary') {
    const model=profile.model;
    if(!model||typeof model.id!=='string'||!model.id||typeof model.label!=='string'||!model.label)throw new Error('The model is unavailable for review.');
    const source=await chooseImage();check();if(!source)return;
    const destinationRef=await chooseDestination();check();if(!destinationRef)return;
    const destinationName=await ui.styledPrompt('Name the new GLB.',{title:'Output name',defaultValue:'model.glb'});check();if(!destinationName)return;
    const prepared=await client.prepareMediaOperation({sessionID,operation,profile_id:profile.profile_id,profile_revision:profile.revision,
      inputs:[{kind:'image',resource_ref:source.ref}],parameters:{},destination:{resource_ref:destinationRef,name:destinationName}});check();
    const image=prepared?.source;
    if(prepared?.state!=='prepared'||prepared.profile?.providerAccountID!==profile.profile_id||prepared.destination?.name!==destinationName
      ||image?.ref!==source.ref||image.resourceID!==source.resourceID||image.revision?.kind!==source.revision?.kind||image.revision?.value!==source.revision?.value
      ||prepared.model?.id!==model.id||prepared.model?.label!==model.label
      ||!['image/png','image/jpeg'].includes(image.mime_type)||!Number.isSafeInteger(image.width)||!Number.isSafeInteger(image.height)
      ||typeof image.name!=='string'||!(/^[0-9a-f]{64}$/).test(image.sha256))throw new Error('The image selection changed; start a new review.');
    const detail=`Profile: ${profile.label || profile.profile_id}\nSource: ${image.name} (${image.width} × ${image.height}, ${image.mime_type})\nModel: ${model.label} (${model.id})\nOutput: GLB model\nDestination: ${prepared.destination.folder} / ${destinationName}\n\n${prepared.notice}`;
    if(!await ui.styledConfirm(detail,{title:'Review model',confirmText:'Generate'}))return;check();
    let result;try{result=await client.runMedia({jobID:prepared.jobID,revision:prepared.revision,parameters:{}});}catch(_){result={state:'unknown_outcome'};}
    check();status(`Media job ${prepared.jobID}: ${result.state}. Open Media jobs to refresh or collect; do not repeat an unknown submission.`);return;
  }
  if(profile.input_kinds?.includes('text_prompt') && profile.output_kinds?.includes('mesh') && profile.output_mime==='model/gltf-binary') {
    const prompt=await ui.styledPrompt('Describe one 3D model (up to 1024 characters).',{title:'Model prompt'});check();if(prompt==null)return;
    const destinationRef=await chooseDestination();check();if(!destinationRef)return;
    const destinationName=await ui.styledPrompt('Name the new GLB.',{title:'Output name',defaultValue:'model.glb'});check();if(!destinationName)return;
    const prepared=await client.prepareMediaOperation({sessionID,operation,profile_id:profile.profile_id,profile_revision:profile.revision,
      inputs:[{kind:'text_prompt',text:prompt}],parameters:{},destination:{resource_ref:destinationRef,name:destinationName}});check();
    if(prepared?.state!=='prepared'||prepared.profile?.providerAccountID!==profile.profile_id||prepared.destination?.name!==destinationName||prepared.parameters?.prompt!==prompt)throw new Error('The selection changed; start a new review.');
    const detail=`Profile: ${profile.label || profile.profile_id}\nPrompt: ${prompt}\nOutput: GLB model\nDestination: ${prepared.destination.folder} / ${destinationName}\n\n${prepared.notice}`;
    if(!await ui.styledConfirm(detail,{title:'Review model',confirmText:'Generate'}))return;check();
    let result;try{result=await client.runMedia({jobID:prepared.jobID,revision:prepared.revision,parameters:{prompt}});}catch(_){result={state:'unknown_outcome'};}
    check();status(`Media job ${prepared.jobID}: ${result.state}. Open Media jobs to refresh or collect; do not repeat an unknown submission.`);return;
  }
  if(profile.input_kinds?.includes('text_prompt') && profile.output_mime==='image/png') {
    const prompt=await ui.styledPrompt('Describe one image (up to 1024 characters).',{title:'Image prompt'});check();if(prompt==null)return;
    const destinationRef=await chooseDestination();check();if(!destinationRef)return;
    const destinationName=await ui.styledPrompt('Name the PNG.',{title:'Output name',defaultValue:'image.png'});check();if(!destinationName)return;
    const prepared=await client.prepareMediaOperation({sessionID,operation,profile_id:profile.profile_id,profile_revision:profile.revision,inputs:[{kind:'text_prompt',text:prompt}],parameters:{},destination:{resource_ref:destinationRef,name:destinationName}});check();
    if(prepared?.state!=='prepared'||prepared.profile?.providerAccountID!==profile.profile_id||prepared.destination?.name!==destinationName||prepared.parameters?.prompt!==prompt)throw new Error('The selection changed; start a new review.');
    if(!await ui.styledConfirm(`Profile: ${profile.label}\nPrompt: ${prompt}\nOutput: PNG\nDestination: ${prepared.destination.folder} / ${destinationName}\n\n${prepared.notice}`,{title:'Review image',confirmText:'Generate'}))return;check();
    let result;try{result=await client.runMedia({jobID:prepared.jobID,revision:prepared.revision,parameters:{prompt}});}catch(_){result={state:'unknown_outcome'};}
    check();status(`Media job ${prepared.jobID}: ${result.state}. Open Media jobs to refresh or collect; do not repeat an unknown submission.`);return;
  }
  if(!profile.input_kinds?.includes('workflow_recipe')||!profile.output_kinds?.includes('image'))throw new Error('Profile input is not supported.');
  const recipeRef=await chooseRecipe();check();if(!recipeRef)return;
  return runMediaJobFlow({client,ui,recipeRef,sessionID,current,chooseDestination,status,neutralProfile:profile});
}

/** Review explicitly assigned source roles for the fixed local material bundle. */
export async function runMaterialPackFlow({ui,sessionID,current,chooseDestination,chooseMap,status}) {
  const check=()=>{if(!current())throw new Error('The Files or chat scope changed.');};
  const roles=['baseColor','roughness','metallic'], sources=[];
  for(const role of roles){const source=await chooseMap(role);check();if(!source)return;sources.push({role,...source});}
  const normal=await ui.styledConfirm('Add an optional tangent-space normal map?',{title:'Optional normal map',confirmText:'Choose normal',cancelText:'Skip'});check();
  if(normal){const source=await chooseMap('normal');check();if(!source)return;sources.push({role:'normal',...source});}
  if(new Set(sources.map(x=>x.resourceID)).size!==sources.length)throw new Error('Choose a different resource for each role.');
  const destinationRef=await chooseDestination();check();if(!destinationRef)return;
  const name=await ui.styledPrompt('Name the material bundle ZIP.',{title:'Output name',defaultValue:'material.zip'});check();if(!name)return;
  const response=await fetch('/api/files-v1/media/material/prepare',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify({sessionID,sources:sources.map(({role,ref})=>({role,resource_ref:ref})),destination:{resource_ref:destinationRef,name},parameters:{}})});
  const prepared=await response.json().catch(()=>null);check();
  if(!response.ok||prepared?.state!=='prepared'||prepared.destination?.name!==name||!Array.isArray(prepared.sources)||prepared.sources.length!==sources.length||!(/^[0-9a-f]{64}$/).test(prepared.manifestSHA256))throw new Error('Material review unavailable or changed.');
  for(let i=0;i<sources.length;i++){const expected=sources[i],actual=prepared.sources[i];if(actual?.role!==expected.role||actual.ref!==expected.ref||actual.resourceID!==expected.resourceID||actual.revision?.kind!==expected.revision?.kind||actual.revision?.value!==expected.revision?.value)throw new Error('A selected map changed; start a new review.');}
  const detail=prepared.sources.map(x=>`${x.role}: ${x.name} (${x.width} × ${x.height})`).join('\n')+`\n\nMetallic-roughness; base color sRGB; scalar and normal maps linear; UV set 0; opaque (alpha ignored); optional normal: tangent OpenGL +Y.\nDestination: ${prepared.destination.folder} / ${name}\n\n${prepared.notice}`;
  if(!await ui.styledConfirm(detail,{title:'Review material bundle',confirmText:'Package'}))return;check();
  let result;try {const run=await fetch('/api/files-v1/media/material/run',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify({jobID:prepared.jobID,revision:prepared.revision,parameters:{}})});result=await run.json();if(!run.ok)throw new Error('unavailable');}catch(_){result={state:'unknown_outcome'};}
  check();status(`Media job ${prepared.jobID}: ${result.state}. Open Media jobs to refresh or collect; do not repeat an unknown submission.`);
}

export async function runMediaJobFlow({client, ui, recipeRef, sessionID, current, chooseDestination, status, neutralProfile=null}) {
  const check = () => { if (!current()) throw new Error('The Files or chat scope changed.'); };
  check();
  let profiles, index;
  if(neutralProfile){ profiles=[{id:neutralProfile.profile_id,label:neutralProfile.label}]; index=0; }
  else {
    const listing = await client.mediaProfiles({sessionID,operation:'image.generate'}); check();
    profiles = (Array.isArray(listing?.profiles) ? listing.profiles : [])
      .filter(row => row?.operation === 'image.generate' && Array.isArray(row.input_kinds) && row.input_kinds.includes('workflow_recipe') && typeof row.profile_id === 'string')
      .map(row => ({ id: row.profile_id, label: row.label }));
    if (!profiles.length) throw new Error('Configure an available local media connection first.');
    const selection = await ui.styledPrompt(profiles.map((row,i)=>`${i+1}. ${String(row.label || row.id).slice(0,120)}`).join('\n'),
      {title:'Choose a media connection', defaultValue:'1'}); check();
    if (selection == null) return;
    index = Number(selection)-1;
    if (!Number.isInteger(index) || !profiles[index]) throw new Error('Choose a listed connection.');
  }
  const preview = await client.inspectMediaRecipe({sessionID,recipeRef}); check();
  if (!preview || !Array.isArray(preview.slots) || preview.slots.length > 32 || !Array.isArray(preview.nodeClasses))
    throw new Error('The recipe preview is unavailable.');
  const selectedOutput = neutralProfile ? 'png' : await ui.styledPrompt('Type png for an image, or flac for one audio output.', {title:'Output profile', defaultValue:'png'}); check();
  if (selectedOutput == null) return;
  const outputProfile = selectedOutput.trim().toLowerCase();
  if (!['png','flac'].includes(outputProfile)) throw new Error('Choose png or flac.');
  const parameters = {};
  for (const slot of preview.slots) {
    if (!slot || typeof slot.name !== 'string' || !['string','integer','number','boolean'].includes(slot.type)) throw new Error('The recipe slot is unsupported.');
    const bounds = ['integer','number'].includes(slot.type) ? ` (${slot.min} to ${slot.max})` : slot.type === 'boolean' ? ' (true or false)' : '';
    const answer = await ui.styledPrompt(`${slot.name}${bounds}`, {title:'Workflow parameter'}); check();
    if (answer == null) return;
    let value = answer;
    if (slot.type === 'boolean') {
      if (!['true','false'].includes(answer)) throw new Error('Use true or false.');
      value = answer === 'true';
    } else if (['integer','number'].includes(slot.type)) {
      value = Number(answer);
      if (!Number.isFinite(value) || (slot.type === 'integer' && !Number.isSafeInteger(value))
          || !Number.isFinite(slot.min) || !Number.isFinite(slot.max) || value < slot.min || value > slot.max) throw new Error('Parameter is outside the recipe bounds.');
    }
    parameters[slot.name] = value;
  }
  const destinationRef = await chooseDestination(); check();
  if (!destinationRef) return;
  const destinationName = await ui.styledPrompt(`Name the new ${outputProfile.toUpperCase()}.`, {title:'Output name', defaultValue:`generated.${outputProfile}`}); check();
  if (!destinationName) return;
  const prepared = neutralProfile ? await client.prepareMediaOperation({sessionID,operation:'image.generate',profile_id:neutralProfile.profile_id,inputs:[{kind:'workflow_recipe',resource_ref:recipeRef}],parameters,destination:{resource_ref:destinationRef,name:destinationName}}) : await client.prepareMedia({sessionID,connectionID:profiles[index].id,recipeRef,destinationRef,destinationName,parameters,...(outputProfile === 'flac' ? {outputProfile} : {})}); check();
  if (prepared?.state !== 'prepared' || typeof prepared.jobID !== 'string' || !Number.isSafeInteger(prepared.revision)
      || prepared.recipe?.sha256 !== preview.sha256 || prepared.profile?.connectionID !== profiles[index].id
      || prepared.destination?.name !== destinationName || typeof prepared.destination?.folder !== 'string'
      || (outputProfile === 'flac' && prepared.outputProfile !== 'flac') || !Array.isArray(prepared.recipe?.nodeClasses)) throw new Error('The selection changed; start a new review.');
  const detail = `Connection: ${profiles[index].label || profiles[index].id}\nProfile: ${prepared.profile.connectionID} · revision ${prepared.profile.connectionRevision} · ${prepared.profile.interface}\nRecipe: ${prepared.recipe.name}\nSHA-256: ${prepared.recipe.sha256}\nNodes: ${prepared.recipe.nodeClasses.join(', ')}\nOutput: ${outputProfile === 'flac' ? 'FLAC via SaveAudioAdvanced node openclank_audio_output; fixed prefix openclank_audio. Header-declared audio fields are checked; audio payload decoding is not verified. The output may embed the workflow and prompt.' : 'PNG'}\nDestination: ${prepared.destination.folder} / ${prepared.destination.name}\nParameters: ${JSON.stringify(parameters)}\n\nRun this one workflow? Local custom nodes may incur costs.`;
  const approved = await ui.styledConfirm(detail, {title:'Review media job', confirmText:'Run'}); check();
  if (!approved) return;
  let result;
  try { result = await client.runMedia({jobID:prepared.jobID,revision:prepared.revision,parameters}); }
  catch (error) {
    if (error?.code === 'source_stale') { status('The source or destination changed or expired; no submission was made.', true); return; }
    result = {state:'unknown_outcome'};
  }
  check();
  for (;;) {
    const audio = result.result?.audio;
    const witness = audio ? ` Header declares ${audio.sampleRate} Hz, ${audio.channels} channel(s), ${audio.bitsPerSample}-bit, ${audio.declaredSamples} samples; audio payload decoding is unverified.` : '';
    status(`Media job ${prepared.jobID}: ${result.state}${result.reason ? ` (${result.reason})` : ''}.${witness}`);
    if (['complete','failed','cancelled'].includes(result.state)) return;
    const action = await ui.styledPrompt('Type status, cancel, reconcile, collect, or leave blank to close.', {title:'Media job', defaultValue:''}); check();
    if (!action) return;
    if (!['status','cancel','reconcile','collect'].includes(action)) throw new Error('Unknown media action.');
    result = await client.mediaAction(action,prepared.jobID); check();
  }
}

// Audio is an optional recording intent. Device names are untrusted display text.
function recordingDialog(title, detail) {
  const dialog=document.createElement('dialog'), heading=document.createElement('h2'), text=document.createElement('p'), fields=document.createElement('div'), footer=document.createElement('div');
  dialog.className='copal-dialog';heading.textContent=title;text.textContent=detail;footer.className='modal-footer';dialog.append(heading,text,fields,footer);document.body.append(dialog);
  return {dialog,fields,footer,text};
}
function recordingButton(text) {const b=document.createElement('button');b.type='button';b.textContent=text;b.className='confirm-btn confirm-btn-secondary';return b;}
async function chooseRecordingIntent(current, destinationName, cropAvailable) {
  const {dialog,fields,footer}=recordingDialog('Record screen',`Record one selected screen or window to ${destinationName} in the chosen folder? Up to 60 seconds at 15 fps. Audio is optional and off by default; choose audio sources after selecting the screen. Stop is available in Media jobs.`);
  const input=(text)=>{const label=document.createElement('label'),box=document.createElement('input');box.type='checkbox';box.checked=false;label.append(box,document.createTextNode(text));fields.append(label);return box;};
  const microphone=input(' Microphone'),system=input(' System output'),crop=cropAvailable?input(' Select a crop after choosing the source'):null,cancel=recordingButton('Cancel'),next=recordingButton('Start');footer.append(cancel,next);
  return await new Promise(resolve=>{let done=false;const finish=value=>{if(done)return;done=true;dialog.close();dialog.remove();resolve(value);};
    cancel.onclick=()=>finish(null);next.onclick=()=>finish(current()?{microphone:microphone.checked,systemOutput:system.checked,cropIntent:!!crop?.checked}:null);
    dialog.addEventListener('cancel',event=>{event.preventDefault();finish(null);});dialog.showModal();});
}

export async function showRecordingAudioChoices({client,current,status,job}) {
  if(!current() || job?.recordingPhase!=='audio_choices' || job?.audioSelection || !job?.audioChoices) return false;
  const {generation,choices,preview}=job.audioChoices, id=/^[0-9a-f]{32}$/;
  if(typeof generation!=='string' || !id.test(generation) || !Array.isArray(choices) || choices.length>24 || !Number.isSafeInteger(job.revision)
      || typeof job.jobID!=='string' || choices.some(c=>!c || typeof c!=='object' || Array.isArray(c)
        || Object.keys(c).sort().join(',')!=='channels,choice_id,kind,label' || !['microphone','system_output'].includes(c.kind)
        || typeof c.choice_id!=='string' || !id.test(c.choice_id) || typeof c.label!=='string' || !c.label
        || new TextEncoder().encode(c.label).length>64 || /\p{Cc}/u.test(c.label)
        || !Array.isArray(c.channels) || !([['MONO'],['FL','FR'],['FR','FL']].some(v=>v.length===c.channels.length && v.every((x,i)=>x===c.channels[i]))))
      || new Set(choices.map(c=>c.choice_id)).size!==choices.length) throw new Error('Audio choices are unavailable.');
  const {dialog,fields,footer,text}=recordingDialog(preview?'Review recording':'Recording audio sources','Choose a source for each kind, or leave it Off. Start continues this same recording.');
  let cropFields=null;
  if(preview) {
    if(!id.test(String(preview.basis_id||'')) || !Number.isSafeInteger(preview.source_width) || !Number.isSafeInteger(preview.source_height) || preview.source_width<1 || preview.source_width>16384 || preview.source_height<1 || preview.source_height>16384 || preview.source_width*preview.source_height>16000000 || typeof preview.png!=='string' || preview.png.length>2796204 || !/^[A-Za-z0-9+/]*={0,2}$/.test(preview.png)) throw new Error('Recording preview unavailable.');
    const image=document.createElement('img');image.alt='Selected source preview';image.src=`data:image/png;base64,${preview.png}`;image.style.maxWidth='100%';fields.append(image);
    const label=document.createElement('label'),toggle=document.createElement('input');toggle.type='checkbox';label.append(toggle,document.createTextNode(' Crop source pixels'));fields.append(label);
    const values={};for(const [key,initial] of [['x',0],['y',0],['width',preview.source_width-preview.source_width%2],['height',preview.source_height-preview.source_height%2]]) { const l=document.createElement('label'),i=document.createElement('input');i.type='number';i.min='0';i.step='2';i.value=String(initial);l.append(document.createTextNode(key),i);fields.append(l);values[key]=i; }
    cropFields={toggle,values};
  }
  const select=kind=>{const label=document.createElement('label'),sel=document.createElement('select'),off=document.createElement('option');label.textContent=kind==='microphone'?'Microphone':'System output';off.value='';off.textContent='Off';sel.append(off);
    for(const choice of choices.filter(c=>c.kind===kind)){const option=document.createElement('option');option.value=choice.choice_id;option.textContent=choice.label.replace(/[\u0000-\u001f\u007f]/g,' ').slice(0,120) || 'Unnamed source';sel.append(option);}
    sel.value='';label.append(sel);fields.append(label);return sel;};
  const microphone=select('microphone'),system=select('system_output'),cancel=recordingButton('Later'),start=recordingButton('Start');footer.append(cancel,start);
  return await new Promise(resolve=>{let done=false,busy=false;const finish=value=>{if(done)return;done=true;dialog.close();dialog.remove();resolve(value);};
    cancel.onclick=()=>{if(!busy)finish(false);};dialog.addEventListener('cancel',event=>{event.preventDefault();if(!busy)finish(false);});
    start.onclick=async()=>{if(busy || !current())return;busy=true;start.disabled=cancel.disabled=true;microphone.disabled=system.disabled=true;
      const choiceFor=(element,kind)=>element.value && choices.some(c=>c.kind===kind && c.choice_id===element.value)?element.value:null;
      const mic=choiceFor(microphone,'microphone'),out=choiceFor(system,'system_output');
      if((microphone.value && !mic)||(system.value && !out)){text.textContent='Audio choices changed. Check the original job status.';finish(false);return;}
      let crop; if(preview) {let rect=null;if(cropFields.toggle.checked){rect=Object.fromEntries(Object.entries(cropFields.values).map(([k,v])=>[k,Number(v.value)]));if(Object.values(rect).some(v=>!Number.isSafeInteger(v)||v<0||v%2) || !rect.width || !rect.height || rect.x+rect.width>preview.source_width || rect.y+rect.height>preview.source_height){text.textContent='Enter an even, in-bounds crop in source pixels.';busy=false;start.disabled=cancel.disabled=false;microphone.disabled=system.disabled=false;return;}}crop={mode:rect?'crop':'full',basis_id:preview.basis_id,rect};}
      const selectionID=crypto.randomUUID().replaceAll('-','');
      try {await client.selectRecordingAudio({jobID:job.jobID,revision:job.revision,selectionID,generation,microphone:mic,systemOutput:out,...(crop?{crop}:{})});
        if(current())status(`Recording ${job.jobID}: audio selection submitted. Check the original job status.`);
      } catch (_) {if(current())status(`Recording ${job.jobID}: audio selection outcome unknown. Check the original job status; do not resend.`,true);}
      finish(true);
    };dialog.showModal();});
}

export async function runRecordingFlow({client, ui, sessionID, current, chooseDestination, status}) {
  const destinationRef = await chooseDestination();
  if (!current() || !destinationRef) return;
  const destinationName = await ui.styledPrompt('Name the recording.', {title:'Record screen', defaultValue:'recording.webm'});
  if (!current() || !destinationName) return;
  let controlMode=false;
  try {
    const response=await fetch(`/api/desktop-control/status?session_id=${encodeURIComponent(sessionID)}`,{credentials:'same-origin'});
    const availability=response.ok ? await response.json() : null;
    if(!current())return;
    if(availability?.recording_target_available===true) {
      controlMode=await ui.styledConfirm('Record the screen currently shared for desktop control? Cancel to choose a separate screen or window.',{title:'Control session recording',confirmText:'Use shared screen'});
      if(!current())return;
    }
  } catch (_) { /* The ordinary human recording chooser remains available. */ }
  let audioIntent=null;
  if(!controlMode && typeof client.recordingAudioReadiness==='function') {
    let readiness;try {readiness=await client.recordingAudioReadiness();}catch (_) {readiness=null;}
    if(!current())return;
    if(readiness?.available===true || readiness?.cropAvailable===true) {audioIntent=await chooseRecordingIntent(current,destinationName,readiness?.cropAvailable===true);if(!current() || !audioIntent)return;}
  }
  const prepared = await client.prepareRecording({sessionID,destinationRef,destinationName,...(controlMode ? {controlSession:true} : {}),...(audioIntent && (audioIntent.microphone || audioIntent.systemOutput) ? {audioIntent:{microphone:audioIntent.microphone,systemOutput:audioIntent.systemOutput}} : {}),...(audioIntent?.cropIntent ? {cropIntent:true} : {})});
  if (!current()) return;
  if (!prepared?.jobID || !Number.isSafeInteger(prepared.revision) || prepared.destination?.name !== destinationName) throw new Error('Recording preparation unavailable.');
  if (!audioIntent) {
    const approved = await ui.styledConfirm(`${controlMode ? 'Record the screen already shared for desktop control' : 'Record one selected screen or window'} to ${prepared.destination.folder} / ${destinationName}?\nUp to 60 seconds at 15 fps, without audio or input events. ${controlMode ? 'Stopping control also stops recording.' : 'Stop is available in Media jobs.'}`, {title:'Record screen',confirmText:'Start'});
    if (!current() || !approved) return;
  }
  try {
    const result = await client.runMedia({jobID:prepared.jobID,revision:prepared.revision,parameters:{}});
    if (current()) status(`Recording ${prepared.jobID}: ${result.state}. Use Media jobs to Stop or check status.`);
  } catch (_) { if (current()) status(`Recording ${prepared.jobID}: outcome unknown. Check the original job; do not start it again.`,true); }
}

export function describeTrimTiming(timing) {
  const requested=timing?.requested, observed=timing?.observed;
  if (!requested || !observed || requested.timebase!=='playback_hint' || observed.timebase!=='source_stream_nanoseconds'
      || !Number.isSafeInteger(requested.start_ms) || !Number.isSafeInteger(requested.end_ms)
      || !Number.isSafeInteger(observed.first_pts_ns) || !Number.isSafeInteger(observed.last_pts_ns)
      || !Number.isSafeInteger(observed.selected_frames) || !Number.isSafeInteger(observed.encoded_frames)
      || !['reached_end','source_eos_before_end','unknown'].includes(observed.coverage)) return 'Observed trim coverage unavailable.';
  return `Requested ${requested.start_ms}–${requested.end_ms} ms (playback hints); observed source PTS ${observed.first_pts_ns}–${observed.last_pts_ns} ns · ${observed.encoded_frames}/${observed.selected_frames} frames · ${observed.coverage.replaceAll('_',' ')}.`;
}

export async function runTrimMediaFlow({client,ui,sessionID,current,chooseDestination,status,source,onIssued}) {
  const check=()=>{if(!current())throw new Error('The original recording, account or task changed.');};
  const frozen={resourceRef:String(source?.resourceRef || ''),resourceID:String(source?.resourceID || ''),name:String(source?.name || ''),
    revision:{kind:source?.revision?.kind,value:source?.revision?.value},startMs:source?.startMs,endMs:source?.endMs};
  if(!frozen.resourceRef || !frozen.resourceID || frozen.revision.kind!=='hostFingerprint' || typeof frozen.revision.value!=='string'
      || !Number.isSafeInteger(frozen.startMs) || !Number.isSafeInteger(frozen.endMs) || frozen.startMs<0
      || frozen.endMs>86400000 || frozen.endMs<=frozen.startMs || frozen.endMs-frozen.startMs>60000)throw new Error('The selected recording range is unavailable.');
  check();const listing=await client.mediaProfiles({sessionID,operation:'video.trim'});check();
  if(!Array.isArray(listing?.profiles)||listing.profiles.length>20)throw new Error('Trim profile unavailable.');
  const profile=listing.profiles.find(item=>item?.profile_id==='trim-webm-v1' && item.operation==='video.trim'
    && item.output_mime==='video/webm' && item.input_kinds?.includes('host_video') && /^[0-9a-f]{64}$/.test(String(item.revision || '')));
  if(!profile)throw new Error('Local WebM trim is unavailable.');
  const destinationRef=await chooseDestination();check();if(!destinationRef)return null;
  const defaultName=`${frozen.name.replace(/\.webm$/i,'') || 'recording'}-trim.webm`;
  const destinationName=await ui.styledPrompt('Name the derived WebM copy.',{title:'Trim recording',defaultValue:defaultName});check();if(!destinationName)return null;
  const prepared=await client.prepareMediaOperation({sessionID,operation:'video.trim',profile_id:profile.profile_id,profile_revision:profile.revision,
    inputs:[{kind:'host_video',resource_ref:frozen.resourceRef,expected_revision:frozen.revision}],
    parameters:{start_ms:frozen.startMs,end_ms:frozen.endMs},destination:{resource_ref:destinationRef,name:destinationName}});check();
  if(prepared?.state!=='prepared' || typeof prepared.jobID!=='string' || !Number.isSafeInteger(prepared.revision)
      || prepared.source?.resourceID!==frozen.resourceID || prepared.source?.revision?.kind!==frozen.revision.kind
      || prepared.source?.revision?.value!==frozen.revision.value || !/^[0-9a-f]{64}$/.test(String(prepared.source?.sha256 || ''))
      || prepared.range?.startMs!==frozen.startMs || prepared.range?.endMs!==frozen.endMs || prepared.range?.timebase!=='requested_hint'
      || prepared.destination?.name!==destinationName || typeof prepared.destination?.folder!=='string')throw new Error('The reviewed recording or destination changed.');
  const detail=`Source: ${frozen.name}\nSource SHA-256: ${prepared.source.sha256}\nRange: ${frozen.startMs}–${frozen.endMs} ms (playback hints)\nDerived copy: ${prepared.destination.folder} / ${destinationName}\n\nThe source is preserved. Actual selected frames and coverage are reported after processing.`;
  if(!await ui.styledConfirm(detail,{title:'Review trim',confirmText:'Trim'}))return null;check();
  onIssued(prepared.jobID);
  let result;try{result=await client.runMedia({jobID:prepared.jobID,revision:prepared.revision,parameters:{}});}catch(_){result={state:'unknown_outcome'};}
  if(current())status(`Trim job ${prepared.jobID}: ${result.state}. Use Check / save trim; do not retry an unknown job.`);
  return prepared.jobID;
}

export function describeRecordingObservations(value) {
  const measurements=value?.measurements;
  if (!measurements || value?.upstream_loss!=='unknown') return 'Recording observations unavailable.';
  const count=value=>Number.isSafeInteger(value) && value>=0 ? String(value) : 'unknown';
  const time=value=>typeof value==='string' && /^(0|[1-9][0-9]{0,19})$/.test(value) ? `${value} ns` : 'unknown';
  const size=value=>Array.isArray(value) && value.length===2 && value.every(n=>Number.isSafeInteger(n) && n>0 && n<=65536) ? `${value[0]} × ${value[1]} px` : 'unknown';
  const stage=(name,row)=>`${name}: buffers ${count(row?.buffers)}, missing PTS ${count(row?.missing_pts)}, missing running time ${count(row?.missing_running)}; PTS ${time(row?.first_pts_ns)}–${time(row?.last_pts_ns)}, running ${time(row?.first_running_ns)}–${time(row?.last_running_ns)}`;
  const rate=measurements.videorate;
  return [`Source pixels: ${size(measurements.source_pixels)}; output pixels: ${size(measurements.output_pixels)}; caps changed: ${typeof measurements.caps_changed==='boolean' ? (measurements.caps_changed?'yes':'no') : 'unknown'}.`,
    stage('Source pad',measurements.source),stage('Rate output pad',measurements.rate_output),stage('Encoder pad',measurements.encoded),
    `Videorate local counters: input ${count(rate?.in)}, output ${count(rate?.out)}, dropped ${count(rate?.drop)}, duplicated ${count(rate?.duplicate)}.`,
    'Upstream and aggregate loss: unknown. These are local stage observations, not a complete loss count.'].join('\n');
}

export function describeRecordingAudioTracks(tracks) {
  if (!Array.isArray(tracks) || tracks.length>2) return 'Audio observations unavailable.';
  if (!tracks.length) return 'No audio tracks selected.';
  const count=value=>Number.isSafeInteger(value) && value>=0 ? String(value) : 'unknown';
  return tracks.map(row=>{
    if (!row || !['microphone','system_output'].includes(row.kind) || row.stage!=='pcm_appsrc_submission' || row.assurance!=='software_correlated_estimate') return 'Audio track observations unavailable.';
    return `${row.kind==='microphone'?'Microphone':'System output'} PCM submission: ${count(row.submitted_frames)} frames; Header observed ${count(row.header_buffers)}, Header absent ${count(row.header_absent_buffers)}, pre-start discarded frames ${count(row.pre_start_discarded_frames)}, observed Header discontinuities ${count(row.discontinuities)}, required metadata failures ${count(row.missing_metadata)}. Complete loss/xrun and physical A/V accuracy: unknown.`;
  }).join('\n');
}

export async function showMediaJobs({client, ui, current, status, chooseDestination, openResult}) {
  let cursor = '';
  for (;;) {
    if (!current()) return;
    const page = await client.mediaJobs(cursor);
    if (!current()) return;
    if (!page || !Array.isArray(page.items) || page.items.length > 50) throw new Error('Media jobs are unavailable.');
    if (!page.items.length) { status('No media jobs on this page.'); return; }
    const lines = page.items.map((item,i)=>`${i+1}. ${String(item.jobID).slice(0,32)} · ${String(item.state)}${item.recordingPhase ? ` · last observed: ${item.recordingPhase.replace('_',' ')}` : ''}${item.trimTiming ? ` · ${describeTrimTiming(item.trimTiming)}` : ''}${item.detached ? ' · detached' : ''}`);
    const choice = await ui.styledPrompt(`${lines.join('\n')}\n${page.nextCursor ? 'Type next for more.' : ''}`, {title:'Media jobs'});
    if (!current() || !choice) return;
    if (choice === 'next' && page.nextCursor) { cursor=page.nextCursor; continue; }
    const index = Number(choice)-1;
    const item = Number.isInteger(index) ? page.items[index] : null;
    if (!item || typeof item.jobID !== 'string') throw new Error('Choose a listed job.');
    const sheetProject=Array.isArray(item.result?.layout?.frames) && item.result.layout.frames.length===4 && !item.result.layout.coordinateFrame && !item.detached;
    const actions = (item.stopAvailable === true ? 'stop, ' : '') + (item.cancelAvailable === true ? 'status, cancel, reconcile, collect, save, or open' : 'status, reconcile, collect, save, or open') + (item.partialAvailable ? ', or save partial' : '') + (sheetProject ? ', or project' : '') + (item.recordingObservations || Array.isArray(item.audioTracks) ? ', or details' : '');
    const action = await ui.styledPrompt(`Type ${actions}.`, {title:'Media job action',defaultValue:'status'});
    if (!current() || !action) return;
    if (action === 'details' && (item.recordingObservations || Array.isArray(item.audioTracks))) {
      await ui.styledConfirm([item.recordingObservations ? describeRecordingObservations(item.recordingObservations) : '', Array.isArray(item.audioTracks) ? describeRecordingAudioTracks(item.audioTracks) : ''].filter(Boolean).join('\n'), {title:'Recording observations',confirmText:'Close'});
      continue;
    }
    if (action === 'save partial') {
      const pending=item.partialRecovery;
      if (pending && ['dispatched','unknown'].includes(pending.state)) {
        const result=await client.saveRecordingPartial({jobID:item.jobID,revision:item.revision,recoveryID:pending.recoveryID});
        if (current()) status(`Partial recovery: ${result.partialRecovery?.state || 'unknown'}.`);
        continue;
      }
      const destinationRef=await chooseDestination(); if (!current() || !destinationRef) return;
      const destinationName=await ui.styledPrompt('Name the incomplete recording copy (.partial).', {title:'Save partial recording',defaultValue:'recording.webm.partial'});
      if (!current() || !destinationName) return;
      const preview=await client.previewRecordingPartial({jobID:item.jobID,destinationRef,destinationName});
      if (!current()) return;
      if (preview?.artifact?.finalized !== false || !preview.recoveryID || preview.destination?.name !== destinationName) throw new Error('Partial recovery unavailable.');
      if (!await ui.styledConfirm(`Save incomplete bytes ${preview.artifact.sha256} to ${preview.destination.folder} / ${destinationName}? This is not a finalized recording.`, {title:'Save partial recording',confirmText:'Save partial'})) continue;
      if (!current()) return;
      try { const result=await client.saveRecordingPartial({jobID:item.jobID,revision:preview.revision,recoveryID:preview.recoveryID});
        if (current()) status(`Partial recovery: ${result.partialRecovery?.state || 'unknown'}.`);
      } catch (_) { if (current()) status('Partial recovery outcome unknown. Check the original recovery before another save.',true); }
      continue;
    }
    if (action === 'stop') {
      if (item.stopAvailable !== true) throw new Error('Stop is unavailable for this job.');
      const stopID = crypto.randomUUID();
      try { const result = await client.stopRecording({jobID:item.jobID,revision:item.revision,stopID});
        if (current()) status(`Recording ${item.jobID}: stop ${result.stopState || 'unknown'}; check status for finalization.`);
      } catch (_) { if (current()) status(`Recording ${item.jobID}: stop outcome unknown. Check status; do not resend.`,true); }
      continue;
    }
    if (action === 'cancel' && item.cancelAvailable !== true) throw new Error('Cancellation is unavailable for this job.');
    if (action === 'reconcile' && ['dispatched','unknown'].includes(item.recovery?.state)) {
      const result = await client.saveMediaRecovery({jobID:item.jobID,revision:item.revision,recoveryID:item.recovery.recoveryID});
      if (current()) status(`Media job ${item.jobID}: ${result.state}${result.reason ? ` (${result.reason})` : ''}`);
      continue;
    }
    if (action === 'open') {
      if (!item.result?.resourceRef) throw new Error('No saved Files result is available.');
      const recordingJobHint = item.state === 'complete' && item.outputMime === 'video/webm' && (item.audio === 'unavailable' || (['selected','off'].includes(item.audio) && item.audioSelection?.state === 'accepted' && Array.isArray(item.audioTracks))) && item.recordingObservations?.upstream_loss === 'unknown' && /^[a-f0-9]{32}$/.test(item.jobID) ? item.jobID : null;
      const imageMime=String(item.result.mimeType || '').toLowerCase();
      const eligibleImage=item.state === 'complete' && !item.detached && /^image[/](?:png|jpeg|gif|webp)$/.test(imageMime);
      const savedImageDigest=eligibleImage && /^[a-f0-9]{64}$/.test(item.result.sha256 || '') ? item.result.sha256 : null;
      if (eligibleImage && !savedImageDigest) throw new Error('The original saved image revision is unavailable.');
      await openResult(item.result.resourceRef,{recordingJobHint,savedImageDigest});
      return;
    }
    if (action === 'project' && sheetProject) {
      if(!await ui.styledConfirm('Bind this committed PNG to an Image Editor project? The PNG remains saved if project binding is unavailable.',{title:'Bind sprite project',confirmText:'Bind'}))continue;
      try {const result=await client.bindSpriteSheetProject(item.jobID); if(current())status(`Sprite project ${result.projectID} bound to the saved PNG.`);}
      catch (_) {if(current())status('PNG is committed; project binding is unavailable or requires reconciliation.',true);}
      continue;
    }
    if (action === 'save') {
      if (item.detached) throw new Error('Publication was withdrawn when the chat was deleted.');
      const mesh = item.outputMime === 'model/gltf-binary';
      const audio = item.outputMime === 'audio/flac';
      const outputLabel = item.outputMime === 'video/webm' ? 'WEBM' : mesh ? 'GLB' : audio ? 'FLAC' : 'PNG';
      const destinationRef = await chooseDestination();
      if (!current() || !destinationRef) return;
      const destinationName = await ui.styledPrompt(`Name the new ${outputLabel}.`, {title:'Save collected output',defaultValue:`generated.${outputLabel.toLowerCase()}`});
      if (!current() || !destinationName) return;
      const preview = await client.previewMediaRecovery({jobID:item.jobID,destinationRef,destinationName});
      if (!current()) return;
      if (preview?.jobID !== item.jobID || !Number.isSafeInteger(preview.revision) || typeof preview.recoveryID !== 'string'
          || preview.destination?.name !== destinationName || typeof preview.destination?.folder !== 'string'
          || typeof preview.artifact?.sha256 !== 'string') throw new Error('Recovery preview is unavailable.');
      const approved = await ui.styledConfirm(`Save retained ${outputLabel} ${preview.artifact.sha256} to ${preview.destination.folder} / ${preview.destination.name}?`,
        {title:'Save collected output',confirmText:'Save'});
      if (!current() || !approved) return;
      try {
        const result = await client.saveMediaRecovery({jobID:item.jobID,revision:preview.revision,recoveryID:preview.recoveryID});
        if (current()) status(`Media job ${item.jobID}: ${result.state}${result.reason ? ` (${result.reason})` : ''}`);
      } catch { if (current()) status(`Media job ${item.jobID}: save outcome unknown; reconcile the same recovery before another save.`,true); }
      continue;
    }
    if (!['status','cancel','reconcile','collect'].includes(action)) throw new Error('Unknown media action.');
    const result = await client.mediaAction(action,item.jobID);
    if (current()) status(`Media job ${item.jobID}: ${result.state}${result.reason ? ` (${result.reason})` : ''}${result.recordingPhase ? ` · ${result.recordingPhase.replaceAll('_',' ')}` : ''}${result.recordingObservations ? `\n${describeRecordingObservations(result.recordingObservations)}` : ''}`);
    if (current() && action==='status' && result.audioSelection) {
      status(`Recording ${item.jobID}: ${result.state}; audio selection ${result.audioSelection.state || 'unknown'}. Check the original job status; do not resend.${Array.isArray(result.audioTracks) ? `\n${describeRecordingAudioTracks(result.audioTracks)}` : ''}`);
    } else if (current() && action==='status' && result.recordingPhase==='audio_choices' && result.audioChoices) {
      await showRecordingAudioChoices({client,current,status,job:{...result,jobID:item.jobID}});
    }
  }
}


export async function runBlenderJobFlow({client, ui, sourceRef, sessionID, current, chooseDestination, status, fourHeadings=false, rigClip=false, materialBake=false}) {
  const check = () => { if (!current()) throw new Error('The Files or chat scope changed.'); };
  check();
  const operation = materialBake ? 'material.bake' : rigClip ? 'blender.rig_pose' : fourHeadings ? 'blender.four_headings' : 'blender.render';
  const item = buildMenu(await client.mediaProfiles({sessionID,operation}) .then(listing => ({operations:[{operation,label:listing?.profiles?.[0]?.operation_label,profiles:listing?.profiles||[],partial:listing?.partial}]})))[0];
  check();
  if (!item || item.state === 'unavailable') throw new Error(`${item?.label || operation} is unavailable: ${item?.reason || 'No configured profile can perform this operation.'}`);
  const selected = needsProfileQuestion(item) ? await ui.styledPrompt(profilePrompt(item),{title:'Choose a profile',defaultValue:'1'}) : null; check();
  if (needsProfileQuestion(item) && selected == null) return;
  const chosen = pickProfile(item, selected);
  const profile = { id: chosen.profile_id, label: chosen.label, form: chosen.form || {} };
  let selectedJoint=null, inspected=null;
  if(rigClip) {
    inspected=await client.prepareLocalOperation(operation,{sessionID,sourceRef},{inspect:true}); check();
    const choices=Array.isArray(inspected.joints)?inspected.joints:[];
    if(!choices.length) throw new Error('No supported weighted joint is available.');
    const joint=await ui.styledPrompt(choices.map((v,i)=>`${i+1}. ${v.jointName} (node ${v.nodeIndex}, parent ${v.parentNode ?? 'root'})`).join('\n'),{title:'Choose exact joint',defaultValue:'1'}); check(); if(joint==null)return;
    selectedJoint=choices[Number(joint)-1]; if(!selectedJoint)throw new Error('Choose a listed joint.');
  }
  let resolution=null;
  const choices=Array.isArray(profile.form.resolution_choices)?profile.form.resolution_choices:[];
  if(materialBake) {const value=await ui.styledPrompt('Choose '+choices.join(', ')+' pixels.',{title:'Bake resolution',defaultValue:String(choices[1]??choices[0]??'')});check();if(value==null)return;resolution=Number(value);if(!choices.includes(resolution)||String(resolution)!==String(value).trim())throw new Error('Choose a supported resolution.');}
  const destinationRef=await chooseDestination(); check(); if (!destinationRef) return;
  const output=profile.form.output||{noun:'file',default_name:'output'};
  const destinationName=await ui.styledPrompt('Name the new '+output.noun+'.',{title:'Output name',defaultValue:output.default_name}); check(); if (!destinationName) return;
  const prepared=await client.prepareLocalOperation(operation,{sessionID,connectionID:profile.id,sourceRef,destinationRef,destinationName,...(rigClip?{selected:selectedJoint}:{}),...(materialBake?{resolution}:{})}); check();
  if (prepared?.state !== 'prepared' || prepared.profile?.connectionID !== profile.id || prepared.destination?.name !== destinationName || !/^[a-f0-9]{64}$/.test(prepared.source?.sha256 || '')) throw new Error('The selection changed; start a new review.');
  if(materialBake && prepared.parameters?.resolution!==resolution)throw new Error('The selection changed; start a new review.');
  if(rigClip && (prepared.source.sha256!==inspected.sourceSHA256 || prepared.rig?.selected?.nodeIndex!==selectedJoint.nodeIndex || prepared.rig?.selected?.jointName!==selectedJoint.jointName || prepared.rig?.selected?.jointOrdinal!==selectedJoint.jointOrdinal || prepared.rig?.selected?.parentNode!==selectedJoint.parentNode)) throw new Error('The rig changed; start a new review.');
  const detail=`Profile: ${profile.label || profile.id} · revision ${prepared.profile.connectionRevision}\nSource: ${prepared.source.name} · SHA-256 ${prepared.source.sha256}${rigClip?`\nSelected joint: ${selectedJoint.jointName} · node ${selectedJoint.nodeIndex} · skin 0`:''}\nOperation: ${prepared.operation}\nDestination: ${prepared.destination.folder} / ${prepared.destination.name}\n\n${materialBake?'Bake base color, roughness and metallic maps using the existing UV layout?':'Render this image?'}`;
  if (!await ui.styledConfirm(detail,{title:materialBake?'Review material bake':'Review Blender render',confirmText:materialBake?'Bake':'Render'})) return; check();
  let result;
  try { result=await client.runMedia({jobID:prepared.jobID,revision:prepared.revision,parameters:materialBake?{resolution}:{}}); }
  catch (_) { result={state:'unknown_outcome'}; }
  check();
  status(`Media job ${prepared.jobID}: ${result.state}. Open Media jobs to refresh or collect.`);
}
