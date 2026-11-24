import { metadataFormatId } from './editor/previewModel.js';

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
    || Object.keys(value).some((key) => !['operation_id', 'generation', 'preparation_receipt_id', 'source_revision', 'target_identity', 'target_revision', 'insertion', 'asset', 'source_digest', 'history', 'request_digest', 'asset_size', 'account_id', 'workspace_id', 'source_identity', 'mode', 'provenance'].includes(key))) throw new FilesFacadeError('Files attachment preparation response is invalid');
  if (typeof value.operation_id !== 'string' || !value.operation_id || value.operation_id.length > 128
    || !Number.isSafeInteger(value.generation) || value.generation < 0
    || typeof value.preparation_receipt_id !== 'string' || !value.preparation_receipt_id || value.preparation_receipt_id.length > 128) throw new FilesFacadeError('Files attachment preparation response is invalid');
  strictRevision(value.source_revision); strictRevision(value.target_revision);
  if (!value.target_identity || typeof value.target_identity !== 'object' || Array.isArray(value.target_identity)
    || Object.keys(value.target_identity).some((key) => !['resource_key', 'resource_ref', 'kind', 'course_id', 'lesson_id', 'assignment_id', 'root_ref', 'root_revision'].includes(key))) throw new FilesFacadeError('Files attachment preparation response is invalid');
  if ((value.target_identity.root_ref == null) !== (value.target_identity.root_revision == null)) throw new FilesFacadeError('Files attachment root identity is invalid');
  if (value.target_identity.root_ref != null) {
    if (value.target_identity.kind !== 'host_document' || typeof value.target_identity.root_ref !== 'string' || !value.target_identity.root_ref) throw new FilesFacadeError('Files attachment root identity is invalid');
    strictRevision(value.target_identity.root_revision);
  }
  if (typeof value.target_identity.kind !== 'string' || !value.target_identity.kind) throw new FilesFacadeError('Files attachment preparation response is invalid');
  if (value.target_identity.resource_key != null) strictResourceKey(value.target_identity.resource_key);
  if (value.target_identity.resource_ref != null && (typeof value.target_identity.resource_ref !== 'string' || !value.target_identity.resource_ref)) throw new FilesFacadeError('Files attachment preparation response is invalid');
  for (const key of ['course_id', 'lesson_id', 'assignment_id']) if (value.target_identity[key] != null && (typeof value.target_identity[key] !== 'string' || !value.target_identity[key] || value.target_identity[key].length > 512)) throw new FilesFacadeError('Files attachment preparation response is invalid');
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

function strictCodeGraphTarget(value, workspaceId, revision) {
  const object = (item) => item && typeof item === 'object' && !Array.isArray(item);
  const integer = (item, minimum = 0) => Number.isSafeInteger(item) && item >= minimum;
  const relative = (item) => typeof item === 'string' && item.length <= 4096
    && !/[\\\u0000]/.test(item) && !item.startsWith('/') && !/^[A-Za-z]:/.test(item)
    && (!item || item.split('/').every(part => part && part !== '.' && part !== '..'));
  const scope = value?.workspace;
  if (!object(value) || value.version !== 1 || !integer(value.generation)
    || Object.keys(value).some(key => !['version', 'generation', 'workspace', 'open_relative', 'scope_kind'].includes(key))
    || !['selected_folder', 'parent_folder', 'captured_workspace'].includes(value.scope_kind)
    || !relative(value.open_relative) || !object(scope)
    || Object.keys(scope).some(key => !['id', 'name', 'location_id', 'relative_folder', 'archived', 'generation', 'revision'].includes(key))
    || typeof scope.id !== 'string' || !scope.id.trim() || scope.id.length > 256
    || typeof scope.name !== 'string' || !scope.name.trim()
    || typeof scope.location_id !== 'string' || !scope.location_id.trim()
    || !relative(scope.relative_folder) || scope.archived !== false
    || !integer(scope.revision, 1) || !integer(scope.generation) || scope.generation > value.generation
    || (value.scope_kind === 'selected_folder' && value.open_relative !== '')
    || (value.scope_kind === 'parent_folder' && !value.open_relative)
    || (workspaceId == null ? value.scope_kind === 'captured_workspace'
      : value.scope_kind !== 'captured_workspace' || scope.id !== workspaceId || scope.revision !== revision)) {
    throw new FilesFacadeError('Code Graph target response is invalid', { code: 'invalid_graph_target' });
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
      || Object.keys(item).some((key) => !['item_id', 'outcome', 'code', 'resource_ref', 'resource_key', 'receipt_id', 'revision', 'provenance', 'history', 'cleanup', 'undo'].includes(key))
      || typeof item.item_id !== 'string' || !item.item_id || ids.has(item.item_id)
      || !['committed', 'unchanged', 'failed', 'denied', 'conflict', 'stale', 'pending'].includes(item.outcome)) throw new FilesFacadeError('Files operation receipt is invalid');
    for (const key of ['code', 'resource_ref', 'resource_key', 'receipt_id']) if (item[key] != null && (typeof item[key] !== 'string' || !item[key] || item[key].length > 16384)) throw new FilesFacadeError('Files operation receipt is invalid');
    if (item.revision != null) strictRevision(item.revision);
    if (item.cleanup != null && (!['pending', 'complete'].includes(item.cleanup) || item.outcome !== 'committed')) throw new FilesFacadeError('Files cleanup status is invalid');
    if (item.undo != null && (item.undo !== 'available' || item.outcome !== 'committed')) throw new FilesFacadeError('Files undo status is invalid');
    for (const key of ['provenance', 'history']) if (item[key] != null && (typeof item[key] !== 'object' || Array.isArray(item[key]))) throw new FilesFacadeError('Files operation receipt is invalid');
    ids.add(item.item_id);
  }
  if (value.state === 'complete' && value.items.some((item) => !['committed', 'unchanged'].includes(item.outcome))) throw new FilesFacadeError('Files operation receipt is invalid');
  return value;
}

