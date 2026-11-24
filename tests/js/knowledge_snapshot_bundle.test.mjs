// Run: node tests/js/knowledge_snapshot_bundle.test.mjs
// Packages: three real single-item snapshots (fact, instruction/preference, persona/identity) built with
// services/memory/knowledge_snapshot_format.canonical_bytes and checked with parse_package, using
// ~/.cache/openclank-devtools/s14-runtime-2026-11-06/bin/python -B (PYTHON_DOTENV_DISABLED=1).
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { buildBundle, parseBundle, importBundle, checkBundleItemStatus, bytesToBase64, sha256Hex, BUNDLE_SCHEMA } from '../../static/js/knowledgeSnapshotBundle.js';

const PKG_B64 = [
  'eyJmb3JtYXQiOiJvcGVuY2xhbmsua25vd2xlZGdlLXNuYXBzaG90LnYxIiwia25vd2xlZGdlIjp7ImNhdGVnb3J5IjoiZmFjdCIsInRleHQiOiJGaXh0dXJlIGZhY3Qgb25lLiIsInRleHRfc2hhMjU2IjoiNWY5Y2I1NGJlMTVmNWVjZmU1YjA1OTRmYjM2MDNkNWMyMTZlNjBiYWZlYTkxMTk3ODY2MmE5ZGIxZWE1MDMyYyJ9LCJvcmlnaW4iOnsiZGVjbGFyZWRfc2NvcGUiOnsia2luZCI6InNlbGVjdGVkX2l0ZW0ifSwiaXRlbV9yZWYiOiJrbm93bGVkZ2UtNWZlY2ViNjZmZmM4NmYzOGQ5NTI3ODZjNmQ2OTZjNzljMmRiYzIzOWRkNGU5MWI0NjcyOWQ3M2EyN2ZiNTdlOSIsInJlY29yZF90eXBlIjoiZmFjdCIsInJldmlzaW9uIjoxLCJ2YWx1ZV9zaGEyNTYiOiI1ZjljYjU0YmUxNWY1ZWNmZTViMDU5NGZiMzYwM2Q1YzIxNmU2MGJhZmVhOTExOTc4NjYyYTlkYjFlYTUwMzJjIn19',
  'eyJmb3JtYXQiOiJvcGVuY2xhbmsua25vd2xlZGdlLXNuYXBzaG90LnYxIiwia25vd2xlZGdlIjp7ImNhdGVnb3J5IjoicHJlZmVyZW5jZSIsInRleHQiOiJGaXh0dXJlIHByZWZlcmVuY2UgdHdvLiIsInRleHRfc2hhMjU2IjoiMTIxNmMwOGIwNWQ5ZDU0OTJiMjhkYmE0OTE1OTkwMGY2ZWQ0ZDEzMTcxZTQ4NGE5ZjczZDdhMmE1YWNkZDUxNiJ9LCJvcmlnaW4iOnsiZGVjbGFyZWRfc2NvcGUiOnsia2luZCI6InNlbGVjdGVkX2l0ZW0ifSwiaXRlbV9yZWYiOiJrbm93bGVkZ2UtNmI4NmIyNzNmZjM0ZmNlMTlkNmI4MDRlZmY1YTNmNTc0N2FkYTRlYWEyMmYxZDQ5YzAxZTUyZGRiNzg3NWI0YiIsInJlY29yZF90eXBlIjoiaW5zdHJ1Y3Rpb24iLCJyZXZpc2lvbiI6MiwidmFsdWVfc2hhMjU2IjoiMTIxNmMwOGIwNWQ5ZDU0OTJiMjhkYmE0OTE1OTkwMGY2ZWQ0ZDEzMTcxZTQ4NGE5ZjczZDdhMmE1YWNkZDUxNiJ9fQ==',
  'eyJmb3JtYXQiOiJvcGVuY2xhbmsua25vd2xlZGdlLXNuYXBzaG90LnYxIiwia25vd2xlZGdlIjp7ImNhdGVnb3J5IjoiaWRlbnRpdHkiLCJ0ZXh0IjoiRml4dHVyZSBpZGVudGl0eSB0aHJlZS4iLCJ0ZXh0X3NoYTI1NiI6IjgzYzcwMDgwOTRlNDRiNjVmZDFmOWRhYjhkMzUwMTEyNjQzMGY1ZWM3NzY2OTgyMGJiODA3OWQwOTVlMTFjMDYifSwib3JpZ2luIjp7ImRlY2xhcmVkX3Njb3BlIjp7ImtpbmQiOiJzZWxlY3RlZF9pdGVtIn0sIml0ZW1fcmVmIjoia25vd2xlZGdlLWQ0NzM1ZTNhMjY1ZTE2ZWVlMDNmNTk3MThiOWI1ZDAzMDE5YzA3ZDhiNmM1MWY5MGRhM2E2NjZlZWMxM2FiMzUiLCJyZWNvcmRfdHlwZSI6InBlcnNvbmEiLCJyZXZpc2lvbiI6MywidmFsdWVfc2hhMjU2IjoiODNjNzAwODA5NGU0NGI2NWZkMWY5ZGFiOGQzNTAxMTI2NDMwZjVlYzc3NjY5ODIwYmI4MDc5ZDA5NWUxMWMwNiJ9fQ==',
];
const PKGS = PKG_B64.map((s) => new Uint8Array(Buffer.from(s, 'base64')));
const same = (a, b) => a.length === b.length && a.every((v, i) => v === b[i]);
const enc = (o) => new TextEncoder().encode(typeof o === 'string' ? o : JSON.stringify(o));
const rejects = (p) => assert.rejects(Promise.resolve().then(() => p()));
let count = 0;
const ok = (name) => { count++; };

