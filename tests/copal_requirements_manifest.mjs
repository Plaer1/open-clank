#!/usr/bin/env node

/**
 * S28 evidence contract for the original 37 requirements plus A01–A13.
 *
 * This is deliberately a receipt validator. A source path is not proof that
 * a scenario ran: complete rows need a zero-exit receipt whose product-scope
 * digest still matches the current checkout. Partial and deferred rows remain
 * visible until their missing evidence is collected.
 */
import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const repo = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const ledgerPath = path.join(repo, '.clankers/robonotes/COPAL-QOL-S26-S28-INTEGRATION-QA-2026-09-07.md');
const ledger = fs.readFileSync(ledgerPath, 'utf8');
const receiptPath = path.join(repo, 'tests/fixtures/copal_requirements_receipts.json');
assert(fs.existsSync(receiptPath), 'checked-in receipt fixture is required');
const receiptFixture = JSON.parse(fs.readFileSync(receiptPath, 'utf8'));
// Schema 4 adds the checkout-wide productDigest. Older fixtures are useful
// historical input, but are never proof of the current product and remain
// inspectable as pending evidence.
const RECEIPT_SCHEMA_VERSION = 4;
const receiptDocument = receiptFixture && typeof receiptFixture === 'object' && !Array.isArray(receiptFixture)
  ? receiptFixture : {};
const receipts = receiptDocument && typeof receiptDocument.receipts === 'object'
  && !Array.isArray(receiptDocument.receipts) ? receiptDocument.receipts : {};
const metaplan = '.clanker/futures/COPAL-QOL-METAPLAN-2026-09-04.md';
const amendment = '.clanker/futures/COPAL-QOL-2026-09-04/AMENDMENT-2026-09-07-SETTINGS-THEMES-EDITOR.md';

const ids = [
  'E01','E02','E03','E04','E05','E06','E07','E08','E09','E10',
  'T01','T02','T03','T04','T05', 'V01','V02','V03','V04',
  'M01','M02','M04', 'H01','H02','H03','H04','H05',
  'P01','P02','P03','P04','P05', 'TH01','TH02','TH03','TH04','TH05',
  'A01','A02','A03','A04','A05','A06','A07','A08','A09','A10','A11','A12','A13',
];

