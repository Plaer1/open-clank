/** Manual, owner-scoped inspection of bounded provider-frame metadata. */
export function mountChatContextInspector(container, getChatID) {
  let controller, epoch = 0, cursor = null, busy = false, disposed = false;
  const root = document.createElement('div');
  const action = document.createElement('button'); action.type = 'button'; action.textContent = 'Inspect provider frames';
  const output = document.createElement('div'); output.setAttribute('role', 'status');
  const notice = document.createElement('p'); notice.setAttribute('role', 'status');
  root.append(action, output, notice); container.append(root);
  const account = () => document.body?.dataset?.accountId || '';
  function invalidate() { epoch++; controller?.abort(); cursor = null; busy = false; output.replaceChildren(); notice.textContent = ''; action.textContent = 'Inspect provider frames'; action.disabled = false; }
  async function load() {
    const chat = getChatID(), owner = account(), current = epoch, continuation = cursor;
    if (!chat || busy || disposed) return;
    busy = true; action.disabled = true; notice.textContent = ''; controller = new AbortController();
    try {
      const query = new URLSearchParams({limit:'5'}); if (continuation) query.set('cursor', continuation);
      const res = await fetch(`/api/orchestration/${encodeURIComponent(chat)}/context-observations?${query}`, {credentials:'same-origin', cache:'no-store', signal:controller.signal});
      if (!res.ok) throw new Error('Context inspection unavailable');
      const data = await res.json();
      if (disposed || current !== epoch || chat !== getChatID() || owner !== account()) return;
      if (!continuation) output.replaceChildren();
      if (data.availability !== 'available') { notice.textContent = 'Provider-frame evidence is unavailable; retry.'; return; }
      for (const item of data.items || []) {
        const details = document.createElement('details');
        const title = document.createElement('summary');
        title.textContent = `Assistant ${item.messageID} · ${item.availability === 'available' ? `${item.frames.length} frames retained, ${item.omittedFrameCount} omitted` : 'receipt unavailable'}`;
        details.append(title);
        details.append(membershipEvidence(item));
        for (const frame of item.frames || []) {
          const section = document.createElement('details');
          const label = document.createElement('summary');
          label.textContent = `Attempt ${frame.sequence.attempt}, transform ${frame.sequence.transform} · ${new Date(frame.observedAt).toLocaleString()}`;
          section.append(label);
          const info = document.createElement('p');
          const window = frame.model.window;
          info.textContent = `${frame.stage === 'model_view_dispatch_observed' ? 'Image request handed to transport · provider delivery unknown' : 'Pre-dispatch source list; delivery evidence is separate'} · ${frame.model.providerID}/${frame.model.modelID} · usable window ${window.hard === 0 ? 'unknown' : window.usable} · token count unavailable · source mapping unknown`;
          section.append(info);
          const identity = document.createElement('p');
          identity.textContent = `Session ${frame.identity.sessionID}${frame.identity.actorID ? ` · actor ${frame.identity.actorID}` : ''}${frame.identity.executionRevision != null ? ` · execution ${frame.identity.executionRevision}` : ''}${frame.identity.rootOperationID ? ` · operation ${frame.identity.rootOperationID}` : ''}${frame.identity.innerTurnID ? ` · turn ${frame.identity.innerTurnID}` : ''}`;
          section.append(identity);
          const profile = document.createElement('details');
          const heading = document.createElement('summary');
          heading.textContent = 'Persona, guidance and Memery';
          profile.append(heading);
          if (frame.profile) {
            for (const [key, label] of [['personaInstructions', 'Persona and host instructions'], ['standingGuidance', 'Standing guidance'], ['recalledMemery', 'Recalled Memery']]) {
              const row = document.createElement('p');
              const digest = frame.profile[key];
              row.textContent = digest ? `${label} · matched in this frame · SHA-256 ${digest}` : `${label} · no verified match`;
              profile.append(row);
              if (key === 'personaInstructions' && digest) {
                const check = document.createElement('button');
                check.type = 'button';
                check.textContent = 'Compare current default persona';
                check.addEventListener('click', async () => {
                  const stillCurrent = () => check.isConnected && !disposed && current === epoch && chat === getChatID() && owner === account();
                  if (!stillCurrent()) return;
                  check.disabled = true;
                  let preview = 'Current default persona does not match this frame.';
                  try {
                    const response = await fetch('/api/presets/default-persona', {credentials:'same-origin',cache:'no-store'});
                    if (!response.ok) throw Error();
                    const {name, system_prompt} = await response.json();
                    if (typeof name !== 'string' || typeof system_prompt !== 'string' || !system_prompt || system_prompt.length > 20000) throw Error();
                    const text = `Your name is ${name}. ${system_prompt}`;
                    const {sha256Hex} = await import('./knowledgeSnapshotBundle.js');
                    for (const value of [text, system_prompt]) {
                      if (await sha256Hex(new TextEncoder().encode(value)) === digest) {
                        preview = `Matching current persona · ${value}`;
                        break;
                      }
                    }
                  } catch { preview = 'Current persona comparison unavailable.'; }
                  if (stillCurrent()) { row.textContent = preview; check.disabled = false; }
                });
                profile.append(check);
              }
            }
          } else {
            const row = document.createElement('p');
            row.textContent = 'This frame has no profile evidence.';
            profile.append(row);
          }
          section.append(profile);
          for (const [name, component] of Object.entries(frame.components)) {
            const entry = document.createElement('p'); entry.textContent = `${name}: ${component.state}${component.digest ? ` · ${component.digest}` : ''}`; section.append(entry);
          }
          const coverage = document.createElement('p'); coverage.textContent = `Sources: ${frame.sources.length} retained, ${frame.omittedSourceCount} omitted`; section.append(coverage);
          for (const source of frame.sources) {
            const ref = document.createElement('p'); ref.textContent = `${source.kind}: ${source.id}${source.revision ? ` · revision ${source.revision}` : ''}${source.digest ? ` · ${source.digest}` : ''}`; section.append(ref);
            const view=source.kind==='media_input' ? source.derivedView : null;
            if (view?.profile==='image-downscale-v1' && view.derivedMime==='image/png' && view.stage==='model_view_pre_dispatch' && view.providerDelivery==='unknown' &&
                Number.isSafeInteger(view.width) && view.width>0 && view.width<=2048 && Number.isSafeInteger(view.height) && view.height>0 && view.height<=2048 &&
                Number.isSafeInteger(view.derivedLength) && view.derivedLength>0 && view.derivedLength<=256*1024) {
              const derived=document.createElement('p');derived.textContent=`Image model view: ${view.width} × ${view.height}, ${view.derivedLength} bytes · provider delivery unknown`;section.append(derived);
            }
            if (source.kind === 'part') section.append(selectionControl(source));
          }
          details.append(section);
        }
        output.append(details);
      }
      if (!data.items?.length && !continuation) output.textContent = 'No provider-frame receipts observed.';
      cursor = data.nextCursor || null;
      action.textContent = cursor ? 'Load more' : 'Inspect again';
    } catch (error) {
      if (!disposed && current === epoch && chat === getChatID() && owner === account() && error.name !== 'AbortError') notice.textContent = 'Provider-frame evidence is unavailable; retry.';
    } finally { if (current === epoch && !disposed) { busy = false; action.disabled = false; } }
  }
  function membershipEvidence(item) {
    const section = document.createElement('section');
    const heading = document.createElement('p'); heading.textContent = 'Native-source transport evidence'; section.append(heading);
    const validText = (value, max=512) => typeof value === 'string' && value.length > 0 && new TextEncoder().encode(value).length <= max;
    const digest = value => typeof value === 'string' && /^[0-9a-f]{64}$/.test(value);
    const memberships = item?.nativeMemberships, deliveries = item?.nativeDeliveries;
    const validSource = source => source && validText(source.messageID) && validText(source.partID) && digest(source.digest) && source.sourceRevision === 'unavailable';
    const keys = (value, expected) => value && typeof value === 'object' && !Array.isArray(value) && Object.keys(value).sort().join('|') === expected.slice().sort().join('|');
    const validReceipt = receipt => receipt && receipt.version === 1 && receipt.state === 'acknowledged' && receipt.coverage === 'partial' && validText(receipt.dispatchID,256) && digest(receipt.bodyDigest) &&
      keys(receipt.identity,['sessionID','rootOperationID','actorID','executionRevision','innerTurnID']) &&
      ['sessionID','rootOperationID','actorID','innerTurnID'].every(k => validText(receipt.identity[k])) && Number.isSafeInteger(receipt.identity.executionRevision) && receipt.identity.executionRevision >= 0;
    if ((memberships === undefined && deliveries === undefined) ||
        (memberships !== undefined && (!Array.isArray(memberships) || memberships.length > 16 || memberships.some(r => !keys(r,['version','identity','profile','dispatchID','bodyDigest','state','coverage','sources']) || !validReceipt(r) || r.profile !== 'openai-chat-native-text-v1' || !Array.isArray(r.sources) || !r.sources.length || r.sources.length > 32 || r.sources.some(s => !keys(s,['messageID','partID','role','digest','sourceRevision','messageIndex','contentIndex']) || !validSource(s) || !['user','assistant'].includes(s.role) || !Number.isSafeInteger(s.messageIndex) || s.messageIndex < 0 || s.messageIndex >= 544 || s.contentIndex !== 0)))) ||
        (deliveries !== undefined && (!Array.isArray(deliveries) || deliveries.length > 16 || deliveries.some(r => !keys(r,['version','identity','profile','dispatchID','bodyDigest','state','coverage','source']) || !validReceipt(r) || r.profile !== 'openai-chat-single-user-text-v1' || !keys(r.source,['messageID','partID','digest','sourceRevision']) || !validSource(r.source))))) {
      const unavailable = document.createElement('p'); unavailable.textContent = 'Native-source evidence unavailable.'; section.append(unavailable); return section;
    }
    const seen = new Set(), lines = [];
    const show = (receipt, source) => {
      const key = JSON.stringify([receipt.dispatchID,receipt.bodyDigest,source.messageID,source.partID,source.digest]);
      if (seen.has(key)) return; seen.add(key);
      lines.push(`Dispatch ${receipt.dispatchID} · body ${receipt.bodyDigest} · ${source.role ? source.role + ' · ' : ''}message ${source.messageID} · part ${source.partID} · source ${source.digest} · source revision unavailable`);
    };
    for (const receipt of memberships || []) for (const source of receipt.sources) show(receipt,source);
    for (const receipt of deliveries || []) show(receipt,receipt.source);
    if (!lines.length) { const unavailable = document.createElement('p'); unavailable.textContent = 'Native-source evidence unavailable.'; section.append(unavailable); }
    else {
      const details = document.createElement('details'), title = document.createElement('summary');
      title.textContent = 'Acknowledged native-source snapshot · partial'; details.append(title);
      let built = false;
      details.addEventListener('toggle', () => {
        if (!details.open || built) return;
        built = true;
        for (const text of lines) { const line = document.createElement('p'); line.textContent = text; details.append(line); }
      });
      section.append(details);
    }
    const limits = document.createElement('p'); limits.textContent = 'Partial acknowledged snapshot; current source state and omitted sources are unknown.'; section.append(limits);
    return section;
  }
  function selectionControl(ref) {
    const box = document.createElement('div');
    const choice = document.createElement('select'); choice.setAttribute('aria-label', 'Context selection');
    for (const [value, label] of [['omit','Omit from context'], ['trim','Trim tool output'], ['restore','Restore this selection']]) {
      const option = document.createElement('option'); option.value = value; option.textContent = label; choice.append(option);
    }
    const previewButton = document.createElement('button'); previewButton.type = 'button'; previewButton.textContent = 'Preview';
    const apply = document.createElement('button'); apply.type = 'button'; apply.textContent = 'Apply selection'; apply.disabled = true;
    const status = document.createElement('small'); status.textContent = 'Restore only removes this selection; erased or hard-compacted content is not restored.';
    let preview = null;
    choice.addEventListener('change', () => { preview = null; apply.disabled = true; });
    async function submit(commit) {
      const chat = getChatID(), owner = account(), current = epoch, selected = choice.value;
      if (!chat || busy || disposed || (commit && !preview)) return;
      busy = true; previewButton.disabled = true; apply.disabled = true; controller = new AbortController();
      try {
        const response = await fetch(`/api/orchestration/${encodeURIComponent(chat)}/context-selection`, {
          method:'POST', credentials:'same-origin', headers:{'Content-Type':'application/json'}, signal:controller.signal,
          body:JSON.stringify({action:selected, partIDs:[ref.id], ...(commit ? {expected:preview} : {})}),
        });
        if (!response.ok) throw new Error('Selection unavailable or stale');
        const result = await response.json();
        if (disposed || current !== epoch || chat !== getChatID() || owner !== account()) return;
        if (commit) { preview = null; status.textContent = 'Selection applied. Inspect again for current evidence.'; }
        else if (choice.value !== selected) { preview = null; status.textContent = 'Selection changed; preview again.'; }
        else { preview = result.preview; status.textContent = `Preview ready for ${selected}; apply to confirm.`; apply.disabled = false; }
      } catch (error) {
        if (!disposed && current === epoch && chat === getChatID() && owner === account() && error.name !== 'AbortError') {
          preview = null; status.textContent = 'Selection unavailable, stale, or outcome unknown. Inspect again.';
        }
      } finally { if (!disposed && current === epoch) { busy = false; previewButton.disabled = false; } }
    }
    previewButton.addEventListener('click', () => void submit(false));
    apply.addEventListener('click', () => void submit(true));
    box.append(choice, previewButton, apply, status); return box;
  }
  action.addEventListener('click', load);
  document.addEventListener('odysseus:session-selected', invalidate);
  document.addEventListener('openclank:auth-context-changed', invalidate);
  window.addEventListener('openclank:auth-context-changed', invalidate);
  return {dispose() { disposed = true; invalidate(); document.removeEventListener('odysseus:session-selected', invalidate); document.removeEventListener('openclank:auth-context-changed', invalidate); window.removeEventListener('openclank:auth-context-changed', invalidate); root.remove(); }};
}
