import { resolveHostRaster, hostRasterTrail, hostRasterMime } from './hostRasterOriginal.js';
import { readBoundedImportBlob, CANVAS_MAX_IMPORT_BYTES } from './clipboard-and-drop.js';

/** Existing Files exact-open and image-project CAS adapter, bound to one session. */
export function createImagePersistence({ session, authority, filesClient, fetchImpl = globalThis.fetch, cryptoImpl = globalThis.crypto }) {
  if (!session?.captureTarget || typeof authority?.validate !== 'function' || typeof authority?.validateScope !== 'function' || !filesClient?.openResource || !filesClient?.contentUrl || !fetchImpl || !cryptoImpl?.subtle) throw new TypeError('image persistence dependencies required');
  let managedSaveMime=null;
  let tail = Promise.resolve(), uncertain = false, pendingOperation = null, project = null, acceptReceipt = null;
  const operationId = () => { if (typeof cryptoImpl.randomUUID !== 'function') throw new Error('operation identity unavailable'); return `imps:${cryptoImpl.randomUUID()}`; };
  const requireLore = (receipt, id) => {
    const lore=receipt?.lore_receipt;
    if (!lore || Object.keys(lore).sort().join(',') !== 'action_id,history_status,outcome' || lore.action_id !== id || lore.outcome !== 'committed' || !['complete','skipped','unavailable'].includes(lore.history_status)) throw new Error('project operation receipt requires reconciliation');
    return lore;
  };
  const markUnknown = (id, target, error) => { uncertain=true; pendingOperation=Object.freeze({operationId:id,target}); throw Object.assign(new Error(`Could not confirm the image save. ${error.message || 'Check the saved image before retrying.'}`), {operationId:id,cause:error}); };
  const targetKey = target => JSON.stringify([target.sessionId,target.scope,target.sourceKey]);
  const copyTarget = target => JSON.stringify({...target,draftId:null,opaqueRef:null});
  const check = (target) => {
    const now = session.captureTarget();
    if (targetKey(now) !== targetKey(target) || authority.validateScope(now) !== true) throw new Error('image authority or identity changed');
    return now;
  };
  async function imageDigest(bytes) {
    const hash = new Uint8Array(await cryptoImpl.subtle.digest('SHA-256', bytes));
    return 'sha256:' + Array.from(hash, byte => byte.toString(16).padStart(2,'0')).join('');
  }
  async function readSource(target, expectedDigest = target.sourceDigest, allowNewRevision = false, candidateRef = false) {
    const assertRead = () => { check(target); if (!allowNewRevision && JSON.stringify(candidateRef ? {...session.captureTarget(),opaqueRef:null} : session.captureTarget()) !== JSON.stringify(candidateRef ? {...target,opaqueRef:null} : target)) throw new Error('image source target changed'); };
    assertRead();
    if (!allowNewRevision && authority.validate(target) !== true) throw new Error('image source authority unavailable');
    if (target.sourceKey?.provider !== 'files' || !target.opaqueRef || !target.sourceRevision || !target.sourceDigest) throw new Error('exact Files image identity is unavailable');
    let opened;
    try {
      opened = await filesClient.openResource(target.opaqueRef);
    } catch (error) {
      if (error?.code !== 'resource_ref_stale') throw error;
      assertRead();
      const renewed = (await filesClient.reissue(target.opaqueRef)).resource;
      assertRead();
      if (!renewed?.ref || renewed.provider !== 'files' || renewed.id !== target.sourceKey.resourceId) throw new Error('Files image identity changed.');
      opened = await filesClient.openResource(renewed.ref);
    }
    assertRead();
    const resource = opened?.resource, payload = opened?.payload;
    if (opened?.target?.app !== 'imps' || resource?.provider !== 'files' || resource.id !== target.sourceKey.resourceId || payload?.provider !== 'files' || !payload.resource_id || !resource.ref || !resource.capabilities?.includes('preview') || !resource.capabilities?.includes('open')) throw new Error('Files image identity mismatch');
    if (!resource.revision?.kind || !resource.revision?.value || (!allowNewRevision && (resource.revision.kind !== target.sourceRevision.kind || resource.revision.value !== target.sourceRevision.value))) throw new Error('Files image revision changed');
    const response = await fetchImpl(filesClient.contentUrl(resource.ref, {purpose:'preview'}), {credentials:'same-origin',cache:'no-store'}); assertRead();
    if (!response.ok) throw new Error('Files image content unavailable');
    const blob = await readBoundedImportBlob(response, CANVAS_MAX_IMPORT_BYTES); assertRead();
    const bytes = await blob.arrayBuffer(); assertRead();
    const digest = await imageDigest(bytes); assertRead();
    if (digest.replace(/^sha256:/,'') !== expectedDigest.replace(/^sha256:/,'')) throw new Error('Files image digest changed');
    const mime=response.headers?.get?.('content-type') || payload.media_type || resource.mime_type || '';
    const saveMime=payload.read_only===false ? hostRasterMime(bytes,mime) : null;
    return Object.freeze({ bytes, mime, saveMime, name:resource.name || payload.filename || 'Image', resourceId:payload.resource_id, resourceRef:resource.ref, revision:resource.revision, digest, readOnly:payload.read_only === true });
  }
  const openSource = async (target = session.captureTarget()) => {
    const source=await readSource(target);
    managedSaveMime=source.saveMime;
    return source;
  };
  const verifyCandidateSource = async target => {
    const source=await readSource(target,target.sourceDigest,false,true);
    managedSaveMime=source.saveMime;
    return source;
  };
  async function request(path, options = {}) {
    const response = await fetchImpl(path, {credentials:'same-origin', ...options, headers:{...(options.body ? {'Content-Type':'application/json'} : {}), ...options.headers}});
    if (!response.ok) {
      const body = await response.json().catch(() => null);
      const detail = typeof body?.detail === 'string' ? body.detail : body?.detail?.message;
      throw Object.assign(new Error(detail || `Image project request failed (${response.status}).`), {status:response.status});
    }
    return response.json();
  }
  async function openProject(target, source, {adopt = true} = {}) {
    check(target);
    let lookup;
    try { lookup = await request(`/api/imps/projects/for-image/files/${encodeURIComponent(source.resourceId)}`); } catch (error) { if (error.status !== 404) throw error; }
    check(target);
    const found = lookup?.project;
    if (!found) { if (target.projectId) throw new Error('image project no longer available'); return null; }
    if (!found.id || !Number.isSafeInteger(found.project_revision) || (target.projectId && (found.id !== target.projectId || found.project_revision !== target.projectRevision)) || (found.expected_image_revision && found.expected_image_revision !== source.digest)) throw new Error('image project identity or revision changed');
    if (adopt) project = {id:found.id, revision:found.project_revision};
    return found;
  }
  function save({ target, state, imageBytes, name, width, height, beforeIssue }) {
    const frozen = Object.freeze({target, state:JSON.parse(JSON.stringify(state)), imageBytes, name, width, height});
    const run = async () => {
      if (uncertain) throw new Error('image save outcome unknown; reconcile project before retry');
      if (typeof acceptReceipt !== 'function' || typeof beforeIssue !== 'function') throw new Error('image receipt or draft owner unavailable');
      let current = check(frozen.target);
      if (current.sourceKey?.provider !== 'files') throw new Error('overwrite is unavailable for this provider');
      if (!current.sourceDigest) throw new Error('image project identity is unavailable');
      const source = await openSource(current); current = check(frozen.target);
      if (!source.saveMime || frozen.imageBytes?.type!==source.saveMime) throw Error('Original encoding or write access is unavailable. Use Save PNG to folder.');
      const outputBytes=await frozen.imageBytes.arrayBuffer();
      if (hostRasterMime(outputBytes,source.saveMime)!==source.saveMime) throw Error('The encoder did not preserve the original image format.');
      check(frozen.target);
      if (!project) {
        if (current.projectId && Number.isSafeInteger(current.projectRevision)) project = {id:current.projectId, revision:current.projectRevision};
        else {
          let lookup;
          try { lookup = await request(`/api/imps/projects/for-image/files/${encodeURIComponent(source.resourceId)}`); } catch (error) { if (error.status !== 404) throw error; }
          check(frozen.target);
          if (lookup?.project) {
            if (lookup.project.expected_image_revision && lookup.project.expected_image_revision !== source.digest) throw new Error('image project revision conflict');
            project = {id:lookup.project.id, revision:lookup.project.project_revision};
          } else {
            let created; const createId=operationId();
            { const draftTarget=await beforeIssue({operationId:createId,kind:'create',target:frozen.target}); const now=check(frozen.target); if (JSON.stringify(draftTarget)!==JSON.stringify(now) || JSON.stringify({...now,draftId:null})!==JSON.stringify({...current,draftId:null})) throw new Error('image target changed during draft'); current=now; }
            try { created = await request('/api/imps/projects',{method:'POST',body:JSON.stringify({operation_id:createId, provider:'files', resource_id:source.resourceId, name:frozen.name, width:frozen.width, height:frozen.height, state:frozen.state, expected_image_revision:source.digest})}); }
            catch (error) { if (!error.status || error.status >= 500) markUnknown(createId,current,error); throw error; }
            try { requireLore(created,createId); } catch (error) { markUnknown(createId,current,error); }
            try { check(frozen.target); } catch (error) { uncertain=true; throw Object.assign(new Error('image project created for a retired origin; reconciliation required'), {receipt:created, cause:error}); }
            project = {id:created.id, revision:created.project_revision};
          }
        }
      }
      if (!project?.id || !Number.isSafeInteger(project.revision)) { uncertain=true; throw new Error('image project receipt requires reconciliation'); }
      const bytes = new Uint8Array(outputBytes); check(frozen.target);
      let binary=''; for (let i=0;i<bytes.length;i+=0x8000) binary += String.fromCharCode(...bytes.subarray(i,i+0x8000));
      const outputDigest = await imageDigest(bytes); check(frozen.target);
      const saveId=operationId();
      const payload={operation_id:saveId,expected_project_revision:project.revision,expected_image_revision:current.sourceDigest.startsWith('sha256:') ? current.sourceDigest : `sha256:${current.sourceDigest}`,new_image_revision:outputDigest,state:frozen.state,name:frozen.name,width:frozen.width,height:frozen.height,image_bytes:btoa(binary)};
      let receipt;
      { const draftTarget=await beforeIssue({operationId:saveId,kind:'save',target:frozen.target}); const now=check(frozen.target); if (JSON.stringify(draftTarget)!==JSON.stringify(now) || JSON.stringify({...now,draftId:null})!==JSON.stringify({...current,draftId:null})) throw new Error('image target changed during draft'); current=now; }
      try { receipt = await request(`/api/imps/projects/${encodeURIComponent(project.id)}/save`,{method:'POST',body:JSON.stringify(payload)}); }
      catch (error) { if (!error.status || error.status >= 500) markUnknown(saveId,current,error); throw error; }
      try { requireLore(receipt,saveId); } catch (error) { markUnknown(saveId,current,error); }
      if (!Number.isSafeInteger(receipt?.project_revision) || receipt.project_revision <= project.revision || receipt.refresh_receipt?.image_revision !== outputDigest || receipt.refresh_receipt?.image?.provider !== 'files' || receipt.refresh_receipt?.image?.resource_id !== source.resourceId) { uncertain=true; throw new Error('image save receipt requires reconciliation'); }
      try { check(frozen.target); } catch (error) { uncertain=true; throw Object.assign(new Error('image save committed for a retired origin; reconciliation required'), {receipt, cause:error}); }
      project.revision = receipt.project_revision;
      let refreshed;
      try { refreshed = await readSource(current, receipt.refresh_receipt.image_revision, true); if (refreshed.resourceId !== source.resourceId) throw new Error('Files backend identity changed'); } catch (error) { uncertain=true; throw Object.assign(new Error('image save committed; Files refresh requires reconciliation'), {receipt, cause:error}); }
      const result = Object.freeze({receipt, expected:current, key:current.sourceKey, projectId:project.id, projectRevision:receipt.project_revision, sourceDigest:refreshed.digest, sourceRevision:refreshed.revision, opaqueRef:refreshed.resourceRef});
      try { acceptReceipt(result); } catch (error) { uncertain=true; throw Object.assign(new Error('image save committed; target refresh requires reconciliation'), {receipt,cause:error}); }
      return result;
    };
    const result=tail.catch(()=>{}).then(run); tail=result; return result;
  }
  function restorePending(value) {
    if (uncertain || pendingOperation || !value || !/^imps:[0-9a-f-]{36}$/.test(value.operationId || '') || !['create','save'].includes(value.kind) || value.phase!=='issued' || !value.target) throw new Error('project operation recovery invalid');
    const now=check(value.target);
    if (authority.validate(now)!==true || JSON.stringify({...now,draftId:null})!==JSON.stringify({...value.target,draftId:null})) throw new Error('project operation recovery target changed');
    uncertain=true; pendingOperation=Object.freeze({operationId:value.operationId,target:now});
  }
  async function observePending() {
    const pending=pendingOperation;
    if (!pending || !uncertain) throw new Error('no original project operation is available');
    const expected=check(pending.target);
    if (authority.validate(expected)!==true || JSON.stringify(expected)!==JSON.stringify(pending.target)) throw new Error('original image target changed');
    const status=await request('/api/imps/project-operations/status',{method:'POST',body:JSON.stringify({operation_id:pending.operationId})});
    if (JSON.stringify(check(pending.target))!==JSON.stringify(expected) || authority.validate(expected)!==true) throw new Error('original image target changed');
    if (status?.state!=='available' || status.action_id!==pending.operationId) return Object.freeze({state:'unknown'});
    if (status.outcome==='not_committed') return Object.freeze({state:'not_committed'});
    if (status.outcome!=='committed' || !['complete','skipped','unavailable'].includes(status.history_status) || !status.project_id || !Number.isSafeInteger(status.project_revision)) return Object.freeze({state:'unknown'});
    const record=await request(`/api/imps/projects/${encodeURIComponent(status.project_id)}`);
    const source=await readSource(expected,record.expected_image_revision,true);
    if (JSON.stringify(check(pending.target))!==JSON.stringify(expected) || authority.validate(expected)!==true || record?.id!==status.project_id || record.project_revision!==status.project_revision || record.image?.provider!=='files' || record.image?.resource_id!==source.resourceId || record.expected_image_revision!==source.digest) return Object.freeze({state:'unknown'});
    // Observation is not permission to adopt a later project or clear an unknown save.
    return Object.freeze({state:'available',projectId:status.project_id,projectRevision:status.project_revision,actionId:status.action_id,outcome:status.outcome,historyStatus:status.history_status});
  }
  function bindReceipt(callback) { if (acceptReceipt || typeof callback !== 'function') throw new Error('image receipt owner is already bound or invalid'); acceptReceipt=callback; return () => { if (acceptReceipt === callback) acceptReceipt=null; }; }
  async function finishCopy(pending, state, receipt, persistPending) {
    const current = check(pending.target);
    if (authority.validate(current) !== true || copyTarget(current) !== copyTarget(pending.target)) throw new Error('copy authority or source changed');
    const item = receipt?.items?.find(value => value.item_id === pending.itemId);
    if (receipt?.operation_id !== pending.operationId || receipt.generation !== pending.generation || item?.outcome !== 'committed' || !item.resource_ref) throw new Error('copy import remains unresolved');
    const opened = await filesClient.openResource(item.resource_ref);
    const afterOpen = check(pending.target);
    if (authority.validate(afterOpen) !== true || copyTarget(afterOpen) !== copyTarget(pending.target)) throw new Error('copy authority or source changed');
    const resource = opened?.resource, payload = opened?.payload;
    if (opened?.target?.app !== 'imps' || resource?.provider !== 'files' || payload?.provider !== 'files' || !payload.resource_id || !resource.revision?.value) throw new Error('copy resource is unavailable');
    if (!/^imps:[0-9a-f-]{36}$/.test(pending.bindingOperationId || '') || !['unissued','issued'].includes(pending.bindingPhase)) throw new Error('copy binding identity unavailable; reconciliation required');
    let result;
    if (pending.bindingPhase === 'issued') {
      const status=await request('/api/imps/project-operations/status',{method:'POST',body:JSON.stringify({operation_id:pending.bindingOperationId})});
      check(pending.target);
      if (status?.state !== 'available' || status.action_id !== pending.bindingOperationId || status.outcome !== 'committed' || !['complete','skipped','unavailable'].includes(status.history_status) || !status.project_id || !Number.isSafeInteger(status.project_revision)) throw new Error('copy binding outcome remains unknown');
      const found=await request(`/api/imps/projects/${encodeURIComponent(status.project_id)}`); check(pending.target);
      const reopened=await filesClient.openResource(item.resource_ref); check(pending.target);
      if (authority.validate(session.captureTarget())!==true || copyTarget(session.captureTarget())!==copyTarget(pending.target) || found?.id!==status.project_id || found.project_revision!==status.project_revision || found.image?.provider!=='files' || found.image?.resource_id!==payload.resource_id || found.expected_image_revision!==resource.revision.value || reopened?.resource?.id!==resource.id || reopened?.resource?.revision?.kind!==resource.revision.kind || reopened?.resource?.revision?.value!==resource.revision.value) throw new Error('copy binding identity changed');
      result={project_id:status.project_id,project_revision:status.project_revision,lore_receipt:{action_id:status.action_id,outcome:status.outcome,history_status:status.history_status}};
    } else {
      if (typeof persistPending !== 'function') throw new Error('copy binding draft owner unavailable');
      pending=Object.freeze({...pending,bindingPhase:'issued'});
      await persistPending(pending); check(pending.target);
      result=await request(pending.sourceProjectId ? `/api/imps/projects/${encodeURIComponent(pending.sourceProjectId)}/save-copy` : '/api/imps/projects/save-copy', {method:'POST',body:JSON.stringify({operation_id:pending.bindingOperationId,provider:'files',resource_id:payload.resource_id,operation_key:pending.operationId,expected_image_revision:resource.revision.value,state,name:pending.name,width:pending.width,height:pending.height})});
      check(pending.target);
      requireLore(result,pending.bindingOperationId);
      if (!result?.project_id || !Number.isSafeInteger(result.project_revision)) throw new Error('copy project receipt requires reconciliation');
    }
    return Object.freeze({result, resource, resourceId:payload.resource_id});
  }
  async function retryCopy(pending, state, persistPending) {
    if (pending?.version !== 2 || !/^imps-copy:[a-zA-Z0-9-]{1,64}$/.test(pending.operationId || '') || pending.itemId !== `${pending.operationId}:asset` || pending.filename !== `edited-${pending.operationId.slice('imps-copy:'.length)}.png` || pending.sourceProjectId !== pending.target?.projectId || !Number.isSafeInteger(pending.generation) || pending.generation < 0) throw new Error('copy recovery metadata invalid');
    check(pending.target);
    let receipt;
    try { receipt = await filesClient.operationReceipt(pending.operationId, {generation:pending.generation}); }
    catch (error) {
      if (error?.status !== 404 || error?.code !== 'resource_unavailable') throw error;
      if (pending.version !== 2 || pending.mime !== 'image/png' || typeof pending.bytes !== 'string' || !pending.destinationRef || !pending.filename || !Number.isSafeInteger(pending.size) || pending.size < 1 || pending.size > CANVAS_MAX_IMPORT_BYTES || pending.bytes.length !== Math.ceil(pending.size / 3) * 4) throw new Error('copy prepared bytes unavailable; reconciliation required');
      let decoded; try { decoded = atob(pending.bytes); } catch { throw new Error('copy prepared bytes invalid'); }
      if (decoded.length !== pending.size) throw new Error('copy prepared bytes invalid');
      const bytes = Uint8Array.from(decoded, char => char.charCodeAt(0));
      const digest = [...new Uint8Array(await cryptoImpl.subtle.digest('SHA-256',bytes))].map(byte=>byte.toString(16).padStart(2,'0')).join('');
      const current = check(pending.target);
      if (digest !== pending.digest || authority.validate(current) !== true || copyTarget(current) !== copyTarget(pending.target)) throw new Error('copy prepared identity or authority changed');
      const file = new File([bytes], pending.filename, {type:pending.mime});
      receipt = await filesClient.importFile(file,{operationId:pending.operationId,itemId:pending.itemId,generation:pending.generation,destinationRef:pending.destinationRef,name:pending.filename,collision:'fail'});
    }
    return finishCopy(pending, state, receipt, persistPending);
  }
  async function startCopy({target,state,imageBytes,name,width,height,persistPending}) {
    if (!filesClient.roots || !filesClient.importFile || !filesClient.operationReceipt || typeof persistPending !== 'function' || typeof cryptoImpl.randomUUID !== 'function') throw new Error('copy recovery dependencies unavailable');
    const current = check(target);
    if (authority.validate(current) !== true || JSON.stringify(current) !== JSON.stringify(target)) throw new Error('copy authority or source changed');
    const roots = await filesClient.roots({copalWorkspace:'default'}); check(target);
    const destination = roots?.entries?.find(entry => entry.provider === 'files' && entry.kind === 'provider_root' && entry.ref && entry.capabilities?.includes('write'));
    if (!destination || !Number.isSafeInteger(roots.policy_generation) || roots.policy_generation < 0) throw new Error('copy destination unavailable');
    const operationId = `imps-copy:${cryptoImpl.randomUUID()}`, itemId = `${operationId}:asset`;
    const filename = `edited-${operationId.slice('imps-copy:'.length)}.png`;
    const operationIdForBinding = `imps:${cryptoImpl.randomUUID()}`;
    if (!imageBytes || !Number.isSafeInteger(imageBytes.size) || imageBytes.size < 1 || imageBytes.size > CANVAS_MAX_IMPORT_BYTES) throw new Error('copy exceeds editor import limit');
    const bytes = new Uint8Array(await imageBytes.arrayBuffer()); check(target);
    const digest = [...new Uint8Array(await cryptoImpl.subtle.digest('SHA-256',bytes))].map(byte=>byte.toString(16).padStart(2,'0')).join(''); check(target);
    let binary=''; for (let i=0;i<bytes.length;i+=0x8000) binary += String.fromCharCode(...bytes.subarray(i,i+0x8000));
    const pending = Object.freeze({version:2,target,operationId,itemId,bindingOperationId:operationIdForBinding,bindingPhase:'unissued',generation:roots.policy_generation,sourceProjectId:target.projectId,name,width,height,filename,destinationRef:destination.ref,mime:'image/png',size:bytes.length,digest,bytes:btoa(binary)});
    await persistPending(pending); check(target);
    const file = new File([imageBytes], filename, {type:'image/png'});
    let receipt;
    try { receipt = await filesClient.importFile(file,{operationId,itemId,generation:pending.generation,destinationRef:destination.ref,name:file.name,collision:'fail'}); }
    catch (error) { throw Object.assign(new Error('copy import outcome requires receipt reconciliation'), {pending,cause:error}); }
    try { return await finishCopy(pending,state,receipt,persistPending); } catch (error) { throw Object.assign(new Error('copy binding requires reconciliation'), {pending,cause:error}); }
  }
  return Object.freeze({get managedSaveMime(){return managedSaveMime;},resolveHostRaster:(original,isCurrent)=>resolveHostRaster(filesClient,original,isCurrent),hostRasterTrail:row=>hostRasterTrail(filesClient,row),statHostRaster:ref=>filesClient.stat(ref),openSource,verifyCandidateSource,openProject,save,bindReceipt,startCopy,retryCopy,observePending,restorePending, get uncertain(){return uncertain;}, get pendingOperation(){return pendingOperation;}});
}
