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
    const fresh = await c.api('/treehouse');
    if (fresh.accountId !== c.ui.snapshot.accountId) return;
    c.ui.snapshot = fresh;
    c.ui.selectedCourse = courseId; c.ui.playerItems ||= {};
    if (activityId) c.ui.playerItems[courseId] = activityId;
    c.persistContext(); c.navigate('courses');
  } catch (error) { c.setStatus(error?.message || 'Learning could not be reopened.', true); }
}
function credentialCard(c, record, refresh) {
  const { h } = c;
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
    const current = await c.api(`/treehouse/courses/${encodeURIComponent(record.courseId)}/learning`);
    await c.api(`/treehouse/courses/${encodeURIComponent(record.courseId)}/learning`, { method:'POST', body:JSON.stringify({type:'credential.revoke',commandId:newId(),expectedRevision:current.revision,payload:{credentialId:record.id,reason}}) });
    await refresh();
  }) }));
  card.append(actions); return card;
}

function renderLibrary(root, c, credentialsOnly = false) {
  styles(); const { h } = c;
  const title = credentialsOnly ? 'Completion records' : 'Learning library';
  const toolbar = h('div', { class:'copal-treehouse-section-head' }, h('div', {}, h('h2', { text:title }),
    h('p', { text:credentialsOnly ? 'Records are bound to your committed course requirements and evidence. Reopen here to check their validity.' : 'Find accessible Tutorials and Quests, explore collections and pick up where you left off.' })));
  const panel = h('section', { class:'th-extension-panel', 'aria-live':'polite' });
  const search = h('input', { type:'search', 'aria-label':'Search accessible learning', placeholder:'Search titles and learning content…', maxlength:'240' });
  const kind = h('select', { 'aria-label':'Course type' }, h('option', { value:'', text:'Tutorials & Quests' }), h('option', {value:'tutorial',text:'Tutorials'}), h('option', {value:'quest',text:'Quests'}));
  let sequence = 0;
  const load = async () => {
    const token = ++sequence; panel.replaceChildren(h('p', { text:'Loading accessible learning…' }));
    const data = await c.api(`/treehouse/learning-library?q=${encodeURIComponent(search.value)}&courseType=${encodeURIComponent(kind.value)}`);
    if (token !== sequence || !root.isConnected) return;
    panel.replaceChildren();
    if (credentialsOnly) {
      if (!data.credentials.length) panel.append(h('p', { text:'No completion records yet. Complete a course, then request its record in Discuss & board.' }));
      for (const record of data.credentials) panel.append(credentialCard(c, record, load));
      return;
    }
    const grid = h('div', { class:'th-library-grid' });
    for (const course of data.courses) {
      const progress = course.progress;
      grid.append(h('article', {class:'copal-card th-library-course'}, h('small', {text:course.courseType === 'tutorial' ? 'TUTORIAL · CLICK-THROUGH CONTENT' : 'QUEST · REQUIRED WORK'}),
        h('h3', {text:course.title}), h('p', {text:course.description}),
        h('p', {text:`${progress.state || 'available'} · ${progress.percent || 0}% · ${progress.availability?.status || 'available'}`}),
        h('button', {class:'copal-btn primary',text:progress.resumeActivityId ? 'Resume learning' : 'Open course',onclick:() => courseOpen(c,course.id,progress.resumeActivityId)})));
    }
    panel.append(h('h3', {text:`${data.courses.length} accessible courses`}), grid);
    if (!data.courses.length) panel.append(h('p', {text:'No accessible courses match this search.'}));
    panel.append(h('h3', {text:'Collections'}));
    for (const collection of data.collections) {
      const row = h('article', {class:'copal-card th-collection'}, h('h4', {text:collection.title}), h('p', {text:collection.description}));
      for (const id of collection.courseIds) {
        const course = c.ui.snapshot.state.courses[id]; if (!course) continue;
        row.append(h('button', {class:'copal-btn',text:course.title,onclick:() => courseOpen(c,id,c.learnerProjection().courses?.[id]?.resumeActivityId)}));
      }
      if (!collection.courseIds.length) row.append(h('p', {text:'This collection is empty.'}));
      if (collection.canEdit) row.append(h('button', {class:'copal-btn',text:'Edit collection',onclick:() => editCollection(collection,data.ownerRevision)}),
        h('button', {class:'copal-btn',text:'Archive collection',onclick:guard(c, async () => {
          await c.api('/treehouse/collections', {method:'POST',body:JSON.stringify({type:'collection.archive',commandId:newId(),expectedRevision:data.ownerRevision,payload:{id:collection.id}})}); await load();
        })}));
      panel.append(row);
    }
    if (!data.collections.length) panel.append(h('p', {text:'No collections yet.'}));
    if (c.adminMode()) panel.append(h('button', {class:'copal-btn',text:'Create collection',onclick:() => editCollection(null,data.ownerRevision)}));
  };
  const editCollection = (existing, revision) => {
    const own = list(c.ui.snapshot.state.courses).filter(course => course.ownerId === c.ui.snapshot.accountId && !course.deletedAt);
    const fields = [{id:'title',label:'Collection title',value:existing?.title || ''},{id:'description',label:'Description',type:'textarea',value:existing?.description || ''},
      ...own.map(course => ({id:course.id,label:course.title,type:'checkbox',value:existing?.courseIds.includes(course.id) || false}))];
    c.openForm(existing ? 'Edit collection' : 'Create collection', fields, 'Save collection', async values => {
      await c.api('/treehouse/collections',{method:'POST',body:JSON.stringify({type:'collection.save',commandId:newId(),expectedRevision:revision,payload:{...(existing ? {id:existing.id} : {}),title:values.title,description:values.description,courseIds:own.filter(course => values[course.id]).map(course => course.id)}})}); await load();
    });
  };
  toolbar.append(h('button', {class:'copal-btn',text:'Reload',onclick:guard(c,load)})); root.append(toolbar);
  if (!credentialsOnly) {
    const form = h('form',{class:'th-extension-actions'},search,kind,h('button',{class:'copal-btn',type:'submit',text:'Search'}));
    form.addEventListener('submit', event => {event.preventDefault(); void guard(c,load)();}); root.append(form);
  }
  root.append(panel); void guard(c,load)();
}

