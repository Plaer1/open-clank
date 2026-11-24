import test from 'node:test';
import assert from 'node:assert/strict';
import * as flow from '../../static/js/mediaJobFlow.js';
import { FilesFacadeClient } from '../../static/js/filesFacadeClient.js';

test('Provider-neutral media job flow', async () => {
  // Neutral flow is driven only by the server's profile fields, never by vendor/family names.
  const mk = (id, extra = {}) => ({ profile_id: id, label: `Label ${id}`, revision: `rev-${id}`, operation: 'mesh.generate',
    input_kinds: ['text_prompt'], output_kinds: ['mesh'], output_mime: 'model/gltf-binary', metered: true, cancel_available: false, ...extra });
  async function run(profile) {
    const calls = [];
    const answers = ['2', 'a small cube', 'out.glb'];   // one compatible profile: no profile question
    const client = {
      mediaProfiles: async b => { calls.push(['profiles', b]);
        return b.operation === '*' ? { operations: [{ operation: 'image.generate', label: 'Generate image', profiles: [] },
          { operation: 'mesh.generate', label: 'Generate 3D model', profiles: [profile] }, { operation: 'animation.generate', label: 'Animate sprite', profiles: [] }] }
          : { profiles: [profile], partial: false }; },
      prepareMediaOperation: async b => { calls.push(['prepare', b]); return { state: 'prepared', jobID: 'j1', revision: 1, profile: { providerAccountID: profile.profile_id },
        destination: { name: b.destination.name, folder: 'F' }, parameters: { prompt: 'a small cube' }, notice: 'n' }; },
      runMedia: async b => { calls.push(['run', b]); return { state: 'queued' }; },
    };
    const ui = { styledPrompt: async () => answers.shift(), styledConfirm: async () => true };
    await flow.runNeutralMediaFlow({ client, ui, sessionID: 's1', current: () => true, chooseDestination: async () => 'dest-ref',
      chooseRecipe: async () => null, chooseImage: async () => null, status: () => {} });
    return calls;
  }
  const a = await run(mk('acct-a', { family_id: 'tripo' }));
  const b = await run(mk('zorblax-7', { family_id: 'zorblax-labs', label: 'Zorblax' }));
  const shape = calls => calls.map(([k, body]) => [k, Object.keys(body).sort()]);
  assert.deepEqual(shape(a), shape(b));
  for (const [calls, id] of [[a, 'acct-a'], [b, 'zorblax-7']]) {
    const p = calls.find(c => c[0] === 'prepare')[1];
    assert.equal(p.profile_id, id); assert.equal(p.profile_revision, `rev-${id}`);
    assert.deepEqual(p.inputs, [{ kind: 'text_prompt', text: 'a small cube' }]);
    assert.equal(p.operation, 'mesh.generate');
  }

  // Removed vendor-named entry points are gone.
  for (const n of ['runTripoJobFlow']) assert.equal(n in flow, false, n);
  const proto = FilesFacadeClient.prototype;
  for (const n of ['prepareSprixen', 'prepareSprixenAnimation', 'prepareTripo', 'prepareMeshy', 'prepareMeshyRefine']) assert.equal(n in proto, false, n);
  assert.equal(typeof proto.prepareMediaOperation, 'function');
  for (const n of ['prepareBlender', 'prepareBlenderFour', 'inspectBlenderRig', 'prepareBlenderRig', 'prepareMaterialBake']) assert.equal(n in proto, false, n);
  assert.equal(typeof proto.prepareLocalOperation, 'function');
  console.log('ok');
});

test('Blender operation selection uses eligible local profiles and source authority', async () => {
  const profile = (id, op, form = {}) => ({ profile_id: id, label: 'Label ' + id, revision: 3, operation: op, operation_label: 'Render GLB image', form });
  function harness(listing, answers) {
    const calls = [];
    const client = {
      mediaProfiles: async b => { calls.push(['profiles', b.operation]); return listing; },
      prepareLocalOperation: async (op, body) => { calls.push(['prepare', op, body.connectionID]);
        return { state: 'prepared', jobID: 'j', revision: 1, profile: { connectionID: body.connectionID, connectionRevision: 3 },
          destination: { name: body.destinationName, folder: 'F' }, source: { sha256: 'a'.repeat(64), name: 'm.glb' }, parameters: { resolution: body.resolution } }; },
      runMedia: async () => ({ state: 'queued' }),
    };
    const asked = [];
    const ui = { styledPrompt: async (text, o) => { asked.push(o.title); return answers.shift(); }, styledConfirm: async () => true };
    return { client, ui, calls, asked };
  }
  const base = { sessionID: 's', current: () => true, chooseDestination: async () => 'dest', status: () => {}, sourceRef: 'src' };

  // one profile: no profile question; output default name comes from the server form metadata
  let h = harness({ profiles: [profile('b1', 'blender.render', { output: { noun: 'PNG', default_name: 'render.png' } })], partial: false }, ['out.png']);
  await flow.runBlenderJobFlow({ ...base, client: h.client, ui: h.ui });
  assert.deepEqual(h.asked, ['Output name']); assert.deepEqual(h.calls[0], ['profiles', 'blender.render']); assert.deepEqual(h.calls[1], ['prepare', 'blender.render', 'b1']);

  // two profiles: one question
  h = harness({ profiles: [profile('b1', 'blender.render'), profile('b2', 'blender.render')], partial: false }, ['2', 'out.png']);
  await flow.runBlenderJobFlow({ ...base, client: h.client, ui: h.ui });
  assert.deepEqual(h.asked, ['Choose a profile', 'Output name']); assert.deepEqual(h.calls[1], ['prepare', 'blender.render', 'b2']);

  // none: refused with the operation label and reason, nothing prepared
  h = harness({ profiles: [], partial: false }, []);
  await assert.rejects(flow.runBlenderJobFlow({ ...base, client: h.client, ui: h.ui, fourHeadings: true }), /blender\.four_headings is unavailable: No configured profile/);
  assert.deepEqual(h.calls, [['profiles', 'blender.four_headings']]);

  // material bake: resolution choices come from the server form
  h = harness({ profiles: [profile('b1', 'material.bake', { resolution_choices: [64, 128], output: { noun: 'ZIP', default_name: 'material.zip' } })], partial: false }, ['128', 'm.zip']);
  await flow.runBlenderJobFlow({ ...base, client: h.client, ui: h.ui, materialBake: true });
  assert.deepEqual(h.calls[0], ['profiles', 'material.bake']); assert.equal(h.calls[1][1], 'material.bake');

  // one operation-keyed client call
  const requests = [];
  const c = Object.create(FilesFacadeClient.prototype); c._request = (path, opts) => { requests.push([path, opts.method]); return {}; };
  c.prepareLocalOperation('blender.render', {}); c.prepareLocalOperation('blender.four_headings', {}); c.prepareLocalOperation('blender.rig_pose', {});
  c.prepareLocalOperation('blender.rig_pose', {}, { inspect: true }); c.prepareLocalOperation('material.bake', {});
  assert.deepEqual(requests.map(r => r[0]), ['/media/blender/prepare', '/media/blender/four-headings/prepare', '/media/blender/rig/prepare', '/media/blender/rig/inspect', '/media/material/bake/prepare']);
  assert.throws(() => c.prepareLocalOperation('image.generate', {}), /Unsupported media operation/);
  assert.throws(() => c.prepareLocalOperation('blender.render', {}, { inspect: true }), /Unsupported/);
  console.log('blender operation-first ok');
});
