export class FilesFacadeError extends Error {
  constructor(message, { code = 'provider_unavailable', status = 0 } = {}) {
    super(message);
    this.name = 'FilesFacadeError';
    this.code = code;
    this.status = status;
  }
}

function strictRevision(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)
    || Object.keys(value).some((key) => !['kind', 'value'].includes(key))) throw new TypeError('revision is invalid');
  const kind = String(value.kind || '').trim();
  const token = String(value.value || '').trim();
  if (!kind || kind.length > 128 || !token || token.length > 512) throw new TypeError('revision is invalid');
  return { kind, value: token };
}

function strictResourceKey(value) {
  if (typeof value === 'string') {
    if (!value.trim() || value.length > 16384 || value.includes('\u0000')) throw new FilesFacadeError('Files resource key is invalid');
    return value;
  }
  if (!value || typeof value !== 'object' || Array.isArray(value)
    || Object.keys(value).some((key) => !['provider', 'account_id', 'workspace_id', 'resource_id'].includes(key))
    || typeof value.provider !== 'string' || !value.provider.trim()
    || typeof value.resource_id !== 'string' || !value.resource_id.trim()) throw new FilesFacadeError('Files resource key is invalid');
  for (const key of ['provider', 'account_id', 'workspace_id', 'resource_id']) {
    if (value[key] != null && (typeof value[key] !== 'string' || !value[key].trim() || value[key].length > 512 || /[\\/\u0000]/.test(value[key]))) throw new FilesFacadeError('Files resource key is invalid');
  }
  return value;
}

function strictPreparation(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)
    || Object.keys(value).some((key) => !['operation_id', 'generation', 'preparation_receipt_id', 'source_revision', 'target_identity', 'target_revision', 'insertion', 'asset', 'source_digest', 'history'].includes(key))) throw new FilesFacadeError('Files attachment preparation response is invalid');
  if (typeof value.operation_id !== 'string' || !value.operation_id || value.operation_id.length > 128
    || !Number.isSafeInteger(value.generation) || value.generation < 0
    || typeof value.preparation_receipt_id !== 'string' || !value.preparation_receipt_id || value.preparation_receipt_id.length > 128) throw new FilesFacadeError('Files attachment preparation response is invalid');
  strictRevision(value.source_revision); strictRevision(value.target_revision);
  if (!value.target_identity || typeof value.target_identity !== 'object' || Array.isArray(value.target_identity)
    || Object.keys(value.target_identity).some((key) => !['resource_key', 'resource_ref', 'kind', 'course_id', 'lesson_id'].includes(key))) throw new FilesFacadeError('Files attachment preparation response is invalid');
  if (typeof value.target_identity.kind !== 'string' || !value.target_identity.kind) throw new FilesFacadeError('Files attachment preparation response is invalid');
  if (value.target_identity.resource_key != null) strictResourceKey(value.target_identity.resource_key);
  if (value.target_identity.resource_ref != null && (typeof value.target_identity.resource_ref !== 'string' || !value.target_identity.resource_ref)) throw new FilesFacadeError('Files attachment preparation response is invalid');
  for (const key of ['course_id', 'lesson_id']) if (value.target_identity[key] != null && (typeof value.target_identity[key] !== 'string' || !value.target_identity[key] || value.target_identity[key].length > 512)) throw new FilesFacadeError('Files attachment preparation response is invalid');
  if (!value.insertion || typeof value.insertion !== 'object' || Array.isArray(value.insertion)
    || Object.keys(value.insertion).some((key) => !['format', 'link_target', 'label', 'media_kind'].includes(key))
    || value.insertion.format !== 'markdown' || ['format', 'link_target', 'label', 'media_kind'].some((key) => typeof value.insertion[key] !== 'string' || !value.insertion[key] || value.insertion[key].length > 512)) throw new FilesFacadeError('Files attachment preparation response is invalid');
  if (value.asset != null && (typeof value.asset !== 'object' || Array.isArray(value.asset) || Object.keys(value.asset).some((key) => !['resource_key', 'resource_ref', 'revision', 'mime_type', 'name'].includes(key)) || !value.asset.resource_key || !value.asset.name || !value.asset.mime_type)) throw new FilesFacadeError('Files attachment preparation response is invalid');
  if (value.asset?.resource_key != null) strictResourceKey(value.asset.resource_key);
  if (value.asset?.resource_ref != null && (typeof value.asset.resource_ref !== 'string' || !value.asset.resource_ref || value.asset.resource_ref.length > 16384)) throw new FilesFacadeError('Files attachment preparation response is invalid');
  if (value.asset?.revision) strictRevision(value.asset.revision);
  if (value.source_digest != null && (typeof value.source_digest !== 'string' || !value.source_digest || value.source_digest.length > 128)) throw new FilesFacadeError('Files attachment preparation response is invalid');
  return value;
}

