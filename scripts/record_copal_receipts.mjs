#!/usr/bin/env node

/**
 * Execute one manifest command and attach its real zero-exit result to every
 * requirement mapped to that exact command. The command is selected from the
 * manifest, never supplied by the caller, and the fixture is replaced only
 * after the command and a post-run source digest both succeed.
 */
import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const repo = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const manifest = path.join(repo, 'tests/copal_requirements_manifest.mjs');
const receiptPath = path.join(repo, 'tests/fixtures/copal_requirements_receipts.json');
const expectedSchemaVersion = 4;

function usage() {
  console.log('Usage: node scripts/record_copal_receipts.mjs --id <ID> [--id <ID> ...]');
  console.log('       node scripts/record_copal_receipts.mjs --combined');
  console.log('Runs the exact manifest command once and atomically records its current digest.');
}

const args = process.argv.slice(2);
if (args.includes('--help') || args.length === 0) {
  usage();
  process.exit(args.length === 0 ? 2 : 0);
}

const selectedIds = [];
let combined = false;
for (let index = 0; index < args.length; index += 1) {
  if (args[index] === '--combined') {
    combined = true;
  } else if (args[index] === '--id' && args[index + 1]) {
    selectedIds.push(...args[++index].split(',').map(id => id.trim()).filter(Boolean));
  } else {
    throw new Error(`unknown argument: ${args[index]}`);
  }
}
assert(!(combined && selectedIds.length), 'choose --combined or --id, not both');
assert(combined || selectedIds.length > 0, 'at least one --id is required');

function readManifest() {
  const run = spawnSync(process.execPath, [manifest, '--json'], {
    cwd: repo,
    encoding: 'utf8',
    maxBuffer: 32 * 1024 * 1024,
  });
  if (run.status !== 0) throw new Error(`manifest inspection failed (exit ${run.status ?? 'unknown'}): ${run.stderr}`);
  return JSON.parse(run.stdout);
}

const fixtureBefore = fs.readFileSync(receiptPath);
const before = readManifest();
assert.equal(before.schemaVersion, expectedSchemaVersion, 'receipt recorder requires the current receipt schema');
const entries = combined
  ? [{ id: 'combinedJourney', ...before.combinedJourney }]
  : selectedIds.map(id => {
    const item = before.requirements[id];
    assert(item, `unknown requirement ID: ${id}`);
    return { id, ...item };
  });
assert(entries.length > 0, 'no mapped evidence selected');
const command = entries[0].command;
assert(command && entries.every(entry => entry.command === command), 'all selected IDs must share one exact manifest command');

const mapped = combined
  ? entries
  : Object.entries(before.requirements)
    .filter(([, item]) => item.command === command)
    .map(([id, item]) => ({ id, ...item }));
assert(mapped.length > 0, 'manifest command has no mapped IDs');
const mappedIds = mapped.map(entry => entry.id);
const sourceDigestBefore = combined ? before.combinedJourney.currentProductDigest : before.requirements[mappedIds[0]].currentProductDigest;
assert.match(sourceDigestBefore, /^[0-9a-f]{64}$/u, 'manifest did not provide a current product digest');

console.error(`Executing mapped command once for ${mappedIds.join(', ')}:`);
console.error(command);
const execution = spawnSync(command, {
  cwd: repo,
  shell: true,
  env: process.env,
  encoding: 'utf8',
  maxBuffer: 64 * 1024 * 1024,
});
const output = [execution.stdout, execution.stderr].filter(Boolean).join('\n');
if (output) process.stderr.write(`${output.slice(-12000)}\n`);
assert(Number.isInteger(execution.status), `mapped command terminated by signal ${execution.signal ?? 'unknown'}; no receipt was written`);
assert.equal(execution.status, 0, `mapped command failed with exit ${execution.status}; no receipt was written`);
// A few mounted suites intentionally return exit 0 with a JSON `skipped`
// payload when Chrome, Python, or a native worker is unavailable. That is a
// useful local probe result, but it is not a qualification receipt. Check the
// complete captured output so a combined journey cannot hide a skipped child.
assert(!/["']skipped["']\s*:/u.test(output), 'mapped command reported skipped prerequisites; no receipt was written');

const after = readManifest();
const sourceDigestAfter = combined ? after.combinedJourney.currentProductDigest : after.requirements[mappedIds[0]].currentProductDigest;
assert.equal(sourceDigestAfter, sourceDigestBefore, 'product source changed during execution; rerun after source freeze');
assert.equal(Buffer.compare(fixtureBefore, fs.readFileSync(receiptPath)), 0, 'receipt fixture changed during execution; refusing overwrite');

const fixture = JSON.parse(fixtureBefore.toString('utf8'));
assert.equal(fixture.schemaVersion, expectedSchemaVersion, 'receipt fixture schema changed during execution');
if (!fixture.receipts || typeof fixture.receipts !== 'object' || Array.isArray(fixture.receipts)) fixture.receipts = {};
const recordedAt = new Date().toISOString();
fixture.generatedAt = recordedAt;
for (const entry of mapped) {
  const receipt = {
    command: entry.command,
    assertions: entry.assertions,
    exitCode: execution.status,
    productDigest: sourceDigestAfter,
    recordedAt,
  };
  if (combined) fixture.combinedJourney = receipt;
  else fixture.receipts[entry.id] = receipt;
}
const serialized = `${JSON.stringify(fixture, null, 2)}\n`;
const temporary = path.join(path.dirname(receiptPath), `.${path.basename(receiptPath)}.${process.pid}.${crypto.randomBytes(6).toString('hex')}.tmp`);
let descriptor;
try {
  descriptor = fs.openSync(temporary, 'wx', 0o600);
  fs.writeFileSync(descriptor, serialized, 'utf8');
  fs.fsyncSync(descriptor);
  fs.closeSync(descriptor);
  descriptor = undefined;
  fs.renameSync(temporary, receiptPath);
} finally {
  if (descriptor !== undefined) fs.closeSync(descriptor);
  if (fs.existsSync(temporary)) fs.unlinkSync(temporary);
}
console.log(`Recorded ${mappedIds.join(', ')} with productDigest ${sourceDigestAfter}.`);
