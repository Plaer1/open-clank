import { MAX_BUNDLE_BYTES, parseBundle } from './knowledgeSnapshotBundle.js';

const schema = 'openclank.personality-profile.v1';
const encoder = new TextEncoder();
const decoder = new TextDecoder('utf-8', {fatal: true});
export const MAX_PROFILE_BYTES = MAX_BUNDLE_BYTES + 65536;

function validatePersona(persona) {
  if (!persona || Object.keys(persona).sort().join(',') !== 'name,system_prompt'
      || typeof persona.name !== 'string' || !persona.name.trim() || [...persona.name].length > 100
      || typeof persona.system_prompt !== 'string' || !persona.system_prompt.trim() || [...persona.system_prompt].length > 10000) {
    throw new Error('A profile needs a name and a system prompt within the editor limits.');
  }
  return {name: persona.name, system_prompt: persona.system_prompt};
}

export async function exportProfile(persona, snapshotBytes) {
  const profile = {schema, persona: validatePersona(persona)};
  if (snapshotBytes) {
    await parseBundle(snapshotBytes);
    profile.knowledge = JSON.parse(decoder.decode(snapshotBytes));
  }
  const bytes = encoder.encode(JSON.stringify(profile));
  if (bytes.length > MAX_PROFILE_BYTES) throw new Error('The profile is too large.');
  return bytes;
}

export async function readProfile(bytes) {
  if (!(bytes instanceof Uint8Array) || bytes.length > MAX_PROFILE_BYTES) throw new Error('The profile is too large.');
  const data = JSON.parse(decoder.decode(bytes));
  if (!data || data.schema !== schema || !['persona,schema','knowledge,persona,schema'].includes(Object.keys(data).sort().join(','))) {
    throw new Error('Choose an Open Clank personality profile.');
  }
  const persona = validatePersona(data.persona);
  const snapshots = data.knowledge === undefined ? [] : (await parseBundle(encoder.encode(JSON.stringify(data.knowledge)))).items;
  return {persona, snapshots};
}