function strictPreparationStatus(value, operationId) {
  if (!value || typeof value !== 'object' || Array.isArray(value)
    || Object.keys(value).some((key) => !['operation_id', 'generation', 'state', 'preparation'].includes(key))
    || value.operation_id !== operationId || !Number.isSafeInteger(value.generation) || value.generation < 0
    || !['pending', 'complete', 'failed', 'stale'].includes(value.state)) throw new FilesFacadeError('Files attachment status is invalid');
  if (value.preparation != null) {
    const preparation = strictPreparation(value.preparation);
    if (preparation.operation_id !== operationId || preparation.generation !== value.generation) throw new FilesFacadeError('Files attachment status is invalid');
    return { ...value, preparation };
  }
  return value;
}

function strictOperationReceipt(value, operationId, expectedGeneration = null) {
  if (!value || typeof value !== 'object' || Array.isArray(value)
    || Object.keys(value).some((key) => !['operation_id', 'generation', 'state', 'items'].includes(key))
    || value.operation_id !== operationId
    || !Number.isSafeInteger(value.generation) || value.generation < 0
    || (expectedGeneration != null && value.generation !== expectedGeneration)
    || !['complete', 'partial', 'pending'].includes(value.state)
    || !Array.isArray(value.items) || value.items.length > 200) throw new FilesFacadeError('Files operation receipt is invalid');
  const ids = new Set();
  for (const item of value.items) {
    if (!item || typeof item !== 'object' || Array.isArray(item)
      || Object.keys(item).some((key) => !['item_id', 'outcome', 'code', 'resource_ref', 'resource_key', 'receipt_id', 'revision', 'provenance', 'history'].includes(key))
      || typeof item.item_id !== 'string' || !item.item_id || ids.has(item.item_id)
      || !['committed', 'unchanged', 'failed', 'denied', 'conflict', 'stale', 'pending'].includes(item.outcome)) throw new FilesFacadeError('Files operation receipt is invalid');
    for (const key of ['code', 'resource_ref', 'resource_key', 'receipt_id']) if (item[key] != null && (typeof item[key] !== 'string' || !item[key] || item[key].length > 16384)) throw new FilesFacadeError('Files operation receipt is invalid');
    if (item.revision != null) strictRevision(item.revision);
    for (const key of ['provenance', 'history']) if (item[key] != null && (typeof item[key] !== 'object' || Array.isArray(item[key]))) throw new FilesFacadeError('Files operation receipt is invalid');
    ids.add(item.item_id);
  }
  if (value.state === 'complete' && value.items.some((item) => !['committed', 'unchanged'].includes(item.outcome))) throw new FilesFacadeError('Files operation receipt is invalid');
  return value;
}

export class FilesFacadeClient {
  constructor({ baseUrl = '/api/files-v1', fetchImpl = globalThis.fetch } = {}) {
    if (typeof fetchImpl !== 'function') throw new TypeError('fetch implementation is required');
    this.baseUrl = String(baseUrl || '').replace(/\/$/, '');
    this.fetchImpl = fetchImpl.bind(globalThis);
  }

  async _request(path, { method = 'GET', body = null, signal = null } = {}) {
    let response;
    try {
      response = await this.fetchImpl(`${this.baseUrl}${path}`, {
        method,
        credentials: 'same-origin',
        signal,
        ...(body == null ? {} : {
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        }),
      });
    } catch (error) {
      if (error?.name === 'AbortError') throw error;
      throw new FilesFacadeError(error?.message || 'Files provider request failed');
    }
    let payload = null;
    try { payload = await response.json(); } catch { payload = null; }
    if (!response.ok) {
      const detail = payload?.detail;
      const code = (detail && typeof detail === 'object' ? detail.code : null)
        || payload?.code
        || (response.status === 409 ? 'stale_cursor' : response.status === 404 ? 'resource_unavailable' : 'provider_unavailable');
      const message = (detail && typeof detail === 'object' ? detail.message : detail)
        || payload?.message
        || `Files provider request failed (${response.status})`;
      throw new FilesFacadeError(String(message), { code: String(code), status: response.status });
    }
    return payload;
  }