const originalEvidence = {
  E01:['tests/copal_browser_disposable_acceptance.mjs','node tests/copal_browser_disposable_acceptance.mjs','mounted-editor-tabs'], E02:['tests/copal_browser_disposable_acceptance.mjs','node tests/copal_browser_disposable_acceptance.mjs','explorer-refresh-anchor'],
  E03:['tests/copal_browser_disposable_acceptance.mjs','node tests/copal_browser_disposable_acceptance.mjs','input-ownership'], E04:['tests/copal_markdown_media_acceptance.mjs','node tests/copal_markdown_media_acceptance.mjs','managed-host-media'],
  E05:['tests/copal_base_performance_acceptance.mjs','node tests/copal_base_performance_acceptance.mjs','typed-base-query'], E06:['tests/copal_mounted_editor_memes_acceptance.mjs','node tests/copal_mounted_editor_memes_acceptance.mjs','rename-move-resource-ref'],
  E07:['tests/copal_markdown_media_acceptance.mjs','node tests/copal_markdown_media_acceptance.mjs','source-preservation'], E08:['tests/copal_browser_disposable_acceptance.mjs','node tests/copal_browser_disposable_acceptance.mjs','quick-open-command-search-navigation'],
  E09:['tests/copal_browser_disposable_acceptance.mjs','node tests/copal_browser_disposable_acceptance.mjs','panels-typed-properties'], E10:['tests/copal_browser_disposable_acceptance.mjs','node tests/copal_browser_disposable_acceptance.mjs','draft-save-cas-external-change'],
  T01:['tests/copal_timeline_geometry_acceptance.mjs','node tests/copal_timeline_geometry_acceptance.mjs','nested-track-grid'], T02:['tests/copal_timeline_geometry_acceptance.mjs','node tests/copal_timeline_geometry_acceptance.mjs','single-day-event-bounds'],
  T03:['tests/copal_timeline_geometry_acceptance.mjs','node tests/copal_timeline_geometry_acceptance.mjs','timeline-geometry'], T04:['tests/copal_timeline_geometry_acceptance.mjs','node tests/copal_timeline_geometry_acceptance.mjs','date-shading'],
  T05:['tests/copal_timeline_geometry_acceptance.mjs','node tests/copal_timeline_geometry_acceptance.mjs','date-anchor-inclusive-end'], V01:['tests/copal_browser_disposable_acceptance.mjs','node tests/copal_browser_disposable_acceptance.mjs','wiki-round-trip'],
  V02:['tests/test_copal_routes.py','PYTHONDONTWRITEBYTECODE=1 venv/bin/python -m pytest -q -p no:cacheprovider tests/test_copal_routes.py','memes-round-trip'], V03:['tests/copal_graph_modes_acceptance.mjs','node tests/copal_graph_modes_acceptance.mjs','graph-galaxy-modes'],
  V04:['tests/copal_tasks_5k_browser_acceptance.mjs','node tests/copal_tasks_5k_browser_acceptance.mjs','unopened-note-task-writeback'], M01:['tests/copal_context_menu_browser_acceptance.mjs','node tests/copal_context_menu_browser_acceptance.mjs','editing-clipboard-spelling'],
  M02:['tests/copal_context_menu_production_browser_acceptance.mjs','node tests/copal_context_menu_production_browser_acceptance.mjs','context-actions'], M04:['tests/copal_context_menu_codemirror_browser_acceptance.mjs','node tests/copal_context_menu_codemirror_browser_acceptance.mjs','keyboard-dismiss-focus-disable'],
  H01:['tests/history_settings_production_browser_acceptance.mjs','node tests/history_settings_production_browser_acceptance.mjs','lore-history-worker'], H02:['tests/test_manage_copal.py','PYTHONDONTWRITEBYTECODE=1 venv/bin/python -m pytest -q -p no:cacheprovider tests/test_manage_copal.py','owner-hooks-provenance'],
  H03:['tests/history_settings_production_browser_acceptance.mjs','node tests/history_settings_production_browser_acceptance.mjs','budget-pause-unavailable'], H04:['packages/openclank-history/tests/restore_contract.rs','CARGO_BUILD_JOBS=1 CARGO_INCREMENTAL=0 cargo test --manifest-path packages/openclank-history/Cargo.toml --locked --offline --features qualification-fixtures --test restore_contract','restore-cas-recovery'],
  H05:['packages/openclank-history/tests/service_subprocess.rs','CARGO_BUILD_JOBS=1 CARGO_INCREMENTAL=0 cargo test --manifest-path packages/openclank-history/Cargo.toml --locked --offline --features qualification-fixtures --test service_subprocess','platform-deferred-retention'], P01:['tests/copal_mounted_editor_memes_acceptance.mjs','node tests/copal_mounted_editor_memes_acceptance.mjs','files-editor-lifecycle'],
  P02:['tests/treehouse_field_guide_browser_acceptance.mjs','node tests/treehouse_field_guide_browser_acceptance.mjs','all-17-disposable-lessons'], P03:['tests/copal_browser_disposable_acceptance.mjs','node tests/copal_browser_disposable_acceptance.mjs','responsive-accessible-settings'],
  P04:['tests/test_copal_redb_production_performance.py','PYTHONDONTWRITEBYTECODE=1 venv/bin/python -m pytest -q -p no:cacheprovider tests/test_copal_redb_production_performance.py','authenticated-redb-index-query'], P05:['tests/copal_mounted_editor_memes_acceptance.mjs','node tests/copal_mounted_editor_memes_acceptance.mjs','memes-treehouse-package-round-trip'],
  TH01:['tests/treehouse_integrated_browser_acceptance.mjs','node tests/treehouse_integrated_browser_acceptance.mjs','learner-admin-separation'], TH02:['tests/treehouse_integrated_browser_acceptance.mjs','node tests/treehouse_integrated_browser_acceptance.mjs','progress-badges-achievements'],
  TH03:['tests/treehouse_integrated_browser_acceptance.mjs','node tests/treehouse_integrated_browser_acceptance.mjs','scoped-reset'], TH04:['tests/treehouse_field_guide_browser_acceptance.mjs','node tests/treehouse_field_guide_browser_acceptance.mjs','field-guide-mounted-courses'], TH05:['tests/treehouse_integrated_browser_acceptance.mjs','node tests/treehouse_integrated_browser_acceptance.mjs','private-share-revoke-progress'],
};
const amendmentEvidence = {
  A01:['tests/theme_browser_acceptance.mjs','node tests/theme_browser_acceptance.mjs','pink-default-custom-preservation'], A02:['tests/test_agent_actor_accounting.py','PYTHONDONTWRITEBYTECODE=1 venv/bin/python -m pytest -q -p no:cacheprovider tests/test_agent_actor_accounting.py','tool-availability-enablement'],
  A03:['tests/test_reminder_endpoints.py','PYTHONDONTWRITEBYTECODE=1 venv/bin/python -m pytest -q -p no:cacheprovider tests/test_reminder_endpoints.py','multiple-endpoint-retention-retry'], A04:['tests/model_labels.mjs','node --test tests/model_labels.mjs tests/model_catalog_identity.mjs','shared-provider-label'],
  A05:['tests/clanker_browser_acceptance.mjs','node tests/clanker_browser_acceptance.mjs','compact-hamburger-right'], A06:['tests/kene_browser_acceptance.mjs','node tests/kene_browser_acceptance.mjs','kene-route-speed-lifecycle'],
  A07:['tests/theme_browser_acceptance.mjs','node tests/theme_browser_acceptance.mjs','settings-themes-customize'], A08:['tests/theme_browser_acceptance.mjs','node tests/theme_browser_acceptance.mjs','theme-account-bleed-lag'],
  A09:['tests/i18n_browser_acceptance.mjs','node tests/i18n_browser_acceptance.mjs','locale-runtime-current-strings'], A10:['tests/test_agent_actor_accounting.py','PYTHONDONTWRITEBYTECODE=1 venv/bin/python -m pytest -q -p no:cacheprovider tests/test_agent_actor_accounting.py','requested-effective-subagent-model'],
  A11:['tests/js/test_copal_notes_buffers.mjs','node --test tests/js/test_copal_notes_buffers.mjs','template-discovery-insert-variables'], A12:['tests/copal_multicursor_browser_acceptance.mjs','node tests/copal_multicursor_browser_acceptance.mjs','parser-rich-comments-source-bytes'],
  A13:['tests/copal_multicursor_browser_acceptance.mjs','node tests/copal_multicursor_browser_acceptance.mjs','multi-range-multiline-editing'],
};
const combinedJourney = {
  source: amendment + '#P0-combined-mounted-50-feature-journey',
  file: 'tests/copal_combined_50_browser_acceptance.mjs',
  command: 'node tests/copal_combined_50_browser_acceptance.mjs',
  // The combined runner shells out to these child journeys. Include their
  // bytes in its digest so a receipt cannot survive a child-test change.
  evidenceFiles: [
    'tests/theme_browser_acceptance.mjs',
    'tests/model_labels.mjs',
    'tests/provider_add_models_browser_acceptance.mjs',
    'tests/copal_host_template_provider_browser_acceptance.mjs',
    'tests/copal_multicursor_browser_acceptance.mjs',
    'tests/copal_mounted_editor_memes_acceptance.mjs',
    'tests/history_settings_production_browser_acceptance.mjs',
    'tests/treehouse_field_guide_browser_acceptance.mjs',
    'tests/i18n_browser_acceptance.mjs',
    'tests/reminder_settings_production_browser_acceptance.mjs',
  ],
  assertions: [
    'theme-pink-default', 'hamburger-right', 'reminders-retained',
    'shared-provider-label', 'modelled-children', 'template-source',
    'multicursor-multiline', 'rich-comments-source', 'files-history-treehouse',
    'locale-account-reload-persistence',
  ],
  runtime: 'browser',
  environment: { backend: 'fixture-or-route', browser: 'required', platform: 'current-host', external: 'stubbed-or-none' },
  receipt: receiptDocument.combinedJourney || null,
};