async function entry(bytes) { return { package_b64: bytesToBase64(bytes), package_sha256: await sha256Hex(bytes) }; }
const hex = 'a'.repeat(64);

async function main() {
  // round trip, determinism
  for (const n of [1, 3]) {
    const file = await buildBundle(PKGS.slice(0, n));
    const { items } = await parseBundle(file);
    assert.equal(items.length, n);
    items.forEach((it, i) => { assert.ok(same(it.bytes, PKGS[i])); assert.equal(it.sha256, createHash('sha256').update(PKGS[i]).digest('hex')); });
    ok('roundtrip');
  }
  assert.ok(same(await buildBundle(PKGS), await buildBundle(PKGS))); ok('deterministic');
  assert.equal(JSON.parse(new TextDecoder().decode(await buildBundle(PKGS))).schema, BUNDLE_SCHEMA); ok('schema');

  // limits
  await rejects(() => buildBundle([])); ok('0 items');
  const many = Array.from({ length: 21 }, (_, i) => new Uint8Array([i + 1]));
  await rejects(() => buildBundle(many)); ok('21 items');
  assert.equal((await parseBundle(await buildBundle(many.slice(0, 20)))).items.length, 20); ok('20 items');
  await rejects(() => buildBundle([new Uint8Array(65537)])); ok('oversize package');
  await rejects(() => buildBundle([new Uint8Array(0)])); ok('empty package');
  await rejects(() => parseBundle(new Uint8Array(1572865))); ok('oversize file');
  await rejects(() => parseBundle(new Uint8Array(0))); ok('empty file');
  await rejects(() => buildBundle([PKGS[0], PKGS[0]])); ok('duplicate build');
  const e0 = await entry(PKGS[0]);
  await rejects(() => parseBundle(enc({ schema: BUNDLE_SCHEMA, items: [e0, e0] }))); ok('duplicate parse');

  // tampering / strictness
  await rejects(() => parseBundle(enc({ schema: BUNDLE_SCHEMA, items: [{ ...e0, package_sha256: hex }] }))); ok('tampered hash');
  await rejects(() => parseBundle(enc({ schema: BUNDLE_SCHEMA, items: [{ ...e0, package_sha256: e0.package_sha256.toUpperCase() }] }))); ok('uppercase hash');
  await rejects(() => parseBundle(enc({ schema: BUNDLE_SCHEMA, items: [e0], extra: 1 }))); ok('unknown top key');
  await rejects(() => parseBundle(enc({ schema: BUNDLE_SCHEMA, items: [{ ...e0, extra: 1 }] }))); ok('unknown item key');
  await rejects(() => parseBundle(enc({ schema: 'x', items: [e0] }))); ok('wrong schema');
  const good = new TextDecoder().decode(await buildBundle([PKGS[0]]));
  await rejects(() => parseBundle(enc(good.replace('{"schema"', '{"schema":"' + BUNDLE_SCHEMA + '","schema"')))); ok('duplicate top key');
  await rejects(() => parseBundle(enc(good.replace('"package_sha256"', '"package_sha256":"' + e0.package_sha256 + '","package_sha256"')))); ok('duplicate item key');
  await rejects(() => parseBundle(enc(' ' + good))); ok('non-canonical whitespace');
  await rejects(() => parseBundle(enc(good.replace(e0.package_b64.slice(0, 4), '!!!!')))); ok('invalid base64');
  const e1 = await entry(PKGS[1]);
  assert.ok(e1.package_b64.endsWith('=='));
  await rejects(() => parseBundle(enc({ schema: BUNDLE_SCHEMA, items: [{ ...e1, package_b64: e1.package_b64.slice(0, -2) }] }))); ok('unpadded base64');
  await rejects(() => parseBundle(new Uint8Array([0x7b, 0xff, 0x7d]))); ok('invalid utf8');
  await rejects(() => parseBundle(new Uint8Array([0xef, 0xbb, 0xbf, ...enc(good)]))); ok('BOM');
  await rejects(() => parseBundle(enc('[]'))); ok('array');

  // importBundle
  const { items } = await parseBundle(await buildBundle(PKGS));
  const preview = (i) => ({ ok: true, json: async () => ({ text: 't' + i, category: 'fact', review_sha256: String(i).repeat(64) }) });
  function harness({ confirm, previewFn, approveFn, curFn } = {}) {
    const log = []; let active = 0; let maxActive = 0; let ids = 0; const seen = [];
    const post = async (phase, body) => {
      active++; maxActive = Math.max(maxActive, active);
      await new Promise((r) => setTimeout(r, 2));
      const i = items.findIndex((it) => bytesToBase64(it.bytes) === body.package_b64);
      log.push([phase, i, body]); active--;
      if (phase === 'preview') return previewFn ? previewFn(i) : preview(i);
      return confirm ? confirm(i, phase) : { ok: true, json: async () => ({ state: 'staged' }) };
    };
    return { log, post, maxActive: () => maxActive, seen,
      args: { items, post, newActionId: () => `00000000-0000-4000-8000-${String(++ids).padStart(12, '0')}`, current: curFn || (() => true),
        approve: approveFn || (() => true), onItem: (r) => seen.push(r) } };
  }
  let h = harness();
  let res = await importBundle(h.args);
  assert.deepEqual(res.map((r) => r.state), ['staged', 'staged', 'staged']);
  assert.deepEqual(res.map((r) => r.index), [0, 1, 2]);
  assert.equal(res.length, items.length); assert.equal(h.seen.length, 3); ok('happy');
  assert.equal(h.maxActive(), 1); ok('sequential');
  assert.deepEqual(h.log.map((l) => l[0] + l[1]), ['preview0', 'confirm0', 'preview1', 'confirm1', 'preview2', 'confirm2']); ok('order');
  const ids = h.log.filter((l) => l[0] === 'preview').map((l) => l[2].action_id);
  assert.equal(new Set(ids).size, 3); ok('fresh ids');
  assert.ok(h.log.filter((l) => l[0] === 'confirm').every((l, k) => l[2].action_id === ids[k] && l[2].review_sha256 === String(k).repeat(64))); ok('confirm body');
  assert.ok(res.every((r, k) => r.sha256 === items[k].sha256)); ok('result sha');

  h = harness({ approveFn: (it) => it.index !== 1 });
  res = await importBundle(h.args);
  assert.deepEqual(res.map((r) => r.state), ['staged', 'skipped', 'staged']);
  assert.ok(!h.log.some((l) => l[0] === 'confirm' && l[1] === 1)); ok('unapproved not confirmed');

  h = harness({ confirm: (i) => { if (i === 1) throw new Error('net'); return { ok: true, json: async () => ({ state: 'staged' }) }; } });
  res = await importBundle(h.args);
  assert.deepEqual(res.map((r) => r.state), ['staged', 'unknown', 'staged']);
  assert.equal(h.log.filter((l) => l[0] === 'confirm' && l[1] === 1).length, 1); ok('confirm throw unknown no retry');
  h = harness({ confirm: () => ({ ok: false, json: async () => ({}) }) });
  res = await importBundle(h.args);
  assert.deepEqual(res.map((r) => r.state), ['unknown', 'unknown', 'unknown']);
  assert.equal(h.log.filter((l) => l[0] === 'confirm').length, 3); ok('non-2xx unknown');
  h = harness({ confirm: (i) => ({ ok: true, json: async () => ({ state: i === 0 ? 'unknown' : 'absent' }) }) });
  res = await importBundle(h.args);
  assert.deepEqual(res.map((r) => r.state), ['unknown', 'not_imported', 'not_imported']); ok('state mapping');

  h = harness({ previewFn: (i) => (i === 0 ? { ok: false, json: async () => ({}) } : preview(i)) });
  res = await importBundle(h.args);
  assert.deepEqual(res.map((r) => r.state), ['invalid', 'staged', 'staged']);
  assert.ok(!h.log.some((l) => l[0] === 'confirm' && l[1] === 0)); ok('preview refusal invalid');
  h = harness({ previewFn: (i) => (i === 1 ? { ok: true, json: async () => ({ text: 5 }) } : preview(i)) });
  res = await importBundle(h.args);
  assert.equal(res[1].state, 'invalid'); ok('malformed preview invalid');

  let live = true;
  h = harness({ curFn: () => live, confirm: async (i) => { if (i === 0) live = false; return { ok: true, json: async () => ({ state: 'staged' }) }; } });
  res = await importBundle(h.args);
  assert.deepEqual(res.map((r) => r.state), ['staged', 'skipped', 'skipped']);
  assert.equal(res.length, 3); assert.equal(h.seen.length, 3);
  assert.ok(!h.log.some((l) => l[1] > 0)); ok('current false stops');
  live = true;
  h = harness({ curFn: () => live, approveFn: () => { live = false; return true; } });
  res = await importBundle(h.args);
  assert.deepEqual(res.map((r) => r.state), ['skipped', 'skipped', 'skipped']);
  assert.ok(!h.log.some((l) => l[0] === 'confirm')); ok('current false after approve no confirm');

  // status
  const st = (v) => async () => v;
  const run = (v) => checkBundleItemStatus({ item: items[0], post: v, actionId: '00000000-0000-4000-8000-000000000001', reviewSha256: hex });
  assert.equal((await run(st({ ok: true, json: async () => ({ state: 'staged' }) }))).state, 'staged');
  assert.equal((await run(st({ ok: true, json: async () => ({ state: 'unknown' }) }))).state, 'unknown');
  assert.equal((await run(st({ ok: true, json: async () => ({ state: 'absent' }) }))).state, 'not_imported');
  assert.equal((await run(st({ ok: false }))).state, 'unknown');
  assert.equal((await run(async () => { throw new Error('x'); })).state, 'unknown');
  let seenBody; await checkBundleItemStatus({ item: items[0], post: async (p, b) => { seenBody = [p, b]; return { ok: true, json: async () => ({ state: 'staged' }) }; }, actionId: 'id', reviewSha256: hex });
  assert.equal(seenBody[0], 'status'); assert.deepEqual(Object.keys(seenBody[1]).sort(), ['action_id', 'package_b64', 'review_sha256']); ok('status');

  console.log(`${count} test groups ok`);
}
main().catch((e) => { console.error(e); process.exit(1); });