  roots({ copalWorkspace = 'default', signal = null } = {}) {
    const workspace = String(copalWorkspace || 'default').trim();
    if (!/^[A-Za-z0-9._-]{1,64}$/.test(workspace)) throw new TypeError('Copal workspace is invalid');
    return this._request(`/roots?copal_workspace=${encodeURIComponent(workspace)}`, { signal });
  }

  children(parentRef, {
    cursor = null,
    limit = 100,
    sort = null,
    query = '',
    signal = null,
  } = {}) {
    return this._request('/children', {
      method: 'POST',
      signal,
      body: {
        parent_ref: String(parentRef || ''),
        cursor,
        limit,
        sort: sort || {},
        query: String(query || ''),
      },
    });
  }

  stat(resourceRef, { signal = null } = {}) {
    return this._request('/stat', {
      method: 'POST',
      signal,
      body: { resource_ref: String(resourceRef || '') },
    });
  }

  reveal(resourceRef, { signal = null } = {}) {
    const value = String(resourceRef || '').trim();
    if (!value) throw new TypeError('resource reference is required');
    return this._request('/reveal', {
      method: 'POST', signal, body: { resource_ref: value },
    });
  }

  reissue(resourceRef, { signal = null } = {}) {
    const value = String(resourceRef || '').trim();
    if (!value) throw new TypeError('resource reference is required');
    return this._request('/reissue', {
      method: 'POST', signal, body: { resource_ref: value },
    });
  }

  places({ signal = null } = {}) {
    return this._request('/places', { signal });
  }

  savePlace(resourceRef, { signal = null } = {}) {
    return this._request('/places', {
      method: 'POST',
      signal,
      body: { resource_ref: String(resourceRef || '') },
    });
  }

  removePlace(placeId, { signal = null } = {}) {
    const value = String(placeId || '').trim();
    if (!/^place-[0-9a-f]{32}$/.test(value)) throw new TypeError('place id is invalid');
    return this._request(`/places/${encodeURIComponent(value)}`, { method: 'DELETE', signal });
  }

  recents({ signal = null } = {}) {
    return this._request('/recents', { signal });
  }

  clearRecents({ signal = null } = {}) {
    return this._request('/recents', { method: 'DELETE', signal });
  }

  savedSearches({ signal = null } = {}) {
    return this._request('/saved-searches', { signal });
  }

  saveSearch({ name, provider = 'all', query, sort = null } = {}, { signal = null } = {}) {
    const label = String(name || '').trim();
    const scope = String(provider || '').trim().toLowerCase();
    const needle = String(query || '').trim();
    if (!label || label.length > 200) throw new TypeError('saved-search name is invalid');
    if (!['all', 'host', 'copal', 'gallery', 'library'].includes(scope)) {
      throw new TypeError('saved-search provider is invalid');
    }
    if (!needle || needle.length > 512) throw new TypeError('saved-search query is invalid');
    return this._request('/saved-searches', {
      method: 'POST', signal, body: { name: label, provider: scope, query: needle, sort: sort || {} },
    });
  }

  removeSavedSearch(searchId, { signal = null } = {}) {
    const value = String(searchId || '').trim();
    if (!/^search-[0-9a-f]{32}$/.test(value)) throw new TypeError('saved-search id is invalid');
    return this._request(`/saved-searches/${encodeURIComponent(value)}`, { method: 'DELETE', signal });
  }

  search(query, { limit = 100, sort = null, copalWorkspace = 'default', signal = null } = {}) {
    const value = String(query || '').trim();
    if (!value) throw new TypeError('search query is required');
    const workspace = String(copalWorkspace || 'default').trim();
    if (!/^[A-Za-z0-9._-]{1,64}$/.test(workspace)) throw new TypeError('Copal workspace is invalid');
    const path = workspace === 'default' ? '/search' : `/search?copal_workspace=${encodeURIComponent(workspace)}`;
    return this._request(path, {
      method: 'POST',
      signal,
      body: {
        query: value,
        limit: Math.max(1, Math.min(200, Math.round(Number(limit) || 100))),
        sort: sort || {},
      },
    });
  }