const evidence = Object.fromEntries(ids.map((id) => {
  const item = originalEvidence[id] || amendmentEvidence[id];
  assert(item, `missing evidence contract for ${id}`);
  const [file, command, assertion] = item;
  const evidenceText = fs.readFileSync(path.join(repo, file), 'utf8');
  const runtime = file.endsWith('.rs') || command.startsWith('CARGO_') || command.includes(' cargo test')
    ? 'backend'
    : command.startsWith('PYTHON')
      ? 'python'
      : (file.endsWith('.mjs') && (file.includes('acceptance') || /\bwithCopalBrowser\b/u.test(evidenceText)))
        ? 'browser'
        : 'node';
  const evidenceFiles = id === 'A04' ? ['tests/model_catalog_identity.mjs'] : [];
  return [id, { source: (id.startsWith('A') ? amendment : metaplan) + `#${id}`, file, command, assertions: [assertion], evidenceFiles, runtime, environment: { backend: runtime === 'backend' ? 'required' : 'fixture-or-route', browser: runtime === 'browser' ? 'required' : 'not-applicable', platform: id === 'H05' ? 'deferred-platform' : 'current-host', external: 'stubbed-or-none' }, receipt: receipts[id] || null }];
}));

const contractStart = ledger.indexOf('## 50-ID evidence contract');
assert(contractStart >= 0, 'ledger must contain the authoritative 50-ID evidence contract');
const rows = new Map();
for (const line of ledger.slice(contractStart).split('\n')) {
  const match = line.match(/^\|\s*([A-Z]+\d+)\s*\|\s*(pass|partial|queued|blocked|deferred-by-platform)\s*\|\s*(.*?)\s*\|\s*$/);
  if (match) {
    assert(!rows.has(match[1]), `duplicate authoritative evidence row: ${match[1]}`);
    rows.set(match[1], { status: match[2], detail: match[3] });
  }
}
assert.equal(rows.size, ids.length, `authoritative ledger must contain exactly ${ids.length} rows`);
assert.deepEqual([...rows.keys()].sort(), [...ids].sort(), 'authoritative ledger IDs must match the canonical 50-ID set');

