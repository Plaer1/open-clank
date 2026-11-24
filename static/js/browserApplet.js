import { openOwnedWidgetState, hasPendingWidgetReceipt, checkWidgetReceipt } from './browser/widgetStateOwner.js';
import { createOpenClankWindow } from './copal/windows.js';
import { appletPath } from './appletRoutes.js';
import { createPreviewFrameMount } from './browser/previewMount.js';
import { mountBrowserConnectionPanel } from './browser/connectionPanel.js';
import { getCurrentSessionId as getCurrentChatId } from './sessions.js';
import uiModule from './ui.js';

const WINDOW_ID = 'browser-applet-window';
const READY_DELAYS = Object.freeze([0, 250, 500, 750]);
const BROWSER_MOUNT_OWNER = Object.freeze({ surface:'browser' });
let frameMount = null;
const sessions = Object.freeze({get current() { return frameMount?.session || null; }});
let windowHandle = null;
let message = null;
let retryButton = null;
let exportSourceButton = null;
let activeModel = null;
let centeredTitle = null;
let connectionPanel = null;
let generation = 0;
let disposed = false;
let listeners = null;
let reportBackupFile = null;
let widgetSaveStatusButton = null;
let filesImportAttempt = null;
let filesImportController = null;
let progressTools = null;
let progressControls = null;
let progressSummary = null;
let progressStatus = null;
let importBackupInput = null;
let exportJsonButton = null;
let exportMarkdownButton = null;
let importBackupButton = null;
let applyImportButton = null;
let saveBackupToFilesButton = null;
let exportPreviousButton = null;

const reportProgress = Object.freeze({
  reset:() => frameMount?.progress.reset(),
  exportBackup:(...args) => frameMount?.progress.exportBackup(...args),
  importBackup:(...args) => frameMount?.progress.importBackup(...args),
  apply:(...args) => frameMount?.progress.apply(...args),
  get hasPrevious() { return Boolean(frameMount?.progress.hasPrevious); },
  get hasPendingImport() { return Boolean(frameMount?.progress.hasPendingImport); },
});

function setMessage(text, bad = false) {
  if (message) {
    message.textContent = String(text || '');
    message.classList.toggle('is-error', Boolean(bad));
  }
  windowHandle?.setStatus(text, bad);
}

function ensureStyles() {
  if (document.querySelector('[data-browser-applet-style]')) return;
  const link = document.createElement('link');
  link.rel = 'stylesheet';
  link.href = new URL('../css/browserApplet.css', import.meta.url).href;
  link.dataset.browserAppletStyle = '';
  document.head.append(link);
}

function normalizeScope(value) {
  return String(value || 'default');
}

function clearReportState() {
  reportProgress.reset();
  reportBackupFile = null;
  if (progressTools) progressTools.hidden = true;
  if (progressTools) progressTools.open = false;
  setProgressStatus('No declared report progress is connected.');
  if (saveBackupToFilesButton) saveBackupToFilesButton.disabled = false;
  if (saveBackupToFilesButton) saveBackupToFilesButton.hidden = true;
  if (saveBackupToFilesButton && filesImportAttempt?.receiptHandles?.length) {
    saveBackupToFilesButton.hidden = false;
    saveBackupToFilesButton.textContent = 'Check pending Files receipt';
  }
  else if (saveBackupToFilesButton) saveBackupToFilesButton.textContent = 'Save backup to Files…';
  if (exportPreviousButton) exportPreviousButton.hidden = true;
  if (applyImportButton) applyImportButton.hidden = true;
  if (importBackupInput) importBackupInput.value = '';
}

function setProgressStatus(value) {
  const text = String(value || '');
  if (progressStatus) progressStatus.textContent = text;
  if (progressSummary) {
    const state = text.split(/[·.]/, 1)[0].trim().slice(0, 48);
    progressSummary.textContent = state && state !== 'No declared report progress is connected.'
      ? `Progress and backups — ${state}` : 'Progress and backups';
  }
}

