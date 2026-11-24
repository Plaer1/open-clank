import { createPreviewFrameMount } from '../browser/previewMount.js';
import { createDependencyAcquisitionGuard } from '../browser/dependencyManifest.js';
import { acquireInlineDependencies } from '../browser/inlineDependencies.js';
import { mountWorkbookEmbed } from '../editor/workbookEmbed.js';
// Shared reference parsing and resolution. A resolved name grants no access:
// callers supply already-authorized documents and provider media URLs.
const unescape = value => String(value || '').replace(/\\([\\`*_[\]{}()#+.!|<>~-])/g, '$1');
const normalize = value => String(value || '').normalize('NFC').toLocaleLowerCase().replace(/\.md$/i, '');
const decode = value => { try { return decodeURIComponent(value); } catch (_) { return value; } };

function cleanPath(path) {
  const parts = [];
  for (const part of path.split('/')) {
    if (!part || part === '.') continue;
    if (part === '..') { if (!parts.length) return null; parts.pop(); }
    else parts.push(part);
  }
  return parts.join('/');
}

export function findReferenceToken(source) {
  const value = String(source || '');
  for (let index = 0; index < value.length; index++) {
    if (value[index] === '\\') { index++; continue; }
    const embed = value[index] === '!';
    // The user's double-bang wiki embed is one token; preserve its source spelling.
    const bangs = embed && value.slice(index,index + 4) === '!![[' ? 2 : embed ? 1 : 0;
    const start = index + bangs;
    if (value[start] !== '[') continue;
    if (value[start + 1] === '[') {
      const match = /^\[\[((?:\\.|[^\]\n])+)\]\]/.exec(value.slice(start));
      if (!match) continue;
      const pieces = match[1].split(/(?<!\\)\|/);
      const target = unescape(pieces.shift()).trim();
      const label = unescape(pieces.join('|'));
      const size = embed && /^(\d+)(?:x(\d+))?$/.exec(label);
      return { index, length:match[0].length + bangs, embed, syntax:'wiki', target, label:size ? '' : label, width:size ? Number(size[1]) : null, height:size?.[2] ? Number(size[2]) : null };
    }
    const labelMatch = /^\[((?:\\.|[^\]\\\n])*)\]\(/.exec(value.slice(start));
    if (!labelMatch) continue;
    const contentStart = start + labelMatch[0].length;
    let depth = 1; let end = contentStart; let angle = false;
    for (; end < value.length; end++) {
      if (value[end] === '\n' || value[end] === '\r') break;
      if (value[end] === '\\') { end++; continue; }
      if (value[end] === '<' && end === contentStart) angle = true;
      if (angle) { if (value[end] === '>') angle = false; continue; }
      if (value[end] === '(') depth++;
      if (value[end] === ')' && --depth === 0) break;
    }
    if (depth !== 0) continue;
    const destination = value.slice(contentStart, end).trim();
    const titled = /^(<[^>]*>|.*?)\s+["'](.*)["']$/.exec(destination);
    const target = unescape((titled?.[1] || destination).replace(/^<|>$/g, ''));
    return { index, length:end + 1 - index, embed, syntax:'markdown', target, label:unescape(labelMatch[1]), title:titled?.[2] || '', width:null, height:null };
  }
  return null;
}

/** Locate rendered references without adopting lookalikes in source-only spans. */
export function referenceExcludedSpans(source) {
  const text = String(source || '');
  const excluded = [];
  let offset = 0, fence = null, frontmatter = /^---\r?\n/.test(text);
  for (const [lineIndex, line] of text.split('\n').entries()) {
    const marker = /^ {0,3}(`{3,}|~{3,})(.*)$/.exec(line);
    if (frontmatter) {
      excluded.push([offset, offset + line.length]);
      if (lineIndex > 0 && line.trim() === '---') frontmatter = false;
    } else if (marker) {
      excluded.push([offset, offset + line.length]);
      if (!fence) fence = marker[1];
      else if (marker[1][0] === fence[0] && marker[1].length >= fence.length && !marker[2].trim()) fence = null;
    } else if (fence) excluded.push([offset, offset + line.length]);
    else {
      for (const match of line.matchAll(/(`+)([\s\S]*?)\1/g)) excluded.push([offset + match.index, offset + match.index + match[0].length]);
    }
    offset += line.length + 1;
  }
  return excluded;
}

export function createReferenceSourceLocator(source) {
  const text = String(source || '');
  const excluded = referenceExcludedSpans(text);
  let cursor = 0;
  return raw => {
    let start = text.indexOf(raw, cursor);
    while (start >= 0 && excluded.some(([from, to]) => start >= from && start < to)) start = text.indexOf(raw, start + raw.length);
    if (start >= 0) cursor = start + raw.length;
    return start;
  };
}

// Reuse the renderer's reference token grammar and the same fenced/code spans.
// `truncated` and `unresolvedDefinitions` describe bounded extraction, not a
// claim that the loaded document corpus is complete.
export function extractDocumentReferences(source, { maxReferences = 256, maxChars = 2 * 1024 * 1024 } = {}) {
  const text = String(source || '');
  const limit = Math.max(1, Math.min(256, Number(maxReferences) || 256));
  const charLimit = Number.isSafeInteger(maxChars) ? Math.max(1, Math.min(maxChars, 2 * 1024 * 1024)) : 2 * 1024 * 1024;
  const bounded = text.slice(0, charLimit), chars = bounded.split('');
  for (const [from, to] of referenceExcludedSpans(bounded)) {
    for (let i = from; i < Math.min(to, chars.length); i++) if (chars[i] !== '\n' && chars[i] !== '\r') chars[i] = ' ';
  }
  let masked = chars.join('');
  const definitions = new Map(); let definitionLimit = false;
  masked = masked.replace(/^[ \t]{0,3}\[(?!\^)([^\]\n]{1,256})\]:[ \t]*(?:<([^>\n]+)>|([^\s]+))(?:[ \t]+["'][^\n]*["'])?[ \t]*$/gm, (whole, label, angle, bare) => {
    const key = label.trim().replace(/\s+/g, ' ').toLocaleLowerCase();
    if (!definitions.has(key)) {
      if (definitions.size < 4096) definitions.set(key, unescape(angle || bare));
      else definitionLimit = true;
    }
    return ' '.repeat(whole.length);
  });
  const references = []; let offset = 0, unresolvedDefinitions = 0, truncated = text.length > bounded.length || definitionLimit;
  const style = /(?<!!)\[([^\]\n]{1,256})\]\[([^\]\n]{0,256})\]|(?<!!)\[([^\]\n]{1,256})\](?![\[\](])/g;
  const indirects = [];
  for (const match of masked.matchAll(style)) {
    if (match.index > 0 && (masked[match.index-1] === '\\' || masked[match.index-1] === '[')) continue;
    if (indirects.length >= 4096) { truncated = true; break; }
    indirects.push(match);
  }
  let indirectIndex = 0;
  while (offset < masked.length) {
    const direct = findReferenceToken(masked.slice(offset));
    while (indirectIndex < indirects.length && indirects[indirectIndex].index < offset) indirectIndex++;
    const indirect = indirects[indirectIndex];
    const indirectFirst = indirect && (!direct || indirect.index < offset + direct.index);
    if (!direct && !indirect) break;
    let token;
    if (indirectFirst) {
      const label = (indirect[2] || indirect[1] || indirect[3]).trim().replace(/\s+/g, ' ').toLocaleLowerCase();
      const target = definitions.get(label);
      if (target) token = { syntax:'markdown', embed:false, target, label:indirect[1] || indirect[3], index:indirect.index, length:indirect[0].length };
      else if (indirect[1]) unresolvedDefinitions++;
      offset = indirect.index + indirect[0].length;
    } else {
      token = { ...direct, index:offset + direct.index };
      offset = token.index + token.length;
    }
    if (token) {
      if (references.length >= limit) { truncated = true; break; }
      references.push(token);
    }
  }
  return { references, truncated, unresolvedDefinitions };
}

function aliasesOf(doc) {
  return [doc.aliases, doc.properties?.aliases, doc.frontmatter?.aliases]
    .flatMap(value => Array.isArray(value) ? value : value ? [value] : []);
}

export function buildReferenceIndex(documents) {
  const names = new Map(), basenames = new Map(), aliases = new Map();
  function add(table, key, doc) {
    const rows = table.get(key);
    if (rows) rows.push(doc);
    else table.set(key, [doc]);
  }
  for (const doc of documents) {
    add(names, normalize(doc.name), doc);
    add(basenames, normalize(String(doc.name).split('/').at(-1)), doc);
    for (const key of new Set(aliasesOf(doc).map(normalize))) add(aliases, key, doc);
  }
  return { names, basenames, aliases };
}

export function resolveReference(reference, { documents, origin = null, index = null } = {}) {
  const raw = reference.target.trim();
  // Installed handbook renditions are same-origin static resources, not
  // owner document names. Keep the authority and path narrow: no encoding,
  // traversal, query or fragment is accepted by this publication namespace.
  if (raw.startsWith('/static/docs/media/')) {
    return /^\/static\/docs\/media\/[a-z0-9-]+\/[a-z0-9][a-z0-9_.-]*\.(?:png|jpe?g|webp|avif|webm|mp4|vtt)$/.test(raw) && !raw.includes('..')
      ? { status:'external', url:raw, reference }
      : { status:'unsupported', reference };
  }
  if (/^(?:https?:\/\/|data:image\/(?:png|jpeg|gif|webp|avif);base64,)/i.test(raw)) return { status:'external', url:raw, reference };
  // App destination links are handled by the shared registry, never by the
  // general chat-command interpreter.
  if (/^clank:\/\//i.test(raw)) {
    const destination = raw.replace(/^clank:\/\//i, '').replace(/\/+$/, '');
    return destination ? { status:'app', destination, reference } : { status:'missing', reference };
  }
  if (/^[a-z][a-z\d+.-]*:/i.test(raw) || raw.startsWith('//')) return { status:'unsupported', reference };
  const hash = raw.indexOf('#');
  const path = decode(hash < 0 ? raw : raw.slice(0, hash));
  const fragment = decode(hash < 0 ? '' : raw.slice(hash + 1));
  if (!path) return origin ? { status:'resolved', target:origin, fragment, reference } : { status:'missing', reference };
  const docs = documents || [];
  const exact = wanted => wanted == null ? [] : index ? (index.names.get(normalize(wanted)) || []) : docs.filter(doc => normalize(doc.name) === normalize(wanted));
  const result = matches => ({ status:matches.length === 1 ? 'resolved' : 'ambiguous', target:matches.length === 1 ? matches[0] : null, candidates:matches, fragment, reference });
  const directory = String(origin?.name || '').split('/').slice(0, -1).join('/');
  const relative = cleanPath(`${directory ? directory + '/' : ''}${path}`);
  const explicit = path.startsWith('./') || path.startsWith('../');
  const root = cleanPath(path);
  for (const candidate of path.startsWith('/') ? [root] : [relative, ...(explicit ? [] : [root])]) {
    const matches = exact(candidate);
    if (matches.length) return result(matches);
  }
  if (!explicit && !path.includes('/')) {
    const basename = index ? (index.basenames.get(normalize(path)) || []) : docs.filter(doc => normalize(String(doc.name).split('/').at(-1)) === normalize(path));
    if (basename.length) return result(basename);
    const aliases = index ? (index.aliases.get(normalize(path)) || []) : docs.filter(doc => aliasesOf(doc).some(alias => normalize(alias) === normalize(path)));
    if (aliases.length) return result(aliases);
  }
  return { status:'missing', fragment, reference };
}

export function extractReferenceSection(text, fragment) {
  const source = String(text || '');
  if (!fragment) return { status:'resolved', text:source, line:1 };
  const lines = source.split(/\r?\n/); const matches = []; let fence = null;
  const wanted = normalize(fragment).replace(/^#/, '');
  for (let i = 0; i < lines.length; i++) {
    const fenced = /^\s*(`{3,}|~{3,})/.exec(lines[i]);
    if (fenced) { if (!fence) fence = fenced[1][0]; else if (fence === fenced[1][0]) fence = null; continue; }
    if (fence) continue;
    if (wanted.startsWith('^')) {
      if (new RegExp(`(?:^|\\s)\\^${wanted.slice(1).replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\s*$`, 'i').test(lines[i])) matches.push({ index:i, block:true });
    } else {
      const heading = /^(#{1,6})\s+(.+?)\s*#*\s*$/.exec(lines[i]);
      if (heading && [normalize(heading[2]), normalize(heading[2]).replace(/\s+/g, '-')].includes(wanted)) matches.push({ index:i, level:heading[1].length });
    }
  }
  if (matches.length !== 1) return { status:matches.length ? 'ambiguous' : 'missing', text:'', line:null };
  const match = matches[0]; let start = match.index; let end = start + 1;
  if (match.block) {
    while (start > 0 && lines[start - 1].trim() && !/^#{1,6}\s/.test(lines[start - 1])) start--;
  } else {
    fence = null;
    for (; end < lines.length; end++) {
      const fenced = /^\s*(`{3,}|~{3,})/.exec(lines[end]);
      if (fenced) { if (!fence) fence = fenced[1][0]; else if (fence === fenced[1][0]) fence = null; continue; }
      if (!fence && /^(#{1,6})\s/.exec(lines[end])?.[1].length <= match.level) break;
    }
  }
  return { status:'resolved', text:lines.slice(start, end).join('\n'), line:start + 1 };
}

import {markTrustedJpegRepresentation} from '../graphics/mediaImageAuthority.js';

export function mediaKind(target, url = '') {
  const mime = String(target?.mimeType || target?.mime_type || target?.mime || '').toLowerCase();
  const path = String(target?.name || url).split(/[?#]/)[0].toLowerCase();
  if (mime.startsWith('image/') || /\.(png|jpe?g|gif|webp|svg|avif|bmp|ico)$/.test(path) || /^data:image\//.test(path)) return 'image';
  if (mime.startsWith('audio/') || /\.(mp3|wav|ogg|m4a|flac|aac)$/.test(path)) return 'audio';
  if (mime.startsWith('video/') || /\.(mp4|webm|mov|m4v)$/.test(path)) return 'video';
  if (mime === 'application/pdf' || /\.pdf$/.test(path)) return 'pdf';
  return 'asset';
}

// Authorized handles stay out of markup; a Notes leaf may opt into the
// shared isolated frame owner after this placeholder is connected.
const inlinePreviewTargets = new WeakMap();
const inlineWorkbookTargets = new WeakMap();

export function createReferenceRenderer({ h, documents, assetUrl, resolveAsset = null, openTarget, openAppDestination = null, renderDocument, maxDepth = 8 }) {
  const diagnostic = (status, reference) => h('span', { class:'copal-reference-error', 'data-reference-status':status, text:`${status === 'ambiguous' ? 'Ambiguous' : status === 'unsupported' ? 'Unsupported' : 'Missing'}: ${reference.target}` });
  return function renderReference(reference, { origin = null, seen = new Set(), sourceRevision = null, authorizedMedia = null, navigation = null } = {}) {
    const linkContext = { origin, reference, sourceRevision, navigation };
    const resolved = authorizedMedia ? {status:authorizedMedia.url ? 'external' : 'resolved', ...authorizedMedia} : resolveReference(reference, { documents:documents(), origin });
    if (resolved.status === 'app') {
      const invoke = (event) => {
        if (typeof openAppDestination === 'function') openAppDestination(resolved.destination, event, navigation);
        else openTarget?.(resolved.destination, null, event, linkContext);
      };
      return h('button', { class:'copal-chip copal-app-link', type:'button', 'data-copal-reading-link':'', 'data-app-destination':resolved.destination, text:reference.label || reference.target, onclick:invoke });
    }
    if (resolved.status === 'missing' && reference.embed && origin && typeof resolveAsset === 'function') {
      const frame = h('span', {class:'copal-media-embed', 'data-reference-status':'loading', text:`Loading: ${reference.target}`});
      Promise.resolve().then(() => resolveAsset(reference, origin)).then(media => {
        const found = Boolean(media?.url || media?.target?.browserPreview || media?.target?.workbookEmbed);
        frame.replaceChildren(found ? renderReference(reference, {origin, seen, sourceRevision, authorizedMedia:media, navigation}) : diagnostic('missing',reference));
        frame.dataset.referenceStatus = found ? 'resolved' : 'missing';
      }).catch(() => {
        frame.dataset.referenceStatus = 'error';
        frame.replaceChildren(h('span', {text:`Could not resolve: ${reference.target}`}), h('button',{type:'button',text:'Retry',onclick:()=>frame.replaceWith(renderReference(reference,{origin,seen,sourceRevision,navigation}))}));
      });
      return frame;
    }
    if (!['resolved', 'external'].includes(resolved.status)) return diagnostic(resolved.status, reference);
    const { target, fragment } = resolved;
    if (!reference.embed) return resolved.url
      ? h('a', { href:resolved.url, target:'_blank', rel:'noopener noreferrer', text:reference.label || reference.target })
      : h('button', { class:'copal-chip', type:'button', 'data-copal-reading-link':'', text:reference.label || reference.target, onclick:(event) => openTarget(target, fragment, event, linkContext) });
    if (target?.workbookEmbed) {
      if (target.fragment && !/^(?:sheet|range|chart)=[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/.test(target.fragment)) return diagnostic('unsupported',reference);
      const placeholder=h('span',{class:'copal-inline-workbook','data-openclank-workbook-embed':'',text:'Loading workbook…'});
      inlineWorkbookTargets.set(placeholder,target);
      return placeholder;
    }
    if (resolved.url || target?.kind === 'asset') {
      // Resolve a vault HTML asset through Files before offering the Browser action.
      if (!authorizedMedia && target?.kind === 'asset' && /\.(?:html?|xhtml)$/i.test(target.name || '') && typeof resolveAsset === 'function') {
        const frame = h('span', {class:'copal-media-embed', 'data-reference-status':'loading', text:`Loading: ${reference.target}`});
        Promise.resolve().then(() => resolveAsset(reference, origin, target)).then(media => {
          const found = Boolean(media?.target?.browserPreview);
          frame.replaceChildren(found ? renderReference(reference, {origin, seen, sourceRevision, authorizedMedia:media, navigation}) : diagnostic('unsupported', reference));
          frame.dataset.referenceStatus = found ? 'resolved' : 'unsupported';
        }).catch(() => {
          frame.dataset.referenceStatus = 'error';
          frame.replaceChildren(h('span', {text:`Could not resolve: ${reference.target}`}));
        });
        return frame;
      }
      if (target?.browserPreview && target.resourceRef && target.revision) {
        const fallback = h('button', {class:'copal-chip', type:'button', text:`Open HTML preview: ${reference.label || target.name}`,
          onclick:() => {
            if (target.isCurrent?.() !== true || !window.browserApplet?.openPreview) return;
            void window.browserApplet.openPreview({resourceRef:target.resourceRef, revision:target.revision,
              dependencies:[], dependencyWarnings:['Linked resources were not acquired for this preview.'],
              title:target.name, accountScope:target.accountScope, workspaceScope:target.workspaceScope, isCurrent:target.isCurrent});
          }});
        const placeholder = h('span', {class:'copal-inline-preview', 'data-openclank-inline-preview':''}, fallback);
        if (target.resourceID && target.accountScope && target.workspaceScope) inlinePreviewTargets.set(placeholder, Object.freeze({
          resourceRef:target.resourceRef, resourceID:target.resourceID, revision:Object.freeze({...target.revision}),
          accountScope:String(target.accountScope), workspaceScope:String(target.workspaceScope), name:String(target.name || 'HTML preview'), isCurrent:target.isCurrent,
        }));
        return placeholder;
      }
      const url = resolved.url || assetUrl(target);
      if (!url) return diagnostic('unsupported', reference);
      const inferred = mediaKind(target, url);
      const kind = resolved.url && inferred === 'asset' ? 'image' : inferred;
      const frame = h('span', { class:'copal-media-embed', 'data-reference-status':'loading' });
      const label = reference.label || target?.name || reference.target;
      const node = kind === 'image' ? h('img', { src:url, alt:label, loading:'lazy' })
        : kind === 'audio' || kind === 'video' ? h(kind, { src:url, controls:true, preload:'metadata', 'aria-label':label })
        : kind === 'pdf' ? h('iframe', { src:url, title:label, loading:'lazy' })
        : h('a', { href:url, download:target?.name || '', text:label });
      node.classList.add('copal-attachment');
      if (reference.title) node.title = reference.title;
      if (reference.width > 0) { node.width = reference.width; node.style.maxWidth = '100%'; }
      if (reference.height > 0) node.height = reference.height;
      const loaded = () => {
        if(kind==='image')markTrustedJpegRepresentation(node,{source:node.src,mediaType:target?.mimeType||target?.mime_type||resolved.media_type,key:String(sourceRevision||origin?.id||'')+':'+String(target?.id||resolved.url||'')});
        frame.dataset.referenceStatus = 'loaded';
      };
      node.addEventListener(kind === 'audio' || kind === 'video' ? 'loadedmetadata' : 'load', loaded);
      node.addEventListener('error', () => { frame.dataset.referenceStatus = 'error'; frame.replaceChildren(h('span', { text:`Could not load: ${label}` }), h('button', { type:'button', text:'Retry', onclick:() => frame.replaceWith(renderReference(reference, { origin, seen, sourceRevision, navigation })) })); });
      frame.append(node); return frame;
    }
    if (seen.has(target.id)) return h('span', { class:'copal-reference-error', 'data-reference-status':'cycle', text:`Embed cycle: ${target.name}` });
    if (seen.size >= maxDepth) return h('span', { class:'copal-reference-error', 'data-reference-status':'depth', text:`Embed depth limit: ${target.name}` });
    const section = extractReferenceSection(target.text, fragment);
    if (section.status !== 'resolved') return diagnostic(section.status, reference);
    return h('span', { class:'copal-transclusion', 'data-reference-status':'resolved' }, h('button', { class:'copal-chip', type:'button', 'data-copal-reading-link':'', text:reference.label || target.name, onclick:(event) => openTarget(target, fragment, event, linkContext) }), renderDocument(section.text, new Set([...seen, target.id]), target, navigation));
  };
}


/** Hydrate only an owned Notes render root. Registration/render never opens a
 * session; an explicit button action is the only way to claim the frame. */
export function mountInlinePreviewHydration(root, {isCurrent = () => true} = {}) {
  if (!root || typeof root.querySelectorAll !== 'function') throw new TypeError('Inline preview root is unavailable');
  const records = new Map(), listeners = new AbortController();
  let disposed = false, scopeTimer = null;
  const stopMonitorIfIdle = () => { if (scopeTimer && ![...records.values()].some(record => record.mount || record.guard || (record.kind === 'workbook' && !record.retired))) { clearInterval(scopeTimer); scopeTimer = null; } };
  const monitorScope = () => {
    if (scopeTimer || disposed) return;
    scopeTimer = setInterval(() => {
      for (const record of records.values()) if ((record.mount || record.guard || record.kind === 'workbook') && !current(record.node,record.target)) retire(record,'The source or account changed. Reopen this document.');
      stopMonitorIfIdle();
    },250);
  };
  const current = (node, target) => {
    try { return !disposed && root.isConnected && node.isConnected && root.contains(node) && isCurrent() && target.isCurrent?.() === true; }
    catch (_) { return false; }
  };
  function retire(record, message = '') {
    if (record.retired) return;
    record.retired = true;
    if (record.kind === 'workbook') {
      record.abort.abort(); record.embed?.dispose(); record.embed=null;
      if (message) record.node.textContent=message;
      stopMonitorIfIdle(); return;
    }
    if (record.guard) { record.guard.abort(); record.guard.dispose(); record.guard = null; }
    if (record.mount) { record.mount.viewportControl.remove(); void record.mount.dispose(); record.mount = null; }
    stopMonitorIfIdle();
    record.open.disabled = true; record.handoff.hidden = true; record.close.hidden = true; record.exportSource.hidden = true; record.widget.hidden = true;
    if (message) record.status.textContent = message;
  }
  function attach(node, target) {
    const slot = document.createElement('span'), controls = document.createElement('span');
    const open = document.createElement('button'), handoff = document.createElement('button'), close = document.createElement('button'), exportSource = document.createElement('button'), widget = document.createElement('button'), status = document.createElement('span');
    open.type = handoff.type = close.type = 'button'; open.textContent = 'Open inline preview'; handoff.textContent = 'Move preview here'; close.textContent = 'Close inline preview';
    handoff.hidden = true; close.hidden = true; exportSource.hidden = true; widget.hidden=true; widget.type='button'; widget.textContent='Widget state…'; exportSource.type = 'button'; exportSource.textContent = 'Export source bundle (partial static)';
    exportSource.title = 'Static declarations only; runtime and dynamically loaded resources may be missing.'; status.setAttribute('role','status');
    slot.className = 'copal-inline-preview-viewport'; slot.style.display = 'block'; slot.style.width = '100%'; slot.style.height = '320px';
    const warning = document.createElement('span'); warning.textContent = 'Linked resources will be checked when opened.';
    controls.append(open,handoff,close,exportSource,widget,status,warning); node.append(controls,slot);
    const record = {node,target,slot,open,handoff,close,exportSource,widget,status,mount:null,guard:null,retired:false}; records.set(node,record);
    const valid = () => !record.retired && current(node,target);
    async function activate(takeover) {
      if (!valid()) { retire(record,'The source or account changed. Reopen this document.'); return; }
      if (record.guard) return;
      const guard = createDependencyAcquisitionGuard({isCurrent:valid}); record.guard = guard; monitorScope();
      let acquired;
      try { acquired = await acquireInlineDependencies({target,signal:guard.signal,isCurrent:valid}); }
      catch (error) {
        if (valid() && guard.timedOut) acquired = {dependencies:[], warnings:['Dependency acquisition timed out; only self-contained content may work.']};
        else { if (valid()) retire(record,'Files source or scope changed. Reopen this document.'); return; }
      } finally { if (record.guard === guard) record.guard = null; guard.dispose(); }
      if (!valid()) return;
      warning.textContent = acquired.warnings.join(' ');
      if (!record.mount) record.mount = createPreviewFrameMount({
        owner:record, getViewport:() => slot, isCurrent:valid, frameClass:'copal-inline-preview-frame',
        onStatus:(message,bad) => { if (!valid()) return; status.textContent = message; handoff.hidden = !String(message).startsWith('Another preview is active.'); },
        onProgressStatus:message => { if (valid()) status.textContent = message; },
        onRetired:() => { if (valid()) { status.textContent = 'Preview moved to another view.'; open.hidden = false; close.hidden = true; handoff.hidden = true; exportSource.hidden = true; widget.hidden = true; } return true; },
      });
      if (!record.mount.viewportControl.isConnected) controls.append(record.mount.viewportControl);
      monitorScope();
      const opened = await record.mount.open({resourceRef:target.resourceRef,revision:target.revision,title:target.name,
        dependencies:acquired.dependencies,dependencyWarnings:acquired.warnings,isCurrent:valid}, {handoff:takeover});
      if (opened && valid()) {
        open.hidden = true; handoff.hidden = true; close.hidden = false; exportSource.disabled = false; exportSource.hidden = false; widget.hidden = !record.mount?.session?.widget_state;
      }
    }
    exportSource.addEventListener('click',async () => {
      const captured = record.mount; if (!captured || !valid()) return; exportSource.disabled = true;
      try { await captured.downloadSourceBundle(); if (valid() && record.mount === captured) status.textContent = 'Source bundle downloaded. Coverage is partial static; runtime dependencies may be missing.'; }
      catch (error) { if (valid() && record.mount === captured && error?.name !== 'AbortError') status.textContent = error?.message || 'Source bundle is unavailable.'; }
      finally { if (valid() && record.mount === captured) exportSource.disabled = false; }
    });
    widget.addEventListener('click', async () => {
      const mount = record.mount;
      const current = () => valid() && record.mount === mount;
      if (!mount || !current()) return;
      try {
        const {openOwnedWidgetState} = await import('../browser/widgetStateOwner.js');
        if (current()) await openOwnedWidgetState({mount, model:target, isCurrent:current});
      } catch (error) {
        if (current()) status.textContent = error.message || 'Widget state unavailable.';
      }
    });
    open.addEventListener('click',() => { void activate(false); });
    handoff.addEventListener('click',() => { void activate(true); });
    close.addEventListener('click',() => { if (!record.mount) return; const mount=record.mount; record.mount=null; mount.viewportControl.remove(); exportSource.hidden=true; widget.hidden=true; stopMonitorIfIdle(); void mount.dispose().then(result => { if (valid()) { status.textContent = result?.cleanupConfirmed ? 'Inline preview closed.' : 'Preview cleanup is unconfirmed.'; open.hidden=false; close.hidden=true; handoff.hidden=true; } }); });
  }
  function attachWorkbook(node,target) {
    const abort=new AbortController(), record={kind:'workbook',node,target,abort,embed:null,retired:false};records.set(node,record);
    const valid=()=>!record.retired && current(node,target);
    monitorScope();
    Promise.resolve().then(()=>target.load(valid,abort.signal)).then(snapshot=>{
      if(!valid())return;
      record.embed=mountWorkbookEmbed(node,{...snapshot,isCurrent:valid,onOpenEditor:identity=>{if(valid())void target.openEditor(identity,valid);}});
    }).catch(()=>{if(valid()){node.textContent='Workbook is unavailable or changed. Open the source to retry.';retire(record);}});
  }
  function sync() {
    if (disposed) return;
    for (const [node,record] of records) if (!root.contains(node)) { retire(record); records.delete(node); }
    for (const node of root.querySelectorAll('[data-openclank-inline-preview]')) {
      const target = inlinePreviewTargets.get(node); if (target && !records.has(node)) attach(node,target);
    }
    for (const node of root.querySelectorAll('[data-openclank-workbook-embed]')) {
      const target = inlineWorkbookTargets.get(node); if (target && !records.has(node)) attachWorkbook(node,target);
    }
  }
  const observer = new MutationObserver(sync); observer.observe(root,{childList:true,subtree:true});
  const invalidate = () => { for (const record of records.values()) retire(record,'Files or account scope changed. Reopen this document.'); };
  document.addEventListener('openclank:auth-context-changed',invalidate,{signal:listeners.signal});
  document.addEventListener('openclank:file-policy-changed',invalidate,{signal:listeners.signal});
  window.addEventListener('workspace-change',invalidate,{signal:listeners.signal});
  window.addEventListener('openclank-files-resource-mutated',invalidate,{signal:listeners.signal});
  sync();
  return Object.freeze({dispose() { if (disposed) return; disposed=true; if (scopeTimer) clearInterval(scopeTimer); scopeTimer=null; observer.disconnect(); listeners.abort(); for (const record of records.values()) retire(record); records.clear(); }});
}