// The product scope is intentionally explicit. Walking these first-party
// roots (instead of asking Git for tracked files) includes an untracked source
// file during a review, while the exclusions keep generated/test output and
// user/live state out of release evidence. The evidence file is added per row
// below, so changing a test also stales its receipt.
const productScopeRoots = [
  'app.py', 'launcher.py', 'openclank_entry.py', 'setup.py', 'pyproject.toml',
  'package.json', 'package-lock.json', 'requirements.txt', 'requirements-optional.txt',
  'companion', 'config', 'core', 'mcp_servers', 'routes', 'scripts', 'services', 'src', 'static',
  'packages/Copal/app.py', 'packages/Copal/copal.toml', 'packages/Copal/components.json',
  'packages/Copal/eslint.config.mjs', 'packages/Copal/next.config.ts', 'packages/Copal/package.json',
  'packages/Copal/postcss.config.mjs', 'packages/Copal/prisma', 'packages/Copal/public', 'packages/Copal/public/data/move-data.json',
  'packages/Copal/scripts', 'packages/Copal/src', 'packages/Copal/tailwind.config.ts',
  'packages/Copal/servo-shell/Cargo.toml', 'packages/Copal/servo-shell/src',
  'packages/Copal/tsconfig.json', 'packages/Copal/ui',
  'packages/Copal/rust/copal-db/Cargo.toml', 'packages/Copal/rust/copal-db/src',
  'packages/odysseus-files/Cargo.toml', 'packages/odysseus-files/Cargo.lock', 'packages/odysseus-files/build.rs', 'packages/odysseus-files/proto', 'packages/odysseus-files/requirements-grpc-codegen.txt', 'packages/odysseus-files/src',
  'packages/openclank-agent-supervisor/Cargo.toml', 'packages/openclank-agent-supervisor/Cargo.lock', 'packages/openclank-agent-supervisor/build.rs', 'packages/openclank-agent-supervisor/proto', 'packages/openclank-agent-supervisor/requirements-grpc-codegen.txt', 'packages/openclank-agent-supervisor/src',
  'packages/openclank-history/Cargo.toml', 'packages/openclank-history/Cargo.lock',
  'packages/openclank-history/src',
  'packages/mimo-code/package.json', 'packages/mimo-code/bun.lock', 'packages/mimo-code/tsconfig.json',
  'packages/mimo-code/packages/opencode/package.json', 'packages/mimo-code/packages/opencode/bunfig.toml', 'packages/mimo-code/packages/opencode/Dockerfile', 'packages/mimo-code/packages/opencode/drizzle.config.ts', 'packages/mimo-code/packages/opencode/parsers-config.ts',
  'packages/mimo-code/packages/opencode/tsconfig.json', 'packages/mimo-code/packages/opencode/src', 'packages/mimo-code/packages/opencode/migration', 'packages/mimo-code/packages/opencode/script',
  'packages/mimo-code/packages/shared/package.json', 'packages/mimo-code/packages/shared/tsconfig.json',
  'packages/mimo-code/packages/shared/src', 'packages/mimo-code/packages/plugin/package.json',
  'packages/mimo-code/packages/plugin/tsconfig.json', 'packages/mimo-code/packages/plugin/src',
  'packages/mimo-code/packages/sdk/js/package.json', 'packages/mimo-code/packages/sdk/js/tsconfig.json',
  'packages/mimo-code/packages/sdk/js/src', 'services/hwfit/data/hf_models.json',
  // The disposable wrapper is an executable evidence boundary. It is added
  // as the per-row evidence file below, without admitting unrelated tests.
  'tests/copal_browser_disposable_acceptance.mjs',
  // Shared browser harness code is executable evidence for every browser row.
  'tests/helpers/copal_browser_fixture.mjs',
  // The validator and pytest collection hooks are part of the evidence
  // boundary. Changing what a receipt means or how a mapped Python suite is
  // collected must invalidate previously recorded qualification.
  'tests/copal_requirements_manifest.mjs', 'tests/conftest.py', 'tests/_taxonomy.py',
];
const ignoredDirectory = (name) => new Set([
  '.archive', '.codebase-memory', '.copal', '.git', '.mimocode', '.next', '.pytest_cache',
  '.references', '.ruff_cache', '.venv', '__pycache__', 'backups', 'build',
  'cache', 'coverage', 'data', 'db', 'dist', 'logs', 'node_modules', 'out',
  'target', 'venv', '.turbo', '.parcel-cache', '.vite',
]).has(name) || name.startsWith('.node_modules.bak-') || name.startsWith('.mimocode-test-fixtures-');
const scopeFiles = new Set();
const collectScopeFiles = (relative) => {
  const absolute = path.join(repo, relative);
  if (!fs.existsSync(absolute)) return;
  const stat = fs.lstatSync(absolute);
  if (stat.isFile()) {
    scopeFiles.add(relative);
    return;
  }
  if (!stat.isDirectory()) return;
  for (const entry of fs.readdirSync(absolute, { withFileTypes: true }).sort((a, b) => a.name < b.name ? -1 : a.name > b.name ? 1 : 0)) {
    if (entry.isDirectory() && ignoredDirectory(entry.name)) continue;
    collectScopeFiles(path.join(relative, entry.name));
  }
};
for (const root of productScopeRoots) collectScopeFiles(root);
const productScopeDigest = (evidenceFile, additionalEvidenceFiles = []) => {
  const files = [...new Set([...scopeFiles, evidenceFile, ...additionalEvidenceFiles])].sort();
  const digest = crypto.createHash('sha256');
  for (const relative of files) {
    const absolute = path.join(repo, relative);
    if (!fs.existsSync(absolute) || !fs.statSync(absolute).isFile()) {
      throw new Error(`product scope file does not exist: ${relative}`);
    }
    digest.update(relative);
    digest.update('\0');
    digest.update(fs.readFileSync(absolute));
    digest.update('\0');
  }
  return digest.digest('hex');
};
const receiptMetadataIsWellFormed = (receipt, item, id) => {
  assert(receipt && typeof receipt === 'object' && !Array.isArray(receipt), `${id}: receipt must be an object`);
  assert.equal(receipt.command, item.command, `${id}: receipt command does not match the evidence contract`);
  assert.deepEqual(receipt.assertions, item.assertions, `${id}: receipt assertions do not match the evidence contract`);
  assert(Number.isInteger(receipt.exitCode), `${id}: receipt exitCode must be an integer`);
  assert.equal(receipt.exitCode, 0, `${id}: recorded receipt did not exit cleanly`);
};
const receiptIsWellFormed = (receipt, item, id, expectedDigest) => {
  receiptMetadataIsWellFormed(receipt, item, id);
  assert.match(receipt.productDigest, /^[0-9a-f]{64}$/u, `${id}: receipt productDigest must be a SHA-256 hex digest`);
  assert.equal(receipt.productDigest, expectedDigest, `${id}: receipt product digest is stale for the Copal product scope`);
};
const receiptState = (receipt, item, id, expectedDigest) => {
  if (!receipt) return { status: 'pending', reason: 'no receipt recorded' };
  try {
    receiptMetadataIsWellFormed(receipt, item, id);
  } catch (error) {
    return { status: 'pending', reason: `stale receipt metadata: ${error.message}` };
  }
  if (receiptDocument.schemaVersion !== RECEIPT_SCHEMA_VERSION) {
    return { status: 'pending', reason: `legacy receipt schema ${String(receiptDocument.schemaVersion)}; rerun with schema ${RECEIPT_SCHEMA_VERSION}` };
  }
  if (!Object.prototype.hasOwnProperty.call(receipt, 'productDigest')) {
    return { status: 'pending', reason: 'legacy receipt has no productDigest; rerun after source freeze' };
  }
  if (typeof receipt.productDigest !== 'string' || !/^[0-9a-f]{64}$/u.test(receipt.productDigest)) {
    return { status: 'pending', reason: 'receipt productDigest is invalid; rerun after source freeze' };
  }
  if (receipt.productDigest !== expectedDigest) {
    return { status: 'pending', reason: 'receipt productDigest is stale; rerun after source freeze' };
  }
  return { status: 'verified', reason: 'productDigest matches current Copal product scope' };
};
const combinedAbsolute = path.join(repo, combinedJourney.file);
assert(fs.existsSync(combinedAbsolute) && fs.statSync(combinedAbsolute).isFile(), 'combined mounted journey fixture is required');
assert(combinedJourney.source.includes('#P0-combined-mounted-50-feature-journey'), 'combined journey must link to its canonical QA anchor');
assert.equal(combinedJourney.assertions.length, 10, 'combined journey must retain all explicit P0 assertion labels');
assert(combinedJourney.command.length > 10, 'combined journey command is missing');
assert.equal(combinedJourney.runtime, 'browser', 'combined journey must remain a browser receipt');
assert.deepEqual(Object.keys(combinedJourney.environment).sort(), ['backend', 'browser', 'external', 'platform'], 'combined journey environment statuses are required');
const combinedCurrentProductDigest = productScopeDigest(combinedJourney.file, combinedJourney.evidenceFiles);
const combinedReceiptState = combinedJourney.receipt
  ? receiptState(combinedJourney.receipt, combinedJourney, 'combined journey', combinedCurrentProductDigest)
  : { status: 'pending', reason: 'no receipt recorded' };