// Which roots a move must ask for, in order, from the root-requirement reply.
export function transferRootsToAsk(requirement) {
  return [...(requirement?.root_required ? ['managed'] : []), ...(requirement?.destination_root_required ? ['destination'] : [])];
}

export class FilesFacadeClient {
  constructor({ baseUrl = '/api/files-v1', fetchImpl = globalThis.fetch } = {}) {
    if (typeof fetchImpl !== 'function') throw new TypeError('fetch implementation is required');
    this.baseUrl = String(baseUrl || '').replace(/\/$/, '');
    this.fetchImpl = fetchImpl.bind(globalThis);
  }

  previewSelectedOutputs(body, {signal = null} = {}) { return this._request('/selected-output/preview', {method:'POST',body,signal}); }
  async exportSelectedOutputs(previewToken, firstRef, {signal = null} = {}) {
    const response=await this.fetchImpl(`${this.baseUrl}/selected-output/export`,{method:'POST',credentials:'same-origin',cache:'no-store',headers:{'Content-Type':'application/json'},body:JSON.stringify({previewToken,firstRef}),signal});
    if(!response.ok)throw new FilesFacadeError('Reviewed export unavailable or changed');
    return await response.blob();
  }

  async previewCheckpointRestore(body, {signal = null} = {}) {
    return this._request('/checkpoint/restore-preview', {method:'POST', body, signal});
  }

  async confirmCheckpointRestore(body, {signal = null} = {}) {
    return this._request('/checkpoint/restore-confirm', {method:'POST', body, signal});
  }

  async checkpointRestoreStatus(body, {signal = null} = {}) {
    return this._request('/checkpoint/restore-status', {method:'POST', body, signal});
  }

  async compareCheckpoint(body, { signal = null } = {}) {
    const value=await this._request('/checkpoint/compare',{method:'POST',body,signal});
    if (!value || value.version!==1 || value.coverage!=='one_selected_checkpoint_file' || !value.checkpoint || !value.current) throw new FilesFacadeError('Checkpoint comparison is unavailable');
    return value;
  }

  async checkpointCapabilities(body, { signal = null } = {}) {
    const value = await this._request('/checkpoint/capabilities', { method:'POST', body, signal });
    if (!value || typeof value.supported !== 'boolean' || Object.keys(value).join(',') !== 'supported') throw new FilesFacadeError('Checkpoint support is unavailable');
    return value;
  }

  async checkpointCapture(body, { signal = null } = {}) {
    const value = await this._request('/checkpoint/capture', { method:'POST', body, signal });
    if (!value || typeof value !== 'object' || !['pending','failed','complete'].includes(value.state) || !Array.isArray(value.items) || value.items.length < 1 || value.items.length > 16)
      throw new FilesFacadeError('Checkpoint status is unavailable');
    return value;
  }

  async checkpointRegistration(body, { signal = null } = {}) {
    const value = await this._request('/checkpoint/registration', { method:'POST', body, signal });
    if (!value || typeof value !== 'object' || !['pending','complete','partial'].includes(value.state) || !Array.isArray(value.items) || value.items.length < 1 || value.items.length > 16)
      throw new FilesFacadeError('History registration status is unavailable');
    return value;
  }

  async _request(path, { method = 'GET', body = null, signal = null } = {}) {
    const listingRead = path === '/children' || path.startsWith('/roots?')
      || (method === 'GET' && (path === '/places' || path.startsWith('/workspaces?')));
    const boundedRead = listingRead;
    const requestSignal = boundedRead ? AbortSignal.any([...(signal ? [signal] : []),AbortSignal.timeout(12_000)]) : signal;
    let response;
    try {
      response = await this.fetchImpl(`${this.baseUrl}${path}`, {
        method,
        credentials: 'same-origin',
        ...((path.startsWith('/media/') || path.startsWith('/analysis/')) ? { cache:'no-store' } : {}),
        signal: requestSignal,
        ...(body == null ? {} : {
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        }),
      });
    } catch (error) {
      if (error?.name === 'AbortError') throw error;
      const code = boundedRead ? error?.name === 'TimeoutError' ? 'read_timeout' : 'transport_unavailable' : 'provider_unavailable';
      throw new FilesFacadeError(error?.message || 'Files provider request failed', {code});
    }
    let payload = null;
    try { payload = await response.json(); } catch (error) {
      if (error?.name === 'AbortError' || signal?.aborted) throw error;
      if (boundedRead && error?.name === 'TimeoutError') throw new FilesFacadeError('Files read timed out', {code:'read_timeout'});
    }
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
    if (listingRead && !Array.isArray(payload?.entries)) {
      throw new FilesFacadeError('Files returned an incomplete listing', {code:'invalid_read'});
    }
    return payload;
  }

  roots({ copalWorkspace = 'default', signal = null } = {}) {
    const workspace = String(copalWorkspace || 'default').trim();
    if (!/^[A-Za-z0-9._-]{1,64}$/.test(workspace)) throw new TypeError('Document workspace is invalid');
    return this._request(`/roots?copal_workspace=${encodeURIComponent(workspace)}`, { signal });
  }

