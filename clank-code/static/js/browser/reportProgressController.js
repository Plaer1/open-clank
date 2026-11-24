import {
  REPORT_CHANNEL, createProgressStore, makeBackupFile, makeProgressEnvelope,
  parseBackupFile, validateProgressEnvelope, validateReportRegistration, validateSavedProgressEnvelope,
} from './reportProgress.js';

function keyFor(session, registration) {
  return `openclank.browser.progress.v1:${session.artifact_id}:${registration.reportId}:${registration.schemaVersion}:${session.digest.replace(/^sha256:/, '')}`;
}

function prefixFor(session, registration) {
  return `openclank.browser.progress.v1:${session.artifact_id}:${registration.reportId}:`;
}

export function createReportProgressController({ context, post, status, onBackup }) {
  let registration = null;
  let store = null;
  let storageKey = '';
  let storagePrefix = '';
  let previousEnvelope = null;
  let backupFile = null;
  let pendingImport = null;
  let initializedSession = '';
  let restored = false;

  function reset() {
    registration = null; store = null; storageKey = ''; storagePrefix = '';
    previousEnvelope = null; backupFile = null; pendingImport = null;
    initializedSession = ''; restored = false;
  }

  function initialize(session, generation) {
    const current = context();
    if (!session || current.session?.session_id !== session.session_id || current.generation !== generation
      || !current.active || !current.frameWindow || initializedSession === session.session_id) return;
    initializedSession = session.session_id;
    post('host-ready', { artifact_id: session.artifact_id, source_digest: session.digest });
  }

  function receive(event) {
    const current = context();
    const session = current.session;
    if (!session || !current.active || !current.frameWindow || event.source !== current.frameWindow
      || event.origin !== current.origin || current.generation < 1 || current.disposed) return;
    const data = event.data;
    if (!data || data.channel !== REPORT_CHANNEL || data.session_id !== session.session_id || data.generation !== current.generation) return;
    try {
      if (data.type === 'register') {
        registration = validateReportRegistration(data, { ...session, generation: current.generation });
        storagePrefix = prefixFor(session, registration);
        storageKey = keyFor(session, registration);
        store = createProgressStore();
        const stored = store.load(storageKey);
        const previous = store.list(storagePrefix).find(row => row.key !== storageKey);
        if (previous) {
          try {
            const envelope = validateSavedProgressEnvelope(previous.envelope);
            if (envelope.report_id === registration.reportId) previousEnvelope = envelope;
          } catch { /* Leave corrupt snapshots untouched and keep this report usable. */ }
        }
        const saved = stored && validateProgressEnvelope(stored, registration);
        if (saved && !saved.conflicts.length) {
          restored = true;
          post('progress-restore', { report_id: registration.reportId, progress: saved.normalized });
          status(`Local progress restored · ${saved.accepted.length} items.`);
        } else if (saved) {
          status(`Saved local progress conflicts with this report (${saved.conflicts.length}); export it or import a compatible backup.`);
        } else {
          status(previousEnvelope ? 'Previous local progress has a different digest/schema and remains available as a JSON backup.' : 'Report connected · no local progress saved yet.');
        }
        return { type: 'register', hasPrevious: Boolean(previousEnvelope) };
      }
      if (!registration || data.report_id !== registration.reportId) return null;
      if (data.type === 'progress-save') {
        if (data.source_digest !== registration.sourceDigest || data.schema_version !== registration.schemaVersion) throw new Error('Progress source digest or schema version does not match this report.');
        const envelope = makeProgressEnvelope(registration, data.items);
        if (store?.save(storageKey, envelope) !== true) throw new Error('Local browser storage is unavailable; export the progress before closing.');
        store.prune(storagePrefix, 10);
        post('progress-saved', { report_id: registration.reportId });
        status(`${restored ? 'Local progress restored and saved' : 'Local progress saved'} · ${envelope.items.length} items · not exported or imported to Files.`);
        return { type: 'saved' };
      }
      if (data.type === 'progress-import-result' && data.accepted === true) {
        if (data.source_digest !== registration.sourceDigest || data.schema_version !== registration.schemaVersion) throw new Error('Imported progress result does not match this report.');
        const envelope = makeProgressEnvelope(registration, data.items);
        if (!store?.save(storageKey, envelope)) throw new Error('Imported progress could not be saved locally.');
        status(`Progress imported into this report and saved locally · ${envelope.items.length} items · not imported to Files.`);
        pendingImport = null;
        return { type: 'imported' };
      }
    } catch (error) {
      status(error?.message || 'Report progress message was rejected.');
    }
    return null;
  }

  function exportBackup(format, usePrevious = false) {
    if (!registration || !store || !storageKey) throw new Error('No declared report progress is connected.');
    const stored = usePrevious ? previousEnvelope : store.load(storageKey);
    if (!stored) throw new Error('Save report progress locally before exporting a backup.');
    const saved = validateSavedProgressEnvelope(stored);
    if (saved.report_id !== registration.reportId) throw new Error('Saved progress belongs to another report.');
    const stale = saved.source_digest !== registration.sourceDigest || saved.schema_version !== registration.schemaVersion;
    backupFile = makeBackupFile(saved, format);
    onBackup(backupFile);
    status(`${stale || usePrevious ? 'Saved local progress backup' : `${format.toUpperCase()} backup`} download started · not imported to Files${stale || usePrevious ? ' · source differs from the current report' : ''}.`);
    return backupFile;
  }

  async function importBackup(file) {
    pendingImport = null;
    if (!registration) throw new Error('No declared report progress is connected.');
    const captured = context();
    const capturedRegistration = registration;
    const capturedKey = storageKey;
    const envelope = await parseBackupFile(file);
    const current = context();
    if (current.disposed || current.generation !== captured.generation || current.session?.session_id !== captured.session?.session_id
      || registration !== capturedRegistration || storageKey !== capturedKey) throw new Error('The preview changed while the backup was being read. Choose the backup again.');
    const checked = validateProgressEnvelope(envelope, capturedRegistration, { allowSourceDigestChange: true });
    backupFile = file;
    onBackup(file);
    pendingImport = checked;
    const conflictSummary = checked.conflicts.length
      ? ` Conflicts: ${checked.conflicts.slice(0, 8).map(item => `${item.id} (${item.reason})`).join(', ')}${checked.conflicts.length > 8 ? `, and ${checked.conflicts.length - 8} more` : ''}.` : '';
    status(`${checked.accepted.length} compatible items found.${conflictSummary} Review, then choose Apply compatible items to the report.`);
    return { accepted: checked.accepted.length, conflicts: checked.conflicts.length };
  }

  async function apply(confirmConflicts) {
    if (!pendingImport || !registration) return false;
    const captured = context();
    const capturedPending = pendingImport;
    const capturedRegistration = registration;
    const accepted = capturedPending.accepted;
    if (capturedPending.conflicts.length && await confirmConflicts(capturedPending.conflicts) !== true) return false;
    const current = context();
    if (current.generation !== captured.generation || current.disposed || !current.frameWindow || current.session?.session_id !== captured.session?.session_id) return false;
    if (registration !== capturedRegistration || pendingImport !== capturedPending) return false;
    post('progress-import', { report_id: capturedRegistration.reportId, items: accepted,
      conflicts: capturedPending.conflicts.map(item => ({ id: item.id, reason: item.reason })) });
    status(`Sent ${accepted.length} compatible items to the report · waiting for its confirmation.`);
    return true;
  }

  return Object.freeze({ reset, initialize, receive, exportBackup, importBackup, apply,
    get hasPrevious() { return Boolean(previousEnvelope); },
    get hasPendingImport() { return Boolean(pendingImport?.accepted?.length); },
    get hasBackup() { return Boolean(backupFile); },
    get registration() { return registration; },
    get backupFile() { return backupFile; },
  });
}
