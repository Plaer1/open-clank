import { normalizeResourceHandle, resourceKeyId, snapshotEnvelope, cloneEnvelope, normalizeRevision } from './resourceModel.js';

function copy(value) { return cloneEnvelope(value); }

// DTO object field order is not a document edit. Arrays retain their order.
function valueEquivalent(left, right) {
  if (Object.is(left, right)) return true;
  if (!left || !right || typeof left !== 'object' || typeof right !== 'object') return false;
  if (Array.isArray(left) || Array.isArray(right)) return Array.isArray(left) && Array.isArray(right) && left.length === right.length && left.every((value, index) => valueEquivalent(value, right[index]));
  const a = Object.keys(left).filter(key => left[key] !== undefined).sort();
  const b = Object.keys(right).filter(key => right[key] !== undefined).sort();
  return a.length === b.length && a.every((key, index) => key === b[index] && valueEquivalent(left[key], right[key]));
}

function equivalent(left, right) {
  if (String(left?.text ?? '') !== String(right?.text ?? '')) return false;
  const fields = new Set([...Object.keys(left || {}), ...Object.keys(right || {})]);
  fields.delete('text');
  return [...fields].every(key => valueEquivalent(left?.[key], right?.[key]));
}

// Source inverses store only the edited span. Native envelope fields share the
// same timeline, without cloning the whole source into every undo record.
function envelopeDelta(before, after) {
  const a = String(before?.text ?? ''), b = String(after?.text ?? '');
  let from = 0, tail = 0;
  while (from < a.length && from < b.length && a[from] === b[from]) from++;
  while (tail < a.length - from && tail < b.length - from && a[a.length - tail - 1] === b[b.length - tail - 1]) tail++;
  const fields = [];
  for (const key of new Set([...Object.keys(before || {}), ...Object.keys(after || {})])) {
    if (key !== 'text' && !valueEquivalent(before?.[key], after?.[key])) fields.push({ key, before:copy(before?.[key]), after:copy(after?.[key]), hadBefore:Object.hasOwn(before || {}, key), hadAfter:Object.hasOwn(after || {}, key) });
  }
  return { from, before:a.slice(from, a.length - tail), after:b.slice(from, b.length - tail), fields };
}

function applyDelta(envelope, delta, reverse) {
  const remove = reverse ? delta.after : delta.before, insert = reverse ? delta.before : delta.after;
  const text = String(envelope.text ?? '');
  if (text.slice(delta.from, delta.from + remove.length) !== remove) throw new Error('Document history no longer matches its source');
  const result = { ...envelope, text:text.slice(0, delta.from) + insert + text.slice(delta.from + remove.length) };
  for (const field of delta.fields) {
    if (reverse ? field.hadBefore : field.hadAfter) result[field.key] = copy(reverse ? field.before : field.after);
    else delete result[field.key];
  }
  return result;
}

