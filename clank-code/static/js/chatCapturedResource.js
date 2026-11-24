/** The locator selects a recorded action; current Human Files remains authoritative. */
export async function openCapturedResult({locator,chat,resourceID=null,isCurrent}) {
  if (typeof isCurrent!=='function' || !isCurrent() || typeof locator!=='string' || !chat ||
      (resourceID!==null && (!locator.startsWith('oc-history-action:v2:') || !/^resource-[0-9a-f]{64}$/.test(resourceID)))) throw Error('Saved file unavailable.');
  const response=await fetch('/api/history/captured-resource',{method:'POST',credentials:'same-origin',cache:'no-store',headers:{'Content-Type':'application/json'},body:JSON.stringify({locator,chat,...(resourceID===null?{}:{resourceID})})});
  if(!response.ok || !isCurrent())throw Error('Saved file unavailable.');
  const result=await response.json();
  if(!isCurrent() || result?.version!==1 || typeof result.resourceRef!=='string' || !result.resourceRef ||
      (resourceID!==null && result.resourceID!==resourceID) || result.revision?.kind!=='hostFingerprint' ||
      !/^sha256:[a-f0-9]{64}:(?:0|[1-9][0-9]*)$/.test(result.revision.value || ''))throw Error('Saved file unavailable.');
  const editor=await import('./copal.js');if(!isCurrent())return false;
  try {if(typeof editor.openResource!=='function' || !await editor.openResource(result.resourceRef,{expectedRevision:result.revision,isCurrent}))throw Error('Editor unavailable.');}
  catch(_){if(!isCurrent())return false;const files=await import('./files.js');if(!isCurrent() || typeof files.revealResource!=='function' || !await files.revealResource(result.resourceRef,{expectedRevision:result.revision,isCurrent}))throw Error('Saved file unavailable.');}
  return true;
}
export function capturedResourceButton(raw) {
  const metadata = raw?.rawOutput?.metadata, locator = metadata?.history?.managed_action_ref;
  if (raw?.status !== 'completed' || typeof locator !== 'string' || locator.length > 4096) return null;
  const grouped = locator.startsWith('oc-history-action:v2:');
  if (!grouped && (metadata?.file?.contract !== 'open-clank.file-result/v1' || !['write','edit'].includes(metadata?.file?.operation) || metadata?.file?.kind !== 'text' || metadata?.file?.media_type === 'text/html' || !locator.startsWith('oc-history-action:v1:'))) return null;
  const scope = window.chatModule?.ratingAccountScope?.(), sessions = window.sessionModule, navigation = sessions?.getSessionNavigationToken?.();
  const button = document.createElement('button'); button.type = 'button'; button.className = 'copal-btn'; button.textContent = grouped ? 'Show saved files' : 'Open saved file';
  button.dataset.chatArtifact = grouped ? 'Saved files' : (metadata.file.path?.split?.(/[\\/]/).pop() || 'Saved file');
  const current = () => { const now = window.chatModule?.ratingAccountScope?.(); return button.isConnected && !!scope?.sid && !!scope?.owner && navigation !== undefined && now?.sid === scope.sid && now.owner === scope.owner && now.authEpoch === scope.authEpoch && document.body.dataset.accountId === scope.owner && sessions?.getCurrentSessionId?.() === scope.sid && sessions?.getSessionNavigationToken?.() === navigation; };
  if (grouped) {
    const container = document.createElement('span'); container.appendChild(button);
    button.addEventListener('click', async event => { event.stopPropagation(); if (button.disabled || !current()) return; button.disabled = true;
      try {
        const {resolveCapturedChange} = await import('./historyView.js');
        if (!current()) return;
        const data = await resolveCapturedChange(locator,current,false), members = data?.members;
        if (!current() || data?.version !== 2) throw Error('Saved files unavailable');
        // Names are optional display metadata from the existing Human-scoped
        // resolver, never selectors or authority for the member open.
        const names = new Map();
        try {
          const response = await fetch('/api/history/captured-change-metadata',{method:'POST',credentials:'same-origin',cache:'no-store',headers:{'Content-Type':'application/json'},body:JSON.stringify({version:1,locators:[locator]})});
          if (response.ok && current()) {
            const value = await response.json(), item = value?.version === 1 && value.items?.length === 1 ? value.items[0] : null;
            if (current() && item?.state === 'available' && item.version === 2 && item.coverage === 'partial_recorded_action' && Array.isArray(item.members) && item.members.length <= 64) {
              for (const member of item.members) {
                if (/^resource-[0-9a-f]{64}$/.test(member?.current_resource_id || '') && typeof member.current_name === 'string' && member.current_name.length > 0 && member.current_name.length <= 1024 && !names.has(member.current_resource_id)) names.set(member.current_resource_id,member.current_name);
              }
            }
          }
        } catch (_) { /* Names are optional; the exact member remains authoritative. */ }
        if (!current()) return;
        const list = document.createElement('span');
        members.forEach((member, index) => {
          const name = names.get(member.current_resource_id) || `saved file ${index + 1}`;
          const available = member.state === 'available' && member.item?.action_id === data.action_id && member.item?.after?.id && member.item.after.availability === 'available' && !member.item.ambiguous_phase;
          if (!available) { const label = document.createElement('span'); label.textContent = ` ${name} unavailable`; list.appendChild(label); return; }
          const open = document.createElement('button'); open.type = 'button'; open.className = 'copal-btn'; open.textContent = `Open ${name}`; open.dataset.chatArtifact = name;
          open.addEventListener('click', async click => { click.stopPropagation(); if (open.disabled || !current()) return; open.disabled = true;
            try { await openCapturedResult({locator,chat:scope.sid,resourceID:member.current_resource_id,isCurrent:current}); }
            catch (_) { if (current()) open.textContent = 'Saved file unavailable'; }
            finally { if (open.isConnected) open.disabled = false; }
          });
          list.appendChild(open);
        });
        if (current()) { container.appendChild(list); button.hidden = true; }
      } catch (_) { if (current()) button.textContent = 'Saved files unavailable'; }
      finally { if (button.isConnected) button.disabled = false; }
    });
    return container;
  }
  button.addEventListener('click', async event => { event.stopPropagation(); if (button.disabled || !current()) return; button.disabled = true;
    try {
      await openCapturedResult({locator,chat:scope.sid,isCurrent:current});
    } catch (_) { if (current()) button.textContent = 'Saved file unavailable'; }
    finally { if (button.isConnected) button.disabled = false; }
  });
  return button;
}
