import { copalStorageKey } from './storage.js';
import { filesFacadeClient } from '../filesFacadeClient.js';
import { FILES_TRANSFER_MIME, parseInternalDragPayload } from '../filesSelectionModel.js';
import { openAppDestination } from './markdownRenderer.js';

// Resolve the shared dialog lazily. TreeHouse is also a small standalone
// surface in the browser and its pure state tests should not need to evaluate
// the theme/color-picker graph just to import the feature.
async function styledConfirm(message, options = {}) {
  const existing = typeof window !== 'undefined' ? window.styledConfirm : null;
  if (typeof existing === 'function') return existing(message, options);
  if (typeof document !== 'undefined' && typeof HTMLInputElement !== 'undefined') {
    const ui = await import('../ui.js');
    return ui.styledConfirm(message, options);
  }
  return false;
}

let sharedEditorPromise = null;
async function sharedSourceEditor() {
  if (!sharedEditorPromise) sharedEditorPromise = import('./codemirror.js');
  return sharedEditorPromise;
}

const SECTIONS = [['courses', 'Courses'], ['skills', 'Skills'], ['assignments', 'Assignments'], ['analytics', 'Analytics'], ['achievements', 'Achievements']];

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

export function treeHouseHandle({ courseId, moduleId = null, activityId = null, lessonId = null, accountId, workspace = 'default', capability = 'learn', grantRevision = null, catalogueRevision = null } = {}) {
  const course = String(courseId || '').trim(); const account = String(accountId || '').trim(); const scope = String(workspace || 'default').trim();
  if (!course || !account || !/^[A-Za-z0-9._-]{1,64}$/.test(scope)) throw new TypeError('TreeHouse handle scope is invalid');
  if (!['learn', 'edit'].includes(capability)) throw new TypeError('TreeHouse capability is invalid');
  return Object.freeze({ kind:'treehouse-handle', courseId:course, moduleId:moduleId == null ? null : String(moduleId), activityId:activityId == null ? null : String(activityId), lessonId:lessonId == null ? null : String(lessonId), accountId:account, workspace:scope, capability, grantRevision:grantRevision == null ? null : String(grantRevision), catalogueRevision:catalogueRevision == null ? null : String(catalogueRevision) });
}

export function treeHouseSourceClassification(source) {
  if (!source || typeof source !== 'object' || Array.isArray(source)) return { supported:false, reason:'Choose a readable Files resource for this lesson.' };
  const kind = String(source.kind || source.type || '').trim().toLowerCase();
  if (source.isDirectory || source.directory || kind === 'directory' || kind === 'folder') return { supported:false, reason:'Folders cannot be attached to a lesson.' };
  const ref = String(source.resourceRef || source.resource_ref || '').trim();
  if (!ref || source.readable === false || source.capabilities?.read === false) return { supported:false, reason:'This resource cannot be read by the current account.' };
  return { supported:true, resourceRef:ref, expectedRevision:source.expectedRevision || source.expected_revision || null };
}

function resourceReadable(resource) {
  const capabilities = resource?.capabilities;
  if (Array.isArray(capabilities)) return capabilities.some((capability) => ['read', 'open', 'download', 'copy'].includes(String(capability).toLowerCase()));
  if (capabilities && typeof capabilities === 'object') return ['read', 'open', 'download', 'copy'].some((capability) => capabilities[capability] === true);
  return resource?.readable !== false;
}

export function validateTreeHouseFilesDrop(raw, { owner = '', accountId = '', workspace = '', pane = '', provider = '', resourceKey = '', resourceRef = '', sourceRevision = null, generation = null, policyGeneration = null, selectionEpoch = null } = {}) {
  const payload = parseInternalDragPayload(raw);
  if (!payload) return { ok:false, reason:'This Files drop is not a supported lesson source.' };
  if (payload.sources.length !== 1) return { ok:false, reason:'Attach one readable Files resource at a time.' };
  const expectedOwner = String(owner || accountId || '').trim();
  if (expectedOwner && payload.owner !== expectedOwner) return { ok:false, reason:'This resource belongs to another account.' };
  if (workspace && payload.workspace !== String(workspace)) return { ok:false, reason:'This resource belongs to another workspace.' };
  if (pane && payload.pane !== String(pane)) return { ok:false, reason:'This Files gesture is no longer active.' };
  if (generation != null && Number(payload.generation) !== Number(generation)) return { ok:false, reason:'File access changed; select the resource again.' };
  const source = payload.sources[0];
  if (provider && payload.provider !== String(provider)) return { ok:false, reason:'This Files provider is no longer active.' };
  if (resourceKey && source.resource_key !== String(resourceKey)) return { ok:false, reason:'This resource selection is no longer active.' };
  if (resourceRef && source.resource_ref !== String(resourceRef)) return { ok:false, reason:'This resource reference is no longer active.' };
  if (sourceRevision && JSON.stringify(source.revision || null) !== JSON.stringify(sourceRevision)) return { ok:false, reason:'This resource revision is stale; select it again.' };
  if (policyGeneration != null && Number(payload.policy_generation) !== Number(policyGeneration)) return { ok:false, reason:'File policy changed; select the resource again.' };
  if (selectionEpoch != null && Number(payload.selection_epoch) !== Number(selectionEpoch)) return { ok:false, reason:'This Files selection is no longer active.' };
  return { ok:true, payload, source };
}

