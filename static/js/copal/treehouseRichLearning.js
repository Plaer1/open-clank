import { registerTreeHouseActivity, registerTreeHouseView } from './treehouseViews.js';
import { registerTreeHouseAuthorFields } from './treehouseAuthoring.js';

const esc = value => JSON.stringify(value).replace(/</g, '\\u003c');
const path = (course, activity) => `/treehouse/courses/${encodeURIComponent(course.id)}/activities/${encodeURIComponent(activity.id)}`;
const guarded = (c, action) => async () => { try { await action(); } catch (e) { c.setStatus(e.message, true); } };

export function openTreeHouseAuthorAssistance(c,course,activity){
  if(!c.courseCanEdit(course.id))throw new Error('Author access required');
  c.openForm('Author with your configured Assistant',[
    {id:'task',label:'Task: rewrite, outline, quiz, or scenario',value:'rewrite'},
    {id:'instructions',label:'Author instructions (staged for review before sending)',type:'textarea',value:`Improve this lesson for ${course.title}. Return a draft; do not publish or change course state.`},
    {id:'draft',label:'Paste the reviewed Assistant lesson draft here to apply it; leave empty to prepare an Assistant request',type:'textarea',value:''}
  ],'Prepare request / apply reviewed draft',async v=>{
    if(!['rewrite','outline','quiz','scenario'].includes(v.task))throw new Error('Choose rewrite, outline, quiz, or scenario');
    if(v.draft.trim()){
      if(v.task!=='rewrite')throw new Error('Outline, quiz and scenario drafts must be reviewed in their corresponding author editors; only lesson rewrites apply here.');
      if(v.draft.length>100000)throw new Error('Draft exceeds the lesson content limit');
      const result=await c.command('activity.update',{activityId:activity.id,content:v.draft});if(!result)throw new Error('Reviewed draft was not applied; reload current author access and revision.');c.setStatus('Reviewed lesson draft applied through the normal author command.');return;
    }
    const response=await fetch('/api/default-chat',{credentials:'same-origin',headers:{Accept:'application/json'}});
    if(!response.ok)throw new Error('Configured Assistant availability could not be checked. Open Models and configure an authorized chat route.');
    const route=await response.json();if(!route.model)throw new Error('Author assistance unavailable: no configured default chat model. Open Models to configure an account.');
    const {stageAssistantContext}=await import('../contextualHelp.js');
    stageAssistantContext({accountId:c.ui.snapshot.accountId,workspace:c.ui.snapshot.workspace||'default',view:'treehouse',surface:'teaching',resourceKind:'treehouse-lesson',resourceId:activity.id,courseId:course.id,lessonId:activity.id,lessonTitle:activity.title,taskQuery:`Author task: ${v.task}. ${v.instructions}`.slice(0,512)});
    const composer=document.getElementById('message');if(!composer)throw new Error('Assistant composer unavailable; reopen the Assistant.');
    composer.value=`Treehouse author task: ${v.task}\n${v.instructions}\nCourse: ${course.title}\nLesson: ${activity.title}\nSource lesson draft (review before sending):\n${String(activity.content||'').slice(0,16000)}\nReturn a draft for my review. Do not publish or mutate the course.`;
    composer.dispatchEvent(new Event('input',{bubbles:true}));composer.focus();c.setStatus('Author request staged in the configured Assistant. Review and send there; use its Cancel control to stop. Paste a reviewed rewrite here to apply.');
  });
}

