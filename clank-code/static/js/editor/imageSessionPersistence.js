import { writeHostRaster, hostRasterRecovery, hostRasterFingerprint } from './hostRasterOriginal.js';
import { renderLayerWithAdjLayers } from './fx/pixel-pass.js';
import { buildThumbnail } from './composite-helpers.js';

const RESULT_LIMIT = 16 * 1024 * 1024;
const validSidecars = value => value == null || (typeof value==='object' && !Array.isArray(value) && Object.keys(value).length===2 && ['base','mask'].every(key => typeof value[key]==='string' && value[key].length>0 && /^[A-Za-z0-9+/]*={0,2}$/.test(value[key])));
const resultSize = (bytes,sidecars) => bytes.length + (sidecars ? sidecars.base.length + sidecars.mask.length : 0);
const resultIdentity = target => JSON.stringify([target.sessionId,target.scope,target.sourceKey,target.sourceRevision,target.sourceDigest]);
const sameResultSource = (identity,target) => { try { const value=JSON.parse(identity); return Array.isArray(value) && value.length===5 && JSON.stringify(value.slice(0,3))===JSON.stringify([target.sessionId,target.scope,target.sourceKey]); } catch { return false; } };
const cloneResult = value => JSON.parse(JSON.stringify(value));
const SIDECAR_MAX_ENCODED = Math.ceil((32 * 1024 * 1024) / 3) * 4;
function attachmentDescriptor(value) {
  const keys=['v','operationId','owner','workspace','chatId','authEpoch','composerEpoch','composerVersion','sourceDigest','sceneRevision','pngDigest','state','uploadId','uploadDigest'], optional=['sourceKind','sourceId'];
  if (!value || typeof value!=='object' || Array.isArray(value) || Object.keys(value).some(key=>!keys.includes(key) && !optional.includes(key)) || keys.some(key=>!Object.hasOwn(value,key)) || value.v!==1 || (Object.hasOwn(value,'sourceKind')!==Object.hasOwn(value,'sourceId')) || (Object.hasOwn(value,'sourceKind') && (!['pending_file','upload','generated'].includes(value.sourceKind) || typeof value.sourceId!=='string' || !/^[a-zA-Z0-9._-]{1,256}$/.test(value.sourceId)))) throw new Error('Invalid attachment edit receipt');
  for (const key of ['operationId','owner','workspace','chatId']) if (typeof value[key]!=='string' || !value[key] || value[key].length>256 || /[\u0000-\u001f\u007f]/u.test(value[key])) throw new Error('Invalid attachment edit identity');
  for (const key of ['authEpoch','composerEpoch','composerVersion','sceneRevision']) if (!Number.isSafeInteger(value[key]) || value[key]<0) throw new Error('Invalid attachment edit revision');
  for (const key of ['sourceDigest','pngDigest']) if (!/^[a-f0-9]{64}$/.test(value[key] || '')) throw new Error('Invalid attachment edit digest');
  if (!['pending','unknown','committed'].includes(value.state) || (value.state==='committed' ? typeof value.uploadId!=='string' || !/^[a-zA-Z0-9._-]{1,256}$/.test(value.uploadId) || value.uploadDigest!==value.pngDigest : value.uploadId!==null || value.uploadDigest!==null)) throw new Error('Invalid attachment upload receipt');
  return Object.freeze({...value});
}
function sourceProvenance(value) {
  const keys=['v','kind','id','locator','digest','owner','workspace','chatId'];
  if (!value || typeof value!=='object' || Array.isArray(value) || Object.keys(value).length!==keys.length || keys.some(key=>!Object.hasOwn(value,key)) || value.v!==1 || !['pending_file','upload','generated','host_file'].includes(value.kind) || typeof value.id!=='string' || !/^[a-zA-Z0-9._-]{1,256}$/.test(value.id) || !/^[a-f0-9]{64}$/.test(value.digest || '')) throw new Error('Invalid image source provenance');
  for (const key of ['owner','workspace']) if (typeof value[key]!=='string' || !value[key] || value[key].length>256 || /[\u0000-\u001f\u007f]/u.test(value[key])) throw new Error('Invalid image source owner');
  if (value.chatId!==null && (typeof value.chatId!=='string' || !value.chatId || value.chatId.length>256 || /[\u0000-\u001f\u007f]/u.test(value.chatId))) throw new Error('Invalid image source chat');
  if (value.kind === 'host_file' && (!/^resource-[a-f0-9]{64}$/.test(value.id) || value.chatId !== null)) throw new Error('Invalid Host image source identity');
  const locator=['pending_file','host_file'].includes(value.kind) ? null : `/api/${value.kind==='upload' ? 'upload' : 'generated-image'}/${encodeURIComponent(value.id)}`;
  if (value.locator!==locator) throw new Error('Invalid image source locator');
  return Object.freeze({...value});
}
const sidecarDimensions = (w,h) => Number.isSafeInteger(w) && Number.isSafeInteger(h) && w>0 && h>0 && w<=8192 && h<=8192 && w*h<=40000000;
export function validateInpaintSidecarGeometry(value, factor = 1) {
  if (!Number.isSafeInteger(factor) || factor < 1 || !Number.isSafeInteger(value?.padPx) || value.padPx < 0 || value.padPx * factor > 80) throw new Error('Inpaint sidecar padding exceeds the save limit');
  for (const key of ['ai','base','mask']) if (!sidecarDimensions(value[key]?.width * factor, value[key]?.height * factor)) throw new Error('Inpaint sidecar dimensions exceed the save limit');
  return true;
}
function validateSidecarEnvelope(value) {
  if (!value || typeof value!=='object' || value.v!==1) return false;
  validateInpaintSidecarGeometry(value);
  let total=0;
  for (const key of ['ai','base','mask']) {
    const part=value[key], text=part?.dataUrl;
    if (!sidecarDimensions(part?.width,part?.height) || typeof text!=='string' || !text.startsWith('data:image/png;base64,')) throw new Error('Invalid inpaint sidecar');
    const encoded=text.slice(22); total+=encoded.length;
    if (!encoded || encoded.length%4 || !/^[A-Za-z0-9+/]*={0,2}$/.test(encoded) || atob(encoded.slice(0,16)).slice(0,8)!=='\x89PNG\r\n\x1a\n') throw new Error('Invalid inpaint sidecar PNG');
  }
  if (total>SIDECAR_MAX_ENCODED) throw new Error('Inpaint sidecars exceed the editor save limit');
  return true;
}
export function encodeInpaintSidecar(source) {
  if (!source) return null;
  if (!['ai','base','mask'].every(key=>typeof source[key]?.toDataURL==='function')) throw new Error('Inpaint sidecar pixels are unavailable');
  const value={v:1,padPx:source.padPx,...Object.fromEntries(['ai','base','mask'].map(key=>[key,{width:source[key].width,height:source[key].height,dataUrl:source[key].toDataURL('image/png')}]))};
  validateSidecarEnvelope(value); return value;
}
export async function decodeInpaintSidecar(value, document, ImageClass=globalThis.Image) {
  if (!validateSidecarEnvelope(value)) return null;
  const entries=await Promise.all(['ai','base','mask'].map(async key => { const part=value[key]; const img=await new Promise((resolve,reject)=>{const image=new ImageClass(); image.onload=()=>resolve(image); image.onerror=()=>reject(new Error('Inpaint sidecar could not be decoded')); image.src=part.dataUrl;}); if (img.naturalWidth!==part.width || img.naturalHeight!==part.height) throw new Error('Inpaint sidecar dimensions disagree'); const canvas=document.createElement('canvas'); canvas.width=part.width; canvas.height=part.height; canvas.getContext('2d').drawImage(img,0,0); return [key,canvas]; }));
  return Object.freeze({padPx:value.padPx,...Object.fromEntries(entries)});
}

