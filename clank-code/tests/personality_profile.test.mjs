import assert from 'node:assert/strict';
import test from 'node:test';
import {exportProfile, readProfile} from '../static/js/personalityProfile.js';
import {buildBundle} from '../static/js/knowledgeSnapshotBundle.js';

test('portable persona carries only explicitly attached reviewed snapshot bytes', async () => {
  const persona = {name: 'Scout', system_prompt: 'Ask which trail to follow.'};
  assert.deepEqual(await readProfile(await exportProfile(persona)), {persona, snapshots: []});
  const knowledge = new TextEncoder().encode('{"knowledge":{"text":"I hike on Fridays."}}');
  const bundle = await buildBundle([knowledge]);
  const packed = await exportProfile(persona, bundle);
  const restored = await readProfile(packed);
  assert.deepEqual(restored.persona, persona);
  assert.deepEqual(restored.snapshots[0].bytes, knowledge);
  const untrusted = JSON.parse(new TextDecoder().decode(packed));
  untrusted.knowledge.items[0].package_sha256 = '1'.repeat(64);
  const encode = object => new TextEncoder().encode(JSON.stringify(object));
  await assert.rejects(readProfile(encode(untrusted)));
  await assert.rejects(readProfile(encode({...untrusted, permissions: {can_write_hexes: true}})));
  await assert.rejects(exportProfile({...persona, privileges: ['edit']}));
});