// Executed only in an opaque sandboxed document and its dedicated Worker.
// Browser results are practice feedback, never trusted grading evidence.
export function runJavaScriptPractice(source, container, onResult) {
  if (typeof source !== 'string' || source.length > 16000) throw new Error('JavaScript source exceeds 16000 characters');
  const nonce = crypto.randomUUID(); let done = false;
  const frame = document.createElement('iframe'); frame.hidden = true;
  frame.setAttribute('sandbox', 'allow-scripts'); frame.setAttribute('referrerpolicy', 'no-referrer'); frame.title = 'Isolated JavaScript practice';
  const workerSource = `for (const name of ['fetch','XMLHttpRequest','WebSocket','EventSource','Worker','SharedWorker','importScripts']) { try { Object.defineProperty(self,name,{value:undefined,writable:false,configurable:false}); } catch (_) {} }
self.onmessage = async e => { try { const result = await new Function('"use strict";\\n' + e.data)(); const output = JSON.stringify(result === undefined ? null : result); if (output.length > 8000) throw new Error('Result exceeds 8000 characters'); self.postMessage({status:'result',output}); } catch (error) { self.postMessage({status:'error',output:String(error.message || error).slice(0,2000)}); } };`;
  frame.srcdoc = `<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline' 'unsafe-eval' blob:; worker-src blob:; connect-src 'none'; img-src 'none'; media-src 'none'; frame-src 'none'; form-action 'none'; base-uri 'none'"><script>const worker = new Worker(URL.createObjectURL(new Blob([${esc(workerSource)}],{type:'text/javascript'}))); worker.onmessage = e => { parent.postMessage({nonce:${esc(nonce)},...e.data},'*'); worker.terminate(); }; worker.onerror = () => { parent.postMessage({nonce:${esc(nonce)},status:'error',output:'Browser worker unavailable'},'*'); worker.terminate(); }; worker.postMessage(${esc(source)});<\/script>`;
  const finish = result => { if (done) return; done = true; clearTimeout(timer); window.removeEventListener('message', receive); frame.remove(); onResult(result); };
  const receive = event => { if (event.source === frame.contentWindow && event.data?.nonce === nonce && ['result','error'].includes(event.data.status)) finish({status:event.data.status,output:String(event.data.output).slice(0,8000)}); };
  window.addEventListener('message', receive);
  const timer = setTimeout(() => finish({status:'timeout',output:'Stopped after the 2 second execution deadline.'}), 2000);
  container.append(frame);
  return () => finish({status:'cancelled',output:'Execution cancelled.'});
}

function renderCode(root, c) {
  const {h,activity} = c, adapter = activity.adapter || {}; let cancel;
  const source = h('textarea', {'aria-label':'JavaScript practice source',rows:'9',maxlength:'16000',value:adapter.source || 'return 2 + 2;'});
  const output = h('pre', {'aria-live':'polite',text:'JavaScript only. Return a JSON serializable value. Runs in your browser for at most 2 seconds; no network or host commands. Memory cannot be hard capped. Practice results do not award mastery.'});
  const stop = h('button',{class:'copal-btn',text:'Cancel',disabled:true,onclick:()=>cancel?.()});
  const run = h('button',{class:'copal-btn primary',text:'Run JavaScript',onclick:()=>{
    cancel?.(); run.disabled=true; stop.disabled=false; output.textContent='Running…';
    cancel=runJavaScriptPractice(source.value,root,result=>{run.disabled=false;stop.disabled=true;output.textContent=`${result.status}: ${result.output}`;
      if(result.status==='result' && adapter.expectedOutput !== undefined) output.textContent+=result.output===adapter.expectedOutput?'\nMatches the authored expected JSON.':'\nDoes not match the authored expected JSON.';
    });
  }});
  const observer=new MutationObserver(()=>{if(!root.isConnected){cancel?.();observer.disconnect();}});observer.observe(document.body,{childList:true,subtree:true});
  root.append(source,h('div',{},run,stop),output);
}

function renderInteractive(root,c){
  const {h,activity}=c,a=activity.adapter||{},result=h('p',{'aria-live':'polite',text:'Choose an answer to check this local practice. Results do not count as verified assessment.'});
  root.append(h('p',{text:a.prompt||'No interactive definition available.'}));
  for(const [index,choice] of (a.choices||[]).entries()) root.append(h('button',{class:'copal-btn',text:choice,onclick:()=>{result.textContent=(index===a.answerIndex?'Correct. ':'Try another answer. ')+(a.feedback||'');}}));
  root.append(result);
}

