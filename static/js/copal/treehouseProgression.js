import { registerTreeHouseView } from './treehouseViews.js';
const values = object => Object.values(object || {});
function installStyles() {
  if (document.getElementById('treehouse-progression-styles')) return;
  const link = document.createElement('link'); link.id = 'treehouse-progression-styles'; link.rel = 'stylesheet'; link.href = new URL('./treehouseProgression.css', import.meta.url).href; document.head.append(link);
}

// Shared T06 track contract. Every milestone is a projection-backed fact;
// order is presentation only and never adds prerequisite/reward rules.
export function renderTreeHouseMilestoneTrack(root, { h, milestones = [], title = 'Your milestones', description = 'Each milestone follows its own completion criteria.' } = {}) {
  installStyles();
  if (!milestones.length) return;
  const done = milestones.filter(item => item.complete).length;
  const track = h('section', { class:'th-milestone-track', 'aria-label':title }, h('header', {}, h('div', {}, h('span', { class:'th-eyebrow', text:'PROGRESSION' }), h('h2', { text:title }), h('p', { text:description })), h('strong', { text:`${done} / ${milestones.length}`, 'aria-label':`${done} of ${milestones.length} milestones reached` })));
  const list = h('ol', { class:'th-milestone-list', 'aria-label':'Milestone states', tabindex:'0' });
  const nextId = milestones.find(item => !item.complete && item.unlocked !== false)?.id;
  for (const [index,item] of milestones.entries()) {
    const state = item.complete ? 'achieved' : item.id === nextId ? 'next' : item.unlocked === false ? 'locked' : 'available';
    const node = h('li', { class:`th-milestone ${state}` }, h('span', { class:'th-milestone-marker', 'aria-hidden':'true', text:item.complete ? '✓' : String(index + 1).padStart(2,'0') }), h('strong', { text:item.label }), h('small', { text:item.complete ? 'Achieved' : state === 'next' ? 'Explore next' : state === 'locked' ? 'Requirements pending' : 'Available' }));
    if (item.progress != null) node.append(h('small', { text:`${item.progress}% complete` }));
    if (item.onOpen) node.append(h('button', { class:'copal-btn', text:'View', 'aria-label':`View ${item.label}`, onclick:item.onOpen }));
    list.append(node);
  }
  track.append(list); root.append(track);
}

export function treeHouseSkillLayout(skills) {
  const ids = new Set(skills.map(skill => skill.id)); const depths = new Map(); const visiting = new Set();
  const byId = new Map(skills.map(skill => [skill.id,skill]));
  const depth = id => {
    if (depths.has(id)) return depths.get(id);
    if (visiting.has(id)) return 0; // UI stays usable if an old corrupt record is read.
    visiting.add(id);
    const deps = (byId.get(id)?.prerequisiteIds || []).filter(key => ids.has(key));
    const value = deps.length ? 1 + Math.max(...deps.map(depth)) : 0;
    visiting.delete(id); depths.set(id,value); return value;
  };
  const rows = new Map();
  return skills.map(skill => { const column = depth(skill.id); const row = rows.get(column) || 0; rows.set(column,row + 1); return { skill, x:column * 246 + 14, y:row * 122 + 14 }; });
}

