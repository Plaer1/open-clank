// static/js/langIcons.js
// Bold, distinctive icons for document languages / file types. Each icon
// fills the 24×24 viewBox with a recognisable silhouette — no fragile little
// inset-on-a-page-outline approach. Designed to read clearly at 12–14px.

const ICONS = {
  // Markdown — the official "M↓" logo silhouette, simplified.
  markdown:
    '<rect x="2" y="5" width="20" height="14" rx="2"/>' +
    '<polyline points="6 15 6 9 9 12 12 9 12 15"/>' +
    '<polyline points="16 9 16 15 13 12"/>' +
    '<polyline points="16 15 19 12 16 9"/>',
  // CSV — bold 3-column spreadsheet
  csv:
    '<rect x="3" y="4" width="18" height="16" rx="1.5"/>' +
    '<line x1="3" y1="9" x2="21" y2="9"/>' +
    '<line x1="3" y1="14" x2="21" y2="14"/>' +
    '<line x1="9" y1="4" x2="9" y2="20"/>' +
    '<line x1="15" y1="4" x2="15" y2="20"/>',
  // Python — interlocking double-snake silhouette (simplified)
  python:
    '<path d="M12 2c-3 0-5 1-5 4v3h6v1H4c-1.5 0-3 1-3 4s1.5 4 3 4h3v-3c0-2 2-3 4-3h5c2 0 4-1 4-3V6c0-3-2-4-5-4z"/>' +
    '<circle cx="9" cy="5" r="1" fill="currentColor"/>' +
    '<circle cx="15" cy="19" r="1" fill="currentColor"/>',
  // HTML — bold angle-bracket code: </>
  html:
    '<polyline points="8 5 2 12 8 19"/>' +
    '<polyline points="16 5 22 12 16 19"/>' +
    '<line x1="14" y1="3" x2="10" y2="21"/>',
  // JSON — bold { }
  json:
    '<path d="M9 3c-3 0-3 4-3 6 0 3-3 3-3 3s3 0 3 3 0 6 3 6"/>' +
    '<path d="M15 3c3 0 3 4 3 6 0 3 3 3 3 3s-3 0-3 3 0 6-3 6"/>',
  // JavaScript — JS letters in a rounded badge
  javascript:
    '<rect x="2" y="2" width="20" height="20" rx="2.5"/>' +
    '<path d="M11 11v6c0 1.5-1 2.2-2.3 2.2S6.5 18.5 6.5 17"/>' +
    '<path d="M14 17.5c0 1.2 1.2 1.7 2.5 1.7s2.5-.6 2.5-1.7c0-2.5-5-2.2-5-4.5 0-1.2 1-1.7 2.3-1.7s2.2.6 2.2 1.7"/>',
  // TypeScript — TS in a rounded badge
  typescript:
    '<rect x="2" y="2" width="20" height="20" rx="2.5"/>' +
    '<polyline points="6 11 13 11 9.5 11 9.5 19"/>' +
    '<path d="M14 17.5c0 1.2 1.2 1.7 2.5 1.7s2.5-.6 2.5-1.7c0-2.5-5-2.2-5-4.5 0-1.2 1-1.7 2.3-1.7s2.2.6 2.2 1.7"/>',
  // YAML — bold indented bullet list
  yaml:
    '<circle cx="5" cy="6.5" r="1.2" fill="currentColor"/>' +
    '<line x1="8" y1="6.5" x2="21" y2="6.5"/>' +
    '<circle cx="8" cy="12" r="1.2" fill="currentColor"/>' +
    '<line x1="11" y1="12" x2="21" y2="12"/>' +
    '<circle cx="8" cy="17.5" r="1.2" fill="currentColor"/>' +
    '<line x1="11" y1="17.5" x2="19" y2="17.5"/>',
  // CSS — # symbol big and bold
  css:
    '<line x1="9" y1="3" x2="7" y2="21"/>' +
    '<line x1="17" y1="3" x2="15" y2="21"/>' +
    '<line x1="3" y1="9" x2="21" y2="9"/>' +
    '<line x1="3" y1="15" x2="21" y2="15"/>',
  // Bash / shell — terminal window with > prompt + cursor
  bash:
    '<rect x="2" y="4" width="20" height="16" rx="1.5"/>' +
    '<polyline points="6 10 9 13 6 16"/>' +
    '<line x1="12" y1="16" x2="18" y2="16"/>',
  sh:
    '<rect x="2" y="4" width="20" height="16" rx="1.5"/>' +
    '<polyline points="6 10 9 13 6 16"/>' +
    '<line x1="12" y1="16" x2="18" y2="16"/>',
  // SQL — database cylinder
  sql:
    '<ellipse cx="12" cy="5" rx="9" ry="3"/>' +
    '<path d="M3 5v6c0 1.7 4 3 9 3s9-1.3 9-3V5"/>' +
    '<path d="M3 11v6c0 1.7 4 3 9 3s9-1.3 9-3v-6"/>' +
    '<path d="M3 17v2c0 1.7 4 3 9 3s9-1.3 9-3v-2"/>',
  // PDF — doc with bold "PDF" block
  pdf:
    '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>' +
    '<polyline points="14 2 14 8 20 8"/>' +
    '<path d="M7 14h1.5a1.2 1.2 0 0 1 0 2.4H7"/>' +
    '<path d="M11 14h1.3a1.4 1.4 0 0 1 1.4 1.4v.6a1.4 1.4 0 0 1-1.4 1.4H11z"/>' +
    '<line x1="15.5" y1="14" x2="17.5" y2="14"/>' +
    '<line x1="15.5" y1="15.7" x2="17" y2="15.7"/>' +
    '<line x1="15.5" y1="14" x2="15.5" y2="17.5"/>',
  // Email — bold envelope
  email:
    '<rect x="2" y="4" width="20" height="16" rx="2"/>' +
    '<path d="m22 7-8.97 5.7a1.94 1.94 0 0 1-2.06 0L2 7"/>',
  // XML — angle brackets like HTML
  xml:
    '<polyline points="8 5 2 12 8 19"/>' +
    '<polyline points="16 5 22 12 16 19"/>' +
    '<line x1="14" y1="3" x2="10" y2="21"/>',
  // SVG — overlapping geometric shapes
  svg:
    '<circle cx="7" cy="7" r="4"/>' +
    '<rect x="13" y="13" width="8" height="8"/>' +
    '<polygon points="13 3 21 3 17 11"/>',
  // Rust — gear / cog (Rust's mark is a gear with R inside)
  rust:
    '<circle cx="12" cy="12" r="3"/>' +
    '<path d="M12 2v3 M12 19v3 M2 12h3 M19 12h3 M4.93 4.93l2.12 2.12 M16.95 16.95l2.12 2.12 M4.93 19.07l2.12-2.12 M16.95 7.05l2.12-2.12"/>' +
    '<circle cx="12" cy="12" r="8"/>',
  // Go — gopher face (circle with two eyes + smile)
  go:
    '<circle cx="12" cy="12" r="9"/>' +
    '<circle cx="9" cy="10" r="1.4" fill="currentColor"/>' +
    '<circle cx="15" cy="10" r="1.4" fill="currentColor"/>' +
    '<path d="M9 15c.8 1.5 5.2 1.5 6 0"/>',
  // Java — coffee cup with steam (Java = coffee)
  java:
    '<path d="M6 11h11v6a3 3 0 0 1-3 3H9a3 3 0 0 1-3-3z"/>' +
    '<path d="M17 12h1.5a2.5 2.5 0 0 1 0 5H17"/>' +
    '<path d="M9 4c0 1.2-1 1.8-1 3s1 1.8 1 3"/>' +
    '<path d="M13 4c0 1.2-1 1.8-1 3s1 1.8 1 3"/>',
  // C — bold open arc
  c:
    '<path d="M18 7a7 7 0 1 0 0 10"/>',
  // C++ — C + two plus signs
  cpp:
    '<path d="M10 7a5 5 0 1 0 0 10"/>' +
    '<line x1="15" y1="10" x2="15" y2="14"/>' +
    '<line x1="13" y1="12" x2="17" y2="12"/>' +
    '<line x1="20" y1="10" x2="20" y2="14"/>' +
    '<line x1="18" y1="12" x2="22" y2="12"/>',
  // C# — C + sharp (♯)
  csharp:
    '<path d="M10 7a5 5 0 1 0 0 10"/>' +
    '<line x1="17" y1="7" x2="15" y2="17"/>' +
    '<line x1="22" y1="7" x2="20" y2="17"/>' +
    '<line x1="14" y1="11" x2="22.5" y2="11"/>' +
    '<line x1="13.5" y1="13" x2="22" y2="13"/>',
  // Ruby — gem with cut facets
  ruby:
    '<polygon points="12 2 21 9 12 22 3 9"/>' +
    '<line x1="3" y1="9" x2="21" y2="9"/>' +
    '<line x1="8" y1="9" x2="12" y2="22"/>' +
    '<line x1="16" y1="9" x2="12" y2="22"/>' +
    '<line x1="8" y1="9" x2="12" y2="2"/>' +
    '<line x1="16" y1="9" x2="12" y2="2"/>',
  // PHP — stylised elephant (PHP's mascot, simplified)
  php:
    '<path d="M3 14c0-3 3-6 7-6h5c2.5 0 5 1.5 5 4v2c0 2-1.5 3.5-3.5 3.5H17"/>' +
    '<path d="M17 17v2 M7 17v3 M11 17v3"/>' +
    '<path d="M18 12c1 0 1.5-.7 1.5-1.5"/>' +
    '<circle cx="7" cy="11" r="0.6" fill="currentColor"/>',
  // Mermaid — connected diagram nodes and directed edges.
  mermaid:
    '<rect x="3" y="4" width="7" height="5" rx="1"/>' +
    '<rect x="14" y="15" width="7" height="5" rx="1"/>' +
    '<path d="M10 7h4M17 9v6M14 12l3-3 3 3"/>',
  // Generic code fallback (used by toml/ini already; left as-is)
  code:
    '<polyline points="8 6 2 12 8 18"/>' +
    '<polyline points="16 6 22 12 16 18"/>',
};