function renderCollaboration(root, c) {
  styles(); const {h} = c;
  const courses = list(c.ui.snapshot.state.courses).filter(course => course.status === 'published' && !course.deletedAt);
  const chooser = h('select', {'aria-label':'Discussion course'},h('option',{value:'',text:'Choose a course…'}),...courses.map(course => h('option',{value:course.id,text:course.title})));
  chooser.value = courses.some(course => course.id === c.ui.selectedCourse) ? c.ui.selectedCourse : '';
  const panel = h('section',{class:'th-extension-panel','aria-live':'polite'});
  root.append(h('h2',{text:'Discuss & board'}),h('p',{text:'A course discussion and shared learning board. Reload to receive others’ changes; saving checks the shared revision. Your draft stays here if a conflict occurs.'}),chooser,panel);
  let sequence = 0;
  const load = async () => {
    const courseId = chooser.value; const token = ++sequence;
    if (!courseId) {panel.replaceChildren(h('p',{text:'Choose an accessible course to collaborate.'}));return;}
    const data = await c.api(`/treehouse/courses/${encodeURIComponent(courseId)}/learning`);
    if (token !== sequence || !root.isConnected || chooser.value !== courseId) return;
    panel.replaceChildren();
    const postKey = draftKey(c,courseId,'post'); const boardKey = draftKey(c,courseId,'board');
    const save = async (type,payload) => {
      await c.api(`/treehouse/courses/${encodeURIComponent(courseId)}/learning`,{method:'POST',body:JSON.stringify({type,payload,commandId:newId(),expectedRevision:data.revision})});
      c.setStatus('Learning contribution saved');
    };
    panel.append(h('div',{class:'th-extension-actions'},h('button',{class:'copal-btn',text:'Reload / reconnect',onclick:guard(c,load)}),h('span',{text:`Shared revision ${data.revision} · manual refresh`})));
    const posts = h('div',{class:'th-discussion-posts'});
    for (const post of data.posts) {
      const card = h('article',{class:`th-discussion-post ${post.parentId ? 'reply' : ''}`,'data-post-id':post.id},
        h('small',{text:`${post.authorId} · ${post.createdAt}${post.parentId ? ' · reply' : ''}`}),h('p',{text:post.body}));
      if (post.parentId) card.append(h('small',{text:`In reply to ${data.posts.find(item => item.id === post.parentId)?.authorId || 'a contribution'}`}));
      for (const attachment of post.attachments || []) {
        const activity = c.ui.snapshot.state.activities[attachment.activityId];
        card.append(h('button',{class:'copal-btn',text:`Open Files-backed lesson: ${activity?.title || 'resource'}`,onclick:() => courseOpen(c,courseId,attachment.activityId)}));
      }
      if (!post.removedAt) {
        const actions = h('div',{class:'th-extension-actions'});
        for (const reaction of ['helpful','thanks','question']) actions.append(h('button',{class:`copal-btn${post.reactions[reaction]?.mine ? ' primary' : ''}`,text:`${reaction} ${post.reactions[reaction]?.count || 0}`,onclick:guard(c,async () => {await save('discussion.react',{postId:post.id,reaction});await load();})}));
        actions.append(h('button',{class:'copal-btn',text:'Reply',onclick:() => c.openForm('Reply to contribution',[{id:'body',label:'Reply',type:'textarea'}],'Post reply',async ({body}) => {await save('discussion.post',{body,parentId:post.id});await load();})}));
        if (post.canRemove) actions.append(h('button',{class:'copal-btn',text:data.canModerate ? 'Moderate / remove' : 'Remove my post',onclick:guard(c,async () => {await save('discussion.remove',{postId:post.id});await load();})}));
        card.append(actions);
      }
      posts.append(card);
    }
    if (!data.posts.length) posts.append(h('p',{text:'Start the course conversation.'}));
    const body = h('textarea',{rows:'4','aria-label':'Discussion contribution',placeholder:'Share a question, insight or useful resource…',maxlength:'12000'}); body.value = draftGet(postKey);
    body.addEventListener('input',()=>draftSet(postKey,body.value));
    const attachment = h('select',{'aria-label':'Files-backed lesson attachment'},h('option',{value:'',text:'No attachment'}),...data.attachmentActivities.map(item => h('option',{value:item.id,text:item.title})));
    panel.append(h('h3',{text:'Course discussion'}),posts,body,attachment,
      h('small',{text:'Resources are attached through Files to a published lesson first. A contribution references that lesson; Files keeps its own read permissions.'}),
      h('button',{class:'copal-btn primary',text:'Post contribution',onclick:guard(c,async () => {await save('discussion.post',{body:body.value,...(attachment.value ? {attachmentActivityIds:[attachment.value]} : {})});draftSet(postKey,null);await load();})}));
    const board = h('textarea',{rows:'10','aria-label':'Shared learning board',maxlength:'40000'});board.value=draftGet(boardKey,data.board.body);
    board.addEventListener('input',()=>draftSet(boardKey,board.value));
    panel.append(h('h3',{text:'Shared learning board'}),h('p',{text:`Board revision ${data.board.revision}. Save replaces the shared text; conflicting drafts are preserved for manual merging.`}),board,
      h('div',{class:'th-extension-actions'},h('button',{class:'copal-btn primary',text:'Save board',onclick:guard(c,async () => {await save('board.save',{body:board.value,boardRevision:data.board.revision});draftSet(boardKey,null);await load();})}),
        h('button',{class:'copal-btn',text:'Use latest board text',onclick:() => {board.value=data.board.body;draftSet(boardKey,null);}})));
    const history=h('details',{},h('summary',{text:`Board history (${data.board.history.length} revisions)`}));
    for (const item of [...data.board.history].reverse().slice(0,20)) history.append(h('article',{class:'th-board-history'},h('small',{text:`Revision ${item.revision} · replaced ${item.replacedAt}`}),h('pre',{text:item.body || '(empty)'})));
    panel.append(history,h('h3',{text:'Your completion record'}));
    for (const record of data.credentials) panel.append(credentialCard(c,record,load));
    panel.append(h('p',{text:data.completion.eligible ? 'Published course requirements are complete. You can request an evidence-bound learning record.' : 'Complete the published course requirements to request a record. Opening content alone does not qualify.'}),
      h('button',{class:'copal-btn',text:'Request completion record',disabled:!data.completion.eligible,onclick:guard(c,async () => {await save('credential.issue',{});await load();})}));
  };
  chooser.addEventListener('change',()=>{c.ui.selectedCourse=chooser.value;c.persistContext();void guard(c,load)();});
  void guard(c,load)();
}
registerTreeHouseView('library',(root,c)=>renderLibrary(root,c));
registerTreeHouseView('credentials',(root,c)=>renderLibrary(root,c,true));
registerTreeHouseView('collaboration',renderCollaboration);