  open(resourceRef, { signal = null } = {}) {
    return this._request('/action', {
      method: 'POST',
      signal,
      body: {
        resource_ref: String(resourceRef || ''),
        action: 'open',
        args: {},
      },
    });
  }

  hostApps(resourceRef, { signal = null } = {}) {
    const value = String(resourceRef || '').trim();
    if (!value) throw new TypeError('resource reference is required');
    return this._request('/host-apps', {
      method: 'POST', signal, body: { resource_ref: value },
    });
  }

  openHost(resourceRef, appId, { signal = null } = {}) {
    const value = String(resourceRef || '').trim();
    const application = String(appId || '').trim();
    if (!value) throw new TypeError('resource reference is required');
    if (!application || application.length > 512) throw new TypeError('host application is invalid');
    return this._request('/open-host', {
      method: 'POST', signal, body: { resource_ref: value, app_id: application },
    });
  }

  workspace(resourceRef, purpose = 'app_folder', { signal = null } = {}) {
    const normalized = String(purpose || '').trim();
    if (!['app_folder', 'agent_workspace'].includes(normalized)) {
      throw new TypeError('workspace purpose is invalid');
    }
    return this._request('/workspace', {
      method: 'POST',
      signal,
      body: {
        resource_ref: String(resourceRef || ''),
        purpose: normalized,
      },
    });
  }

  workspaces({ includeArchived = false, signal = null } = {}) {
    return this._request(`/workspaces?include_archived=${includeArchived ? 'true' : 'false'}`, { signal });
  }

  updateWorkspace(workspaceId, update, { signal = null } = {}) {
    const identifier = String(workspaceId || '').trim();
    if (!identifier || identifier.length > 256) throw new TypeError('workspace id is invalid');
    const body = {};
    if (Object.hasOwn(update || {}, 'name')) {
      const name = String(update.name || '').trim();
      if (!name || name.length > 200) throw new TypeError('workspace name is invalid');
      body.name = name;
    }
    if (Object.hasOwn(update || {}, 'archived')) body.archived = Boolean(update.archived);
    if (Object.hasOwn(update || {}, 'expected_revision')) {
      const revision = Number(update.expected_revision);
      if (!Number.isInteger(revision) || revision < 1) throw new TypeError('workspace revision is invalid');
      body.expected_revision = revision;
    }
    if (!Object.hasOwn(body, 'name') && !Object.hasOwn(body, 'archived')) {
      throw new TypeError('workspace update is empty');
    }
    return this._request(`/workspaces/${encodeURIComponent(identifier)}`, {
      method: 'PATCH', signal, body,
    });
  }

  workspaceResource(workspaceId, relativePath = '', { signal = null } = {}) {
    const identifier = String(workspaceId || '').trim();
    if (!identifier || identifier.length > 256) throw new TypeError('workspace id is invalid');
    const relative = String(relativePath || '').replace(/\\/g, '/');
    if (
      relative.length > 4096
      || relative.startsWith('/')
      || /^[A-Za-z]:/.test(relative)
      || relative.split('/').some((part, index, parts) => (
        part === '..' || (part === '' && parts.length > 1) || (part === '.' && relative !== '.')
      ))
    ) throw new TypeError('workspace-relative path is invalid');
    return this._request('/workspace-resource', {
      method: 'POST', signal, body: { workspace_id: identifier, relative_path: relative },
    });
  }

  action(resourceRef, action, args = {}, { signal = null, actionId = null } = {}) {
    const normalized = String(action || '').trim().toLowerCase();
    if (!['favorite.set', 'archive.set', 'rename', 'move', 'trash', 'restore'].includes(normalized)) {
      throw new TypeError('resource action is unsupported');
    }
    if (['favorite.set', 'archive.set'].includes(normalized) && (!args || typeof args.value !== 'boolean' || Object.keys(args).length !== 1)) {
      throw new TypeError('resource action requires one boolean value');
    }
    if (['rename', 'move'].includes(normalized) && (!args || typeof args.name !== 'string' || !args.name.trim() || Object.keys(args).length !== 1)) throw new TypeError('resource action requires one name');
    if (['trash', 'restore'].includes(normalized) && Object.keys(args || {}).length) throw new TypeError('resource action accepts no arguments');
    const payloadArgs = ['favorite.set', 'archive.set'].includes(normalized) ? { value: args.value } : { ...args };
    return this._request('/action', {
      method: 'POST',
      signal,
      body: {
        resource_ref: String(resourceRef || ''),
        action: normalized,
        ...(actionId ? { action_id: String(actionId) } : {}),
        args: payloadArgs,
      },
    });
  }

