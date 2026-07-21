const SECTIONS = [['courses', 'Courses'], ['skills', 'Skills'], ['assignments', 'Assignments'], ['analytics', 'Analytics']];

export function treeHouseCommandId(prefix = 'command') {
  const random = globalThis.crypto?.randomUUID?.() || `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
  return `${prefix}:${random}`;
}

export function profileRoles(profile) {
  return new Set(profile?.roles || []);
}

export function hasTreeHouseRole(profile, ...roles) {
  const current = profileRoles(profile);
  return roles.some((role) => current.has(role));
}

export function moveTreeHouseItem(items, itemId, delta) {
  const next = [...items]; const index = next.indexOf(itemId); const target = index + delta;
  if (index < 0 || target < 0 || target >= next.length) return next;
  [next[index], next[target]] = [next[target], next[index]];
  return next;
}

export function createTreeHouseFeature({ h, api, setStatus, renderMarkdown, openDocument }) {
  const ui = {
    actorId: localStorage.getItem('odysseus-treehouse-actor') || 'owner',
    section: localStorage.getItem('odysseus-treehouse-section') || 'courses',
    selectedCourse: null,
    snapshot: null,
    token: 0,
    body: null,
  };

  const values = (object) => Object.values(object || {});
  const csv = (value) => String(value || '').split(',').map((item) => item.trim()).filter(Boolean);
  const learnerProjection = () => ui.snapshot?.projection?.learners?.[ui.actorId] || { points: 0, badges: [], quests: [], streak: 0, courses: {}, skills: {}, pointEvidence: [] };
  const currentEnrollment = (courseId) => values(ui.snapshot?.state?.enrollments).find((item) => item.courseId === courseId && item.profileId === ui.actorId);

  function field(label, control) {
    return h('label', { class: 'copal-treehouse-field' }, h('span', { text: label }), control);
  }

  function openForm(title, fields, submitLabel, onSubmit) {
    const dialog = h('dialog', { class: 'copal-dialog copal-treehouse-dialog' }, h('h2', { text: title }));
    const controls = {};
    for (const spec of fields) {
      let control;
      if (spec.type === 'textarea') {
        control = h('textarea', { rows: String(spec.rows || 4), placeholder: spec.placeholder || '' });
        control.value = spec.value || '';
      } else if (spec.type === 'select') {
        control = h('select');
        for (const option of spec.options || []) control.append(h('option', { value: option.value, text: option.label, selected: option.value === spec.value }));
        control.value = spec.value || spec.options?.[0]?.value || '';
      } else if (spec.type === 'checkbox') {
        control = h('input', { type: 'checkbox' }); control.checked = !!spec.value;
      } else {
        control = h('input', { type: spec.type || 'text', value: spec.value ?? '', placeholder: spec.placeholder || '', min: spec.min, max: spec.max });
      }
      controls[spec.id] = control;
      dialog.append(field(spec.label, control));
    }
    const feedback = h('p', { class: 'copal-treehouse-feedback', role: 'status' });
    const cancel = h('button', { class: 'copal-btn', type: 'button', text: 'Cancel', onclick: () => dialog.close() });
    const submit = h('button', { class: 'copal-btn primary', type: 'button', text: submitLabel, onclick: async () => {
      submit.disabled = true; feedback.textContent = '';
      try {
        const output = {};
        for (const spec of fields) {
          const control = controls[spec.id];
          output[spec.id] = spec.type === 'checkbox' ? control.checked : control.value;
        }
        await onSubmit(output); dialog.close();
      } catch (error) {
        feedback.textContent = error.message; feedback.classList.add('error'); submit.disabled = false;
      }
    } });
    dialog.append(feedback, h('div', { class: 'copal-dialog-actions' }, cancel, submit));
    document.body.append(dialog); dialog.addEventListener('close', () => dialog.remove()); dialog.showModal();
    Object.values(controls)[0]?.focus();
  }

  async function load() {
    const token = ++ui.token;
    let snapshot;
    try {
      snapshot = await api(`/treehouse?actor=${encodeURIComponent(ui.actorId)}`);
    } catch (error) {
      if (ui.actorId !== 'owner' && (error.status === 404 || /profile not found|inactive/i.test(error.message))) {
        ui.actorId = 'owner'; localStorage.setItem('odysseus-treehouse-actor', ui.actorId);
        return load();
      }
      throw error;
    }
    if (token !== ui.token) return false;
    ui.snapshot = snapshot;
    if (!snapshot.state.profiles[ui.actorId]) {
      ui.actorId = 'owner'; localStorage.setItem('odysseus-treehouse-actor', ui.actorId); return load();
    }
    return true;
  }

  async function command(type, payload = {}) {
    if (!ui.snapshot) throw new Error('TreeHouse is not loaded');
    setStatus('Saving TreeHouse…');
    const response = await api('/treehouse/commands', {
      method: 'POST',
      body: JSON.stringify({
        type, payload, actorId: ui.actorId,
        commandId: treeHouseCommandId(type),
        expectedRevision: ui.snapshot.state.revision,
      }),
    });
    ui.snapshot = response;
    setStatus(`TreeHouse saved · revision ${response.state.revision}`);
    renderLoaded();
    return response.result;
  }

  function roleToolbar(root) {
    const snapshot = ui.snapshot;
    const actor = snapshot.actor;
    const select = h('select', { 'aria-label': 'TreeHouse profile' });
    for (const profile of values(snapshot.state.profiles).filter((item) => item.active !== false)) {
      select.append(h('option', { value: profile.id, text: `${profile.displayName} · ${(profile.roles || []).join('/')}`, selected: profile.id === ui.actorId }));
    }
    select.value = ui.actorId;
    select.addEventListener('change', async () => {
      ui.actorId = select.value; localStorage.setItem('odysseus-treehouse-actor', ui.actorId);
      ui.body.replaceChildren(h('div', { class: 'copal-empty', text: 'Switching TreeHouse profile…' }));
      try { await load(); renderLoaded(); } catch (error) { renderFailure(error); }
    });
    const toolbar = h('div', { class: 'copal-treehouse-rolebar' }, h('strong', { text: 'TreeHouse' }), select,
      h('span', { text: `${snapshot.state.revision} revisions · ${snapshot.projection.eventCount} durable events` }));
    if (snapshot.permissions.admin) toolbar.append(h('button', { class: 'copal-btn', text: '+ Profile', onclick: createProfile }));
    if (snapshot.permissions.author) toolbar.append(h('button', { class: 'copal-btn', text: 'Import legacy', onclick: migrateLegacy }));
    root.append(toolbar);
  }

  function summary(root) {
    const progress = learnerProjection();
    const cards = h('div', { class: 'copal-treehouse-summary' },
      h('section', { class: 'copal-card' }, h('h3', { text: 'Points' }), h('strong', { text: String(progress.points || 0) }), h('small', { text: 'event-derived' })),
      h('section', { class: 'copal-card' }, h('h3', { text: 'Badges' }), h('strong', { text: String(progress.badges?.length || 0) }), h('small', { text: 'with evidence links' })),
      h('section', { class: 'copal-card' }, h('h3', { text: 'Streak' }), h('strong', { text: `${progress.streak || 0} day${progress.streak === 1 ? '' : 's'}` }), h('small', { text: 'consecutive learning days' })),
      h('section', { class: 'copal-card' }, h('h3', { text: 'Quests' }), h('strong', { text: String(progress.quests?.length || 0) }), h('small', { text: 'completed' })),
    );
    root.append(cards);
  }

  function navigation(root) {
    const nav = h('nav', { class: 'copal-treehouse-nav', 'aria-label': 'TreeHouse sections' });
    for (const [id, label] of SECTIONS) nav.append(h('button', { class: `copal-btn${ui.section === id ? ' primary' : ''}`, text: label, onclick: () => { ui.section = id; localStorage.setItem('odysseus-treehouse-section', id); renderLoaded(); } }));
    root.append(nav);
  }

  function createProfile() {
    openForm('Create TreeHouse profile', [
      { id: 'displayName', label: 'Display name' },
      { id: 'roles', label: 'Roles', type: 'select', options: [
        { value: 'learner', label: 'Learner' },
        { value: 'instructor,learner', label: 'Instructor + learner' },
        { value: 'admin,instructor,learner', label: 'Administrator' },
      ] },
    ], 'Create', ({ displayName, roles }) => command('profile.create', { displayName, roles: csv(roles) }));
  }

  async function migrateLegacy() {
    const dry = await api('/treehouse/migrate?dry_run=true', {
      method: 'POST',
      body: JSON.stringify({ actorId: ui.actorId, commandId: treeHouseCommandId('migration-dry'), expectedRevision: ui.snapshot.state.revision }),
    });
    const counts = dry.plan.counts;
    if (!counts.documents) { setStatus('TreeHouse legacy import: no new documents'); return; }
    if (!window.confirm(`Import ${counts.documents} legacy document(s) as ${counts.courses} course(s), ${counts.skills} skill(s), and ${counts.tasks} assignment(s)? Source documents will not be changed.`)) return;
    const applied = await api('/treehouse/migrate?dry_run=false', {
      method: 'POST',
      body: JSON.stringify({ actorId: ui.actorId, commandId: treeHouseCommandId('migration'), expectedRevision: ui.snapshot.state.revision }),
    });
    ui.snapshot = applied; setStatus(`TreeHouse imported ${applied.result.imported.documents} documents`); renderLoaded();
  }

  function createCourse() {
    openForm('Create course', [
      { id: 'title', label: 'Course title' },
      { id: 'description', label: 'Description', type: 'textarea' },
      { id: 'tags', label: 'Tags (comma separated)' },
    ], 'Create draft', ({ title, description, tags }) => command('course.create', { title, description, tags: csv(tags) }));
  }

  function editCourse(course) {
    openForm(`Edit · ${course.title}`, [
      { id: 'title', label: 'Course title', value: course.title },
      { id: 'description', label: 'Description', type: 'textarea', value: course.description || '' },
    ], 'Save course', ({ title, description }) => command('course.update', { courseId: course.id, title, description }));
  }

  function createModule(courseId) {
    openForm('Add course module', [
      { id: 'title', label: 'Module title' },
      { id: 'description', label: 'Description', type: 'textarea' },
    ], 'Add module', ({ title, description }) => command('module.create', { courseId, title, description }));
  }

  function editModule(module) {
    openForm(`Edit · ${module.title}`, [
      { id: 'title', label: 'Module title', value: module.title },
      { id: 'description', label: 'Description', type: 'textarea', value: module.description || '' },
    ], 'Save module', ({ title, description }) => command('module.update', { moduleId: module.id, title, description }));
  }

  function createActivity(moduleId) {
    const skills = values(ui.snapshot.state.skills);
    openForm('Add learning activity', [
      { id: 'title', label: 'Activity title' },
      { id: 'activityType', label: 'Type', type: 'select', options: ['lesson', 'markdown', 'video', 'resource', 'custom'].map((value) => ({ value, label: value })) },
      { id: 'content', label: 'Lesson content', type: 'textarea', rows: 8 },
      { id: 'points', label: 'Completion points', type: 'number', value: 10, min: 0, max: 10000 },
      { id: 'skillIds', label: `Skill IDs (comma separated)${skills.length ? ` · available: ${skills.map((item) => `${item.title}=${item.id}`).join(', ')}` : ''}` },
    ], 'Add activity', ({ title, activityType, content, points, skillIds }) => command('activity.create', { moduleId, title, activityType, content, points: Number(points), skillIds: csv(skillIds) }));
  }

  function editActivity(activity) {
    openForm(`Edit · ${activity.title}`, [
      { id: 'title', label: 'Activity title', value: activity.title },
      { id: 'content', label: 'Lesson content', type: 'textarea', rows: 8, value: activity.content || '' },
      { id: 'points', label: 'Completion points', type: 'number', value: activity.points, min: 0, max: 10000 },
      { id: 'skillIds', label: 'Skill IDs (comma separated)', value: (activity.skillIds || []).join(', ') },
      { id: 'status', label: 'Status', type: 'select', value: activity.status, options: ['draft', 'published', 'archived'].map((value) => ({ value, label: value })) },
    ], 'Save activity', ({ title, content, points, skillIds, status }) => command('activity.update', { activityId: activity.id, title, content, points: Number(points), skillIds: csv(skillIds), status }));
  }

  async function enroll(courseId) {
    await command('enrollment.enroll', { courseId }); ui.selectedCourse = courseId;
  }

  function courseCard(course, root) {
    const state = ui.snapshot.state; const progress = learnerProjection().courses?.[course.id];
    const modules = course.moduleIds.map((id) => state.modules[id]).filter(Boolean);
    const card = h('article', { class: 'copal-card copal-treehouse-course' },
      h('header', {}, h('h3', { text: course.title }), h('span', { class: `copal-treehouse-state ${course.status}`, text: course.status })),
      h('p', { text: course.description || 'No description yet.' }),
      h('p', { text: `${modules.length} module${modules.length === 1 ? '' : 's'}${progress ? ` · ${progress.percent}% complete` : ''}` }));
    const actions = h('div', { class: 'copal-treehouse-actions' }, h('button', { class: 'copal-btn', text: ui.selectedCourse === course.id ? 'Hide' : 'Open', onclick: () => { ui.selectedCourse = ui.selectedCourse === course.id ? null : course.id; renderLoaded(); } }));
    if (ui.snapshot.permissions.learner && course.status === 'published' && !currentEnrollment(course.id)) actions.append(h('button', { class: 'copal-btn primary', text: 'Enroll', onclick: () => enroll(course.id) }));
    if (ui.snapshot.permissions.learner && currentEnrollment(course.id)) actions.append(h('button', { class: 'copal-btn danger', text: 'Unenroll', onclick: () => { if (window.confirm('Withdraw from this course?')) command('enrollment.unenroll', { courseId: course.id }); } }));
    if (ui.snapshot.permissions.author && course.authorIds.includes(ui.actorId) || ui.snapshot.permissions.admin) {
      actions.append(h('button', { class: 'copal-btn', text: 'Edit', onclick: () => editCourse(course) }));
      actions.append(h('button', { class: 'copal-btn', text: '+ Module', onclick: () => createModule(course.id) }));
      if (course.status === 'draft') actions.append(h('button', { class: 'copal-btn primary', text: 'Publish', onclick: () => command('course.publish', { courseId: course.id }) }));
      if (course.status !== 'archived') actions.append(h('button', { class: 'copal-btn danger', text: 'Archive', onclick: () => command('course.archive', { courseId: course.id }) }));
      actions.append(h('button', { class: 'copal-btn danger', text: 'Delete', onclick: () => { if (window.confirm('Delete this course and all its content? This cannot be undone.')) command('course.delete', { courseId: course.id }); } }));
    }
    card.append(actions); root.append(card);
  }

  function courseDetail(course, root) {
    if (!course) return;
    const state = ui.snapshot.state; const progress = learnerProjection();
    const moduleProgress = progress.courses?.[course.id]?.modules || {};
    const detail = h('section', { class: 'copal-treehouse-detail' }, h('h2', { text: course.title }));
    for (const [moduleIndex, moduleId] of course.moduleIds.entries()) {
      const module = state.modules[moduleId]; if (!module) continue;
      const mp = moduleProgress[moduleId];
      const moduleHeader = h('header', {}, h('h3', { text: module.title }), h('small', { text: mp ? `${mp.completed}/${mp.total} items · ${mp.percent}%` : `${module.activityIds.length} activities · ${module.assignmentIds.length} assignments` }));
      if (ui.snapshot.permissions.author) moduleHeader.append(h('div', { class: 'copal-treehouse-order' },
        h('button', { class: 'copal-btn', text: 'Edit', 'aria-label': `Edit ${module.title}`, onclick: () => editModule(module) }),
        h('button', { class: 'copal-btn', text: '↑', title: 'Move module earlier', 'aria-label': `Move ${module.title} earlier`, disabled: moduleIndex === 0, onclick: () => command('course.reorder_modules', { courseId: course.id, moduleIds: moveTreeHouseItem(course.moduleIds, module.id, -1) }) }),
        h('button', { class: 'copal-btn', text: '↓', title: 'Move module later', 'aria-label': `Move ${module.title} later`, disabled: moduleIndex === course.moduleIds.length - 1, onclick: () => command('course.reorder_modules', { courseId: course.id, moduleIds: moveTreeHouseItem(course.moduleIds, module.id, 1) }) }),
        h('button', { class: 'copal-btn danger', text: 'Delete', 'aria-label': `Delete ${module.title}`, onclick: () => { if (window.confirm(`Delete module "${module.title}" and all its content?`)) command('module.delete', { moduleId: module.id }); } })));
      const moduleCard = h('article', { class: 'copal-card copal-treehouse-module' }, moduleHeader);
      if (module.description) moduleCard.append(h('p', { text: module.description }));
      for (const [activityIndex, activityId] of module.activityIds.entries()) {
        const activity = state.activities[activityId]; if (!activity || activity.status === 'archived') continue;
        const complete = progress.completedActivityIds?.includes(activityId);
        const row = h('div', { class: `copal-treehouse-activity${complete ? ' complete' : ''}` },
          h('div', {}, h('strong', { text: activity.title }), h('small', { text: `${activity.activityType} · ${activity.points} points${activity.skillIds?.length ? ` · ${activity.skillIds.length} skills` : ''}` })),
          h('span', { text: complete ? 'Completed' : activity.status }));
        if (activity.content) row.append(h('details', {}, h('summary', { text: 'Open lesson' }), h('div', { class: 'copal-meme-body' }, renderMarkdown(activity.content))));
        if (ui.snapshot.permissions.author) row.append(h('div', { class: 'copal-treehouse-order' },
          h('button', { class: 'copal-btn', text: 'Edit', 'aria-label': `Edit ${activity.title}`, onclick: () => editActivity(activity) }),
          h('button', { class: 'copal-btn', text: '↑', title: 'Move activity earlier', 'aria-label': `Move ${activity.title} earlier`, disabled: activityIndex === 0, onclick: () => command('module.reorder_items', { moduleId: module.id, activityIds: moveTreeHouseItem(module.activityIds, activity.id, -1), assignmentIds: module.assignmentIds }) }),
          h('button', { class: 'copal-btn', text: '↓', title: 'Move activity later', 'aria-label': `Move ${activity.title} later`, disabled: activityIndex === module.activityIds.length - 1, onclick: () => command('module.reorder_items', { moduleId: module.id, activityIds: moveTreeHouseItem(module.activityIds, activity.id, 1), assignmentIds: module.assignmentIds }) }),
          h('button', { class: 'copal-btn danger', text: '×', title: 'Delete activity', 'aria-label': `Delete ${activity.title}`, onclick: () => { if (window.confirm(`Delete activity "${activity.title}"?`)) command('activity.delete', { activityId: activity.id }); } })));
        if (ui.snapshot.permissions.learner && currentEnrollment(course.id) && activity.status === 'published' && !complete) row.append(h('button', { class: 'copal-btn primary', text: 'Mark complete', onclick: () => command('activity.complete', { activityId }) }));
        moduleCard.append(row);
      }
      if (ui.snapshot.permissions.author) moduleCard.append(h('button', { class: 'copal-btn', text: '+ Activity', onclick: () => createActivity(module.id) }), h('button', { class: 'copal-btn', text: '+ Assignment', onclick: () => createAssignment(module.id) }));
      detail.append(moduleCard);
    }
    root.append(detail);
  }

  function renderCourses(root) {
    const toolbar = h('div', { class: 'copal-treehouse-section-head' }, h('div', {}, h('h2', { text: 'Courses' }), h('p', { text: 'Author, publish, enroll, navigate, and complete durable learning paths.' })));
    if (ui.snapshot.permissions.author) toolbar.append(h('button', { class: 'copal-btn primary', text: '+ Course', onclick: createCourse }));
    root.append(toolbar);
    const grid = h('div', { class: 'copal-card-grid' });
    const courses = values(ui.snapshot.state.courses).filter((item) => item.status !== 'archived' || ui.snapshot.permissions.author);
    for (const course of courses) courseCard(course, grid);
    if (!courses.length) grid.append(h('div', { class: 'copal-empty', text: ui.snapshot.permissions.author ? 'No courses yet. Create the first draft.' : 'No published courses are available.' }));
    root.append(grid); courseDetail(ui.snapshot.state.courses[ui.selectedCourse], root);
  }

  function createSkill() {
    const skills = values(ui.snapshot.state.skills);
    openForm('Create skill', [
      { id: 'title', label: 'Skill title' },
      { id: 'description', label: 'Description', type: 'textarea' },
      { id: 'prerequisiteIds', label: `Prerequisite IDs${skills.length ? ` · ${skills.map((item) => `${item.title}=${item.id}`).join(', ')}` : ''}` },
      { id: 'masteryThreshold', label: 'Prerequisite mastery points', type: 'number', value: 60, min: 0 },
      { id: 'evidencePoints', label: 'Approved evidence points', type: 'number', value: 25, min: 0 },
    ], 'Create skill', ({ title, description, prerequisiteIds, masteryThreshold, evidencePoints }) => command('skill.create', { title, description, prerequisiteIds: csv(prerequisiteIds), masteryThreshold: Number(masteryThreshold), evidencePoints: Number(evidencePoints) }));
  }

  function editSkill(skill) {
    openForm(`Edit · ${skill.title}`, [
      { id: 'title', label: 'Skill title', value: skill.title },
      { id: 'description', label: 'Description', type: 'textarea', value: skill.description || '' },
      { id: 'prerequisiteIds', label: 'Prerequisite IDs', value: (skill.prerequisiteIds || []).join(', ') },
    ], 'Save skill', ({ title, description, prerequisiteIds }) => command('skill.update', { skillId: skill.id, title, description, prerequisiteIds: csv(prerequisiteIds) }));
  }

  function submitEvidence(skillId) {
    openForm('Submit skill evidence', [
      { id: 'description', label: 'What proves this skill?', type: 'textarea', rows: 6 },
      { id: 'sourceUrl', label: 'Optional source URL' },
    ], 'Submit for review', ({ description, sourceUrl }) => command('evidence.submit', { skillId, description, sourceUrl }));
  }

  function createBadge() {
    const state = ui.snapshot.state;
    openForm('Create evidence-backed badge', [
      { id: 'title', label: 'Badge title' },
      { id: 'description', label: 'Description', type: 'textarea' },
      { id: 'type', label: 'Criteria', type: 'select', options: [
        { value: 'points', label: 'Total points' }, { value: 'skill', label: 'Skill points' },
        { value: 'course', label: 'Course completion' }, { value: 'quest', label: 'Quest completion' },
      ] },
      { id: 'target', label: `Target ID (skill/course/quest; blank for total points) · ${values(state.skills).map((item) => item.id).join(', ')}` },
      { id: 'threshold', label: 'Point threshold', type: 'number', value: 100, min: 0 },
    ], 'Create badge', ({ title, description, type, target, threshold }) => {
      const criteria = { type };
      if (type === 'points') criteria.threshold = Number(threshold);
      if (type === 'skill') { criteria.skillId = target; criteria.threshold = Number(threshold); }
      if (type === 'course') criteria.courseId = target;
      if (type === 'quest') criteria.questId = target;
      return command('badge.create', { title, description, criteria });
    });
  }

  function editBadge(badge) {
    const state = ui.snapshot.state;
    openForm(`Edit · ${badge.title}`, [
      { id: 'title', label: 'Badge title', value: badge.title },
      { id: 'description', label: 'Description', type: 'textarea', value: badge.description || '' },
      { id: 'type', label: 'Criteria', type: 'select', value: badge.criteria?.type || 'points', options: [
        { value: 'points', label: 'Total points' }, { value: 'skill', label: 'Skill points' },
        { value: 'course', label: 'Course completion' }, { value: 'quest', label: 'Quest completion' },
      ] },
      { id: 'target', label: 'Target ID (skill/course/quest)' },
      { id: 'threshold', label: 'Point threshold', type: 'number', value: badge.criteria?.threshold || 100, min: 0 },
    ], 'Save badge', ({ title, description, type, target, threshold }) => {
      const criteria = { type };
      if (type === 'points') criteria.threshold = Number(threshold);
      if (type === 'skill') { criteria.skillId = target; criteria.threshold = Number(threshold); }
      if (type === 'course') criteria.courseId = target;
      if (type === 'quest') criteria.questId = target;
      return command('badge.update', { badgeId: badge.id, title, description, criteria });
    });
  }

  function createQuest() {
    const state = ui.snapshot.state;
    openForm('Create quest', [
      { id: 'title', label: 'Quest title' },
      { id: 'description', label: 'Description', type: 'textarea' },
      { id: 'activityIds', label: `Activity IDs · ${values(state.activities).map((item) => `${item.title}=${item.id}`).join(', ')}` },
      { id: 'assignmentIds', label: `Assignment IDs · ${values(state.assignments).map((item) => `${item.title}=${item.id}`).join(', ')}` },
      { id: 'rewardPoints', label: 'Reward points', type: 'number', value: 25, min: 0 },
    ], 'Create quest', ({ title, description, activityIds, assignmentIds, rewardPoints }) => command('quest.create', { title, description, activityIds: csv(activityIds), assignmentIds: csv(assignmentIds), rewardPoints: Number(rewardPoints) }));
  }

  function editQuest(quest) {
    const state = ui.snapshot.state;
    openForm(`Edit · ${quest.title}`, [
      { id: 'title', label: 'Quest title', value: quest.title },
      { id: 'description', label: 'Description', type: 'textarea', value: quest.description || '' },
      { id: 'activityIds', label: 'Activity IDs', value: (quest.activityIds || []).join(', ') },
      { id: 'assignmentIds', label: 'Assignment IDs', value: (quest.assignmentIds || []).join(', ') },
      { id: 'rewardPoints', label: 'Reward points', type: 'number', value: quest.rewardPoints, min: 0 },
    ], 'Save quest', ({ title, description, activityIds, assignmentIds, rewardPoints }) => command('quest.update', { questId: quest.id, title, description, activityIds: csv(activityIds), assignmentIds: csv(assignmentIds), rewardPoints: Number(rewardPoints) }));
  }

  function renderSkills(root) {
    const state = ui.snapshot.state; const progress = learnerProjection();
    const toolbar = h('div', { class: 'copal-treehouse-section-head' }, h('div', {}, h('h2', { text: 'Skills & evidence' }), h('p', { text: 'Prerequisites gate evidence. Every proficiency point links back to a durable event.' })));
    if (ui.snapshot.permissions.author) toolbar.append(h('button', { class: 'copal-btn primary', text: '+ Skill', onclick: createSkill }), h('button', { class: 'copal-btn', text: '+ Badge', onclick: createBadge }), h('button', { class: 'copal-btn', text: '+ Quest', onclick: createQuest }));
    root.append(toolbar);
    const map = h('section', { class: 'copal-card copal-treehouse-skill-map', 'aria-label': 'Skill prerequisite map' }, h('h3', { text: 'Prerequisite map' }));
    const mapList = h('ul');
    for (const skill of values(state.skills)) {
      const prerequisites = (skill.prerequisiteIds || []).map((id) => state.skills[id]?.title || id);
      mapList.append(h('li', {}, h('strong', { text: skill.title }), h('span', { text: prerequisites.length ? ` ← ${prerequisites.join(', ')}` : ' · foundation' })));
    }
    map.append(mapList); root.append(map);
    const grid = h('div', { class: 'copal-card-grid' });
    for (const skill of values(state.skills)) {
      const item = progress.skills?.[skill.id] || { points: 0, level: 'novice', unlocked: !skill.prerequisiteIds?.length, evidenceEventIds: [] };
      const prereqs = (skill.prerequisiteIds || []).map((id) => state.skills[id]?.title || id);
      const card = h('article', { class: `copal-card copal-treehouse-skill${item.unlocked ? '' : ' locked'}` },
        h('h3', { text: skill.title }), h('p', { text: skill.description || 'No description.' }),
        h('p', { text: prereqs.length ? `Prerequisites: ${prereqs.join(', ')}` : 'Foundation skill' }),
        h('div', { class: 'copal-progress' }, h('span', { style: `width:${Math.min(100, item.points)}%` })),
        h('p', { text: `${item.points} points · ${item.level} · ${item.evidenceEventIds?.length || 0} evidence events${item.unlocked ? '' : ' · locked'}` }));
      if (ui.snapshot.permissions.learner && item.unlocked) card.append(h('button', { class: 'copal-btn', text: 'Submit evidence', onclick: () => submitEvidence(skill.id) }));
      if (ui.snapshot.permissions.author) {
        card.append(h('button', { class: 'copal-btn', text: 'Edit skill', onclick: () => editSkill(skill) }));
        card.append(h('button', { class: 'copal-btn danger', text: '×', title: 'Delete skill', onclick: () => { if (window.confirm(`Delete skill "${skill.title}"?`)) command('skill.delete', { skillId: skill.id }); } }));
      }
      grid.append(card);
    }
    if (!values(state.skills).length) grid.append(h('div', { class: 'copal-empty', text: 'No skills have been defined.' }));
    root.append(grid);
    const evidence = h('section', { class: 'copal-card copal-treehouse-evidence' }, h('h2', { text: 'Evidence review' }));
    const visible = values(state.evidence);
    for (const item of visible) {
      const row = h('div', { class: 'copal-task-row' }, h('span', { text: `${state.profiles[item.profileId]?.displayName || item.profileId}: ${item.description}` }), h('small', { text: `${state.skills[item.skillId]?.title || item.skillId} · ${item.status}` }));
      if (ui.snapshot.permissions.grade && item.status === 'pending') row.append(h('button', { class: 'copal-btn primary', text: 'Approve', onclick: () => command('evidence.review', { evidenceId: item.id, decision: 'approved', note: 'Verified in TreeHouse' }) }), h('button', { class: 'copal-btn danger', text: 'Reject', onclick: () => command('evidence.review', { evidenceId: item.id, decision: 'rejected', note: 'Needs more evidence' }) }));
      evidence.append(row);
    }
    if (!visible.length) evidence.append(h('p', { text: 'No evidence submissions yet.' }));
    root.append(evidence);
    const rewards = h('div', { class: 'copal-card-grid' });
    const badgeSection = h('section', { class: 'copal-card' }, h('h3', { text: 'Badges' }));
    for (const badge of values(state.badges)) {
      const earned = progress.badges?.some((item) => item.badgeId === badge.id);
      const row = h('p', { text: `${earned ? '✓' : '○'} ${badge.title}` });
      if (ui.snapshot.permissions.author) {
        row.append(h('button', { class: 'copal-btn', text: 'Edit', onclick: () => editBadge(badge) }));
        row.append(h('button', { class: 'copal-btn danger', text: '×', title: 'Delete badge', onclick: () => { if (window.confirm(`Delete badge "${badge.title}"?`)) command('badge.delete', { badgeId: badge.id }); } }));
      }
      badgeSection.append(row);
    }
    if (!values(state.badges).length) badgeSection.append(h('p', { text: 'No badges yet.' }));
    rewards.append(badgeSection);
    const questSection = h('section', { class: 'copal-card' }, h('h3', { text: 'Quests' }));
    for (const quest of values(state.quests)) {
      const done = progress.quests?.some((item) => item.questId === quest.id);
      const row = h('p', { text: `${done ? '✓' : '○'} ${quest.title} · ${quest.rewardPoints} points` });
      if (ui.snapshot.permissions.author) {
        row.append(h('button', { class: 'copal-btn', text: 'Edit', onclick: () => editQuest(quest) }));
        row.append(h('button', { class: 'copal-btn danger', text: '×', title: 'Delete quest', onclick: () => { if (window.confirm(`Delete quest "${quest.title}"?`)) command('quest.delete', { questId: quest.id }); } }));
      }
      questSection.append(row);
    }
    if (!values(state.quests).length) questSection.append(h('p', { text: 'No quests yet.' }));
    rewards.append(questSection);
    root.append(rewards);
  }

  function createAssignment(moduleId = null) {
    const state = ui.snapshot.state;
    const modules = values(state.modules);
    if (!moduleId && !modules.length) { setStatus('Create a course module before adding an assignment', true); return; }
    openForm('Create assignment', [
      { id: 'moduleId', label: 'Module', type: 'select', value: moduleId || modules[0]?.id, options: modules.map((item) => ({ value: item.id, label: `${state.courses[item.courseId]?.title || ''} · ${item.title}` })) },
      { id: 'title', label: 'Assignment title' },
      { id: 'prompt', label: 'Prompt', type: 'textarea', rows: 6 },
      { id: 'dueAt', label: 'Due date/time (optional)', type: 'datetime-local' },
      { id: 'maxPoints', label: 'Maximum points', type: 'number', value: 100, min: 1 },
      { id: 'skillIds', label: `Skill IDs · ${values(state.skills).map((item) => `${item.title}=${item.id}`).join(', ')}` },
      { id: 'allowRetries', label: 'Allow retries after grading', type: 'checkbox', value: true },
      { id: 'maxAttempts', label: 'Maximum attempts (0 is unlimited)', type: 'number', value: 0, min: 0 },
    ], 'Create draft', (data) => command('assignment.create', { ...data, dueAt: data.dueAt ? new Date(data.dueAt).toISOString() : '', maxPoints: Number(data.maxPoints), maxAttempts: Number(data.maxAttempts), skillIds: csv(data.skillIds) }));
  }

  function editAssignment(assignment) {
    openForm(`Edit · ${assignment.title}`, [
      { id: 'title', label: 'Assignment title', value: assignment.title },
      { id: 'prompt', label: 'Prompt', type: 'textarea', rows: 6, value: assignment.prompt || '' },
      { id: 'dueAt', label: 'Due date/time (optional)', type: 'datetime-local', value: assignment.dueAt ? assignment.dueAt.slice(0, 16) : '' },
      { id: 'maxPoints', label: 'Maximum points', type: 'number', value: assignment.maxPoints, min: 1 },
    ], 'Save assignment', ({ title, prompt, dueAt, maxPoints }) => command('assignment.update', { assignmentId: assignment.id, title, prompt, dueAt: dueAt ? new Date(dueAt).toISOString() : '', maxPoints: Number(maxPoints) }));
  }

  function submitAssignment(assignment) {
    openForm(`Submit · ${assignment.title}`, [
      { id: 'answer', label: assignment.prompt || 'Your answer', type: 'textarea', rows: 8 },
    ], 'Submit', ({ answer }) => command('submission.submit', { assignmentId: assignment.id, answer }));
  }

  function gradeSubmission(submission) {
    const assignment = ui.snapshot.state.assignments[submission.assignmentId];
    openForm(`Grade · ${assignment.title}`, [
      { id: 'score', label: `Score (0–${assignment.maxPoints})`, type: 'number', value: submission.grade ?? assignment.maxPoints, min: 0, max: assignment.maxPoints },
      { id: 'feedback', label: 'Feedback', type: 'textarea', rows: 5, value: submission.feedback || '' },
    ], 'Save grade', ({ score, feedback }) => command('submission.grade', { submissionId: submission.id, score: Number(score), feedback }));
  }

  function renderAssignments(root) {
    const state = ui.snapshot.state;
    const toolbar = h('div', { class: 'copal-treehouse-section-head' }, h('div', {}, h('h2', { text: 'Assignments' }), h('p', { text: 'Draft, publish, submit, retry, grade, and explain progress end to end.' })));
    if (ui.snapshot.permissions.author) toolbar.append(h('button', { class: 'copal-btn primary', text: '+ Assignment', onclick: () => createAssignment() }));
    root.append(toolbar);
    const list = h('div', { class: 'copal-card-grid' });
    for (const assignment of values(state.assignments)) {
      const course = state.courses[assignment.courseId]; const submission = state.submissions[`${assignment.id}:${ui.actorId}`];
      const card = h('article', { class: 'copal-card' }, h('h3', { text: assignment.title }), h('p', { text: assignment.prompt || 'No prompt.' }), h('p', { text: `${course?.title || 'Course'} · ${assignment.maxPoints} points · ${assignment.status}${assignment.dueAt ? ` · due ${new Date(assignment.dueAt).toLocaleString()}` : ''}` }));
      if (submission) card.append(h('p', { text: `Your submission: ${submission.status} · attempt ${submission.attempts}${submission.grade != null ? ` · ${submission.grade}/${assignment.maxPoints}` : ''}${submission.feedback ? ` · ${submission.feedback}` : ''}` }));
      if (ui.snapshot.permissions.author) card.append(h('button', { class: 'copal-btn', text: 'Edit', onclick: () => editAssignment(assignment) }));
      if (ui.snapshot.permissions.author && assignment.status === 'draft') card.append(h('button', { class: 'copal-btn primary', text: 'Publish', onclick: () => command('assignment.publish', { assignmentId: assignment.id }) }));
      if (ui.snapshot.permissions.author) card.append(h('button', { class: 'copal-btn danger', text: '×', title: 'Delete assignment', onclick: () => { if (window.confirm(`Delete assignment "${assignment.title}"?`)) command('assignment.delete', { assignmentId: assignment.id }); } }));
      if (ui.snapshot.permissions.learner && assignment.status === 'published' && currentEnrollment(assignment.courseId)) card.append(h('button', { class: 'copal-btn primary', text: submission ? 'Submit another attempt' : 'Submit', onclick: () => submitAssignment(assignment) }));
      list.append(card);
    }
    if (!values(state.assignments).length) list.append(h('div', { class: 'copal-empty', text: 'No assignments yet.' }));
    root.append(list);
    if (ui.snapshot.permissions.grade) {
      const grading = h('section', { class: 'copal-card copal-treehouse-grading' }, h('h2', { text: 'Submission grading' }));
      for (const submission of values(state.submissions)) grading.append(h('div', { class: 'copal-task-row' }, h('span', { text: `${state.profiles[submission.profileId]?.displayName || submission.profileId} · ${state.assignments[submission.assignmentId]?.title}` }), h('small', { text: `${submission.status} · attempt ${submission.attempts}` }), h('button', { class: 'copal-btn', text: submission.status === 'graded' ? 'Regrade' : 'Grade', onclick: () => gradeSubmission(submission) })));
      if (!values(state.submissions).length) grading.append(h('p', { text: 'No learner submissions yet.' }));
      root.append(grading);
    }
  }

  function renderAnalytics(root) {
    const state = ui.snapshot.state; const projection = ui.snapshot.projection; const mine = learnerProjection();
    root.append(h('div', { class: 'copal-treehouse-section-head' }, h('div', {}, h('h2', { text: ui.snapshot.permissions.analytics ? 'Instructor analytics' : 'My progress evidence' }), h('p', { text: 'Computed from durable events; no browser-only counters.' }))));
    if (ui.snapshot.permissions.analytics) {
      const leaderboard = h('section', { class: 'copal-card' }, h('h3', { text: 'Leaderboard' }));
      for (const [index, item] of (projection.leaderboard || []).entries()) leaderboard.append(h('div', { class: 'copal-task-row' }, h('strong', { text: `${index + 1}. ${item.displayName}` }), h('small', { text: `${item.points} points` })));
      const courses = h('section', { class: 'copal-card' }, h('h3', { text: 'Course outcomes' }));
      for (const [courseId, item] of Object.entries(projection.courses || {})) courses.append(h('div', { class: 'copal-task-row' }, h('span', { text: state.courses[courseId]?.title || courseId }), h('small', { text: `${item.enrollments} enrolled · ${item.averageProgress}% progress · ${item.completedLearners} complete${item.averageGrade == null ? '' : ` · ${item.averageGrade}% average grade`}` })));
      root.append(h('div', { class: 'copal-card-grid' }, leaderboard, courses));
      const events = h('section', { class: 'copal-card copal-treehouse-events' }, h('h3', { text: `${projection.eventCount} durable events` }));
      for (const event of [...state.events].reverse().slice(0, 100)) events.append(h('div', { class: 'copal-task-row' }, h('span', { text: event.type }), h('small', { text: `${state.profiles[event.subjectId]?.displayName || event.subjectId} · ${new Date(event.at).toLocaleString()} · ${event.id}` })));
      root.append(events);
    }
    const evidence = h('section', { class: 'copal-card copal-treehouse-events' }, h('h3', { text: 'Point explanations' }));
    for (const item of mine.pointEvidence || []) evidence.append(h('div', { class: 'copal-task-row' }, h('span', { text: item.explanation }), h('small', { text: `+${item.points} · ${item.eventId}` })));
    if (!(mine.pointEvidence || []).length) evidence.append(h('p', { text: 'No learning events have awarded points yet.' }));
    root.append(evidence);
  }

  function renderLoaded() {
    if (!ui.body || !ui.snapshot) return;
    const root = h('div', { class: 'copal-treehouse-workspace' });
    roleToolbar(root); summary(root); navigation(root);
    const content = h('section', { class: 'copal-treehouse-content' });
    if (ui.section === 'courses') renderCourses(content);
    else if (ui.section === 'skills') renderSkills(content);
    else if (ui.section === 'assignments') renderAssignments(content);
    else renderAnalytics(content);
    root.append(content); ui.body.replaceChildren(root);
  }

  function renderFailure(error) {
    ui.body?.replaceChildren(h('div', { class: 'copal-empty' }, h('h2', { text: 'TreeHouse could not load' }), h('p', { text: error.message }), h('button', { class: 'copal-btn', text: 'Retry', onclick: () => render(ui.body) })));
  }

  async function render(body) {
    ui.body = body;
    body.replaceChildren(h('div', { class: 'copal-empty', text: 'Loading TreeHouse domain…' }));
    try { if (await load()) renderLoaded(); } catch (error) { renderFailure(error); }
  }

  return { render, command, get snapshot() { return ui.snapshot; } };
}