export async function wireTreeHouseMediaResume(media,c){
  const {course,activity,h}=c; if(!course||!activity)return;
  const base=path(course,activity),status=h('p',{'aria-live':'polite',text:'Loading playback position…'});
  media.after(status); let revision=0,ready=false,saving=false,pending=false,failed=false,lastSave=0;
  try {const state=await c.api(`${base}/playback`); if(!media.isConnected)return;revision=state.revision;
    const resume=()=>{if(state.position>0&&Number.isFinite(media.duration)) media.currentTime=Math.min(state.position,Math.max(0,media.duration-0.1));};
    if(media.readyState>=1)resume();else media.addEventListener('loadedmetadata',resume,{once:true});ready=true;status.textContent=state.position>0?`Resume saved at ${Math.floor(state.position)} seconds.`:'Playback position saves on pause and every 10 seconds.';
  } catch(e){status.textContent=`Resume unavailable: ${e.message}`;failed=true;}
  const save=async()=>{if(!ready||failed||!media.isConnected)return;if(saving){pending=true;return;}saving=true;
    try {const result=await c.api(`${base}/playback`,{method:'PUT',body:JSON.stringify({expectedRevision:revision,position:media.currentTime})});revision=result.revision;status.textContent=`Position saved at ${Math.floor(result.position)} seconds.`;}
    catch(e){failed=true;status.textContent=`Position not saved: ${e.message}. Reopen this episode to reload.`;}
    finally {saving=false;if(pending){pending=false;void save();}}
  };
  media.addEventListener('pause',()=>void save());media.addEventListener('ended',()=>void save());
  media.addEventListener('timeupdate',()=>{if(Date.now()-lastSave>10000){lastSave=Date.now();void save();}});
  const caption=activity.adapter?.captionOperationId;
  if(caption){try{const r=await c.api(`${base}/resources/${encodeURIComponent(caption)}`);if(!media.isConnected)return;if(r.mimeType!=='text/vtt')throw new Error('Prepared caption must be text/vtt (WebVTT).');
    const track=h('track',{kind:'captions',label:activity.adapter.captionLabel||'Captions',srclang:activity.adapter.captionLanguage||'en',src:`/api/files-v1/content?purpose=preview&resource_ref=${encodeURIComponent(r.resourceRef)}`,default:true});media.append(track);
    track.addEventListener('error',()=>{status.textContent='Captions unavailable; Files access or WebVTT loading failed.';});
  }catch(e){status.textContent=`Captions unavailable: ${e.message}`;}}
}

function podcastEditor(c,course,current,refresh){
  const audio=Object.values(c.ui.snapshot.state.activities||{}).filter(a=>a.courseId===course.id&&a.activityType==='audio'&&!a.deletedAt);
  c.openForm('Organize podcast',[{id:'title',label:'Podcast title',value:current?.title||course.title},{id:'description',label:'Description',type:'textarea',value:current?.description||''},
    {id:'episodes',label:'Ordered episodes JSON: activityId, operationId, title, description',type:'textarea',value:JSON.stringify(current?.episodes||audio.filter(a=>a.sourceAttachments?.length).map(a=>({activityId:a.id,operationId:a.sourceAttachments[0].operationId,title:a.title,description:''})),null,2)},
    {id:'published',label:'Publication: draft or published',value:current?.published?'published':'draft'}], 'Save podcast', async v=>{
    if(!['draft','published'].includes(v.published))throw new Error('Use draft or published');
    const fresh=await c.api(`/treehouse/courses/${encodeURIComponent(course.id)}/podcast`);
    await c.api(`/treehouse/courses/${encodeURIComponent(course.id)}/podcast`,{method:'PUT',body:JSON.stringify({expectedRevision:fresh.revision,podcast:{title:v.title,description:v.description,published:v.published==='published',episodes:JSON.parse(v.episodes)}})});await refresh();
  });
}

