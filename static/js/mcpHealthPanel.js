// Passive data only: callers own authorization, selection and cancellation.
export function renderManagedMcpHealth(container, result) {
  const paragraph = text => { const node=document.createElement('p');node.textContent=text;container.append(node); };
  container.replaceChildren();
  if (result?.status !== 'observed' || !Array.isArray(result.servers)) { paragraph('Managed MCP observation is unavailable for this turn or account.'); return; }
  const count = value => Number.isSafeInteger(value) && value >= 0 ? String(value) : 'unknown';
  const duration = value => Number.isSafeInteger(value) && value >= 0 ? `${value} ms` : 'unknown';
  const time = value => Number.isSafeInteger(value) && value >= 0 && value <= 8640000000000000 ? new Date(value).toLocaleString() : 'unknown';
  paragraph(`Managed MCP: ${result.servers.length} eligible connected clients captured for this turn · ${count(result.omittedServers)} omitted. Other configured or disconnected servers are not included.`);
  for (const server of result.servers.slice(0,128)) {
    if (!server || typeof server.serverKey !== 'string' || !/^[A-Za-z0-9_-]{1,128}$/.test(server.serverKey)) continue;
    const status=['connected','degraded','disabled','failed','needs_auth','needs_client_registration','disconnected','unknown'].includes(server.status) ? server.status.replaceAll('_',' ') : 'unknown';
    const category=['tool_error','transport','cancelled'].includes(server.lastCallCategory) ? server.lastCallCategory.replaceAll('_',' ') : 'none observed';
    paragraph(`${server.serverKey} · ${status} · generation ${count(server.connectionGeneration)} · observed ${time(server.observedAt)} · discovery ${duration(server.discoveryMs)} · last call ${duration(server.lastCallMs)} at ${time(server.lastCallAt)} (${category}) · discovered ${count(server.discoveredCount)} · candidate wrappers ${count(server.offeredToPromptCount)}`);
  }
  paragraph('Candidate wrappers precede prompt filtering. Model-visible tools and call permission are unknown.');
}

// Exact-turn candidate snapshot. It does not assert a model offer or grant.
export function renderExecCapabilities(container, result) {
  const paragraph = text => { const node = document.createElement('p'); node.textContent = text; container.append(node); };
  container.replaceChildren();
  const exec = result?.status === 'observed' ? result.exec : null;
  if (!exec || typeof exec.registryCandidate !== 'boolean') { paragraph('Exec observation is unavailable for this turn or account.'); return; }
  paragraph(`Exec registry candidate: ${exec.registryCandidate ? 'available' : 'not available'}. Model offer and call permission are unknown.`);
  if (!exec.registryCandidate) return;
  const limits = exec.limits;
  const num = value => Number.isSafeInteger(value) && value >= 0 ? String(value) : 'unknown';
  if (!limits || typeof limits !== 'object') { paragraph('Runtime limits unavailable.'); return; }
  paragraph(`Calls: ${num(limits.toolCallsDefault)} default / ${num(limits.toolCallsMaximum)} maximum; concurrency ${num(limits.concurrency)}. Active compute: ${num(limits.activeSecondsDefault)} seconds default / ${num(limits.activeSecondsMaximum)} maximum; wall limit ${num(limits.wallMilliseconds)} ms.`);
  paragraph(`Byte limits: result ${num(limits.resultBytes)}, logs ${num(limits.logBytes)}, code ${num(limits.codeBytes)}, file ${num(limits.fileBytes)}.`);
  if (typeof exec.declarations === 'string' && new TextEncoder().encode(exec.declarations).length <= 32768) {
    const details = document.createElement('details');
    const summary = document.createElement('summary'); summary.textContent = 'Typed tool declarations';
    const pre = document.createElement('pre'); pre.textContent = exec.declarations;
    details.append(summary, pre); container.append(details);
  } else paragraph('Typed declarations unavailable or omitted at the size limit.');
}
