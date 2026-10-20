import assert from 'node:assert/strict';
import test from 'node:test';
import { compareEntries, constrainSortSpec, entryIconKey, isDirectory, isTextPath, languageForPath, normalizeSortKeys, providerEntryMetadata, sortEntries } from '../../static/js/editor/entryModel.js';
import { boundedTextPreview, isPreviewable, previewKind, readBoundedTextResponse } from '../../static/js/editor/previewModel.js';

test('entry model keeps folders before files for both directions', () => {
  const entries = [
    { name: 'z.txt', kind: 'file' },
    { name: 'b-folder', kind: 'directory' },
    { name: 'a-folder', kind: 'directory' },
    { name: 'a.txt', kind: 'file' },
  ];
  assert.deepEqual(sortEntries(entries, { key: 'name', direction: 'asc' }).map(item => item.name), ['a-folder', 'b-folder', 'a.txt', 'z.txt']);
  assert.deepEqual(sortEntries(entries, { key: 'name', direction: 'desc' }).map(item => item.name), ['b-folder', 'a-folder', 'z.txt', 'a.txt']);
  assert.equal(isDirectory(entries[1]), true);
});

test('entry model uses stable identity to break display ties', () => {
  const entries = [
    { name: 'README', kind: 'file', resource_id: 'b' },
    { name: 'readme', kind: 'file', resource_id: 'a' },
  ];
  assert.equal(compareEntries(entries[0], entries[1], { key: 'name' }) > 0, true);
});

test('kind and modified sorting use provider metadata instead of generic file kind', () => {
  const entries = [
    { name: 'notes', kind: 'file', sort_kind: 'text', media_type: 'text/plain', modified_unix_ms: 30 },
    { name: 'photo', kind: 'file', media_type: 'image/png', modified_unix_ms: 10 },
    { name: 'bundle', kind: 'file', sort_kind: 'archive', media_type: 'application/zip', modified_unix_ms: 20 },
  ];
  assert.deepEqual(sortEntries(entries, { key: 'kind' }).map(item => item.name), ['bundle', 'photo', 'notes']);
  assert.deepEqual(sortEntries(entries, { key: 'modified' }).map(item => item.name), ['photo', 'bundle', 'notes']);
});

test('managed sort negotiation keeps every advertised mode and visibly falls back from unsupported keys', () => {
  assert.deepEqual(normalizeSortKeys(['modified', 'name', 'modified', 'bogus']), ['modified', 'name']);
  for (const key of ['name', 'kind', 'modified', 'size']) {
    for (const direction of ['asc', 'desc']) {
      assert.deepEqual(constrainSortSpec({ key, direction, directoriesFirst: false }, ['name', 'kind', 'modified', 'size']), {
        key, direction, directoriesFirst: false, collation: 'open-clank-v1',
      });
    }
  }
  assert.deepEqual(constrainSortSpec({ key: 'size', direction: 'desc' }, ['name', 'modified']), {
    key: 'name', direction: 'desc', directoriesFirst: true, collation: 'open-clank-v1',
  });
  assert.equal(constrainSortSpec({ key: 'modified', directories_first: false }, ['name', 'modified']).directoriesFirst, false);
});

test('entry icon association accepts MIME hints and directories', () => {
  assert.equal(entryIconKey({ name: 'photo.bin', kind: 'file', media_type: 'image/png' }), 'image');
  assert.equal(entryIconKey({ name: 'src', kind: 'directory' }), 'folder');
  assert.equal(entryIconKey({ name: 'main.rs', kind: 'file' }), 'rust');
  assert.equal(entryIconKey({ name: 'architecture.mmd', kind: 'file' }), 'mermaid');
});

test('preview model prefers server descriptors and keeps extension detection as a hint', () => {
  assert.equal(previewKind({ name: 'photo.bin', preview: { kind: 'image' } }), 'image');
  assert.equal(previewKind({ name: 'photo.png', preview: { allowed: false } }), null);
  assert.equal(previewKind({ name: 'recording.bin', media_type: 'audio/mpeg' }), 'audio');
  assert.equal(isPreviewable({ name: 'notes.md' }), true);
  assert.equal(isPreviewable({ name: 'archive.zip' }), false);
});