export function flattenImageScene(scene, document = globalThis.document) {
  const out=document.createElement('canvas'); out.width=scene.imgWidth; out.height=scene.imgHeight; const ctx=out.getContext('2d');
  for (const layer of scene.layers) { if (!layer.visible) continue; ctx.globalAlpha=layer.opacity; const offset=scene.layerOffsets.get(layer.id) || {x:0,y:0}; ctx.drawImage(renderLayerWithAdjLayers(layer),offset.x,offset.y); }
  ctx.globalAlpha=1; return out;
}

export function editableImageMetadata(node) {
  const value = {...(node?._editableExtras || {})};
  for (const [key,item] of Object.entries(node || {})) { if (key.startsWith('_') || ['canvas','ctx','masks','dataUrl','canvasW','canvasH','inpaintSource'].includes(key) || typeof item === 'function' || item === undefined) continue; value[key]=JSON.parse(JSON.stringify(item)); }
  return value;
}
function serializeNode(node) { const source=encodeInpaintSidecar(node.inpaintSource), prior=node._editableExtras?.inpaintSource, metadata=editableImageMetadata(node); if (source && prior?.v===1) { for (const key of ['ai','base','mask']) source[key]={...prior[key],...source[key]}; Object.assign(source,{...prior,...source}); } else if (!source && prior?.v===1) delete metadata.inpaintSource; return {...metadata,...(source ? {inpaintSource:source} : {}),canvasW:node.canvas.width,canvasH:node.canvas.height,dataUrl:node.canvas.toDataURL('image/png'),...(node.masks ? {masks:node.masks.map(serializeNode)} : {})}; }
export function serializeEditableImageScene(scene) { return {...(scene._editableExtras || {}),v:2,imgWidth:scene.imgWidth,imgHeight:scene.imgHeight,activeLayerId:scene.activeLayerId,lastInpaintLayerId:scene.lastInpaintLayerId || null,nextLayerId:scene.nextLayerId,layers:scene.layers.map(layer=>({...serializeNode(layer),offset:{...(scene.layerOffsets.get(layer.id) || {x:0,y:0})}}))}; }

