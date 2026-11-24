// Compatibility seam for file/language callers. Artwork lives in uiIcons.js.
import { LANGUAGE_REGISTRY, resolveLanguage, isTextPath, EDITOR_BINARY_SUFFIX_PATTERN } from './editor/languageRegistry.js';
import { uiIcon, iconId, hasUiIcon } from './uiIcons.js';
export { uiIcon, UI_ICON_IDS } from './uiIcons.js';

// Map registry IDs to artwork families, never duplicate its source associations.
const LANGUAGE_ART = Object.freeze({ plaintext:'text', javascriptreact:'javascript', typescriptreact:'typescript',
  jsonc:'json', json5:'json', jsonl:'json', scss:'css', less:'css', sass:'css', uss:'css',
  shellscript:'bash', powershell:'bash', bat:'bash', toml:'yaml', ini:'yaml', 'unity-yaml':'yaml',
  xsl:'xml', uxml:'xml', handlebars:'html', razor:'html', jade:'html',
  chatagent:'markdown', instructions:'markdown', prompt:'markdown', mdx:'markdown' });
const legacyLanguages = Object.freeze({ md:'markdown', py:'python', htm:'html', js:'javascript', ts:'typescript',
  yml:'yaml', shell:'bash', zsh:'bash', sh:'bash', 'c++':'cpp', 'c#':'csharp', rs:'rust', rb:'ruby',
  cjs:'javascript', mjs:'javascript', jsx:'javascript', tsx:'typescript', cs:'csharp', cc:'cpp', h:'cpp', hpp:'cpp', mmd:'mermaid' });
const knownLanguages = new Map();
for (const entry of LANGUAGE_REGISTRY) {
  for (const name of [entry.id,entry.displayName,entry.modeName,...entry.aliases].filter(Boolean)) {
    const key = String(name).toLowerCase();
    if (!knownLanguages.has(key) || key === entry.id) knownLanguages.set(key,entry.id);
  }
}
function languageArt(language) {
  const key = String(language || '').trim().toLowerCase();
  const id = legacyLanguages[key] || knownLanguages.get(key);
  if (!id) return ['pdf','csv','email','svg','code','text'].includes(key) ? key : '';
  const family = LANGUAGE_ART[id] || id;
  return hasUiIcon(family) ? family : 'code';
}
/** SVG for a registered language/type, or empty markup when unknown. */
export function langIcon(language, size = 14, options = {}) {
  const key = languageArt(language);
  return key ? uiIcon(key,size,options) : '';
}
export function glyphIcon(name, size = 16, options = {}) { return uiIcon(name,size,options); }

const IMAGE_EXTENSIONS = new Set(['avif','bmp','gif','heic','heif','ico','jpeg','jpg','png','tif','tiff','webp']);
const VIDEO_EXTENSIONS = new Set(['avi','m4v','mkv','mov','mp4','mpeg','mpg','webm']);
const AUDIO_EXTENSIONS = new Set(['aac','aiff','flac','m4a','mp3','ogg','opus','wav']);
const ARCHIVE_EXTENSIONS = new Set(['7z','bz2','gz','rar','tar','tgz','xz','zip']);
const DOCUMENT_EXTENSIONS = new Set(['doc','docx','odt','pages','rtf','txt']);
const SHEET_EXTENSIONS = new Set(['numbers','ods','xls','xlsx']);
const PRESENTATION_EXTENSIONS = new Set(['key','odp','ppt','pptx']);
const DATABASE_EXTENSIONS = new Set(['db','redb','sqlite','sqlite3']);
const FONT_EXTENSIONS = new Set(['eot','otf','ttc','ttf','woff','woff2']);
const ROLES = Object.freeze({ favorite:'star-filled', favorites:'star-filled', workspaces:'workspace',
  images:'gallery', documents:'library', 'image-gallery':'gallery', 'document-library':'library',
  'email-library':'email', 'file-home':'home', 'this-computer':'computer', collections:'library' });
