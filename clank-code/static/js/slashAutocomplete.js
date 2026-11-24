// static/js/slashAutocomplete.js
// Lightweight popup that surfaces the existing /command registry as users
// type. Reads COMMANDS from slashCommands.js — no command logic lives here.

import { loadCommandCatalog } from './slashCommands.js';

const POPUP_ID = 'slash-autocomplete';
const MAX_VISIBLE = 14;
const EXCLUDED = new Set(['flip','roll','8ball','fortune','odyssey','ascii']);

function _catalogEntries(catalog) {
  const entries = [];
  for (const entry of catalog.entries) {
    if (entry.hidden || EXCLUDED.has(entry.name)) continue;
    entries.push(entry);
    // An alias row is only inserted when it resolves to exactly this identity.
    for (const token of entry.aliases) if (catalog.winners.get(token.toLowerCase()) === entry) entries.push({ ...entry, token, aliases: [], usage: `${token} ${entry.arguments}`.trim() });
  }
  return entries;
}

function _scoreMatch(entry, query) {
  // query already starts with "/". Match against token + aliases. Prefix wins
  // over substring; alias match scores slightly lower than token match.
  const q = query.toLowerCase();
  const t = entry.token.toLowerCase();
  if (t === q) return 1000;
  if (t.startsWith(q)) return 500 + (50 - Math.min(50, t.length - q.length));
  for (const a of entry.aliases) {
    const al = a.toLowerCase();
    if (al === q) return 900;
    if (al.startsWith(q)) return 400;
  }
  if (t.includes(q)) return 100;
  if ([entry.help, ...(entry.searchTerms || [])].some(value => String(value).toLowerCase().includes(q.slice(1)))) return 25;  // help text
  return 0;
}

function _exactCommandGroupItems(all, query) {
  const q = query.toLowerCase();
  if (!/^\/[a-z0-9_-]+$/i.test(q)) return [];
  const parent = all.find(entry => entry.token.toLowerCase() === q);
  if (!parent) return [];
  const prefix = q + ' ';
  const children = all.filter(entry => entry.token.toLowerCase().startsWith(prefix));
  if (!children.length) return [];
  return children.concat(parent);
}

function _ensurePopup(textarea) {
  let el = document.getElementById(POPUP_ID);
  if (el) return el;
  el = document.createElement('div');
  el.id = POPUP_ID;
  el.className = 'slash-autocomplete-popup';
  el.setAttribute('role', 'listbox');
  el.setAttribute('aria-label', 'Slash commands');
  document.body.appendChild(el);
  return el;
}

function _position(popup, textarea) {
  const r = textarea.getBoundingClientRect();
  const maxH = Math.min(window.innerHeight * 0.5, 360);
  popup.style.maxHeight = maxH + 'px';
  // Anchor above the textarea, left-aligned with it
  popup.style.left = Math.round(r.left) + 'px';
  popup.style.width = Math.max(280, Math.round(Math.min(r.width, 520))) + 'px';
  // Place above when there's enough room, otherwise below.
  const aboveSpace = r.top;
  if (aboveSpace > maxH + 20) {
    popup.style.bottom = (window.innerHeight - r.top + 6) + 'px';
    popup.style.top = '';
  } else {
    popup.style.top = (r.bottom + 6) + 'px';
    popup.style.bottom = '';
  }
}

function _render(popup, items, selectedIdx, query, notice = '') {
  if (!items.length) {
    popup.innerHTML = `<div class="slash-ac-empty" role="status">${notice ? _esc(notice) : 'No commands match <code>' + _esc(query) + '</code>'}</div>`;
    return;
  }
  // Group by category for the headers
  let html = '';
  let lastCat = null;
  for (let i = 0; i < items.length; i++) {
    const it = items[i];
    if (it.category !== lastCat) {
      html += `<div class="slash-ac-cat">${_esc(it.category || 'Other')}</div>`;
      lastCat = it.category;
    }
    const sel = i === selectedIdx ? ' slash-ac-row-sel' : '';
    const usage = it.usage && it.usage !== it.token ? ` <span class="slash-ac-usage">${_esc(it.usage)}</span>` : '';
    html += `<div class="slash-ac-row${sel}" role="option" aria-selected="${i === selectedIdx}" data-idx="${i}" data-token="${_esc(it.token)}">`
         +    `<span class="slash-ac-token">${_esc(it.token)}</span>`
         +    `<span class="slash-ac-help">${_esc(it.help)} · ${_esc(it.origin || '')} · ${_esc(it.scope || '')}${it.availability !== 'available' ? ' · ' + _esc(it.availability || 'unavailable') + ': ' + _esc(it.reason || '') : ''}</span>`
         +    usage
         + `</div>`;
  }
  html += `<div class="slash-ac-empty" role="status">${notice ? _esc(notice) + ' · ' : ''}Use /commands for the complete filtered catalog.</div>`;
  popup.innerHTML = html;
  // Scroll selected into view
  const selEl = popup.querySelector('.slash-ac-row-sel');
  if (selEl) selEl.scrollIntoView({ block: 'nearest' });
}

