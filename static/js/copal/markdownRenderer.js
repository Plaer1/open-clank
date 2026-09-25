/**
 * Shared full-document Markdown renderer.
 *
 * Explicit callbacks replace applet-singleton imports so Code Editor rich
 * comments, Copal Editor, Wiki bodies and TreeHouse can consume one renderer
 * without initialization cycles. Rendering is never code execution: plugin
 * query blocks stay inert and app links go through a typed destination
 * registry, never the chat-command interpreter.
 */
import { findReferenceToken, createReferenceRenderer, extractReferenceSection } from './markdownResources.js';

// ---------------------------------------------------------------------------
// App destination registry (clank://<screen>/<panel-or-item>)
// ---------------------------------------------------------------------------
const appDestinations = new Map();

/**
 * Register a typed app destination handler. The handler opens/focuses the
 * screen and must preserve current drafts. It must not toggle an already-open
 * window closed or start an unrelated chat.
 */
export function registerAppDestination(name, handler) {
  const key = String(name || '').trim().toLowerCase();
  if (!key || typeof handler !== 'function') return () => {};
  appDestinations.set(key, handler);
  return () => { if (appDestinations.get(key) === handler) appDestinations.delete(key); };
}

export function listAppDestinations() {
  return [...appDestinations.keys()];
}

/**
 * Resolve and open a clank:// destination. Unknown targets produce a useful
 * error; they never execute an arbitrary route or chat command.
 */