  openResource(resourceRef, { signal = null } = {}) {
    return this._request('/open-resource', {
      method: 'POST',
      signal,
      body: { resource_ref: String(resourceRef || '') },
    });
  }

  saveResource(resourceRef, { expectedRevision, text = '', signal = null } = {}) {
    const value = String(resourceRef || '').trim();
    if (!value) throw new TypeError('resource reference is required');
    const revision = expectedRevision && typeof expectedRevision === 'object' ? expectedRevision : {};
    if (revision.kind !== 'hostFingerprint' || !String(revision.value || '').trim()) {
      throw new TypeError('host fingerprint revision is required');
    }
    return this._request('/save-resource', {
      method: 'POST',
      signal,
      body: {
        resource_ref: value,
        expected_revision: { kind: 'hostFingerprint', value: String(revision.value) },
        text: String(text ?? ''),
      },
    });
  }

  createResource(parentRef, { name, text = '', actionId = null, signal = null } = {}) {
    const parent = String(parentRef || '').trim();
    const filename = String(name || '').trim();
    if (!parent) throw new TypeError('parent resource reference is required');
    if (!filename || filename.length > 240 || filename.includes('/') || filename.includes('\\')) {
      throw new TypeError('resource name is invalid');
    }
    if (!/\.(?:md|markdown)$/i.test(filename)) throw new TypeError('resource name must be Markdown');
    return this._request('/create', {
      method: 'POST', signal,
      body: { parent_ref: parent, name: filename, text: String(text ?? ''), ...(actionId ? { action_id: String(actionId) } : {}) },
    });
  }

  createDirectory(parentRef, {
    name, operationId, generation, expectedRevision = null, collision = 'fail', signal = null,
  } = {}) {
    const parent = String(parentRef || '').trim();
    const directory = String(name || '').trim();
    const operation = String(operationId || '').trim();
    const currentGeneration = Number(generation);
    if (!parent) throw new TypeError('parent resource reference is required');
    if (!directory || directory.length > 240 || directory.includes('/') || directory.includes('\\')) {
      throw new TypeError('directory name is invalid');
    }
    if (!operation || operation.length > 128 || !Number.isSafeInteger(currentGeneration) || currentGeneration < 0) {
      throw new TypeError('directory operation is invalid');
    }
    const normalizedCollision = String(collision || 'fail').trim().toLowerCase();
    if (!['fail', 'reuse'].includes(normalizedCollision)) throw new TypeError('directory collision policy is invalid');
    const revision = expectedRevision == null ? null : strictRevision(expectedRevision);
    return this._request('/create-directory', {
      method: 'POST', signal,
      body: {
        parent_ref: parent, name: directory, operation_id: operation, generation: currentGeneration,
        ...(revision ? { expected_revision: revision } : {}),
        collision: normalizedCollision,
      },
    });
  }

  resolveResource(resourceKey, { signal = null } = {}) {
    const key = resourceKey && typeof resourceKey === 'object' ? { ...resourceKey } : { provider: 'copal', resource_id: String(resourceKey || '').trim() };
    if (key.provider !== 'copal' || !key.resource_id || Object.keys(key).some((item) => !['provider', 'resource_id', 'account_id', 'workspace_id'].includes(item))) {
      throw new TypeError('resource key is invalid');
    }
    return this._request('/resolve-resource', {
      method: 'POST', signal, body: { resource_key: key },
    });
  }

  transferResources({ operationId, generation, kind, sources, destinationRef, collision = 'fail' } = {}, { signal = null } = {}) {
    const operation = String(operationId || '').trim();
    const destination = String(destinationRef || '').trim();
    const normalizedKind = String(kind || '').trim().toLowerCase();
    const normalizedCollision = String(collision || 'fail').trim().toLowerCase();
    if (!operation || operation.length > 128 || !destination || !['move', 'copy'].includes(normalizedKind)) throw new TypeError('transfer request is invalid');
    if (!['fail', 'rename'].includes(normalizedCollision) || !Array.isArray(sources) || sources.length < 1 || sources.length > 200) throw new TypeError('transfer request is invalid');
    const currentGeneration = Number(generation);
    if (!Number.isSafeInteger(currentGeneration) || currentGeneration < 0) throw new TypeError('transfer generation is invalid');
    const normalizedSources = sources.map((source) => {
      const itemId = String(source?.itemId ?? source?.item_id ?? '').trim();
      const ref = String(source?.resourceRef ?? source?.resource_ref ?? '').trim();
      if (!itemId || itemId.length > 128 || !ref) throw new TypeError('transfer source is invalid');
      const expected = source?.expectedRevision || source?.expected_revision;
      return { item_id: itemId, resource_ref: ref, ...(expected ? { expected_revision: strictRevision(expected) } : {}) };
    });
    return this._request('/transfers', {
      method: 'POST', signal,
      body: { operation_id: operation, generation: currentGeneration, kind: normalizedKind, sources: normalizedSources, destination_ref: destination, collision: normalizedCollision },
    }).then((payload) => strictOperationReceipt(payload, operation, currentGeneration));
  }

