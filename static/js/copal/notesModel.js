function scalar(raw) {
  const value = String(raw ?? '').trim();
  if (value === 'true') return true;
  if (value === 'false') return false;
  if (value === 'null' || value === '~') return null;
  if (/^-?\d+(?:\.\d+)?$/.test(value)) return Number(value);
  if ((value.startsWith('[') && value.endsWith(']')) || (value.startsWith('{') && value.endsWith('}'))) {
    try { return JSON.parse(value.replace(/'/g, '"')); } catch (_) {}
  }
  return value.replace(/^(["'])(.*)\1$/, '$2');
}

export function propertyType(value, key = '') {
  if (typeof value === 'boolean') return 'checkbox';
  if (typeof value === 'number') return 'number';
  if (Array.isArray(value)) return /tags?/i.test(key) ? 'tags' : 'list';
  if (value && typeof value === 'object') return 'source';
  if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(String(value))) return 'datetime';
  if (/^\d{4}-\d{2}-\d{2}$/.test(String(value))) return 'date';
  return 'text';
}

export function coercePropertyValue(value, type = 'text') {
  if (type === 'checkbox') return value === true || value === 'true';
  if (type === 'number') return Number.isFinite(Number(value)) ? Number(value) : 0;
  if (type === 'list' || type === 'tags') {
    return (Array.isArray(value) ? value : String(value || '').split(','))
      .map((item) => String(item).trim().replace(/^#/, ''))
      .filter(Boolean);
  }
  if (type === 'object') {
    const parsed = typeof value === 'string' ? JSON.parse(value) : value;
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('Object properties require a JSON object.');
    return parsed;
  }
  return value == null ? '' : String(value);
}

export function parseFrontmatter(text) {
  const source = String(text || '');
  const lines = source.split('\n');
  if (lines[0]?.trim() !== '---') return { valid:true, present:false, start:0, end:0, entries:[] };
  const endLine = lines.findIndex((line, index) => index > 0 && line.trim() === '---');
  if (endLine < 0) return { valid:false, present:true, start:0, end:source.length, entries:[], error:'Unterminated frontmatter block' };
  const entries = [];
  let offset = lines[0].length + 1;
  for (let index = 1; index < endLine; index += 1) {
    const raw = lines[index];
    const match = /^(\s*)([A-Za-z0-9_.-]+):(.*)$/.exec(raw);
    if (match) {
      const valueRaw = match[3].trim();
      const value = scalar(valueRaw);
      const valueStart = offset + raw.indexOf(':') + 1 + (match[3].length - match[3].trimStart().length);
      entries.push({
        key:match[2], value, raw:valueRaw, type:propertyType(value, match[2]), line:index + 1,
        lineFrom:offset, lineTo:offset + raw.length,
        keyFrom:offset + match[1].length, keyTo:offset + match[1].length + match[2].length,
        valueFrom:valueStart, valueTo:valueStart + valueRaw.length,
      });
    }
    offset += raw.length + 1;
  }
  const end = lines.slice(0, endLine + 1).join('\n').length;
  return { valid:true, present:true, start:0, end, endLine:endLine + 1, entries };
}

export function formatPropertyValue(value, type = propertyType(value)) {
  if (type === 'checkbox') return value === true || value === 'true' ? 'true' : 'false';
  if (type === 'number') return Number.isFinite(Number(value)) ? String(Number(value)) : '0';
  if (type === 'list' || type === 'tags') {
    const values = Array.isArray(value) ? value : String(value || '').split(',').map((item) => item.trim()).filter(Boolean);
    return JSON.stringify(values.map(String));
  }
  if (type === 'date' || type === 'datetime') return String(value || '');
  if (value == null) return '';
  const string = String(value);
  return /[:#\[\]{},]|^\s|\s$/.test(string) ? JSON.stringify(string) : string;
}

export function setFrontmatterProperty(text, key, value, type) {
  const source = String(text || '');
  if (!/^[A-Za-z0-9_.-]+$/.test(String(key || ''))) throw new Error('Property names may contain letters, numbers, dots, dashes, and underscores.');
  const parsed = parseFrontmatter(source);
  if (!parsed.valid) throw new Error(parsed.error);
  const rendered = formatPropertyValue(value, type);
  const entry = parsed.entries.find((item) => item.key === key);
  if (entry) return `${source.slice(0, entry.valueFrom)}${rendered}${source.slice(entry.valueTo)}`;
  if (!parsed.present) return `---\n${key}: ${rendered}\n---\n${source ? `\n${source}` : ''}`;
  const closing = source.lastIndexOf('---', parsed.end);
  return `${source.slice(0, closing)}${key}: ${rendered}\n${source.slice(closing)}`;
}

export function removeFrontmatterProperty(text, key) {
  const source = String(text || '');
  const parsed = parseFrontmatter(source);
  if (!parsed.valid) throw new Error(parsed.error);
  const entry = parsed.entries.find((item) => item.key === key);
  if (!entry) return source;
  const newline = source[entry.lineTo] === '\n' ? 1 : 0;
  return `${source.slice(0, entry.lineFrom)}${source.slice(entry.lineTo + newline)}`;
}

export function renameFrontmatterProperty(text, key, nextKey) {
  const source = String(text || '');
  if (!/^[A-Za-z0-9_.-]+$/.test(String(nextKey || ''))) throw new Error('Property names may contain letters, numbers, dots, dashes, and underscores.');
  const parsed = parseFrontmatter(source);
  if (!parsed.valid) throw new Error(parsed.error);
  const entry = parsed.entries.find((item) => item.key === key);
  if (!entry || entry.key === nextKey) return source;
  if (parsed.entries.some((item) => item.key === nextKey)) throw new Error(`Property “${nextKey}” already exists.`);
  return `${source.slice(0, entry.keyFrom)}${nextKey}${source.slice(entry.keyTo)}`;
}

export function moveFrontmatterProperty(text, key, direction) {
  const source = String(text || '');
  const parsed = parseFrontmatter(source);
  if (!parsed.valid) throw new Error(parsed.error);
  const index = parsed.entries.findIndex((entry) => entry.key === key);
  const targetIndex = index + Number(direction);
  if (index < 0 || targetIndex < 0 || targetIndex >= parsed.entries.length) return source;
  const current = parsed.entries[index];
  const target = parsed.entries[targetIndex];
  const currentLine = source.slice(current.lineFrom, current.lineTo);
  const targetLine = source.slice(target.lineFrom, target.lineTo);
  if (target.lineFrom < current.lineFrom) {
    return `${source.slice(0, target.lineFrom)}${currentLine}${source.slice(target.lineTo, current.lineFrom)}${targetLine}${source.slice(current.lineTo)}`;
  }
  return `${source.slice(0, current.lineFrom)}${targetLine}${source.slice(current.lineTo, target.lineFrom)}${currentLine}${source.slice(target.lineTo)}`;
}

export function outlineEntries(text) {
  const source = String(text || '');
  const lines = source.split('\n');
  const entries = [];
  let offset = 0;
  for (let index = 0; index < lines.length; index += 1) {
    const match = /^(#{1,6})\s+(.+?)\s*#*\s*$/.exec(lines[index]);
    if (match) entries.push({ line:index + 1, level:match[1].length, text:match[2], from:offset, to:offset + lines[index].length });
    offset += lines[index].length + 1;
  }
  return entries;
}

function sectionFor(entries, index, lineCount) {
  const entry = entries[index];
  let endLine = lineCount + 1;
  for (let next = index + 1; next < entries.length; next += 1) {
    if (entries[next].level <= entry.level) { endLine = entries[next].line; break; }
  }
  return { ...entry, endLine };
}

export function moveHeadingSection(text, line, direction) {
  const source = String(text || '');
  const lines = source.split('\n');
  const entries = outlineEntries(source);
  const index = entries.findIndex((entry) => entry.line === Number(line));
  if (index < 0 || ![-1, 1].includes(Number(direction))) return source;
  const current = sectionFor(entries, index, lines.length);
  if (direction < 0) {
    let target = index - 1;
    while (target >= 0 && entries[target].level > current.level) target -= 1;
    if (target < 0) return source;
    const targetSection = sectionFor(entries, target, lines.length);
    const block = lines.slice(current.line - 1, current.endLine - 1);
    const remainder = [...lines.slice(0, current.line - 1), ...lines.slice(current.endLine - 1)];
    remainder.splice(targetSection.line - 1, 0, ...block);
    return remainder.join('\n');
  }
  let target = index + 1;
  while (target < entries.length && entries[target].level > current.level) target += 1;
  if (target >= entries.length) return source;
  const targetSection = sectionFor(entries, target, lines.length);
  const block = lines.slice(current.line - 1, current.endLine - 1);
  const remainder = [...lines.slice(0, current.line - 1), ...lines.slice(current.endLine - 1)];
  const removedBeforeTarget = current.endLine - current.line;
  const insertion = targetSection.endLine - 1 - removedBeforeTarget;
  remainder.splice(insertion, 0, ...block);
  return remainder.join('\n');
}

export function moveHeadingSectionTo(text, fromLine, targetLine) {
  const source = String(text || '');
  const lines = source.split('\n');
  const entries = outlineEntries(source);
  const fromIndex = entries.findIndex((entry) => entry.line === Number(fromLine));
  const targetIndex = entries.findIndex((entry) => entry.line === Number(targetLine));
  if (fromIndex < 0 || targetIndex < 0 || fromIndex === targetIndex) return source;
  const current = sectionFor(entries, fromIndex, lines.length);
  const target = sectionFor(entries, targetIndex, lines.length);
  if (target.line >= current.line && target.line < current.endLine) return source;
  const block = lines.slice(current.line - 1, current.endLine - 1);
  const remainder = [...lines.slice(0, current.line - 1), ...lines.slice(current.endLine - 1)];
  const removed = block.length;
  const insertion = target.line < current.line ? target.line - 1 : target.endLine - 1 - removed;
  remainder.splice(Math.max(0, insertion), 0, ...block);
  return remainder.join('\n');
}

export function outlineTree(entries) {
  const root = { line: 0, level: 0, text: '', kind: 'heading', children: [] };
  const stack = [root];
  for (const entry of entries) {
    const node = { ...entry, children: [] };
    while (stack.length > 1 && stack[stack.length - 1].level >= entry.level) {
      stack.pop();
    }
    stack[stack.length - 1].children.push(node);
    stack.push(node);
  }
  return root.children;
}

export function flattenTree(nodes, result = []) {
  for (const node of nodes) {
    result.push(node);
    if (node.children?.length) flattenTree(node.children, result);
  }
  return result;
}

export function reparentHeading(text, line, newLevel) {
  const source = String(text || '');
  const lines = source.split('\n');
  const entries = outlineEntries(source);
  const index = entries.findIndex((entry) => entry.line === Number(line));
  if (index < 0) return source;
  const level = Math.max(1, Math.min(6, Number(newLevel)));
  const lineIdx = entries[index].line - 1;
  lines[lineIdx] = `${'#'.repeat(level)} ${entries[index].text}`;
  return lines.join('\n');
}

export function parseCanvasDocument(text) {
  try {
    const value = JSON.parse(String(text || '{}'));
    return {
      valid:true,
      nodes:(Array.isArray(value.nodes) ? value.nodes : []).map((node, index) => ({
        id:String(node.id || `node-${index}`), type:String(node.type || 'text'),
        label:String(node.text || node.file || node.id || `Node ${index + 1}`), file:node.file || null,
        x:Number(node.x) || 0, y:Number(node.y) || 0, width:Math.max(160, Number(node.width) || 240), height:Math.max(80, Number(node.height) || 120),
      })),
      edges:(Array.isArray(value.edges) ? value.edges : []).map((edge, index) => ({
        id:String(edge.id || `edge-${index}`), from:String(edge.fromNode || ''), to:String(edge.toNode || ''), label:String(edge.label || ''),
      })),
    };
  } catch (error) {
    return { valid:false, nodes:[], edges:[], error:error instanceof Error ? error.message : 'Invalid Canvas JSON' };
  }
}

export function unlinkedMentions(docs, target) {
  const aliases = Array.isArray(target?.frontmatter?.aliases) ? target.frontmatter.aliases : target?.frontmatter?.aliases ? [target.frontmatter.aliases] : [];
  const names = [target?.name, String(target?.name || '').split('/').pop(), ...aliases]
    .map((name) => String(name || '').replace(/\.[^.]+$/, '').trim())
    .filter((name, index, all) => name && all.indexOf(name) === index);
  if (!names.length) return [];
  return docs.flatMap((doc) => {
    if (!doc || doc.id === target.id) return [];
    if (['canvas', 'base', 'asset'].includes(String(doc.kind || '').toLowerCase())) return [];
    const source = String(doc.text || '');
    const withoutLinks = source.replace(/!?\[\[[^\]]+\]\]/g, ' ');
    const lower = withoutLinks.toLowerCase();
    const name = names.find((candidate) => new RegExp(`(^|[^\\p{L}\\p{N}_])${escapeRegExp(candidate)}(?=$|[^\\p{L}\\p{N}_])`, 'iu').test(lower));
    if (!name) return [];
    const index = lower.search(new RegExp(escapeRegExp(name), 'iu'));
    const start = Math.max(0, index - 60);
    const end = Math.min(withoutLinks.length, index + name.length + 90);
    return [{ doc, snippet:withoutLinks.slice(start, end).replace(/\s+/g, ' ').trim() }];
  });
}

function escapeRegExp(value) {
  return String(value || '').replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

export function normalizedDocumentNames(doc) {
  const aliases = Array.isArray(doc?.frontmatter?.aliases) ? doc.frontmatter.aliases : doc?.frontmatter?.aliases ? [doc.frontmatter.aliases] : [];
  return [...new Set([doc?.name, String(doc?.name || '').split('/').pop(), ...aliases]
    .map((value) => String(value || '').replace(/\.[^.\/]+$/, '').trim().toLowerCase())
    .filter(Boolean))];
}

export function normalizeLinkTarget(value) {
  return String(value || '').split('|', 1)[0].split('#', 1)[0].replace(/\.[^.\/]+$/, '').trim().toLowerCase();
}

export function resolveDocumentLink(docs, value) {
  const target = normalizeLinkTarget(value);
  if (!target) return null;
  return docs.find((doc) => normalizedDocumentNames(doc).includes(target)) || null;
}

export function databaseRelations(text, docs = []) {
  const relations = [];
  const seen = new Set();
  for (const match of String(text || '').matchAll(/(!?)\[\[([^\]\n]+)\]\]/g)) {
    const raw = match[2].split('|', 1)[0].trim();
    const [target, ...fragmentParts] = raw.split('#');
    const name = target.trim();
    if (!name) continue;
    const kind = match[1] ? 'embed' : 'link';
    const fragment = fragmentParts.join('#').trim();
    const key = `${kind}\0${name}\0${fragment}`;
    if (seen.has(key)) continue;
    seen.add(key);
    const resolved = resolveDocumentLink(docs, name);
    relations.push({ kind, target:name, targetDocumentId:resolved?.id || null, ...(fragment ? { fragment } : {}) });
  }
  return relations;
}

export function linkedMentions(docs, target) {
  const wanted = new Set(normalizedDocumentNames(target));
  return docs.flatMap((doc) => {
    if (!doc || doc.id === target?.id) return [];
    const relation = (doc.relations || []).find((value) => ['link', 'embed'].includes(value.kind) && value.targetDocumentId === target?.id);
    const link = relation?.target || (doc.links || []).find((value) => wanted.has(normalizeLinkTarget(value)));
    if (!link) return [];
    const lines = String(doc.text || '').split('\n');
    const lineIndex = lines.findIndex((line) => line.toLowerCase().includes(`[[${String(link).toLowerCase()}`));
    return [{ doc, line:lineIndex >= 0 ? lineIndex + 1 : null, snippet:lineIndex >= 0 ? lines[lineIndex].trim() : `Links to ${target.name}` }];
  });
}

export function fuzzyScore(value, query) {
  const text = String(value || '').toLowerCase();
  const wanted = String(query || '').trim().toLowerCase();
  if (!wanted) return 1;
  const direct = text.indexOf(wanted);
  if (direct >= 0) return 10_000 - direct * 10 - text.length;
  let at = 0;
  let score = 0;
  let streak = 0;
  for (const character of wanted) {
    const found = text.indexOf(character, at);
    if (found < 0) return -1;
    streak = found === at ? streak + 1 : 0;
    score += 20 + streak * 8 - found;
    at = found + 1;
  }
  return score;
}

export function wordCount(text) {
  const value = String(text || '').trim();
  return value ? value.split(/\s+/).length : 0;
}