export async function prepareTreeHouseLessonAttachment({ handle, source, filesClient = filesFacadeClient, mode = 'link', signal = null, operationId = null, currentHandle = null, applyAttachment, expectedPolicyGeneration = null } = {}) {
  if (!['link', 'embed'].includes(String(mode || '').trim().toLowerCase())) throw new Error('Attachment mode is unsupported.');
  const classification = treeHouseSourceClassification(source);
  if (!classification.supported) throw new Error(classification.reason);
  if (!handle || handle.kind !== 'treehouse-handle' || handle.capability !== 'edit' || !handle.lessonId) throw new Error('This lesson does not have editor attachment access.');
  if (currentHandle && (currentHandle.accountId !== handle.accountId || currentHandle.workspace !== handle.workspace || currentHandle.grantRevision !== handle.grantRevision || currentHandle.catalogueRevision !== handle.catalogueRevision || currentHandle.capability !== 'edit')) throw new Error('Lesson access changed; the attachment was not applied.');
  if (typeof applyAttachment !== 'function') throw new TypeError('TreeHouse lesson mutation is required');
  const roots = await filesClient.roots({ copalWorkspace:handle.workspace, signal });
  const generation = Number(roots?.policy_generation);
  if (!Number.isSafeInteger(generation) || generation < 0) throw new Error('File access generation is unavailable; retry the lesson action.');
  if (expectedPolicyGeneration != null && generation !== Number(expectedPolicyGeneration)) throw new Error('File policy changed; select the resource again.');
  const op = String(operationId || `treehouse-lesson-${globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`}`);
  const sourceStat = await filesClient.stat(classification.resourceRef, { signal });
  const authorized = sourceStat?.resource || sourceStat;
  if (!authorized || authorized.kind === 'folder' || authorized.kind === 'directory' || !resourceReadable(authorized)) throw new Error('This resource cannot be read by the current account.');
  const prepared = await filesClient.prepareAttachment({ operationId:op, generation, workspace:handle.workspace, source:{ resourceRef:String(authorized.ref || classification.resourceRef), ...(classification.expectedRevision ? { expectedRevision:classification.expectedRevision } : {}) }, target:{ kind:'treehouse_lesson', courseId:handle.courseId, lessonId:handle.lessonId, expectedRevision:{ kind:'treehouse', value:JSON.stringify({ grantRevision:Number(handle.grantRevision || 0), catalogueRevision:Number(handle.catalogueRevision || 0) }) } }, mode }, { signal });
  await applyAttachment(handle, { operationId:op, preparationReceiptId:prepared.preparation_receipt_id, preparation:prepared });
  return { outcome:'prepared', operationId:op, preparation:prepared };
}

export function createTreeHouseFeature({ h, api, setStatus, renderMarkdown, openDocument, filesClient = filesFacadeClient, getScope = null }) {
  const ui = {
    actorId: 'owner',
    mode: 'learner',
    section: 'courses',
    selectedCourse: null,
    snapshot: null,
    token: 0,
    body: null,
  };
  const lessonAttachmentOperations = new Map();
  const lessonAttachmentControllers = new Map();
  const treehouseScope = () => {
    const supplied = typeof getScope === 'function' ? getScope() : (typeof window !== 'undefined' ? window.__odysseusGetActiveCopalContext?.() : null);
    return supplied && typeof supplied === 'object' ? supplied : {};
  };

  function lessonHandle(course, activity) {
    const capability = ui.snapshot?.courseCapabilities?.[course.id] || {};
    const grants = values(ui.snapshot?.state?.courseGrants).filter((grant) => grant.courseId === course.id && !grant.revokedAt);
    const grant = grants.find((item) => item.recipientId === ui.actorId || item.ownerId === ui.actorId) || grants[0];
    return treeHouseHandle({
      courseId:course.id, moduleId:activity.moduleId, activityId:activity.id,
      lessonId:activity.id, accountId:ui.snapshot?.accountId || ui.actorId,
      workspace:ui.snapshot?.workspace || treehouseScope().workspace || 'default', capability:ui.mode === 'admin' && capability.edit ? 'edit' : 'learn',
      grantRevision:grant?.revision ?? grant?.accessRevision ?? null, catalogueRevision:ui.snapshot?.state?.revision,
    });
  }

  async function attachLessonResource(course, activity, source, { mode = 'link', gestureContext = null, operationId = null, signal = null } = {}) {
    const handle = lessonHandle(course, activity);
    if (handle.capability !== 'edit') throw new Error('Learner access cannot attach a source to this lesson.');
    if (gestureContext) {
      const checked = validateTreeHouseFilesDrop(gestureContext.raw || gestureContext.payload || gestureContext, { owner:gestureContext.owner || handle.accountId, workspace:handle.workspace, pane:gestureContext.pane || '', generation:gestureContext.generation, policyGeneration:gestureContext.policyGeneration, selectionEpoch:gestureContext.selectionEpoch, provider:gestureContext.provider, resourceKey:gestureContext.resourceKey, resourceRef:gestureContext.resourceRef, sourceRevision:gestureContext.sourceRevision });
      if (!checked.ok) throw new Error(checked.reason);
      source = checked.source;
    }
    const op = String(operationId || `treehouse-lesson-${activity.id}-${source?.item_id || source?.resource_ref || 'source'}`);
    if (lessonAttachmentOperations.has(op)) return lessonAttachmentOperations.get(op);
    const current = lessonHandle(course, activity);
    const controller = typeof AbortController === 'function' ? new AbortController() : null;
    const relayAbort = () => controller?.abort();
    if (signal?.aborted) controller?.abort();
    else signal?.addEventListener?.('abort', relayAbort, { once:true });
    lessonAttachmentControllers.set(op, controller);
    const pending = prepareTreeHouseLessonAttachment({ handle, currentHandle:current, source, filesClient, mode, signal:controller?.signal || signal, operationId:op, expectedPolicyGeneration:gestureContext?.policyGeneration ?? null, applyAttachment:async (targetHandle, result) => {
      // The current S01 facade may fail closed until its TreeHouse target
      // provider is published. Keep that exact receipt/error; do not fall
      // back to a Files move or mutate curriculum through a generic route.
      const fresh = lessonHandle(course, activity);
      if (fresh.accountId !== targetHandle.accountId || fresh.workspace !== targetHandle.workspace || fresh.catalogueRevision !== targetHandle.catalogueRevision || fresh.grantRevision !== targetHandle.grantRevision || fresh.capability !== 'edit') throw new Error('Lesson access changed; preparation retained and no lesson mutation was applied.');
      const response = await command('lesson.attach_source', { courseId:targetHandle.courseId, lessonId:targetHandle.lessonId, preparationReceiptId:result.preparationReceiptId, operationId:result.operationId, expectedRevision:targetHandle.grantRevision, policyGeneration:result.preparation?.generation });
      if (!response) throw new Error('Lesson attachment was not applied.');
    }}).then((result) => { lessonAttachmentOperations.set(op, result); setStatus('Source attached to lesson'); return result; }).catch((error) => { lessonAttachmentOperations.delete(op); setStatus(error?.message || 'Lesson source attachment failed; retry from the receipt.', true); throw error; }).finally(() => { lessonAttachmentControllers.delete(op); signal?.removeEventListener?.('abort', relayAbort); });
    lessonAttachmentOperations.set(op, pending);
    return pending;
  }

  const values = (object) => Object.values(object || {});
  const csv = (value) => String(value || '').split(',').map((item) => item.trim()).filter(Boolean);
  const contextKey = (base, accountId = ui.snapshot?.accountId || ui.actorId, mode = ui.mode) => copalStorageKey(`${base}:${accountId}:${mode}`, ui.snapshot?.workspace || 'default');
  const preferenceKey = (base, accountId = ui.snapshot?.accountId || ui.actorId) => copalStorageKey(`${base}:${accountId}`, ui.snapshot?.workspace || 'default');
  const persistContext = (accountId = ui.snapshot?.accountId || ui.actorId, mode = ui.mode) => {
    localStorage.setItem(contextKey('odysseus-treehouse-section', accountId, mode), ui.section);
    localStorage.setItem(contextKey('odysseus-treehouse-course', accountId, mode), ui.selectedCourse || '');
  };
  const learnerProjection = () => ui.snapshot?.projection?.learners?.[ui.actorId] || { points: 0, badges: [], quests: [], streak: 0, courses: {}, skills: {}, pointEvidence: [] };
  const adminMode = () => ui.mode === 'admin' && !!ui.snapshot?.permissions?.author;
  const courseCanEdit = (courseId) => adminMode() && !!ui.snapshot?.courseCapabilities?.[courseId]?.edit;
  const analyticsMode = () => ui.mode === 'admin' && !!ui.snapshot?.permissions?.analytics;
  const gradingMode = () => ui.mode === 'admin' && !!ui.snapshot?.permissions?.grade;
  const currentEnrollment = (courseId) => values(ui.snapshot?.state?.enrollments).find((item) => item.courseId === courseId && item.profileId === ui.actorId);

  function lessonContext(course, activity) {
    const active = typeof window !== 'undefined' && typeof window.__odysseusGetActiveCopalContext === 'function'
      ? window.__odysseusGetActiveCopalContext() || {} : {};
    return {
      ...active,
      accountId: ui.snapshot?.accountId || active.accountId || ui.actorId,
      workspace: ui.snapshot?.workspace || active.workspace || 'default',
      view: 'treehouse', resourceKind: 'treehouse-lesson', resourceId: activity.id,
      courseId: course.id, lessonId: activity.id,
      // The activity can describe where its practice seed opens (for example
      // Timeline or Editor), but the help request itself belongs to the
      // TreeHouse surface. Keeping that distinction here preserves the
      // course/lesson context and avoids opening a second surface's guide.
      lessonTitle: activity.title, surface: 'treehouse', lessonSurface: activity.surface || null,
    };
  }

  function requestLessonHelp(course, activity) {
    const detail = lessonContext(course, activity);
    if (typeof window === 'undefined') return detail;
    window.dispatchEvent(new CustomEvent('openclank:contextual-help', { detail }));
    return detail;
  }

  function loadState() {
    ui.actorId = localStorage.getItem(copalStorageKey('odysseus-treehouse-actor')) || 'owner';
    ui.mode = localStorage.getItem(copalStorageKey('odysseus-treehouse-mode')) || 'learner';
    ui.section = localStorage.getItem(copalStorageKey('odysseus-treehouse-section')) || 'courses';
  }

  function suspendScope() {
    ui.token += 1;
    ui.lessonDropRoot?.removeEventListener?.('dragover', ui.lessonDragOver);
    ui.lessonDropRoot?.removeEventListener?.('drop', ui.lessonDrop);
    ui.lessonDropRoot = null;
    ui.lessonDragOver = null;
    ui.lessonDrop = null;
    for (const controller of lessonAttachmentControllers.values()) controller?.abort();
    lessonAttachmentControllers.clear();
    lessonAttachmentOperations.clear();
    ui.snapshot = null;
    ui.body = null;
    ui.selectedCourse = null;
    ui.actorId = 'owner';
    ui.mode = 'learner';
    ui.section = 'courses';
  }

  function field(label, control) {
    return h('label', { class: 'copal-treehouse-field' }, h('span', { text: label }), control);
  }

  async function openExpandedTextarea(control, label) {
    if (!control || typeof document === 'undefined') return false;
    const returnFocus = document.activeElement;
    const dialog = h('dialog', { class: 'copal-dialog copal-treehouse-expanded-editor', 'aria-labelledby': 'copal-treehouse-expanded-title' },
      h('h2', { id: 'copal-treehouse-expanded-title', text: `Edit ${label.toLowerCase()}` }),
      h('p', { class: 'copal-dialog-help', text: 'Use Mod-d for the next match, Mod-Shift-L for all matches, and Mod-Alt-arrow for vertical cursors.' }));
    const host = h('div', { class: 'copal-codemirror-host copal-treehouse-expanded-host' });
    const feedback = h('p', { class: 'copal-treehouse-feedback', role: 'status' });
    const apply = h('button', { class: 'copal-btn primary', type: 'button', text: 'Apply' });
    const cancel = h('button', { class: 'copal-btn', type: 'button', text: 'Cancel', onclick: () => dialog.close() });
    dialog.append(host, feedback, h('div', { class: 'copal-dialog-actions' }, cancel, apply));
    let editor = null;
    apply.addEventListener('click', () => {
      if (!editor) return;
      control.value = editor.getValue();
      control.dispatchEvent(new Event('input', { bubbles:true }));
      dialog.close();
    });
    document.body.append(dialog);
    dialog.addEventListener('close', () => { editor?.destroy?.(); dialog.remove(); returnFocus?.focus?.(); });
    dialog.showModal();
    try {
      const module = await sharedSourceEditor();
      const factory = module.createSourceEditor || module.createMarkdownEditor;
      editor = factory({ parent:host, doc:String(control.value || ''), mode:'source', language:'Markdown', lineWrapping:true });
      await editor.languageReady;
      editor.focus();
      return true;
    } catch (error) {
      feedback.textContent = `Expanded editor unavailable: ${error.message}`;
      feedback.classList.add('error');
      return false;
    }
  }

  function openForm(title, fields, submitLabel, onSubmit) {
    const returnFocus = typeof document !== 'undefined' ? document.activeElement : null;
    const dialog = h('dialog', { class: 'copal-dialog copal-treehouse-dialog', 'aria-labelledby': 'copal-treehouse-dialog-title' }, h('h2', { id: 'copal-treehouse-dialog-title', text: title }));
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
      const wrapper = field(spec.label, control);
      if (spec.type === 'textarea' && spec.expandable !== false) {
        wrapper.append(h('button', {
          class: 'copal-btn copal-treehouse-expand-editor', type: 'button', text: 'Open multi-edit',
          title: 'Open the shared editor with multiple cursors',
          onclick: (event) => { event?.preventDefault?.(); void openExpandedTextarea(control, spec.label); },
        }));
      }
      dialog.append(wrapper);
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
    document.body.append(dialog); dialog.addEventListener('close', () => { dialog.remove(); returnFocus?.focus?.(); }); dialog.showModal();
    Object.values(controls)[0]?.focus();
  }

  async function load() {
    const token = ++ui.token;
    let snapshot;
    try {
      snapshot = await api(`/treehouse?actor=${encodeURIComponent(ui.actorId)}`);
    } catch (error) {
      if (token !== ui.token) return false;
      if (ui.actorId !== 'owner' && (error.status === 404 || /profile not found|inactive/i.test(error.message))) {
        ui.actorId = 'owner'; localStorage.setItem(copalStorageKey('odysseus-treehouse-actor'), ui.actorId);
        return load();
      }
      throw error;
    }
    if (token !== ui.token) return false;
    ui.snapshot = snapshot;
    // Authenticated TreeHouse identity is server-issued.  Keep the legacy
    // profile selector only for the explicit local fixture; otherwise all
    // progress, mode context and authoring follow this account.
    if (snapshot.accountId && snapshot.actor?.id) {
      ui.actorId = snapshot.actor.id;
      ui.snapshot = snapshot;
      const accountMode = localStorage.getItem(preferenceKey('odysseus-treehouse-mode', snapshot.accountId));
      if (accountMode === 'learner' || accountMode === 'admin') ui.mode = accountMode;
      ui.section = localStorage.getItem(contextKey('odysseus-treehouse-section', snapshot.accountId, ui.mode)) || 'courses';
      ui.selectedCourse = localStorage.getItem(contextKey('odysseus-treehouse-course', snapshot.accountId, ui.mode)) || null;
      localStorage.setItem(copalStorageKey('odysseus-treehouse-actor'), ui.actorId);
      localStorage.setItem(preferenceKey('odysseus-treehouse-mode', snapshot.accountId), ui.mode);
    }
    const shareToken = new URLSearchParams(window.location.search).get('treehouseShare');
    if (shareToken && snapshot.accountId && !sessionStorage.getItem(`treehouse-share:${shareToken}`)) {
      const accepted = await command('course.accept_share', { shareToken });
      // Record success only after the server has committed the grant. Preserve
      // unrelated query state used by the surrounding Copal view.
      sessionStorage.setItem(`treehouse-share:${shareToken}`, 'accepted');
      const nextUrl = new URL(window.location.href);
      nextUrl.searchParams.delete('treehouseShare');
      window.history.replaceState({}, '', `${nextUrl.pathname}${nextUrl.search}${nextUrl.hash}`);
    }
    if (!snapshot.state.profiles[ui.actorId]) {
      ui.actorId = 'owner'; localStorage.setItem(copalStorageKey('odysseus-treehouse-actor'), ui.actorId); return load();
    }
    // Best-effort toast drain after a successful load (S12 task-identity).
    void pollAchievementNotifications();
    return true;
  }

  async function command(type, payload = {}) {
    if (!ui.snapshot) throw new Error('TreeHouse is not loaded');
    const token = ui.token;
    setStatus('Saving TreeHouse…');
    const response = await api('/treehouse/commands', {
      method: 'POST',
      body: JSON.stringify({
        type, payload, actorId: ui.actorId,
        commandId: treeHouseCommandId(type),
        expectedRevision: ui.snapshot.state.revision,
      }),
    });
    if (token !== ui.token) return null;
    ui.snapshot = response;
    setStatus(`TreeHouse saved · revision ${response.state.revision}`);
    renderLoaded();
    return response.result;
  }

  function roleToolbar(root) {
    const snapshot = ui.snapshot;
    const actor = snapshot.actor;
    const select = snapshot.accountId ? h('span', { class: 'copal-treehouse-account', text: `${snapshot.actor?.displayName || snapshot.accountId}` }) : h('select', { 'aria-label': 'TreeHouse profile' });
    if (!snapshot.accountId) {
      for (const profile of values(snapshot.state.profiles).filter((item) => item.active !== false)) {
        select.append(h('option', { value: profile.id, text: `${profile.displayName} · ${(profile.roles || []).join('/')}`, selected: profile.id === ui.actorId }));
      }
      select.value = ui.actorId;
      select.addEventListener('change', async () => {
        ui.actorId = select.value; localStorage.setItem(copalStorageKey('odysseus-treehouse-actor'), ui.actorId);
        ui.body.replaceChildren(h('div', { class: 'copal-empty', text: 'Switching TreeHouse profile…' }));
        try { await load(); renderLoaded(); } catch (error) { renderFailure(error); }
      });
    }
    const toolbar = h('div', { class: 'copal-treehouse-rolebar' }, h('strong', { text: 'TreeHouse' }), select,
      h('span', { text: `${snapshot.state.revision} revisions · ${snapshot.projection.eventCount} durable events` }));
    const modes = h('div', { class: 'copal-treehouse-mode', role: 'group', 'aria-label': 'TreeHouse mode' });
    for (const mode of ['learner', 'admin']) {
      if (mode === 'admin' && !snapshot.permissions.author) continue;
      modes.append(h('button', { class: `copal-btn${ui.mode === mode ? ' primary' : ''}`, text: mode === 'learner' ? 'Learner' : 'Admin', 'aria-pressed': String(ui.mode === mode), onclick: () => {
        if (snapshot.accountId) persistContext(snapshot.accountId, ui.mode);
        ui.mode = mode;
        if (snapshot.accountId) {
          localStorage.setItem(preferenceKey('odysseus-treehouse-mode', snapshot.accountId), mode);
          ui.section = localStorage.getItem(contextKey('odysseus-treehouse-section', snapshot.accountId, mode)) || (mode === 'learner' ? 'courses' : 'courses');
          ui.selectedCourse = localStorage.getItem(contextKey('odysseus-treehouse-course', snapshot.accountId, mode)) || null;
        }
        else localStorage.setItem(copalStorageKey('odysseus-treehouse-mode'), mode);
        if (mode === 'learner' && !['courses', 'analytics', 'achievements'].includes(ui.section)) ui.section = 'courses';
        renderLoaded();
      } }));
    }
    toolbar.append(modes);
    if (adminMode() && snapshot.permissions.admin && !snapshot.accountId) toolbar.append(h('button', { class: 'copal-btn', text: '+ Profile', onclick: createProfile }));
    if (adminMode() && snapshot.permissions.author) toolbar.append(h('button', { class: 'copal-btn', text: 'Import legacy', onclick: migrateLegacy }));
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
    const visibleCourseCount = values(ui.snapshot?.state?.courses).filter((course) => !course.deletedAt).length;
    const resetScope = ui.snapshot?.accountId ? `${ui.snapshot.actor?.displayName || ui.snapshot.accountId} in ${ui.snapshot.workspace || 'this workspace'}` : `${ui.actorId} in this workspace`;
    const reset = h('button', { class: 'copal-btn danger', text: 'Reset my progress', onclick: async () => {
      if (!await styledConfirm(`Reset Class progress for ${visibleCourseCount} visible course${visibleCourseCount === 1 ? '' : 's'} for ${resetScope}? This clears your learner progress, submissions, evidence, and course completion awards. Account-wide achievements earned in the House are lifetime records and stay with you. Curricula and other learners stay intact.`, { title: 'Reset TreeHouse progress', confirmText: 'Reset progress', danger: true })) return;
      await command('progress.reset');
    } });
    root.append(cards, h('div', { class: 'copal-treehouse-actions' }, reset));
  }

  function navigation(root) {
    const nav = h('nav', { class: 'copal-treehouse-nav', 'aria-label': 'TreeHouse sections' });
    const allowed = ui.mode === 'admin' ? SECTIONS : SECTIONS.filter(([id]) => ['courses', 'analytics', 'achievements'].includes(id));
    for (const [id, label] of allowed) nav.append(h('button', { type: 'button', class: `copal-btn${ui.section === id ? ' primary' : ''}`, text: label, 'aria-current': ui.section === id ? 'page' : false, onclick: () => { ui.section = id; persistContext(); renderLoaded(); } }));
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
    if (!await styledConfirm(`Import ${counts.documents} legacy document(s) as ${counts.courses} course(s), ${counts.skills} skill(s), and ${counts.tasks} assignment(s)? Source documents will not be changed.`, { title: 'Import TreeHouse content', confirmText: 'Import' })) return;
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

  function shareCourse(course) {
    const recipientOptions = (ui.snapshot.recipientOptions || []).map((item) => ({ value: item.accountId, label: `${item.username} · ${item.accountId}` }));
    openForm(`Share · ${course.title}`, [
      recipientOptions.length
        ? { id: 'recipientId', label: 'Recipient account', type: 'select', options: recipientOptions }
        : { id: 'recipientId', label: 'Recipient username or account ID' },
      { id: 'capability', label: 'Access', type: 'select', options: [
        { value: 'learn', label: 'Learner' }, { value: 'edit', label: 'Editor' },
      ] },
    ], 'Create share link', async ({ recipientId, capability }) => {
      const result = await command('course.share', { courseId: course.id, recipientId, capability });
      if (result?.shareToken) {
        const shareUrl = new URL(window.location.href);
        shareUrl.searchParams.set('treehouseShare', result.shareToken);
        try { await navigator.clipboard?.writeText(shareUrl.toString()); } catch (_) { /* clipboard is optional */ }
        setStatus(`Share link created${navigator.clipboard ? ' and copied' : ''}. Revoke it from this course card.`);
      }
    });
  }

  async function revokeCourseShare(course, grant) {
    if (!await styledConfirm(`Revoke ${grant.capability} access for ${grant.recipientId}?`, { title: 'Revoke course share', confirmText: 'Revoke access', danger: true })) return;
    await command('course.revoke_share', { grantId: grant.id });
  }

  async function exportCoursePackage(course) {
    try {
      const packageData = await api(`/treehouse/courses/${encodeURIComponent(course.id)}/export`);
      const blob = new Blob([JSON.stringify(packageData, null, 2)], { type: 'application/json' });
      const link = document.createElement('a'); link.href = URL.createObjectURL(blob);
      link.download = `${String(course.title || course.id).replace(/[^a-z0-9_-]+/gi, '-').replace(/^-|-$/g, '') || 'course'}.copal-course.json`;
      link.click(); URL.revokeObjectURL(link.href); setStatus(`Exported ${course.title}`);
    } catch (error) { setStatus(`Course export failed: ${error?.message || error}`); }
  }

  async function exportCoursePackageToFiles(course, destinationRef, { collision = 'fail', signal = null } = {}) {
    const destination = String(destinationRef || '').trim();
    if (!destination) throw new TypeError('Authorized Files destination is required');
    const packageData = await api(`/treehouse/courses/${encodeURIComponent(course.id)}/export`);
    const bytes = new Blob([JSON.stringify(packageData, null, 2)], { type:'application/json' });
    const scope = treehouseScope();
    const roots = await filesClient.roots({ copalWorkspace:ui.snapshot?.workspace || scope.workspace || 'default', signal });
    const generation = Number(roots?.policy_generation);
    if (!Number.isSafeInteger(generation) || generation < 0) throw new Error('File access generation is unavailable; retry the export.');
    const operationId = `treehouse-course-export-${course.id}-${packageData.packageId || treeHouseCommandId('package')}`;
    const receipt = await filesClient.importFile(bytes, { operationId, itemId:String(course.id), generation, destinationRef:destination, name:`${String(course.title || course.id).replace(/[^a-z0-9_-]+/gi, '-').replace(/^-|-$/g, '') || 'course'}.copal-course.json`, collision, signal });
    setStatus(`Exported ${course.title} to Files`);
    return { coursePackage:packageData, filesReceipt:receipt, operationId };
  }

  function importCoursePackage() {
    const input = h('input', { type: 'file', accept: '.json,application/json' });
    input.addEventListener('change', async () => {
      const file = input.files?.[0]; if (!file) return;
      try {
        const packageData = JSON.parse(await file.text());
        await api('/treehouse/courses/import', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(packageData) });
        setStatus(`Imported ${packageData.course?.title || packageData.course?.id || 'course'}`); await load();
      } catch (error) { setStatus(`Course import failed: ${error?.message || error}`); }
    });
    input.click();
  }

  function courseCard(course, root) {
    const state = ui.snapshot.state; const progress = learnerProjection().courses?.[course.id];
    const modules = course.moduleIds.map((id) => state.modules[id]).filter(Boolean);
    const card = h('article', { class: 'copal-card copal-treehouse-course', 'data-copal-context-object':'treehouse', 'data-treehouse-id':course.id },
      h('header', {}, h('h3', { text: course.title }), h('span', { class: `copal-treehouse-state ${course.status}`, text: course.status })),
      h('p', { text: course.description || 'No description yet.' }),
      h('p', { text: `${modules.length} module${modules.length === 1 ? '' : 's'}${progress ? ` · ${progress.percent}% complete` : ''}` }));
    const actions = h('div', { class: 'copal-treehouse-actions' }, h('button', { class: 'copal-btn', text: ui.selectedCourse === course.id ? 'Hide' : 'Open', onclick: () => { ui.selectedCourse = ui.selectedCourse === course.id ? null : course.id; persistContext(); renderLoaded(); } }));
    if (ui.snapshot.permissions.learner && course.status === 'published' && !currentEnrollment(course.id)) actions.append(h('button', { class: 'copal-btn primary', text: 'Enroll', onclick: () => enroll(course.id) }));
    if (ui.snapshot.permissions.learner && currentEnrollment(course.id)) actions.append(h('button', { class: 'copal-btn danger', text: 'Unenroll', onclick: async () => { if (await styledConfirm('Withdraw from this course?', { title: 'Withdraw from course', confirmText: 'Withdraw', danger: true })) await command('enrollment.unenroll', { courseId: course.id }); } }));
    const capability = ui.snapshot.courseCapabilities?.[course.id] || {};
    if (adminMode() && capability.edit) {
      actions.append(h('button', { class: 'copal-btn', text: 'Edit', onclick: () => editCourse(course) }));
      actions.append(h('button', { class: 'copal-btn', text: '+ Module', onclick: () => createModule(course.id) }));
      if (course.status === 'draft') actions.append(h('button', { class: 'copal-btn primary', text: 'Publish', onclick: () => command('course.publish', { courseId: course.id }) }));
      if (course.status !== 'archived') actions.append(h('button', { class: 'copal-btn danger', text: 'Archive', onclick: () => command('course.archive', { courseId: course.id }) }));
      actions.append(h('button', { class: 'copal-btn danger', text: 'Delete', onclick: async () => { if (await styledConfirm('Delete this course and all its content? This cannot be undone.', { title: 'Delete course', confirmText: 'Delete course', danger: true })) await command('course.delete', { courseId: course.id }); } }));
      actions.append(h('button', { class: 'copal-btn', text: 'Share', onclick: () => shareCourse(course) }));
      actions.append(h('button', { class: 'copal-btn', text: 'Export', 'aria-label': `Export ${course.title}`, onclick: () => exportCoursePackage(course) }));
      for (const grant of values(ui.snapshot.state.courseGrants).filter((item) => item.courseId === course.id && !item.revokedAt && item.ownerId === ui.actorId)) {
        actions.append(h('button', { class: 'copal-btn danger', text: `Revoke ${grant.recipientId}`, onclick: () => revokeCourseShare(course, grant) }));
      }
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
      if (courseCanEdit(course.id)) moduleHeader.append(h('div', { class: 'copal-treehouse-order' },
        h('button', { class: 'copal-btn', text: 'Edit', 'aria-label': `Edit ${module.title}`, onclick: () => editModule(module) }),
        h('button', { class: 'copal-btn', text: '↑', title: 'Move module earlier', 'aria-label': `Move ${module.title} earlier`, disabled: moduleIndex === 0, onclick: () => command('course.reorder_modules', { courseId: course.id, moduleIds: moveTreeHouseItem(course.moduleIds, module.id, -1) }) }),
        h('button', { class: 'copal-btn', text: '↓', title: 'Move module later', 'aria-label': `Move ${module.title} later`, disabled: moduleIndex === course.moduleIds.length - 1, onclick: () => command('course.reorder_modules', { courseId: course.id, moduleIds: moveTreeHouseItem(course.moduleIds, module.id, 1) }) }),
        h('button', { class: 'copal-btn danger', text: 'Delete', 'aria-label': `Delete ${module.title}`, onclick: async () => { if (await styledConfirm(`Delete module "${module.title}" and all its content?`, { title: 'Delete module', confirmText: 'Delete module', danger: true })) await command('module.delete', { moduleId: module.id }); } })));
      const moduleCard = h('article', { class: 'copal-card copal-treehouse-module', 'data-copal-context-object':'treehouse', 'data-treehouse-id':module.id }, moduleHeader);
      if (module.description) moduleCard.append(h('p', { text: module.description }));
      for (const [activityIndex, activityId] of module.activityIds.entries()) {
        const activity = state.activities[activityId]; if (!activity || activity.status === 'archived') continue;
        const complete = progress.completedActivityIds?.includes(activityId);
        const row = h('div', { class: `copal-treehouse-activity${complete ? ' complete' : ''}`, 'data-copal-context-object':'treehouse', 'data-treehouse-id':activity.id, 'data-field-guide-surface': activity.surface?.key || '', 'data-field-guide-lesson': activity.fieldGuideKey || activity.id },
          h('div', {}, h('strong', { text: activity.title }), h('small', { text: `${activity.activityType} · ${activity.points} points${activity.skillIds?.length ? ` · ${activity.skillIds.length} skills` : ''}` })),
          h('span', { text: complete ? 'Completed' : activity.status }));
        if (activity.content) row.append(h('details', {}, h('summary', { text: 'Open lesson' }), h('div', { class: 'copal-meme-body' }, renderMarkdown(activity.content))));
        const practice = activity.practice || {};
        if (practice.seed || activity.verifierSpec?.evidence) {
          const practiceBody = h('div', { class: 'copal-treehouse-practice', 'aria-label': 'Disposable practice and verifier' },
            h('strong', { text: `Practice fixture: ${practice.title || activity.practiceFixture || 'disposable exercise'}` }),
            h('p', { text: practice.expectedEvidence || activity.verifierSpec?.evidence || 'Complete the exercise and inspect the saved result.' }));
          if (practice.seed) practiceBody.append(h('pre', { class: 'copal-treehouse-practice-seed', tabindex: '0' }, practice.seed));
          row.append(practiceBody);
        }
        // Hidden achievement-linked components.  Learner mode never names an
        // unearned ultra rare and shows mystery entries as ???.  Ultra-rares
        // are omitted entirely so no empty secret container is rendered.
        const adminSpoilers = adminMode() && ui.snapshot?.permissions?.admin;
        const hints = activity.achievementHints || [];
        const visibleHints = hints.filter((hint) => hint.rarity !== 'ultra' || adminSpoilers);
        if (visibleHints.length) {
          row.append(h('div', { class: 'copal-treehouse-lesson-hints', 'aria-label': 'Related achievements' },
            visibleHints.map((hint) => h('small', {
              class: `copal-treehouse-lesson-hint copal-treehouse-lesson-hint-${hint.rarity}`,
              'data-achievement-hint': hint.id,
              text: hint.secret && !adminSpoilers ? '???' : hint.id,
            }))));
        }
        const surface = activity.surface || {};
        const lessonActions = h('div', { class: 'copal-treehouse-actions', 'aria-label': 'Lesson actions' });
        const openDestination = (event) => {
          // Shared app-link resolver: focus the destination view while
          // preserving chat identity and unsaved editor drafts. Never a
          // full page reload and never a legacy /copal/* route.
          const target = String(surface.appLink || (surface.key ? `clank://${surface.key}` : '')).trim();
          if (target) {
            event?.preventDefault?.();
            openAppDestination(target, event);
            return;
          }
          if (surface.href && surface.href !== '#') return; // let the anchor navigate
          event?.preventDefault?.();
        };
        lessonActions.append(h('a', {
          class: 'copal-btn',
          href: surface.href || '#',
          'data-app-destination': surface.appLink || (surface.key ? `clank://${surface.key}` : ''),
          'aria-label': `Open ${surface.label || 'lesson'} destination`,
          title: `Open ${surface.label || 'lesson'} destination`,
          text: `Open ${surface.label || 'destination'}`,
          onclick: openDestination,
        }));
        lessonActions.append(h('button', { type: 'button', class: 'copal-btn', 'aria-label': `Ask for help with ${activity.title}`, title: 'Attach this lesson and its active workspace context to help', text: 'Ask for help', onclick: () => requestLessonHelp(course, activity) }));
        if (courseCanEdit(course.id)) lessonActions.append(h('button', { type:'button', class:'copal-btn', 'aria-label':`Attach Files resource to ${activity.title}`, title:'Drop one readable Files resource here to attach it to this lesson', text:'Attach Files resource', onclick:() => setStatus('Drop one readable Files resource on this lesson to attach it.', false) }));
        row.append(lessonActions);
        if (courseCanEdit(course.id)) row.append(h('div', { class: 'copal-treehouse-order' },
          h('button', { class: 'copal-btn', text: 'Edit', 'aria-label': `Edit ${activity.title}`, onclick: () => editActivity(activity) }),
          h('button', { class: 'copal-btn', text: '↑', title: 'Move activity earlier', 'aria-label': `Move ${activity.title} earlier`, disabled: activityIndex === 0, onclick: () => command('module.reorder_items', { moduleId: module.id, activityIds: moveTreeHouseItem(module.activityIds, activity.id, -1), assignmentIds: module.assignmentIds }) }),
          h('button', { class: 'copal-btn', text: '↓', title: 'Move activity later', 'aria-label': `Move ${activity.title} later`, disabled: activityIndex === module.activityIds.length - 1, onclick: () => command('module.reorder_items', { moduleId: module.id, activityIds: moveTreeHouseItem(module.activityIds, activity.id, 1), assignmentIds: module.assignmentIds }) }),
          h('button', { class: 'copal-btn danger', text: '×', title: 'Delete activity', 'aria-label': `Delete ${activity.title}`, onclick: async () => { if (await styledConfirm(`Delete activity "${activity.title}"?`, { title: 'Delete activity', confirmText: 'Delete activity', danger: true })) await command('activity.delete', { activityId: activity.id }); } })));
        if (ui.snapshot.permissions.learner && currentEnrollment(course.id) && activity.status === 'published' && !complete) row.append(h('button', { class: 'copal-btn primary', text: 'Mark complete', onclick: () => command('activity.complete', { activityId }) }));
        moduleCard.append(row);
      }
      if (courseCanEdit(course.id)) moduleCard.append(h('button', { class: 'copal-btn', text: '+ Activity', onclick: () => createActivity(module.id) }), h('button', { class: 'copal-btn', text: '+ Assignment', onclick: () => createAssignment(module.id) }));
      detail.append(moduleCard);
    }
    root.append(detail);
  }

  function renderCourses(root) {
    const toolbar = h('div', { class: 'copal-treehouse-section-head' }, h('div', {}, h('h2', { text: 'Courses' }), h('p', { text: 'Author, publish, enroll, navigate, and complete durable learning paths.' })));
    if (adminMode()) toolbar.append(h('button', { class: 'copal-btn primary', text: '+ Course', onclick: createCourse }), h('button', { class: 'copal-btn', text: 'Import course', onclick: importCoursePackage }));
    root.append(toolbar);
    const grid = h('div', { class: 'copal-card-grid' });
    const courses = values(ui.snapshot.state.courses).filter((item) => adminMode() ? item.status !== 'archived' : item.status === 'published');
    for (const course of courses) courseCard(course, grid);
    if (!courses.length) grid.append(h('div', { class: 'copal-empty', text: adminMode() ? 'No courses yet. Create the first draft.' : 'No published courses are available.' }));
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
    if (adminMode()) toolbar.append(h('button', { class: 'copal-btn primary', text: '+ Skill', onclick: createSkill }), h('button', { class: 'copal-btn', text: '+ Badge', onclick: createBadge }), h('button', { class: 'copal-btn', text: '+ Quest', onclick: createQuest }));
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
      if (adminMode()) {
        card.append(h('button', { class: 'copal-btn', text: 'Edit skill', onclick: () => editSkill(skill) }));
        card.append(h('button', { class: 'copal-btn danger', text: '×', title: 'Delete skill', onclick: async () => { if (await styledConfirm(`Delete skill "${skill.title}"?`, { title: 'Delete skill', confirmText: 'Delete skill', danger: true })) await command('skill.delete', { skillId: skill.id }); } }));
      }
      grid.append(card);
    }
    if (!values(state.skills).length) grid.append(h('div', { class: 'copal-empty', text: 'No skills have been defined.' }));
    root.append(grid);
    const evidence = h('section', { class: 'copal-card copal-treehouse-evidence' }, h('h2', { text: 'Evidence review' }));
    const visible = values(state.evidence);
    for (const item of visible) {
      const row = h('div', { class: 'copal-task-row' }, h('span', { text: `${state.profiles[item.profileId]?.displayName || item.profileId}: ${item.description}` }), h('small', { text: `${state.skills[item.skillId]?.title || item.skillId} · ${item.status}` }));
      if (gradingMode() && item.status === 'pending') row.append(h('button', { class: 'copal-btn primary', text: 'Approve', onclick: () => command('evidence.review', { evidenceId: item.id, decision: 'approved', note: 'Verified in TreeHouse' }) }), h('button', { class: 'copal-btn danger', text: 'Reject', onclick: () => command('evidence.review', { evidenceId: item.id, decision: 'rejected', note: 'Needs more evidence' }) }));
      evidence.append(row);
    }
    if (!visible.length) evidence.append(h('p', { text: 'No evidence submissions yet.' }));
    root.append(evidence);
    const rewards = h('div', { class: 'copal-card-grid' });
    const badgeSection = h('section', { class: 'copal-card' }, h('h3', { text: 'Badges' }));
    for (const badge of values(state.badges)) {
      const earned = progress.badges?.some((item) => item.badgeId === badge.id);
      const row = h('p', { text: `${earned ? '✓' : '○'} ${badge.title}` });
      if (adminMode()) {
        row.append(h('button', { class: 'copal-btn', text: 'Edit', onclick: () => editBadge(badge) }));
        row.append(h('button', { class: 'copal-btn danger', text: '×', title: 'Delete badge', onclick: async () => { if (await styledConfirm(`Delete badge "${badge.title}"?`, { title: 'Delete badge', confirmText: 'Delete badge', danger: true })) await command('badge.delete', { badgeId: badge.id }); } }));
      }
      badgeSection.append(row);
    }
    if (!values(state.badges).length) badgeSection.append(h('p', { text: 'No badges yet.' }));
    rewards.append(badgeSection);
    const questSection = h('section', { class: 'copal-card' }, h('h3', { text: 'Quests' }));
    for (const quest of values(state.quests)) {
      const done = progress.quests?.some((item) => item.questId === quest.id);
      const row = h('p', { text: `${done ? '✓' : '○'} ${quest.title} · ${quest.rewardPoints} points` });
      if (adminMode()) {
        row.append(h('button', { class: 'copal-btn', text: 'Edit', onclick: () => editQuest(quest) }));
        row.append(h('button', { class: 'copal-btn danger', text: '×', title: 'Delete quest', onclick: async () => { if (await styledConfirm(`Delete quest "${quest.title}"?`, { title: 'Delete quest', confirmText: 'Delete quest', danger: true })) await command('quest.delete', { questId: quest.id }); } }));
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
    if (adminMode()) toolbar.append(h('button', { class: 'copal-btn primary', text: '+ Assignment', onclick: () => createAssignment() }));
    root.append(toolbar);
    const list = h('div', { class: 'copal-card-grid' });
    for (const assignment of values(state.assignments)) {
      const course = state.courses[assignment.courseId]; const submission = state.submissions[`${assignment.id}:${ui.actorId}`];
      const card = h('article', { class: 'copal-card' }, h('h3', { text: assignment.title }), h('p', { text: assignment.prompt || 'No prompt.' }), h('p', { text: `${course?.title || 'Course'} · ${assignment.maxPoints} points · ${assignment.status}${assignment.dueAt ? ` · due ${new Date(assignment.dueAt).toLocaleString()}` : ''}` }));
      if (submission) card.append(h('p', { text: `Your submission: ${submission.status} · attempt ${submission.attempts}${submission.grade != null ? ` · ${submission.grade}/${assignment.maxPoints}` : ''}${submission.feedback ? ` · ${submission.feedback}` : ''}` }));
      if (courseCanEdit(assignment.courseId)) card.append(h('button', { class: 'copal-btn', text: 'Edit', onclick: () => editAssignment(assignment) }));
      if (courseCanEdit(assignment.courseId) && assignment.status === 'draft') card.append(h('button', { class: 'copal-btn primary', text: 'Publish', onclick: () => command('assignment.publish', { assignmentId: assignment.id }) }));
      if (courseCanEdit(assignment.courseId)) card.append(h('button', { class: 'copal-btn danger', text: '×', title: 'Delete assignment', onclick: async () => { if (await styledConfirm(`Delete assignment "${assignment.title}"?`, { title: 'Delete assignment', confirmText: 'Delete assignment', danger: true })) await command('assignment.delete', { assignmentId: assignment.id }); } }));
      if (ui.snapshot.permissions.learner && assignment.status === 'published' && currentEnrollment(assignment.courseId)) card.append(h('button', { class: 'copal-btn primary', text: submission ? 'Submit another attempt' : 'Submit', onclick: () => submitAssignment(assignment) }));
      list.append(card);
    }
    if (!values(state.assignments).length) list.append(h('div', { class: 'copal-empty', text: 'No assignments yet.' }));
    root.append(list);
    if (gradingMode()) {
      const grading = h('section', { class: 'copal-card copal-treehouse-grading' }, h('h2', { text: 'Submission grading' }));
      for (const submission of values(state.submissions)) {
        const assignment = state.assignments[submission.assignmentId];
        if (!assignment || !courseCanEdit(assignment.courseId)) continue;
        grading.append(h('div', { class: 'copal-task-row' }, h('span', { text: `${state.profiles[submission.profileId]?.displayName || submission.profileId} · ${assignment.title}` }), h('small', { text: `${submission.status} · attempt ${submission.attempts}` }), h('button', { class: 'copal-btn', text: submission.status === 'graded' ? 'Regrade' : 'Grade', onclick: () => gradeSubmission(submission) })));
      }
      if (!values(state.submissions).length) grading.append(h('p', { text: 'No learner submissions yet.' }));
      root.append(grading);
    }
  }

  // -- Achievements (S29) ------------------------------------------------

  async function achievementApi(path, options = {}) {
    return api(`/treehouse/achievements${path}`, options);
  }

  async function showAchievementToast(note) {
    // S12 task-identity: when the award evidence names an original task chat,
    // the toast action opens that durable session — never a substitute chat.
    const openOriginal = () => {
      if (note.sessionId && window.sessionModule?.selectSession) {
        window.sessionModule.selectSession(note.sessionId);
      }
    };
    let showToast = window.uiModule?.showToast;
    if (!showToast) {
      try {
        const ui = await import('../ui.js');
        showToast = ui.showToast;
      } catch (_) { showToast = null; }
    }
    const label = note.title || note.achievementKey || 'Achievement';
    const body = note.rarity === 'ultra' ? `Ultra rare earned: ${label}` : `Achievement earned: ${label}`;
    if (showToast) {
      showToast(body, {
        duration: 12000,
        action: note.sessionId ? { label: 'Open task', onClick: openOriginal } : undefined,
      });
    }
    // Also fire a browser Notification when permitted; clicking opens the
    // same original task chat when the award names one.
    try {
      if (typeof Notification !== 'undefined' && Notification.permission === 'granted') {
        const ntf = new Notification(body, {
          tag: 'achievement-' + (note.achievementId || note.outboxId || label),
          icon: '/static/favicon.ico',
        });
        if (note.sessionId) ntf.onclick = () => { window.focus(); openOriginal(); };
      }
    } catch (_) {}
  }

  async function pollAchievementNotifications() {
    // Dedupe by award ID across reconnects/tabs via the durable outbox.
    // Grouped bursts (historical backfill) emit one summary toast.
    if (!ui.snapshot?.accountId) return;
    if (ui._achievementPolling) return;
    ui._achievementPolling = true;
    try {
      const data = await achievementApi('/notifications?limit=20');
      const notes = data.notifications || [];
      if (!notes.length) return;
      const batches = new Map();
      for (const note of notes) batches.set(note.batchId || note.achievementId, note);
      if (batches.size === 1 && (notes[0].batchId === 'backfill' || notes.length > 1)) {
        // One catch-up summary rather than dozens of toasts.
        if (notes.length > 1) {
          await showAchievementToast({ title: `${notes.length} achievements from earlier activity`, achievementId: 'batch', outboxId: notes[0].outboxId });
          for (const note of notes) {
            try { await achievementApi(`/notifications/${encodeURIComponent(note.outboxId)}/delivered`, { method: 'POST' }); } catch (_) {}
          }
          return;
        }
      }
      for (const note of notes) {
        await showAchievementToast(note);
        try { await achievementApi(`/notifications/${encodeURIComponent(note.outboxId)}/delivered`, { method: 'POST' }); }
        catch (_) {
          // A failed toast cannot erase the durable award; leave the outbox
          // row for retry rather than marking delivered.
          try { await achievementApi(`/notifications/${encodeURIComponent(note.outboxId)}/failed`, { method: 'POST' }); } catch (_) {}
        }
      }
    } catch (_) {
      // Polling is best-effort; the achievements view remains the durable record.
    } finally {
      ui._achievementPolling = false;
    }
  }

  function renderAchievements(root) {
    const toolbar = h('div', { class: 'copal-treehouse-section-head' }, h('div', {}, h('h2', { text: 'Achievements' }), h('p', { text: 'Account-wide lifetime awards earned from real activity. Deterministic receipts only — no model judges your work.' })));
    const host = h('div', { class: 'copal-empty', text: 'Loading achievements…' });
    root.append(toolbar, host);
    const adminSpoilers = adminMode() && ui.snapshot?.permissions?.admin;
    achievementApi(`?admin=${adminSpoilers ? 'true' : 'false'}`).then((presentation) => {
      const counter = h('section', { class: 'copal-card' },
        h('h3', { text: 'Progress' }),
        h('strong', { text: presentation.counter || '0/34' }),
        h('small', { text: adminSpoilers ? 'admin spoilers on' : 'mystery entries show ??? until earned' }),
      );
      const list = h('div', { class: 'copal-treehouse-achievements', role: 'list', 'aria-label': 'Achievement catalog' });
      for (const entry of presentation.entries || []) {
        const locked = entry.locked && !entry.earned;
        const title = entry.earned ? entry.title : (entry.rarity === 'mystery' ? '???' : (adminSpoilers ? entry.title : entry.title));
        const item = h('article', {
          class: `copal-card copal-achievement copal-achievement-${entry.rarity}${entry.earned ? ' earned' : ''}`,
          role: 'listitem',
          'data-achievement-id': entry.id,
        },
          h('h4', { text: title }),
          h('p', { text: entry.earned || adminSpoilers || entry.rarity === 'normal' ? (entry.summary || '') : '' }),
          h('small', { text: entry.earned ? 'Earned' : (entry.rarity === 'ultra' ? 'Ultra rare' : (entry.rarity === 'mystery' ? '???' : 'Locked')) }),
        );
        list.append(item);
      }
      host.replaceChildren(counter, list);
      void pollAchievementNotifications();
    }).catch((error) => {
      host.replaceChildren(h('div', { class: 'copal-empty' }, h('p', { text: error.message || 'Achievements unavailable' })));
    });
  }

  function renderAnalytics(root) {
    const state = ui.snapshot.state; const projection = ui.snapshot.projection; const mine = learnerProjection();
    root.append(h('div', { class: 'copal-treehouse-section-head' }, h('div', {}, h('h2', { text: analyticsMode() ? 'Instructor analytics' : 'My progress evidence' }), h('p', { text: 'Computed from durable events; no browser-only counters.' }))));
    if (analyticsMode()) {
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
    const root = h('div', { class: 'copal-treehouse-workspace', role: 'region', 'aria-label': 'TreeHouse workspace' });
    const hasFilesPayload = (event) => [...(event.dataTransfer?.types || [])].includes(FILES_TRANSFER_MIME);
    const currentActivity = (node) => {
      const id = String(node?.dataset?.fieldGuideLesson || '').trim();
      if (!id) return null;
      const activity = values(ui.snapshot?.state?.activities).find((item) => String(item.fieldGuideKey || item.id) === id || String(item.id) === id);
      if (!activity) return null;
      const course = values(ui.snapshot?.state?.courses).find((item) => item.moduleIds?.includes(activity.moduleId) || item.moduleIds?.some((moduleId) => ui.snapshot.state.modules[moduleId]?.activityIds?.includes(activity.id)));
      return course ? { course, activity } : null;
    };
    const lessonDragOver = (event) => {
      const target = event.target?.closest?.('[data-copal-context-object="treehouse"]');
      if (!target || (!hasFilesPayload(event) && !(event.dataTransfer?.files?.length))) return;
      event.preventDefault();
      const lesson = currentActivity(target);
      if (!hasFilesPayload(event)) { event.dataTransfer.dropEffect = 'none'; setStatus('Browser files must be explicitly imported before lesson attachment.', true); return; }
      const checked = validateTreeHouseFilesDrop(event.dataTransfer.getData(FILES_TRANSFER_MIME), { workspace:ui.snapshot?.workspace || treehouseScope().workspace || 'default', pane:treehouseScope().pane || '', generation:null });
      event.dataTransfer.dropEffect = lesson?.course && lessonHandle(lesson.course, lesson.activity).capability === 'edit' && checked.ok ? 'copy' : 'none';
      if (!lesson) setStatus('Drop a Files resource on a lesson attachment target.', true);
      else if (!checked.ok) setStatus(checked.reason, true);
      else if (lessonHandle(lesson.course, lesson.activity).capability !== 'edit') setStatus('Learner access cannot attach a source to this lesson.', true);
    };
    const lessonDrop = (event) => {
      const target = event.target?.closest?.('[data-copal-context-object="treehouse"]');
      if (!target) return;
      event.preventDefault(); event.stopPropagation();
      const lesson = currentActivity(target);
      if (!lesson) { setStatus('This TreeHouse card is not a lesson attachment target.', true); return; }
      if (!hasFilesPayload(event)) { setStatus('Browser files must be explicitly imported before lesson attachment.', true); return; }
      const handle = lessonHandle(lesson.course, lesson.activity);
      if (handle.capability !== 'edit') { setStatus('Learner access cannot attach a source to this lesson.', true); return; }
      const checked = validateTreeHouseFilesDrop(event.dataTransfer.getData(FILES_TRANSFER_MIME), { owner:treehouseScope().owner || handle.accountId, workspace:handle.workspace, pane:treehouseScope().pane || '', generation:null });
      if (!checked.ok) { setStatus(checked.reason, true); return; }
      void attachLessonResource(lesson.course, lesson.activity, checked.source, { gestureContext:{ raw:event.dataTransfer.getData(FILES_TRANSFER_MIME), owner:treehouseScope().owner || handle.accountId, accountId:handle.accountId, workspace:handle.workspace, pane:treehouseScope().pane || '', generation:checked.payload.generation, policyGeneration:checked.payload.policy_generation, selectionEpoch:checked.payload.selection_epoch, provider:checked.payload.provider, resourceKey:checked.source.resource_key, resourceRef:checked.source.resource_ref, sourceRevision:checked.source.revision } });
    };
    root.addEventListener('dragover', lessonDragOver);
    root.addEventListener('drop', lessonDrop);
    ui.lessonDropRoot = root; ui.lessonDragOver = lessonDragOver; ui.lessonDrop = lessonDrop;
    roleToolbar(root); summary(root); navigation(root);
    const content = h('section', { class: 'copal-treehouse-content' });
    if (ui.section === 'courses') renderCourses(content);
    else if (ui.section === 'skills') renderSkills(content);
    else if (ui.section === 'assignments') renderAssignments(content);
    else if (ui.section === 'achievements') renderAchievements(content);
    else renderAnalytics(content);
    root.append(content); ui.body.replaceChildren(root);
  }

  function renderFailure(error) {
    ui.body?.replaceChildren(h('div', { class: 'copal-empty' }, h('h2', { text: 'TreeHouse could not load' }), h('p', { text: error.message }), h('button', { class: 'copal-btn', text: 'Retry', onclick: () => render(ui.body) })));
  }

  async function handleContextCommand(command, target) {
    if (command !== 'open-treehouse-item') return false;
    const id = String(target?.dataset?.treehouseId || '').trim();
    if (!id || !ui.snapshot) return false;
    const state = ui.snapshot.state || {};
    const course = values(state.courses).find((item) => item.id === id);
    if (course) {
      ui.section = 'courses';
      ui.selectedCourse = ui.selectedCourse === course.id ? null : course.id;
      persistContext();
      renderLoaded();
      return true;
    }
    const module = values(state.modules).find((item) => item.id === id);
    if (module) {
      const parent = values(state.courses).find((item) => item.moduleIds?.includes(module.id));
      if (parent) { ui.section = 'courses'; ui.selectedCourse = parent.id; persistContext(); renderLoaded(); }
      return Boolean(parent);
    }
    const activity = values(state.activities).find((item) => item.id === id);
    if (!activity) return false;
    const parent = values(state.courses).find((courseItem) => courseItem.moduleIds?.some((moduleId) => state.modules[moduleId]?.activityIds?.includes(activity.id)));
    if (parent) { ui.section = 'courses'; ui.selectedCourse = parent.id; persistContext(); renderLoaded(); }
    const href = String(activity.surface?.href || '').trim();
    const appLink = String(activity.surface?.appLink || (activity.surface?.key ? `clank://${activity.surface.key}` : '')).trim();
    if (appLink) {
      // Surface destinations resolve through the shared app-link registry so
      // context-menu activation focuses the same view as the lesson action,
      // preserving chat identity and unsaved drafts.
      openAppDestination(appLink, null);
    } else if (href && href !== '#') {
      window.location.assign(href);
    }
    return true;
  }

  async function render(body) {
    ui.body = body;
    body.replaceChildren(h('div', { class: 'copal-empty', text: 'Loading TreeHouse domain…' }));
    try { if (await load()) renderLoaded(); } catch (error) { if (ui.body === body) renderFailure(error); }
  }

  return { render, command, handleContextCommand, loadState, suspendScope, attachLessonResource, exportCoursePackageToFiles, get snapshot() { return ui.snapshot; } };
}