// Neutral Open Clank filesystem/navigation glyphs. These are intentionally
// platform-agnostic: the operating system may supply content-thumbnail pixels,
// but it never supplies the Files chrome or file/folder icon language.
const GLYPHS = {
  file:
    '<path d="M6 2h8l4 4v16H6z"/>' +
    '<path d="M14 2v5h5"/>',
  folder:
    '<path d="M3 6.5h6l2 2H21v10.5a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>' +
    '<path d="M3 10h18"/>',
  'folder-open':
    '<path d="M3 8V6.5h6l2 2h8a2 2 0 0 1 2 2v1"/>' +
    '<path d="M4.5 11h17l-3 10h-17z"/>',
  image:
    '<rect x="3" y="3" width="18" height="18" rx="2"/>' +
    '<circle cx="8.5" cy="8.5" r="1.5"/>' +
    '<path d="m4 18 5-5 3 3 2-2 6 6"/>',
  video:
    '<rect x="3" y="5" width="18" height="14" rx="2"/>' +
    '<path d="m10 9 5 3-5 3z"/>',
  audio:
    '<path d="M9 18V6l10-2v12"/>' +
    '<circle cx="6" cy="18" r="3"/>' +
    '<circle cx="16" cy="16" r="3"/>',
  archive:
    '<path d="M6 2h12v20H6z"/>' +
    '<path d="M10 2v3h4V2M10 8h4M10 11h4M10 14h4"/>' +
    '<rect x="10" y="17" width="4" height="3" rx=".5"/>',
  text:
    '<path d="M6 2h8l4 4v16H6z"/>' +
    '<path d="M14 2v5h5M9 11h6M9 15h6M9 19h4"/>',
  spreadsheet:
    '<rect x="3" y="3" width="18" height="18" rx="2"/>' +
    '<path d="M3 9h18M3 15h18M9 3v18M15 3v18"/>',
  presentation:
    '<rect x="3" y="4" width="18" height="13" rx="2"/>' +
    '<path d="M8 21h8M12 17v4M7 13l3-3 3 2 4-4"/>',
  database:
    '<ellipse cx="12" cy="5" rx="8" ry="3"/>' +
    '<path d="M4 5v7c0 1.7 3.6 3 8 3s8-1.3 8-3V5M4 12v7c0 1.7 3.6 3 8 3s8-1.3 8-3v-7"/>',
  font:
    '<path d="M5 20 11 4h2l6 16M7 15h10"/>',
  executable:
    '<rect x="3" y="4" width="18" height="16" rx="2"/>' +
    '<path d="m7 9 3 3-3 3M13 15h4"/>',
  volume:
    '<path d="M4 5h16v14H4z"/>' +
    '<path d="M4 15h16M8 18h.01M16 18h.01"/>',
  workspace:
    '<rect x="3" y="4" width="18" height="16" rx="2"/>' +
    '<path d="M8 4v16M8 9h13"/>',
  gallery:
    '<rect x="3" y="3" width="18" height="18" rx="2"/>' +
    '<path d="M8 3v18M3 9h18M8 15h13"/>',
  library:
    '<path d="M4 5h5v15H4zM10 5h5v15h-5zM16 4l4-1 2 15-4 1z"/>',
  star:
    '<path d="m12 3 2.8 5.7 6.2.9-4.5 4.4 1.1 6.2-5.6-3-5.6 3 1.1-6.2L3 9.6l6.2-.9z"/>',
  'star-filled':
    '<path d="m12 3 2.8 5.7 6.2.9-4.5 4.4 1.1 6.2-5.6-3-5.6 3 1.1-6.2L3 9.6l6.2-.9z" fill="currentColor"/>',
  'chevron-right': '<path d="m9 5 7 7-7 7"/>',
  'chevron-down': '<path d="m5 9 7 7 7-7"/>',
  up: '<path d="m6 10 6-6 6 6M12 4v16"/>',
  refresh: '<path d="M20 11a8 8 0 1 0 1 4"/><path d="M20 4v7h-7"/>',
  restore: '<path d="M4 10a8 8 0 1 1 2 8"/><path d="M4 4v6h6"/><path d="M12 8v5l3 2"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="m16 16 5 5"/>',
  download:
    '<path d="M12 3v12M7 10l5 5 5-5"/>' +
    '<path d="M4 19h16"/>',
  'folder-plus':
    '<path d="M3 6.5h6l2 2H21v10.5a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>' +
    '<path d="M3 10h18M12 13v5M9.5 15.5h5"/>',
  close: '<path d="m6 6 12 12M18 6 6 18"/>',
  symlink:
    '<path d="M10 13a5 5 0 0 0 7.5.5l2-2a5 5 0 0 0-7-7l-1.1 1.1"/>' +
    '<path d="M14 11a5 5 0 0 0-7.5-.5l-2 2a5 5 0 0 0 7 7l1.1-1.1"/>',
  unavailable:
    '<path d="M6 2h8l4 4v16H6zM14 2v5h5"/>' +
    '<path d="m8 16 8-8"/>',
  error:
    '<path d="M12 3 2.5 20h19z"/>' +
    '<path d="M12 9v5M12 17h.01"/>',
  loading:
    '<path d="M20 12a8 8 0 1 1-2.3-5.7"/>' +
    '<path d="M20 4v6h-6"/>',
};