  operationReceipt(operationId, { signal = null, generation = null } = {}) {
    const value = String(operationId || '').trim();
    if (!value || value.length > 128) throw new TypeError('operation id is invalid');
    const expectedGeneration = generation == null ? null : Number(generation);
    if (expectedGeneration != null && (!Number.isSafeInteger(expectedGeneration) || expectedGeneration < 0)) throw new TypeError('operation generation is invalid');
    return this._request(`/operations/${encodeURIComponent(value)}`, { signal }).then((payload) => strictOperationReceipt(payload, value, expectedGeneration));
  }

  async importFile(file, { operationId, itemId, generation, destinationRef, name, relativePath = '', collision = 'fail', signal = null } = {}) {
    if (!file || typeof file !== 'object') throw new TypeError('import file is required');
    const operation = String(operationId || '').trim();
    const item = String(itemId || '').trim();
    const destination = String(destinationRef || '').trim();
    const filename = String(name || file.name || '').trim();
    const relative = String(relativePath || filename).replace(/\\/g, '/').trim();
    const currentGeneration = Number(generation);
    const parts = relative.split('/');
    if (!operation || operation.length > 128 || !item || item.length > 128 || !destination || !filename || !relative || relative.length > 4096 || parts.some((part) => !part || part === '.' || part === '..' || /[\\\0]/.test(part)) || parts.at(-1) !== filename || !Number.isSafeInteger(currentGeneration) || currentGeneration < 0) throw new TypeError('import request is invalid');
    if (!['fail', 'rename'].includes(String(collision || 'fail').toLowerCase())) throw new TypeError('import collision policy is invalid');
    const form = new FormData();
    form.append('file', file, filename);
    form.append('metadata', JSON.stringify({ operation_id: operation, item_id: item, generation: currentGeneration, destination_ref: destination, name: filename, relative_path: relative, collision: String(collision || 'fail').toLowerCase() }));
    let response;
    try {
      response = await this.fetchImpl(`${this.baseUrl}/imports`, { method: 'POST', credentials: 'same-origin', signal, body: form });
    } catch (error) {
      if (error?.name === 'AbortError') throw error;
      throw new FilesFacadeError(error?.message || 'Files import failed');
    }
    let payload = null;
    try { payload = await response.json(); } catch { payload = null; }
    if (!response.ok) {
      const detail = payload?.detail;
      throw new FilesFacadeError((detail && typeof detail === 'object' ? detail.message : detail) || payload?.message || `Files import failed (${response.status})`, { code: (detail && typeof detail === 'object' ? detail.code : null) || payload?.code || 'provider_unavailable', status: response.status });
    }
    return strictOperationReceipt(payload, operation, currentGeneration);
  }

  /** Create a durable zero-byte Markdown resource through the import receipt lane. */
  createFile(parentRef, {
    name = 'Untitled.md', operationId, itemId = operationId, generation,
    collision = 'fail', signal = null,
  } = {}) {
    const parent = String(parentRef || '').trim();
    const filename = String(name || '').trim();
    if (!parent || !filename || !operationId || !itemId) throw new TypeError('file creation request is invalid');
    const empty = new Blob([], { type: 'text/markdown' });
    return this.importFile(empty, {
      operationId, itemId, generation, destinationRef: parent,
      name: filename, relativePath: filename, collision, signal,
    });
  }

