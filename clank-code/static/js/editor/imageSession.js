/** Image session identity and lifecycle foundation. No pixel runtime lives here. */
import { normalizeImageRevision, normalizeResourceKey, resourceKeyId } from '../copal/resourceModel.js';

const VERSION = 1;
const UNAVAILABLE = Object.freeze({ status:'unavailable', reason:'image_runtime_pending' });

/** Capture one save per distinct dirty session, independently of later focus. */
export async function saveDistinctImageSessions(sessions) {
  const targets = [...new Set(sessions)].filter(session => session?.isDirty?.());
  const results = [];
  for (const session of targets) {
    try {
      const value = await session.save();
      results.push({status:'fulfilled',value});
      if (value?.status === 'cancelled') break;
    } catch (reason) {
      results.push({status:'rejected',reason});
    }
  }
  return results;
}

function text(value, field, { optional = false, max = 16384 } = {}) {
  if (value == null && optional) return null;
  if (typeof value !== 'string' || !value.trim() || value !== value.trim() || value.length > max || /[\u0000-\u001f\u007f]/.test(value) || /^data:/i.test(value)) {
    throw new TypeError(`${field} is invalid`);
  }
  return value;
}
function uuid() {
  if (typeof globalThis.crypto?.randomUUID !== 'function') throw new Error('secure image session identity is unavailable');
  return globalThis.crypto.randomUUID();
}
function revision(value) { return value == null ? null : normalizeImageRevision(value); }
function generation(value) {
  if (value == null) return null;
  if (!Number.isSafeInteger(value) || value < 0) throw new TypeError('policyGeneration is invalid');
  return value;
}
function projectRevision(value) {
  if (value == null) return null;
  if (Number.isSafeInteger(value) && value >= 0) return value;
  return text(value, 'projectRevision', {max:256});
}
function sourceDigest(value) {
  if (value == null) return null;
  const digest = text(value, 'sourceDigest', {max:71});
  if (!/^(?:sha256:)?[a-f0-9]{64}$/.test(digest)) throw new TypeError('sourceDigest is invalid');
  return digest;
}

/** Only bounded identity/recovery metadata is retained; arbitrary scene payloads are ignored. */
export function normalizeImageRecovery(value = {}) {
  if (!value || typeof value !== 'object' || value.version !== VERSION) throw new TypeError('unsupported image recovery version');
  const scope = Object.freeze({ accountId:text(value.scope?.accountId, 'accountId', {max:256}), workspaceId:text(value.scope?.workspaceId, 'workspaceId', {max:256}) });
  const key = value.sourceKey == null ? null : normalizeResourceKey(value.sourceKey);
  if (key && (key.accountId !== scope.accountId || key.workspaceId !== scope.workspaceId)) throw new TypeError('image source scope does not match session');
  return Object.freeze({
    version:VERSION, sessionId:text(value.sessionId, 'sessionId', {max:256}), scope, sourceKey:key,
    sourceRevision:revision(value.sourceRevision), opaqueRef:text(value.opaqueRef, 'opaqueRef', {optional:true}),
    sourceDigest:sourceDigest(value.sourceDigest), policyGeneration:generation(value.policyGeneration),
    projectId:text(value.projectId, 'projectId', {optional:true,max:256}), projectRevision:projectRevision(value.projectRevision),
    draftId:text(value.draftId, 'draftId', {optional:true,max:256}),
  });
}

function hasBlankIdentity(target) {
  return target.sourceKey === null && target.sourceRevision === null && target.sourceDigest === null &&
    target.opaqueRef === null && target.projectId === null && target.projectRevision === null && target.draftId === null;
}

function hasPendingCloseRecovery(controller) {
  if (!controller || controller.closeAccepted) return false;
  const state=controller.recoveryStatus?.()?.state;
  return ['in_flight','copy_pending','draft_uncertain','save_uncertain'].includes(state) ||
    Boolean(controller.pendingDraftAttempt || controller.pendingDraftReceipt || controller.pendingSaveAttempt || controller.pendingSaveReceipt);
}

