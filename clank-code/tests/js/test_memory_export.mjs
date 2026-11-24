import assert from 'node:assert/strict';
import test from 'node:test';

import {
  MEMORY_EXPORT_KINDS,
  MEMORY_EXPORT_SECTIONS,
  MEMORY_EXPORT_SECTION_CHOICES,
  MemoryExportFilterError,
  buildMemoryExportQuery,
  memoryAssetUrl,
  memoryExportFilename,
} from '../../static/js/util/memoryExport.js';

test('untouched defaults reproduce the pre-dialog one-click bundle export', () => {
  assert.equal(buildMemoryExportQuery(), '?format=bundle');
  assert.equal(buildMemoryExportQuery({}), '?format=bundle');
  assert.equal(buildMemoryExportQuery({ format: 'bundle' }), '?format=bundle');
  assert.equal(memoryExportFilename({}), 'open-clank-memory-bundle-v3.zip');
});

test('json format omits the format param entirely (absent = JSON server-side)', () => {
  assert.equal(buildMemoryExportQuery({ format: 'json' }), '');
  assert.equal(memoryExportFilename({ format: 'json' }), 'open-clank-memory-export.json');
});

test('sections and kinds assemble as comma-separated values', () => {
  const query = buildMemoryExportQuery({
    format: 'json',
    sections: ['curated', 'raw'],
    kinds: ['fact', 'wiki'],
  });
  const params = new URLSearchParams(query);
  assert.equal(params.get('sections'), 'curated,raw');
  assert.equal(params.get('kinds'), 'fact,wiki');
  assert.equal(params.get('format'), null);
});

test('date range, content match, and archived toggle map to S00 params', () => {
  const query = buildMemoryExportQuery({
    format: 'bundle',
    since: '2026-08-01',
    until: '2026-08-21T23:59:59Z',
    q: '  dentist  ',
    includeArchived: false,
  });
  const params = new URLSearchParams(query);
  assert.equal(params.get('format'), 'bundle');
  assert.equal(params.get('since'), '2026-08-01');
  assert.equal(params.get('until'), '2026-08-21T23:59:59Z');
  assert.equal(params.get('q'), 'dentist');
  assert.equal(params.get('include_archived'), 'false');
});

test('archived included and blank q stay omitted (backend defaults)', () => {
  const query = buildMemoryExportQuery({ includeArchived: true, q: '   ' });
  assert.equal(query, '?format=bundle');
});

test('asset modes and images-only shortcut assemble bundle-only params', () => {
  const params = new URLSearchParams(buildMemoryExportQuery({ assets: 'images', assetsOnly: true }));
  assert.equal(params.get('assets'), 'images');
  assert.equal(params.get('assets_only'), 'true');
  assert.equal(buildMemoryExportQuery({ assets: 'none' }), '?format=bundle&assets=none');
  // 'all' is the backend default and stays off the query string.
  assert.equal(buildMemoryExportQuery({ assets: 'all' }), '?format=bundle');
});

test('invalid values raise the typed error the backend would return as 422', () => {
  const cases = [
    [{ sections: [] }, 'sections must name at least one comma-separated value.'],
    [{ sections: ['curated', 'bogus'] }, 'sections must be chosen from:'],
    [{ kinds: [] }, 'kinds must name at least one comma-separated value.'],
    [{ since: 'not-a-date' }, 'since must be an ISO-8601 timestamp, e.g. 2026-08-21T00:00:00Z.'],
    [{ until: '2026-13-40' }, 'until must be an ISO-8601 timestamp, e.g. 2026-08-21T00:00:00Z.'],
    [{ assets: 'videos' }, 'assets must be all, images, or none.'],
    [{ format: 'json', assetsOnly: true }, 'assets_only requires format=bundle.'],
  ];
  for (const [options, message] of cases) {
    assert.throws(
      () => buildMemoryExportQuery(options),
      (error) => {
        assert.ok(error instanceof MemoryExportFilterError);
        assert.ok(error.message.includes(message), `${error.message} should include ${message}`);
        assert.deepEqual(error.problem, {
          code: 'MEMORY_EXPORT_FILTER_INVALID',
          message: error.message,
          retryable: false,
        });
        return true;
      },
    );
  }
});

test('section choices are a strict subset of the backend section set', () => {
  for (const choice of MEMORY_EXPORT_SECTION_CHOICES) {
    assert.ok(MEMORY_EXPORT_SECTIONS.includes(choice), `${choice} must be backend-valid`);
  }
  assert.ok(MEMORY_EXPORT_KINDS.length > 0);
});

test('asset URL is encoded and requires an id', () => {
  assert.equal(memoryAssetUrl('asset_ab12'), '/api/memory/assets/asset_ab12');
  assert.equal(memoryAssetUrl('asset x/y'), '/api/memory/assets/asset%20x%2Fy');
  assert.throws(() => memoryAssetUrl(''), /asset id is required/);
  assert.throws(() => memoryAssetUrl(null), /asset id is required/);
});