const snapshot = {};
for (const id of ids) {
  const item = evidence[id];
  const absolute = path.join(repo, item.file);
  assert(fs.existsSync(absolute), `${id}: evidence file does not exist: ${item.file}`);
  assert(fs.statSync(absolute).isFile(), `${id}: evidence path must name a file: ${item.file}`);
  assert(item.source.includes(`#${id}`), `${id}: evidence must link to its canonical requirement`);
  assert(item.command.length > 10, `${id}: evidence command is missing`);
  assert(item.assertions.length > 0 && item.assertions.every(Boolean), `${id}: executable assertion identifiers are missing`);
  assert(['browser', 'backend', 'python', 'node'].includes(item.runtime), `${id}: unsupported evidence runtime`);
  assert.deepEqual(Object.keys(item.environment).sort(), ['backend', 'browser', 'external', 'platform'], `${id}: backend/browser/platform/external statuses are required`);
  assert.match(item.environment.backend, /required|fixture-or-route/);
  assert.match(item.environment.browser, /required|not-applicable/);
  assert.match(item.environment.platform, /current-host|deferred-platform/);
  assert.match(item.environment.external, /stubbed-or-none/);
  const row = rows.get(id);
  assert(row.detail.length > 20, `${id}: ledger limitation/evidence detail is too vague`);
  const currentProductDigest = productScopeDigest(item.file, item.evidenceFiles);
  const receipt = receiptState(item.receipt, item, id, currentProductDigest);
  if (row.status === 'pass') {
    assert.equal(receipt.status, 'verified', `${id}: pass requires a current product-scope receipt`);
  } else {
    assert.match(row.detail, /pending|partial|open|queued|defer|limit|fixture|platform|runtime|unavailable|source|browser|backend|receipt|rerun|remain|exists|current/i, `${id}: partial/deferred row must state its boundary`);
  }
  snapshot[id] = { ...item, currentSourceDigest: crypto.createHash('sha256').update(fs.readFileSync(absolute)).digest('hex'), currentProductDigest, currentSourceMtime: fs.statSync(absolute).mtime.toISOString(), receiptStatus: receipt.status, receiptReason: receipt.reason, status: row.status };
}