function actionId(localRevision) {
  const suffix = globalThis.crypto?.randomUUID?.() || `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
  return `save-${Math.max(0, Number(localRevision) || 0)}-${suffix}`;
}

class ResourceBuffer {
  constructor(handle, envelope, { save = null, actorId = '', epoch = '', scope = null, historyLimit = 2000, historyLimitBytes = 32 * 1024 * 1024 } = {}) {
    this.handle = normalizeResourceHandle(handle);
    this.key = this.handle.key;
    this.id = resourceKeyId(this.key);
    this.actorId = String(actorId || this.key.accountId);
    this.epoch = String(epoch);
    const initialScope = scope && typeof scope === 'object' ? scope : {};
    this.scope = Object.freeze({
      ...initialScope,
      accountId:String(initialScope.accountId ?? this.key.accountId),
      workspace:String(initialScope.workspace ?? initialScope.workspaceId ?? this.key.workspaceId),
      workspaceId:String(initialScope.workspaceId ?? this.key.workspaceId),
      actorId:String(initialScope.actorId || this.actorId),
      epoch:String(initialScope.epoch ?? this.epoch),
    });
    this.envelope = copy(envelope);
    this.acceptedEnvelope = copy(envelope);
    this.localRevision = 0;
    this.savedLocalRevision = 0;
    this.status = 'saved';
    this.error = null;
    this.recoveryError = null;
    this.conflict = null;
    this.projections = null;
    this.actionIds = new Map();
    this.save = typeof save === 'function' ? save : null;
    this.pending = null;
    this.pendingSheetRevision = null;
    this.running = null;
    this.drainPromise = null;
    this.invalidated = false;
    this.listeners = new Set();
    this.views = new Map();
    this.history = [];
    this.redoHistory = [];
    this.historyLimit = Math.max(1000, Math.round(Number(historyLimit) || 2000));
    this.historyBytes = 0;
    this.redoBytes = 0;
    this.historyLimitBytes = Math.max(64 * 1024, Math.round(Number(historyLimitBytes) || 32 * 1024 * 1024));
    this.historyNotice = ''; this.lastHistoryTime = 0; this.saveRuns = new Map();
    this.pendingEdits = new Map(); this.pendingEditRevision = 0;
    this.recovered = false; this.uncertainSave = null; this.runningTarget = null;
  }

  subscribe(listener) { if (typeof listener === 'function') this.listeners.add(listener); return () => this.listeners.delete(listener); }
  _emit() { const state = this.state(); for (const listener of this.listeners) listener(state); }
  state() { return { key:this.key, localRevision:this.localRevision, savedLocalRevision:this.savedLocalRevision, dirty:Boolean(this.uncertainSave) || this.pendingEdits.size > 0 || this.recovered || !equivalent(this.envelope, this.acceptedEnvelope) || Boolean(this.running && this.runningTarget && !equivalent(this.envelope, this.runningTarget.envelope)), status:this.status, error:this.error, recoveryError:this.recoveryError, conflict:this.conflict, projections:this.projections, canUndo:this.history.length > 0, canRedo:this.redoHistory.length > 0, historyNotice:this.historyNotice, undoDepth:this.history.length, uncertainSave:Boolean(this.uncertainSave), pendingEdits:this.pendingEdits.size }; }
  _actionId(localRevision = this.localRevision) {
    if (!this.actionIds.has(localRevision)) this.actionIds.set(localRevision, actionId(localRevision));
    return this.actionIds.get(localRevision);
  }
  acknowledge(localRevision, revision, envelope = null) {
    const next = normalizeRevision(revision);
    if (this.invalidated || !Number.isSafeInteger(localRevision) || localRevision < this.savedLocalRevision || localRevision > this.localRevision) return false;
    this.handle = { ...this.handle, revision:next };
    this.savedLocalRevision = localRevision;
    if (envelope) this.acceptedEnvelope = copy(envelope);
    else if (localRevision === this.localRevision) this.acceptedEnvelope = copy(this.envelope);
    this.recovered = false;
    for (const key of this.actionIds.keys()) if (key <= localRevision) this.actionIds.delete(key);
    this.pending = this.state().dirty ? this.snapshot() : null;
    // A newer local edit may have captured a pending envelope while the
    // acknowledged request was in flight. Rebuild that snapshot after the
    // handle advances so its next CAS uses the committed head, while keeping
    // the newer envelope and immutable local revision intact.
    if (this.pending && this.pending.expectedRevision.value !== next.value) this.pending = this.snapshot();
    if (!this.pending) this.pendingSheetRevision = null;
    this.status = this.pending ? 'unsaved' : 'saved';
    this.error = null; this.conflict = null;
    this._emit(); return true;
  }
  retryAtRevision(localRevision, revision) {
    if (this.invalidated || this.running || this.drainPromise || localRevision !== this.localRevision) return false;
    this.handle = { ...this.handle, revision:normalizeRevision(revision) };
    // Explicitly rebasing after a conflict is a new operation. An uncertain
    // replay still uses the existing pending snapshot/action ID via flush().
    this.actionIds.set(localRevision, actionId(localRevision));
    this.conflict = null; this.error = null; this.status = 'unsaved'; this.pendingSheetRevision = null;
    this.pending = this.snapshot(); this._emit(); return true;
  }
  acquireView(viewId, initial = {}) {
    const id = String(viewId || `view-${this.views.size + 1}`);
    if (!this.views.has(id)) this.views.set(id, { selection:null, scrollTop:0, ...copy(initial) });
    return copy(this.views.get(id));
  }
  updateView(viewId, patch = {}) { const id = String(viewId); const current = this.views.get(id) || {}; this.views.set(id, { ...current, ...copy(patch) }); return copy(this.views.get(id)); }
  releaseView(viewId) { this.views.delete(String(viewId)); }
  apply(transaction, { origin = 'local', history = true, sheet = false } = {}) {
    const transactionInput = typeof transaction === 'function' ? copy(this.envelope) : null;
    const next = typeof transaction === 'function' ? transaction(transactionInput) : transaction;
    if (next === undefined) throw new TypeError('transaction must return an envelope');
    if (equivalent(this.envelope, next)) return this.snapshot();
    const delta = history ? envelopeDelta(this.envelope, next) : null;
    this.envelope = next === transactionInput ? next : copy(next);
    this.localRevision += 1;
    this.pendingSheetRevision = sheet === true ? this.localRevision : null;
    if (delta) {
      const now = Date.now(), bytes = JSON.stringify(delta).length * 2;
      const previous = this.history.at(-1);
      if (origin === 'typing' && previous?.origin === origin && now - this.lastHistoryTime < 500 && !delta.fields.length) {
        previous.deltas.push(delta); previous.bytes += bytes;
      } else this.history.push({ localRevision:this.localRevision, deltas:[delta], origin, bytes });
      this.lastHistoryTime = now; this.historyBytes += bytes;
      while (this.history.length > this.historyLimit || this.historyBytes > this.historyLimitBytes) {
        const removed = this.history.shift(); this.historyBytes -= removed?.bytes || 0;
        this.historyNotice = 'Older undo groups were released at the document history memory limit.';
      }
    }
    if (origin !== 'history') { this.redoHistory = []; this.redoBytes = 0; }
    if (origin === 'remote') { this.savedLocalRevision = this.localRevision; this.acceptedEnvelope = copy(this.envelope); }
    else { this.status = 'unsaved'; this.error = null; this.conflict = null; this.projections = null; }
    this.pending = this.state().dirty ? this.snapshot() : null;
    if (!this.pending) this.status = 'saved';
    this._emit();
    return this.snapshot();
  }
  undo() {
    const entry = this.history.pop();
    if (!entry) return null;
    this.redoHistory.push(entry);
    this.historyBytes -= entry.bytes || 0; this.redoBytes += entry.bytes || 0;
    for (const delta of [...entry.deltas].reverse()) this.envelope = applyDelta(this.envelope, delta, true);
    this.pendingSheetRevision = null;
    this.localRevision += 1; this.status = 'unsaved'; this.error = null; this.conflict = null;
    this.lastHistoryTime = 0; this.pending = this.state().dirty ? this.snapshot() : null; this.status = this.pending ? 'unsaved' : 'saved'; this._emit(); return this.snapshot();
  }
  redo() {
    const entry = this.redoHistory.pop();
    if (!entry) return null;
    this.history.push(entry);
    this.historyBytes += entry.bytes || 0; this.redoBytes -= entry.bytes || 0;
    for (const delta of entry.deltas) this.envelope = applyDelta(this.envelope, delta, false);
    this.pendingSheetRevision = null;
    this.localRevision += 1; this.status = 'unsaved'; this.error = null; this.conflict = null;
    this.lastHistoryTime = 0; this.pending = this.state().dirty ? this.snapshot() : null; this.status = this.pending ? 'unsaved' : 'saved'; this._emit(); return this.snapshot();
  }
  snapshot() {
    return { ...snapshotEnvelope({ key:this.key, expectedRevision:this.handle.revision, localRevision:this.localRevision, actionId:this.localRevision ? this._actionId() : null, scope:this.scope, envelope:this.envelope }), uncertainSave:this.uncertainSave, pendingEdits:[...this.pendingEdits.values()].map(edit => ({ id:String(edit.id), body:String(edit.body ?? ''), expectedSource:String(edit.expectedSource ?? ''), expectedLocalRevision:Number(edit.expectedLocalRevision || 0) })) };
  }
  async _flushOne(submitted, options = {}) {
    if (this.status === 'conflict') return { outcome:'conflict', conflict:this.conflict };
    if (!this.save) throw new Error('resource buffer has no save adapter');
    if (this.uncertainSave && submitted.actionId !== this.uncertainSave.actionId) { this.error = new Error('The previous Save outcome is unknown. Retry that captured Save before saving newer edits.'); this.status = 'error'; this._emit(); return false; }
    this.runningTarget = submitted;
    this.status = 'saving'; this.error = null; this._emit();
    this.running = (async () => {
      try {
        if (this.invalidated) { this.status = 'cancelled'; return false; }
        const result = await this.save(submitted, { ...options, key:this.key, sheet:submitted.localRevision === this.pendingSheetRevision });
        if (this.invalidated) { this.status = 'cancelled'; return false; }
        const outcome = result?.outcome;
        if (outcome === 'conflict' || result?.status === 409) {
          this.status = 'conflict';
          this.conflict = { local:copy(submitted), remote:copy(result.remote || result.current || result.snapshot || null), result };
          return result;
        }
        if (outcome !== 'applied' || !result?.revision) {
          this.status = 'error'; this.error = Object.assign(new Error(result?.message || 'resource save did not return an applied receipt'), { result });
          this.pending = submitted;
          if (result?.uncertain === true) this.uncertainSave = submitted;
          return false;
        }
        this.uncertainSave = null;
        this.projections = result.projections || null;
        this.acknowledge(submitted.localRevision, result.revision, result.snapshot?.envelope || submitted.envelope);
        return result;
      } catch (error) {
        this.status = 'error'; this.error = error; this.pending = submitted;
        if (error?.retryable !== false) this.uncertainSave = submitted;
        return false;
      } finally {
        this.running = null; this.runningTarget = null; this._emit();
      }
    })();
    return this.running;
  }
  flush(options = {}) {
    // A Save gesture is an undo boundary, even if typing continues before its
    // receipt arrives. Undo can then return exactly to the captured save point.
    this.lastHistoryTime = 0;
    const target = options.snapshot || this.snapshot();
    if (this.saveRuns.has(target.localRevision)) return this.saveRuns.get(target.localRevision);
    if (!this.state().dirty && !this.running) return Promise.resolve(true);
    const previous = this.drainPromise;
    const run = (async () => {
      if (previous) { const result = await previous; if (result === false || result?.outcome === 'conflict') return result; }
      if (this.invalidated) return false;
      // Earlier saves of this resource may advance CAS, never the captured
      // envelope/action. An uncertain failure above cannot silently rebase.
      const submitted = previous ? snapshotEnvelope({ ...target, expectedRevision:this.handle.revision }) : target;
      return this._flushOne(submitted, options);
    })();
    this.saveRuns.set(target.localRevision, run); this.drainPromise = run;
    run.finally(() => { this.saveRuns.delete(target.localRevision); if (this.drainPromise === run) this.drainPromise = null; });
    return run;
  }
  discard() {
    if (this.running || this.drainPromise || this.uncertainSave) return false;
    for (const edit of this.pendingEdits.values()) { try { edit.discard?.(); } catch (_) {} }
    this.pendingEdits.clear(); this.pendingEditRevision += 1;
    this.envelope = copy(this.acceptedEnvelope); this.localRevision += 1; this.savedLocalRevision = this.localRevision;
    this.pending = null; this.pendingSheetRevision = null; this.recovered = false;
    this.history = []; this.redoHistory = []; this.historyBytes = 0; this.redoBytes = 0; this.lastHistoryTime = 0; this.historyNotice = '';
    this.error = null; this.conflict = null; this.status = 'saved'; this._emit(); return true;
  }
  resolveExternal(envelope, revision, { force = false } = {}) {
    if (this.state().dirty && !force) {
      this.status = 'conflict'; this.conflict = { local:this.snapshot(), remote:{ envelope:copy(envelope), revision:normalizeRevision(revision) } }; this._emit(); return false;
    }
    if (!equivalent(this.envelope, envelope)) this.apply(envelope, { origin:'remote', history:true });
    this.envelope = copy(envelope); this.acceptedEnvelope = copy(envelope); this.recovered = false; this.handle = { ...this.handle, revision:normalizeRevision(revision) };
    this.localRevision += 1; this.savedLocalRevision = this.localRevision; this.pending = null; this.pendingSheetRevision = null; this.status = 'saved'; this.error = null; this.conflict = null; this._emit(); return true;
  }
  recover(snapshot) {
    const restored = snapshotEnvelope(snapshot);
    if (resourceKeyId(restored.key) !== this.id) throw new Error('draft belongs to another resource');
    if (this.invalidated || this.running || this.drainPromise) throw new Error('draft recovery requires a current idle buffer');
    // Durable storage is actor/resource keyed, not lifetime-epoch keyed.
    // Validate every recorded stable owner field before changing live state;
    // legacy snapshots without scope still require the exact resource key and
    // the registry's actor-partitioned recovery lookup.
    for (const field of ['accountId', 'workspace', 'workspaceId', 'storageNamespace', 'actorId']) {
      if (restored.scope?.[field] != null && String(restored.scope[field]) !== String(this.scope[field] ?? '')) {
        throw Object.assign(new Error(`draft belongs to another ${field} scope`), { code:'recovery_scope', retryable:false });
      }
    }
    this.envelope = copy(restored.envelope); this.localRevision = restored.localRevision; this.savedLocalRevision = Math.max(0, restored.localRevision - 1);
    this.recovered = true;
    if (snapshot.uncertainSave) {
      const uncertain = snapshotEnvelope(snapshot.uncertainSave);
      if (resourceKeyId(uncertain.key) !== this.id || uncertain.localRevision > restored.localRevision) throw new Error('Uncertain save belongs to another resource revision');
      for (const field of ['accountId', 'workspace', 'workspaceId', 'storageNamespace', 'actorId']) if (uncertain.scope?.[field] != null && String(uncertain.scope[field]) !== String(this.scope[field] ?? '')) throw new Error('Uncertain save belongs to another owner');
      this.uncertainSave = uncertain;
    }
    this.pendingEdits = new Map((Array.isArray(snapshot.pendingEdits) ? snapshot.pendingEdits : []).filter(edit => edit && typeof edit.id === 'string' && typeof edit.body === 'string').map(edit => [edit.id, { id:edit.id, body:edit.body, expectedSource:String(edit.expectedSource ?? ''), expectedLocalRevision:Number(edit.expectedLocalRevision || 0) }]));
    this.actionIds.clear();
    if (restored.actionId) this.actionIds.set(restored.localRevision, restored.actionId);
    // Retain the original CAS/action for uncertain replay, while a new explicit
    // flush uses the verified current lifetime and its existing save guards.
    // Recovery itself never writes or acknowledges the recovered source.
    this.handle = { ...this.handle, revision:restored.expectedRevision }; this.pendingSheetRevision = null; this.status = 'unsaved'; this.pending = this.snapshot(); this._emit(); return this.snapshot();
  }
}

export function createBufferRegistry({ storage = globalThis.localStorage, namespace = 'copal-buffer-draft:v1' } = {}) {
  const buffers = new Map();
  // Failed persistent writes retain the newest snapshot under its immutable
  // actor/resource key. This survives scope teardown, not a process restart.
  const memoryDrafts = new Map();
  const recoveryTimers = new Map();
  let activeScope = '';
  const scopeFor = (options, key) => `${String(options.actorId || key.accountId)}:${String(options.epoch || '')}`;
  const mapKey = (key, scope) => `${scope}:${resourceKeyId(key)}`;
  // Epochs invalidate live work; durable drafts must remain recoverable after
  // restart or a display-name change, so they are not part of the key.
  const draftKey = (buffer) => `${namespace}:${encodeURIComponent(buffer.actorId)}:${encodeURIComponent(buffer.id)}`;
  return {
    acquire(handle, envelope, options = {}) {
      const scope = scopeFor(options, handle.key);
      const id = mapKey(handle.key, scope);
      let buffer = buffers.get(id);
      if (!buffer) { buffer = new ResourceBuffer(handle, envelope, options); buffers.set(id, buffer); }
      else if (typeof options.save === 'function' && !buffer.running) buffer.save = options.save;
      return buffer;
    },
    get(key, options = {}) { return buffers.get(mapKey(key, scopeFor(options, key))); },
    release(key, options = {}) { const id = mapKey(key, scopeFor(options, key)); const buffer = buffers.get(id); if (buffer && !buffer.state().dirty && !buffer.running) buffers.delete(id); },
    clearEpoch(epoch) {
      activeScope = String(epoch);
      for (const [id, buffer] of buffers) if (buffer.epoch !== activeScope) { buffer.invalidated = true; buffers.delete(id); }
    },
    setScope(actorId, epoch) {
      activeScope = `${String(actorId)}:${String(epoch)}`;
      for (const [id, buffer] of buffers) if (`${buffer.actorId}:${buffer.epoch}` !== activeScope) { buffer.invalidated = true; buffers.delete(id); }
    },
    invalidateAll() {
      for (const buffer of buffers.values()) buffer.invalidated = true;
      buffers.clear();
    },
    persistDraft(buffer) {
      clearTimeout(recoveryTimers.get(buffer)); recoveryTimers.delete(buffer);
      if (!buffer.state().dirty) return false;
      const key = draftKey(buffer); const snapshot = buffer.snapshot();
      try {
        if (!storage) throw new Error('Draft recovery storage is unavailable');
        storage.setItem(key, JSON.stringify(snapshot));
        memoryDrafts.delete(key); buffer.recoveryError = null; return true;
      } catch (error) {
        memoryDrafts.set(key, { snapshot, error }); buffer.recoveryError = error; return false;
      }
    },
    scheduleDraft(buffer) {
      clearTimeout(recoveryTimers.get(buffer));
      recoveryTimers.set(buffer, setTimeout(() => this.persistDraft(buffer), 250));
    },
    recoverDraft(handle, options = {}) {
      const probe = options.offerOnly ? new ResourceBuffer(handle, options.envelope, options) : this.acquire(handle, options.envelope, options);
      const key = draftKey(probe);
      try {
        const memory = memoryDrafts.get(key);
        const raw = memoryDrafts.has(key) ? null : storage?.getItem(key);
        const saved = memory?.snapshot || (raw ? JSON.parse(raw) : null);
        if (!saved || saved.discarded === true) {
          // Probing thousands of indexed documents for recovery must not leave
          // thousands of clean, revision-pinned buffers behind.
          if (!options.offerOnly) this.release(handle.key, options);
          return null;
        }
        const currentRevision = probe.handle.revision;
        probe.recover(saved);
        if (options.offerOnly) return { snapshot:copy(saved), recoveryError:memory?.error || null };
        probe.recoveryError = memory?.error || null;
        if (currentRevision.kind !== saved.expectedRevision?.kind || currentRevision.value !== saved.expectedRevision?.value) {
          probe.status = 'conflict';
          probe.conflict = { local:probe.snapshot(), remote:{ revision:currentRevision, envelope:copy(options.envelope) } };
        }
        return probe;
      } catch (error) { probe.recoveryError = error; return null; }
    },
    discardDraft(buffer) {
      clearTimeout(recoveryTimers.get(buffer)); recoveryTimers.delete(buffer);
      const key = draftKey(buffer);
      try {
        storage?.removeItem(key); memoryDrafts.delete(key); buffer.recoveryError = null; return true;
      } catch (error) {
        try {
          if (!storage) throw error;
          storage.setItem(key, JSON.stringify({ discarded:true }));
          memoryDrafts.delete(key); buffer.recoveryError = null; return true;
        } catch (_) {
          buffer.recoveryError = error; return false;
        }
      }
    },
    values() { return [...buffers.values()]; },
  };
}

export { ResourceBuffer };
