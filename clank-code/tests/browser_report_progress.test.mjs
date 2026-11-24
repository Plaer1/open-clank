import test from 'node:test';
import assert from 'node:assert/strict';
import {
  REPORT_CHANNEL, createProgressStore, makeBackupFile, makeProgressEnvelope,
  parseBackupFile, validateProgressEnvelope, validateReportRegistration,
  validateSavedProgressEnvelope,
} from '../static/js/browser/reportProgress.js';
import { createReportProgressController } from '../static/js/browser/reportProgressController.js';

const digest = value => `sha256:${value.repeat(64)}`;
const session = { session_id: 'session-a', generation: 7, digest: digest('a') };
const registration = validateReportRegistration({
  channel: REPORT_CHANNEL, type: 'register', session_id: session.session_id, generation: session.generation,
  report_id: 'lab-report', source_digest: session.digest, schema_version: 2,
  items: [{ id: 'question-1', digest: digest('b') }],
}, session);
const progress = makeProgressEnvelope(registration, [{ id: 'question-1', digest: digest('b'), value: { answer: '```' } }]);

test('registration binds report progress to the exact preview digest and generation', () => {
  assert.throws(() => validateReportRegistration({
    channel: REPORT_CHANNEL, type: 'register', session_id: 'other', generation: 7,
    report_id: 'lab-report', source_digest: session.digest, schema_version: 2,
    items: [{ id: 'question-1', digest: digest('b') }],
  }, session), /registration is invalid/);
});

test('JSON and Markdown data backups round-trip without executing content', async () => {
  for (const format of ['json', 'markdown']) {
    const file = makeBackupFile(progress, format, new Date('2026-10-28T12:00:00.000Z'));
    const parsed = await parseBackupFile(file);
    assert.deepEqual(parsed, progress);
  }
});

test('unknown IDs and changed source or item digests stay visible as conflicts', () => {
  const checked = validateProgressEnvelope({
    ...progress, source_digest: digest('c'), items: [
      { id: 'removed-question', digest: digest('d'), value: 'old answer' },
      { id: 'question-1', digest: digest('e'), value: 'changed question answer' },
    ],
  }, registration, { allowSourceDigestChange: true });
  assert.deepEqual(checked.conflicts.map(item => item.reason), ['changed_source_digest', 'unknown_item', 'changed_item_digest']);
  assert.deepEqual(checked.accepted, []);
});

test('local progress storage survives module instances and corrupt data is not loaded', async () => {
  const rows = new Map();
  const storage = {
    get length() { return rows.size; },
    key: index => [...rows.keys()][index] ?? null,
    getItem: key => rows.get(key) ?? null,
    setItem: (key, value) => rows.set(key, value),
    removeItem: key => rows.delete(key),
  };
  const firstStore = createProgressStore(storage);
  assert.equal(firstStore.save('artifact/report:v2:digest-a', progress), true);
  assert.deepEqual(createProgressStore(storage).load('artifact/report:v2:digest-a'), progress);
  await new Promise(resolve => setTimeout(resolve, 2));
  firstStore.save('artifact/report:v2:digest-b', { ...progress, source_digest:digest('c') });
  assert.deepEqual(firstStore.list('artifact/report').map(row => row.key), ['artifact/report:v2:digest-b', 'artifact/report:v2:digest-a']);
  firstStore.prune('artifact/report', 1);
  assert.equal(firstStore.load('artifact/report:v2:digest-a'), null);
  rows.set('broken', '{');
  assert.equal(createProgressStore(storage).load('broken'), null);
});

test('saved progress from an older digest remains exportable as an explicit backup', () => {
  const older = { ...progress, source_digest:digest('c') };
  assert.deepEqual(validateSavedProgressEnvelope(older), older);
  const file = makeBackupFile(older, 'json');
  assert.equal(file.name, 'lab-report-progress.json');
});

test('malformed and oversized backups are rejected', async () => {
  await assert.rejects(parseBackupFile(new File(['not json'], 'x.json', { type: 'application/json' })), /malformed/);
  await assert.rejects(parseBackupFile(new File(['x'.repeat(512 * 1024 + 10)], 'large.json')), /size limit/);
});

test('a backup finishing after preview replacement cannot bind to the new report', async () => {
  const source = { id: 'question-1', digest: digest('b'), value: { answer: 'old' } };
  const firstSession = { session_id: 'session-old', artifact_id: 'a'.repeat(32), digest: digest('a') };
  const nextSession = { session_id: 'session-next', artifact_id: 'b'.repeat(32), digest: digest('c') };
  const frameWindow = {};
  let current = { session: firstSession, generation: 1, active: true, disposed: false, frameWindow,
    origin: 'http://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.artifacts.localhost:3456' };
  const controller = createReportProgressController({ context: () => current, post: () => true, status: () => {}, onBackup: () => {} });
  controller.receive({ source: frameWindow, origin: current.origin, data: {
    channel: REPORT_CHANNEL, type: 'register', session_id: firstSession.session_id, generation: 1,
    report_id: 'lab-report', source_digest: firstSession.digest, schema_version: 2,
    items: [{ id: 'question-1', digest: digest('b') }],
  } });
  const contents = JSON.stringify({ ...progress, items: [source] });
  let release;
  const file = { name: 'backup.json', type: 'application/json', size: contents.length,
    text: () => new Promise(resolve => { release = () => resolve(contents); }) };
  const pending = controller.importBackup(file);
  await Promise.resolve();
  current = { ...current, session: nextSession, generation: 2,
    origin: 'http://bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.artifacts.localhost:3456' };
  release();
  await assert.rejects(pending, /preview changed/);
  assert.equal(controller.hasBackup, false);
  assert.equal(controller.hasPendingImport, false);
});