function sessionOrigin() { return frameMount?.origin || ''; }

function downloadReportBackup(format, usePrevious = false) {
  try {
    const file = reportProgress.exportBackup(format, usePrevious);
    const url = URL.createObjectURL(file);
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = file.name;
    anchor.hidden = true;
    document.body.append(anchor);
    anchor.click();
    anchor.remove();
    setTimeout(() => URL.revokeObjectURL(url), 30_000);
  } catch (error) {
    setProgressStatus(error?.message || 'Backup export failed.');
  }
}

async function chooseReportBackup(file) {
  applyImportButton.hidden = true;
  try {
    const result = await reportProgress.importBackup(file);
    applyImportButton.textContent = result.conflicts ? `Apply ${result.accepted} compatible items` : `Apply ${result.accepted} items`;
    applyImportButton.hidden = !result.accepted;
  } catch (error) {
    setProgressStatus(error?.message || 'Backup import was rejected.');
  }
}

async function applyReportBackup() {
  await reportProgress.apply(async conflicts => {
    const accepted = reportProgress.hasPendingImport ? true : false;
    if (!accepted) return false;
    const reasons = conflicts.map(item => item.reason).join(', ');
    return await uiModule.styledConfirm(
      `Compatible items will be sent. Conflicts will be skipped: ${reasons}.`,
      { title: 'Apply compatible progress', confirmText: 'Apply compatible items', cancelText: 'Cancel' },
    );
  });
}

async function saveBackupInFiles() {
  const capturedFile = reportBackupFile;
  const capturedSession = sessions.current?.session_id || '';
  const capturedOrigin = sessionOrigin();
  const capturedArtifactId = sessions.current?.artifact_id || '';
  const capturedGeneration = generation;
  const reconcileAttempt = filesImportAttempt;
  if (!capturedFile && !reconcileAttempt?.receiptHandles?.length) return;
  if (reconcileAttempt?.unknown && !reconcileAttempt.receiptHandles?.length) {
    setProgressStatus('This Files operation was submitted, but its receipt ID is unavailable. Do not start a duplicate import; recover the original Files operation first.');
    return;
  }
  filesImportController?.abort();
  const controller = new AbortController();
  filesImportController = controller;
  const isCurrent = () => capturedGeneration === generation && !disposed && activeModel != null
    && sessions.current?.session_id === capturedSession && sessionOrigin() === capturedOrigin
    && sessions.current?.artifact_id === capturedArtifactId && !controller.signal.aborted;
  saveBackupToFilesButton.disabled = true;
  if (reconcileAttempt?.receiptHandles?.length) setProgressStatus('Checking the existing Files operation receipt…');
  else setProgressStatus('Choose an authorized Files folder and collision policy…');
  try {
    const files = await import('./files.js');
    if (!isCurrent()) return;
    if (reconcileAttempt?.receiptHandles?.length) {
      const result = await files.reconcileBrowserReportImport(reconcileAttempt.receiptHandles, { signal:controller.signal, isCurrent });
      if (result?.terminal) {
        filesImportAttempt = null;
        if (result.committed) setProgressStatus(`Files imported · ${reconcileAttempt.file?.name || 'report backup'} · existing committed receipt confirmed.`);
        else setProgressStatus('The existing Files operation is complete and did not commit. Choose Save backup to Files to start a new operation.');
        saveBackupToFilesButton.textContent = 'Save backup to Files…';
      } else {
        filesImportAttempt = { ...reconcileAttempt, receiptHandles:result?.receiptHandles || reconcileAttempt.receiptHandles };
        if (isCurrent()) setProgressStatus('Files operation remains unknown or pending. Its original receipt is retained; check again before starting another import.');
        saveBackupToFilesButton.textContent = 'Check pending Files receipt';
      }
      return;
    }
    const result = await files.importBrowserReportFile(capturedFile, { signal:controller.signal, isCurrent });
    if (result?.receiptHandles?.length) {
      filesImportAttempt = { file:capturedFile, receiptHandles:result.receiptHandles,
        artifactId:capturedArtifactId, origin:capturedOrigin, sessionId:capturedSession, generation:capturedGeneration };
      saveBackupToFilesButton.textContent = 'Check pending Files receipt';
    } else if (result?.submitted && !result?.committed && !result?.rejected) {
      filesImportAttempt = { file:capturedFile, receiptHandles:[], unknown:true,
        artifactId:capturedArtifactId, origin:capturedOrigin, sessionId:capturedSession, generation:capturedGeneration };
    }
    if (!isCurrent()) return;
    if (result?.committed) setProgressStatus(`Files imported · ${capturedFile.name} · committed receipt confirmed.`);
    else if (result?.pending || result?.receiptHandles?.length) {
      setProgressStatus('Files operation is pending or unknown. Check its original receipt before starting another import.');
      saveBackupToFilesButton.textContent = 'Check pending Files receipt';
    }
    else if (result?.rejected) setProgressStatus('Files returned a rejected receipt; the backup was not committed.');
    else if (result?.submitted) setProgressStatus('Files import outcome is not confirmed; its operation receipt must be checked before retrying.');
    else setProgressStatus(result?.cancelled ? 'Files import was cancelled before staging.' : 'Files import was not committed.');
  } catch (error) {
    if (isCurrent()) setProgressStatus(`${error?.message || 'Files import failed.'} Check its operation receipt before retrying.`);
  } finally {
    if (filesImportController === controller) filesImportController = null;
    if (isCurrent()) saveBackupToFilesButton.disabled = false;
  }
}

