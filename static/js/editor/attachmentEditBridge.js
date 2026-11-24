/** One captured pending-file edit. A lost upload response is never retried. */
export async function beginAttachmentEdit({file,capture,isCurrent,open,commit,fetchImpl=globalThis.fetch}) {
  if (!(file instanceof File) || !capture || typeof isCurrent!=='function' || !isCurrent() || typeof open!=='function' || typeof commit!=='function') throw new Error('Attachment origin is unavailable');
  const hash=async blob=>Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',await blob.arrayBuffer())),byte=>byte.toString(16).padStart(2,'0')).join('');
  const sourceDigest=await hash(file); if (!isCurrent()) throw new Error('The originating attachment changed');
  const source={v:1,kind:capture.sourceKind || 'pending_file',id:capture.sourceId || crypto.randomUUID(),locator:capture.sourceLocator || null,digest:sourceDigest,owner:capture.owner,workspace:capture.workspace,chatId:capture.sourceChatId===undefined ? capture.sid : capture.sourceChatId};
  const opened=await open(file,isCurrent,source), {session,runtime}=opened;
  const target=session.captureTarget(); if (target.scope.accountId!==capture.owner || target.scope.workspaceId!==capture.workspace) throw new Error('Attachment owner or workspace changed');
  let applying=false;
  opened.installApply(async () => {
    if (applying || runtime.attachmentApplyStatus()) throw new Error('This attachment result requires recovery; it will not be uploaded again');
    if (!isCurrent()) throw new Error('The original chat or attachment changed');
    applying=true;
    try {
      const snapshot=await runtime.captureAttachmentImage(), pngDigest=await hash(snapshot.blob);
      if (!isCurrent() || !runtime.isAttachmentSnapshotCurrent(snapshot)) throw new Error('The original chat or image changed');
      const descriptor={v:1,operationId:crypto.randomUUID(),owner:capture.owner,workspace:target.scope.workspaceId,chatId:capture.sid,authEpoch:capture.authEpoch,composerEpoch:capture.epoch,composerVersion:capture.version,sourceDigest,sourceKind:source.kind,sourceId:source.id,sceneRevision:snapshot.revision,pngDigest,state:'pending',uploadId:null,uploadDigest:null};
      await runtime.persistAttachmentApply(descriptor);
      if (!isCurrent() || !runtime.isAttachmentSnapshotCurrent(snapshot)) { await runtime.cancelUnsubmittedAttachmentApply(descriptor.operationId); throw new Error('The original chat or image changed before upload'); }
      const record=runtime.createAttachmentOutcomeRecorder(descriptor);
      const form=new FormData(); form.append('files',snapshot.blob,'edited-image.png'); form.append('session_id',capture.sid);
      let receipt;
      try { const response=await fetchImpl('/api/upload',{method:'POST',credentials:'same-origin',body:form}); if (!response.ok) throw new Error('Upload outcome is unknown'); const body=await response.json(); receipt=body?.files?.[0]; if (body?.files?.length!==1 || typeof receipt?.id!=='string' || !/^[a-zA-Z0-9._-]{1,256}$/.test(receipt.id) || receipt.checksum_sha256!==pngDigest) throw new Error('Upload receipt is invalid'); }
      catch (error) { record({state:'unknown'}); try {await runtime.persistRecordedAttachment();} catch {} throw new Error('The upload outcome needs reconciliation; the original attachment is unchanged.',{cause:error}); }
      record({state:'committed',uploadId:receipt.id,uploadDigest:pngDigest});
      await runtime.persistRecordedAttachment();
      if (!isCurrent() || !runtime.isAttachmentSnapshotCurrent(snapshot) || !commit(file,snapshot.blob,{...receipt,sessionId:capture.sid},capture)) return {status:'retained'};
      return {status:'attached'};
    } finally { applying=false; }
  });
  return opened;
}
