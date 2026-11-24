// Read-only memory explorer: pure helpers (unit-testable) plus one render function. Titles and counts only.
export const EVENT_LABELS = { created: 'Created', edited: 'Edited', superseded: 'Superseded', forgotten: 'Forgotten' };

export function sortedCounts(map) {
  return Object.entries(map || {}).filter(([, n]) => Number.isSafeInteger(n) && n >= 0).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
}

/** Fill missing days with 0 so the series reads as time, ending at `endDay` (YYYY-MM-DD, UTC). */
export function dailySeries(growth, days, endDay) {
  const known = new Map((growth || []).map(g => [g.day, g.created]));
  const end = new Date(endDay + 'T00:00:00Z');
  const out = [];
  for (let i = days - 1; i >= 0; i--) {
    const d = new Date(end.getTime() - i * 86400000).toISOString().slice(0, 10);
    out.push({ day: d, created: known.get(d) || 0 });
  }
  return out;
}

/** Sum a daily series into 7-day buckets, oldest first; the last bucket may be shorter. */
export function weeklySeries(daily) {
  const out = [];
  for (let i = 0; i < daily.length; i += 7) {
    const slice = daily.slice(i, i + 7);
    out.push({ from: slice[0].day, created: slice.reduce((n, d) => n + d.created, 0) });
  }
  return out;
}

export function bar(value, max, width = 20) {
  if (!max || value <= 0) return '';
  return '█'.repeat(Math.max(1, Math.round((value / max) * width)));
}

export function changeLine(change) {
  const label = EVENT_LABELS[change.event] || 'Changed';
  const name = change.title || (typeof change.block_id === 'string' ? change.block_id.slice(0, 8) : 'memory');   // an opaque short id, never the kind again
  return `${label} · ${name} · ${change.kind || 'memory'} · rev ${change.revision} · ${change.at || ''}`;
}

export function unavailableList(data) {
  if (!data) return ['everything'];
  if (data.state === 'legacy') return data.unavailable || [];
  const out = [];
  if (data.links?.domains?.state === 'unavailable') out.push('domains');
  if (data.truncated) out.push('complete counts (store larger than the read bound)');
  return out;
}

function el(tag, text, cls) { const n = document.createElement(tag); if (text != null) n.textContent = text; if (cls) n.className = cls; return n; }

export function renderExplorer(root, data, { onSelect, onOpenReference, today = new Date().toISOString().slice(0, 10) } = {}) {
  root.replaceChildren();
  if (!data || (data.state !== 'ready' && data.state !== 'legacy')) { root.append(el('p', 'Explorer is unavailable.')); return; }
  const head = el('div', null, 'memory-explorer-head');
  head.append(el('strong', `${data.total} memories`), el('span', data.state === 'legacy' ? ' · legacy store: some figures unavailable' : ' · typed store'));
  root.append(head);
  for (const [title, map] of [['By kind', data.by_kind], ['By state', data.by_status]]) {
    const section = el('section'); section.append(el('h4', title));
    if (!map) { section.append(el('p', 'Unavailable on this store.', 'settings-hint')); root.append(section); continue; }
    const rows = sortedCounts(map), max = rows[0]?.[1] || 0;
    for (const [name, n] of rows) section.append(el('div', `${name.padEnd(14)} ${String(n).padStart(5)} ${bar(n, max)}`, 'memory-explorer-row'));
    root.append(section);
  }
  const growth = el('section'); growth.append(el('h4', 'Growth (created per week, last 90 days)'));
  const weeks = weeklySeries(dailySeries(data.growth, 90, today)), wmax = Math.max(0, ...weeks.map(w => w.created));
  for (const w of weeks) growth.append(el('div', `${w.from} ${String(w.created).padStart(5)} ${bar(w.created, wmax)}`, 'memory-explorer-row'));
  root.append(growth);
  const recent = el('section'); recent.append(el('h4', 'Newest changes'));
  if (!data.recent?.length) recent.append(el('p', 'No changes yet.', 'settings-hint'));
  for (const change of data.recent || []) {
    const row = el('button', changeLine(change), 'memory-explorer-change'); row.type = 'button';
    row.addEventListener('click', () => onSelect?.(change.block_id));
    recent.append(row);
  }
  root.append(recent);
  const links = el('section'); links.append(el('h4', 'Links of the selected memory'));
  if (!data.links) links.append(el('p', 'Select a change above.', 'settings-hint'));
  else if (data.links.state !== 'ready') links.append(el('p', 'Links unavailable for this record.', 'settings-hint'));
  else {
    links.append(el('p', `${data.links.title || data.links.block_id} · evidence ${data.links.evidence_count ?? 'unavailable'} · domains unavailable`));
    links.append(el('p', `Supersedes ${data.links.supersedes.length} earlier revision(s)` + data.links.supersedes.map(s => ` · r${s.revision} ${s.status}`).join('')));
    links.append(el('p', `Cited by ${data.links.cited_by.length}: ` + (data.links.cited_by.map(c => c.title || c.block_id).join(', ') || 'none')));
  }
  root.append(links);
  if (data.current_plan_references) {
    const refs = data.current_plan_references;
    const section = el('section'); section.append(el('h4', 'Current plan references'));
    for (const item of Array.isArray(refs.items) ? refs.items : []) {
      if (!item || typeof item.resourceRef !== 'string' || typeof item.role !== 'string') continue;
      const button = el('button', item.name ? `${item.role} · ${item.name}` : item.role, 'memory-explorer-change');
      button.type = 'button'; button.addEventListener('click', () => onOpenReference?.(item.resourceRef)); section.append(button);
    }
    if (refs.complete !== true) section.append(el('p', 'Some current plan references could not be checked.', 'settings-hint'));
    else if (!refs.items?.length) section.append(el('p', 'No references in the current associated plan.', 'settings-hint'));
    root.append(section);
  }
  const gaps = unavailableList(data);
  if (gaps.length) root.append(el('p', 'Unavailable: ' + gaps.join(', '), 'settings-hint'));
}

const requests = new WeakMap();
export async function loadExplorer(root, block = null, context = {}) {
  if (!root) return;
  const serial = (requests.get(root) || 0) + 1;
  requests.set(root, serial);
  const scope = context.getScope?.() || null;
  const session = context.getSession?.() || null;
  const current = () => {
    const now = context.getScope?.() || null;
    return requests.get(root) === serial && (!context.getScope || (!!scope && !!now && scope.owner === now.owner && scope.authEpoch === now.authEpoch)) &&
      (!context.getSession || (context.getSession() || null) === session);
  };
  if (!current()) return;
  root.replaceChildren(el('p', 'Loading…'));
  let data = null;
  try {
    const response = await fetch('/api/memory/explorer?days=90' + (block ? '&block=' + encodeURIComponent(block) : '') +
      (session ? '&session=' + encodeURIComponent(session) : ''), { credentials:'same-origin', cache:'no-store' });
    data = response.ok ? await response.json() : null;
  } catch (_) { data = null; }
  if (!current()) return;
  renderExplorer(root, data, {
    onSelect: id => loadExplorer(root, id, context),
    onOpenReference: async ref => {
      if (!current()) return;
      try { const editor = await import('./codeEditor.js'); if (current()) await editor.openResource(ref); }
      catch (_) { if (current()) root.append(el('p', 'This plan reference is unavailable.', 'settings-hint')); }
    },
  });
}
