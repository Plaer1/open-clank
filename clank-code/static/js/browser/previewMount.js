import {createPreviewViewport} from './previewViewport.js';
import { createWidgetStateController } from './widgetState.js';
import { createPreviewSession } from './previewSession.js';
import { createReportProgressController } from './reportProgressController.js';

// Conservative first profile: one interactive preview globally. References
// can rotate, so no resource identity is inferred from a client-side token.
// Only this coordinator owns the existing WB06 session. Rendering a placeholder
// never acquires it; callers must explicitly request a claim or handoff.
export function createPreviewMountCoordinator({sessionFactory = createPreviewSession} = {}) {
  const session = sessionFactory();
  let holder = null, sequence = 0, blocked = false, queue = Promise.resolve();
  const serial = operation => {
    const next = queue.then(operation, operation);
    queue = next.catch(() => {});
    return next;
  };
  const owns = lease => Boolean(lease && holder?.lease === lease && !blocked);
  async function closeHolder() {
    try {
      const result = await session.close();
      if (!result?.cleanupConfirmed) { blocked = true; return {cleanupConfirmed:false}; }
      return result;
    } catch (_) { blocked = true; return {cleanupConfirmed:false}; }
  }
  function acquire({owner, onRetire, handoff = false} = {}) {
    if (!owner || typeof onRetire !== 'function') throw new TypeError('Preview mount owner is required');
    return serial(async () => {
      if (blocked) return {state:'unavailable', reason:'cleanup_unconfirmed'};
      if (holder && holder.owner !== owner && !handoff) return {state:'unavailable', reason:'preview_in_use'};
      if (holder) {
        const previous = holder;
        holder = null;
        // Another view is retired synchronously before cancellation or publish.
        // Replacing the same owner is an explicit open by that owner.
        if (previous.owner !== owner) {
          try { if (await previous.onRetire() !== true) throw new Error('Retirement was not confirmed'); }
          catch (_) { blocked = true; return {state:'unavailable',reason:'retire_unconfirmed'}; }
        }
        const closed = await closeHolder();
        if (!closed.cleanupConfirmed) return {state:'unavailable',reason:'cleanup_unconfirmed'};
      }
      const lease = Object.freeze({id:++sequence});
      holder = {owner,onRetire,lease};
      return {state:'acquired',lease};
    });
  }
  async function create(lease, options) {
    if (!owns(lease)) throw new Error('Preview mount no longer owns the session.');
    try {
      const result = await session.create({...options, isCurrent:() => owns(lease) && (!options.isCurrent || options.isCurrent())});
      if (owns(lease) && (result?.cleanupConfirmed === false || result?.priorCleanupConfirmed === false)) blocked = true;
      return result;
    } catch (error) { if (owns(lease) && error?.cleanupUnknown) blocked = true; throw error; }
  }
  function status(lease, sessionId, options) {
    if (!owns(lease)) throw new Error('Preview mount no longer owns the session.');
    return session.status(sessionId, options);
  }
  function exportSourceBundle(lease, sessionId, options) {
    if (!owns(lease) || session.current?.session_id !== sessionId) throw new Error('Preview mount no longer owns the session.');
    return session.exportSourceBundle(sessionId, options);
  }
  function release(lease) {
    return serial(async () => {
      if (!holder || holder.lease !== lease) return {cleanupConfirmed:!blocked, released:false};
      holder = null;
      const result = await closeHolder();
      return {...result,released:true};
    });
  }
  return Object.freeze({acquire,create,status,exportSourceBundle,release,current:lease => owns(lease) ? session.current : null});
}

// One module instance per app page; Browser and future inline surfaces share it.
export const previewMountCoordinator = createPreviewMountCoordinator();


