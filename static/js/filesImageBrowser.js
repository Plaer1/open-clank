import { uiIcon, fileIcon } from './langIcons.js';
import { filesFacadeClient } from './filesFacadeClient.js';

function isFolder(entry) {
  return Array.isArray(entry?.capabilities) && entry.capabilities.includes('children');
}

function isImage(entry) {
  return String(entry?.kind || '').includes('image') || String(entry?.mime_type || '').startsWith('image/');
}

/**
 * Opens a Files-backed image navigator rooted at the visible Gallery folder.
 * It follows one folder at a time and exposes each folder's sealed pagination
 * cursor, so it neither flattens ordinary folders nor enumerates all images.
 */
export async function openFilesImageBrowser({ onPick, title = 'Pick from Files', empty = 'No images or folders here' } = {}) {
  if (typeof onPick !== 'function') throw new TypeError('onPick is required');
  const roots = await filesFacadeClient.roots();
  const root = (roots.entries || []).find((entry) => entry.provider === 'files' && entry.name === 'Gallery');
  if (!root?.ref) throw new Error('Gallery folder is unavailable');

  const overlay = document.createElement('div');
  overlay.style.cssText = 'position:fixed;inset:0;z-index:10001;background:rgba(0,0,0,0.7);display:flex;align-items:center;justify-content:center;';
  const panel = document.createElement('div');
  panel.style.cssText = 'background:var(--panel,#1e1e1e);border-radius:12px;padding:16px;max-width:500px;max-height:70vh;overflow-y:auto;width:90%;';
  const header = document.createElement('div');
  header.style.cssText = 'display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;gap:8px;';
  const back = document.createElement('button');
  back.type = 'button'; back.innerHTML = uiIcon('back', 16); back.setAttribute('aria-label', 'Previous folder'); back.title = 'Previous folder'; back.hidden = true;
  const heading = document.createElement('span'); heading.style.cssText = 'font-size:13px;font-weight:600;flex:1;'; heading.textContent = title;
  const close = document.createElement('button');
  close.type = 'button'; close.innerHTML = uiIcon('close', 16); close.setAttribute('aria-label', 'Close image picker'); close.title = 'Close image picker'; close.style.cssText = 'background:none;border:none;color:var(--fg);cursor:pointer;font-size:18px;';
  header.append(back, heading, close);
  const grid = document.createElement('div');
  grid.style.cssText = 'display:grid;grid-template-columns:repeat(auto-fill,minmax(80px,1fr));gap:6px;';
  const more = document.createElement('button');
  more.type = 'button'; more.textContent = 'Load more'; more.hidden = true;
  more.style.cssText = 'margin-top:10px;width:100%;';
  panel.append(header, grid, more); overlay.appendChild(panel); document.body.appendChild(overlay);
  close.addEventListener('click', () => overlay.remove());
  overlay.addEventListener('click', (event) => { if (event.target === overlay) overlay.remove(); });

  const stack = [];
  let current = { ref: root.ref, name: root.name, cursor: null };
  async function render({ append = false } = {}) {
    if (!append) grid.replaceChildren();
    const response = await filesFacadeClient.children(current.ref, { cursor: append ? current.cursor : null, limit: 50 });
    for (const entry of response.entries || []) {
      if (!isFolder(entry) && !isImage(entry)) continue;
      const tile = document.createElement('button'); tile.type = 'button';
      tile.style.cssText = 'min-width:0;border:1px solid var(--border);border-radius:6px;background:var(--bg);color:var(--fg);padding:4px;cursor:pointer;';
      if (isFolder(entry)) {
        const label = document.createElement('span'); label.textContent = entry.name || 'Folder';
        const icon = document.createElement('span'); icon.innerHTML = fileIcon({ ...entry, kind: 'folder' }, 32);
        tile.append(icon, label);
        tile.addEventListener('click', () => { stack.push(current); current = { ref: entry.ref, name: entry.name || 'Folder', cursor: null }; render().catch(showError); });
      } else {
        const image = document.createElement('img'); image.src = filesFacadeClient.contentUrl(entry.ref, { purpose: 'preview' }); image.alt = entry.name || 'Image';
        image.style.cssText = 'width:100%;aspect-ratio:1;object-fit:cover;border-radius:4px;display:block;'; tile.appendChild(image);
        tile.addEventListener('click', () => { overlay.remove(); onPick(entry); });
      }
      grid.appendChild(tile);
    }
    current.cursor = response.next_cursor || null;
    more.hidden = !current.cursor; back.hidden = stack.length === 0; heading.textContent = `${title} · ${current.name}`;
    if (!grid.children.length) grid.textContent = empty;
  }
  function showError(error) { grid.textContent = error?.message || 'Could not load this folder'; more.hidden = true; }
  more.addEventListener('click', () => render({ append: true }).catch(showError));
  back.addEventListener('click', () => { const prior = stack.pop(); if (prior) { current = prior; current.cursor = null; render().catch(showError); } });
  await render();
}
