const MAX_DEPENDENCIES = 32;
const MAX_DIRECTORY_LOOKUPS = 64;
const MAX_ENTRIES_PER_LOOKUP = 400;
const MAX_DEPTH = 12;
const MAX_RESOURCE_BYTES = 8 * 1024 * 1024;
const MAX_TOTAL_BYTES = 16 * 1024 * 1024;
const MAX_SCANNED_TEXT_BYTES = 2 * 1024 * 1024;
const ALLOWED_MIME = new Set([
  'text/css', 'text/javascript', 'application/javascript',
  'image/png', 'image/jpeg', 'image/gif', 'image/webp', 'image/svg+xml',
]);

function abortError(message = 'Files dependency acquisition was cancelled.') {
  return typeof DOMException === 'function'
    ? new DOMException(message, 'AbortError') : Object.assign(new Error(message), { name:'AbortError' });
}

function canonicalName(value) {
  return String(value || '').normalize('NFC');
}

export function isDirectPreviewChild({ folderRef, entryParentRef = null, treeEntry = false, directory = false } = {}) {
  const folder = String(folderRef || '');
  if (!folder || directory) return false;
  return treeEntry ? String(entryParentRef || '') === folder : true;
}

export function createDependencyAcquisitionGuard({ isCurrent = () => true, timeoutMs = 12_000, pollIntervalMs = 50 } = {}) {
  const controller = new AbortController();
  const timeout = Math.max(1, Math.min(30_000, Number(timeoutMs) || 12_000));
  let timedOut = false;
  const timeoutId = setTimeout(() => { timedOut = true; controller.abort(); }, timeout);
  const intervalId = setInterval(() => {
    try { if (!isCurrent()) controller.abort(); }
    catch { controller.abort(); }
  }, Math.max(20, Math.min(500, Number(pollIntervalMs) || 50)));
  return Object.freeze({
    signal:controller.signal,
    abort:() => controller.abort(),
    get timedOut() { return timedOut; },
    dispose:() => { clearTimeout(timeoutId); clearInterval(intervalId); },
  });
}

function isExternal(value) {
  return /^(?:[A-Za-z][A-Za-z0-9+.-]*:|\/\/)/.test(value);
}