const ALIASES = {
  md: 'markdown',
  py: 'python',
  htm: 'html',
  js: 'javascript',
  ts: 'typescript',
  yml: 'yaml',
  shell: 'bash',
  zsh: 'bash',
  'c++': 'cpp',
  'c#': 'csharp',
  rs: 'rust',
  rb: 'ruby',
  toml: 'yaml',
  ini: 'yaml',
  cjs: 'javascript',
  mjs: 'javascript',
  jsx: 'javascript',
  tsx: 'typescript',
  scss: 'css',
  cc: 'cpp',
  cs: 'csharp',
  h: 'cpp',
  hpp: 'cpp',
  mmd: 'mermaid',
  mermaid: 'mermaid',
};

/**
 * Return SVG markup for the given language/type, or '' if unknown.
 * @param {string} lang   language name (case-insensitive)
 * @param {number} [size] pixel width/height of the rendered SVG (default 14)
 * @param {object} [opts] { className, style } extra attrs on the <svg>
 */
export function langIcon(lang, size = 14, opts = {}) {
  if (!lang) return '';
  const key = String(lang).toLowerCase();
  const inner = ICONS[key] || ICONS[ALIASES[key]] || '';
  if (!inner) return '';
  const cls = (opts && opts.className) ? ` class="${opts.className}"` : '';
  const style = (opts && opts.style) ? ` style="${opts.style}"` : '';
  return (
    `<svg${cls}${style} width="${size}" height="${size}" viewBox="0 0 24 24" ` +
    `fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" ` +
    `aria-hidden="true" focusable="false">` +
    `${inner}</svg>`
  );
}