  prepareAttachment({ operationId, generation, source, target, mode, workspace = null } = {}, { signal = null } = {}) {
    const operation = String(operationId || '').trim();
    const currentGeneration = Number(generation);
    const normalizedMode = String(mode || '').trim().toLowerCase();
    if (!operation || operation.length > 128 || !Number.isSafeInteger(currentGeneration) || currentGeneration < 0 || !source || !target || !['link', 'embed'].includes(normalizedMode)) throw new TypeError('attachment preparation is invalid');
    const sourceKeys = Object.keys(source);
    const sourceRef = source.resourceRef || source.resource_ref;
    const importId = source.importReceiptId || source.import_receipt_id;
    const hasRef = Boolean(sourceRef), hasImport = Boolean(importId);
    if (hasRef === hasImport || sourceKeys.some((key) => !['resourceRef', 'resource_ref', 'importReceiptId', 'import_receipt_id', 'itemId', 'item_id', 'expectedRevision', 'expected_revision'].includes(key))) throw new TypeError('attachment source must have one variant');
    const expected = source.expectedRevision || source.expected_revision;
    const normalizedSource = hasRef
      ? { resource_ref: String(sourceRef), ...(expected ? { expected_revision: strictRevision(expected) } : {}) }
      : { import_receipt_id: String(importId), item_id: String(source.itemId || source.item_id || ''), ...(expected ? { expected_revision: strictRevision(expected) } : {}) };
    if (!normalizedSource.item_id && !hasRef) throw new TypeError('attachment import source requires item id');
    if (!target || typeof target !== 'object') throw new TypeError('attachment target is invalid');
    const targetKeys = Object.keys(target);
    const targetKind = String(target.kind || '').trim();
    const targetRef = target.resourceRef || target.resource_ref;
    const targetExpected = target.expectedRevision || target.expected_revision;
    if (targetKind === 'copal_document') {
      if (!targetRef || targetKeys.some((key) => !['kind', 'resourceRef', 'resource_ref', 'expectedRevision', 'expected_revision'].includes(key))) throw new TypeError('attachment target is invalid');
    } else if (targetKind === 'treehouse_lesson') {
      if (!target.courseId && !target.course_id || !target.lessonId && !target.lesson_id || targetKeys.some((key) => !['kind', 'courseId', 'course_id', 'lessonId', 'lesson_id', 'expectedRevision', 'expected_revision'].includes(key))) throw new TypeError('attachment target is invalid');
    } else throw new TypeError('attachment target is invalid');
    const normalizedTarget = targetKind === 'copal_document'
      ? { kind: targetKind, resource_ref: String(targetRef), ...(targetExpected ? { expected_revision: strictRevision(targetExpected) } : {}) }
      : { kind: targetKind, course_id: String(target.courseId || target.course_id), lesson_id: String(target.lessonId || target.lesson_id), ...(targetExpected ? { expected_revision: strictRevision(targetExpected) } : {}) };
    const workspaceQuery = workspace ? `?copal_workspace=${encodeURIComponent(String(workspace))}` : '';
    return this._request(`/attachments/prepare${workspaceQuery}`, { method: 'POST', signal, body: { operation_id: operation, generation: currentGeneration, source: normalizedSource, target: normalizedTarget, mode: normalizedMode } }).then((payload) => {
      const prepared = strictPreparation(payload);
      if (prepared.operation_id !== operation || prepared.generation !== currentGeneration) throw new FilesFacadeError('Files attachment preparation response is stale');
      return prepared;
    });
  }

  attachmentPreparationReceipt(operationId, { signal = null, workspace = null } = {}) {
    const value = String(operationId || '').trim();
    if (!value || value.length > 128) throw new TypeError('operation id is invalid');
    const workspaceValue = workspace == null ? '' : String(workspace || '').trim();
    if (workspaceValue && !/^[A-Za-z0-9._-]{1,64}$/.test(workspaceValue)) throw new TypeError('Copal workspace is invalid');
    const suffix = workspaceValue ? `?copal_workspace=${encodeURIComponent(workspaceValue)}` : '';
    return this._request(`/attachments/${encodeURIComponent(value)}${suffix}`, { signal }).then((payload) => strictPreparationStatus(payload, value));
  }

