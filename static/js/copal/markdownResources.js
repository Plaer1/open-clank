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
    const start = index + (embed ? 1 : 0);
    if (value[start] !== '[') continue;
    if (value[start + 1] === '[') {
      const match = /^\[\[((?:\\.|[^\]])+)\]\]/.exec(value.slice(start));
      if (!match) continue;
      const pieces = match[1].split(/(?<!\\)\|/);
      const target = unescape(pieces.shift()).trim();
      const label = unescape(pieces.join('|'));
      const size = embed && /^(\d+)(?:x(\d+))?$/.exec(label);
      return { index, length:match[0].length + (embed ? 1 : 0), embed, syntax:'wiki', target, label:size ? '' : label, width:size ? Number(size[1]) : null, height:size?.[2] ? Number(size[2]) : null };
    }
    const labelMatch = /^\[((?:\\.|[^\]\\])*)\]\(/.exec(value.slice(start));
    if (!labelMatch) continue;
    const contentStart = start + labelMatch[0].length;
    let depth = 1; let end = contentStart; let angle = false;
    for (; end < value.length; end++) {
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

export function resolveReference(reference, { documents, origin = null } = {}) {
  const raw = reference.target.trim();
  if (/^(?:https?:\/\/|data:image\/(?:png|jpeg|gif|webp|avif);base64,)/i.test(raw)) return { status:'external', url:raw, reference };
  if (/^[a-z][a-z\d+.-]*:/i.test(raw) || raw.startsWith('//')) return { status:'unsupported', reference };
  const hash = raw.indexOf('#');
  const path = decode(hash < 0 ? raw : raw.slice(0, hash));
  const fragment = decode(hash < 0 ? '' : raw.slice(hash + 1));
  if (!path) return origin ? { status:'resolved', target:origin, fragment, reference } : { status:'missing', reference };
  const docs = documents || [];
  const exact = wanted => wanted == null ? [] : docs.filter(doc => normalize(doc.name) === normalize(wanted));
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
    const basename = docs.filter(doc => normalize(String(doc.name).split('/').at(-1)) === normalize(path));
    if (basename.length) return result(basename);
    const aliases = docs.filter(doc => [doc.aliases, doc.properties?.aliases].flatMap(value => Array.isArray(value) ? value : value ? [value] : []).some(alias => normalize(alias) === normalize(path)));
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

export function mediaKind(target, url = '') {
  const mime = String(target?.mimeType || target?.mime_type || target?.mime || '').toLowerCase();
  const path = String(target?.name || url).split(/[?#]/)[0].toLowerCase();
  if (mime.startsWith('image/') || /\.(png|jpe?g|gif|webp|svg|avif|bmp|ico)$/.test(path) || /^data:image\//.test(path)) return 'image';
  if (mime.startsWith('audio/') || /\.(mp3|wav|ogg|m4a|flac|aac)$/.test(path)) return 'audio';
  if (mime.startsWith('video/') || /\.(mp4|webm|mov|m4v)$/.test(path)) return 'video';
  if (mime === 'application/pdf' || /\.pdf$/.test(path)) return 'pdf';
  return 'asset';
}

export function createReferenceRenderer({ h, documents, assetUrl, openTarget, renderDocument, maxDepth = 8 }) {
  const diagnostic = (status, reference) => h('span', { class:'copal-reference-error', 'data-reference-status':status, text:`${status === 'ambiguous' ? 'Ambiguous' : status === 'unsupported' ? 'Unsupported' : 'Missing'}: ${reference.target}` });
  return function renderReference(reference, { origin = null, seen = new Set() } = {}) {
    const resolved = resolveReference(reference, { documents:documents(), origin });
    if (!['resolved', 'external'].includes(resolved.status)) return diagnostic(resolved.status, reference);
    const { target, fragment } = resolved;
    if (!reference.embed) return resolved.url
      ? h('a', { href:resolved.url, target:'_blank', rel:'noopener noreferrer', text:reference.label || reference.target })
      : h('button', { class:'copal-chip', type:'button', text:reference.label || reference.target, onclick:() => openTarget(target, fragment) });
    if (resolved.url || target?.kind === 'asset') {
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
      const loaded = () => { frame.dataset.referenceStatus = 'loaded'; };
      node.addEventListener(kind === 'audio' || kind === 'video' ? 'loadedmetadata' : 'load', loaded);
      node.addEventListener('error', () => { frame.dataset.referenceStatus = 'error'; frame.replaceChildren(h('span', { text:`Could not load: ${label}` }), h('button', { type:'button', text:'Retry', onclick:() => frame.replaceWith(renderReference(reference, { origin, seen })) })); });
      frame.append(node); return frame;
    }
    if (seen.has(target.id)) return h('span', { class:'copal-reference-error', 'data-reference-status':'cycle', text:`Embed cycle: ${target.name}` });
    if (seen.size >= maxDepth) return h('span', { class:'copal-reference-error', 'data-reference-status':'depth', text:`Embed depth limit: ${target.name}` });
    const section = extractReferenceSection(target.text, fragment);
    if (section.status !== 'resolved') return diagnostic(section.status, reference);
    return h('span', { class:'copal-transclusion', 'data-reference-status':'resolved' }, h('button', { class:'copal-chip', type:'button', text:reference.label || target.name, onclick:() => openTarget(target, fragment) }), renderDocument(section.text, new Set([...seen, target.id]), target));
  };
}