export function openAppDestination(destination, event = null) {
  const raw = String(destination || '').replace(/^clank:\/\//i, '').replace(/\/+$/, '');
  if (!raw) return { ok:false, error:'Empty app destination.' };
  const [screen, ...rest] = raw.split('/');
  const key = String(screen || '').trim().toLowerCase();
  const handler = appDestinations.get(key);
  if (!handler) {
    return { ok:false, error:`Unknown app destination: ${raw}`, destination:raw };
  }
  const panel = rest.filter(Boolean).join('/') || null;
  try {
    handler({ screen:key, panel, destination:raw, event });
    return { ok:true, destination:raw };
  } catch (error) {
    return { ok:false, error:error?.message || `App destination failed: ${raw}`, destination:raw };
  }
}

// ---------------------------------------------------------------------------
// Full Markdown renderer factory
// ---------------------------------------------------------------------------

/**
 * @param {object} options
 * @param {function} options.h - hyperscript element factory
 * @param {function} options.documents - returns the visible document list
 * @param {function} [options.findByName] - resolve a short name to a document
 * @param {function} [options.assetUrl] - map an asset target to a URL
 * @param {function} options.openTarget - (target, fragment, event) navigation
 * @param {function} [options.openAppDestination] - (destination, event) override
 * @param {function} [options.onConvertPluginBlock] - inert-block conversion hook
 * @returns {{ renderMarkdown: function, renderPreview: function, renderReference: function, appendMarkdownInline: function }}
 */
export function createMarkdownRenderer({
  h,
  documents,
  findByName = null,
  assetUrl = () => null,
  openTarget,
  openAppDestination: appOpen = openAppDestination,
  onConvertPluginBlock = null,
}) {
  const resolveName = (name) => (typeof findByName === 'function' ? findByName(name) : (documents() || []).find((doc) => doc.name === name) || null);

  const renderReference = createReferenceRenderer({
    h,
    documents:() => documents() || [],
    assetUrl,
    openTarget,
    openAppDestination:(destination, event) => {
      const result = appOpen(destination, event);
      if (result && result.ok === false && result.error && typeof openTarget === 'function') {
        // Surface a useful error without executing anything else.
        openTarget({ kind:'app-destination-error', name:result.error, id:null }, null, event);
      }
      return result;
    },
    renderDocument:(text, seen, origin) => renderMarkdown(text, seen, origin),
  });

  function appendMarkdownInline(parent, value, context = {}) {
    let rest = String(value || '');
    const token = /(!?\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|([^\]]+))?\]\]|`([^`]+)`|\*\*([^*]+)\*\*|~~([^~]+)~~|==([^=]+)==|(?<!\*)\*([^*]+)\*(?!\*)|\[([^\]]+)\]\(([^)\s]+)\)|\$([^$\n]+)\$|<%[^%]*%>|(?<![\p{L}\p{N}_])#([A-Za-z0-9_/-]+))/u;
    const appendText = (text) => parent.append(document.createTextNode(String(text || '').replace(/\\(?=[\\`*_[\]{}()#+.!|~-])/g, '')));
    const openFromEvent = (target, event) => {
      if (!target) return;
      openTarget(target, null, event);
    };
    while (rest) {
      const match = rest.match(token);
      const reference = findReferenceToken(rest);
      if (reference && (!match || reference.index <= match.index)) {
        appendText(rest.slice(0, reference.index));
        parent.append(renderReference(reference, context));
        rest = rest.slice(reference.index + reference.length); continue;
      }
      if (!match) { appendText(rest); return; }
      appendText(rest.slice(0, match.index));
      if (match[2]) {
        const target = resolveName(match[2]);
        parent.append(h('button', { class:'copal-chip', type:'button', text:match[3] || match[2], onclick:(event) => openFromEvent(target, event) }));
      } else if (match[4]) parent.append(h('code', { text:match[4] }));
      else if (match[5]) parent.append(h('strong', { text:match[5] }));
      else if (match[6]) parent.append(h('del', { text:match[6] }));
      else if (match[7]) parent.append(h('mark', { text:match[7] }));
      else if (match[8]) parent.append(h('em', { text:match[8] }));
      else if (match[9]) {
        const isExternal = /^https?:\/\//i.test(match[10]);
        const isApp = /^clank:\/\//i.test(match[10]);
        if (isExternal) parent.append(h('a', { href:match[10], target:'_blank', rel:'noopener noreferrer', text:match[9] }));
        else if (isApp) parent.append(h('button', { class:'copal-chip copal-app-link', type:'button', 'data-app-destination':match[10].replace(/^clank:\/\//i, ''), text:match[9], onclick:(event) => { const result = appOpen(match[10], event); if (result && result.ok === false) openTarget({ kind:'app-destination-error', name:result.error, id:null }, null, event); } }));
        else {
          const target = resolveName(match[10]);
          parent.append(h('button', { class:`copal-chip${target ? '' : ' unresolved'}`, type:'button', disabled:!target, text:match[9], onclick:(event) => openFromEvent(target, event) }));
        }
      } else if (match[11]) parent.append(h('span', { class:'copal-inline-math', text:match[11] }));
      else if (match[12]) parent.append(h('code', { class:'copal-templater-block', text:match[12] }));
      else if (match[13]) parent.append(h('span', { class:'copal-markdown-tag', text:`#${match[13]}` }));
      rest = rest.slice(match.index + match[0].length);
    }
  }

  function renderMarkdown(text, seen = new Set(), origin = null) {
    const root = h('div');
    const inline = (parent, value) => appendMarkdownInline(parent, value, { origin, seen });
    const lines = String(text || '').split('\n');
    let lineOffset = 0;
    if (lines[0]?.trim() === '---') {
      const end = lines.findIndex((line, index) => index > 0 && line.trim() === '---');
      if (end > 0) { lines.splice(0, end + 1); lineOffset = end + 1; }
    }
    let codeBlock = null;
    for (let index = 0; index < lines.length; index += 1) {
      const raw = lines[index].replace(/%%[^%\n]*(?:%(?!%)[^%\n]*)*%%/g, '');
      const fence = /^\s*```\s*([^\s`]*)/.exec(raw);
      if (fence) {
        if (codeBlock) { root.append(codeBlock.wrapper); codeBlock = null; }
        else {
          const lang = fence[1] || '';
          const isPluginBlock = /^(dataview|tasks|dataviewjs|tasksjs)$/i.test(lang);
          const code = h('code', { 'data-language':lang });
          const pre = h('pre', { class:'copal-markdown-code' }, code);
          const copy = h('button', { type:'button', class:'copal-btn copal-code-copy', text:'Copy', onclick:async() => navigator.clipboard.writeText(code.textContent || '') });
          const header = fence[1] ? h('figcaption', { text:fence[1] }) : null;
          if (isPluginBlock) {
            const banner = h('div', { class:'copal-plugin-block-banner' },
              h('span', { class:'copal-plugin-block-badge', text:`${lang} block` }),
              h('span', { text:'Inert — plugin queries are not executed in the editor.' }));
            const convertBtn = onConvertPluginBlock
              ? h('button', { type:'button', class:'copal-btn copal-plugin-convert', text:'Convert to Base', onclick:() => onConvertPluginBlock(code.textContent || '', lang) })
              : null;
            codeBlock = { code, wrapper:h('figure', { class:'copal-code-block copal-plugin-block' }, header, banner, copy, convertBtn, pre) };
          } else {
            codeBlock = { code, wrapper:h('figure', { class:'copal-code-block' }, header, copy, pre) };
          }
        }
        continue;
      }
      if (codeBlock) { codeBlock.code.append(document.createTextNode(`${raw}\n`)); continue; }
      if (!raw.trim()) continue;
      if (/^\s*%%/.test(raw) || /^\s*<!--/.test(raw)) continue;
      if (raw.trim().startsWith('$$')) {
        const math = [raw];
        while (!(math.length > 1 || raw.trim() !== '$$') || !math.at(-1).trim().endsWith('$$')) {
          if (index + 1 >= lines.length) break;
          math.push(lines[++index]);
        }
        root.append(h('pre', { class:'copal-math-block', text:math.join('\n').replace(/^\s*\$\$|\$\$\s*$/g, '').trim() }));
        continue;
      }
      const callout = raw.match(/^>\s*\[!([A-Za-z0-9_-]+)\][+-]?\s*(.*)$/);
      if (callout) {
        const body = [];
        while (lines[index + 1]?.match(/^>\s?/)) body.push(lines[++index].replace(/^>\s?/, ''));
        const box = h('aside', { class:`copal-callout copal-callout-${callout[1].toLowerCase()}` }, h('strong', { text:callout[2] || callout[1] }));
        for (const line of body) { const paragraph = h('p'); inline(paragraph, line); box.append(paragraph); }
        root.append(box);
        continue;
      }
      if (raw.includes('|') && /^\s*\|?\s*:?-{3,}/.test(lines[index + 1] || '')) {
        const cells = (line) => line.trim().replace(/^\||\|$/g, '').split('|').map((cell) => cell.trim());
        const table = h('table', { class:'copal-markdown-table' });
        const header = h('tr'); for (const value of cells(raw)) { const cell = h('th'); inline(cell, value); header.append(cell); }
        table.append(h('thead', {}, header)); index += 1;
        const body = h('tbody');
        while (lines[index + 1]?.includes('|')) {
          const row = h('tr'); for (const value of cells(lines[++index])) { const cell = h('td'); inline(cell, value); row.append(cell); }
          body.append(row);
        }
        table.append(body); root.append(table); continue;
      }
      const heading = raw.match(/^(#{1,6})\s+(.*)$/);
      if (heading) { const node = h(`h${heading[1].length}`, { 'data-line':String(index + 1 + lineOffset) }); inline(node, heading[2]); root.append(node); continue; }
      const task = raw.match(/^(\s*)[-*+] \[([ xX])\]\s+(.*)$/);
      if (task) { const checkbox = h('input', { type:'checkbox', disabled:true, 'aria-label':task[3] }); checkbox.checked = !!task[2].trim(); const node = h('p', { class:'copal-markdown-task', style:`--indent:${task[1].length}` }, checkbox); inline(node, task[3]); root.append(node); continue; }
      const bullet = raw.match(/^(\s*)[-*+]\s+(.*)$/);
      if (bullet) { const node = h('p', { class:'copal-markdown-bullet', style:`--indent:${bullet[1].length}` }, '• '); inline(node, bullet[2]); root.append(node); continue; }
      const ordered = raw.match(/^(\s*)(\d+[.)])\s+(.*)$/);
      if (ordered) { const node = h('p', { class:'copal-markdown-bullet ordered', style:`--indent:${ordered[1].length}` }, `${ordered[2]} `); inline(node, ordered[3]); root.append(node); continue; }
      const quote = raw.match(/^>\s?(.*)$/);
      if (quote) { const node = h('blockquote'); inline(node, quote[1]); root.append(node); continue; }
      if (/^\s*([-*_])(?:\s*\1){2,}\s*$/.test(raw)) { root.append(h('hr')); continue; }
      const footnote = raw.match(/^\s*\[\^([^\]]+)\]:\s*(.*)$/);
      if (footnote) { const node = h('aside', { class:'copal-footnote' }, h('sup', { text:footnote[1] })); inline(node, footnote[2]); root.append(node); continue; }
      const para = h('p');
      inline(para, raw);
      root.append(para);
    }
    if (codeBlock) root.append(codeBlock.wrapper);
    return root;
  }

  function renderPreview(source) {
    const reference = findReferenceToken(String(source || ''));
    return reference ? renderReference(reference, { origin:null, seen:new Set() }) : null;
  }

  return { renderMarkdown, renderPreview, renderReference, appendMarkdownInline, extractReferenceSection };
}

export { extractReferenceSection, findReferenceToken };