  previewAnalysisProject(body, { signal = null } = {}) {
    return this._request('/analysis/projects/preview', { method:'POST', body, signal });
  }

  initializeAnalysisProject(body, { signal = null } = {}) {
    return this._request('/analysis/projects/initialize', { method:'POST', body, signal });
  }

  saveAnalysisObservation(body, { signal = null } = {}) {
    return this._request('/analysis/projects/observation', { method:'POST', body, signal });
  }

  analysisCandidates(body, { signal = null } = {}) { return this._request('/analysis/results/candidates', {method:'POST',body,signal}); }
  completeAnalysisResult(body, { signal = null } = {}) { return this._request('/analysis/results/complete', {method:'POST',body,signal}); }
  readAnalysisDossier(body, { signal = null } = {}) { return this._request('/analysis/results/dossier/read', {method:'POST',body,signal}); }
  inspectAnalysisFunction(body, { signal = null } = {}) { return this._request('/analysis/results/dossier/function', {method:'POST',body,signal}); }
  staticAnalysisOverlay(body, { signal = null } = {}) { return this._request('/analysis/results/dossier/static-overlay', {method:'POST',body,signal}); }
  saveAnalysisIngest(body, { signal = null } = {}) { return this._request('/analysis/ingest/save', {method:'POST',body,signal}); }
  analysisIngestOverlay(body, { signal = null } = {}) { return this._request('/analysis/ingest/overlay', {method:'POST',body,signal}); }
  readAnalysisFunctionExport(body, { signal = null } = {}) { return this._request('/analysis/results/functions/read', {method:'POST',body,signal}); }
  exportAnalysisFunctions(body, { signal = null } = {}) { return this._request('/analysis/results/functions/export', {method:'POST',body,signal}); }
  saveAnalysisElfDossier(body, { signal = null } = {}) { return this._request('/analysis/results/dossier/elf', {method:'POST',body,signal}); }
  saveAnalysisDossier(body, { signal = null } = {}) { return this._request('/analysis/results/dossier/save', {method:'POST',body,signal}); }
  analysisSnapshotCapabilities({ signal = null } = {}) { return this._request('/analysis/snapshots/capabilities', {signal}); }
  previewAnalysisSnapshot(body, { signal = null } = {}) { return this._request('/analysis/snapshots/preview', {method:'POST',body,signal}); }

  compareAnalysisSnapshot(body, { signal = null } = {}) { return this._request('/analysis/snapshots/compare', {method:'POST',body,signal}); }

  inspectMediaRecipe(body, { signal = null } = {}) {
    return this._request('/media/inspect', { method:'POST', body, signal });
  }

  mediaProfiles(body, { signal = null } = {}) {
    return this._request('/media/profiles', { method:'POST', body, signal });
  }

  prepareMediaOperation(body, { signal = null } = {}) {
    return this._request('/media/operation/prepare', { method:'POST', body, signal });
  }

  prepareMedia(body, { signal = null } = {}) {
    return this._request('/media/prepare', { method:'POST', body, signal });
  }

  /** One operation-keyed call for the retained local Blender operations; request shapes differ only by path. */
  prepareLocalOperation(operation, body, { signal = null, inspect = false } = {}) {
    const paths = { 'material.bake': '/media/material/bake/prepare', 'blender.render': '/media/blender/prepare',
      'blender.four_headings': '/media/blender/four-headings/prepare', 'blender.rig_pose': inspect ? '/media/blender/rig/inspect' : '/media/blender/rig/prepare' };
    if (!Object.hasOwn(paths, operation) || (inspect && operation !== 'blender.rig_pose')) throw new Error('Unsupported media operation.');
    return this._request(paths[operation], { method:'POST', body, signal });
  }
  prepareSpriteSheet(body, { signal = null } = {}) {
    return this._request('/media/sprite-sheet/prepare', { method:'POST', body, signal });
  }
  prepareSpriteCrops(body, { signal = null } = {}) {
    return this._request('/media/sprite-sheet/crops/prepare', { method:'POST', body, signal });
  }
  bindSpriteSheetProject(jobID, { signal = null } = {}) {
    return this._request('/media/sprite-sheet/project', { method:'POST', body:{jobID}, signal });
  }

  latexReadiness({ signal = null } = {}) {
    return this._request('/media/latex/readiness', { method:'GET', signal });
  }

  prepareLatex(body, { signal = null } = {}) {
    return this._request('/media/latex/prepare', { method:'POST', body, signal });
  }

  recordingAudioReadiness({ signal = null } = {}) {
    return this._request('/media/recording/audio-readiness', { signal });
  }

  selectRecordingAudio(body, { signal = null } = {}) {
    return this._request('/media/recording/audio-select', { method:'POST', body, signal });
  }

  prepareRecording(body, { signal = null } = {}) {
    return this._request('/media/recording/prepare', { method:'POST', body, signal });
  }

  previewRecordingPartial(body, { signal = null } = {}) {
    return this._request('/media/recording/partial-preview', { method:'POST', body, signal });
  }

  saveRecordingPartial(body, { signal = null } = {}) {
    return this._request('/media/recording/partial-save', { method:'POST', body, signal });
  }

  stopRecording(body, { signal = null } = {}) {
    return this._request('/media/recording/stop', { method:'POST', body, signal });
  }

  runMedia(body, { signal = null } = {}) {
    return this._request('/media/run', { method:'POST', body, signal });
  }