export function renderTreeHousePodcasts(root,c){
  const {h}=c,search=h('input',{type:'search','aria-label':'Search podcasts',placeholder:'Discover accessible podcasts…',maxlength:'240'}),panel=h('div',{'aria-live':'polite'});let sequence=0;
  root.append(h('h2',{text:'Podcasts'}),h('p',{text:'Episodes inherit course access and current Files permissions. Publication makes episodes discoverable to course recipients.'}),search,panel);
  const load=async()=>{const serial=++sequence,data=await c.api(`/treehouse/podcasts?q=${encodeURIComponent(search.value)}`);if(serial!==sequence||!root.isConnected)return;panel.replaceChildren();
    if(!data.podcasts.length)panel.append(h('p',{text:'No accessible podcasts match.'}));
    for(const podcast of data.podcasts){const card=h('article',{class:'copal-treehouse-card'},h('h3',{text:podcast.title}),h('p',{text:podcast.description}),h('small',{text:podcast.published?'Published to course recipients':'Author draft'}));
      if(c.courseCanEdit(podcast.courseId))card.append(h('button',{class:'copal-btn',text:'Organize episodes',onclick:()=>podcastEditor(c,c.ui.snapshot.state.courses[podcast.courseId],podcast,load)}));
      for(const ep of podcast.episodes){const row=h('section',{},h('h4',{text:ep.title}),h('p',{text:ep.description}));row.append(h('button',{class:'copal-btn',text:'Open episode',onclick:guarded(c,async()=>{
        const course=c.ui.snapshot.state.courses[podcast.courseId],activity=c.ui.snapshot.state.activities[ep.activityId];if(!course||!activity)throw new Error('Reload Treehouse to retrieve this episode.');
        const resource=await c.api(`${path(course,activity)}/resources/${encodeURIComponent(ep.operationId)}`);if(!resource.mimeType?.startsWith('audio/'))throw new Error('Episode source is not supported audio.');
        const media=h('audio',{controls:true,preload:'metadata',src:`/api/files-v1/content?purpose=preview&resource_ref=${encodeURIComponent(resource.resourceRef)}`});row.querySelector('audio')?.pause();row.querySelector('audio')?.remove();row.append(media);await wireTreeHouseMediaResume(media,{...c,course,activity});
      })}));card.append(row);}panel.append(card);}
    for(const course of Object.values(c.ui.snapshot.state.courses||{}).filter(x=>c.courseCanEdit(x.id)&&!x.deletedAt&&!data.podcasts.some(p=>p.courseId===x.id)))panel.append(h('button',{class:'copal-btn',text:`Create podcast for ${course.title}`,onclick:()=>podcastEditor(c,course,null,load)}));
  };search.addEventListener('change',guarded(c,load));void load().catch(e=>{panel.textContent=e.message;});
}

registerTreeHouseActivity('code',renderCode);registerTreeHouseActivity('interactive',renderInteractive);registerTreeHouseView('podcasts',renderTreeHousePodcasts);
registerTreeHouseAuthorFields('code','JavaScript practice',({h,activity})=>{const a=activity.adapter||{},source=h('textarea',{'aria-label':'JavaScript function body',rows:'8',value:a.source||'return 2 + 2;'}),expected=h('input',{'aria-label':'Expected JSON output (optional)',value:a.expectedOutput||''});return{node:h('div',{},h('p',{text:'JavaScript only; browser practice with a 2 second deadline. Return JSON. Results are not trusted grades.'}),source,expected),value:()=>({language:'javascript',source:source.value,...(expected.value?{expectedOutput:expected.value}:{})})};});
registerTreeHouseAuthorFields('interactive','Choice practice',({h,activity})=>{const a=activity.adapter||{},prompt=h('textarea',{'aria-label':'Question',value:a.prompt||''}),choices=h('textarea',{'aria-label':'One choice per line',value:(a.choices||['Choice one','Choice two']).join('\n')}),answer=h('input',{type:'number',min:'0','aria-label':'Correct answer index (zero based)',value:String(a.answerIndex||0)}),feedback=h('textarea',{'aria-label':'Feedback',value:a.feedback||''});return{node:h('div',{},prompt,choices,answer,feedback),value:()=>({prompt:prompt.value,choices:choices.value.split('\n').filter(Boolean),answerIndex:Number(answer.value),feedback:feedback.value})};});
for(const type of ['audio','video'])registerTreeHouseAuthorFields(type,type==='audio'?'Audio':'Video',({h,activity})=>{const a=activity.adapter||{},caption=h('input',{'aria-label':'Prepared WebVTT caption operation ID (optional)',value:a.captionOperationId||''}),language=h('input',{'aria-label':'Caption language',value:a.captionLanguage||'en'}),label=h('input',{'aria-label':'Caption label',value:a.captionLabel||'Captions'});return{node:h('div',{},h('p',{text:'Attach a text/vtt caption resource through Files first, then enter its prepared operation ID. Portable imports require fresh media and caption preparation.'}),caption,language,label),value:()=>caption.value?{captionOperationId:caption.value,captionLanguage:language.value,captionLabel:label.value}:{}};});
