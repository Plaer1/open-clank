import { wireDialog } from './copal/overlays.js';

/** Current-account discovery only; publication IDs are selectors, not grants. */
export function openSharedResources({isCurrent, openSharedText, fetchImpl = fetch, wire = wireDialog} = {}) {
  if (typeof isCurrent !== 'function' || typeof openSharedText !== 'function' || isCurrent() !== true)
    throw new Error('Shared files unavailable.');
  const account = document.body.dataset.accountId;
  if (!account) throw new Error('Shared files unavailable.');
  const originalAuth = window.chatModule?.ratingAccountScope?.();
  let closed = false, busy = false, cursor = null, pageCount = 0;
  const abort = new AbortController(), seen = new Set(), seenCursors = new Set();
  const dialog = document.createElement('dialog'); dialog.className = 'styled-dialog'; dialog.setAttribute('aria-label','Shared with me');
  const title = document.createElement('h3'); title.textContent = 'Shared with me';
  const note = document.createElement('p'); note.textContent = 'Open a shared file in Notes. Access is checked again when it opens.';
  const list = document.createElement('div'), status = document.createElement('p'); status.setAttribute('role','status');
  const footer = document.createElement('div'); footer.className='modal-footer';
  const more = document.createElement('button'); more.type='button'; more.className='confirm-btn'; more.textContent='Load more'; more.hidden=true;
  const closeButton = document.createElement('button'); closeButton.type='button'; closeButton.className='confirm-btn'; closeButton.textContent='Close';
  footer.append(more,closeButton); dialog.append(title,note,list,status,footer); document.body.append(dialog); wire(dialog);
  const authCurrent = () => {
    if (document.body.dataset.accountId !== account) return false;
    if (!originalAuth) return true;
    const now=window.chatModule?.ratingAccountScope?.();
    return !!now && now.owner===originalAuth.owner && now.authEpoch===originalAuth.authEpoch;
  };
  const current = () => {
    if (closed) return false;
    try { if (authCurrent() && isCurrent() === true) return true; } catch (_) {}
    done(); return false;
  };
  const cleanup = () => {
    if (closed) return; closed=true; abort.abort();
    for (const target of [window,document]) {
      for (const name of ['openclank:auth-context-changed','openclank-account-changed']) target.removeEventListener(name, invalidate);
      target.removeEventListener('openclank-window-closed', checkScope);
    }
  };
  const done = () => { cleanup(); if(dialog.open) dialog.close(); else dialog.remove(); };
  const invalidate = () => done();
  const checkScope = () => { current(); };
  for (const target of [window,document]) {
    for (const name of ['openclank:auth-context-changed','openclank-account-changed']) target.addEventListener(name,invalidate);
    target.addEventListener('openclank-window-closed',checkScope);
  }
  dialog.addEventListener('close',cleanup,{once:true});closeButton.addEventListener('click',done);
  async function load() {
    if (!current() || busy) return; busy=true;more.disabled=true;status.textContent='Loading shared files…';
    try {
      const query=cursor ? '?cursor='+encodeURIComponent(cursor) : '';
      const response=await fetchImpl('/api/files/shared-with-me'+query,{credentials:'same-origin',cache:'no-store',signal:abort.signal});
      const value=await response.json(); if(!current())return;
      if(!response.ok || !value || !Array.isArray(value.items) || value.items.length>20 || typeof value.partial!=='boolean' ||
          !(value.next_cursor===null || (typeof value.next_cursor==='string' && /^sw1\.[A-Za-z0-9_-]{1,4092}={0,2}$/.test(value.next_cursor)))) throw new Error('Shared files unavailable.');
      const next=value.next_cursor;if(next && seenCursors.has(next))throw new Error('Shared files changed. Reopen the list.');
      for(const item of value.items) {
        if(!item || typeof item.publication_id!=='string' || !/^[0-9a-f]{32}$/.test(item.publication_id) ||
            typeof item.resource_id!=='string' || !item.resource_id || item.resource_id.length>512 ||
            typeof item.filename!=='string' || !item.filename || item.filename.length>1024 || !['viewer','commenter','editor'].includes(item.role)) throw new Error('Shared files unavailable.');
      }
      for(const item of value.items) {
        if(seen.has(item.resource_id))continue;seen.add(item.resource_id);
        const row=document.createElement('div'), button=document.createElement('button');button.type='button';button.className='confirm-btn';
        button.textContent=item.filename+' · '+item.role;button.addEventListener('click',async()=>{
          if(!current() || button.disabled)return;button.disabled=true;status.textContent='Opening shared file…';
          try {let result;try {result=await openSharedText({publicationID:item.publication_id,name:item.filename,isCurrent:authCurrent});} catch (_) {if(!current())return;const copal=await import('./copal.js');result=await copal.openSharedImage({publicationID:item.publication_id,name:item.filename,isCurrent:authCurrent});}if(!current())return;if(!result)throw new Error('Shared file unavailable.');done();}
          catch(error){if(current()){status.textContent=error?.message || 'Shared file unavailable.';button.disabled=false;}}
        });row.append(button);list.append(row);
      }
      pageCount++;cursor=next;if(next)seenCursors.add(next);more.hidden=!next || pageCount>=50;status.textContent=pageCount>=50 && next ? 'Showing the first 1,000 shared files.' : seen.size ? (value.partial && !next ? 'Some shared files are unavailable.' : '') : (next ? 'No files on this page.' : value.partial ? 'Shared files are unavailable.' : 'No shared files.');
    } catch(error) {if(current() && error?.name!=='AbortError'){status.textContent=error?.message || 'Shared files unavailable.';more.hidden=true;}}
    finally {busy=false;more.disabled=false;}
  }
  more.addEventListener('click',()=>void load());dialog.showModal();void load();
  return Object.freeze({close:done});
}
