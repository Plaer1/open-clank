// One chat/draft owner for both the menu and explicit slash command.
export const RESPONSE_STYLES=Object.freeze(['normal','concise','brief','minimal']);
export function createResponseStyleController({session=()=>window.sessionModule, account=()=>window.chatModule?.ratingAccountScope?.(), request=(url,options)=>fetch(url,options)}={}) {
  const valid=mode=>RESPONSE_STYLES.includes(mode);
  let unsent=null;
  const same=(a,b)=>!!a && !!b && a.s===b.s && a.owner===b.owner && a.authEpoch===b.authEpoch && a.sid===b.sid && a.draft===b.draft && a.nav===b.nav;
  const remembered=c=>same(unsent?.scope,c) ? unsent.mode : null;
  const retain=(c,mode)=>{if(current(c))unsent={scope:c,mode};};
  const capture=()=>{const s=session(), a=account(), sid=s?.getCurrentSessionId?.(), draft=s?.getPendingChat?.(), nav=s?.getSessionNavigationToken?.();
    if(!s || !a?.owner || a.sid!==sid || document.body?.dataset?.accountId!==a.owner || nav===undefined || (!sid&&!draft) || (sid ? s.isCurrentSessionIncognito?.() : document.getElementById('incognito-toggle')?.checked))return null;
    return {s,owner:a.owner,authEpoch:a.authEpoch,sid,draft:sid?null:draft,nav};};
  const current=c=>{const a=account(),s=session();return !!c && s===c.s && a?.owner===c.owner && a.authEpoch===c.authEpoch && a.sid===c.sid && document.body?.dataset?.accountId===c.owner && s.getCurrentSessionId()===c.sid && s.getSessionNavigationToken()===c.nav && (c.sid ? !s.isCurrentSessionIncognito?.() : s.getPendingChat()===c.draft && !document.getElementById('incognito-toggle')?.checked);};
  const url=c=>`/api/session/${encodeURIComponent(c.sid)}/response-style`;
  const read=async(c=capture())=>{if(!current(c))return {state:'unavailable'};
    if(!c.sid){const mode=c.draft.responseStyle || 'normal';if(!valid(mode))return {state:'unavailable'};if(c.draft.styleCreateUnknown)return {state:'unknown',requested:remembered(c) || mode};return {state:'draft',mode,requested:remembered(c)};}
    try {const r=await request(url(c),{credentials:'same-origin',cache:'no-store'});if(!current(c))return {state:'stale'};if(!r.ok)return {state:'unavailable'};const v=await r.json();if(!current(c))return {state:'stale'};
      if(!v || Object.keys(v).sort().join()!=='mode,revision' || !valid(v.mode) || !Number.isSafeInteger(v.revision) || v.revision<0)return {state:'unavailable'};
      return {state:'saved',mode:v.mode,revision:v.revision,requested:remembered(c)};}catch{return current(c)?{state:'unavailable'}:{state:'stale'};}};
  const set=async(mode,c=capture())=>{if(!valid(mode)||!current(c))return {state:'unavailable',requested:mode};
    if(!c.sid){if(c.s.isPendingChatMaterializing?.() || c.draft.styleCreateUnknown){retain(c,mode);return {state:'unknown',requested:mode};}c.draft.responseStyle=mode;unsent=null;return {state:'draft',mode};}
    const before=await read(c);if(!current(c))return {state:'stale',requested:mode};if(before.state!=='saved'){retain(c,mode);return {state:'unavailable',requested:mode};}
    try {const r=await request(url(c),{method:'PUT',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode,expected_revision:before.revision})});
      if(!current(c))return {state:'stale',requested:mode};
      if(r.status===409){retain(c,mode);const latest=await read(c);return current(c)?{state:'conflict',requested:mode,current:latest}:{state:'stale',requested:mode};}
      if(!r.ok){if(r.status>=500){retain(c,mode);const latest=await read(c);return current(c)?{state:'unknown',requested:mode,current:latest}:{state:'stale',requested:mode};}retain(c,mode);return {state:'unavailable',requested:mode};}
      const v=await r.json();if(!current(c))return {state:'stale',requested:mode};
      if(!v || Object.keys(v).sort().join()!=='mode,revision' || v.mode!==mode || !Number.isSafeInteger(v.revision) || v.revision!==(before.mode===mode ? before.revision : before.revision+1)) {retain(c,mode);const latest=await read(c);return current(c)?{state:'unknown',requested:mode,current:latest}:{state:'stale',requested:mode};}
      unsent=null;return {state:'saved',mode:v.mode,revision:v.revision};
    }catch{if(!current(c))return {state:'stale',requested:mode};retain(c,mode);const latest=await read(c);return current(c)?{state:'unknown',requested:mode,current:latest}:{state:'stale',requested:mode};}};
  return Object.freeze({capture,current,read,set});
}
export const responseStyle=createResponseStyleController();