test('preview text hints share the editor language and common filename vocabulary', () => {
  const expected = new Map([
    ['settings.toml', 'TOML'], ['script.sh', 'Shell'], ['query.sql', 'SQL'], ['task.rb', 'Ruby'],
    ['index.php', 'PHP'], ['App.swift', 'Swift'], ['Program.cs', 'C#'], ['Main.kt', 'Kotlin'],
    ['Dockerfile', 'Dockerfile'], ['Makefile', 'Plain text'], ['README', 'Plain text'],
    ['.gitignore', 'Plain text'], ['.dockerignore', 'Plain text'], ['.editorconfig', 'Plain text'],
    ['.eslintrc.json', 'JSON'], ['.bashrc', 'Shell'],
    ['architecture.mmd', 'Mermaid'], ['workflow.mermaid', 'Mermaid'],
  ]);
  for (const [name, language] of expected) {
    assert.equal(languageForPath(name), language, name);
    assert.equal(isTextPath(name), true, name);
    assert.equal(previewKind({ name }), 'text', name);
  }
  assert.equal(isTextPath('archive.zip'), false);
  assert.equal(previewKind({ name: 'README', preview: { allowed: false } }), null, 'server descriptor remains authoritative');
});

test('managed provider metadata preserves timestamps, media semantics, and Copal text preview', () => {
  const iso = '2026-08-12T12:34:56Z';
  const naiveUtc = '2026-08-12T12:34:56';
  assert.deepEqual(providerEntryMetadata({ kind: 'note', ts: 1_700_000_000 }, 'copal'), {
    modified_unix_ms: 1_700_000_000_000,
    media_type: 'text/markdown',
    sort_kind: 'note',
    preview_kind: 'text',
  });
  assert.deepEqual(providerEntryMetadata({ url: '/api/generated-image/photo.png', updated_at: naiveUtc }, 'gallery'), {
    modified_unix_ms: Date.parse(iso),
    media_type: 'image/png',
    sort_kind: 'image',
  });
  assert.deepEqual(providerEntryMetadata({ language: 'pdf', updated_at: iso }, 'library'), {
    modified_unix_ms: Date.parse(iso),
    media_type: 'application/pdf',
    sort_kind: 'pdf',
  });
  assert.deepEqual(providerEntryMetadata({ filename: 'bundle.zip', mime_type: 'application/zip', created_at: iso }, 'published'), {
    modified_unix_ms: Date.parse(iso),
    media_type: 'application/zip',
    sort_kind: 'archive',
  });
});

test('bounded text preview preserves both ends with explicit truncation', () => {
  const result = boundedTextPreview('a'.repeat(100), { size: 100 }, 20);
  assert.equal(result.truncated, true);
  assert.match(result.text, /preview truncated/);
  assert.match(result.text, /^a+/);
  assert.match(result.text, /a+$/);
});

test('managed text preview retains a hard byte cap and cancels the remaining stream', async () => {
  let cancelled = false;
  const stream = new ReadableStream({
    start(controller) {
      controller.enqueue(new TextEncoder().encode('x'.repeat(400)));
    },
    cancel() { cancelled = true; },
  });
  const response = new Response(stream, {
    headers: { 'Content-Type': 'text/plain; charset=utf-8', 'Content-Length': '400' },
  });

  const result = await readBoundedTextResponse(response, { limit: 100 });

  assert.equal(result.bytesRead, 100);
  assert.equal(result.text.length, 100);
  assert.equal(result.truncated, true);
  assert.equal(result.tailIncluded, false);
  assert.equal(result.size, 400);
  assert.equal(cancelled, true);
});

test('managed text preview reports exact responses and refuses sampled binary', async () => {
  const exact = await readBoundedTextResponse(new Response('exact text', {
    headers: { 'Content-Type': 'text/plain; charset=utf-8' },
  }), { limit: 100 });
  assert.deepEqual(
    { text: exact.text, truncated: exact.truncated, bytesRead: exact.bytesRead, size: exact.size },
    { text: 'exact text', truncated: false, bytesRead: 10, size: 10 },
  );

  await assert.rejects(
    readBoundedTextResponse(new Response(new Uint8Array([65, 1, 66])), { limit: 100 }),
    /appears to be binary/,
  );
});

test('managed text preview cancellation reaches the provider stream', async () => {
  let cancelled = false;
  const stream = new ReadableStream({
    pull() { return new Promise(() => {}); },
    cancel() { cancelled = true; },
  });
  const controller = new AbortController();
  const preview = readBoundedTextResponse(new Response(stream), { signal: controller.signal });
  controller.abort();

  await assert.rejects(preview, { name: 'AbortError' });
  assert.equal(cancelled, true);
});