  mediaAction(action, jobID, { signal = null } = {}) {
    if (!['status','cancel','reconcile','collect'].includes(action)) throw new TypeError('invalid media action');
    return this._request(`/media/${action}`, { method:'POST', body:{jobID}, signal });
  }

  openRecordedMedia(jobID, { signal = null } = {}) {
    return this._request('/media/recording/open', {method:'POST', body:{jobID}, signal});
  }

  mediaJobs(after = '', { signal = null } = {}) {
    return this._request(`/media/jobs?after=${encodeURIComponent(after)}&limit=50`, { signal });
  }

  previewMediaRecovery(body, { signal = null } = {}) {
    return this._request('/media/recovery/preview', { method:'POST', body, signal });
  }

  saveMediaRecovery(body, { signal = null } = {}) {
    return this._request('/media/recovery/save', { method:'POST', body, signal });
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

  stat(resourceRef, { signal = null, purpose = null } = {}) {
    return this._request('/stat', {
      method: 'POST',
      signal,
      body: { resource_ref: String(resourceRef || ''), ...(['inspect','inspect_elf','inspect_pe','inspect_macho','inspect_sqlite','snapshot','checkpoint'].includes(purpose) ? {purpose} : {}) },
    });
  }

  inspectModelHeader(resourceRef, expectedRevision, { signal = null, format = 'safetensors' } = {}) {
    if (!metadataFormatId(format)) throw new TypeError('Inspection format is unavailable');
    return this._request(`/inspect/${format}`, { method:'POST', signal,
      body:{ resource_ref:String(resourceRef || ''), expected_revision:expectedRevision } });
  }

  reveal(resourceRef, { signal = null, expectedRevision = null } = {}) {
    const value = String(resourceRef || '').trim();
    if (!value) throw new TypeError('resource reference is required');
    return this._request('/reveal', {
      method: 'POST', signal,
      body: { resource_ref: value, ...(expectedRevision ? {expected_revision: expectedRevision} : {}) },
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
    if (!['all', 'host', 'copal', 'files', 'library'].includes(scope)) {
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
    if (!/^[A-Za-z0-9._-]{1,64}$/.test(workspace)) throw new TypeError('Document workspace is invalid');
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

  open(resourceRef, { signal = null, purpose = null } = {}) {
    return this._request('/action', {
      method: 'POST',
      signal,
      body: {
        resource_ref: String(resourceRef || ''),
        action: 'open',
        args: purpose === 'inspect' ? {purpose} : {},
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

  codeGraphTarget(resourceRef, options = {}) {
    // Navigation only. Do not reuse workspace(), which intentionally adopts
    // loose media for the separate "Use as workspace" workflow.
    if (!options || typeof options !== 'object' || Array.isArray(options)
      || Object.keys(options).some(key => !['workspaceId', 'expectedWorkspaceRevision', 'signal'].includes(key))) {
      throw new TypeError('Code Graph target options are invalid');
    }
    const { workspaceId = null, expectedWorkspaceRevision = null, signal = null } = options;
    if (typeof resourceRef !== 'string' || resourceRef.length < 8 || resourceRef.length > 16384 || resourceRef.includes('\u0000')) {
      throw new TypeError('resource reference is invalid');
    }
    if ((workspaceId == null) !== (expectedWorkspaceRevision == null)
      || (workspaceId != null && (typeof workspaceId !== 'string' || !workspaceId.trim() || workspaceId.length > 256
        || !Number.isSafeInteger(expectedWorkspaceRevision) || expectedWorkspaceRevision < 1))) {
      throw new TypeError('Code Graph Workspace capture is invalid');
    }
    return this._request('/code-graph-target', {
      method: 'POST', signal, body: {
        resource_ref: resourceRef,
        ...(workspaceId == null ? {} : { workspace_id: workspaceId, expected_workspace_revision: expectedWorkspaceRevision }),
      },
    }).then(value => strictCodeGraphTarget(value, workspaceId, expectedWorkspaceRevision));
  }

  workspaces({ includeArchived = false, signal = null } = {}) {
    return this._request(`/workspaces?include_archived=${includeArchived ? 'true' : 'false'}`, { signal });
  }

  async previewSelectedHistory(body, {signal = null} = {}) {
    let response;
    try { response=await this.fetchImpl('/api/session-transfer/preview',{method:'POST',credentials:'same-origin',cache:'no-store',signal,headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}); }
    catch(error) { if(error?.name==='AbortError')throw error;throw new FilesFacadeError('Selected history is unavailable'); }
    let value;try{value=await response.json();}catch{throw new FilesFacadeError('Selected history is unavailable');}
    if(!response.ok && !(response.status===409 && value?.available===false))throw new FilesFacadeError('Selected history is unavailable',{status:response.status});
    return value;
  }

  async selectedHistoryImport(body, {signal = null} = {}) {
    return this._selectedHistoryResult('/api/session-transfer/import',body,signal);
  }

  async reconcileSelectedHistory(selector, {signal = null} = {}) {
    return this._selectedHistoryResult('/api/session-transfer/reconcile',{selector},signal);
  }

  async _selectedHistoryResult(path,body,signal) {
    let response;
    try {response=await this.fetchImpl(path,{method:'POST',credentials:'same-origin',cache:'no-store',signal,headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});}
    catch(error){if(error?.name==='AbortError')throw error;throw new FilesFacadeError('Selected import outcome is unavailable');}
    let value;try{value=await response.json();}catch{throw new FilesFacadeError('Selected import outcome is unavailable',{status:response.status});}
    if(!response.ok && !(response.status===409 && value?.version===1 && ['unknown','unavailable','conflict'].includes(value.state)))throw new FilesFacadeError('Selected import outcome is unavailable',{status:response.status});
    return value;
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

  workspaceCaptureDirectory(workspaceId, layout, { signal = null } = {}) {
    const identifier = String(workspaceId || '').trim();
    if (!identifier || identifier.length > 256 || !['movs', 'pics'].includes(layout)) throw new TypeError('capture layout is invalid');
    return this._request('/workspace-resource', { method:'POST', signal, body:{workspace_id:identifier, capture_layout:layout} });
  }

  actionRootRequirement({generation, resourceRef, name, expectedRevision = null} = {}, {signal = null} = {}) {
    if (!Number.isSafeInteger(Number(generation)) || Number(generation) < 0 || !resourceRef || !name) throw new TypeError('Rename preflight is invalid');
    return this._request('/action/root-requirement', {method:'POST', signal, body:{generation:Number(generation), resource_ref:String(resourceRef), name:String(name), ...(expectedRevision ? {expected_revision:strictRevision(expectedRevision)} : {})}}).then(result => {
      if (!result || typeof result.root_required !== 'boolean' || Object.keys(result).some(key=>key !== 'root_required')) throw new Error('Rename preflight is unavailable.');
      return result;
    });
  }

  action(resourceRef, action, args = {}, { signal = null, actionId = null, expectedRevision = null, managedRoot = null } = {}) {
    const normalized = String(action || '').trim().toLowerCase();
    if (!['favorite.set', 'archive.set', 'rename', 'move', 'trash', 'restore'].includes(normalized)) {
      throw new TypeError('resource action is unsupported');
    }
    if (['favorite.set', 'archive.set'].includes(normalized) && (!args || typeof args.value !== 'boolean' || Object.keys(args).length !== 1)) {
      throw new TypeError('resource action requires one boolean value');
    }
    if (['rename', 'move'].includes(normalized) && (!args || typeof args.name !== 'string' || !args.name.trim() || Object.keys(args).length !== 1)) throw new TypeError('resource action requires one name');
    if (normalized === 'trash' && Object.keys(args || {}).length) throw new TypeError('resource action accepts no arguments');
    // restore takes nothing (Copal) or exactly one Host trash entry returned by a trash action.
    if (normalized === 'restore' && Object.keys(args || {}).length) {
      const entry = args.entry;
      const keys = ['fingerprint', 'id', 'original_path', 'root_id', 'trashed_path'];
      if (Object.keys(args).length !== 1 || !entry || typeof entry !== 'object' || Object.keys(entry).sort().join() !== keys.join()
        || keys.some((key) => typeof entry[key] !== 'string' || !entry[key])) throw new TypeError('restore accepts one trash entry');
    }
    const payloadArgs = ['favorite.set', 'archive.set'].includes(normalized) ? { value: args.value } : { ...args };
    return this._request('/action', {
      method: 'POST',
      signal,
      body: {
        resource_ref: String(resourceRef || ''),
        action: normalized,
        ...(actionId ? { action_id: String(actionId) } : {}),
        ...(expectedRevision ? { expected_revision:strictRevision(expectedRevision) } : {}),
        ...(managedRoot ? { managed_root:{resource_ref:String(managedRoot.ref || ''), ...(managedRoot.revision ? {expected_revision:strictRevision(managedRoot.revision)} : {})} } : {}),
        args: payloadArgs,
      },
    });
  }

  openResource(resourceRef, { signal = null, expectedRevision = null, purpose = null } = {}) {
    return this._request('/open-resource', {
      method: 'POST',
      signal,
      body: { resource_ref: String(resourceRef || ''), ...(expectedRevision ? { expected_revision:expectedRevision } : {}), ...(purpose === 'inspect' ? {purpose} : {}) },
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

  createResource(parentRef, { name, text = '', actionId = null, operationId = null, expectedParentRevision = null, statusOnly = false, signal = null } = {}) {
    const parent = String(parentRef || '').trim();
    const filename = String(name || '').trim();
    if (!parent) throw new TypeError('parent resource reference is required');
    if (!filename || filename.length > 240 || filename.includes('/') || filename.includes('\\')) {
      throw new TypeError('resource name is invalid');
    }
    const ordinary = operationId !== null || expectedParentRevision !== null || statusOnly;
    if (!ordinary && !/\.(?:md|markdown)$/i.test(filename)) throw new TypeError('resource name must be Markdown');
    if (ordinary && (!/\.(?:md|markdown|html|svg)$/i.test(filename) || typeof operationId !== 'string' || !operationId.trim()
        || !expectedParentRevision || expectedParentRevision.kind !== 'hostDirectoryRevision'
        || !/^sha256:[a-f0-9]{64}$/.test(expectedParentRevision.value || ''))) throw new TypeError('ordinary create identity is invalid');
    return this._request('/create', {
      method: 'POST', signal,
      body: { parent_ref: parent, name: filename, text: String(text ?? ''), ...(actionId ? { action_id: String(actionId) } : {}),
        ...(ordinary ? { operation_id: operationId, expected_parent_revision: {kind:'hostDirectoryRevision',value:expectedParentRevision.value}, status_only: !!statusOnly } : {}) },
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
    if (!['copal','files'].includes(key.provider) || !key.resource_id || Object.keys(key).some((item) => !['provider', 'resource_id', 'account_id', 'workspace_id'].includes(item)) || (key.provider === 'files' && (!/^image:[A-Za-z0-9._-]{1,240}$/.test(key.resource_id) || !key.account_id || key.workspace_id != null))) {
      throw new TypeError('resource key is invalid');
    }
    return this._request('/resolve-resource', {
      method: 'POST', signal, body: { resource_key: key },
    });
  }

  binders({ signal = null } = {}) { return this._request('/binders', {signal}); }
  resolveBinder(binderId, catalogRevision, descriptor, {signal = null, openHome = false} = {}) {
    if (typeof binderId!=='string' || !binderId || !Number.isSafeInteger(catalogRevision) || !descriptor || descriptor.id!==binderId || !Number.isSafeInteger(descriptor.revision)) throw new TypeError('Binder identity is invalid');
    return this._request('/binders/resolve',{method:'POST',signal,body:{open_home:openHome,binder_id:binderId,expected_catalog_revision:catalogRevision,expected_descriptor_revision:descriptor.revision,expected_descriptor:descriptor}});
  }
  previewBinder({sourceRef, untouchedRef, accountId, catalogRevision, binderId, displayName}, {signal = null} = {}) {
    return this._request('/binders/preview', {method:'POST', signal, body:{source_ref:sourceRef, untouched_ref:untouchedRef, expected_account_id:accountId, expected_catalog_revision:catalogRevision, binder_id:binderId, display_name:displayName}});
  }
  applyBinderConversion(body, {signal = null} = {}) {
    return this._request('/binders/conversions', {method:'POST', body, signal});
  }
  binderConversionReceipt(operationId, {signal = null} = {}) {
    return this._request(`/binders/conversions/${encodeURIComponent(operationId)}`, {signal});
  }
  configureBinder({resourceRef,name,accountId,catalogRevision,makeDefault=false}, {signal = null} = {}) {
    if (typeof resourceRef!=='string' || !resourceRef || typeof name!=='string' || !name.trim() || typeof accountId!=='string' || !accountId || !Number.isSafeInteger(catalogRevision) || typeof makeDefault!=='boolean') throw new TypeError('Binder configuration is invalid');
    return this._request('/binders/configure',{method:'POST',signal,body:{resource_ref:resourceRef,name,expected_account_id:accountId,expected_catalog_revision:catalogRevision,make_default:makeDefault}});
  }

  transferRootRequirement({ generation, sources, destinationRef } = {}, { signal = null } = {}) {
    const currentGeneration = Number(generation);
    if (!Number.isSafeInteger(currentGeneration) || currentGeneration < 0 || !Array.isArray(sources) || !sources.length || sources.length > 200 || typeof destinationRef !== 'string' || !destinationRef) throw new TypeError('Transfer preflight is invalid');
    const normalized = sources.map(source => ({ item_id:String(source?.itemId || ''), resource_ref:String(source?.resourceRef || ''), ...(source?.expectedRevision ? { expected_revision:strictRevision(source.expectedRevision) } : {}) }));
    if (normalized.some(source => !source.item_id || !source.resource_ref)) throw new TypeError('Transfer source is invalid');
    return this._request('/transfers/root-requirement', {method:'POST', signal, body:{generation:currentGeneration, sources:normalized, destination_ref:destinationRef}}).then(result => {
      // The one-key reply is an older server: no destination root is needed there.
      if (!result || typeof result.root_required !== 'boolean' || Object.keys(result).some(key => !['root_required', 'destination_root_required'].includes(key))
        || ('destination_root_required' in result && typeof result.destination_root_required !== 'boolean')) throw new Error('Transfer preflight is unavailable.');
      return { root_required: result.root_required, destination_root_required: result.destination_root_required === true };
    });
  }

  transferResources({ operationId, generation, kind, sources, destinationRef, collision = 'fail', managedRoot = null, destinationRoot = null } = {}, { signal = null } = {}) {
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
      body: { operation_id: operation, generation: currentGeneration, kind: normalizedKind, sources: normalizedSources, destination_ref: destination, collision: normalizedCollision,
        ...(managedRoot ? {managed_root:{resource_ref:String(managedRoot.ref || ''), ...(managedRoot.revision ? {expected_revision:strictRevision(managedRoot.revision)} : {})}} : {}),
        ...(destinationRoot ? {destination_root:{resource_ref:String(destinationRoot.ref || ''), ...(destinationRoot.revision ? {expected_revision:strictRevision(destinationRoot.revision)} : {})}} : {}) },
    }).then((payload) => strictOperationReceipt(payload, operation, currentGeneration));
  }

  operationReceipt(operationId, { signal = null, generation = null } = {}) {
    const value = String(operationId || '').trim();
    if (!value || value.length > 128) throw new TypeError('operation id is invalid');
    const expectedGeneration = generation == null ? null : Number(generation);
    if (expectedGeneration != null && (!Number.isSafeInteger(expectedGeneration) || expectedGeneration < 0)) throw new TypeError('operation generation is invalid');
    return this._request(`/operations/${encodeURIComponent(value)}`, { signal }).then((payload) => strictOperationReceipt(payload, value, expectedGeneration));
  }

  finishFolderCleanup(operationId, itemId, { signal = null, managedRoot = null } = {}) {
    const operation = String(operationId || '').trim();
    const item = String(itemId || '').trim();
    if (!operation || operation.length > 128 || !item || item.length > 128) throw new TypeError('cleanup identity is invalid');
    const body = managedRoot ? {resource_ref:String(managedRoot.ref || ''), expected_revision:strictRevision(managedRoot.revision)} : undefined;
    return this._request(`/operations/${encodeURIComponent(operation)}/folder-cleanup?item_id=${encodeURIComponent(item)}`, { method:'POST', signal, ...(body ? {body} : {}) })
      .then((value) => {
        if (!value || typeof value !== 'object' || Array.isArray(value)
          || Object.keys(value).some((key) => !['operation_id', 'item_id', 'outcome', 'cleanup'].includes(key))
          || value.operation_id !== operation || value.item_id !== item || value.outcome !== 'committed'
          || !['pending', 'complete'].includes(value.cleanup)) throw new FilesFacadeError('Files cleanup response is invalid');
        return value;
      });
  }

  /** Undo one completed cross-root file move. Idempotent: calling it again is the same undo (read back as committed or pending). */
  undoMove(operationId, itemId, { signal = null } = {}) {
    const operation = String(operationId || '').trim();
    const item = String(itemId || '').trim();
    if (!operation || operation.length > 128 || !item || item.length > 128) throw new TypeError('undo identity is invalid');
    return this._request(`/operations/${encodeURIComponent(operation)}/undo-move?item_id=${encodeURIComponent(item)}`, { method:'POST', signal })
      .then((value) => {
        if (!value || typeof value !== 'object' || Array.isArray(value) || Object.keys(value).some((key) => !['operation_id', 'item_id', 'undo'].includes(key))
          || value.operation_id !== operation || value.item_id !== item || !['committed', 'pending'].includes(value.undo)) throw new FilesFacadeError('Files undo response is invalid');
        return value;
      });
  }

  saveIwiPngCopy(request, { signal = null } = {}) {
    return this._request('/iwi-png-copy', { method:'POST', signal, body:request })
      .then(value => strictOperationReceipt(value, request.operation_id, request.generation));
  }

  async importFile(file, { operationId, itemId, generation, destinationRef, name, relativePath = '', collision = 'fail', videoFrameSource = null, signal = null } = {}) {
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
    if (videoFrameSource != null && (!videoFrameSource || Object.keys(videoFrameSource).sort().join()!=='expectedRevision,kind,sourceRef,timeHintMs,version' || videoFrameSource.version!==1 || videoFrameSource.kind!=='video_frame_copy' || typeof videoFrameSource.sourceRef!=='string' || !videoFrameSource.sourceRef || !videoFrameSource.expectedRevision || !Number.isSafeInteger(videoFrameSource.timeHintMs) || videoFrameSource.timeHintMs<0 || videoFrameSource.timeHintMs>86400000)) throw new TypeError('Video frame source is invalid');
    const form = new FormData();
    form.append('file', file, filename);
    form.append('metadata', JSON.stringify({ operation_id: operation, item_id: item, generation: currentGeneration, destination_ref: destination, name: filename, relative_path: relative, collision: String(collision || 'fail').toLowerCase(), ...(videoFrameSource ? {videoFrameSource} : {}) }));
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
    if (targetKind === 'copal_document' || targetKind === 'host_document') {
      const allowed=['kind', 'resourceRef', 'resource_ref', 'expectedRevision', 'expected_revision', ...(targetKind === 'host_document' ? ['rootRef','root_ref','expectedRootRevision','expected_root_revision'] : [])];
      if (!targetRef || targetKeys.some((key) => !allowed.includes(key))) throw new TypeError('attachment target is invalid');
    } else if (targetKind === 'treehouse_submission') {
      if (!target.courseId || !target.assignmentId || targetKeys.some(key => !['kind','courseId','assignmentId','expectedRevision'].includes(key))) throw new TypeError('submission attachment target is invalid');
    } else if (targetKind === 'treehouse_lesson') {
      if (!target.courseId && !target.course_id || !target.lessonId && !target.lesson_id || targetKeys.some((key) => !['kind', 'courseId', 'course_id', 'lessonId', 'lesson_id', 'expectedRevision', 'expected_revision'].includes(key))) throw new TypeError('attachment target is invalid');
    } else throw new TypeError('attachment target is invalid');
    const rootRef=target.rootRef || target.root_ref, rootRevision=target.expectedRootRevision || target.expected_root_revision;
    if (Boolean(rootRef) !== Boolean(rootRevision) || (rootRef && (!targetExpected || targetKind !== 'host_document'))) throw new TypeError('attachment root is invalid');
    const normalizedTarget = ['copal_document','host_document'].includes(targetKind)
      ? { kind: targetKind, resource_ref: String(targetRef), ...(targetExpected ? { expected_revision: strictRevision(targetExpected) } : {}), ...(rootRef ? {root_ref:String(rootRef),expected_root_revision:strictRevision(rootRevision)} : {}) }
      : targetKind === 'treehouse_submission' ? {kind:targetKind,course_id:String(target.courseId),assignment_id:String(target.assignmentId),...(targetExpected?{expected_revision:strictRevision(targetExpected)}:{})} : { kind: targetKind, course_id: String(target.courseId || target.course_id), lesson_id: String(target.lessonId || target.lesson_id), ...(targetExpected ? { expected_revision: strictRevision(targetExpected) } : {}) };
    const workspaceQuery = workspace ? `?copal_workspace=${encodeURIComponent(String(workspace))}` : '';
    return this._request(`/attachments/prepare${workspaceQuery}`, { method: 'POST', signal, body: { operation_id: operation, generation: currentGeneration, source: normalizedSource, target: normalizedTarget, mode: normalizedMode } }).then((payload) => {
      const prepared = strictPreparation(payload);
      if (prepared.operation_id !== operation || prepared.generation !== currentGeneration) throw new FilesFacadeError('Files attachment preparation response is stale');
      if (rootRef && (prepared.target_identity.root_ref !== String(rootRef) || prepared.target_identity.root_revision?.kind !== strictRevision(rootRevision).kind || prepared.target_identity.root_revision?.value !== strictRevision(rootRevision).value)) throw new FilesFacadeError('Files attachment root changed');
      return prepared;
    });
  }

  async prepareAttachmentUpload({operationId, generation, file, target, mode, workspace = null} = {}, {signal = null} = {}) {
    const operation = String(operationId || '').trim(), currentGeneration = Number(generation);
    if (!operation || operation.length > 128 || !Number.isSafeInteger(currentGeneration) || currentGeneration < 0
        || !file || !Number.isSafeInteger(file.size) || file.size < 1 || file.size > 10 * 1024 * 1024
        || typeof file.arrayBuffer !== 'function' || target?.kind !== 'host_document' || !target.resource_ref
        || !target.expected_revision || !['link','embed'].includes(mode)) throw new TypeError('Host attachment upload is invalid');
    const name=String(file.name || ''), mime=String(file.type || 'application/octet-stream');
    if (!name || name.length > 240 || /[\/\\\u0000-\u001f\u007f]/u.test(name) || name === '.' || name === '..' || mime.length > 128 || /[\u0000-\u001f\u007f]/u.test(mime)) throw new TypeError('Host attachment upload is invalid');
    const bytes=await file.arrayBuffer();
    if (bytes.byteLength !== file.size) throw new TypeError('Host attachment upload changed');
    const digest=[...new Uint8Array(await crypto.subtle.digest('SHA-256',bytes))].map(b=>b.toString(16).padStart(2,'0')).join('');
    const metadata={operation_id:operation,generation:currentGeneration,target,mode,name,mime};
    const encoded=new TextEncoder().encode(JSON.stringify(metadata));
    let binary=''; for (const byte of encoded) binary+=String.fromCharCode(byte);
    const header=btoa(binary).replace(/\+/g,'-').replace(/\//g,'_').replace(/=+$/,'');
    if (header.length > 65536) throw new TypeError('Host attachment metadata is too large');
    let response;
    try { response=await this.fetchImpl(`${this.baseUrl}/attachments/prepare-upload${workspace ? `?copal_workspace=${encodeURIComponent(String(workspace))}` : ''}`,
      {method:'POST',credentials:'same-origin',cache:'no-store',signal,headers:{'Content-Type':'application/octet-stream','X-OpenClank-Attachment':header},body:bytes}); }
    catch(error) { if(error?.name === 'AbortError') throw error; throw new FilesFacadeError(error?.message || 'Files provider request failed'); }
    let payload=null; try {payload=await response.json();} catch {}
    if (!response.ok) { const detail=payload?.detail; throw new FilesFacadeError(String((detail && typeof detail==='object' ? detail.message : detail) || 'Host attachment unavailable'), {code:String(detail?.code || 'provider_unavailable'),status:response.status}); }
    const prepared=strictPreparation(payload), source=prepared.source_identity;
    if (prepared.operation_id !== operation || prepared.generation !== currentGeneration || source?.kind !== 'client_upload'
        || source.sha256 !== digest || source.size !== bytes.byteLength || source.name !== name || source.mime !== mime
        || prepared.target_identity?.resource_ref !== target.resource_ref
        || (target.root_ref && (prepared.target_identity?.root_ref !== target.root_ref || prepared.target_identity?.root_revision?.kind !== target.expected_root_revision?.kind || prepared.target_identity?.root_revision?.value !== target.expected_root_revision?.value))) throw new FilesFacadeError('Host attachment receipt changed');
    return prepared;
  }

  attachmentPreparationReceipt(operationId, { signal = null, workspace = null } = {}) {
    const value = String(operationId || '').trim();
    if (!value || value.length > 128) throw new TypeError('operation id is invalid');
    const workspaceValue = workspace == null ? '' : String(workspace || '').trim();
    if (workspaceValue && !/^[A-Za-z0-9._-]{1,64}$/.test(workspaceValue)) throw new TypeError('Document workspace is invalid');
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

  contentUrl(resourceRef, { purpose = 'download', expectedRevision = null } = {}) {
    const value = String(resourceRef || '').trim();
    if (!value) throw new TypeError('resource reference is required');
    const normalizedPurpose = String(purpose || '').trim().toLowerCase();
    if (!['download', 'preview'].includes(normalizedPurpose)) throw new TypeError('content purpose is invalid');
    const query = new URLSearchParams({ purpose:normalizedPurpose });
    if (expectedRevision != null) query.set('expected_revision', JSON.stringify(expectedRevision));
    return `${this.baseUrl}/content/${encodeURIComponent(value)}?${query}`;
  }

  thumbnailUrl(resourceRef, { width = 160, height = 160, scale = 1, icon = false, expectedRevision = null } = {}) {
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
    if (icon) query.set("icon", "true");
    if (expectedRevision != null) query.set('expected_revision', JSON.stringify(expectedRevision));
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
          if (!['files-change', 'files-watch-error'].includes(event) || !data) continue;
          let payload;
          try { payload = JSON.parse(data); } catch {
            throw new FilesFacadeError('Files watch returned an invalid event', { code: 'partial_stream' });
          }
          if (event === 'files-watch-error') {
            throw new FilesFacadeError('Live folder updates stopped', { code: payload?.code || 'provider_unavailable' });
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