function cancelFilesImport() {
  filesImportController?.abort();
  filesImportController = null;
}

function frameRetired(reason) {
  ++generation; cancelFilesImport(); activeModel = null; clearReportState();
  if (exportSourceButton) exportSourceButton.hidden = true;
  setMessage(reason === 'handoff' ? 'Preview moved to another view. Open it again to take it back.' : 'This Browser preview expired. Reopen the file from Files.');
}

function retire(reason, { closeWindow = false } = {}) {
  const retiredGeneration = ++generation;
  cancelFilesImport();
  activeModel = null;
  if (exportSourceButton) exportSourceButton.hidden = true;
  clearReportState();
  const cleanup = frameMount?.close(reason) || Promise.resolve({cleanupConfirmed:true});
  setMessage(reason || 'Browser preview closed.');
  void cleanup.then(result => {
    if (retiredGeneration === generation && result && !result.cleanupConfirmed && !closeWindow && !disposed) {
      setMessage(`${reason || 'Browser preview closed.'} Server cleanup is unconfirmed; the session expires within 15 minutes.`, true);
    }
  });
  if (closeWindow && windowHandle?.visible) void windowHandle.requestCloseAsync();
  return cleanup;
}

function bindScopeListeners() {
  if (listeners) return;
  listeners = new AbortController();
  const signal = listeners.signal;
  document.addEventListener('openclank:widget-receipt-changed',()=>{if(widgetSaveStatusButton)widgetSaveStatusButton.hidden=!hasPendingWidgetReceipt();},{signal});
  document.addEventListener('openclank:auth-context-changed', event => {
    const account = String(event.detail?.accountId || '');
    if (!activeModel || account !== activeModel.accountScope) void retire('The authenticated account changed. Open the file again in Browser.', { closeWindow: true });
  }, { signal });
  document.addEventListener('openclank:file-policy-changed', () => {
    if (activeModel) void retire('Files access changed. Open the file again to create a fresh preview.', { closeWindow: true });
  }, { signal });
  window.addEventListener('workspace-change', event => {
    if (activeModel && normalizeScope(event.detail?.workspaceId) !== activeModel.workspaceScope) {
      void retire('The workspace changed. Open the file again in Browser.', { closeWindow: true });
    }
  }, { signal });
  window.addEventListener('openclank-files-resource-mutated', event => {
    if (activeModel && String(event.detail?.resourceRef || '') === activeModel.resourceRef) {
      void retire('This file changed. Open it again in Browser to refresh the preview.', { closeWindow: true });
    }
  }, { signal });
}

