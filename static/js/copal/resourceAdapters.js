import { normalizeResourceHandle, normalizeResourceKey, normalizeRevision, snapshotEnvelope, cloneEnvelope } from './resourceModel.js';

/** Convert server DTOs without treating access refs or paths as identities. */
export function fromCopalResource(value = {}, context = {}) {
  const source = value && typeof value === 'object' ? value : {};
  return normalizeResourceHandle({
    key: source.key || {
      accountId: source.accountId || context.accountId,
      workspaceId: source.workspaceId || context.workspaceId,
      provider: 'copal', resourceId: source.id || source.resourceId,
    },
    revision: source.revision || { kind: 'copalHead', value: source.head || source.version || '0' },
    locator: source.locator || {
      displayName: source.displayName || source.name || source.id,
      locationLabel: source.locationLabel || source.name || source.id,
      ...(source.resourceRef == null ? {} : { opaqueRef: source.resourceRef }),
    },
    representation: source.representation || (source.kind === 'base' ? 'base' : 'nativeNote'),
    capabilities: source.capabilities || { read:true },
  });
}

export function fromHostResource(value = {}, context = {}) {
  const source = value && typeof value === 'object' ? value : {};
  // resourceId is server supplied. A path is display/location metadata only.
  return normalizeResourceHandle({
    key: source.key || {
      accountId: source.accountId || context.accountId,
      workspaceId: source.workspaceId || context.workspaceId,
      provider: 'host', resourceId: source.resourceId || source.id,
    },
    revision: source.revision || { kind: 'hostFingerprint', value: source.fingerprint || source.etag || '0' },
    locator: source.locator || {
      displayName: source.displayName || source.name || source.path,
      locationLabel: source.locationLabel || source.path || source.name,
      ...(source.resourceRef == null ? {} : { opaqueRef: source.resourceRef }),
    },
    representation: source.representation || 'text',
    capabilities: source.capabilities || { read:true },
  });
}

export function makeMutationRequest(handle, envelope, localRevision, { actionId, origin = 'editor' } = {}) {
  const resource = normalizeResourceHandle(handle);
  return {
    actionId: String(actionId || globalThis.crypto?.randomUUID?.() || `action-${Date.now()}-${Math.random().toString(36).slice(2)}`),
    key: resource.key,
    expectedRevision: resource.revision,
    snapshot: snapshotEnvelope({ key:resource.key, expectedRevision:resource.revision, localRevision, envelope }),
    origin: String(origin),
  };
}

export function createResourceAdapter({ resource, save, resolve = null, recover = null } = {}) {
  const handle = normalizeResourceHandle(resource);
  if (typeof save !== 'function') throw new TypeError('adapter save function is required');
  return Object.freeze({
    kind: handle.key.provider,
    resource: handle,
    resolve: typeof resolve === 'function' ? resolve : async () => handle,
    recover: typeof recover === 'function' ? recover : async () => null,
    async save(snapshot, options = {}) {
      const immutable = snapshotEnvelope({ ...snapshot, key:handle.key });
      if (!handle.capabilities.edit) throw Object.assign(new Error('resource is read-only'), { code:'forbidden' });
      const result = await save({
        key: immutable.key,
        expectedRevision: immutable.expectedRevision,
        localRevision: immutable.localRevision,
        envelope: cloneEnvelope(immutable.envelope),
        actionId: options.actionId,
        origin: options.origin,
      });
      return result;
    },
  });
}

export function revisionFromResponse(value, fallback) {
  if (value?.revision) return normalizeRevision(value.revision);
  if (value?.head) return normalizeRevision({ kind:'copalHead', value:value.head });
  if (value?.fingerprint) return normalizeRevision({ kind:'hostFingerprint', value:value.fingerprint });
  return normalizeRevision(fallback);
}
