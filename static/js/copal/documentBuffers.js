import { normalizeResourceHandle, resourceKeyId, snapshotEnvelope, cloneEnvelope, normalizeRevision } from './resourceModel.js';

function copy(value) { return cloneEnvelope(value); }

function actionId(localRevision) {
  const suffix = globalThis.crypto?.randomUUID?.() || `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
  return `save-${Math.max(0, Number(localRevision) || 0)}-${suffix}`;
}

class ResourceBuffer {
  constructor(handle, envelope, { save = null, actorId = '', epoch = '', scope = null, historyLimit = 100, historyLimitBytes = 2 * 1024 * 1024 } = {}) {
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
    this.historyLimit = Math.max(1, Math.round(Number(historyLimit) || 100));
    this.historyBytes = 0;
    this.redoBytes = 0;
    this.historyLimitBytes = Math.max(64 * 1024, Math.round(Number(historyLimitBytes) || 2 * 1024 * 1024));
  }

  subscribe(listener) { if (typeof listener === 'function') this.listeners.add(listener); return () => this.listeners.delete(listener); }
  _emit() { const state = this.state(); for (const listener of this.listeners) listener(state); }
  state() { return { key:this.key, localRevision:this.localRevision, savedLocalRevision:this.savedLocalRevision, dirty:this.localRevision !== this.savedLocalRevision, status:this.status, error:this.error, recoveryError:this.recoveryError, conflict:this.conflict, projections:this.projections }; }
  _actionId(localRevision = this.localRevision) {
    if (!this.actionIds.has(localRevision)) this.actionIds.set(localRevision, actionId(localRevision));
    return this.actionIds.get(localRevision);
  }
  acknowledge(localRevision, revision) {
    const next = normalizeRevision(revision);
    if (this.invalidated || !Number.isSafeInteger(localRevision) || localRevision < this.savedLocalRevision || localRevision > this.localRevision) return false;
    this.handle = { ...this.handle, revision:next };
    this.savedLocalRevision = localRevision;
    for (const key of this.actionIds.keys()) if (key <= localRevision) this.actionIds.delete(key);
    this.pending = this.localRevision > localRevision ? this.snapshot() : null;
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
    // Large typing envelopes should not be cloned into an undo record only to
    // discard that record after measuring it.  Transaction history remains
    // available for bounded sheet edits, while oversized edits commit once.
    let recordHistory = history;
    if (recordHistory) {
      const beforeBytes = JSON.stringify(this.envelope).length;
      const afterBytes = JSON.stringify(next).length;
      if (beforeBytes + afterBytes > this.historyLimitBytes) recordHistory = false;
    }
    const before = recordHistory ? copy(this.envelope) : null;
    this.envelope = next === transactionInput ? next : copy(next);
    this.localRevision += 1;
    this.pendingSheetRevision = sheet === true ? this.localRevision : null;
    if (recordHistory) {
      const entry = { localRevision:this.localRevision, before, after:copy(this.envelope), origin };
      entry.bytes = JSON.stringify(entry).length;
      this.history.push(entry); this.historyBytes += entry.bytes;
      while (this.history.length > this.historyLimit || this.historyBytes > this.historyLimitBytes) { const removed = this.history.shift(); this.historyBytes -= removed?.bytes || 0; }
    }
    if (origin !== 'history') { this.redoHistory = []; this.redoBytes = 0; }
    if (origin === 'remote') this.savedLocalRevision = this.localRevision;
    else { this.status = 'unsaved'; this.error = null; this.conflict = null; this.projections = null; }
    this._emit();
    return this.snapshot();
  }
  undo() {
    const entry = this.history.pop();
    if (!entry) return null;
    this.redoHistory.push(entry);
    this.historyBytes -= entry.bytes || 0; this.redoBytes += entry.bytes || 0;
    this.envelope = copy(entry.before);
    this.pendingSheetRevision = null;
    this.localRevision += 1; this.status = 'unsaved'; this.error = null; this.conflict = null;
    this.pending = this.snapshot(); this._emit(); return this.snapshot();
  }
  redo() {
    const entry = this.redoHistory.pop();
    if (!entry) return null;
    this.history.push(entry);
    this.historyBytes += entry.bytes || 0; this.redoBytes -= entry.bytes || 0;
    this.envelope = copy(entry.after);
    this.pendingSheetRevision = null;
    this.localRevision += 1; this.status = 'unsaved'; this.error = null; this.conflict = null;
    this.pending = this.snapshot(); this._emit(); return this.snapshot();
  }
  snapshot() { return snapshotEnvelope({ key:this.key, expectedRevision:this.handle.revision, localRevision:this.localRevision, actionId:this.localRevision ? this._actionId() : null, scope:this.scope, envelope:this.envelope }); }
  async _flushOne(options = {}) {
    if (this.status === 'conflict') return { outcome:'conflict', conflict:this.conflict };
    if (!this.save) throw new Error('resource buffer has no save adapter');
    if (!this.pending || this.pending.localRevision !== this.localRevision) this.pending = this.snapshot();
    const submitted = this.pending;
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
          return false;
        }
        this.projections = result.projections || null;
        this.acknowledge(submitted.localRevision, result.revision);
        return result;
      } catch (error) {
        this.status = 'error'; this.error = error; this.pending = submitted; return false;
      } finally {
        this.running = null; this._emit();
      }
    })();
    return this.running;
  }
  async flush(options = {}) {
    if (this.drainPromise) return this.drainPromise;
    this.drainPromise = (async () => {
      let result = true;
      while (this.pending || this.localRevision > this.savedLocalRevision) {
        result = await this._flushOne(options);
        if (result?.outcome === 'conflict' || result === false || this.invalidated) return result;
      }
      return result;
    })();
    try { return await this.drainPromise; } finally { this.drainPromise = null; }
  }
  resolveExternal(envelope, revision, { force = false } = {}) {
    if (this.state().dirty && !force) {
      this.status = 'conflict'; this.conflict = { local:this.snapshot(), remote:{ envelope:copy(envelope), revision:normalizeRevision(revision) } }; this._emit(); return false;
    }
    this.envelope = copy(envelope); this.handle = { ...this.handle, revision:normalizeRevision(revision) };
    this.localRevision += 1; this.savedLocalRevision = this.localRevision; this.pending = null; this.pendingSheetRevision = null; this.status = 'saved'; this.error = null; this.conflict = null; this._emit(); return true;
  }
  recover(snapshot) {
    const restored = snapshotEnvelope(snapshot);
    if (resourceKeyId(restored.key) !== this.id) throw new Error('draft belongs to another resource');
    this.envelope = copy(restored.envelope); this.localRevision = restored.localRevision; this.savedLocalRevision = Math.max(0, restored.localRevision - 1);
    if (restored.actionId) this.actionIds.set(restored.localRevision, restored.actionId);
    if (restored.scope) this.scope = Object.freeze({ ...this.scope, ...restored.scope, actorId:String(restored.scope.actorId || this.actorId), epoch:String(restored.scope.epoch || this.epoch) });
    this.handle = { ...this.handle, revision:restored.expectedRevision }; this.pendingSheetRevision = null; this.status = 'unsaved'; this.pending = restored; this._emit(); return this.snapshot();
  }
}

export function createBufferRegistry({ storage = globalThis.localStorage, namespace = 'copal-buffer-draft:v1' } = {}) {
  const buffers = new Map();
  // Failed persistent writes retain the newest snapshot under its immutable
  // actor/resource key. This survives scope teardown, not a process restart.
  const memoryDrafts = new Map();
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
    recoverDraft(handle, options = {}) {
      const probe = this.acquire(handle, options.envelope, options);
      const key = draftKey(probe);
      try {
        const memory = memoryDrafts.get(key);
        const raw = memoryDrafts.has(key) ? null : storage?.getItem(key);
        const saved = memory?.snapshot || (raw ? JSON.parse(raw) : null);
        if (!saved) {
          // Probing thousands of indexed documents for recovery must not leave
          // thousands of clean, revision-pinned buffers behind.
          this.release(handle.key, options);
          return null;
        }
        const currentRevision = probe.handle.revision;
        probe.recover(saved);
        probe.recoveryError = memory?.error || null;
        if (currentRevision.kind !== saved.expectedRevision?.kind || currentRevision.value !== saved.expectedRevision?.value) {
          probe.status = 'conflict';
          probe.conflict = { local:probe.snapshot(), remote:{ revision:currentRevision, envelope:copy(options.envelope) } };
        }
        return probe;
      } catch (error) { probe.recoveryError = error; return null; }
    },
    discardDraft(buffer) {
      const key = draftKey(buffer);
      try {
        storage?.removeItem(key); memoryDrafts.delete(key); buffer.recoveryError = null; return true;
      } catch (error) {
        // Suppress stale persisted content for this session after a live save
        // or explicit discard, even when persistent cleanup fails.
        memoryDrafts.set(key, null); buffer.recoveryError = error; return false;
      }
    },
    values() { return [...buffers.values()]; },
  };
}

export { ResourceBuffer };