function openWidgetState() {
  const mount=frameMount, model=activeModel, expected=generation;
  const current=()=>!disposed && mount===frameMount && model===activeModel && expected===generation && model?.isCurrent() !== false;
  try {openOwnedWidgetState({mount,model,isCurrent:current});}catch(error){setMessage(error.message,true);}
}
async function checkWidgetSaveStatus() {
  try {
    const result=await checkWidgetReceipt();
    if(widgetSaveStatusButton)widgetSaveStatusButton.hidden=!hasPendingWidgetReceipt();
    const messages={none:'No widget Files receipt is pending.',saved:'Widget state copy saved.',saved_recovery_unavailable:'Widget state copy saved; recovery copy unavailable.',rejected:'Widget state copy was not saved.',pending:'Widget Files receipt is still pending.',unknown:'Widget Files outcome is unknown; recover the original operation before retrying.',unavailable:'The receipt belongs to another account or workspace.'};
    setMessage(messages[result.state] || 'Widget Files receipt unavailable.',result.state!=='saved');
  } catch(error) {setMessage(error.message || 'Widget Files receipt unavailable.',true);}
}

function buildWindow() {
  if (windowHandle?.root?.isConnected) return windowHandle;
  ensureStyles();
  disposed = false;
  const body = document.createElement('section');
  body.className = 'browser-applet';
  body.dataset.browserApplet = '';
  message = document.createElement('p');
  message.className = 'browser-applet-status';
  message.setAttribute('role', 'status');
  message.textContent = 'Choose an authorized HTML file in Files and select Open in Browser.';
  const viewport = document.createElement('div');
  viewport.className = 'browser-applet-viewport';
  retryButton = document.createElement('button');
  retryButton.type = 'button';
  retryButton.className = 'browser-applet-retry';
  retryButton.textContent = 'Retry connection';
  retryButton.hidden = true;
  retryButton.addEventListener('click', () => retryPreview());
  exportSourceButton = document.createElement('button');
  exportSourceButton.type = 'button'; exportSourceButton.textContent = 'Export source bundle (partial static)'; exportSourceButton.hidden = true;
  exportSourceButton.title = 'Static declarations only; runtime and dynamically loaded resources may be missing.';
  exportSourceButton.addEventListener('click', async () => {
    const captured = frameMount, expected = generation;
    if (!captured || !activeModel) return;
    exportSourceButton.disabled = true;
    try {
      await captured.downloadSourceBundle();
      if (captured === frameMount && expected === generation) setMessage('Source bundle downloaded. Coverage is partial static; runtime dependencies may be missing.');
    } catch (error) {
      if (captured === frameMount && expected === generation && error?.name !== 'AbortError') setMessage(error?.message || 'Source bundle is unavailable.',true);
    } finally { if (captured === frameMount && expected === generation && exportSourceButton) exportSourceButton.disabled = false; }
  });
  const widgetButton = document.createElement('button'); widgetButton.type='button'; widgetButton.textContent='Widget state…'; widgetButton.addEventListener('click',openWidgetState);
  widgetSaveStatusButton=document.createElement('button'); widgetSaveStatusButton.type='button'; widgetSaveStatusButton.textContent='Check widget save status'; widgetSaveStatusButton.hidden=!hasPendingWidgetReceipt(); widgetSaveStatusButton.addEventListener('click',()=>void checkWidgetSaveStatus());
  progressTools = document.createElement('details');
  progressTools.className = 'browser-applet-progress-tools';
  progressTools.hidden = true;
  progressSummary = document.createElement('summary');
  progressSummary.textContent = 'Progress and backups';
  progressControls = document.createElement('div');
  progressControls.className = 'browser-applet-progress-controls';
  progressStatus = document.createElement('p');
  progressStatus.className = 'browser-applet-progress-status';
  progressStatus.setAttribute('role', 'status');
  setProgressStatus('No declared report progress is connected.');
  exportJsonButton = document.createElement('button');
  exportJsonButton.type = 'button';
  exportJsonButton.textContent = 'Export JSON backup';
  exportJsonButton.addEventListener('click', () => downloadReportBackup('json'));
  exportMarkdownButton = document.createElement('button');
  exportMarkdownButton.type = 'button';
  exportMarkdownButton.textContent = 'Export Markdown backup';
  exportMarkdownButton.addEventListener('click', () => downloadReportBackup('markdown'));
  exportPreviousButton = document.createElement('button');
  exportPreviousButton.type = 'button';
  exportPreviousButton.textContent = 'Export previous progress JSON';
  exportPreviousButton.hidden = true;
  exportPreviousButton.addEventListener('click', () => downloadReportBackup('json', true));
  importBackupButton = document.createElement('button');
  importBackupButton.type = 'button';
  importBackupButton.textContent = 'Import progress backup';
  importBackupButton.addEventListener('click', () => importBackupInput?.click());
  importBackupInput = document.createElement('input');
  importBackupInput.type = 'file';
  importBackupInput.accept = '.json,.md,application/json,text/markdown';
  importBackupInput.hidden = true;
  importBackupInput.addEventListener('change', () => {
    const file = importBackupInput.files?.[0] || null;
    importBackupInput.value = '';
    if (file) void chooseReportBackup(file);
  });
  applyImportButton = document.createElement('button');
  applyImportButton.type = 'button';
  applyImportButton.textContent = 'Apply compatible items';
  applyImportButton.hidden = true;
  applyImportButton.addEventListener('click', () => void applyReportBackup());
  saveBackupToFilesButton = document.createElement('button');
  saveBackupToFilesButton.type = 'button';
  saveBackupToFilesButton.textContent = 'Save backup to Files…';
  saveBackupToFilesButton.hidden = true;
  saveBackupToFilesButton.addEventListener('click', () => void saveBackupInFiles());
  progressControls.append(progressStatus, exportJsonButton, exportMarkdownButton, exportPreviousButton, importBackupButton,
    applyImportButton, saveBackupToFilesButton, importBackupInput);
  progressTools.append(progressSummary, progressControls);
  body.append(message, retryButton, exportSourceButton, widgetButton, widgetSaveStatusButton, progressTools, viewport);

  windowHandle = createOpenClankWindow({
    id: WINDOW_ID, label: 'Browser', subtitle: 'Interactive Files preview',
    icon: 'globe', className: 'browser-applet-window', navigationChrome: false,
    minWidth: 720, minHeight: 480, chatVisibleMaximize: true,
    accountScope: document.body?.dataset?.accountId || null,
    workspaceScope: null,
    onBeforeClose: async () => {
      if (!disposed) {
        disposed = true;
        listeners?.abort(); listeners = null;
        await retire('Browser preview closed.');
        await connectionPanel?.close();
        connectionPanel = null;
      }
      return true;
    },
    onClosed: () => {
      disposed = true;
      listeners?.abort(); listeners = null;
      if (frameMount) { void frameMount.dispose(); frameMount = null; }
      if (connectionPanel) { void connectionPanel.close(); connectionPanel = null; }
      windowHandle = null;
      centeredTitle = null;
      message = null;
      retryButton = null; exportSourceButton = null;
      progressTools = null; progressStatus = null; importBackupInput = null;
      progressControls = null;
      progressSummary = null;
      exportJsonButton = null; exportMarkdownButton = null; importBackupButton = null;
      exportPreviousButton = null;
      applyImportButton = null; saveBackupToFilesButton = null;
    },
  });
  if (!windowHandle.body.contains(body)) windowHandle.body.replaceChildren(body);
  centeredTitle = document.createElement('div');
  centeredTitle.className = 'browser-applet-centered-title';
  centeredTitle.setAttribute('aria-live', 'polite');
  centeredTitle.setAttribute('role', 'heading');
  centeredTitle.setAttribute('aria-level', '2');
  centeredTitle.textContent = 'Browser';
  windowHandle.header.append(centeredTitle);
  connectionPanel = mountBrowserConnectionPanel(windowHandle.header, {
    currentSession: () => getCurrentChatId() || '',
    status: text => setMessage(text),
  });
  viewport.dataset.browserAppletViewport = '';
  frameMount = createPreviewFrameMount({
    owner:BROWSER_MOUNT_OWNER, getViewport:() => viewport,
    isCurrent:() => !disposed && Boolean(activeModel) && activeModel.isCurrent() !== false,
    onStatus:(text,bad,retry) => { setMessage(text,bad); if (retryButton) retryButton.hidden = !retry; },
    onProgressStatus:setProgressStatus,
    onProgress:(result,progress) => {
      if (result.type === 'register') { progressTools.hidden = false; progressTools.open = false; exportPreviousButton.hidden = !progress.hasPrevious; }
      if (result.type === 'imported') applyImportButton.hidden = true;
    },
    onBackup:file => { reportBackupFile = file; if (saveBackupToFilesButton) saveBackupToFilesButton.hidden = false; },
    onRetired:frameRetired,
  });
  viewport.before(frameMount.viewportControl);
  bindScopeListeners();
  return windowHandle;
}