function skillState(skill, item = {}) {
  return item.unlocked === false ? 'locked' : item.level === 'master' ? 'mastered' : Number(item.points || 0) > 0 ? 'in-progress' : 'available';
}
function selectionDialog(context, { title, fields, choices = [], save }) {
  const { h } = context; const returnFocus = document.activeElement;
  const dialog = h('dialog', { class:'copal-dialog th-progression-dialog', 'aria-label':title }, h('h2', { text:title }));
  const controls = new Map();
  for (const spec of fields) {
    const input = spec.type === 'textarea' ? h('textarea', { rows:'4' }) : h('input', { type:spec.type || 'text', min:spec.min });
    input.value = spec.value ?? ''; controls.set(spec.key,input); dialog.append(h('label', {}, h('span', { text:spec.label }), input));
  }
  const selections = new Map();
  for (const choice of choices) {
    const group = h('fieldset', {}, h('legend', { text:choice.label })); const selected = new Set(choice.selected || []);
    const inputs = [];
    for (const item of choice.items) { const input = h('input', { type:'checkbox', value:item.id, checked:selected.has(item.id) }); inputs.push(input); group.append(h('label', { class:'th-choice' }, input, h('span', { text:item.title }))); }
    if (!inputs.length) group.append(h('p', { text:'Nothing to choose yet.' })); selections.set(choice.key,inputs); dialog.append(group);
  }
  const feedback = h('p', { role:'status' });
  const submit = h('button', { class:'copal-btn primary', text:'Save', onclick:async () => {
    submit.disabled = true;
    try { const data = Object.fromEntries([...controls].map(([key,input]) => [key,input.value])); for (const [key,inputs] of selections) data[key] = inputs.filter(input => input.checked).map(input => input.value); await save(data); dialog.close(); }
    catch (error) { feedback.textContent = error.message; submit.disabled = false; }
  } });
  dialog.append(feedback,h('div', { class:'copal-dialog-actions' }, h('button', { class:'copal-btn', text:'Cancel', onclick:() => dialog.close() }),submit));
  document.body.append(dialog); dialog.addEventListener('close', () => { dialog.remove(); returnFocus?.focus?.(); }); dialog.showModal();
}
function editSkill(context, skill = null) {
  const skills = values(context.ui.snapshot.state.skills).filter(item => !item.deletedAt && item.id !== skill?.id);
  selectionDialog(context, { title:skill ? `Edit skill · ${skill.title}` : 'Create skill', fields:[{key:'title',label:'Skill title',value:skill?.title},{key:'description',label:'Description',type:'textarea',value:skill?.description},{key:'masteryThreshold',label:'Points required to unlock dependent skills',type:'number',min:0,value:skill?.masteryThreshold ?? 60},{key:'evidencePoints',label:'Points for approved evidence',type:'number',min:0,value:skill?.evidencePoints ?? 25}], choices:[{key:'prerequisiteIds',label:'Prerequisite skills',items:skills,selected:skill?.prerequisiteIds}], save:data => context.command(skill ? 'skill.update' : 'skill.create', { ...data, masteryThreshold:Number(data.masteryThreshold), evidencePoints:Number(data.evidencePoints), ...(skill ? {skillId:skill.id} : {}) }) });
}
function editMission(context, mission = null) {
  const state = context.ui.snapshot.state;
  const title = item => ({ ...item, title:`${state.courses[item.courseId]?.title || 'Course'} · ${item.title}` });
  selectionDialog(context, { title:mission ? `Edit mission · ${mission.title}` : 'Create mission', fields:[{key:'title',label:'Mission title',value:mission?.title},{key:'description',label:'Description',type:'textarea',value:mission?.description},{key:'rewardPoints',label:'Completion reward points',type:'number',min:0,value:mission?.rewardPoints ?? 25}], choices:[{key:'activityIds',label:'Required activities',items:values(state.activities).filter(item => !item.deletedAt).map(title),selected:mission?.activityIds},{key:'assignmentIds',label:'Required reviewed tasks',items:values(state.assignments).filter(item => !item.deletedAt).map(title),selected:mission?.assignmentIds}], save:data => context.command(mission ? 'quest.update' : 'quest.create', {...data,rewardPoints:Number(data.rewardPoints),...(mission ? {questId:mission.id} : {})}) });
}
function editBadge(context,badge = null) {
  const state = context.ui.snapshot.state;
  const options = [{value:'points|',label:'Total learning points'},...values(state.skills).filter(item => !item.deletedAt).map(item => ({value:`skill|${item.id}`,label:`Skill · ${item.title}`})),...values(state.courses).filter(item => !item.deletedAt).map(item => ({value:`course|${item.id}`,label:`Course · ${item.title}`})),...values(state.quests).filter(item => !item.deletedAt).map(item => ({value:`quest|${item.id}`,label:`Mission · ${item.title}`}))];
  const criteria = badge?.criteria || {type:'points'};
  const target = criteria.skillId || criteria.courseId || criteria.questId || '';
  context.openForm(badge ? `Edit badge · ${badge.title}` : 'Create badge',[{id:'title',label:'Badge title',value:badge?.title},{id:'description',label:'Description',type:'textarea',value:badge?.description},{id:'target',label:'Completion criteria',type:'select',value:`${criteria.type}|${target}`,options},{id:'threshold',label:'Points needed (for a points or skill criterion)',type:'number',min:0,value:criteria.threshold ?? 100}], 'Save badge', data => { const [type,id] = data.target.split('|'); const criterion = {type}; if(type === 'points' || type === 'skill') criterion.threshold = Number(data.threshold); if(type === 'skill') criterion.skillId = id; if(type === 'course') criterion.courseId = id; if(type === 'quest') criterion.questId = id; return context.command(badge ? 'badge.update' : 'badge.create',{title:data.title,description:data.description,criteria:criterion,...(badge ? {badgeId:badge.id} : {})}); });
}
function removeObject(context,type,item) {
  return async () => { if(await context.styledConfirm(`Delete ${item.title}?`,{title:`Delete ${type}`,confirmText:'Delete',danger:true})) {try {await context.command(`${type}.delete`,{[`${type}Id`]:item.id});}catch(error){context.setStatus(error.message,true);}} };
}
function submitEvidence(context, skill) {
  context.openForm(`Show your skill · ${skill.title}`, [{id:'description',label:'What have you made or done?',type:'textarea',rows:6},{id:'sourceUrl',label:'Optional evidence link'}], 'Send for review', data => context.command('evidence.submit',{skillId:skill.id,...data}));
}
function openCourse(context, courseId, itemId = null) {
  context.ui.selectedCourse = courseId; context.ui.playerItems ||= {}; if (itemId) context.ui.playerItems[courseId] = itemId;
  context.navigate('courses');
}
function skillDetail(host, skill, context) {
  const { h,ui } = context; const state = ui.snapshot.state; const progress = context.learnerProjection(); const item = progress.skills?.[skill.id] || {};
  const approved = values(state.evidence).filter(evidence => evidence.skillId === skill.id && evidence.profileId === ui.actorId && evidence.status === 'approved');
  const prereqs = (skill.prerequisiteIds || []).map(id => state.skills[id]).filter(Boolean);
  const detail = h('section', { class:'th-skill-detail', 'aria-label':`${skill.title} skill details` }, h('span', { class:'th-eyebrow', text:'SKILL PATH' }), h('h2', { text:skill.title }), h('p', { text:skill.description || 'Build this skill through learning and reviewed work.' }), h('div', { class:'th-card-badges' }, h('span', { class:'th-state', text:skillState(skill,item).replace('-',' ') }), h('span', { class:'th-state', text:`${item.points || 0} points · ${item.level || 'novice'}` })), h('p', { class:'th-trust-note', text:approved.length ? `${approved.length} approved evidence submission${approved.length === 1 ? '' : 's'}. Learning points also include traversal; they are not a verified mastery score.` : 'Learning points include lesson activity. No reviewed skill evidence has been approved yet.' }));
  const requirements = h('div', { class:'th-skill-prerequisites' }, h('h3', { text:'Unlock requirements' }));
  if (!prereqs.length) requirements.append(h('p', { text:'Foundation skill · available from the start.' }));
  for (const dep of prereqs) { const dp = progress.skills?.[dep.id]?.points || 0; requirements.append(h('button', { class:'copal-btn', text:`${dep.title} · ${dp}/${dep.masteryThreshold ?? 60} points`, onclick:() => { ui.selectedSkill = dep.id; context.renderLoaded(); } })); }
  detail.append(requirements);
  if (ui.snapshot.permissions.learner && item.unlocked !== false) detail.append(h('button', { class:'copal-btn primary', text:'Submit skill evidence', onclick:() => submitEvidence(context,skill) }));
  if (context.adminMode()) detail.append(h('button', { class:'copal-btn', text:'Edit skill', onclick:() => editSkill(context,skill) }),h('button', {class:'copal-btn danger',text:'Delete skill',onclick:removeObject(context,'skill',skill)}));
  const related = h('section', {}, h('h3', { text:'Learn & practice' }));
  for (const activity of values(state.activities).filter(activity => activity.status === 'published' && !activity.deletedAt && activity.skillIds?.includes(skill.id))) related.append(h('button', { class:'copal-btn', text:`${state.courses[activity.courseId]?.title || 'Course'} · ${activity.title}`, onclick:() => openCourse(context,activity.courseId,activity.id) }));
  if (!related.childNodes.length || related.childNodes.length === 1) related.append(h('p', { text:'Your instructor can link lessons and missions to this skill.' }));
  detail.append(related);
  const evidence = h('section', {}, h('h3', { text:'Evidence' }));
  const visible = values(state.evidence).filter(record => record.skillId === skill.id && (record.profileId === ui.actorId || context.adminMode() && ui.snapshot.permissions.grade));
  for (const record of visible) {
    const row = h('article', { class:'th-evidence-row' }, h('strong', { text:record.status }), h('p', { text:record.description }), h('small', { text:record.reviewNote || record.note || '' }));
    if (context.adminMode() && ui.snapshot.permissions.grade && record.status === 'pending') for (const [decision,label] of [['approved','Approve'],['rejected','Request changes']]) row.append(h('button', { class:'copal-btn', text:label, onclick:() => context.openForm(`${label} evidence`,[{id:'note',label:'Feedback',type:'textarea'}],label,data => context.command('evidence.review',{evidenceId:record.id,decision,...data})) }));
    evidence.append(row);
  }
  if (!visible.length) evidence.append(h('p', { text:'No evidence submitted for this skill yet.' })); detail.append(evidence);
  const diagnostics = h('details', { class:'th-diagnostics' }, h('summary', { text:'Evidence references' }), h('ul', {}, (item.evidenceEventIds || []).map(id => h('li', { text:id })))); detail.append(diagnostics);
  host.replaceChildren(detail);
}
function renderSkills(root,context) {
  installStyles(); const { h,ui } = context; const state = ui.snapshot.state; const progress = context.learnerProjection();
  const skills = values(state.skills).filter(skill => !skill.deletedAt);
  const head = h('div', { class:'copal-treehouse-section-head' }, h('div', {}, h('span', { class:'th-eyebrow', text:'GROW YOUR CAPABILITIES' }), h('h1', { text:'Your skill paths' }), h('p', { text:'Follow prerequisites, practice in a Quest, and share evidence of what you can do.' })));
  if (context.adminMode()) head.append(h('button', { class:'copal-btn primary', text:'Create skill', onclick:() => editSkill(context) }),h('button', { class:'copal-btn', text:'Create mission', onclick:() => editMission(context) }),h('button', {class:'copal-btn',text:'Create badge',onclick:() => editBadge(context)})); root.append(head);
  const courses = values(state.courses).filter(course => course.status === 'published' && !course.deletedAt);
  renderTreeHouseMilestoneTrack(root, { h,title:'Learning milestones',description:'Tutorial traversal and Quest work have different completion criteria. These are your current saved course states.', milestones:courses.map(course => ({id:course.id,label:course.title,complete:progress.courses?.[course.id]?.complete,progress:progress.courses?.[course.id]?.percent || 0,onOpen:() => openCourse(context,course.id)})) });
  const viewModes = h('div', { class:'th-filter-group', role:'group', 'aria-label':'Skill display' }); ui.skillDisplay ||= 'graph';
  for (const [id,label] of [['graph','Skill graph'],['list','Skill list']]) viewModes.append(h('button', { class:`copal-btn${ui.skillDisplay === id ? ' primary' : ''}`,text:label,'aria-pressed':String(ui.skillDisplay === id),onclick:() => {ui.skillDisplay = id;context.renderLoaded();} })); root.append(viewModes);
  const layout = h('div', { class:'th-skill-workspace' }); const map = h('div', { class:ui.skillDisplay === 'graph' ? 'th-skill-graph' : 'th-skill-list', 'aria-label':'Skills and their prerequisite connections' });
  const openSkill = id => {ui.selectedSkill = id;context.renderLoaded();};
  const nodes = treeHouseSkillLayout(skills);
  if (ui.skillDisplay === 'graph' && nodes.length) {
    const width = Math.max(...nodes.map(node => node.x)) + 230; const height = Math.max(...nodes.map(node => node.y)) + 125;
    const plane = h('div', { class:'th-skill-plane',style:`width:${width}px;height:${height}px` });
    const svg = document.createElementNS('http://www.w3.org/2000/svg','svg'); svg.setAttribute('width',width);svg.setAttribute('height',height);svg.setAttribute('aria-hidden','true');
    for (const node of nodes) for (const depId of node.skill.prerequisiteIds || []) {
      const dep = nodes.find(item => item.skill.id === depId); if (!dep) continue;
      const path = document.createElementNS(svg.namespaceURI,'path'); const sx = dep.x + 204,sy = dep.y + 44,tx = node.x,ty = node.y + 44;
      path.setAttribute('d',`M${sx},${sy} C${sx + 28},${sy} ${tx - 28},${ty} ${tx},${ty}`); path.setAttribute('class',progress.skills?.[depId]?.points >= (dep.skill.masteryThreshold ?? 60) ? 'unlocked' : 'locked'); svg.append(path);
    }
    plane.append(svg);
    for (const node of nodes) { const item = progress.skills?.[node.skill.id] || {}; plane.append(h('button', { class:`th-skill-node ${skillState(node.skill,item)}${ui.selectedSkill === node.skill.id ? ' selected' : ''}`,style:`left:${node.x}px;top:${node.y}px`,'aria-label':`${node.skill.title}, ${skillState(node.skill,item)}, ${item.points || 0} points`,onclick:() => openSkill(node.skill.id) },h('span', { class:'th-step-state',text:item.unlocked === false ? '◇' : item.level === 'master' ? '✦' : '○' }),h('strong', {text:node.skill.title}),h('small', {text:`${item.points || 0} points · ${item.level || 'novice'}`}))); }
    map.append(plane);
  } else for (const skill of skills) { const item = progress.skills?.[skill.id] || {}; const prereq = (skill.prerequisiteIds || []).map(id => state.skills[id]?.title || 'Earlier skill'); map.append(h('button', { class:`th-skill-list-row ${skillState(skill,item)}`,onclick:() => openSkill(skill.id) },h('strong', {text:skill.title}),h('span', {text:`${item.points || 0} points · ${skillState(skill,item)}`}),h('small', {text:prereq.length ? `Requires ${prereq.join(', ')}` : 'Foundation'}))); }
  if (!skills.length) map.append(h('div', {class:'copal-empty',text:'No skill paths have been published yet. Your courses remain available in the learning hub.'}));
  const detail = h('aside', {class:'th-skill-detail-host'}); const selected = skills.find(skill => skill.id === ui.selectedSkill) || skills[0]; if (selected) skillDetail(detail,selected,context);
  layout.append(map,detail); root.append(layout);
  const badges = values(state.badges).filter(badge => !badge.deletedAt);
  if (badges.length) { const collection = h('section', {class:'th-missions'},h('h2', {text:'Learning badges'})); for (const badge of badges) { const earned = progress.badges?.some(receipt => receipt.badgeId === badge.id); const card = h('article', {class:`th-mission-card${earned ? ' achieved' : ''}`},h('span', {class:'th-state',text:earned ? 'Earned' : 'Requirements pending'}),h('h3', {text:badge.title}),h('p', {text:badge.description || 'Awarded when its learning criterion is met.'})); if(context.adminMode())card.append(h('button', {class:'copal-btn',text:'Edit badge',onclick:() => editBadge(context,badge)}),h('button', {class:'copal-btn danger',text:'Delete badge',onclick:removeObject(context,'badge',badge)})); collection.append(card); } root.append(collection); }
  const missions = values(state.quests).filter(mission => mission.status === 'active' && !mission.deletedAt);
  if (missions.length) {
    const section = h('section', {class:'th-missions'},h('h2', {text:'Missions within your Quests'}));
    for (const mission of missions) {
      const receipt = progress.quests?.find(item => item.questId === mission.id); const activityIds = mission.activityIds || [],assignmentIds = mission.assignmentIds || [];
      const allCourses = new Set([...activityIds.map(id => state.activities[id]?.courseId),...assignmentIds.map(id => state.assignments[id]?.courseId)].filter(Boolean));
      const card = h('article', {class:`th-mission-card${receipt ? ' achieved' : ''}`},h('div', {class:'th-card-badges'},h('span', {class:'th-state',text:receipt ? 'Completed' : 'Work required'}),h('span', {class:'th-state',text:`${mission.rewardPoints || 0} reward points`})),h('h3', {text:mission.title}),h('p', {text:mission.description || 'Complete its required activities and reviewed tasks.'}));
      const objectives = h('ul'); for (const id of activityIds) objectives.append(h('li', {text:`${progress.completedActivityIds?.includes(id) ? '✓' : '○'} ${state.activities[id]?.title || 'Activity'}`})); for (const id of assignmentIds) objectives.append(h('li', {text:`${progress.gradedSubmissionIds?.includes(`${id}:${ui.actorId}`) ? '✓' : '○'} ${state.assignments[id]?.title || 'Task'} · reviewed work`})); card.append(objectives);
      for (const id of allCourses) if (state.courses[id]?.status === 'published') card.append(h('button', {class:'copal-btn',text:`Open ${state.courses[id].title}`,onclick:() => openCourse(context,id)}));
      if (context.adminMode()) card.append(h('button', {class:'copal-btn',text:'Edit mission',onclick:() => editMission(context,mission)}),h('button', {class:'copal-btn danger',text:'Delete mission',onclick:removeObject(context,'quest',mission)})); section.append(card);
    } root.append(section);
  }
}
registerTreeHouseView('skills',renderSkills);