const STATES = new Set(['unavailable','error','loading','success','warning','info','upload','download']);
const MIME_LANGUAGE = Object.freeze({
  'application/javascript':'javascript','application/json':'json','application/ld+json':'json',
  'application/sql':'sql','application/typescript':'typescript','application/xml':'xml','application/x-yaml':'yaml',
  'image/svg+xml':'svg','text/javascript':'javascript','text/markdown':'markdown','text/typescript':'typescript',
  'text/x-markdown':'markdown','text/x-python':'python','text/x-shellscript':'bash','text/x-sql':'sql',
  'text/xml':'xml','text/yaml':'yaml','text/x-mermaid':'mermaid',
});
const BINARY_SOURCE_SUFFIX = new RegExp(EDITOR_BINARY_SUFFIX_PATTERN, 'i');
function safeDescriptor(value) { return value && typeof value === 'object' ? value : {}; }
function stateOf(safe) { return iconId(safe.state || safe.iconState || safe.icon_state || ''); }
function baseKey(safe) {
  const name = String(safe.name || safe.filename || safe.path || '');
  const kind = String(safe.kind || safe.type || '').trim().toLowerCase();
  const mime = String(safe.mimeType || safe.mime_type || safe.media_type || '').split(';',1)[0].trim().toLowerCase();
  const role = String(safe.role || safe.navigationRole || safe.navigation_role || '').trim().toLowerCase();
  if (role && hasUiIcon(ROLES[role] || role)) return iconId(ROLES[role] || role);
  if (kind.includes('directory') || kind === 'folder') return safe.open ? 'folder-open' : 'folder';
  if (kind.includes('volume') || kind === 'drive') return 'volume';
  if (kind.includes('symlink') || kind === 'link') return 'symlink';
  const leaf = name.toLowerCase().replace(/[\\/]+$/,'').split(/[\\/]/).pop() || '';
  const extension = leaf.includes('.') ? leaf.split('.').pop() : '';
  if (extension === 'svg' || mime === 'image/svg+xml') return 'svg';
  if (extension === 'csv' || extension === 'tsv' || mime.includes('csv')) return 'csv';
  // Extension-guessed video/mp2t also describes TypeScript. The canonical
  // source association wins unless the descriptor supplies real media/binary evidence.
  const path = safe.path || name;
  const sample = typeof safe.content === 'string' ? safe.content.slice(0,4096) : String(safe.firstLine || '').slice(0,4096);
  const binary = safe.binary === true || safe.isBinary === true || safe.is_binary === true
    || kind === 'binary' || kind.includes('executable') || mime.includes('executable')
    || BINARY_SOURCE_SUFFIX.test(path) || sample.includes('\0');
  const mediaKind = ['image','video','audio'].find(family => kind === family || kind.startsWith(family + '/'));
  const explicitMedia = mediaKind || kind === 'media'
    || IMAGE_EXTENSIONS.has(extension) || VIDEO_EXTENSIONS.has(extension) || AUDIO_EXTENSIONS.has(extension)
    || mime.startsWith('image/') || mime.startsWith('audio/')
    || (mime.startsWith('video/') && mime !== 'video/mp2t');
  const explicit = !binary && !explicitMedia ? languageArt(safe.language) : '';
  const source = !binary && !explicitMedia ? resolveLanguage('', { path, content:sample }) : null;
  if (mime === 'video/mp2t') {
    if (explicit) return explicit;
    if (source && source.id !== 'plaintext') return languageArt(source.id) || 'code';
  }
  if (mediaKind) return mediaKind;
  if (mime.startsWith('image/') || IMAGE_EXTENSIONS.has(extension)) return 'image';
  if (mime.startsWith('video/') || VIDEO_EXTENSIONS.has(extension)) return 'video';
  if (mime.startsWith('audio/') || AUDIO_EXTENSIONS.has(extension)) return 'audio';
  if (mime.includes('zip') || mime.includes('archive') || ARCHIVE_EXTENSIONS.has(extension)) return 'archive';
  if (extension === 'pdf' || mime === 'application/pdf') return 'pdf';
  if (SHEET_EXTENSIONS.has(extension) || mime.includes('spreadsheet')) return 'spreadsheet';
  if (PRESENTATION_EXTENSIONS.has(extension) || mime.includes('presentation')) return 'presentation';
  if (DATABASE_EXTENSIONS.has(extension) || mime.includes('database')) return 'database';
  if (FONT_EXTENSIONS.has(extension) || mime.startsWith('font/')) return 'font';
  if (kind.includes('executable') || mime.includes('executable')) return 'executable';
  if (explicit) return explicit;
  if (source && source.id !== 'plaintext') return languageArt(source.id) || 'code';
  if (binary || explicitMedia) return 'file';
  if (MIME_LANGUAGE[mime]) return MIME_LANGUAGE[mime];
  if (isTextPath(safe.path || name) || DOCUMENT_EXTENSIONS.has(extension) || mime.startsWith('text/')) return 'text';
  return 'file';
}
/** Stable classification, with legacy state precedence. No content/OS reads. */
export function fileIconKey(descriptor = {}) {
  const safe = safeDescriptor(descriptor);
  const state = stateOf(safe) || iconId(safe.kind || safe.type);
  return STATES.has(state) ? state : baseKey(safe);
}
/** Preserve the underlying file/collection silhouette when a status is present. */
export function fileIcon(descriptor = {}, size = 16, options = {}) {
  const safe = safeDescriptor(descriptor);
  const state = stateOf(safe) || iconId(safe.kind || safe.type);
  const base = baseKey(safe);
  const meaningfulBase = base !== 'file' || !!(safe.name || safe.filename || safe.path);
  if (STATES.has(state)) return uiIcon(meaningfulBase ? base : state,size,{ ...options, state:meaningfulBase ? state : options.state });
  return uiIcon(base,size,options);
}
export default { langIcon, glyphIcon, fileIcon, fileIconKey, uiIcon };
