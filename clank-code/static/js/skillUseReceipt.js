// Historical tool metadata identifies the body loaded then; it does not read or
// substitute a file that may since have changed or disappeared.
export function skillUseReceipt(event, {live = false} = {}) {
  if (!event || event.exit_code !== 0) return null;
  const receipt = (live ? event.data : event.structured)?.rawOutput?.metadata?.skillUse;
  if (!receipt || receipt.version !== 1 || typeof receipt.name !== 'string' || receipt.name.length > 1024 ||
      typeof receipt.bodySHA256 !== 'string' || !/^[a-f0-9]{64}$/.test(receipt.bodySHA256)) return null;
  if (live && (event.status !== 'completed' || !event.id || receipt.callID !== event.id)) return null;
  if (!live && event.id && receipt.callID && event.id !== receipt.callID) return null;
  const text = value => typeof value === 'string' && value.length <= 1024 ? value : null;
  const fileList = value => Array.isArray(value) ? value.slice(0, 10).map(text).filter(Boolean) : [];
  return {name: receipt.name, skillID: text(receipt.skillID), revision: Number.isSafeInteger(receipt.revision) ? receipt.revision : null,
    source: text(receipt.source), origin: text(receipt.origin), bodySHA256: receipt.bodySHA256,
    matchingDescription: text(receipt.matchingDescription),
    loadedFiles: fileList(receipt.loadedFiles), referenceSamples: fileList(receipt.referenceSamples),
    loadedAt: text(receipt.loadedAt), sessionID: text(receipt.sessionID), messageID: text(receipt.messageID),
    callID: text(receipt.callID), actorID: text(receipt.actorID), turnID: text(receipt.turnID), invocation: text(receipt.invocation),
    domain: receipt.domain && text(receipt.domain.block_id) && Number.isSafeInteger(receipt.domain.revision) && receipt.domain.revision > 0 ? {block_id:receipt.domain.block_id,revision:receipt.domain.revision} : null};
}

export function renderSkillUseReceipt(event, esc, options) {
  const skill = skillUseReceipt(event, options);
  if (!skill) return '';
  const lines = [
    skill.matchingDescription && `Matching description · ${skill.matchingDescription}`,
    ...skill.loadedFiles.map(file => `Loaded file · ${file}`),
    ...skill.referenceSamples.map(file => `Sampled reference · ${file}`),
    `Body SHA-256: ${skill.bodySHA256}`,
    skill.skillID && `Skill ID: ${skill.skillID}`,
    skill.revision != null && `Revision: ${skill.revision}`,
    skill.source && `Source at load: ${skill.source}`,
    skill.origin && `Origin: ${skill.origin}`,
    skill.loadedAt && `Loaded: ${skill.loadedAt}`,
    skill.actorID && `Actor: ${skill.actorID}`,
    skill.turnID && `Turn: ${skill.turnID}`,
    skill.callID && `Call: ${skill.callID}`,
    skill.domain && `Requested domain: ${skill.domain.block_id} · revision ${skill.domain.revision} (qualified at load)`,
    'Current source availability is not checked; this receipt refers to the loaded body.',
  ].filter(Boolean).join('\n');
  return `<details class="agent-tool-output"><summary>Skill used: ${esc(skill.name)}</summary><pre>${esc(lines)}</pre></details>`;
}

/** Explicit current lifecycle read; the historical receipt never supplies a path. */
export function currentSkillSourceButton(event, options) {
  const receipt=skillUseReceipt(event,options);
  if (!receipt?.skillID || receipt.skillID.length>512 || !Number.isSafeInteger(receipt.revision) || receipt.revision<1) return null;
  const box=document.createElement('div'), button=document.createElement('button'), result=document.createElement('pre');
  button.type='button';button.textContent='Inspect current skill source';box.append(button,result);
  button.addEventListener('click',async event=>{
    event.stopPropagation();
    const original=window.chatModule?.ratingAccountScope?.(), account=document.body?.dataset?.accountId;
    const current=()=>{const now=window.chatModule?.ratingAccountScope?.();return !!account && original?.owner===account && document.body?.dataset?.accountId===account && now?.owner===account && now.sid===original.sid && now.authEpoch===original.authEpoch && button.isConnected;};
    if (!current() || button.disabled)return;
    result.textContent='';button.disabled=true;
    try {
      const response=await fetch(`/api/skills/${encodeURIComponent(receipt.skillID)}/current-source`,{credentials:'same-origin',cache:'no-store'});
      if(!current())return;
      if(!response.ok)throw new Error('unavailable');
      const value=await response.json();if(!current())return;
      if(value?.state!=='ready' || value.skill_id!==receipt.skillID || !Number.isSafeInteger(value.revision) || value.revision<1 || typeof value.source!=='string' || new TextEncoder().encode(value.source).length>262144 || value.comparison!=='unavailable')throw new Error('unavailable');
      result.textContent=`Current published skill · revision ${value.revision}\nBody comparison unavailable; historical output is unchanged.\n\n${value.source}`;
    } catch { if(current())result.textContent='Current skill source unavailable.'; }
    finally {if(button.isConnected)button.disabled=false;}
  });
  return box;
}