function sessionFrom(recovery, onDispose, onBind, newlyOpened) {
  let saved = normalizeImageRecovery(recovery);
  let disposed = false;
  const runtimes = new Map();
  const selectRuntime = viewId => viewId != null ? runtimes.get(viewId) || null : runtimes.size === 1 ? runtimes.values().next().value : null;
  let sceneAuthority = null;
  let persistenceController = null;
  let neverInitialized = newlyOpened && hasBlankIdentity(saved);
  const uninitializedCloseTokens = new WeakSet();
  const canCloseUninitializedBlank = () => !disposed && neverInitialized && hasBlankIdentity(saved) &&
    runtimes.size === 0 && sceneAuthority === null && persistenceController === null;
  const views = new Map();
  const requireLive = () => { if (disposed) throw new Error('image session is disposed'); };
  const snapshot = () => normalizeImageRecovery(saved);
  const refresh = (expected, receipt) => {
    requireLive(); if (JSON.stringify(snapshot()) !== JSON.stringify(expected)) throw new Error('image target changed');
    const key = receipt?.key == null ? null : normalizeResourceKey(receipt.key);
    if ((saved.sourceKey == null) !== (key == null) || (key && resourceKeyId(key) !== resourceKeyId(saved.sourceKey))) throw new Error('image receipt identity mismatch');
    const next=normalizeImageRecovery({...saved,opaqueRef:receipt.opaqueRef ?? saved.opaqueRef,sourceRevision:receipt.sourceRevision ?? saved.sourceRevision,sourceDigest:receipt.sourceDigest ?? saved.sourceDigest,projectId:receipt.projectId ?? saved.projectId,projectRevision:receipt.projectRevision ?? saved.projectRevision,policyGeneration:receipt.policyGeneration ?? saved.policyGeneration,draftId:receipt.draftId ?? saved.draftId});
    if (saved.projectId && next.projectId !== saved.projectId) throw new Error('image project identity mismatch');
    if (saved.projectRevision != null && next.projectRevision != null && Number(next.projectRevision) < Number(saved.projectRevision)) throw new Error('image project revision regressed');
    saved=next;
    if (!hasBlankIdentity(saved)) neverInitialized=false;
    return snapshot();
  };
  return Object.freeze({
    get sessionId() { return saved.sessionId; },
    captureTarget() { requireLive(); return snapshot(); },
    hasRuntime() { requireLive(); return runtimes.size > 0; },
    hasOpenRuntime() { requireLive(); return [...runtimes.values()].some(runtime => runtime?.isEditorOpen?.() === true); },
    hasReadyRuntime() { requireLive(); return [...runtimes.values()].some(runtime => runtime?.isEditorOpen?.() === true && runtime?.isSceneReady?.() === true); },
    getSceneAuthority() { requireLive(); return sceneAuthority; },
    canCloseUninitializedBlank,
    markInitialized() { requireLive(); neverInitialized=false; },
    getPersistenceController() { requireLive(); return persistenceController; },
    recoveryStatus() { requireLive(); return persistenceController?.recoveryStatus?.() || Object.freeze({state:'unavailable',canRetryCopy:false}); },
    attachPersistenceController(controller) {
      requireLive(); if (persistenceController || typeof controller?.dispose !== 'function') throw new Error('image persistence is already attached or invalid');
      persistenceController=controller;
      return Object.freeze({
        refresh(expected,receipt) { if (persistenceController !== controller) throw new Error('image persistence owner changed'); return refresh(expected,receipt); },
        detach() {
          if (persistenceController !== controller) return false;
          const pending=hasPendingCloseRecovery(controller);
          if (neverInitialized && (pending || controller.recoveryStatus?.()?.state !== 'ready' || controller.dirty !== false ||
              controller.imageSourceProvenance || controller.retainedResultStatus?.()?.records?.length !== 0)) neverInitialized=false;
          if (pending) return false;
          if (!controller.dispose()) return false;
          persistenceController=null;
          return true;
        },
      });
    },
    attachSceneAuthority(scene) {
      requireLive();
      if (sceneAuthority || typeof scene?.subscribeView !== 'function' || typeof scene?.dispose !== 'function') throw new Error('image scene is already attached or invalid');
      sceneAuthority = scene;
      return () => {
        if (sceneAuthority !== scene) return false;
        const pending=hasPendingCloseRecovery(persistenceController);
        if (neverInitialized && (pending || scene.revision !== 0 || scene.dirty !== false || scene.state?.layers?.length !== 0 ||
            scene.state?.undoStack?.length !== 0 || scene.state?.redoStack?.length !== 0)) neverInitialized=false;
        if (pending) return false;
        if (!scene.dispose()) return false;
        sceneAuthority=null;
        return true;
      };
    },
    capabilities(viewId) { return selectRuntime(viewId)?.capabilities() || Object.freeze({ runtime:false, save:false, saveCopy:false, undo:false, redo:false, reason:runtimes.size > 1 && viewId == null ? 'image_view_required' : 'image_runtime_pending' }); },
    isDirty() { requireLive(); return sceneAuthority?.dirty ?? selectRuntime()?.isDirty?.() ?? null; },
    async save(viewId) { requireLive(); if (!saved.sourceKey && !persistenceController?.canSaveHostOriginal && !persistenceController?.canSaveRecoveredCopy && !persistenceController?.needsHostSaveDestination) return {status:'unavailable',reason:'This image has no original. Use Save as copy.'}; if (viewId != null && !runtimes.has(viewId)) return UNAVAILABLE; if (persistenceController?.saveCurrent) return persistenceController.saveCurrent(); const runtime=selectRuntime(viewId); return runtime?.save ? runtime.save() : UNAVAILABLE; },
    async saveCopy(viewId) { requireLive(); if (viewId != null && !runtimes.has(viewId)) return UNAVAILABLE; if (persistenceController?.saveCopyCurrent) return persistenceController.saveCopyCurrent(); const runtime=selectRuntime(viewId); return runtime?.exportToGallery ? runtime.exportToGallery() : UNAVAILABLE; },
    async retrySaveCopy() { requireLive(); return persistenceController?.retryCopy ? persistenceController.retryCopy() : UNAVAILABLE; },
    undo(viewId) { requireLive(); const runtime=selectRuntime(viewId); return runtime?.undo ? runtime.undo() : UNAVAILABLE; },
    redo(viewId) { requireLive(); const runtime=selectRuntime(viewId); return runtime?.redo ? runtime.redo() : UNAVAILABLE; },
    async beforeClose(choice,viewId) { requireLive(); const runtime=selectRuntime(viewId); if (runtime?.beforeClose) return runtime.beforeClose(choice); if (runtimes.size) return UNAVAILABLE; const token=await persistenceController?.prepareClose?.(choice); return token?.status==='prepared' && persistenceController.commitToken(token) ? {status:'closed'} : token || UNAVAILABLE; },
    async prepareClose(choice,viewId) {
      requireLive();
      if (choice === 'close_uninitialized') {
        if (!canCloseUninitializedBlank()) return UNAVAILABLE;
        const token=Object.freeze({status:'prepared'});
        uninitializedCloseTokens.add(token);
        return token;
      }
      const runtime=selectRuntime(viewId);
      return runtime?.prepareClose ? runtime.prepareClose(choice) : runtimes.size ? UNAVAILABLE : persistenceController?.prepareClose?.(choice) || UNAVAILABLE;
    },
    validateClose(token,viewId) {
      requireLive();
      if (uninitializedCloseTokens.has(token)) return canCloseUninitializedBlank();
      const runtime=selectRuntime(viewId);
      return runtime?.validateClose ? runtime.validateClose(token) === true : runtimes.size ? false : persistenceController?.validateToken?.(token) === true;
    },
    commitClose(token,viewId) {
      requireLive();
      if (uninitializedCloseTokens.has(token)) return canCloseUninitializedBlank() && uninitializedCloseTokens.delete(token);
      const runtime=selectRuntime(viewId);
      return runtime?.commitClose ? runtime.commitClose(token) === true : runtimes.size ? false : persistenceController?.commitToken?.(token) === true;
    },
    async restoreDraft(viewId) { requireLive(); const runtime=selectRuntime(viewId); return runtime?.restoreDraft ? runtime.restoreDraft() : UNAVAILABLE; },
    serializeRecovery() { requireLive(); return snapshot(); },
    mountView({ viewId = uuid(), zoom = 1 } = {}) {
      requireLive(); const id=text(viewId, 'viewId', {max:256});
      if (views.has(id)) throw new Error('image view already mounted');
      if (!Number.isFinite(zoom) || zoom <= 0) throw new TypeError('zoom is invalid');
      views.set(id, { viewId:id, zoom, visible:false });
      return Object.freeze({ ...UNAVAILABLE, viewId:id });
    },
    setVisible(viewId, visible) { requireLive(); const view=views.get(viewId); if (!view) return false; view.visible=visible === true; return true; },
    setViewZoom(viewId, zoom) { requireLive(); const view=views.get(viewId); if (!view) return false;
      if (!Number.isFinite(zoom) || zoom <= 0) throw new TypeError('zoom is invalid'); view.zoom=zoom; return true; },
    releaseView(viewId) { requireLive(); return views.delete(viewId); },
    viewState(viewId) { requireLive(); const view=views.get(viewId); return view ? Object.freeze({ ...view }) : null; },
    attachRuntime(candidate, {viewId = 'default'} = {}) {
      requireLive(); const id=text(viewId, 'viewId', {max:256});
      if (runtimes.has(id) || [...runtimes.values()].includes(candidate) || !candidate || typeof candidate.capabilities !== 'function') throw new Error('image view runtime is already attached or invalid');
      runtimes.set(id,candidate);
      return Object.freeze({
        viewId:id,
        refresh(expected, receipt) { requireLive(); if (runtimes.get(id) !== candidate) throw new Error('image runtime owner changed'); return refresh(expected,receipt); },
        detach() { if (runtimes.get(id) !== candidate) return false; runtimes.delete(id); return true; },
      });
    },
    bindManagedReceipt(receipt) {
      requireLive();
      if (saved.sourceKey) throw new Error('image session already has a durable source');
      text(receipt?.receiptId, 'receiptId', {max:256});
      const next=normalizeImageRecovery({ ...saved, sourceKey:receipt.key, sourceRevision:receipt.revision ?? null,
        opaqueRef:receipt.opaqueRef ?? null, sourceDigest:receipt.sourceDigest ?? null,
        policyGeneration:receipt.policyGeneration ?? null });
      if (!next.sourceKey) throw new TypeError('receipt source key is required');
      onBind(next.sourceKey);
      saved=next;
      neverInitialized=false;
      return snapshot();
    },
    dispose() { if (disposed || hasPendingCloseRecovery(persistenceController) || sceneAuthority?.canDispose?.() === false || persistenceController?.canDispose?.() === false) return false; disposed=true; try { for (const runtime of [...runtimes.values()]) runtime?.dispose?.(); } finally { runtimes.clear(); persistenceController?.dispose(); persistenceController=null; sceneAuthority?.dispose(); sceneAuthority=null; views.clear(); onDispose(); } return true; },
  });
}