function svgMarkup(inner, size, opts = {}) {
  const cls = (opts && opts.className) ? ` class="${opts.className}"` : '';
  const style = (opts && opts.style) ? ` style="${opts.style}"` : '';
  return (
    `<svg${cls}${style} width="${size}" height="${size}" viewBox="0 0 24 24" ` +
    `fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" ` +
    `aria-hidden="true" focusable="false">` +
    `${inner}</svg>`
  );
}

/** Return one of the neutral Open Clank filesystem/navigation glyphs. */
export function glyphIcon(name, size = 16, opts = {}) {
  const inner = GLYPHS[String(name || '').toLowerCase()] || GLYPHS.file;
  return svgMarkup(inner, size, opts);
}

const CODE_EXTENSIONS = new Set([
  'c', 'cc', 'cjs', 'cpp', 'cs', 'css', 'go', 'h', 'hpp', 'htm', 'html', 'ini', 'java',
  'js', 'json', 'jsx', 'md', 'mmd', 'mermaid', 'mjs', 'php', 'py', 'rb', 'rs', 'scss', 'sh', 'sql',
  'svg', 'toml', 'ts', 'tsx', 'xml', 'yaml', 'yml', 'zsh',
]);
const IMAGE_EXTENSIONS = new Set(['avif', 'bmp', 'gif', 'heic', 'heif', 'ico', 'jpeg', 'jpg', 'png', 'tif', 'tiff', 'webp']);
const VIDEO_EXTENSIONS = new Set(['avi', 'm4v', 'mkv', 'mov', 'mp4', 'mpeg', 'mpg', 'webm']);
const AUDIO_EXTENSIONS = new Set(['aac', 'aiff', 'flac', 'm4a', 'mp3', 'ogg', 'opus', 'wav']);
const ARCHIVE_EXTENSIONS = new Set(['7z', 'bz2', 'gz', 'rar', 'tar', 'tgz', 'xz', 'zip']);
const DOCUMENT_EXTENSIONS = new Set(['doc', 'docx', 'odt', 'pages', 'rtf', 'txt']);
const SHEET_EXTENSIONS = new Set(['numbers', 'ods', 'xls', 'xlsx']);
const PRESENTATION_EXTENSIONS = new Set(['key', 'odp', 'ppt', 'pptx']);
const DATABASE_EXTENSIONS = new Set(['db', 'redb', 'sqlite', 'sqlite3']);
const FONT_EXTENSIONS = new Set(['eot', 'otf', 'ttc', 'ttf', 'woff', 'woff2']);
const ROLE_ALIASES = {
  favorite: 'star-filled',
  favorites: 'star-filled',
};
const STATE_ALIASES = {
  denied: 'unavailable',
  failed: 'error',
  offline: 'unavailable',
  pending: 'loading',
  revoked: 'unavailable',
};
const MIME_ICONS = {
  'application/javascript': 'javascript',
  'application/json': 'json',
  'application/ld+json': 'json',
  'application/sql': 'sql',
  'application/typescript': 'typescript',
  'application/xml': 'xml',
  'application/x-yaml': 'yaml',
  'image/svg+xml': 'svg',
  'text/javascript': 'javascript',
  'text/markdown': 'markdown',
  'text/typescript': 'typescript',
  'text/x-markdown': 'markdown',
  'text/x-python': 'python',
  'text/x-shellscript': 'bash',
  'text/x-sql': 'sql',
  'text/xml': 'xml',
  'text/yaml': 'yaml',
  'text/x-mermaid': 'mermaid',
};

