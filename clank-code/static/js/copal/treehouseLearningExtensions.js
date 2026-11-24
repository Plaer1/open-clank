import {searchShared,rootOrder,list as sharedList} from './treehouseDiscovery.js';
import {topicProgressLabel} from './treehouseTopicWeb.js';
import { registerTreeHouseView } from './treehouseViews.js';

const newId = () => globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`;
const list = value => Object.values(value || {});
function styles() {
  if (document.getElementById('treehouse-extensions-styles')) return;
  const link = document.createElement('link'); link.id = 'treehouse-extensions-styles'; link.rel = 'stylesheet';
  link.href = new URL('./treehouseLearningExtensions.css', import.meta.url).href; document.head.append(link);
}
function guard(context, action) {
  return async event => {
    const button = event?.currentTarget; if (button) button.disabled = true;
    try { await action(event); }
    catch (error) { context.setStatus(error?.message || 'Learning could not be saved. Reload and try again.', true); }
    finally { if (button?.isConnected) button.disabled = false; }
  };
}
function draftKey(c, id, kind) { return `treehouse-learning:${c.ui.snapshot.accountId}:${c.ui.snapshot.workspace}:${id}:${kind}`; }
function draftGet(key, initial = '') { try { return sessionStorage.getItem(key) ?? initial; } catch { return initial; } }
function draftSet(key, value) { try { if (value === null) sessionStorage.removeItem(key); else sessionStorage.setItem(key, value); } catch {} }
async function courseOpen(c, courseId, activityId = null) {
  try {
    // Contributions use catalogue CAS, so refresh the main command revision before learning.
    c.beforeNavigation?.();const account=c.ui.snapshot.accountId,workspace=c.ui.snapshot.workspace;
    const fresh = await c.api('/treehouse');
    if (fresh.accountId!==account||c.ui.snapshot.accountId!==account||fresh.workspace!==workspace||c.ui.snapshot.workspace!==workspace)return;
    c.ui.snapshot = fresh;c.discovery.invalidate();
    c.ui.selectedCourse = courseId; c.ui.playerItems ||= {};
    if (activityId) c.ui.playerItems[courseId] = activityId;
    c.persistContext();
    if(fresh.state.courses[courseId]?.status==='published'&&fresh.permissions.learner)await c.command('course.open',{courseId,...(activityId?{activityId}:{})});
    if(c.ui.snapshot.accountId!==account||c.ui.snapshot.workspace!==workspace)return;
    c.navigate('courses');
  } catch (error) { c.setStatus(error?.message || 'Learning could not be reopened.', true); }
}
function credentialCard(c, record, refresh) {
  const { h } = c;const scope=JSON.stringify([c.ui.snapshot.accountId,c.ui.snapshot.workspace]);const current=()=>scope===JSON.stringify([c.ui.snapshot.accountId,c.ui.snapshot.workspace]);
  const card = h('article', { class:`th-completion-record ${record.status}`, 'data-credential-id':record.id },
    h('small', { text:'OPEN CLANK TREEHOUSE · LEARNING RECORD' }), h('h3', { text:record.title }),
    h('p', { text:`Issued to ${record.learnerName}` }),
    h('p', { text:record.courseType === 'tutorial' ? 'Tutorial · content traversal' : 'Quest · required work' }),
    h('p', { text:record.reviewedAssessment ? 'Reviewed assessment evidence' : 'Completion according to the course requirements' }),
    h('strong', { text:`${record.status[0].toUpperCase()}${record.status.slice(1)}` }),
    h('p', { text:record.validityReason || 'Current requirements and evidence match this record.' }),
    h('small', { text:`${record.issuedAt} · ${record.issuerName}` }),
    h('small', { text:`Curriculum revision ${record.curriculumRevision} · completion rule ${record.completionRuleVersion} · progress generation ${record.resetGeneration}` }),
    h('p', { text:'An app learning record; no third-party accreditation or mastery claim.' }));
  const href = `/api/copal/treehouse/courses/${encodeURIComponent(record.courseId)}/credentials/${encodeURIComponent(record.id)}/export?workspace=${encodeURIComponent(c.ui.snapshot.workspace || 'default')}`;
  const actions = h('div', { class:'th-extension-actions' }, h('a', { class:'copal-btn', text:'Export readable HTML', href, download:`treehouse-${record.id}.html` }),
    h('a', { class:'copal-btn', text:'Export evidence JSON', href:`${href}&format=json`, download:`treehouse-${record.id}.json` }));
  if (c.courseCanEdit(record.courseId) && record.status !== 'revoked') actions.append(h('button', { class:'copal-btn', text:'Revoke record', onclick:() => c.openForm('Revoke completion record', [{ id:'reason', label:'Reason', type:'textarea' }], 'Revoke', async ({reason}) => {
    if(!current())return;
    const latest = await c.api(`/treehouse/courses/${encodeURIComponent(record.courseId)}/learning`);if(!current())return;
    await c.api(`/treehouse/courses/${encodeURIComponent(record.courseId)}/learning`, { method:'POST', body:JSON.stringify({type:'credential.revoke',commandId:newId(),expectedRevision:latest.revision,payload:{credentialId:record.id,reason}}) });
    if(current())await refresh();
  }) }));
  card.append(actions); return card;
}

function renderLibrary(root,c) {
 styles();const {h,ui}=c;
 root.append(h('h1',{text:'Library'}));
 const search=h('input',{type:'search','aria-label':'Search learning',placeholder:'Search learning…',maxlength:240,value:ui.libraryQuery||''});search.value=ui.libraryQuery||'';
 const form=h('form',{class:'th-extension-actions'},search,h('button',{type:'submit',class:'copal-btn',text:'Search'}));
 form.addEventListener('submit',event=>{event.preventDefault();c.present({libraryQuery:search.value.trim()});});
 search.addEventListener('input',()=>{ui.libraryQuery=search.value;});root.append(form);
 const panel=h('section',{class:'th-extension-panel','aria-live':'polite'});root.append(panel);
 const load=async(fresh=false)=>{
  panel.replaceChildren(h('p',{role:'status',text:'Loading learning…'}));
  try {
   const data=await c.discovery.load({fresh});if(!data||!root.isConnected)return;
   const shared=data.sharedCatalogue,query=search.value.trim();panel.replaceChildren();
   const filter=h('div',{class:'th-discovery-roots',role:'group','aria-label':'Main Quest filter'},h('button',{class:'copal-btn',text:'All Main Quests','aria-pressed':String(!ui.libraryRoot),onclick:()=>c.present({libraryRoot:''})}));
   for(const main of sharedList(shared?.mainQuests).sort((a,b)=>rootOrder.indexOf(a.id)-rootOrder.indexOf(b.id)))filter.append(h('button',{class:'copal-btn',text:main.title,'aria-pressed':String(ui.libraryRoot===main.id),onclick:()=>c.present({libraryRoot:main.id})}));
   if(shared){panel.append(filter);const matches=searchShared(shared,query,ui.libraryRoot),grid=h('div',{class:'th-library-grid'});
    for(const quest of matches.quests)grid.append(h('article',{class:'copal-card th-library-course','data-shared-quest':quest.id},h('h2',{text:quest.title}),h('p',{text:quest.objective||''}),h('small',{text:quest.required?'Required Quest':'Elective Quest'}),h('p',{text:topicProgressLabel(quest.progress)}),h('button',{class:'copal-btn primary',text:quest.progress?.resumeActivityId?'Resume Quest':'Open Quest',onclick:guard(c,()=>c.openSharedQuest(quest.id))})));
    panel.append(grid);
    if(query){const lessons=h('div',{class:'th-library-lessons'});for(const lesson of matches.lessons){const quest=shared.quests.find(x=>x.id===lesson.courseId);lessons.append(h('article',{class:'copal-card','data-shared-lesson':lesson.id},h('small',{text:quest?.title||'Quest'}),h('h3',{text:lesson.title}),h('p',{text:lesson.outcome||''}),h('button',{class:'copal-btn',text:'Open lesson',onclick:guard(c,()=>c.openSharedQuest(lesson.courseId,lesson.id))})));}panel.append(lessons);}
    if(!matches.quests.length&&(!query||!matches.lessons.length))panel.append(h('p',{text:'No learning matches this search.'}));
    panel.append(h('small',{text:'Shared search uses lesson titles and outcomes.'}));
   }else panel.append(h('p',{role:'status',text:'Shared learning is unavailable. Private courses remain below.'}));
   const privateCourses=(data.courses||[]).filter(x=>(c.adminMode()||ui.snapshot.state.courses[x.id]?.status==='published')&&(!query||[x.title,x.description].some(t=>String(t||'').toLocaleLowerCase().includes(query.toLocaleLowerCase()))));
   const own=h('details',{class:'th-private-learning'},h('summary',{text:'Private courses'}));
   for(const course of privateCourses)own.append(h('article',{class:'copal-card'},h('h3',{text:course.title}),h('p',{text:course.description||''}),h('small',{text:course.courseType==='tutorial'?'Tutorial':'Quest'}),h('button',{class:'copal-btn',text:course.progress?.resumeActivityId?'Resume':'Open',onclick:guard(c,()=>courseOpen(c,course.id,course.progress?.resumeActivityId))})));
   if(!privateCourses.length)own.append(h('p',{text:query?'No private courses match.':'No accessible private courses.'}));panel.append(own);
  }catch(error){if(root.isConnected)panel.replaceChildren(h('p',{role:'alert',text:error.message}),h('button',{class:'copal-btn',text:'Retry',onclick:()=>load(true)}));}
 };void load();
}

function renderRecords(root,c) {
 styles();const {h,ui}=c;root.append(h('h1',{text:'Completion records'}),h('p',{text:'Records for your private courses.'}));
 const panel=h('section',{class:'th-extension-panel','aria-live':'polite'});root.append(panel);
 const scope=()=>JSON.stringify([c.ui.snapshot?.accountId,c.ui.snapshot?.workspace]);const origin=scope();
 const current=()=>root.isConnected&&scope()===origin;let sequence=0;
 const load=async(fresh=false)=>{
  const ticket=++sequence;panel.replaceChildren(h('p',{text:'Loading records…'}));
  try{const data=await c.discovery.load({fresh});if(!data||!current()||ticket!==sequence)return;panel.replaceChildren();
   const chooser=h('select',{'aria-label':'Private course'},h('option',{value:'',text:'Choose a private course…'}),...(data.courses||[]).map(x=>h('option',{value:x.id,text:x.title})));
   chooser.value=(data.courses||[]).some(x=>x.id===ui.recordCourse)?ui.recordCourse:'';
   const detail=h('section',{'aria-live':'polite'});panel.append(chooser,detail);
   const show=async()=>{
    const courseId=chooser.value,request=++sequence;detail.replaceChildren();if(!courseId)return;
    detail.append(h('p',{text:'Loading course record…'}));
    try{const info=await c.api(`/treehouse/courses/${encodeURIComponent(courseId)}/learning`);if(!current()||request!==sequence||chooser.value!==courseId)return;detail.replaceChildren();
     for(const record of info.credentials||[])detail.append(credentialCard(c,record,()=>load(true)));
     detail.append(h('p',{text:info.completion.eligible?'Required course work is complete.':'Complete this course’s required work to request a record.'}),h('button',{class:'copal-btn',text:'Request completion record',disabled:!info.completion.eligible,onclick:guard(c,async()=>{
      const latest=await c.api(`/treehouse/courses/${encodeURIComponent(courseId)}/learning`);if(!current()||chooser.value!==courseId)return;if(!latest.completion.eligible)throw Error('Course requirements changed. Reload the record.');
      await c.api(`/treehouse/courses/${encodeURIComponent(courseId)}/learning`,{method:'POST',body:JSON.stringify({type:'credential.issue',commandId:newId(),expectedRevision:latest.revision,payload:{}})});if(!current())return;c.discovery.invalidate();await load(true);
     })}));
    }catch(error){if(current()&&request===sequence)detail.replaceChildren(h('p',{role:'alert',text:error.message}),h('button',{class:'copal-btn',text:'Retry',onclick:show}));}
   };
   chooser.addEventListener('change',()=>{ui.recordCourse=chooser.value;c.recordNavigation();void show();});
   if(!data.credentials?.length)panel.append(h('p',{text:'No completion records yet.'}));void show();
  }catch(error){if(current())panel.replaceChildren(h('p',{role:'alert',text:error.message}),h('button',{class:'copal-btn',text:'Retry',onclick:()=>load(true)}));}
 };void load();
}
registerTreeHouseView('library',renderLibrary);
registerTreeHouseView('credentials',renderRecords);
