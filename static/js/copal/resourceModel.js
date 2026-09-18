/** Shared, authority-neutral Copal resource and revision DTOs. */

const PROVIDERS = new Set(['copal', 'host', 'treehouse']);
const REPRESENTATIONS = new Set(['nativeNote', 'markdown', 'text', 'base', 'meme', 'asset', 'courseDraft']);
const REVISION_KINDS = new Set(['copalHead', 'hostFingerprint', 'domain']);

const string = (value, field) => {
  const result = String(value ?? '').trim();
  if (!result) throw new TypeError(`${field} is required`);
  return result;
};

export function normalizeResourceKey(value = {}) {
  const source = value && typeof value === 'object' ? value : {};
  const provider = string(source.provider, 'provider');
  if (!PROVIDERS.has(provider)) throw new TypeError(`unsupported provider: ${provider}`);
  return Object.freeze({
    accountId: string(source.accountId, 'accountId'),
    workspaceId: string(source.workspaceId, 'workspaceId'),
    provider,
    resourceId: string(source.resourceId, 'resourceId'),
  });
}

export function resourceKeyId(value) {
  const key = normalizeResourceKey(value);
  return [key.accountId, key.workspaceId, key.provider, key.resourceId]
    .map((part) => encodeURIComponent(part)).join(':');
}

export function sameResourceKey(left, right) {
  try { return resourceKeyId(left) === resourceKeyId(right); } catch (_) { return false; }
}

export function normalizeRevision(value = {}) {
  const source = value && typeof value === 'object' ? value : {};
  const kind = string(source.kind, 'revision kind');
  if (!REVISION_KINDS.has(kind)) throw new TypeError(`unsupported revision kind: ${kind}`);
  return Object.freeze({ kind, value: string(source.value, 'revision value') });
}

function capabilities(value = {}) {
  const source = value && typeof value === 'object' ? value : {};
  return Object.freeze(Object.fromEntries(['read', 'edit', 'rename', 'move', 'trash', 'attach', 'reveal']
    .map((name) => [name, source[name] === true])));
}

function resourceMetadata(value = {}) {
  const source = value && typeof value === 'object' ? value : {};
  const allowed = ['encoding', 'newline', 'bomBytes', 'mode', 'language'];
  const result = Object.fromEntries(allowed
    .filter((name) => source[name] != null)
    .map((name) => [name, source[name]]));
  return Object.keys(result).length ? Object.freeze(result) : null;
}

export function normalizeResourceHandle(value = {}) {
  const source = value && typeof value === 'object' ? value : {};
  const representation = string(source.representation, 'representation');
  if (!REPRESENTATIONS.has(representation)) throw new TypeError(`unsupported representation: ${representation}`);
  const location = source.locator && typeof source.locator === 'object' ? source.locator : {};
  const metadata = resourceMetadata(source.metadata);
  return Object.freeze({
    key: normalizeResourceKey(source.key),
    revision: normalizeRevision(source.revision),
    locator: Object.freeze({
      displayName: string(location.displayName, 'displayName'),
      locationLabel: string(location.locationLabel, 'locationLabel'),
      ...(location.opaqueRef == null ? {} : { opaqueRef: String(location.opaqueRef) }),
    }),
    representation,
    capabilities: capabilities(source.capabilities),
    ...(metadata ? { metadata } : {}),
  });
}

export function cloneEnvelope(value) {
  if (value === undefined) return undefined;
  if (typeof structuredClone === 'function') return structuredClone(value);
  return JSON.parse(JSON.stringify(value));
}

function freezeDeep(value, seen = new WeakSet()) {
  if (!value || typeof value !== 'object' || seen.has(value)) return value;
  seen.add(value);
  for (const child of Object.values(value)) freezeDeep(child, seen);
  return Object.freeze(value);
}

export function snapshotEnvelope({ key, expectedRevision, localRevision = 0, envelope, actionId = null, scope = null } = {}) {
  if (!Number.isSafeInteger(localRevision) || localRevision < 0) throw new TypeError('localRevision must be a non-negative integer');
  const normalizedScope = scope && typeof scope === 'object' ? Object.freeze(Object.fromEntries(
    ['accountId', 'workspace', 'workspaceId', 'storageNamespace', 'actorId', 'epoch']
      .filter((field) => scope[field] != null)
      .map((field) => [field, String(scope[field])]),
  )) : null;
  return Object.freeze({
    key: normalizeResourceKey(key),
    expectedRevision: normalizeRevision(expectedRevision),
    localRevision,
    ...(actionId == null ? {} : { actionId:string(actionId, 'actionId') }),
    ...(normalizedScope ? { scope:normalizedScope } : {}),
    envelope: freezeDeep(cloneEnvelope(envelope)),
  });
}

export function sameEnvelope(left, right) {
  try { return JSON.stringify(left) === JSON.stringify(right); } catch (_) { return false; }
}

export function normalizeMutationReceipt(value = {}) {
  const source = value && typeof value === 'object' ? value : {};
  return Object.freeze({
    actionId: string(source.actionId, 'actionId'),
    key: normalizeResourceKey(source.key),
    before: source.before === 'absent' ? 'absent' : normalizeRevision(source.before),
    after: source.after === 'absent' ? 'absent' : normalizeRevision(source.after),
    ...(source.acknowledgedLocalRevision == null ? {} : { acknowledgedLocalRevision: Number(source.acknowledgedLocalRevision) }),
    outcome: ['applied', 'conflict', 'failed', 'partial'].includes(source.outcome) ? source.outcome : 'failed',
    history: ['pending', 'complete', 'paused_budget', 'failed_io', 'uncovered'].includes(source.history) ? source.history : 'uncovered',
    historyPhase: ['before', 'after', 'complete', 'none'].includes(source.historyPhase) ? source.historyPhase : 'none',
    coverage: ['knownMutationHooks', 'declaredRootsBaseline', 'observedAfterOnly', 'uncovered'].includes(source.coverage) ? source.coverage : 'uncovered',
    ...(source.beforeVersionId == null ? {} : { beforeVersionId: String(source.beforeVersionId) }),
    ...(source.afterVersionId == null ? {} : { afterVersionId: String(source.afterVersionId) }),
    changedKeys: (Array.isArray(source.changedKeys) ? source.changedKeys : []).map(normalizeResourceKey),
  });
}

export const RESOURCE_MODEL_VERSION = 1;
