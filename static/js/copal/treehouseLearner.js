import { renderTreeHouseActivity } from './treehouseViews.js';

const list = value => Object.values(value || {});
export function treeHouseCourseKind(course, progress) {
  return progress?.courseType || course?.courseType || 'quest';
}
export function treeHouseCourseItems(state, course) {
  return (course.moduleIds || []).flatMap(id => {
    const module = state.modules[id];
    if (!module || module.deletedAt) return [];
    return [...(module.activityIds || []).map(key => ({ ...state.activities[key], itemKind:'activity', module })),
      ...(module.assignmentIds || []).map(key => ({ ...state.assignments[key], itemKind:'assignment', module }))]
      .filter(item => item.id && !item.deletedAt && item.status === 'published');
  });
}

export function renderTreeHouseLearner(root, context) {
  const { h, ui, command, setStatus, learnerProjection, persistContext, renderLoaded, currentEnrollment } = context;
  const state = ui.snapshot.state; const progress = learnerProjection();
  const courses = list(state.courses).filter(course => course.status === 'published' && !course.deletedAt);
  const run = action => async event => {
    const button = event?.currentTarget, playerBody = ui.body, focused = document.activeElement;
    const accountId = ui.snapshot?.accountId;
    const hadFocus = !!playerBody?.contains(focused);
    let focusMoved = false;
    const onFocus = movement => { if (![focused,button,playerBody,document.body,document.documentElement].includes(movement.target)) focusMoved = true; };
    const onPointer = movement => { if (movement.target !== button && !button?.contains(movement.target)) focusMoved = true; };
    const onBlur = () => { focusMoved = true; };
    const focusGuard = () => hadFocus && !focusMoved && ui.learnerFocusGuard === focusGuard && ui.body === playerBody && ui.snapshot?.accountId === accountId && playerBody?.isConnected && playerBody.getClientRects().length > 0 && document.visibilityState === 'visible' && document.hasFocus() && [focused,button,playerBody,document.body,document.documentElement].includes(document.activeElement);
    ui.learnerFocusGuard = focusGuard;
    document.addEventListener('focusin',onFocus,true);
    document.addEventListener('pointerdown',onPointer,true);
    window.addEventListener('blur',onBlur);
    if (button) button.disabled = true;
    try { await action(event); }
    catch (error) { setStatus(error?.message || 'Learning action could not be saved. Try again.', true); }
    finally {
      document.removeEventListener('focusin',onFocus,true);
      document.removeEventListener('pointerdown',onPointer,true);
      window.removeEventListener('blur',onBlur);
      if (button?.isConnected) button.disabled = false;
      if (focusGuard()) {
        const target = button?.isConnected && !button.disabled ? button : playerBody;
        target.focus({preventScroll:true});
      }
      if (ui.learnerFocusGuard === focusGuard) ui.learnerFocusGuard = null;
    }
  };
  const openCourse = async (course, itemId = null) => {
    const focusGuard = ui.learnerFocusGuard;
    const previousCourse = ui.selectedCourse, previousItem = ui.playerItems?.[previousCourse];
    ui.lessonScrolls ||= {};
    if (previousCourse && previousItem) ui.lessonScrolls[`${previousCourse}:${previousItem}`] = ui.body?.scrollTop || 0;
    ui.selectedCourse = course.id;
    const cp = progress.courses?.[course.id];
    const items = treeHouseCourseItems(state, course);
    const resume = itemId || cp?.resumeActivityId || ui.playerItems?.[course.id] || items[0]?.id;
    ui.playerItems ||= {}; ui.playerItems[course.id] = resume;
    persistContext();
    // Opening is engagement/resume only. No traversal or required work is completed.
    if (ui.snapshot.permissions.learner) await command('course.open', { courseId:course.id, ...(items.find(item => item.id === resume)?.itemKind === 'activity' ? { activityId:resume } : {}) });
    renderLoaded();
    if (ui.body) { ui.body.scrollTop = ui.lessonScrolls[`${course.id}:${resume}`] || 0; if (focusGuard?.()) ui.body.focus({preventScroll:true}); }
  };
  const backToHub = () => { ui.lessonScrolls ||= {}; if (ui.selectedCourse && ui.playerItems?.[ui.selectedCourse]) ui.lessonScrolls[`${ui.selectedCourse}:${ui.playerItems[ui.selectedCourse]}`] = ui.body?.scrollTop || 0; ui.selectedCourse = null; persistContext(); renderLoaded(); if (ui.body) ui.body.scrollTop = 0; };
  const selected = courses.find(course => course.id === ui.selectedCourse);
  const badge = (text, kind = '') => h('span', { class:`th-state ${kind}`, text });
  const meter = (cp, label) => h('div', { class:'th-meter', role:'progressbar', 'aria-label':label, 'aria-valuemin':'0', 'aria-valuemax':'100', 'aria-valuenow':String(cp?.percent || 0) }, h('span', { style:`width:${Math.max(0, Math.min(100, Number(cp?.percent) || 0))}%` }));
  const unmetPrerequisites = course => course.freeExploration ? [] : (course.prerequisites || []).filter(id => !progress.courses?.[id]?.complete).map(id => state.courses[id]?.title || 'an earlier course');

  if (!selected) {
    const finished = courses.filter(course => progress.courses?.[course.id]?.complete).length;
    const hero = h('section', { class:'th-hub-hero' },
      h('div', {}, h('span', { class:'th-eyebrow', text:'YOUR LEARNING ADVENTURE' }), h('h1', { text:'Small steps. New possibilities.' }),
        h('p', { text:'Explore a Tutorial at your own pace, or take on a Quest and put your skills to work.' })),
      h('div', { class:'th-hub-stats', 'aria-label':'Learning overview' }, h('strong', { text:String(finished) }), h('span', { text:'courses completed' }), h('strong', { text:String(progress.points || 0) }), h('span', { text:'learning points' })));
    root.append(hero);
    const continuing = courses.find(course => ['in-progress','opened'].includes(progress.courses?.[course.id]?.state) && !progress.courses[course.id]?.complete);
    if (continuing) {
      const cp = progress.courses[continuing.id];
      root.append(h('section', { class:'th-continue' }, h('div', {}, h('span', { class:'th-eyebrow', text:'PICK UP WHERE YOU LEFT OFF' }), h('h2', { text:continuing.title }), h('p', { text:`${cp.percent || 0}% complete · ${treeHouseCourseKind(continuing, cp) === 'tutorial' ? 'Tutorial' : 'Quest'}` })), h('button', { class:'copal-btn primary', text:'Continue learning', onclick:run(() => openCourse(continuing)) })));
    }
    const filters = h('div', { class:'th-library-toolbar' });
    const group = h('div', { class:'th-filter-group', role:'group', 'aria-label':'Course type' });
    ui.courseFilter ||= 'all';
    for (const [id, label] of [['all','All paths'],['tutorial','Tutorials'],['quest','Quests']]) group.append(h('button', { class:`copal-btn${ui.courseFilter === id ? ' primary' : ''}`, text:label, 'aria-pressed':String(ui.courseFilter === id), onclick:() => { ui.courseFilter = id; renderLoaded(); } }));
    const search = h('input', { class:'copal-search th-course-search', type:'search', placeholder:'Find your next path…', 'aria-label':'Search learning paths', value:ui.courseSearch || '' });
    const grid = h('div', { class:'th-course-grid' });
    const drawCourses = () => {
      grid.replaceChildren();
      for (const [index, course] of courses.entries()) {
        const cp = progress.courses?.[course.id]; const kind = treeHouseCourseKind(course, cp);
        if (ui.courseFilter !== 'all' && ui.courseFilter !== kind) continue;
        if (ui.courseSearch && !`${course.title} ${course.description || ''} ${(course.tags || []).join(' ')}`.toLowerCase().includes(ui.courseSearch.toLowerCase())) continue;
        const locked = unmetPrerequisites(course); const items = treeHouseCourseItems(state, course);
        const card = h('article', { class:`th-course-card th-course-${kind}`, 'data-copal-context-object':'treehouse', 'data-treehouse-id':course.id },
          h('div', { class:'th-course-cover', 'aria-hidden':'true' }, h('span', { class:'th-cover-symbol', text:kind === 'tutorial' ? '✦' : '⚑' }), h('span', { class:'th-cover-number', text:String(index + 1).padStart(2,'0') })),
          h('div', { class:'th-course-card-body' }, h('div', { class:'th-card-badges' }, badge(kind === 'tutorial' ? 'Tutorial' : 'Quest', kind), badge(cp?.verified ? 'Verified' : cp?.complete ? 'Completed' : locked.length ? 'Prerequisites' : cp?.state === 'in-progress' ? 'In progress' : cp?.state === 'opened' ? 'Opened' : 'Available')),
            h('h2', { text:course.title }), h('p', { text:course.description || (kind === 'tutorial' ? 'Explore the lessons, then continue to the next step.' : 'Learn the foundations and complete the required missions.') }),
            h('small', { text:`${course.moduleIds?.length || 0} chapters · ${items.length} steps` }), meter(cp, `${course.title} completion`),
            h('div', { class:'th-card-footer' }, h('small', { text:`${cp?.percent || 0}% complete` }), h('button', { class:'copal-btn primary', text:cp?.complete ? 'Revisit' : cp?.state === 'in-progress' || cp?.state === 'opened' ? 'Continue' : 'Explore', onclick:run(() => openCourse(course)) }))));
        if (locked.length) card.querySelector('.th-course-card-body').append(h('p', { class:'th-requirement', text:`Complete first: ${locked.join(', ')}.` }));
        grid.append(card);
      }
      if (!grid.childNodes.length) grid.append(h('div', { class:'copal-empty', text:courses.length ? 'No paths match this search. Try another title or type.' : 'Your learning library is ready for its first published path.' }));
    };
    search.addEventListener('input', () => { ui.courseSearch = search.value; drawCourses(); });
    filters.append(group, search); root.append(filters, grid); drawCourses();
    return;
  }

  const cp = progress.courses?.[selected.id] || {}; const kind = treeHouseCourseKind(selected, cp);
  const items = treeHouseCourseItems(state, selected);
  ui.playerItems ||= {};
  const current = items.find(item => item.id === ui.playerItems[selected.id]) || items.find(item => item.id === cp.resumeActivityId) || items[0];
  const activeIndex = items.indexOf(current);
  const completed = item => item.itemKind === 'activity' ? progress.completedActivityIds?.includes(item.id) : !(cp.missingAssignmentIds || []).includes(item.id) && (cp.requiredAssignmentIds || []).includes(item.id);
  const required = item => (item.itemKind === 'activity' ? cp.requiredActivityIds : cp.requiredAssignmentIds)?.includes(item.id);
  const selectItem = async item => { await openCourse(selected, item.id); };
  const head = h('header', { class:'th-player-head' }, h('button', { class:'copal-btn', icon:'back', text:'Learning hub', onclick:backToHub }),
    h('div', { class:'th-player-heading' }, h('h1', { text:selected.title }), h('div', { class:'th-card-badges' }, badge(kind === 'tutorial' ? 'Tutorial' : 'Quest', kind), badge(cp.verified ? 'Verified completion' : cp.complete ? 'Completed' : 'In progress'), h('small', { text:`${cp.percent || 0}% complete` }))));
  root.append(head, meter(cp, `${selected.title} completion`));
  const layout = h('div', { class:'th-player-layout' });
  const index = h('details', { class:'th-course-index', open:(ui.body?.clientWidth || 1000) > 680 }, h('summary', { text:'Course index' }));
  for (const moduleId of selected.moduleIds || []) {
    const module = state.modules[moduleId]; if (!module) continue;
    const chapter = h('section', { class:'th-index-chapter' }, h('h3', { text:module.title }));
    for (const item of items.filter(item => item.module.id === moduleId)) chapter.append(h('button', { class:`th-index-step${current?.id === item.id ? ' active' : ''}`, 'aria-current':current?.id === item.id ? 'step' : false, onclick:run(() => selectItem(item)) },
      h('span', { class:'th-step-state', text:completed(item) ? '✓' : item.itemKind === 'assignment' ? '◇' : '○', 'aria-label':completed(item) ? 'Complete' : item.itemKind === 'assignment' ? 'Task' : 'Lesson' }), h('span', {}, h('strong', { text:item.title }), h('small', { text:item.itemKind === 'assignment' ? required(item) ? 'Required task' : 'Optional practice' : kind === 'tutorial' ? 'Read & continue' : required(item) ? 'Required mission' : 'Lesson' }))));
    index.append(chapter);
  }
  layout.append(index);
  const lesson = h('article', { class:'th-lesson', 'data-copal-context-object':'treehouse', 'data-treehouse-id':current?.id || '', 'data-field-guide-lesson':current?.fieldGuideKey || current?.id || '' });
  if (!current) { lesson.append(h('div', { class:'copal-empty', text:'This course has no published steps yet.' })); layout.append(lesson); root.append(layout); return; }
  lesson.append(h('span', { class:'th-eyebrow', text:`${current.module.title} · ${activeIndex + 1} OF ${items.length}` }), h('h2', { text:current.title }));
  const rendererContext = { ...context, course:selected, activity:current, progress:cp, preview:false };
  if (!renderTreeHouseActivity(lesson, rendererContext)) {
    const markdown = current.itemKind === 'assignment' ? current.prompt : current.content;
    if (markdown) lesson.append(h('div', { class:'th-lesson-body copal-meme-body' }, context.renderMarkdown(markdown)));
    const practice = current.practice || {};
    if (practice.seed || practice.expectedEvidence || current.verifierSpec?.evidence) {
      const practicePanel = h('section', { class:'th-practice' }, h('h3', { text:kind === 'tutorial' ? 'Try it, if you like' : 'Your mission' }), h('p', { text:practice.expectedEvidence || current.verifierSpec?.evidence || 'Try this exercise in your workspace.' }));
      if (practice.seed) practicePanel.append(h('details', {}, h('summary', { text:'Exercise materials' }), h('pre', { text:practice.seed })));
      lesson.append(practicePanel);
    }
    if (current.itemKind === 'assignment') {
      const submission = state.submissions[`${current.id}:${ui.actorId}`];
      if (submission) lesson.append(h('section', { class:'th-feedback', role:'status' }, h('strong', { text:submission.status === 'graded' ? `Reviewed · ${submission.grade}/${current.maxPoints}` : 'Submitted · awaiting review' }), h('p', { text:submission.feedback || 'Your work has been saved.' })));
      lesson.append(h('button', { class:'copal-btn primary', text:submission ? 'Submit another attempt' : 'Submit work', disabled:!currentEnrollment(selected.id), onclick:() => context.submitAssignment(current) }));
    }
  }
  const lessonActions = h('div', { class:'th-lesson-actions' });
  if (current.surface?.appLink || current.surface?.key || current.surface?.href) {
    const surface = current.surface;
    lessonActions.append(h('a', { class:'copal-btn', text:`Open ${surface.label || 'practice workspace'}`, href:surface.href || '#', onclick:run(async event => {
      const target = surface.appLink || (surface.key ? `clank://${surface.key}` : ''); if (!target) return;
      event.preventDefault(); const opened = await context.openAppDestination(target, event, { sourceKind:'treehouse' });
      if (opened?.ok === false) throw new Error(opened.error || 'Could not open the practice workspace.');
    }) }));
  }
  lessonActions.append(h('button', { class:'copal-btn', text:'Ask for help', onclick:() => context.requestLessonHelp(selected, current) }));
  lesson.append(lessonActions);
  const locks = unmetPrerequisites(selected);
  if (!currentEnrollment(selected.id) && !selected.freeExploration) lesson.append(h('div', { class:'th-enrollment' }, h('p', { text:locks.length ? `Complete first: ${locks.join(', ')}.` : 'Join this path to save your progress and work.' }), h('button', { class:'copal-btn primary', text:'Start learning', disabled:!!locks.length || !ui.snapshot.permissions.learner, onclick:run(() => command('enrollment.enroll', { courseId:selected.id })) })));
  const footer = h('footer', { class:'th-player-footer' });
  if (activeIndex > 0) footer.append(h('button', { class:'copal-btn', text:'Previous', onclick:run(() => selectItem(items[activeIndex - 1])) }));
  const canSave = ui.snapshot.permissions.learner && (currentEnrollment(selected.id) || selected.freeExploration);
  if (kind === 'tutorial') footer.append(h('button', { class:'copal-btn primary', text:activeIndex === items.length - 1 ? completed(current) ? 'Return to hub' : 'Finish Tutorial' : 'Continue', disabled:!canSave, onclick:run(async () => {
    if (current.itemKind === 'activity' && !completed(current)) await command('activity.complete', { activityId:current.id });
    if (activeIndex < items.length - 1) await selectItem(items[activeIndex + 1]); else backToHub();
  }) }));
  else {
    if (current.itemKind === 'activity' && !completed(current) && current.completion !== 'verified') footer.append(h('button', { class:'copal-btn', text:current.completion === 'self-check' ? 'Record my self-check' : required(current) ? 'Record completed mission' : 'Mark lesson read', disabled:!canSave, onclick:run(() => command('activity.complete', { activityId:current.id })) }));
    if (current.itemKind === 'activity' && current.completion === 'verified' && !completed(current)) {
      const verificationFeedback = h('p', { class:'th-requirement', role:'alert' });
      footer.append(h('button', { class:'copal-btn', text:'Check saved evidence', disabled:!canSave, onclick:run(async () => {
        verificationFeedback.textContent = '';
        verificationFeedback.removeAttribute('data-verification-code');
        try { await command('activity.complete', { activityId:current.id }); }
        catch (error) {
          // Render the server's typed outcome; no local completion/evidence is invented.
          verificationFeedback.textContent = error?.message || 'Saved evidence could not be verified. Try again.';
          const code = error?.code || error?.detail?.code;
          if (code) verificationFeedback.setAttribute('data-verification-code', String(code));
          throw error;
        }
      }) }));
      lesson.append(verificationFeedback);
    }
    if (activeIndex < items.length - 1) footer.append(h('button', { class:'copal-btn primary', text:'Next step', onclick:run(() => selectItem(items[activeIndex + 1])) }));
    else footer.append(h('button', { class:'copal-btn primary', text:'Return to hub', onclick:backToHub }));
  }
  if (kind === 'quest' && current.completion === 'verified' && !completed(current)) lesson.append(h('p', { class:'th-requirement', role:'status', text:'Complete this mission in its practice workspace, then choose Check saved evidence. The server verifies saved proof before this objective is complete.' }));
  lesson.append(footer); layout.append(lesson);
  const objectives = h('details', { class:'th-objectives', open:(ui.body?.clientWidth || 1000) > 1050 }, h('summary', { text:kind === 'tutorial' ? 'Your progress' : 'Mission objectives' }),
    h('p', { text:kind === 'tutorial' ? 'Read the lessons and click Continue. Optional exercises do not block your Tutorial.' : 'Explore freely. Your Quest finishes when its required work meets the criteria below.' }));
  const missing = [...(cp.missingActivityIds || []), ...(cp.missingAssignmentIds || [])];
  const requiredItems = items.filter(required);
  const objectiveList = h('ul');
  for (const item of requiredItems) objectiveList.append(h('li', {}, h('span', { text:missing.includes(item.id) ? '○ ' : '✓ ' }), h('span', { text:item.title }), item.itemKind === 'assignment' ? h('small', { text:selected.completionCriteria?.assignmentMode === 'submitted' ? 'Submit work' : 'Review required' }) : ''));
  if (!requiredItems.length) objectiveList.append(h('li', { text:cp.complete ? 'Path completed.' : 'No required work in this path.' }));
  objectives.append(objectiveList); layout.append(objectives); root.append(layout);
}