function retryPreview() {
  if (!activeModel) return false;
  if (activeModel.isCurrent() === false) {
    setMessage('The selected Files resource changed. Reopen it from Files to preview the current revision.', true);
    retryButton.hidden = true;
    return false;
  }
  if (sessions.current) return frameMount?.retry() || false;
  void openPreview(activeModel);
  return true;
}

function setCanonicalRoute() {
  const path = appletPath('browser');
  if (`${location.pathname}${location.search}` !== path) history.pushState({ applet: 'browser' }, '', path);
}

async function openPreview(model) {
  cancelFilesImport();
  const handle = buildWindow();
  handle.setSubtitle('Interactive Files preview');
  handle.setTitle('Browser');
  handle.show(document.activeElement);
  setCanonicalRoute();
  ++generation;
  activeModel = Object.freeze({
    resourceRef: String(model.resourceRef || ''),
    accountScope: String(model.accountScope || document.body?.dataset?.accountId || ''),
    workspaceScope: normalizeScope(model.workspaceScope),
    revision: model.revision && typeof model.revision === 'object' ? Object.freeze({ ...model.revision }) : null,
    dependencies: Array.isArray(model.dependencies) ? model.dependencies.map(item => Object.freeze({ ...item })) : [],
    dependencyWarnings: Array.isArray(model.dependencyWarnings) ? model.dependencyWarnings.map(String).slice(0, 16) : [],
    title: String(model.title || 'HTML preview'),
    isCurrent: typeof model.isCurrent === 'function' ? model.isCurrent : () => true,
  });
  clearReportState();
  handle.inputContext.accountScope = activeModel.accountScope;
  handle.inputContext.workspaceScope = activeModel.workspaceScope;
  if (centeredTitle) centeredTitle.textContent = activeModel.title;
  const captured = frameMount, expected = generation;
  if (exportSourceButton) exportSourceButton.hidden = true;
  const opened = await captured.open(activeModel,{handoff:true});
  if (opened && captured === frameMount && expected === generation && exportSourceButton) { exportSourceButton.disabled = false; exportSourceButton.hidden = false; }
  return opened;
}

function open() {
  const handle = buildWindow();
  handle.show(document.activeElement);
  if (!sessions.current && !activeModel) {
    setMessage('Choose an authorized HTML file in Files and select Open in Browser.');
    retryButton.hidden = true;
  }
  return handle;
}

export default Object.freeze({ open, openPreview, close: () => retire('Browser preview closed.') });