/** Session-owned serialized operations. Pixel encoding is supplied by the initiating view. */
export function createImageSessionPersistence({session,scene,adapter,authority,hostOriginal=null,requestHostSave=null,fetchImpl=globalThis.fetch}) {
  if (!session?.captureTarget || !scene || !adapter?.save || !adapter?.bindReceipt || typeof authority?.validateScope !== 'function') throw new TypeError('session persistence dependencies required');
  const copyDraftToken=Symbol(), closeRecoveryProof=new WeakMap();
  const operationDraftToken=Object.freeze({}); let pendingProjectOperation=null;
  let tail=Promise.resolve(), draftTail=Promise.resolve(), draftCount=0, draftUncertain=false, draftId=null, pendingDraftAttempt=null, pendingDraftReceipt=null, pendingSaveAttempt=null, pendingSaveReceipt=null, saveCount=0, savedRevision=0, pendingCopy=null, copyBusy=false, closeAccepted=false, disposed=false, receiptOwner=null, reauthorizing=false;
  let tokens=new WeakSet(); const proofs=new WeakMap(), restoredResults=new Set();
  let results=[], oversizeBytes=null, resultVersion=0, resultDurableVersion=0, resultAuthority=null, removeGestureListener=null, resultScheduled=false, resultPersistencePaused=false, resultError=null;
  let attachmentApply=null, attachmentVersion=0, attachmentDurableVersion=0, attachmentInFlight=false;
  let imageSourceProvenance=null, recoveredDraftId=null, recoveredCopyReceipt=null;
  const current = target => { const now=session.captureTarget(); if (disposed || authority.validateScope(now)!==true || JSON.stringify([now.sessionId,now.scope,now.sourceKey])!==JSON.stringify([target.sessionId,target.scope,target.sourceKey])) throw new Error('image session authority changed'); return now; };
  const accept = result => { if (!receiptOwner) throw new Error('session receipt owner unavailable'); receiptOwner.refresh(result.expected,result); };
  let unbind=adapter.bindReceipt(accept);
  const quiescent = () => !disposed && !closeAccepted && !attachmentInFlight && (!attachmentApply || attachmentApply.state!=='pending') && !saveCount && !draftCount && !draftUncertain && adapter.uncertain !== true && !copyBusy && !pendingCopy && !pendingDraftAttempt && !pendingDraftReceipt && !pendingSaveAttempt && !pendingSaveReceipt && !results.some(item => item.status==='reserved' || item.status==='submitted') && (!resultAuthority || resultAuthority.canApplyAt(scene.sceneRevision));
  const queueIdentity = () => JSON.stringify(results.map(({id,kind,status,identity,epoch,generation})=>[id,kind,status,identity,epoch,generation]));
  const queueResultDraft = () => {
    if (pendingProjectOperation || pendingCopy || copyBusy || resultScheduled || resultPersistencePaused || disposed || closeAccepted || attachmentInFlight || (resultVersion===resultDurableVersion && attachmentVersion===attachmentDurableVersion) || !resultAuthority?.canApplyAt(scene.sceneRevision)) return;
    resultScheduled=true;
    queueMicrotask(async () => {
      resultScheduled=false;
      if (pendingProjectOperation || pendingCopy || copyBusy || disposed || closeAccepted || resultPersistencePaused || attachmentInFlight || (resultVersion===resultDurableVersion && attachmentVersion===attachmentDurableVersion) || !resultAuthority?.canApplyAt(scene.sceneRevision) || saveCount || draftCount || draftUncertain || reauthorizing) return;
      try { await controller.saveDraft(controller.captureSnapshot(),pendingCopy); resultError=null; }
      catch (error) { resultError=String(error?.message || 'result draft unavailable'); }
    });
  };
  const changedResults = () => { resultVersion++; tokens=new WeakSet(); queueResultDraft(); };
  const findResult = id => { const item=results.find(result => result.id===id); if (!item) throw new Error('retained result unavailable'); return item; };
  const controller={
    get imageSourceProvenance(){return imageSourceProvenance;},
    setImageSourceProvenance(value) { const next=sourceProvenance(value), target=session.captureTarget(); current(target); if (imageSourceProvenance || next.owner!==target.scope.accountId || next.workspace!==target.scope.workspaceId || target.sourceKey!==null) throw new Error('Image source provenance changed'); imageSourceProvenance=next; return next; },
    prepareRestoreSourceProvenance(value) { if (imageSourceProvenance) throw new Error('Image source provenance already exists'); if (value==null) return ()=>{}; const next=sourceProvenance(value), target=session.captureTarget(); current(target); if (next.owner!==target.scope.accountId || next.workspace!==target.scope.workspaceId || target.sourceKey!==null) throw new Error('Image source provenance owner changed'); return ()=>{current(target); if (imageSourceProvenance) throw new Error('Image source provenance already exists'); imageSourceProvenance=next;}; },
    get attachmentApply(){return attachmentApply;},
    setAttachmentApply(value) { const next=attachmentDescriptor(value), target=session.captureTarget(); current(target); if (next.owner!==target.scope.accountId || next.workspace!==target.scope.workspaceId || attachmentApply || next.state!=='pending' || (imageSourceProvenance && (next.sourceKind!==imageSourceProvenance.kind || next.sourceId!==imageSourceProvenance.id || next.sourceDigest!==imageSourceProvenance.digest))) throw new Error('Attachment edit outcome requires reconciliation'); attachmentApply=next; attachmentVersion++; tokens=new WeakSet(); return next; },
    createAttachmentOutcomeRecorder(value) { const expected=attachmentDescriptor(value), target=session.captureTarget(); current(target); if (attachmentInFlight || !attachmentApply || JSON.stringify(attachmentApply)!==JSON.stringify(expected) || expected.state!=='pending') throw new Error('Attachment operation identity changed'); attachmentInFlight=true; let settled=false; return outcome => { if (settled || disposed || !attachmentInFlight || !attachmentApply || JSON.stringify(attachmentApply)!==JSON.stringify(expected)) throw new Error('Attachment operation identity changed'); const next=attachmentDescriptor({...expected,...outcome}); const base=item=>JSON.stringify({...item,state:'pending',uploadId:null,uploadDigest:null}); if (base(next)!==base(expected) || !['unknown','committed'].includes(next.state)) throw new Error('Attachment outcome identity changed'); settled=true; attachmentInFlight=false; attachmentApply=next; attachmentVersion++; tokens=new WeakSet(); return next; }; },
    clearUnsubmittedAttachmentApply(operationId) { if (attachmentInFlight || !attachmentApply || attachmentApply.operationId!==operationId || attachmentApply.state!=='pending') throw new Error('Attachment outcome cannot be cleared'); current(session.captureTarget()); attachmentApply=null; attachmentVersion++; tokens=new WeakSet(); },
    prepareRestoreAttachmentApply(value) { if (attachmentApply) throw new Error('Attachment edit recovery already exists'); if (value==null) return () => {}; const next=attachmentDescriptor(value), target=session.captureTarget(); current(target); if (next.owner!==target.scope.accountId || next.workspace!==target.scope.workspaceId) throw new Error('Attachment edit owner changed'); return () => { current(target); if (attachmentApply) throw new Error('Attachment edit recovery already exists'); attachmentApply=next.state==='pending' ? attachmentDescriptor({...next,state:'unknown'}) : next; attachmentVersion++; attachmentDurableVersion=attachmentVersion; }; },
    get adapter(){return adapter;},
    canReauthorize() { return !reauthorizing && quiescent() && !session.hasRuntime(); },
    async proveReauthorization({candidate,newAdapter,newAuthority,isCurrent,verifyBlank}) {
      if (!controller.canReauthorize() || typeof isCurrent !== 'function' || typeof newAdapter?.verifyCandidateSource !== 'function' || typeof newAdapter?.openProject !== 'function' || typeof newAuthority?.validate !== 'function' || typeof newAuthority?.validateScope !== 'function') throw new Error('retained image is not ready for reauthorization');
      candidate=JSON.parse(JSON.stringify(candidate));
      const expected=session.captureTarget();
      const sceneIdentity=[scene.sceneRevision,scene.managedResourceId,scene.managedProvider];
      const queueSnapshot={version:resultVersion,durableVersion:resultDurableVersion,identity:queueIdentity(),oversize:oversizeBytes,attachmentVersion,attachmentDurableVersion,attachment:JSON.stringify(attachmentApply),source:JSON.stringify(imageSourceProvenance)};
      const sameQueue=() => resultVersion===queueSnapshot.version && resultDurableVersion===queueSnapshot.durableVersion && queueIdentity()===queueSnapshot.identity && oversizeBytes===queueSnapshot.oversize && attachmentVersion===queueSnapshot.attachmentVersion && attachmentDurableVersion===queueSnapshot.attachmentDurableVersion && JSON.stringify(attachmentApply)===queueSnapshot.attachment && JSON.stringify(imageSourceProvenance)===queueSnapshot.source;
      const live=() => isCurrent() && quiescent() && sameQueue() && !session.hasRuntime() && JSON.stringify([scene.sceneRevision,scene.managedResourceId,scene.managedProvider])===JSON.stringify(sceneIdentity) && JSON.stringify(session.captureTarget())===JSON.stringify(expected) && newAuthority.validateScope(candidate)===true && newAuthority.validate(candidate)===true;
      const blank=candidate.sourceKey===null;
      if (JSON.stringify({...candidate,opaqueRef:null})!==JSON.stringify({...expected,opaqueRef:null}) || (blank ? candidate.opaqueRef!==null || !candidate.draftId || typeof verifyBlank!=='function' : !candidate.opaqueRef) || !live()) throw new Error('retained image identity or authority changed');
      reauthorizing=true;
      try {
        if (blank) { if (await verifyBlank(candidate)!==true || !live()) throw new Error('retained image draft changed'); }
        else { const project=await newAdapter.openProject(candidate,{resourceId:scene.managedResourceId,digest:candidate.sourceDigest},{adopt:false});
          if (!live() || (project?.id || null)!==(candidate.projectId || null) || (candidate.projectId && project.project_revision!==candidate.projectRevision)) throw new Error('retained image project changed');
          const source=await newAdapter.verifyCandidateSource(candidate);
          if (!live() || source.resourceId!==scene.managedResourceId || scene.managedProvider!=='files') throw new Error('retained image source changed'); }
        const proof=Object.freeze({}); proofs.set(proof,{candidate,expected,newAdapter,newAuthority,isCurrent,sceneIdentity,queueSnapshot}); return proof;
      } finally {reauthorizing=false;}
    },
    commitReauthorization(proof) {
      const value=proofs.get(proof); if (!value || !controller.canReauthorize() || !value.isCurrent() || resultVersion!==value.queueSnapshot.version || resultDurableVersion!==value.queueSnapshot.durableVersion || queueIdentity()!==value.queueSnapshot.identity || oversizeBytes!==value.queueSnapshot.oversize || attachmentVersion!==value.queueSnapshot.attachmentVersion || attachmentDurableVersion!==value.queueSnapshot.attachmentDurableVersion || JSON.stringify(attachmentApply)!==value.queueSnapshot.attachment || JSON.stringify(imageSourceProvenance)!==value.queueSnapshot.source || JSON.stringify([scene.sceneRevision,scene.managedResourceId,scene.managedProvider])!==JSON.stringify(value.sceneIdentity) || JSON.stringify(session.captureTarget())!==JSON.stringify(value.expected) || value.newAuthority.validateScope(value.candidate)!==true || value.newAuthority.validate(value.candidate)!==true) throw new Error('retained image reauthorization changed');
      const nextUnbind=value.newAdapter.bindReceipt(accept);
      const previousRef=scene.imageRef, previousPause=resultPersistencePaused; resultPersistencePaused=true;
      try { scene.imageRef=value.candidate.opaqueRef; receiptOwner.refresh(value.expected,{key:value.expected.sourceKey,opaqueRef:value.candidate.opaqueRef}); }
      catch(error) {scene.imageRef=previousRef; resultPersistencePaused=previousPause; try {nextUnbind?.();} catch (_) {} throw error;}
      const previous=unbind; adapter=value.newAdapter; authority=value.newAuthority; unbind=nextUnbind; tokens=new WeakSet(); proofs.delete(proof); try {previous?.();} catch (_) {} return true;
    },
    setReceiptOwner(owner) { if (receiptOwner || !owner?.refresh) throw new Error('session receipt owner is invalid'); receiptOwner=owner; },
    resumeResultPersistence() { if (disposed) return false; resultPersistencePaused=false; queueResultDraft(); return true; },
    setResultSceneAuthority(value) {
      if (resultAuthority || typeof value?.subscribeGestureEnd!=='function' || typeof value?.canApplyAt!=='function') throw new Error('result scene authority is invalid');
      resultAuthority=value; removeGestureListener=value.subscribeGestureEnd(queueResultDraft); queueResultDraft();
    },
    reserveResult({kind,epoch,generation}) {
      if (results.some(item=>item.status!=='resolved')) throw new Error('Finish the current image operation or resolve its retained result first.');
      if (resultVersion!==resultDurableVersion) { queueResultDraft(); throw new Error('The previous image result is still being saved to the draft. Try again once it settles.'); }
      if (!['ai','import'].includes(kind) || typeof epoch!=='string' || !epoch || epoch.length>256 || !Number.isSafeInteger(generation) || generation<0) throw new Error('The image operation is no longer current.');
      results=[]; restoredResults.clear();
      const target=session.captureTarget(); current(target);
      const id=globalThis.crypto?.randomUUID?.(); if (!id) throw new Error('result identity unavailable');
      results.push({id,kind,status:'reserved',identity:resultIdentity(target),epoch,generation}); changedResults(); return id;
    },
    submitResult(id) { const item=findResult(id); if (item.status!=='reserved') throw new Error('result is not reserved'); item.status='submitted'; changedResults(); },
    receiveResult(id,{bytes,mime,metadata={},sidecars=null}) {
      const item=findResult(id); if (!['reserved','submitted'].includes(item.status) || typeof bytes!=='string' || !bytes || !/^[A-Za-z0-9+/]*={0,2}$/.test(bytes) || typeof mime!=='string' || !/^image\/[a-z0-9.+-]+$/i.test(mime) || !validSidecars(sidecars)) throw new Error('result bytes unavailable');
      const meta=cloneResult(metadata); if (JSON.stringify(meta).length>16384) throw new Error('result metadata exceeds retention limit');
      if (resultSize(bytes,sidecars)>RESULT_LIMIT) {oversizeBytes=Object.freeze({bytes,sidecars:sidecars ? Object.freeze({...sidecars}) : null}); Object.assign(item,{status:'memory_only',mime,metadata:meta});}
      else Object.assign(item,{status:'received',bytes,mime,metadata:meta,...(sidecars ? {sidecars:{...sidecars}} : {})}); changedResults();
    },
    markResultUnknown(id) { const item=findResult(id); if (item.status!=='submitted') throw new Error('result was not submitted'); item.status='unknown'; changedResults(); },
    cancelLocalReservation(id) { const item=findResult(id); if (item.kind!=='import' || item.status!=='reserved' || Object.hasOwn(item,'bytes')) return false; item.status='resolved'; changedResults(); return true; },
    rejectResult(id) { const item=findResult(id); if (item.status!=='submitted') throw new Error('result was not submitted'); item.status='resolved'; changedResults(); },
    resolveResult(id) { const item=findResult(id); current(session.captureTarget()); if (!['reserved','received','memory_only'].includes(item.status)) throw new Error('result requires reconciliation'); item.status='resolved'; if (oversizeBytes) {oversizeBytes=null; delete item.mime;} changedResults(); },
    retainedResultStatus() { const target=session.captureTarget(); current(target); return Object.freeze({records:results.map(({id,kind,status,identity,metadata,generation})=>Object.freeze({id,kind,status,inpaint:Boolean(metadata?.inpaint),mask:Boolean(metadata?.mask),originChanged:restoredResults.has(id) || generation!==scene.sceneRevision || identity!==resultIdentity(target)})),durable:resultVersion===resultDurableVersion,error:resultError}); },
    readRetainedResult(id) { const target=session.captureTarget(); current(target); const item=findResult(id); if (!['received','memory_only'].includes(item.status) || !sameResultSource(item.identity,target) || authority.validate?.(target)!==true || (item.status==='memory_only' && !oversizeBytes)) throw new Error('retained result authority changed'); return Object.freeze({...cloneResult(item),...(item.status==='memory_only' ? oversizeBytes : {}),originChanged:restoredResults.has(id) || item.generation!==scene.sceneRevision || item.identity!==resultIdentity(target)}); },
    async persistRetainedResults() { if (!resultAuthority?.canApplyAt(scene.sceneRevision)) throw new Error('Finish the current image gesture first'); await controller.saveDraft(controller.captureSnapshot(),pendingCopy); return controller.retainedResultStatus(); },
    prepareRestoreResults(value) {
      if (results.length || !Array.isArray(value) || value.length>1) throw new Error('retained result draft is invalid');
      const target=session.captureTarget(); current(target); const identity=resultIdentity(target), version=resultVersion, prepared=[];
      for (const input of value) {
        const item=cloneResult(input);
        const allowed=new Set(['id','kind','status','identity','epoch','generation','bytes','mime','metadata','sidecars']);
        const hasBytes=Object.hasOwn(item || {},'bytes'), hasMime=Object.hasOwn(item || {},'mime');
        if (!item || typeof item!=='object' || Array.isArray(item) || Object.keys(item).some(key=>!allowed.has(key)) || typeof item.id!=='string' || !item.id || item.id.length>256 || !['ai','import'].includes(item.kind) || !['reserved','submitted','received','unknown','resolved','memory_only','unavailable'].includes(item.status) || typeof item.identity!=='string' || item.identity.length>8192 || !sameResultSource(item.identity,target) || typeof item.epoch!=='string' || !item.epoch || item.epoch.length>256 || !Number.isSafeInteger(item.generation) || item.generation<0 || (item.status==='received' && !hasBytes) || (item.sidecars!=null && (!hasBytes || !validSidecars(item.sidecars))) || (hasBytes && (!['received','resolved'].includes(item.status) || typeof item.bytes!=='string' || !item.bytes || resultSize(item.bytes,item.sidecars)>RESULT_LIMIT || !/^[A-Za-z0-9+/]*={0,2}$/.test(item.bytes))) || ((hasBytes || item.status==='memory_only') && (!hasMime || !/^image\/[a-z0-9.+-]+$/i.test(item.mime || ''))) || (hasMime && !['received','resolved','memory_only','unavailable'].includes(item.status)) || (item.metadata!=null && (typeof item.metadata!=='object' || Array.isArray(item.metadata))) || JSON.stringify(item.metadata || {}).length>16384) throw new Error('retained result draft is invalid');
        if (item.status==='reserved') item.status='resolved'; // A reservation has no running picker or request after reload.
        if (item.status==='submitted') item.status='unknown'; if (item.status==='memory_only') item.status='unavailable'; prepared.push(item);
      }
      return () => { current(target); if (results.length || resultVersion!==version || resultIdentity(session.captureTarget())!==identity) throw new Error('retained result restore changed'); results=prepared; for (const item of prepared) restoredResults.add(item.id); resultVersion++; resultDurableVersion=resultVersion; };
    },
    restoreResults(value) { controller.prepareRestoreResults(value)(); },
    get savedRevision(){return savedRevision;}, set savedRevision(value){savedRevision=value;},
    get pendingCopy(){return pendingCopy;}, set pendingCopy(value){pendingCopy=value;},
    get pendingDraftAttempt(){return pendingDraftAttempt;}, get pendingDraftReceipt(){return pendingDraftReceipt;}, get pendingSaveAttempt(){return pendingSaveAttempt;}, get pendingSaveReceipt(){return pendingSaveReceipt;},
    get copyBusy(){return copyBusy;}, set copyBusy(value){copyBusy=Boolean(value);},
    get saveCount(){return saveCount;}, get draftCount(){return draftCount;}, get closeAccepted(){return closeAccepted;},
    get dirty(){return scene.sceneRevision!==savedRevision || Boolean(pendingCopy) || Boolean(attachmentApply) || results.some(item=>item.status!=='resolved') || draftUncertain || adapter.uncertain === true;},
    recoveryStatus() {
      let scopeAvailable=false; try { scopeAvailable=!disposed && authority.validateScope(session.captureTarget())===true; } catch (_) {}
      if (!scopeAvailable) return Object.freeze({state:'scope_unavailable',canRetryCopy:false});
      if (reauthorizing) return Object.freeze({state:'in_flight',canRetryCopy:false});
      if (draftUncertain || pendingDraftReceipt) return Object.freeze({state:'draft_uncertain',canRetryCopy:false});
      if (adapter.uncertain === true || pendingProjectOperation || pendingSaveReceipt || pendingSaveAttempt) return Object.freeze({state:'save_uncertain',canRetryCopy:false});
      if (saveCount || draftCount || copyBusy) return Object.freeze({state:'in_flight',canRetryCopy:false});
      if (pendingCopy) return Object.freeze({state:'copy_pending',canRetryCopy:true});
      return Object.freeze({state:'ready',canRetryCopy:false});
    },
    prepareRestoreProjectOperation(value) {
      if (value==null) return ()=>{};
      const target=session.captureTarget(); current(target);
      if (pendingProjectOperation || !value || Object.keys(value).sort().join(',')!=='kind,operationId,phase,target' || !/^imps:[0-9a-f-]{36}$/.test(value.operationId || '') || !['create','save'].includes(value.kind) || value.phase!=='issued' || JSON.stringify({...value.target,draftId:null,opaqueRef:null})!==JSON.stringify({...target,draftId:null,opaqueRef:null})) throw new Error('project operation recovery target changed');
      return ()=>{ current(target); adapter.restorePending({...value,target:{...value.target,opaqueRef:target.opaqueRef}}); pendingProjectOperation=Object.freeze(value); };
    },
    async observeSaveOutcome() {
      if (typeof adapter.observePending!=='function' || !pendingProjectOperation) return Object.freeze({state:'unknown'});
      if (adapter.uncertain!==true) adapter.restorePending({...pendingProjectOperation,target:{...pendingProjectOperation.target,opaqueRef:session.captureTarget().opaqueRef}});
      return adapter.observePending();
    },
    async verifySource() {
      if (saveCount || draftCount || draftUncertain || adapter.uncertain === true || copyBusy || pendingCopy || pendingSaveReceipt || pendingDraftReceipt) throw new Error('image operation requires reconciliation before remount');
      const target=session.captureTarget(); current(target);
      if (authority.validate(target)!==true) throw new Error('image source authority unavailable');
      if (target.sourceKey == null) { if (target.sourceRevision != null || target.sourceDigest != null || target.opaqueRef != null || target.projectId != null || target.projectRevision != null || scene.managedResourceId || scene.managedProvider) throw new Error('blank image identity changed'); return Object.freeze({target,source:null}); }
      if (typeof adapter.openSource!=='function' || typeof adapter.openProject!=='function') throw new Error('image source authority unavailable');
      if (!scene.managedResourceId || scene.managedProvider!=='files') throw new Error('retained image source identity unavailable');
      const project=await adapter.openProject(target,{resourceId:scene.managedResourceId,digest:target.sourceDigest},{adopt:false}); current(target);
      if (JSON.stringify(session.captureTarget())!==JSON.stringify(target) || (project?.id || null)!==(target.projectId || null) || (target.projectId && project.project_revision!==target.projectRevision)) throw new Error('retained image project changed');
      const source=await adapter.openSource(target); current(target);
      if (JSON.stringify(session.captureTarget())!==JSON.stringify(target) || source.resourceId!==scene.managedResourceId) throw new Error('retained image source identity changed');
      return Object.freeze({target,source});
    },
    captureSnapshot() { const target=session.captureTarget(); current(target); return Object.freeze({target,sceneRevision:scene.sceneRevision,stateDoc:JSON.parse(JSON.stringify(serializeEditableImageScene(scene))),name:scene.draftName || undefined,width:scene.imgWidth || undefined,height:scene.imgHeight || undefined,provider:scene.managedProvider,resource:scene.managedResourceId,projectId:scene.projectId,projectRevision:scene.projectRevision,expectedImageRevision:scene.expectedImageRevision || '',draftId:scene.draftId}); },
    get hostOriginal() { return hostOriginal; },
    async restoreHostOriginal(record,target,pixelsSaved=false,pixelsPending=false) {
      if (!record) return;
      if (target.sourceKey || !target.draftId || record.v!==1 || record.sessionId!==target.sessionId ||
          record.scope?.accountId!==target.scope.accountId || record.scope?.workspaceId!==target.scope.workspaceId) {
        throw Error('Saved image destination does not belong to this draft.');
      }
      const restored=hostRasterRecovery(record,target);
      const isCurrent=()=>{try {return JSON.stringify(current(target))===JSON.stringify(target) && authority.validate(target)===true;} catch {return false;}};
      restored.stat=ref=>adapter.statHostRaster(ref);
      let confirmed=false;
      try { await adapter.resolveHostRaster(restored,isCurrent); confirmed=true; }
      catch { /* Restore editable pixels even if Files is offline or the original changed. Save rechecks. */ }
      if (!isCurrent()) throw Error('Restored image authority changed.');
      hostOriginal=restored;
      if (confirmed && (pixelsSaved || pixelsPending && record.pendingFingerprint===restored.revision.value)) { savedRevision=scene.sceneRevision; scene.persistDirty=false; }
    },
    async saveToHostDestination({name,write}) {
      if (draftCount && !saveCount) await draftTail;
      if (typeof write!=='function' || !scene.layers.length || closeAccepted || controller.recoveryStatus().state!=='ready') throw Error('Image is unavailable for saving.');
      const snapshot=controller.captureSnapshot(), target=snapshot.target;
      if (target.sourceKey) throw Error('Choose Save as copy for a managed Files image.');
      const isCurrent=()=>{try {const live=current(target); return !closeAccepted && authority.validate(live)===true && live.sessionId===target.sessionId;} catch {return false;}};
      if (!isCurrent() || resultAuthority && !resultAuthority.canApplyAt(snapshot.sceneRevision)) throw Error('Finish editing before saving this image.');
      saveCount++;
      try {
        const canvas=flattenImageScene(scene);
        const blob=await new Promise((resolve,reject)=>canvas.toBlob(value=>value ? resolve(value) : reject(Error('Canvas encode failed')),'image/png'));
        if (blob.type!=='image/png' || blob.size>32*1024*1024) throw Error('This image cannot be saved as PNG.');
        const expected=await hostRasterFingerprint(blob);
        await controller.saveDraft(snapshot,null,null,operationDraftToken);
        if (!isCurrent()) throw Error('Image destination changed.');
        const result=await write(blob,expected);
        if (!isCurrent() || result?.revision?.kind!=='hostFingerprint' || result.revision.value!==expected) throw Error('Could not confirm the saved PNG. Check the selected folder.');
        const next=hostRasterRecovery({name,mime:'image/png',resourceId:result.resourceId,resourceRef:result.resourceRef,revision:result.revision},target);
        next.stat=ref=>adapter.statHostRaster(ref);
        const row=await adapter.resolveHostRaster(next,isCurrent);
        next.trail=await adapter.hostRasterTrail(row);
        if (!isCurrent()) throw Error('Image destination changed.');
        hostOriginal=next; recoveredDraftId=null; recoveredCopyReceipt=null;
        scene.draftName=name;
        await controller.saveDraft({...snapshot,name,confirmedHostPixels:true},null,null,operationDraftToken);
        savedRevision=snapshot.sceneRevision; scene.persistDirty=scene.sceneRevision!==savedRevision;
        return {status:'saved_copy',sceneRevision:savedRevision,resourceId:next.resourceId,name,revision:{...next.revision}};
      } finally {saveCount--; queueResultDraft();}
    },
    setHostSaveHandler(handler) {
      if (typeof handler==='function' && authority.validate(current(session.captureTarget()))===true) requestHostSave=handler;
    },
    get canSaveManagedOriginal() { return Boolean(adapter.managedSaveMime); },
    get needsHostSaveDestination() {
      return typeof requestHostSave==='function' && !session.captureTarget().sourceKey && !hostOriginal?.mime && Boolean(scene.layers.length);
    },
    get canSaveHostOriginal() { return Boolean(hostOriginal?.mime); },
    markRestoredDraft(target) {
      if (JSON.stringify(current(target))!==JSON.stringify(target) || authority.validate(target)!==true) throw new Error('restored draft authority changed');
      recoveredDraftId=!hostOriginal && target.draftId && !target.sourceKey && !target.opaqueRef && !target.projectId && target.projectRevision==null && !target.sourceDigest && !target.sourceRevision ? target.draftId : null;
      recoveredCopyReceipt=null;
    },
    get canSaveRecoveredCopy() {
      const target=session.captureTarget();
      return Boolean(recoveredDraftId && target.draftId===recoveredDraftId && !target.sourceKey && scene.layers.length && typeof adapter.startCopy==='function' && typeof adapter.retryCopy==='function');
    },
    async saveCurrent() {
      if (draftCount && !saveCount) await draftTail;
      if (!scene.layers.length) return Promise.resolve({status:'unavailable',reason:'image_scene_empty'});
      if (reauthorizing || draftCount || draftUncertain || copyBusy || pendingCopy) return Promise.reject(new Error('draft or copy reconciliation required before saving'));
      const target=session.captureTarget();
      if (controller.needsHostSaveDestination) return requestHostSave();
      if (hostOriginal && !hostOriginal.mime) throw new Error('Save as copy to preserve this image format.');
      if (target.sourceKey && !controller.canSaveManagedOriginal) return {status:'unavailable',reason:'Original encoding or write access is unavailable. Use Save PNG to folder.'};
      if (!target.sourceKey && !hostOriginal) {
        if (!controller.canSaveRecoveredCopy) return {status:'unavailable',reason:'This image has no original. Use Save as copy.'};
        const revision=scene.sceneRevision;
        if (closeAccepted || authority.validate(target)!==true || controller.recoveryStatus().state!=='ready' || resultAuthority && !resultAuthority.canApplyAt(revision)) throw new Error('Recovered image is unavailable for saving.');
        if (recoveredCopyReceipt?.sceneRevision===revision) {
          if (resultVersion!==resultDurableVersion || attachmentVersion!==attachmentDurableVersion) await controller.saveDraft(controller.captureSnapshot());
          return recoveredCopyReceipt;
        }
        const copy=await controller.saveCopyCurrent();
        if (copy?.status==='unavailable') return copy;
        current(target);
        savedRevision=revision;
        scene.persistDirty=scene.sceneRevision!==revision;
        recoveredCopyReceipt=Object.freeze({...copy,status:'saved_copy',sceneRevision:revision});
        return recoveredCopyReceipt;
      }
      const snapshot=controller.captureSnapshot(), canvas=flattenImageScene(scene), mime=hostOriginal?.mime || adapter.managedSaveMime;
      const blob=new Promise((resolve,reject)=>canvas.toBlob(value=>value?resolve(value):reject(new Error('Canvas encode failed')),mime,mime==='image/jpeg'?0.92:undefined));
      if (!hostOriginal) return controller.save(snapshot,blob);
      if (saveCount) throw new Error('An original save is already in progress.');
      saveCount++;
      try {
        const pixels=await blob;
        const isCurrent=()=>{ try { return !closeAccepted && authority.validate(current(snapshot.target))===true; } catch { return false; } };
        await adapter.resolveHostRaster(hostOriginal,isCurrent);
        hostOriginal.pendingFingerprint=await hostRasterFingerprint(pixels);
        hostOriginal.pendingSceneRevision=snapshot.sceneRevision;
        await controller.saveDraft(snapshot,null,null,operationDraftToken);
        const receipt=await writeHostRaster(hostOriginal,pixels,isCurrent,fetchImpl,hostOriginal.pendingFingerprint);
        hostOriginal.pendingFingerprint=null;
        await controller.saveDraft({...snapshot,confirmedHostPixels:true},null,null,operationDraftToken);
        savedRevision=snapshot.sceneRevision;
        scene.persistDirty=scene.sceneRevision!==savedRevision;
        return receipt;
      } finally { saveCount--; queueResultDraft(); }
    },
    saveCopyCurrent() {
      if (!scene.layers.length) return Promise.resolve({status:'unavailable',reason:'image_scene_empty'});
      if (reauthorizing || saveCount || draftCount || copyBusy || pendingCopy) return Promise.resolve({status:'unavailable',reason:'copy_pending_or_unavailable'});
      const snapshot=controller.captureSnapshot(), thumbnail=buildThumbnail(scene.layers,scene.imgWidth,scene.imgHeight,scene.layerOffsets,160,0.6), canvas=flattenImageScene(scene);
      const blob=new Promise((resolve,reject)=>canvas.toBlob(value=>value?resolve(value):reject(new Error('Canvas encode failed')),'image/png'));
      return controller.startCopy(snapshot,blob,thumbnail);
    },
    canDispose(){return !reauthorizing && (closeAccepted || (!attachmentApply && resultVersion===resultDurableVersion && !results.some(item=>item.status!=='resolved') && !saveCount && !draftCount && !draftUncertain && adapter.uncertain !== true && !copyBusy && !pendingCopy && (!scene.layers.length || scene.sceneRevision===savedRevision)));},
    async save(snapshot,blob) {
      if (reauthorizing || draftCount || draftUncertain || copyBusy || pendingCopy) throw new Error('draft or copy reconciliation required before saving');
      if (!blob) throw new Error('image save bytes unavailable');
      saveCount++;
      const encoded=Promise.resolve(blob).then(value=>({value}),error=>({error}));
      const run=async()=>{ const resultBlob=await encoded; if (resultBlob.error) throw resultBlob.error; const imageBytes=resultBlob.value; const attemptTarget=current(snapshot.target); let result; try {result=await adapter.save({target:snapshot.target,state:snapshot.stateDoc,imageBytes,name:snapshot.name,width:snapshot.width,height:snapshot.height,beforeIssue:async value=>{ pendingProjectOperation=Object.freeze({...value,phase:'issued'}); const saved=await controller.saveDraft(snapshot,pendingCopy,null,operationDraftToken); if (saved.status!=='saved') throw new Error('project operation draft unavailable'); return current(snapshot.target); }});} catch(error) {if (error?.receipt) pendingSaveReceipt=error.receipt; if (adapter.uncertain === true) pendingSaveAttempt=Object.freeze({target:attemptTarget,sceneRevision:snapshot.sceneRevision,projectId:attemptTarget.projectId,projectRevision:attemptTarget.projectRevision,expectedImageRevision:attemptTarget.sourceDigest,operationId:adapter.pendingOperation?.operationId || null}); throw error;}
        const refreshed=current(snapshot.target); scene.imageRef=refreshed.opaqueRef; scene.projectId=refreshed.projectId; scene.projectRevision=refreshed.projectRevision; scene.expectedImageRevision=refreshed.sourceDigest;
        pendingProjectOperation=null; try { await controller.saveDraft(snapshot,pendingCopy,null,operationDraftToken); } catch (error) { pendingProjectOperation=Object.freeze({operationId:result.receipt.lore_receipt.action_id,kind:'save',target:snapshot.target,phase:'issued'}); throw Object.assign(new Error('save committed; draft reconciliation required'),{receipt:result.receipt,cause:error}); }
        pendingSaveAttempt=null; pendingSaveReceipt=null; savedRevision=snapshot.sceneRevision; scene.persistDirty=scene.sceneRevision!==snapshot.sceneRevision; return result.receipt; };
      const result=tail.catch(()=>{}).then(run); tail=result; try {return await result;} finally {saveCount--; queueResultDraft();}
    },
    async saveDraft(snapshot,pending=null,thumbnail=null,token=null) {
      if (pendingProjectOperation && token!==operationDraftToken) throw new Error('Reconcile the pending image save before replacing its recovery draft.');
      if ((pendingCopy || copyBusy) && token!==copyDraftToken) throw new Error('Reconcile the pending image copy before replacing its recovery draft.');
      if (reauthorizing || (saveCount && token!==operationDraftToken) || (resultAuthority && !resultAuthority.canApplyAt(scene.sceneRevision))) throw new Error('image gesture or save must settle before saving a draft');
      const frozen={...snapshot,imageId:scene.imageId,stateDoc:JSON.parse(JSON.stringify(snapshot.stateDoc))}; draftCount++;
      const run=async()=>{
        const expected=current(frozen.target); if (draftUncertain) throw new Error('draft creation outcome requires reconciliation');
        if (resultAuthority && !resultAuthority.canApplyAt(frozen.sceneRevision)) throw new Error('image scene changed before draft');
        const capturedResults=cloneResult(results), capturedResultVersion=resultVersion, capturedAttachment=attachmentApply, capturedAttachmentVersion=attachmentVersion;
        const payload={...frozen.stateDoc,imageId:frozen.imageId,...(hostOriginal ? {_imageHostOriginal:hostRasterRecovery(hostOriginal,expected),_imageHostPixelsSaved:frozen.confirmedHostPixels===true || frozen.sceneRevision===savedRevision,_imageHostPixelsPending:Boolean(hostOriginal.pendingFingerprint) && frozen.sceneRevision===hostOriginal.pendingSceneRevision} : {}),...(pendingProjectOperation ? {_imageProjectOperation:pendingProjectOperation} : {}),_imageSceneRevision:frozen.sceneRevision,_imageSessionRecovery:{...expected,draftId:null},_imageRetainedResults:capturedResults,...(pending ? {_imagePendingCopy:pending} : {}),...(capturedAttachment ? {_imageAttachmentApply:capturedAttachment} : {}),...(imageSourceProvenance ? {_imageSourceProvenance:imageSourceProvenance} : {})};
        let id=draftId || expected.draftId || null, response;
        const body=JSON.stringify({...(!id ? {request_id:globalThis.crypto.randomUUID()} : {}),name:frozen.name || 'Untitled',source_image_id:frozen.imageId || null,width:frozen.width,height:frozen.height,payload,thumbnail});
        pendingDraftAttempt=Object.freeze({draftId:id,target:expected,body,sceneRevision:frozen.sceneRevision,capturedResultVersion,capturedAttachmentVersion,pending});
        try { response=await fetchImpl(id ? `/api/editor-drafts/${encodeURIComponent(id)}` : '/api/editor-drafts',{method:id?'PUT':'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body}); }
        catch (error) { draftUncertain=true; throw error; }
        if (!response.ok) {if (response.status>=500) draftUncertain=true; else pendingDraftAttempt=null; throw new Error(`draft save failed (${response.status})`);}
        let receipt; try {receipt=await response.json();} catch(error) {draftUncertain=true; throw error;}
        if (!id) {if (typeof receipt?.id!=='string' || !receipt.id.trim()) {draftUncertain=true; throw new Error('draft creation receipt is missing an ID; reconciliation required');} id=receipt.id;}
        pendingDraftReceipt=receipt;
        try { const now=current(frozen.target); if (JSON.stringify(now)!==JSON.stringify(expected)) throw new Error('draft target changed'); receiptOwner.refresh(expected,{key:expected.sourceKey,draftId:id}); }
        catch (error) {draftUncertain=true; throw Object.assign(new Error('draft committed for changed target; reconciliation required'),{receipt,cause:error});}
        draftId=id; pendingDraftAttempt=null; pendingDraftReceipt=null; scene.draftId=id; scene.persistDirty=scene.sceneRevision!==frozen.sceneRevision; resultDurableVersion=capturedResultVersion; attachmentDurableVersion=capturedAttachmentVersion;
        return {status:'saved',receipt,sceneRevision:frozen.sceneRevision};
      };
      const result=draftTail.catch(()=>{}).then(run); draftTail=result; let saved=false; try{const value=await result; saved=true; return value;}finally{draftCount--; if (saved && resultVersion!==resultDurableVersion) queueResultDraft();}
    },
    async reconcileDraft() {
      const attempt=pendingDraftAttempt;
      if (!draftUncertain || !attempt || draftCount || saveCount || copyBusy || reauthorizing) return false;
      const target=current(attempt.target);
      if (JSON.stringify(target)!==JSON.stringify(attempt.target)) return false;
      draftCount++;
      try {
        // Replaying a keyed creation returns its original receipt even if the first response was lost.
        // Existing drafts are inspected before binding so recovery never overwrites a newer edit.
        const id=attempt.draftId;
        const response=id
          ? await fetchImpl(`/api/editor-drafts/${encodeURIComponent(id)}`,{credentials:'same-origin',cache:'no-store'})
          : await fetchImpl('/api/editor-drafts',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:attempt.body});
        if (!response.ok) return false;
        const receipt=await response.json();
        if (!receipt?.id || (id && (receipt.id!==id || JSON.stringify(receipt.payload)!==JSON.stringify(JSON.parse(attempt.body).payload))) ||
            pendingDraftAttempt!==attempt || JSON.stringify(current(attempt.target))!==JSON.stringify(target)) return false;
        receiptOwner.refresh(target,{key:target.sourceKey,draftId:receipt.id});
        scene.draftId=draftId=receipt.id;
        scene.persistDirty=scene.sceneRevision!==attempt.sceneRevision;
        resultDurableVersion=attempt.capturedResultVersion; attachmentDurableVersion=attempt.capturedAttachmentVersion;
        pendingCopy=attempt.pending; pendingDraftAttempt=null; pendingDraftReceipt=null; draftUncertain=false;
        return true;
      } finally { draftCount--; }
    },
    async startCopy(snapshot,blob,thumbnail) {
      if (reauthorizing || saveCount || draftCount || copyBusy || pendingCopy) return {status:'unavailable',reason:'copy_pending_or_unavailable'};
      copyBusy=true;
      try { blob=await blob; current(snapshot.target); if (scene.sceneRevision!==snapshot.sceneRevision) throw new Error('copy snapshot changed before allocation');
        const result=await adapter.startCopy({target:snapshot.target,state:snapshot.stateDoc,imageBytes:blob,name:`${snapshot.name || 'Untitled'} copy`,width:snapshot.width,height:snapshot.height,
          persistPending:async pending=>{ if (scene.sceneRevision!==snapshot.sceneRevision) throw new Error('copy snapshot changed before draft'); const saved=await controller.saveDraft(snapshot,pending,thumbnail,copyDraftToken); if (saved.status!=='saved') throw new Error('copy draft requires reconciliation'); pendingCopy=pending; }});
        await controller.saveDraft(snapshot,null,thumbnail,copyDraftToken); pendingCopy=null; return result;
      } finally {copyBusy=false;}
    },
    async retryCopy() {
      if (reauthorizing || saveCount || draftCount || draftUncertain || adapter.uncertain === true || copyBusy || !pendingCopy) return {status:'unavailable',reason:'copy_recovery_unavailable'};
      copyBusy=true; const pending=pendingCopy;
      try { const target=current(pending.target); if (!target.draftId) throw new Error('copy draft unavailable');
        const response=await fetchImpl(`/api/editor-drafts/${encodeURIComponent(target.draftId)}`,{credentials:'same-origin'}); if (!response.ok) throw new Error('copy draft unavailable');
        const draft=await response.json(); current(pending.target); if (JSON.stringify(draft?.payload?._imagePendingCopy)!==JSON.stringify(pending)) throw new Error('copy recovery draft changed');
        const data={...draft.payload}; delete data._imageSessionRecovery; delete data._imageProjectOperation; delete data._imagePendingCopy; delete data._imageSceneRevision; delete data._imageRetainedResults; delete data._imageHostOriginal; delete data._imageHostPixelsSaved; delete data._imageHostPixelsPending; delete data._imageAttachmentApply; delete data._imageSourceProvenance; delete data.imageId;
        const result=await adapter.retryCopy(pending,data,async next=>{
          const before=controller.captureSnapshot();
          const saved=await controller.saveDraft({...before,stateDoc:data},next,draft.thumbnail || null,copyDraftToken);
          if (saved.status!=='saved') throw new Error('copy draft requires reconciliation');
          pendingCopy=next;
        });
        if (resultAuthority && !resultAuthority.canApplyAt(scene.sceneRevision)) throw new Error('finish the image gesture before copy reconciliation');
        const currentSnapshot=controller.captureSnapshot();
        await controller.saveDraft(currentSnapshot,null,draft.thumbnail || null,copyDraftToken); pendingCopy=null; return result;
      } finally {copyBusy=false;}
    },
    prepareToken(target,choice) { current(target); const token=Object.freeze({status:'prepared',target:JSON.stringify(session.captureTarget()),revision:scene.sceneRevision,resultVersion,attachmentVersion,choice}); tokens.add(token); return token; },
    canCloseWithRecovery() {
      const state=controller.recoveryStatus().state;
      return !closeAccepted && !saveCount && !draftCount && !copyBusy && !attachmentInFlight &&
        (state==='copy_pending' || state==='save_uncertain' && pendingProjectOperation && !pendingCopy) &&
        !attachmentApply && !pendingDraftAttempt && !results.some(item=>['reserved','submitted','memory_only'].includes(item.status)) &&
        resultVersion===resultDurableVersion && (!resultAuthority || resultAuthority.canApplyAt(scene.sceneRevision)) &&
        Boolean(session.captureTarget().draftId);
    },
    async prepareClose(choice,operations={}) {
      if (!choice || choice==='cancel') return {status:'cancelled'};
      if (choice==='close_recovery') {
        if (!controller.canCloseWithRecovery()) return {status:'pending'};
        const target=session.captureTarget(), operation=pendingCopy || pendingProjectOperation;
        const field=pendingCopy ? '_imagePendingCopy' : '_imageProjectOperation';
        const token=controller.prepareToken(target,choice);
        const response=await fetchImpl(`/api/editor-drafts/${encodeURIComponent(target.draftId)}`,{credentials:'same-origin',cache:'no-store'});
        const draft=response.ok ? await response.json() : null;
        if (draft?.id!==target.draftId || JSON.stringify(draft.payload?.[field])!==JSON.stringify(operation) ||
            !draft.payload?._imageSessionRecovery || Object.entries({...target,draftId:null}).some(([key,value])=>
              key!=='opaqueRef' && JSON.stringify(draft.payload._imageSessionRecovery[key])!==JSON.stringify(value))) return {status:'pending'};
        closeRecoveryProof.set(token,operation);
        return controller.validateToken(token) ? token : {status:'pending'};
      }
      if (attachmentApply && (attachmentApply.state==='pending' || choice!=='keep_draft')) return {status:'pending',reason:'attachment_outcome_requires_recovery'};
      if (results.some(item=>item.kind==='import' && item.status==='reserved')) return {status:'pending',reason:'image_import_must_settle_or_cancel'};
      if (results.some(item=>item.status==='submitted')) return {status:'pending',reason:'result_outcome_unknown'};
      if (results.some(item=>item.status==='memory_only') && choice!=='discard') return {status:'pending',reason:'result_memory_only'};
      if (results.some(item=>!['resolved','unknown','unavailable'].includes(item.status)) && choice!=='discard' && choice!=='keep_draft') return {status:'pending',reason:'retained_result_unresolved'};
      if (reauthorizing || saveCount || draftCount || draftUncertain || adapter.uncertain === true || copyBusy || pendingCopy) return {status:'pending',reason:'copy_or_save_reconciliation_required'};
      const target=session.captureTarget(); current(target);
      if (choice==='save') {
        const startingResults=resultVersion, startingAttachment=attachmentVersion;
        const result=await (operations.save ? operations.save() : controller.saveCurrent());
        if (result?.status==='cancelled') return {status:'cancelled'};
        const writtenRevision=result?.status==='saved_copy' ? result.sceneRevision : savedRevision;
        if (!result || result.status==='unavailable' || writtenRevision!==scene.sceneRevision ||
            startingResults!==resultVersion || startingAttachment!==attachmentVersion) {
          return {status:'pending',reason:'image_not_fully_saved'};
        }
      }
      else if (choice==='keep_draft') { const result=operations.draft ? await operations.draft() : await controller.saveDraft(controller.captureSnapshot()); if (result?.status!=='saved' || !scene.draftId || scene.sceneRevision!==result.sceneRevision) return {status:'pending',reason:'draft_not_current'}; }
      else if (choice!=='discard') return {status:'unavailable',reason:'unknown_close_choice'};
      current(target); return controller.prepareToken(session.captureTarget(),choice);
    },
    validateToken(token) {
      if (token?.choice==='close_recovery') return tokens.has(token) && controller.canCloseWithRecovery() &&
        closeRecoveryProof.get(token)===(pendingCopy || pendingProjectOperation) && token.target===JSON.stringify(session.captureTarget()) &&
        token.revision===scene.sceneRevision && token.resultVersion===resultVersion && token.attachmentVersion===attachmentVersion;
      if (!tokens.has(token) || token.attachmentVersion!==attachmentVersion || token.resultVersion!==resultVersion ||
          token.revision!==scene.sceneRevision || reauthorizing || saveCount || draftCount || draftUncertain ||
          adapter.uncertain===true || copyBusy || pendingCopy || results.some(item=>item.status==='submitted' || (item.kind==='import' && item.status==='reserved'))) return false;
      if (attachmentApply && (token.choice!=='keep_draft' || attachmentApply.state==='pending' || attachmentVersion!==attachmentDurableVersion)) return false;
      if (token.choice==='keep_draft' && resultVersion!==resultDurableVersion) return false;
      try {
        const target=session.captureTarget();
        return authority.validateScope(target)===true && JSON.stringify(target)===token.target;
      } catch { return false; }
    },
    commitToken(token) {if (!controller.validateToken(token)) return false; tokens.delete(token); if (token.choice==='close_recovery') pendingCopy=null; closeAccepted=true; return true;},
    dispose() {if (disposed || !controller.canDispose()) return false; disposed=true; removeGestureListener?.(); unbind?.(); receiptOwner=null; return true;},
  };
  return Object.freeze(controller);
}
