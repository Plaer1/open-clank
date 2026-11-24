// Content-free first-party observations only; no path/name based joins.
const keys = ['version','accountID','workspaceID','hostChatID','engineSessionID','actorID','rootOperationID','executionRevision','turnID','toolCallID','resultSHA256','observedAt','operation','outcome','resource'];
const MAX_OBSERVED_EVENTS = 4096;
const receiptKeys = ['workspace_id','registration_id','source_snapshot_id','checkout_id','head_generation','admission_epoch','policy_generation','config_fingerprint'];
const plain = v => v && typeof v === 'object' && !Array.isArray(v);
const text = v => typeof v === 'string' && v.length > 0 && v.length <= 256;
const same = (a,b) => JSON.stringify(receiptKeys.map(k=>a?.[k] ?? null)) === JSON.stringify(receiptKeys.map(k=>b?.[k] ?? null));
function validReceipt(value) {
  return plain(value) && Object.keys(value).length === receiptKeys.length && receiptKeys.every(k=>
    ['admission_epoch','policy_generation'].includes(k) ? Number.isSafeInteger(value[k]) && value[k]>=0 : text(value[k]));
}
export function validateCodeActivity(event) {
  if (!plain(event) || Object.keys(event).length !== keys.length || !keys.every(k=>Object.hasOwn(event,k)) ||
      event.version!==1 || event.operation!=='read' || event.outcome!=='observed' ||
      !['accountID','workspaceID','hostChatID','engineSessionID','actorID','rootOperationID','turnID','toolCallID'].every(k=>text(event[k])) ||
      !Number.isSafeInteger(event.executionRevision) || event.executionRevision<0 || !Number.isSafeInteger(event.observedAt) || event.observedAt<0 ||
      typeof event.resultSHA256!=='string' || !/^[a-f0-9]{64}$/.test(event.resultSHA256)) return false;
  const resource=event.resource;
  return resource===null || plain(resource) && ['receipt,resourceID,sourceHash','receipt,resourceID,sourceHash,sourceSHA256'].includes(Object.keys(resource).sort().join(',')) &&
    (!Object.hasOwn(resource,'sourceSHA256') || typeof resource.sourceSHA256==='string' && /^[a-f0-9]{64}$/.test(resource.sourceSHA256)) &&
    typeof resource.resourceID==='string' && /^file:[a-f0-9]{64}$/i.test(resource.resourceID) &&
    typeof resource.sourceHash==='string' && /^[a-f0-9]{64}$/i.test(resource.sourceHash) &&
    validReceipt(resource.receipt) && resource.receipt.workspace_id===event.workspaceID;
}
const scopeKey = context => JSON.stringify([
  context?.accountID ?? null, context?.workspaceID ?? null, context?.chatID ?? null, context?.epoch ?? null,
  context?.receipt?.registration_id ?? null, context?.receipt?.checkout_id ?? null,
  context?.receipt?.admission_epoch ?? null, context?.receipt?.policy_generation ?? null,
]);
const observedBoundaryListeners = new Set();

export function subscribeToCodeContextBoundaries(listener) {
  observedBoundaryListeners.add(listener);
  return () => observedBoundaryListeners.delete(listener);
}
export function publishCodeContextBoundary(event) {
  if (!validBoundary(event)) return;
  for (const listener of observedBoundaryListeners) {
    try { listener(event); } catch { /* A Graph presentation cannot interrupt the chat mutation. */ }
  }
}
function validBoundary(event) {
  return plain(event) && ['compact','clear'].includes(event.operation) && text(event.eventID)
    && text(event.accountID) && text(event.hostChatID) && text(event.workspaceID)
    && Number.isSafeInteger(event.observedAt) && event.observedAt>=0;
}

