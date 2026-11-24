// The existing Files importer owns operation receipts; state lasts only with its UI owner.
export async function runDeliverablePdfFiles({state,isCurrent,reconcile,prepare,verifyBeforeImport = null,beforeDispatch = null,receiptBinding,importFile,filename,
  makeFile = (blob, name) => new File([blob],name,{type:'application/pdf'})} = {}) {
  if (!state || typeof state !== 'object' || typeof isCurrent !== 'function' || typeof reconcile !== 'function'
      || typeof prepare !== 'function' || typeof importFile !== 'function') throw new TypeError('PDF owner unavailable');
  if (state.busy) return {state:'pending',reason:'operation_in_progress'};
  if (state.unknown) return {state:'unknown',reason:'receipt_unavailable'};
  state.busy = true;
  try {
    if (!isCurrent()) return {state:'unavailable',reason:'stale_source'};
    if (state.handles?.length) {
      if (state.receiptBinding !== receiptBinding) return {state:'unknown',reason:'receipt_scope_changed'};
      const result = await reconcile(state.handles,{isCurrent});
      if (!result?.terminal) { state.handles = result?.receiptHandles || state.handles; return {state:'pending',reason:'receipt_pending'}; }
      state.handles = null; state.receiptBinding = null; return {state:result.committed ? 'saved' : 'rejected',reconciled:true};
    }
    const rendered = await prepare();
    if (!isCurrent()) return {state:'unavailable',reason:'stale_source'};
    if (rendered?.state !== 'ready') return {state:'unavailable',reason:rendered?.reason || 'pdf_renderer_unavailable'};
    if (verifyBeforeImport && (await verifyBeforeImport() !== true || !isCurrent())) return {state:'unavailable',reason:'stale_source'};
    const file = makeFile(rendered.blob,filename);
    if (!isCurrent()) return {state:'unavailable',reason:'stale_source'};
    let result;
    try { result = await importFile(file,{isCurrent,purpose:'pdf',...(beforeDispatch ? {beforeDispatch} : {})}); }
    catch (_) { state.unknown = true; return {state:'unknown',reason:'receipt_unavailable'}; }
    if (result?.stale && !result?.submitted) return {state:'unavailable',reason:'stale_source'};
    if (result?.receiptHandles?.length) { state.receiptBinding = receiptBinding; state.handles = result.receiptHandles; return {state:'pending',reason:'receipt_pending'}; }
    if (result?.submitted && !result?.committed && !result?.rejected) { state.unknown = true; return {state:'unknown',reason:'receipt_unavailable'}; }
    if (result?.committed) return {state:'saved'};
    return {state:result?.rejected ? 'rejected' : 'cancelled'};
  } finally { state.busy = false; }
}