const statuses = Object.values(snapshot).reduce((counts, item) => { counts[item.status] = (counts[item.status] || 0) + 1; return counts; }, {});
const verifiedReceipts = Object.values(snapshot).filter(item => item.receiptStatus === 'verified').length;
const pendingReceipts = Object.values(snapshot).filter(item => item.receiptStatus === 'pending').length;
const summary = `Copal S28 evidence contract: ${ids.length}/${ids.length} IDs described; ${verifiedReceipts} receipts verified; ${pendingReceipts} receipts pending; ${statuses.pass || 0} receipt-backed pass; ${statuses.partial || 0} partial; ${statuses.queued || 0} queued; ${statuses['deferred-by-platform'] || 0} platform-deferred.`;
if (process.argv.includes('--json')) {
  console.log(JSON.stringify({ schemaVersion: RECEIPT_SCHEMA_VERSION, generatedAt: new Date().toISOString(), summary, ledger: path.relative(repo, ledgerPath), productScope: { roots: productScopeRoots, ignoredDirectories: 'generated/test output, .references, target, node_modules, and user/live data', fileCount: scopeFiles.size }, combinedJourney: { ...combinedJourney, productDigest: combinedJourney.receipt ? combinedJourney.receipt.productDigest || null : null, currentProductDigest: combinedCurrentProductDigest, receiptStatus: combinedReceiptState.status, receiptReason: combinedReceiptState.reason }, requirements: snapshot }, null, 2));
} else {
  console.log(summary);
  const combinedLabel = combinedReceiptState.status === 'verified' ? 'verified' : 'pending';
  console.log(`Combined P0 production qualification gate ${combinedLabel}: ${combinedJourney.command} (${combinedJourney.assertions.length} labelled child and mounted-handoff assertions; ${combinedReceiptState.reason}).`);
}