function extensionOf(name) {
  const value = String(name || '').toLowerCase().replace(/[\\/]+$/, '');
  const leaf = value.split(/[\\/]/).pop() || '';
  if (!leaf.includes('.') || leaf.startsWith('.') && leaf.indexOf('.', 1) < 0) return '';
  return leaf.split('.').pop() || '';
}

/** Resolve a stable icon key without touching file contents or the operating system. */
export function fileIconKey(descriptor = {}) {
  const safe = descriptor && typeof descriptor === 'object' ? descriptor : {};
  const name = safe.name || safe.filename || safe.path || '';
  const kind = safe.kind || safe.type || '';
  const mimeType = safe.mimeType || safe.mime_type || safe.media_type || '';
  const language = String(safe.language || '').trim().toLowerCase();
  const role = String(safe.role || safe.navigationRole || safe.navigation_role || '').trim().toLowerCase();
  const state = String(safe.state || safe.iconState || safe.icon_state || '').trim().toLowerCase();
  const open = !!safe.open;
  const normalizedKind = String(kind || '').trim().toLowerCase();
  const mime = String(mimeType || '').split(';', 1)[0].trim().toLowerCase();
  const roleKey = ROLE_ALIASES[role] || role;
  const stateKey = STATE_ALIASES[state] || state;
  if (GLYPHS[stateKey]) return stateKey;
  if (GLYPHS[roleKey]) return roleKey;
  if (normalizedKind.includes('directory') || normalizedKind === 'folder') return open ? 'folder-open' : 'folder';
  if (normalizedKind.includes('volume') || normalizedKind === 'drive') return 'volume';
  if (normalizedKind.includes('symlink') || normalizedKind === 'link') return 'symlink';
  const kindState = STATE_ALIASES[normalizedKind] || normalizedKind;
  if (['unavailable', 'error', 'loading'].includes(kindState)) return kindState;
  const leaf = String(name || '').toLowerCase().split(/[\\/]/).pop() || '';
  const extension = extensionOf(leaf);
  const languageKey = ALIASES[language] || language;
  if (ICONS[languageKey]) return languageKey;
  if (extension === 'svg' || mime === 'image/svg+xml') return 'svg';
  if (extension === 'csv' || mime.includes('csv')) return 'csv';
  if (mime.startsWith('image/') || IMAGE_EXTENSIONS.has(extension)) return 'image';
  if (mime.startsWith('video/') || VIDEO_EXTENSIONS.has(extension)) return 'video';
  if (mime.startsWith('audio/') || AUDIO_EXTENSIONS.has(extension)) return 'audio';
  if (mime.includes('zip') || mime.includes('archive') || ARCHIVE_EXTENSIONS.has(extension)) return 'archive';
  if (extension === 'pdf' || mime === 'application/pdf') return 'pdf';
  if (CODE_EXTENSIONS.has(extension) || ['dockerfile', 'makefile'].includes(leaf)) return ALIASES[extension] || extension || 'code';
  if (SHEET_EXTENSIONS.has(extension) || mime.includes('spreadsheet')) return 'spreadsheet';
  if (PRESENTATION_EXTENSIONS.has(extension) || mime.includes('presentation')) return 'presentation';
  if (DATABASE_EXTENSIONS.has(extension) || mime.includes('database')) return 'database';
  if (FONT_EXTENSIONS.has(extension) || mime.startsWith('font/')) return 'font';
  const mimeKey = MIME_ICONS[mime] || ALIASES[mime] || mime;
  if (ICONS[mimeKey] || GLYPHS[mimeKey]) return mimeKey;
  if (DOCUMENT_EXTENSIONS.has(extension) || mime.startsWith('text/')) return 'text';
  if (normalizedKind.includes('executable') || mime.includes('executable')) return 'executable';
  return 'file';
}

/** Return a language icon when known, otherwise the neutral filetype glyph. */
export function fileIcon(descriptor = {}, size = 16, opts = {}) {
  const key = fileIconKey(descriptor);
  if (ICONS[key] || ICONS[ALIASES[key]]) return langIcon(key, size, opts);
  return glyphIcon(key, size, opts);
}

export default { langIcon, glyphIcon, fileIcon, fileIconKey };