/** Reuse one exact blank recovery admission after concurrent receipt reads. */
export function admitRecoveredBlank(imageMap, registry, recovery, scope) {
  if (!(imageMap instanceof Map) || typeof scope !== 'string' || !scope) throw new TypeError('blank image recovery context is unavailable');
  const expected=normalizeImageRecovery(recovery), id=`image-session:${expected.sessionId}`;
  if (expected.sourceKey!==null || expected.sourceRevision!==null || expected.sourceDigest!==null || expected.opaqueRef!==null || expected.projectId!==null || expected.projectRevision!==null || !expected.draftId) throw new Error('blank image recovery identity is invalid');
  const existing=imageMap.get(id);
  if (existing) { if (!existing.blank || existing.scope!==scope || JSON.stringify(existing.session.captureTarget())!==JSON.stringify(expected)) throw new Error('Blank image recovery conflicts with an existing session.'); return existing; }
  const session=registry.restore(expected), entry={session,scope,views:new Map(),initialLoad:null,blank:true,presetSize:{w:1,h:1},restoreDraft:true};
  imageMap.set(id,entry); return entry;
}

const registryInternals = new WeakMap(), sessionRegistryLinks = new WeakMap();

/** One session per scoped durable source; blank sessions remain distinct until a receipt binds them. */
export function createImageSessionRegistry() {
  const sessions = new Map(), sources = new Map();
  const internal = {
    remove(session) { if (sessions.get(session.sessionId) === session) sessions.delete(session.sessionId); for (const [id,owner] of sources) if (owner===session) sources.delete(id); },
    bind(session,sourceKey) { const id=resourceKeyId(sourceKey); if (sources.has(id) && sources.get(id)!==session) throw new Error('image source is already open'); sources.set(id,session); },
  };
  function create(value, newlyOpened = false) {
    const recovery = normalizeImageRecovery(value);
    const byId = sessions.get(recovery.sessionId);
    if (byId) {
      if (JSON.stringify(byId.captureTarget()) !== JSON.stringify(recovery)) throw new Error('image session identity conflicts with retained state');
      return byId;
    }
    const key = recovery.sourceKey && resourceKeyId(recovery.sourceKey);
    if (key && sources.has(key)) {
      const existing = sources.get(key), current = existing.captureTarget();
      if (JSON.stringify(current.sourceRevision) !== JSON.stringify(recovery.sourceRevision)) throw new Error('image source revision changed');
      for (const field of ['sourceDigest', 'projectId', 'projectRevision', 'draftId']) {
        if (recovery[field] != null && current[field] !== recovery[field]) throw new Error('image session recovery metadata conflicts');
      }
      return existing;
    }
    let session; const link={owner:internal};
    session = sessionFrom(recovery, () => link.owner.remove(session), sourceKey => link.owner.bind(session,sourceKey), newlyOpened);
    sessionRegistryLinks.set(session,link);
    sessions.set(recovery.sessionId, session);
    if (key) sources.set(key, session);
    return session;
  }
  const api = Object.freeze({
    open({ scope, sourceKey = null, sourceRevision = null, opaqueRef = null, sourceDigest = null,
           policyGeneration = null, projectId = null, projectRevision = null, draftId = null } = {}) {
      return create({ version:VERSION, sessionId:uuid(), scope, sourceKey, sourceRevision, opaqueRef,
        sourceDigest, policyGeneration, projectId, projectRevision, draftId }, true);
    },
    restore(recovery) { return create(recovery); },
    transferFrom(source,session,commit) {
      const old=registryInternals.get(source), link=sessionRegistryLinks.get(session), target=session?.captureTarget?.();
      const key=target?.sourceKey && resourceKeyId(target.sourceKey);
      if (!old || old===internal || !link || link.owner!==old || old.sessions.get(target.sessionId)!==session || typeof commit!=='function' || sessions.has(target.sessionId) || (key && sources.has(key))) throw new Error('image registry transfer conflicts');
      commit(); old.remove(session); sessions.set(target.sessionId,session); if (key) sources.set(key,session); link.owner=internal; return session;
    },
    get(sessionId) { return sessions.get(sessionId) || null; },
    all() { return [...sessions.values()]; },
    async saveAll() { return Promise.all([...sessions.values()].map(async session => ({ sessionId:session.sessionId, result:await session.save() }))); },
  });
  internal.sessions=sessions; registryInternals.set(api,internal); return api;
}
