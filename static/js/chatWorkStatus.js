// View-only counts from pages already authorized and fetched by this Workload mount.
const SOURCES = [
  ['native_task', 'Root commitments', 'commitment'], ['native_actor', 'Root actors', 'execution'],
  ['native_peer_actor', 'Direct peer actors', 'execution'],
  ['workflow', 'Session workflows (recorded)', 'execution'],
  ['shell_job', 'Shell jobs', 'execution'], ['media_job', 'Media jobs', 'execution'],
  ['memory_job', 'Source-linked memory jobs', 'execution'],
];
const sameScope = (a,b) => !!a && !!b && !!b.account && !!b.chat && a.account === b.account && a.chat === b.chat &&
  a.workspace === b.workspace && a.authEpoch === b.authEpoch;

export function currentChatWorkStatus(pages, scope, epoch) {
  const result = SOURCES.map(([source,label,category]) => {
    const page = pages.get(source);
    if (!page) return {source,label,category,availability:'not_loaded',statuses:[]};
    if (page.requestEpoch !== epoch || !sameScope(page.requestScope, scope) ||
        !['available','partial'].includes(page.availability) || !Array.isArray(page.items) ||
        (page.nativeScope?.hostChatId && page.nativeScope.hostChatId !== scope.chat))
      return {source,label,category,availability:'unavailable',statuses:[]};
    const counts = new Map(); let rejected = false;
    const native = source === 'native_task' || source === 'native_actor' || source === 'workflow';
    const peer = source === 'native_peer_actor';
    for (const row of page.items) {
      const ref = row?.sourceRef, status = row?.nativeStatus;
      if (!ref || ref.kind !== (peer ? 'native_actor' : source) || row.displayCategory !== category ||
          typeof status !== 'string' || !status || status.length > 80 ||
          (row.hostChatId != null && row.hostChatId !== scope.chat) ||
          (native && page.nativeScope?.rootSessionId && ref.sessionId !== page.nativeScope.rootSessionId) ||
          (peer && (!Array.isArray(page.nativeScope?.peerSessions) || !page.nativeScope.peerSessions.includes(ref.sessionId))) ||
          (!native && !peer && source !== 'memory_job' && ref.sessionId !== scope.chat) ||
          (source === 'memory_job' && ref.sessionId != null && ref.sessionId !== scope.chat)) {
        rejected = true; continue;
      }
      counts.set(status, (counts.get(status) || 0) + 1);
    }
    return {source,label,category,availability:page.availability === 'partial' || !!page.nextCursor ||
      source === 'memory_job' || source === 'workflow' || peer || rejected ? 'partial' : 'available',
      statuses:[...counts].sort(([a],[b]) => a.localeCompare(b)).map(([status,count]) => ({status,count}))};
  });
  const goal = pages.get('goal');
  if (!goal) {
    result.push({source:'goal',label:'Goals',category:'commitment',availability:'not_loaded',statuses:[]});
  } else if (goal.requestEpoch === epoch && sameScope(goal.requestScope, scope) && goal.availability === 'stale') {
    result.push({source:'goal',label:'Goals',category:'commitment',availability:'stale',statuses:[]});
  } else if (goal.requestEpoch !== epoch || !sameScope(goal.requestScope, scope) ||
      !['available','partial'].includes(goal.availability) || !goal.value || !Array.isArray(goal.value.queue)) {
    result.push({source:'goal',label:'Goals',category:'commitment',availability:'unavailable',statuses:[]});
  } else {
    const counts = new Map(), goals = []; let invalid = false;
    for (const [position,item] of [...(goal.value.active ? [goal.value.active] : []), ...goal.value.queue].entries()) {
      if (!item || typeof item.id !== 'string' || !item.id || item.id.length > 256 || !Number.isSafeInteger(item.revision) || item.revision < 1 ||
          typeof item.status !== 'string' || !item.status || item.status.length > 80) { invalid = true; continue; }
      counts.set(item.status, (counts.get(item.status) || 0) + 1);
      goals.push({id:item.id,revision:item.revision,status:item.status,role:goal.value.active && position === 0 ? 'active' : 'queued'});
    }
    result.push({source:'goal',label:'Goals',category:'commitment',availability:invalid ? 'partial' : goal.availability,
      statuses:[...counts].sort(([a],[b]) => a.localeCompare(b)).map(([status,count]) => ({status,count})), goals});
  }
  return result;
}