function normalizeLocalReference(value, basePath) {
  const source = String(value || '').trim();
  if (!source || source.startsWith('#') || /^(?:data|blob):/i.test(source)) return { ignored:true };
  if (isExternal(source)) return { external:true };
  if (/[\u0000-\u001f\u007f]/.test(source) || source.includes('\\')) return { invalid:'invalid_character' };
  const rawPath = source.split(/[?#]/, 1)[0];
  if (!rawPath) return { ignored:true };
  const absolute = rawPath.startsWith('/');
  const stack = absolute ? [] : String(basePath || '/index.html').split('/').slice(1, -1);
  const parts = rawPath.split('/');
  if (absolute) parts.shift();
  for (const rawPart of parts) {
    if (!rawPart) return { invalid:'ambiguous_separator' };
    let part;
    try { part = decodeURIComponent(rawPart); } catch { return { invalid:'invalid_encoding' }; }
    if (part === '.' || part === '') continue;
    if (part === '..') {
      if (!stack.length) return { invalid:'outside_root' };
      stack.pop();
      continue;
    }
    // The origin server decodes the request path once. Reject encodings that
    // would become separators on that second boundary, and literal percent
    // names which the current manifest schema deliberately excludes.
    if (/[\\/?#\u0000-\u001f\u007f]/.test(part) || part.includes('%')) return { invalid:'ambiguous_encoded_path' };
    // Keep the decoded URL spelling in the origin manifest. Files name
    // matching normalizes only for lookup; the origin server does not NFC
    // normalize decoded request paths.
    stack.push(part);
  }
  if (!stack.length) return { invalid:'empty_path' };
  const path = stack.join('/');
  if (new TextEncoder().encode(path).byteLength > 256) return { invalid:'path_too_long' };
  return { path };
}

function collectHtmlUrls(source) {
  if (typeof DOMParser !== 'function') throw new Error('Safe HTML dependency scanning is unavailable.');
  const documentValue = new DOMParser().parseFromString(String(source || ''), 'text/html');
  const urls = [];
  const add = (value, kind = 'asset') => { if (String(value || '').trim()) urls.push({ value:String(value).trim(), kind, basePath:'/index.html' }); };
  for (const element of documentValue.querySelectorAll('script[src],link[href],img[src],source[src],video[poster],audio[src],image[href]')) {
    const tag = String(element.tagName || '').toLowerCase();
    const attr = tag === 'video' ? 'poster' : (tag === 'link' ? 'href' : (tag === 'image' ? 'href' : 'src'));
    if (tag === 'link' && !/(?:^|\s)(?:stylesheet|modulepreload)(?:\s|$)/i.test(element.getAttribute('rel') || '')) continue;
    add(element.getAttribute(attr), tag === 'script' ? 'script' : 'asset');
    if (element.hasAttribute('srcset')) urls.push({ value:'', kind:'unsupported_srcset', basePath:'/index.html' });
    if (tag === 'script' && String(element.getAttribute('type') || '').toLowerCase() === 'module') {
      urls.push({ value:'', kind:'unsupported_module_imports', basePath:'/index.html' });
    }
  }
  for (const element of documentValue.querySelectorAll('style')) {
    urls.push(...collectCssUrls(element.textContent || '', '/index.html'));
  }
  for (const element of documentValue.querySelectorAll('[style]')) {
    urls.push(...collectCssUrls(element.getAttribute('style') || '', '/index.html'));
  }
  if (documentValue.querySelector('base[href]')) urls.unshift({ value:'', kind:'unsupported_base', basePath:'/index.html' });
  return urls;
}

function collectCssUrls(source, basePath) {
  const css = String(source || '').replace(/\/\*[\s\S]*?\*\//g, '');
  const urls = [];
  const seen = new Set();
  const add = (value, kind = 'asset') => {
    const normalized = String(value || '').trim();
    if (normalized && !seen.has(normalized)) { seen.add(normalized); urls.push({ value:normalized, kind, basePath }); }
  };
  for (const match of css.matchAll(/@import\s+(?:url\(\s*)?(?:(['"])(.*?)\1|([^'"\s);]+))\s*\)?/gi)) add(match[2] ?? match[3], 'stylesheet');
  for (const match of css.matchAll(/url\(\s*(['"]?)(.*?)\1\s*\)/gi)) add(match[2]);
  return urls;
}

function resourceRef(entry) { return String(entry?.resource_ref || entry?.resourceRef || entry?.ref || '').trim(); }
function revisionValue(entry) {
  const revision = entry?.revision || entry?.stat_revision || null;
  if (!revision || typeof revision !== 'object' || typeof revision.kind !== 'string' || typeof revision.value !== 'string') return null;
  return { kind:revision.kind, value:revision.value };
}

export async function acquirePreviewDependencies({
  html,
  rootRef,
  client,
  readText,
  signal = null,
  isCurrent = () => true,
  onProgress = () => {},
} = {}) {
  if (typeof html !== 'string' || !client || typeof client.children !== 'function' || typeof client.stat !== 'function'
    || typeof readText !== 'function') throw new TypeError('Files preview dependency readers are required.');
  if (!String(rootRef || '').trim()) return { dependencies:[], warnings:['Linked local resources were not acquired because the selected Files folder is unavailable.'] };

  const dependencies = new Map();
  const warnings = [];
  const statCache = new Map();
  const childCache = new Map();
  const cssCache = new Map();
  const queued = collectHtmlUrls(html);
  let directoryLookups = 0;
  let entryRows = 0;
  let totalBytes = 0;
  let scannedTextBytes = new TextEncoder().encode(html).byteLength;
  if (scannedTextBytes > MAX_SCANNED_TEXT_BYTES) {
    return { dependencies:[], warnings:['Linked local resources were not scanned because the HTML exceeds the bounded scan size.'] };
  }

  const warn = reason => { if (warnings.length < 16 && !warnings.includes(reason)) warnings.push(reason); };
  const assertCurrent = () => {
    if (signal?.aborted || !isCurrent()) throw abortError('Files access or Browser selection changed during dependency acquisition.');
  };

  async function exactChild(parent, name) {
    assertCurrent();
    const parentRef = resourceRef(parent) || String(parent || '');
    const normalizedName = canonicalName(name);
    const key = `${parentRef}\u0000${normalizedName}`;
    if (childCache.has(key)) return childCache.get(key);
    if (++directoryLookups > MAX_DIRECTORY_LOOKUPS) throw new Error('Local dependency traversal exceeded its directory limit.');
    const variants = [...new Set([String(name), normalizedName, normalizedName.normalize('NFD')])];
    const matches = new Map();
    for (const query of variants) {
      let cursor = null;
      const cursors = new Set();
      let complete = false;
      for (let page = 0; page < 4; page += 1) {
        assertCurrent();
        const response = await client.children(parentRef, { cursor, limit:100, query, signal });
        assertCurrent();
        const entries = Array.isArray(response?.entries) ? response.entries : [];
        entryRows += entries.length;
        if (entryRows > MAX_ENTRIES_PER_LOOKUP * MAX_DIRECTORY_LOOKUPS) throw new Error('Local dependency traversal exceeded its entry limit.');
        for (const entry of entries) {
          if (canonicalName(entry?.name) !== normalizedName) continue;
          if (!entry?.id) throw new Error('Files did not return a stable resource identity.');
          matches.set(entry.id, entry);
        }
        if (matches.size > 1) throw new Error('A local dependency path is ambiguous in Files.');
        const nextCursor = response?.next_cursor || null;
        if (!nextCursor) { complete = true; break; }
        if (cursors.has(nextCursor)) throw new Error('A local dependency directory lookup could not be completed safely.');
        if (page === 3) throw new Error('A local dependency directory lookup exceeded its page limit.');
        cursors.add(nextCursor);
        cursor = nextCursor;
      }
      if (!complete) throw new Error('A local dependency directory lookup could not be completed safely.');
    }
    const result = [...matches.values()][0] || null;
    childCache.set(key, result);
    return result;
  }

  async function resolveFile(path) {
    const parts = path.split('/');
    if (!parts.length || parts.length > MAX_DEPTH) throw new Error('A local dependency path exceeds its depth limit.');
    let parent = { ref:String(rootRef), resource_ref:String(rootRef), kind:'folder', capabilities:['children'] };
    for (let index = 0; index < parts.length; index += 1) {
      const child = await exactChild(parent, parts[index]);
      if (!child) throw new Error('A linked local resource is not present in the selected Files folder.');
      const ref = resourceRef(child);
      if (!ref) throw new Error('Files did not return an authorized resource reference.');
      if (index < parts.length - 1) {
        const capabilities = new Set(child.capabilities || []);
        if (!capabilities.has('children')) throw new Error('A linked local folder is not readable.');
        parent = child;
      } else {
        assertCurrent();
        const statResponse = await client.stat(ref, { signal });
        assertCurrent();
        const stat = statResponse?.resource || statResponse;
        const currentRef = resourceRef(stat);
        const capabilities = new Set(stat?.capabilities || child.capabilities || []);
        const childRevision = revisionValue(child);
        const revision = revisionValue(stat);
        if (childRevision && revision && (childRevision.kind !== revision.kind || childRevision.value !== revision.value)) throw new Error('Files resource revision changed during dependency resolution.');
        const mediaType = String(stat?.mime_type || stat?.media_type || child.mime_type || child.media_type || '').split(';', 1)[0].trim().toLowerCase();
        const size = Number(stat?.size ?? child.size);
        if (!child.id || !stat?.id || child.id !== stat.id || !currentRef || child.policy_generation !== stat.policy_generation) throw new Error('Files resource identity changed during dependency resolution.');
        if (!capabilities.has('preview') || !capabilities.has('stat') || !revision) throw new Error('A linked resource lacks Files preview/stat authority.');
        if (!ALLOWED_MIME.has(mediaType)) throw new Error('A linked local resource has an unsupported media type.');
        if (Number.isFinite(size) && (size < 0 || size > MAX_RESOURCE_BYTES)) throw new Error('A linked resource exceeds the preview size limit.');
        return { resourceRef:currentRef, revision, mediaType, size:Number.isFinite(size) ? size : null };
      }
    }
    throw new Error('A linked local resource could not be resolved.');
  }

  while (queued.length) {
    assertCurrent();
    const row = queued.shift();
    if (row.kind === 'unsupported_srcset') { warn('Some responsive image sources were not acquired.'); continue; }
    if (row.kind === 'unsupported_module_imports') { warn('JavaScript module imports are not acquired; only declared script URLs are included.'); continue; }
    if (row.kind === 'unsupported_base') {
      warn('A base URL is unsupported; linked resources were not acquired.');
      return { dependencies:[], warnings };
    }
    const resolved = normalizeLocalReference(row.value, row.basePath);
    if (resolved.ignored) continue;
    if (resolved.external) { warn('External resources remain blocked in the isolated preview.'); continue; }
    if (resolved.invalid) {
      warn(resolved.invalid === 'outside_root' ? 'A linked resource escapes the selected Files folder and was not included.' : 'A linked resource has an unsupported or ambiguous URL path.');
      continue;
    }
    if (dependencies.has(resolved.path)) continue;
    if (dependencies.size >= MAX_DEPENDENCIES) { warn('The preview dependency count limit was reached.'); break; }
    try {
      const resource = await resolveFile(resolved.path);
      const countedBytes = resource.size || 0;
      if (totalBytes + countedBytes > MAX_TOTAL_BYTES) throw new Error('The preview dependency total size limit was reached.');
      totalBytes += countedBytes;
      dependencies.set(resolved.path, { path:resolved.path, resource_ref:resource.resourceRef, expected_revision:resource.revision });
      onProgress({ count:dependencies.size, path:resolved.path });
      if (resource.mediaType === 'text/css') {
        let css = cssCache.get(resource.resourceRef);
        if (css == null) {
          assertCurrent();
          css = await readText(resource.resourceRef, signal, resource.revision);
          assertCurrent();
          if (typeof css !== 'string') throw new Error('Files returned invalid CSS content.');
          scannedTextBytes += new TextEncoder().encode(css).byteLength;
          if (scannedTextBytes > MAX_SCANNED_TEXT_BYTES) throw new Error('The total CSS scan size limit was reached.');
          cssCache.set(resource.resourceRef, css);
        }
        queued.push(...collectCssUrls(css, `/${resolved.path}`));
      }
    } catch (error) {
      if (error?.name === 'AbortError') throw error;
      warn(error?.message || 'A linked local resource could not be included.');
    }
  }
  assertCurrent();
  return { dependencies:[...dependencies.values()], warnings };
}

export const previewDependencyLimits = Object.freeze({
  maxDependencies:MAX_DEPENDENCIES,
  maxDirectoryLookups:MAX_DIRECTORY_LOOKUPS,
  maxDepth:MAX_DEPTH,
  maxResourceBytes:MAX_RESOURCE_BYTES,
  maxTotalBytes:MAX_TOTAL_BYTES,
});