  queryBaseResource({ baseRef, expectedRevision = null, corpusRef, generation, viewId = null, query = {}, page = 0, pageSize = 100, contextRef = null, draftDefinition = null } = {}, { signal = null } = {}) {
    const base = String(baseRef || '').trim();
    const corpus = String(corpusRef || '').trim();
    const currentGeneration = Number(generation);
    if (!base || !corpus || !Number.isSafeInteger(currentGeneration) || currentGeneration < 0 || !Number.isInteger(Number(page)) || Number(page) < 0 || !Number.isInteger(Number(pageSize)) || Number(pageSize) < 1 || Number(pageSize) > 500) throw new TypeError('Base query request is invalid');
    return this._request('/bases/query', { method: 'POST', signal, body: { base_ref: base, ...(expectedRevision ? { expected_revision: expectedRevision } : {}), corpus_ref: corpus, generation: currentGeneration, ...(viewId ? { view_id: String(viewId) } : {}), query: query && typeof query === 'object' ? { ...query } : {}, page: Number(page), page_size: Number(pageSize), ...(contextRef ? { context_ref: String(contextRef) } : {}), ...(draftDefinition != null ? { draft_definition: String(draftDefinition) } : {}) } });
  }

  contentUrl(resourceRef, { purpose = 'download' } = {}) {
    const value = String(resourceRef || '').trim();
    if (!value) throw new TypeError('resource reference is required');
    const normalizedPurpose = String(purpose || '').trim().toLowerCase();
    if (!['download', 'preview'].includes(normalizedPurpose)) throw new TypeError('content purpose is invalid');
    return `${this.baseUrl}/content/${encodeURIComponent(value)}?purpose=${normalizedPurpose}`;
  }

  thumbnailUrl(resourceRef, { width = 160, height = 160, scale = 1 } = {}) {
    const value = String(resourceRef || '').trim();
    if (!value) throw new TypeError('resource reference is required');
    const boundedWidth = Math.max(1, Math.min(1024, Math.round(Number(width) || 160)));
    const boundedHeight = Math.max(1, Math.min(1024, Math.round(Number(height) || 160)));
    const boundedScale = Math.max(1, Math.min(3, Number(scale) || 1));
    const query = new URLSearchParams({
      width: String(boundedWidth),
      height: String(boundedHeight),
      scale: String(boundedScale),
    });
    return `${this.baseUrl}/thumbnail/${encodeURIComponent(value)}?${query}`;
  }

  async *watch(resourceRef, { signal = null } = {}) {
    const value = String(resourceRef || '').trim();
    if (!value) throw new TypeError('resource reference is required');
    let response;
    try {
      response = await this.fetchImpl(`${this.baseUrl}/watch`, {
        method: 'POST',
        credentials: 'same-origin',
        signal,
        headers: {
          'Content-Type': 'application/json',
          Accept: 'text/event-stream',
        },
        body: JSON.stringify({ resource_ref: value }),
      });
    } catch (error) {
      if (error?.name === 'AbortError') throw error;
      throw new FilesFacadeError(error?.message || 'Files watch could not start');
    }
    if (!response.ok || !response.body?.getReader) {
      let payload = null;
      try { payload = await response.json(); } catch { payload = null; }
      const detail = payload?.detail;
      throw new FilesFacadeError(
        (detail && typeof detail === 'object' ? detail.message : detail)
          || payload?.message
          || `Files watch could not start (${response.status})`,
        {
          code: (detail && typeof detail === 'object' ? detail.code : null)
            || payload?.code
            || 'provider_unavailable',
          status: response.status,
        },
      );
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    try {
      while (true) {
        const { value: chunk, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(chunk, { stream: true }).replace(/\r\n/g, '\n');
        while (true) {
          const boundary = buffer.indexOf('\n\n');
          if (boundary < 0) break;
          const block = buffer.slice(0, boundary);
          buffer = buffer.slice(boundary + 2);
          if (!block || block.startsWith(':')) continue;
          const event = block.split('\n').find(line => line.startsWith('event:'))?.slice(6).trim();
          const data = block.split('\n').find(line => line.startsWith('data:'))?.slice(5).trim();
          if (event !== 'files-change' || !data) continue;
          let payload;
          try { payload = JSON.parse(data); } catch {
            throw new FilesFacadeError('Files watch returned an invalid event', { code: 'partial_stream' });
          }
          if (!payload || typeof payload.kind !== 'string' || !Number.isSafeInteger(Number(payload.sequence))) {
            throw new FilesFacadeError('Files watch returned an invalid event', { code: 'partial_stream' });
          }
          yield payload;
        }
      }
    } finally {
      try { await reader.cancel(); } catch { /* already closed */ }
    }
  }
}

export const filesFacadeClient = new FilesFacadeClient();