// Common frame owner for the Browser and a future explicitly activated inline
// surface. It does not authorize refs; WB06 and Files remain the authority.
export function createPreviewFrameMount({coordinator = previewMountCoordinator, owner, getViewport,
  isCurrent = () => true, onStatus = () => {}, onProgressStatus = () => {}, onProgress = () => {},
  onBackup = () => {}, onRetired = () => {}, frameClass = 'browser-applet-frame'} = {}) {
  if (!owner || typeof getViewport !== 'function') throw new TypeError('Preview frame owner is required');
  let lease = null, frame = null, model = null, generation = 0, disposed = false, handoffAllowed = false;
  let expiryTimer = null, kickTimer = null, statusController = null, exportController = null;
  const viewportChoice=createPreviewViewport();
  const listeners = new AbortController(), delays = [0,250,500,750];
  const session = () => coordinator.current(lease);
  const scopeCurrent = () => { try { return Boolean(!disposed && model && isCurrent() && model.isCurrent()); } catch (_) { return false; } };
  const current = expected => generation === expected && scopeCurrent();
  const origin = () => { try { return new URL(String(session()?.url || '')).origin; } catch { return ''; } };
  function validUrl(value) {
    try {
      const url = new URL(String(value?.url || '')), port = Number(value?.origin_port);
      return url.protocol === 'http:' && url.hostname === `${String(value?.artifact_id || '')}.artifacts.localhost`
        && /^[a-f0-9]{32}$/.test(String(value?.artifact_id || '')) && Number.isInteger(port) && port > 0 && port <= 65535
        && url.port === String(port) && url.pathname === '/index.html' && !url.username && !url.password && !url.search && !url.hash;
    } catch { return false; }
  }
  function post(type, fields = {}) {
    const value = session(), target = frame?.contentWindow, destination = origin();
    if (!value || !scopeCurrent() || !target || !destination) return false;
    target.postMessage({channel:'openclank.report.v1',type,session_id:value.session_id,generation,...fields},destination);
    return true;
  }
  const progress = createReportProgressController({
    context:() => ({session:session(),generation,active:scopeCurrent(),disposed,frameWindow:frame?.contentWindow || null,origin:origin()}),
    post, status:(...args) => { if (scopeCurrent()) onProgressStatus(...args); },
    onBackup:(...args) => { if (scopeCurrent()) onBackup(...args); },
  });
  const widgetContext = () => ({session:session(),generation,active:scopeCurrent(),disposed,frameWindow:frame?.contentWindow || null,origin:origin()});
  const widget = createWidgetStateController({context:widgetContext, post:data => { const target=frame?.contentWindow, destination=origin(); if (!scopeCurrent() || !target || !destination) return false; target.postMessage(data,destination); return true; }});
  const receive = event => { if (!scopeCurrent()) return; widget.receive(event); const result = progress.receive(event); if (result && scopeCurrent()) onProgress(result, progress); };
  if (typeof window !== 'undefined') window.addEventListener('message', receive, {signal:listeners.signal});
  function clearFrame() {
    viewportChoice.detach();
    statusController?.abort(); statusController = null;
    exportController?.abort(); exportController = null;
    if (expiryTimer) clearTimeout(expiryTimer); if (kickTimer) clearTimeout(kickTimer);
    expiryTimer = null; kickTimer = null;
    if (frame) { frame.onload = null; frame.onerror = null; frame.removeAttribute('src'); frame.remove(); frame = null; }
  }
  async function release() {
    const old = lease; lease = null;
    return old ? coordinator.release(old) : {cleanupConfirmed:true,released:false};
  }
  async function retireForHandoff() {
    ++generation; clearFrame(); progress.reset(); widget.reset(); model = null; lease = null;
    return await onRetired('handoff') !== false;
  }
  async function close(reason = 'closed') {
    ++generation; clearFrame(); progress.reset(); widget.reset(); model = null;
    const result = await release();
    return result;
  }
  async function checkDelivered(expected) {
    const value = session();
    if (!value || !current(expected) || statusController && !statusController.signal.aborted) return false;
    if (kickTimer) clearTimeout(kickTimer); kickTimer = null;
    const controller = new AbortController(); statusController = controller;
    const deadline = setTimeout(() => controller.abort(),5000);
    try {
      for (const delay of delays) {
        if (delay) await new Promise(resolve => setTimeout(resolve,delay));
        if (controller.signal.aborted || !current(expected)) return false;
        try {
          const state = await coordinator.status(lease,value.session_id,{signal:controller.signal});
          if (controller.signal.aborted || !current(expected)) return false;
          if (state.status === 'delivered' && state.delivered === true) {
            const warning = model.dependencyWarnings.length ? ` ${model.dependencyWarnings.join(' ')}` : '';
            onStatus(`Opened ${model.title}.${warning}`, Boolean(warning), false);
            progress.initialize(value,expected); return true;
          }
          if (['stale','revoked','expired'].includes(state.status)) {
            clearFrame(); void release();
            onStatus(state.status === 'expired' ? 'This Browser preview expired. Reopen the file from Files.' : 'This Browser preview is no longer authorized. Reopen the file from Files.',true,false);
            return false;
          }
        } catch (error) {
          if (error?.name === 'AbortError' && !controller.signal.aborted) return false;
          if (!current(expected)) return false;
          clearFrame(); void release();
          onStatus(error?.code === 'request_timeout' ? 'Browser status timed out. The preview session may still expire automatically; retry to check again.'
            : error.status === 401 ? 'Your authenticated session ended. Sign in and reopen the file.' : 'Browser preview status is unavailable. Reopen the file from Files.',true,error?.status !== 401);
          return false;
        }
      }
      if (current(expected)) onStatus('The isolated page is still connecting. Retry to check again; status checks stop after this attempt.',true,true);
      return false;
    } finally { clearTimeout(deadline); if (statusController === controller) statusController = null; }
  }
  function loadFrame() {
    const value = session(), expected = generation;
    if (!value || !validUrl(value) || !frame) { onStatus('The isolated Browser origin is unavailable. Reopen the file from Files.',true,false); return false; }
    onStatus('Loading the isolated HTML preview…',false,false);
    frame.onload = () => { if (frame?.src === value.url) void checkDelivered(expected); };
    frame.onerror = () => { if (frame?.src === value.url) void checkDelivered(expected); };
    if (frame.src === value.url) {
      frame.removeAttribute('src');
      requestAnimationFrame(() => { if (frame && current(expected)) frame.src = value.url; });
    } else frame.src = value.url;
    kickTimer = setTimeout(() => { if (current(expected)) void checkDelivered(expected); },900);
    return true;
  }
  async function open(input, {handoff = false} = {}) {
    if (disposed) return false;
    const expected = ++generation;
    handoffAllowed = handoff === true;
    clearFrame(); progress.reset(); widget.reset();
    model = Object.freeze({resourceRef:String(input?.resourceRef || ''),revision:input?.revision && typeof input.revision === 'object' ? Object.freeze({...input.revision}) : null,
      dependencies:Array.isArray(input?.dependencies) ? input.dependencies.map(item => Object.freeze({...item, ...(item?.expected_revision && typeof item.expected_revision === 'object' ? {expected_revision:Object.freeze({...item.expected_revision})} : {})})) : [],
      dependencyWarnings:Array.isArray(input?.dependencyWarnings) ? input.dependencyWarnings.map(String).slice(0,16) : [],
      title:String(input?.title || 'HTML preview'),isCurrent:typeof input?.isCurrent === 'function' ? input.isCurrent : () => true});
    onStatus(`Preparing ${model.title}…`,false,false);
    try {
      const claim = await coordinator.acquire({owner,onRetire:retireForHandoff,handoff});
      if (!current(expected)) { if (claim?.lease) await coordinator.release(claim.lease); return false; }
      if (claim.state !== 'acquired') { onStatus(claim.reason === 'preview_in_use' ? 'Another preview is active. Use an explicit handoff to open this view.' : 'Previous preview cleanup is unconfirmed. Reopen after the session is resolved.',true,false); return false; }
      lease = claim.lease;
      const result = await coordinator.create(claim.lease,{resourceRef:model.resourceRef,revision:model.revision,dependencies:model.dependencies,dependencyWarnings:model.dependencyWarnings,isCurrent:() => current(expected)});
      if (result.stale || !current(expected)) {
        await coordinator.release(claim.lease);
        if (lease === claim.lease) lease = null;
        return false;
      }
      const value = result.session;
      if (!value || !validUrl(value)) { await release(); onStatus('The server returned an unsupported Browser origin. This preview is unavailable here.',true,false); return false; }
      const viewport = getViewport();
      if (!viewport || !current(expected)) { await release(); return false; }
      frame = document.createElement('iframe'); frame.className = String(frameClass || 'browser-applet-frame'); frame.title = model.title;
      frame.setAttribute('sandbox','allow-scripts allow-same-origin allow-downloads'); frame.setAttribute('referrerpolicy','no-referrer'); frame.dataset.artifactId = value.artifact_id;
      viewport.replaceChildren(frame);
      viewportChoice.attach(frame,viewport);
      expiryTimer = setTimeout(() => { if (current(expected)) { void close('expired'); onRetired('expired'); } },Math.max(0,Number(value.expires_at)*1000-Date.now()));
      return loadFrame();
    } catch (error) {
      if (!current(expected)) return false;
      const cleanup = error.cleanupUnknown ? ' Server cleanup is unconfirmed; any late session expires within 15 minutes.' : '';
      onStatus(`${error.message || 'Browser preview could not be created.'}${cleanup}`,true,model.isCurrent()); return false;
    }
  }
  async function downloadSourceBundle() {
    const expected = generation, capturedLease = lease, value = session();
    if (!value || !current(expected) || exportController) throw new Error('The current preview is unavailable.');
    const controller = new AbortController(); exportController = controller;
    try {
      const blob = await coordinator.exportSourceBundle(capturedLease,value.session_id,{signal:controller.signal});
      if (controller.signal.aborted || !current(expected) || lease !== capturedLease || session()?.session_id !== value.session_id) throw new DOMException('Preview changed.', 'AbortError');
      const url = URL.createObjectURL(blob);
      try {
        const anchor = document.createElement('a'); anchor.href = url; anchor.download = 'open-clank-source-bundle.zip';
        document.body.append(anchor); anchor.click(); anchor.remove();
      } finally { setTimeout(() => URL.revokeObjectURL(url),1000); }
      return true;
    } finally { if (exportController === controller) exportController = null; }
  }
  async function verifyWidgetLease() {
    const expected=generation, captured=lease, value=session();
    if (!value || !captured || !current(expected)) throw Error('Preview unavailable.');
    const result=await coordinator.status(captured,value.session_id,{});
    if (!current(expected) || lease!==captured || session()?.session_id!==value.session_id || !['active','delivered'].includes(result?.status)) throw Error('Preview changed or expired.');
    return true;
  }
  const widgetState=Object.freeze({declaration:()=>widget.declaration(), envelope:values=>widget.envelope(values), readEnvelope:value=>widget.readEnvelope(value), verify:verifyWidgetLease,
    async capture(){await verifyWidgetLease();const result=await widget.capture();await verifyWidgetLease();return result;},
    async apply(values){await verifyWidgetLease();const result=await widget.apply(values);await verifyWidgetLease();return result;}});
  function retry() { if (!model || disposed || !isCurrent() || !model.isCurrent()) return false; if (session()) return loadFrame(); void open(model,{handoff:handoffAllowed}); return true; }
  async function dispose() { if (disposed) return {cleanupConfirmed:true}; disposed = true; listeners.abort(); return close('disposed'); }
  return Object.freeze({open,retry,close,dispose,downloadSourceBundle,progress,widgetState,viewportControl:viewportChoice.control,get session(){return session()},get frameWindow(){return frame?.contentWindow || null},get generation(){return generation},get origin(){return origin()}});
}
