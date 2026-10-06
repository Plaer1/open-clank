import { number } from './usageCharts.js';
import { mdToHtml, sanitizeAllowedHtml } from './markdown.js';
// Conversation presentation over lossless archive rows; no HTML from saved bodies.
const node = (tag, text = '', cls = '') => { const n = document.createElement(tag); n.textContent = text; n.className = cls; return n; };
const compact = value => value != null && value !== '' && number(value)!=null ? Intl.NumberFormat(undefined, { notation: 'compact', maximumFractionDigits: 1 }).format(number(value)) : '—';
export function metricText(metric, money = false) {
  const value = metric?.value ?? metric?.amount;
  if (value == null || !['reported', 'partial', 'complete', 'observed', 'estimated'].includes(metric?.state || 'reported')) return '—';
  const qualifier = metric?.state === 'estimated' ? ' ≈' : metric?.state === 'partial' ? ' · partial' : '';
  return (money ? Intl.NumberFormat(undefined, { style: 'currency', currency: metric.currency || 'USD', maximumFractionDigits: 2 }).format(number(value)) : compact(value)) + qualifier;
}
export function identityLabels(values) { return (values || []).map(item => item.label || item.name || item.handle).filter(Boolean); }
export function sessionMeta(session) {
  const time = session.last_activity_ms ? new Date(session.last_activity_ms).toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) : '';
  return [session.workspace_identity?.label, time, session.message_count ? metricText(session.message_count) + ' msgs' : session.part_count != null ? session.part_count + ' parts' : '', session.tokens ? metricText(session.tokens) + ' tokens' : ''].filter(Boolean).join(' · ');
}
function envelope(part) {
  if (part.runtime_generation !== 'managed-mimo-v1' || part.source_ref?.authority !== 'conversation_archive' || part.body_truncated) return null;
  try { const data = JSON.parse(part.text); return data && data.type === part.part_type && data.id === part.part_id && data.messageID === part.message_id ? data : null; } catch { return null; }
}
function disclosure(label, text) { const d = node('details', '', 'usage-evidence'); d.append(node('summary', label), node('pre', text, 'logging-source')); return d; }
function prose(text) {
  const container = node('div', '', 'usage-message-body');
  const template = document.createElement('template');
  template.innerHTML = sanitizeAllowedHtml(mdToHtml(String(text)));
  // Archived prose never loads remote media automatically. Identity/body stays in evidence.
  for (const media of template.content.querySelectorAll('img,video,audio,iframe')) media.replaceWith(node('span', `[${media.getAttribute('alt') || 'Saved media'}]`));
  container.append(template.content);
  window.odysseusHighlight?.highlightAll(container);
  return container;
}
export function renderConversation(data, { onChunk, onWorkspace, onSourceSession, search = false } = {}) {
  const viewport = node('div', '', 'logs-viewport logs-transcript usage-transcript');
  let previous = null, group = null;
  for (const part of data.items || []) {
    const saved = envelope(part), role = part.role || 'Observed';
    const technical = part.presentation_kind === 'engine_evidence' || role === 'system' || saved?.synthetic === true || saved?.ignored === true || ['step-start', 'step-finish', 'snapshot', 'patch', 'compaction'].includes(part.part_type);
    const thinking = part.part_type === 'reasoning';
    const tool = ['tool', 'tool_call', 'tool_result', 'tool_use', 'tool_output'].includes(part.part_type);
    const kind = technical ? 'technical' : thinking ? 'thinking' : tool ? 'tool' : role === 'user' ? 'user' : role === 'assistant' ? 'assistant' : 'observed';
    const key = search ? part.part_id : technical ? 'consecutive-technical' : `${part.actor_id}:${part.message_id}:${kind}`;
    if (!group || key !== previous) {
      group = node(technical ? 'details' : 'article', '', `logs-part usage-message usage-message-${kind}`);
      const heading = node('header', '', 'usage-message-heading');
      const title = technical ? 'Supplied context / technical details' : thinking ? 'Thinking' : tool ? 'Tool calls' : role === 'user' ? 'User' : role === 'assistant' ? 'Assistant' : role;
      heading.append(node('strong', title));
      const stamp = part.time_created || part.time_updated;
      if (stamp) heading.append(node('time', new Date(stamp).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })));
      group.append(technical ? node('summary', 'Supplied context / technical details') : heading); viewport.append(group); previous = key;
    }
    const row = node('section', '', 'usage-message-part'); row.dataset.partId = part.part_id || '';
    const text = typeof saved?.text === 'string' ? saved.text : part.text || '(No saved text body)';
    if (technical || thinking || tool) {
      const label = technical ? part.part_type + (saved?.synthetic ? ' · supplied by engine' : '') : thinking ? 'Show reasoning' : [part.tool_name || part.part_type, part.status].filter(Boolean).join(' · ');
      row.append(disclosure(label, text));
    } else row.append(prose(text));
    const evidence = disclosure('Exact saved evidence', JSON.stringify({ ...part, text: part.text }, null, 2));
    const controls = node('div', '', 'usage-part-controls');
    const copy = node('button', 'Copy'); copy.type = 'button'; copy.addEventListener('click', async () => { try { await navigator.clipboard.writeText(text); copy.textContent = 'Copied'; } catch { copy.textContent = 'Copy unavailable'; } }); controls.append(copy);
    if (part.body_truncated && onChunk) { const more = node('button', 'Read body chunks'); more.type = 'button'; more.addEventListener('click', () => onChunk(part, row)); controls.append(more); }
    if (part.workspace_handle && onWorkspace) { const filter = node('button', 'Workspace'); filter.type = 'button'; filter.addEventListener('click', () => onWorkspace(part.workspace_handle)); evidence.append(filter); }
    if (search && onSourceSession) { const open = node('button', 'Open matching conversation'); open.type = 'button'; open.addEventListener('click', () => onSourceSession(part)); controls.append(open); }
    const footer=node('footer','','usage-message-footer');footer.append(controls,evidence);row.append(footer);group.append(row);
  }
  if (!data.items?.length) viewport.append(node('p', data.coverage?.state === 'unavailable' ? 'Conversation evidence unavailable.' : search ? 'No saved parts match this search.' : 'No saved parts match this filter.', 'usage-empty'));
  return viewport;
}
export function renderVitals(session, data) {
  const panel = node('aside', '', 'usage-session-vitals'); panel.setAttribute('aria-label', 'Session analysis');
  panel.append(node('h3', 'Session analysis'));
  const section = (title, rows) => { const s = node('section', '', 'usage-vitals-section'); s.append(node('h4', title)); const list = node('dl'); for (const [label, value, state] of rows) { const v = node('dd', value); if (state && state !== 'reported') v.title = state; list.append(node('dt', label), v); } s.append(list); panel.append(s); };
  section('Conversation', [['Messages', metricText(session.message_count), session.message_count?.state], ['Saved parts', compact(session.part_count)], ['Actors', compact(session.actor_count)], ['Workspace', session.workspace_identity?.label || '—']]);
  section('Observed usage', [['Tokens', metricText(session.tokens), session.tokens?.state], ['Cost', metricText(session.cost, true), session.cost?.state], ...Object.entries(session.token_metrics || {}).map(([k,v]) => [k.replaceAll('_',' '), metricText(v), v?.state])]);
  section('Models / providers', [['Models', identityLabels(session.model_identities).join(', ') || '—'], ['Providers', identityLabels(session.provider_identities).join(', ') || '—']]);
  section('Tools / timing', [['Tool calls', metricText(session.tools?.calls != null ? { value: session.tools.calls, state: session.tools.state } : null), session.tools?.state], ['Tool time', session.tools?.duration_ms != null ? compact(session.tools.duration_ms / 1000) + 's' : '—'], ['Timing coverage', session.tools?.coverage?.reason || session.tools?.coverage?.state || 'Not observed']]);
  panel.append(disclosure('Session source / coverage', JSON.stringify({ session, coverage: data.coverage, actors: data.actors?.coverage }, null, 2)));
  return panel;
}