export function createCodeActivityModel() {
  let scope = null, active = null, rows = [], boundaries = [], replayFloor = -1;
  const visuals = new Map();
  const seen = new Map();
  function clear() { scope=null;active=null;rows=[];boundaries=[];replayFloor=-1;seen.clear();visuals.clear(); }
  function setContext(context) {
    const key = scopeKey(context);
    if (key!==scope) { clear();scope=key; }
    if (context?.nodes!==active?.nodes) {
      visuals.clear();
      for (const node of context?.nodes || []) {
        if (node?.kind==='file' && typeof node.resource_id==='string') visuals.set(node.resource_id,visuals.has(node.resource_id)?null:node.id);
      }
    }
    active=context;
  }
  function admit(key, observedAt) {
    if (seen.has(key) || observedAt<=replayFloor) return false;
    seen.set(key,observedAt);
    if (seen.size>MAX_OBSERVED_EVENTS) {
      replayFloor=Math.min(...seen.values());
      for (const [candidate,time] of seen) if(time<=replayFloor)seen.delete(candidate);
    }
    return true;
  }
  const scoped=event=>event.accountID===active?.accountID && event.workspaceID===active?.workspaceID && event.hostChatID===active?.chatID;
  function link(row) {
    const usable = row.readReceipt && validReceipt(active?.receipt) && same(row.readReceipt,active.receipt);
    const visualID = usable ? visuals.get(row.resourceID) ?? null : null;
    return {...row,readReceipt:row.readReceipt?{...row.readReceipt}:null,status:visualID?'linked':'unlinked',visualID};
  }
  function add(event, context, maximum=20) {
    setContext(context);
    if (!validateCodeActivity(event) || !scoped(event)) return null;
    const key=JSON.stringify(['read',event.engineSessionID,event.rootOperationID,event.actorID,event.executionRevision,event.turnID,event.toolCallID]);
    if (!admit(key,event.observedAt)) return null;
    const resource=event.resource;
    const row={key,kind:'read',actorID:event.actorID,engineSessionID:event.engineSessionID,executionRevision:event.executionRevision,
      rootOperationID:event.rootOperationID,turnID:event.turnID,toolCallID:event.toolCallID,observedAt:event.observedAt,
      resultSHA256:event.resultSHA256,resourceID:resource?.resourceID ?? null,sourceHashAtRead:resource?.sourceHash ?? null,
      sourceSHA256:resource?.sourceSHA256 ?? null,readReceipt:resource?{...resource.receipt}:null};
    rows.push(row);
    rows.sort((a,b)=>a.observedAt-b.observedAt || a.key.localeCompare(b.key));
    const cap=Number.isSafeInteger(maximum)?Math.max(1,Math.min(100,maximum)):20;
    if (rows.length>cap) rows=rows.slice(-cap);
    return link(row);
  }
  function addBoundary(event,context) {
    setContext(context);
    if (!validBoundary(event) || !scoped(event)) return false;
    const key=JSON.stringify([event.operation,event.eventID]);
    if (!admit(key,event.observedAt)) return false;
    boundaries.push({key,kind:'context_boundary',operation:event.operation,observedAt:event.observedAt});
    boundaries.sort((a,b)=>a.observedAt-b.observedAt || a.key.localeCompare(b.key));
    if (boundaries.length>20) boundaries=boundaries.slice(-20);
    return true;
  }
  function timeline() {
    const observations = [...rows.map(link), ...boundaries].sort((a,b)=>a.observedAt-b.observedAt || a.key.localeCompare(b.key));
    const previous = new Map();
    let boundary = null;
    const items = observations.map(row=>{
      if (row.kind==='context_boundary') {boundary=row;return {...row};}
      const identity=row.resourceID && JSON.stringify([row.engineSessionID,row.actorID,row.resourceID]);
      const earlier=identity && previous.get(identity);
      if (identity) previous.set(identity,row);
      const reread=earlier && boundary && earlier.observedAt<boundary.observedAt && boundary.observedAt<row.observedAt;
      return {...row,rereadAfter:reread?{boundaryKey:boundary.key,operation:boundary.operation,
        priorReadKey:earlier.key,priorReadAt:earlier.observedAt}:null};
    });
    return {coverage:'partial_observed_events',firstObservedAt:items[0]?.observedAt ?? null,
      lastObservedAt:items.at(-1)?.observedAt ?? null,replayFloor:replayFloor<0?null:replayFloor,items};
  }
  return {setContext,add,addBoundary,timeline,snapshot:()=>rows.map(link),clear};
}

/** Join distinct native observations and committed records without comparing
 * path strings, or conflating indexed BLAKE3 with a Lore SHA-256 witness. */
export function joinCodeActivityHistory(page, reads, context) {
  const graph=page?.projection?.graph_scope, receipt=context?.receipt;
  if (!validReceipt(receipt) || graph?.workspace_id!==context.workspaceID || graph.workspace_id!==receipt.workspace_id
      || graph.checkout_id!==receipt.checkout_id || graph.policy_generation!==receipt.policy_generation
      || page?.projection?.coverage!=='partial_recorded_activity' || !Array.isArray(page.items) || page.items.length>20) return [];
  return page.items.filter(change=>change?.origin && change.origin.host_chat_id===context.chatID).map(change=>{
    const actor=change.origin;
    const verified=reads.filter(read=>read.rootOperationID===actor.root_operation_id && read.engineSessionID===actor.engine_session_id
      && read.actorID===actor.actor_id && read.executionRevision===actor.execution_revision && read.turnID===actor.inner_turn_id
      && read.resourceID===change.code_resource_id && typeof change.after_sha256==='string' && read.sourceSHA256===change.after_sha256);
    return {...change,readWitnesses:verified.map(read=>read.key),coverage:verified.length?'same_revision_read_observed':'read_coverage_unknown'};
  });
}