function _esc(s) {
  return String(s).replace(/[&<>"']/g, c => ({ '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;','\'':'&#39;' }[c]));
}

export function initSlashAutocomplete(textarea) {
  if (!textarea || textarea._slashAcWired) return;
  textarea._slashAcWired = true;

  let all = [];
  let catalogRevision = 0;
  let catalogNotice = 'Loading command catalog';
  let popup = null;
  let visible = false;
  let items = [];
  let selectedIdx = 0;

  const hide = () => {
    if (!visible) return;
    visible = false;
    if (popup) popup.style.display = 'none';
    textarea.setAttribute('aria-expanded', 'false');
  };

  const show = () => {
    if (!popup) popup = _ensurePopup(textarea);
    visible = true;
    popup.style.display = 'block';
    textarea.setAttribute('aria-controls', POPUP_ID);
    textarea.setAttribute('aria-expanded', 'true');
    _position(popup, textarea);
  };

  const refresh = () => {
    const v = textarea.value;
    // Only trigger when the message starts with "/" (no leading space) and
    // contains at most one space after the command (so subcommands work).
    // If the user has moved past the slash command (newline, longer prose),
    // the menu hides — we don't autocomplete mid-sentence.
    if (!v.startsWith('/') || v.includes('\n')) { hide(); return; }
    const query = v.trim();
    const groupItems = _exactCommandGroupItems(all, query);
    if (groupItems.length) {
      items = groupItems.slice(0, MAX_VISIBLE);
    } else {
      items = all
      .map(e => ({ e, s: _scoreMatch(e, query) }))
      .filter(x => x.s > 0)
      .sort((a, b) => b.s - a.s)
      .slice(0, MAX_VISIBLE)
      .map(x => x.e);
    }
    if (!items.length && query.length > 1) { selectedIdx = 0; show(); _render(popup, [], 0, query, catalogNotice); return; }
    if (!items.length) {
      // Just "/" with no matches — fall back to showing everything up to MAX_VISIBLE
      items = all.slice(0, MAX_VISIBLE);
    }
    selectedIdx = 0;
    show();
    _render(popup, items, selectedIdx, query, catalogNotice);
  };

  const refreshCatalog = async () => {
    const revision = ++catalogRevision;
    let catalog;
    try { catalog = await loadCommandCatalog({ fresh: true }); }
    catch (error) {
      if (revision !== catalogRevision) return;
      catalogNotice = 'Command catalog unavailable: ' + error.message + '. Focus the composer to retry.';
      if (textarea.value.startsWith('/')) refresh();
      return;
    }
    if (revision !== catalogRevision) return;
    catalogNotice = (catalog.errors || []).join(' · ');
    all = _catalogEntries(catalog);
    if (textarea.value.startsWith('/')) refresh();
  };
  refreshCatalog();
  for (const event of ['odysseus:session-selected', 'odysseus:mimo-state-changed', 'odysseus:skills-changed', 'odysseus:skill-trust-changed', 'odysseus:workspace-changed', 'odysseus:provider-changed', 'odysseus:mcp-changed', 'odysseus:command-catalog-changed', 'openclank:languagechange']) document.addEventListener(event, refreshCatalog);

  const insert = (token) => {
    textarea.value = token + ' ';
    textarea.dispatchEvent(new Event('input', { bubbles: true }));
    textarea.focus();
    const len = textarea.value.length;
    textarea.setSelectionRange(len, len);
    hide();
  };

  textarea.addEventListener('input', refresh);
  textarea.addEventListener('focus', () => { if (textarea.value.startsWith('/')) { refresh(); refreshCatalog(); } });
  textarea.addEventListener('blur', () => { setTimeout(hide, 120); });  // delay so click works

  textarea.addEventListener('keydown', (e) => {
    if (!visible || !items.length) return;
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      selectedIdx = (selectedIdx + 1) % items.length;
      _render(popup, items, selectedIdx, textarea.value, catalogNotice);
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      selectedIdx = (selectedIdx - 1 + items.length) % items.length;
      _render(popup, items, selectedIdx, textarea.value, catalogNotice);
    } else if (e.key === 'Tab' || (e.key === 'Enter' && !e.shiftKey)) {
      // Tab always inserts. Enter inserts only when the user hasn't already
      // typed a full command + args — i.e. the popup is still in completion
      // mode, not in "ready to submit a typed-out command" mode.
      const v = textarea.value.trim();
      const exactHit = items.find(it => it.token === v || it.aliases.includes(v));
      if (e.key === 'Enter' && exactHit) {
        // User typed the whole command — let the normal submit path handle it
        hide();
        return;
      }
      e.preventDefault();
      insert(items[selectedIdx].token);
    } else if (e.key === 'Escape') {
      e.preventDefault();
      hide();
    }
  });

  // Re-position on window resize / scroll
  window.addEventListener('resize', () => { if (visible) _position(popup, textarea); });

  // Click handler on the popup (delegated)
  document.addEventListener('mousedown', (e) => {
    if (!visible || !popup) return;
    const row = e.target.closest?.('.slash-ac-row');
    if (row && popup.contains(row)) {
      e.preventDefault();
      const tok = row.dataset.token;
      if (tok) insert(tok);
    }
  });
}

export default { initSlashAutocomplete };
