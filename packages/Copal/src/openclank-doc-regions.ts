/**
 * Documentation-region adapters for rich source comments.
 *
 * Every advertised language label receives a real comment/docstring adapter:
 * Lezer structure where a grammar exists, stateful stream scanners for the
 * legacy StreamLanguage rows. Adapters return exact source bounds, content
 * spans (delimiter-stripped Markdown), delimiter kind and safe insertion
 * boundaries. Ordinary strings stay raw highlighted code; only genuine
 * documentation comments and Python docstrings project Markdown.
 *
 * Round-trip contract: concatenating each region's outer source with the
 * skipped text between regions reconstructs the original bytes exactly.
 * An untouched projection never mutates program bytes.
 */

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type DelimiterKind =
  | 'line'
  | 'block'
  | 'doc-line'
  | 'doc-block'
  | 'docstring'
  | 'html-comment'
  | 'mermaid-comment'
  | 'none';

export interface ContentSpan {
  from: number;
  to: number;
}

export interface DocumentationRegion {
  /** Exact outer source start, including the open delimiter. */
  from: number;
  /** Exact outer source end, including the close delimiter when present. */
  to: number;
  contentFrom: number;
  contentTo: number;
  /** Per-line source spans of Markdown content after delimiter stripping. */
  contentRanges: ContentSpan[];
  /** Content language; documentation content is always Markdown. */
  language: string;
  delimiterKind: DelimiterKind;
  open: string;
  close?: string;
  kind: 'comment' | 'docstring';
  /** Line start of the first line covered by this region (indentation context). */
  lineStart: number;
  /** End of the last line covered by this region (before the next line break). */
  lineEnd: number;
}

export type SafeInsertionResult =
  | { ok: true; from: number; to: number; indent: string; text: string; comment: string }
  | { ok: false; error: string };

export type CommentWrapResult =
  | { ok: true; text: string }
  | { ok: false; error: string };

export interface RegionOptions {
  /** Dialect under a shared label, e.g. `jsonc` under `JSON`. */
  dialect?: string;
  /** Filename used for dialect routing when language label alone is ambiguous. */
  path?: string;
}

// ---------------------------------------------------------------------------
// Consolidated language registry (30 advertised labels + aliases)
// ---------------------------------------------------------------------------

export const ADVERTISED_LANGUAGE_LABELS: readonly string[] = Object.freeze([
  'JavaScript', 'JavaScript JSX', 'TypeScript', 'TypeScript JSX', 'Python', 'Rust', 'Go', 'Java',
  'Kotlin', 'C', 'C/C++ header', 'C++', 'C++ header', 'C#', 'Ruby', 'PHP', 'Swift', 'Shell',
  'JSON', 'YAML', 'TOML', 'XML', 'HTML', 'CSS', 'SCSS', 'Markdown', 'SQL', 'Mermaid', 'Dockerfile',
  'Plain text',
]);

const ALIAS_TO_LABEL: Record<string, string> = {
  'javascript': 'JavaScript', 'js': 'JavaScript', 'node': 'JavaScript', 'mjs': 'JavaScript', 'cjs': 'JavaScript',
  'javascript jsx': 'JavaScript JSX', 'jsx': 'JavaScript JSX',
  'typescript': 'TypeScript', 'ts': 'TypeScript',
  'typescript jsx': 'TypeScript JSX', 'tsx': 'TypeScript JSX',
  'python': 'Python', 'py': 'Python',
  'rust': 'Rust', 'rs': 'Rust',
  'go': 'Go', 'golang': 'Go',
  'java': 'Java',
  'kotlin': 'Kotlin', 'kt': 'Kotlin', 'kts': 'Kotlin',
  'c': 'C',
  'c/c++ header': 'C/C++ header', 'c/c++header': 'C/C++ header', 'c header': 'C/C++ header',
  'c++': 'C++', 'cpp': 'C++', 'cxx': 'C++', 'cc': 'C++',
  'c++ header': 'C++ header', 'c++header': 'C++ header', 'hpp': 'C++ header', 'hxx': 'C++ header', 'hh': 'C++ header',
  'c#': 'C#', 'csharp': 'C#', 'cs': 'C#',
  'ruby': 'Ruby', 'rb': 'Ruby',
  'php': 'PHP',
  'swift': 'Swift',
  'shell': 'Shell', 'bash': 'Shell', 'zsh': 'Shell', 'sh': 'Shell', 'shellscript': 'Shell',
  'json': 'JSON', 'jsonc': 'JSON', 'json with comments': 'JSON',
  'yaml': 'YAML', 'yml': 'YAML',
  'toml': 'TOML',
  'xml': 'XML',
  'html': 'HTML', 'htm': 'HTML',
  'css': 'CSS',
  'scss': 'SCSS', 'sass': 'SCSS',
  'markdown': 'Markdown', 'md': 'Markdown',
  'sql': 'SQL',
  'mermaid': 'Mermaid', 'mmd': 'Mermaid',
  'dockerfile': 'Dockerfile', 'docker': 'Dockerfile',
  'plain text': 'Plain text', 'text': 'Plain text', 'plaintext': 'Plain text', 'txt': 'Plain text', '': 'Plain text',
};

const EXTENSION_TO_DIALECT: Record<string, string> = {
  jsonc: 'jsonc', json5: 'jsonc', json: 'json',
};

const FILENAME_TO_DIALECT: Record<string, string> = {
  '.babelrc': 'jsonc', '.eslintrc': 'jsonc', '.prettierrc': 'jsonc', 'tsconfig.json': 'jsonc', 'jsconfig.json': 'jsonc',
};

interface CommentForm {
  open: string;
  close?: string;
  doc?: boolean;
  nested?: boolean;
}

interface LanguageSpec {
  label: string;
  /** Comment forms usable as rich documentation regions. */
  forms: CommentForm[];
  /** Whether Python-style docstrings apply. */
  docstrings?: boolean;
  /** HTML/PHP embed other grammars inside script/style regions. */
  embedded?: boolean;
  /** Stream-adapter id when no Lezer grammar backs comment extraction. */
  streamAdapter?: string;
}

const LANGUAGE_SPECS: Record<string, LanguageSpec> = {
  'JavaScript': {
    label: 'JavaScript',
    forms: [
      { open: '///', doc: true }, { open: '//!', doc: true }, { open: '//' }, { open: '/**', close: '*/', doc: true }, { open: '/*!', close: '*/', doc: true }, { open: '/*', close: '*/' },
    ],
  },
  'JavaScript JSX': {
    label: 'JavaScript JSX',
    forms: [
      { open: '///', doc: true }, { open: '//' }, { open: '/**', close: '*/', doc: true }, { open: '/*', close: '*/' },
    ],
  },
  'TypeScript': {
    label: 'TypeScript',
    forms: [
      { open: '///', doc: true }, { open: '//!', doc: true }, { open: '//' }, { open: '/**', close: '*/', doc: true }, { open: '/*!', close: '*/', doc: true }, { open: '/*', close: '*/' },
    ],
  },
  'TypeScript JSX': {
    label: 'TypeScript JSX',
    forms: [
      { open: '///', doc: true }, { open: '//' }, { open: '/**', close: '*/', doc: true }, { open: '/*', close: '*/' },
    ],
  },
  'Python': {
    label: 'Python',
    forms: [{ open: '#' }],
    docstrings: true,
  },
  'Rust': {
    label: 'Rust',
    forms: [
      { open: '//!', doc: true }, { open: '///', doc: true }, { open: '//', nested: false },
      { open: '/*!', close: '*/', doc: true, nested: true }, { open: '/**', close: '*/', doc: true, nested: true },
      { open: '/*', close: '*/', nested: true },
    ],
  },
  'Go': {
    label: 'Go',
    forms: [{ open: '//' }, { open: '/*', close: '*/' }],
  },
  'Java': {
    label: 'Java',
    forms: [
      { open: '///', doc: true }, { open: '//' }, { open: '/**', close: '*/', doc: true }, { open: '/*!', close: '*/', doc: true }, { open: '/*', close: '*/' },
    ],
  },
  'Kotlin': {
    label: 'Kotlin',
    forms: [
      { open: '//' }, { open: '/**', close: '*/', doc: true, nested: true }, { open: '/*!', close: '*/', doc: true, nested: true },
      { open: '/*', close: '*/', nested: true },
    ],
    streamAdapter: 'kotlin',
  },
  'C': {
    label: 'C',
    forms: [
      { open: '///', doc: true }, { open: '//' }, { open: '/**', close: '*/', doc: true }, { open: '/*!', close: '*/', doc: true }, { open: '/*', close: '*/' },
    ],
  },
  'C/C++ header': {
    label: 'C/C++ header',
    forms: [
      { open: '///', doc: true }, { open: '//' }, { open: '/**', close: '*/', doc: true }, { open: '/*!', close: '*/', doc: true }, { open: '/*', close: '*/' },
    ],
  },
  'C++': {
    label: 'C++',
    forms: [
      { open: '///', doc: true }, { open: '//' }, { open: '/**', close: '*/', doc: true }, { open: '/*!', close: '*/', doc: true }, { open: '/*', close: '*/' },
    ],
  },
  'C++ header': {
    label: 'C++ header',
    forms: [
      { open: '///', doc: true }, { open: '//' }, { open: '/**', close: '*/', doc: true }, { open: '/*!', close: '*/', doc: true }, { open: '/*', close: '*/' },
    ],
  },
  'C#': {
    label: 'C#',
    forms: [
      { open: '///', doc: true }, { open: '//' }, { open: '/**', close: '*/', doc: true }, { open: '/*', close: '*/' },
    ],
    streamAdapter: 'csharp',
  },
  'Ruby': {
    label: 'Ruby',
    forms: [{ open: '#' }, { open: '=begin', close: '=end', doc: true }],
    streamAdapter: 'ruby',
  },
  'PHP': {
    label: 'PHP',
    forms: [
      { open: '///', doc: true }, { open: '//' }, { open: '#' }, { open: '/**', close: '*/', doc: true }, { open: '/*!', close: '*/', doc: true }, { open: '/*', close: '*/' },
    ],
    embedded: true,
  },
  'Swift': {
    label: 'Swift',
    forms: [
      { open: '///', doc: true }, { open: '//' }, { open: '/**', close: '*/', doc: true, nested: true }, { open: '/*!', close: '*/', doc: true, nested: true },
      { open: '/*', close: '*/', nested: true },
    ],
    streamAdapter: 'swift',
  },
  'Shell': {
    label: 'Shell',
    forms: [{ open: '#' }],
    streamAdapter: 'shell',
  },
  'JSON': {
    label: 'JSON',
    forms: [{ open: '//' }, { open: '/*', close: '*/' }],
    streamAdapter: 'jsonc',
  },
  'YAML': {
    label: 'YAML',
    forms: [{ open: '#' }],
  },
  'TOML': {
    label: 'TOML',
    forms: [{ open: '#' }],
    streamAdapter: 'toml',
  },
  'XML': {
    label: 'XML',
    forms: [{ open: '<!--', close: '-->', doc: true }],
  },
  'HTML': {
    label: 'HTML',
    forms: [{ open: '<!--', close: '-->', doc: true }],
    embedded: true,
  },
  'CSS': {
    label: 'CSS',
    forms: [{ open: '/*', close: '*/' }],
  },
  'SCSS': {
    label: 'SCSS',
    forms: [{ open: '//' }, { open: '/*', close: '*/' }],
    streamAdapter: 'scss',
  },
  'Markdown': {
    label: 'Markdown',
    forms: [],
  },
  'SQL': {
    label: 'SQL',
    forms: [{ open: '--' }, { open: '/*', close: '*/' }],
  },
  'Mermaid': {
    label: 'Mermaid',
    forms: [{ open: '%%' }],
    streamAdapter: 'mermaid',
  },
  'Dockerfile': {
    label: 'Dockerfile',
    forms: [{ open: '#' }],
    streamAdapter: 'dockerfile',
  },
  'Plain text': {
    label: 'Plain text',
    forms: [],
  },
};

/** Normalize any alias/dialect spelling to its advertised label. */
export function advertisedLabel(language?: string, options: RegionOptions = {}): string {
  const raw = String(language ?? '').toLowerCase().trim();
  const dialect = String(options.dialect || options.path || '').toLowerCase();
  if (raw === 'json' && (dialect.includes('jsonc') || dialect.endsWith('.jsonc'))) return 'JSON';
  if (ALIAS_TO_LABEL[raw]) return ALIAS_TO_LABEL[raw];
  const collapsed = raw.replace(/[\s_/-]+/g, '');
  for (const key of Object.keys(ALIAS_TO_LABEL)) {
    if (key.replace(/[\s_/-]+/g, '') === collapsed) return ALIAS_TO_LABEL[key];
  }
  return String(language || '').trim() || 'Plain text';
}

/** Dialect under a shared display label. `.jsonc` is comment-capable JSON. */
export function dialectForPath(path?: string, language?: string): string {
  const raw = String(path || '').toLowerCase().replace(/\\/g, '/');
  const leaf = raw.split('/').pop() || '';
  if (FILENAME_TO_DIALECT[leaf]) return FILENAME_TO_DIALECT[leaf];
  const ext = leaf.includes('.') ? leaf.split('.').pop() || '' : '';
  if (EXTENSION_TO_DIALECT[ext]) return EXTENSION_TO_DIALECT[ext];
  const lang = String(language || advertisedLabel(leaf)).toLowerCase();
  if (lang.includes('jsonc')) return 'jsonc';
  return '';
}

/** True when this label's documentation regions project rich Markdown. */
export function supportsRichComments(language?: string, options: RegionOptions = {}): boolean {
  const label = advertisedLabel(language, options);
  const spec = LANGUAGE_SPECS[label];
  if (!spec) return false;
  if (label === 'Markdown' || label === 'Plain text') return false;
  if (label === 'JSON') {
    const dialect = String(options.dialect || options.path || '').toLowerCase();
    return dialect.includes('jsonc') || /(?:^|[/\\.])jsonc$/i.test(String(options.path || ''));
  }
  return spec.forms.length > 0 || spec.docstrings === true;
}

/** Full advertised matrix, including non-comment applicable behavior rows. */
export function languageMatrix(): Array<{ label: string; rich: boolean; behavior: string }> {
  return ADVERTISED_LANGUAGE_LABELS.map((label) => {
    const spec = LANGUAGE_SPECS[label];
    if (label === 'Markdown') return { label, rich: false, behavior: 'full-document-rich-preview' };
    if (label === 'Plain text') return { label, rich: false, behavior: 'plain-editing-no-invented-comments' };
    if (label === 'JSON') return { label, rich: false, behavior: 'strict-json-no-comments-jsonc-rich' };
    return { label, rich: supportsRichComments(label), behavior: spec?.docstrings ? 'comments-and-docstrings' : 'comments' };
  });
}

export function languageSpec(language?: string, options: RegionOptions = {}): LanguageSpec | null {
  return LANGUAGE_SPECS[advertisedLabel(language, options)] || null;
}

// ---------------------------------------------------------------------------
// Shared region construction
// ---------------------------------------------------------------------------

function stripLineMarker(line: string, open: string, doc: boolean): { skip: number } {
  // `line` already starts AFTER the open delimiter (contentFrom). Strip one
  // optional space so `// Hello` projects `Hello` while `#Heading` keeps `#`.
  let skip = 0;
  if (line[skip] === ' ' || line[skip] === '\t') skip += 1;
  return { skip };
}

function stripBlockContinuation(line: string, doc: boolean): number {
  // Block-doc continuation lines commonly look like ` * text` or `* text`.
  // Strip the leading whitespace + `*` + one optional space, but never swallow
  // a meaningful Markdown `*` list bullet without that documentation star.
  const match = /^([ \t]*\*)([ \t]?)/.exec(line);
  if (match) return match[0].length;
  return 0;
}

function buildRegion(
  source: string,
  from: number,
  to: number,
  open: string,
  close: string | undefined,
  delimiterKind: DelimiterKind,
  kind: 'comment' | 'docstring',
  language: string,
  lineStrip: (line: string, at: number) => number,
): DocumentationRegion {
  let contentFrom = from + open.length;
  let contentTo = close ? to - close.length : to;
  if (contentTo < contentFrom) contentTo = contentFrom;
  const contentRanges: ContentSpan[] = [];
  let cursor = contentFrom;
  const isBlock = !!close;
  while (cursor <= contentTo) {
    const newline = source.indexOf('\n', cursor);
    const rawEnd = newline < 0 || newline >= contentTo ? contentTo : newline;
    // CRLF files: the `\r` belongs to the source bytes but not to Markdown.
    const end = rawEnd > cursor && source[rawEnd - 1] === '\r' ? rawEnd - 1 : rawEnd;
    let lineFrom = cursor;
    const line = source.slice(lineFrom, end);
    const strip = lineStrip(line, lineFrom);
    lineFrom += strip;
    if (lineFrom <= end) contentRanges.push({ from: lineFrom, to: end });
    if (newline < 0 || newline >= contentTo) break;
    cursor = newline + 1;
  }
  const lineStart = source.lastIndexOf('\n', from - 1) + 1;
  const nextBreak = source.indexOf('\n', to);
  const lineEnd = nextBreak < 0 ? source.length : nextBreak;
  return {
    from, to, contentFrom, contentTo, contentRanges,
    language, delimiterKind, open, close, kind, lineStart, lineEnd,
    ...(isBlock ? {} : {}),
  };
}

function groupAdjacentLineRegions(source: string, regions: DocumentationRegion[], language: string, forms: CommentForm[]): DocumentationRegion[] {
  const grouped: DocumentationRegion[] = [];
  for (const current of regions) {
    const previous = grouped.at(-1);
    if (!previous) { grouped.push(current); continue; }
    const previousSource = source.slice(previous.from, previous.to);
    const currentSource = source.slice(current.from, current.to);
    const previousDelimiter = forms.find((item) => !item.close && previousSource.startsWith(item.open));
    const currentDelimiter = forms.find((item) => !item.close && currentSource.startsWith(item.open));
    const gap = source.slice(previous.to, current.from);
    if (previousDelimiter?.open === currentDelimiter?.open && /^(?:\r?\n)[ \t]*$/.test(gap)) {
      previous.to = current.to;
      previous.contentTo = current.contentTo;
      previous.lineEnd = current.lineEnd;
      previous.contentRanges.push(...current.contentRanges);
    } else grouped.push(current);
  }
  return grouped;
}

// ---------------------------------------------------------------------------
// String/comment scanners for stream-adapter languages and lookalike exclusion
// ---------------------------------------------------------------------------

interface ScanContext {
  source: string;
  index: number;
  regions: DocumentationRegion[];
  /** Incomplete heredoc/raw tag waiting for a terminator on a later line. */
  pendingHeredoc: { tag: string; indented: boolean; raw: boolean } | null;
}

type StringHandler = (ctx: ScanContext) => void;

function matchAt(source: string, index: number, needle: string): boolean {
  return source.startsWith(needle, index);
}

function sourceIndexOfNewline(source: string, from: number, limit: number): number {
  const index = source.indexOf('\n', from);
  return index < 0 || index >= limit ? -1 : index;
}

function scanLineString(ctx: ScanContext, quote: string, escapes = true): void {
  // Consume a single-line quoted string starting at ctx.index (on the quote).
  ctx.index += 1;
  while (ctx.index < ctx.source.length) {
    const ch = ctx.source[ctx.index];
    if (ch === '\\' && escapes) { ctx.index += 2; continue; }
    if (ch === quote) { ctx.index += 1; return; }
    if (ch === '\n') return;
    ctx.index += 1;
  }
}

function scanTripleString(ctx: ScanContext, quote: string, raw = false): void {
  const delim = quote.repeat(3);
  if (!matchAt(ctx.source, ctx.index, delim)) return;
  ctx.index += 3;
  while (ctx.index < ctx.source.length) {
    if (!raw && ctx.source[ctx.index] === '\\') { ctx.index += 2; continue; }
    if (matchAt(ctx.source, ctx.index, delim)) { ctx.index += 3; return; }
    ctx.index += 1;
  }
}

function scanMultilineRaw(ctx: ScanContext, hashes: number): void {
  // Swift raw strings: #"..."#, ##"..."##. Count of `#` must match.
  const hashesStr = '#'.repeat(hashes);
  const open = `${hashesStr}"`;
  if (!matchAt(ctx.source, ctx.index, open)) return;
  ctx.index += open.length;
  const close = `"${hashesStr}`;
  while (ctx.index < ctx.source.length) {
    if (matchAt(ctx.source, ctx.index, close)) { ctx.index += close.length; return; }
    ctx.index += 1;
  }
}

function lineIndentAndText(source: string, pos: number): { indent: string; text: string; lineStart: number; lineEnd: number } {
  const lineStart = source.lastIndexOf('\n', pos - 1) + 1;
  const lineEnd = source.indexOf('\n', pos);
  const end = lineEnd < 0 ? source.length : lineEnd;
  const text = source.slice(lineStart, end);
  const indent = /^[ \t]*/.exec(text)?.[0] || '';
  return { indent, text, lineStart, lineEnd: end };
}

function pushLineRegion(ctx: ScanContext, from: number, to: number, open: string, form: CommentForm, language: string): void {
  const kind: DelimiterKind = form.doc ? 'doc-line' : (language === 'Mermaid' ? 'mermaid-comment' : 'line');
  const region = buildRegion(ctx.source, from, to, open, undefined, kind, 'comment', language, (line) => stripLineMarker(line, open, !!form.doc).skip);
  ctx.regions.push(region);
}

function pushBlockRegion(ctx: ScanContext, from: number, to: number, open: string, close: string, form: CommentForm, language: string): void {
  const kind: DelimiterKind = form.doc ? 'doc-block' : 'block';
  const region = buildRegion(ctx.source, from, to, open, close, kind, 'comment', language, (line, at) => {
    if (at === from + open.length) return 0;
    return stripBlockContinuation(line, !!form.doc);
  });
  ctx.regions.push(region);
}

function scanFormsBlock(ctx: ScanContext, forms: CommentForm[], language: string): boolean {
  const sorted = [...forms].filter((f) => f.close).sort((a, b) => b.open.length - a.open.length);
  for (const form of sorted) {
    if (!matchAt(ctx.source, ctx.index, form.open)) continue;
    const from = ctx.index;
    ctx.index += form.open.length;
    if (form.nested) {
      let depth = 1;
      while (ctx.index < ctx.source.length && depth > 0) {
        if (matchAt(ctx.source, ctx.index, form.open)) { depth += 1; ctx.index += form.open.length; continue; }
        if (matchAt(ctx.source, ctx.index, form.close!)) { depth -= 1; ctx.index += form.close!.length; continue; }
        ctx.index += 1;
      }
    } else {
      const closeIndex = ctx.source.indexOf(form.close!, ctx.index);
      ctx.index = closeIndex < 0 ? ctx.source.length : closeIndex + form.close!.length;
    }
    pushBlockRegion(ctx, from, ctx.index, form.open, ctx.source.slice(from + form.open.length, ctx.index).endsWith(form.close!) ? form.close! : form.close!, form, language);
    return true;
  }
  return false;
}

function scanFormsLine(ctx: ScanContext, forms: CommentForm[], language: string, validStart: (ctx: ScanContext) => boolean): boolean {
  const sorted = [...forms].filter((f) => !f.close).sort((a, b) => b.open.length - a.open.length);
  for (const form of sorted) {
    if (!matchAt(ctx.source, ctx.index, form.open)) continue;
    if (!validStart(ctx)) return false;
    const from = ctx.index;
    const newline = ctx.source.indexOf('\n', ctx.index);
    const to = newline < 0 ? ctx.source.length : newline;
    ctx.index = to;
    pushLineRegion(ctx, from, to, form.open, form, language);
    return true;
  }
  return false;
}

/**
 * Generic stateful scanner shared by C-family, Java-family and similar rows.
 * Carries string state through characters and merges only spans inside one
 * comment. Never matches comment delimiters inside strings or lookalikes.
 */
function scanGeneric(ctx: ScanContext, forms: CommentForm[], language: string, options: {
  nestedBlock?: boolean;
  lineValid?: (ctx: ScanContext) => boolean;
  rawTripleQuote?: string | null;
  verbatimAt?: boolean;
  hashLine?: boolean;
  htmlComment?: boolean;
} = {}): void {
  const lineValid = options.lineValid || (() => true);
  while (ctx.index < ctx.source.length) {
    const ch = ctx.source[ctx.index];
    if (ch === '\n') { ctx.index += 1; continue; }
    if (options.htmlComment && matchAt(ctx.source, ctx.index, '<!--')) {
      const from = ctx.index;
      const close = ctx.source.indexOf('-->', ctx.index + 4);
      ctx.index = close < 0 ? ctx.source.length : close + 3;
      const form = { open: '<!--', close: '-->', doc: true };
      pushBlockRegion(ctx, from, ctx.index, '<!--', '-->', form, language);
      continue;
    }
    if (options.verbatimAt && ch === '@' && ctx.source[ctx.index + 1] === '"') {
      // C# verbatim string: "" escapes a quote; no backslash escapes.
      ctx.index += 2;
      while (ctx.index < ctx.source.length) {
        if (ctx.source[ctx.index] === '"') {
          if (ctx.source[ctx.index + 1] === '"') { ctx.index += 2; continue; }
          ctx.index += 1; break;
        }
        ctx.index += 1;
      }
      continue;
    }
    if (options.rawTripleQuote && matchAt(ctx.source, ctx.index, options.rawTripleQuote)) {
      scanTripleString(ctx, options.rawTripleQuote[0], true);
      continue;
    }
    if (ch === '"' || ch === "'") {
      // Detect triple quotes first (Python/Rotlin/Swift/C# raw strings).
      const triple = ch + ch + ch;
      if (matchAt(ctx.source, ctx.index, triple)) { scanTripleString(ctx, ch, false); continue; }
      scanLineString(ctx, ch, true);
      continue;
    }
    if (ch === '`') {
      // JS template literals; skip interpolation-inside strings loosely.
      ctx.index += 1;
      while (ctx.index < ctx.source.length) {
        if (ctx.source[ctx.index] === '\\') { ctx.index += 2; continue; }
        if (ctx.source[ctx.index] === '`') { ctx.index += 1; break; }
        if (ctx.source[ctx.index] === '\n') break;
        ctx.index += 1;
      }
      continue;
    }
    if (scanFormsBlock(ctx, forms, language)) continue;
    if (scanFormsLine(ctx, forms, language, lineValid)) continue;
    ctx.index += 1;
  }
}

// ---------------------------------------------------------------------------
// Language-specific stream adapters (the eight legacy/local rows + JSONC/SCSS)
// ---------------------------------------------------------------------------

function scanKotlin(ctx: ScanContext, forms: CommentForm[]): void {
  while (ctx.index < ctx.source.length) {
    const ch = ctx.source[ctx.index];
    if (ch === '\n') { ctx.index += 1; continue; }
    if (matchAt(ctx.source, ctx.index, '"""')) { scanTripleString(ctx, '"', true); continue; }
    if (ch === '"' || ch === "'") { scanLineString(ctx, ch, true); continue; }
    if (scanFormsBlock(ctx, forms, 'Kotlin')) continue;
    if (scanFormsLine(ctx, forms, 'Kotlin', () => true)) continue;
    ctx.index += 1;
  }
}

function scanCsharp(ctx: ScanContext, forms: CommentForm[]): void {
  while (ctx.index < ctx.source.length) {
    const ch = ctx.source[ctx.index];
    if (ch === '\n') { ctx.index += 1; continue; }
    // C# 11 raw strings: """...""" and $"""...""".
    const rawMatch = /^(\$?)("""+)/.exec(ctx.source.slice(ctx.index, ctx.index + 8));
    if (rawMatch) {
      const hashes = rawMatch[2].length;
      const delim = '"'.repeat(hashes);
      ctx.index += rawMatch[1].length + hashes;
      const close = ctx.source.indexOf(delim, ctx.index);
      ctx.index = close < 0 ? ctx.source.length : close + hashes;
      continue;
    }
    if (ch === '@' && ctx.source[ctx.index + 1] === '"') {
      ctx.index += 2;
      while (ctx.index < ctx.source.length) {
        if (ctx.source[ctx.index] === '"') {
          if (ctx.source[ctx.index + 1] === '"') { ctx.index += 2; continue; }
          ctx.index += 1; break;
        }
        ctx.index += 1;
      }
      continue;
    }
    if (ch === '"') {
      // Interpolated $"..." still ends at the closing quote; braces inside stay data.
      if (ctx.source[ctx.index - 1] === '$' || (ctx.index > 0 && ctx.source[ctx.index - 1] === '@' && ctx.source[ctx.index - 2] === '$')) {
        // fall through to normal string scan
      }
      scanLineString(ctx, ch, true);
      continue;
    }
    if (ch === "'") { scanLineString(ctx, ch, true); continue; }
    if (scanFormsBlock(ctx, forms, 'C#')) continue;
    if (scanFormsLine(ctx, forms, 'C#', () => true)) continue;
    ctx.index += 1;
  }
}

function scanSwift(ctx: ScanContext, forms: CommentForm[]): void {
  while (ctx.index < ctx.source.length) {
    const ch = ctx.source[ctx.index];
    if (ch === '\n') { ctx.index += 1; continue; }
    // Raw strings: #", ##", ###" with matching closing hashes.
    if (ch === '#') {
      const hashes = /^(#+)/.exec(ctx.source.slice(ctx.index))?.[1].length || 0;
      if (hashes && ctx.source[ctx.index + hashes] === '"') {
        scanMultilineRaw(ctx, hashes);
        continue;
      }
    }
    if (matchAt(ctx.source, ctx.index, '"""')) { scanTripleString(ctx, '"', false); continue; }
    if (ch === '"' || ch === "'") { scanLineString(ctx, ch, true); continue; }
    if (scanFormsBlock(ctx, forms, 'Swift')) continue;
    if (scanFormsLine(ctx, forms, 'Swift', () => true)) continue;
    ctx.index += 1;
  }
}

function scanRuby(ctx: ScanContext, forms: CommentForm[]): void {
  const heredoc = /<<[-~]?(['"]?)([A-Za-z_][A-Za-z0-9_]*)\1/g;
  while (ctx.index < ctx.source.length) {
    const ch = ctx.source[ctx.index];
    if (ch === '\n') { ctx.index += 1; continue; }
    // Column-sensitive =begin/=end documentation blocks (column 0 only).
    if (ctx.index === 0 || ctx.source[ctx.index - 1] === '\n') {
      if (matchAt(ctx.source, ctx.index, '=begin')) {
        const from = ctx.index;
        const endMatch = /\n=end\b[^\n]*/.exec(ctx.source.slice(ctx.index));
        const to = endMatch ? ctx.index + endMatch.index + endMatch[0].length : ctx.source.length;
        ctx.index = to;
        // `=begin` / `=end` are whole-line markers. Content is the lines
        // between them; the `=end` line itself is not Markdown.
        const contentFrom = from + '=begin'.length;
        const endLineStart = endMatch ? from + endMatch.index + 1 : to;
        const contentTo = endMatch ? endLineStart : to;
        const contentRanges: ContentSpan[] = [];
        let cursor = contentFrom;
        while (cursor < contentTo) {
          const newline = sourceIndexOfNewline(ctx.source, cursor, contentTo);
          const end = newline < 0 ? contentTo : newline;
          let lineFrom = cursor;
          if (lineFrom < end && (ctx.source[lineFrom] === ' ' || ctx.source[lineFrom] === '\t')) lineFrom += 1;
          if (lineFrom <= end) contentRanges.push({ from: lineFrom, to: end });
          if (newline < 0) break;
          cursor = newline + 1;
        }
        const lineStart = ctx.source.lastIndexOf('\n', from - 1) + 1;
        const lineEnd = (() => { const n = ctx.source.indexOf('\n', to); return n < 0 ? ctx.source.length : n; })();
        ctx.regions.push({
          from, to, contentFrom, contentTo, contentRanges,
          language: 'Ruby', delimiterKind: 'doc-block',
          open: '=begin', close: '=end', kind: 'comment',
          lineStart, lineEnd,
        });
        continue;
      }
    }
    // Percent strings: %q{}, %Q{}, %w[], %i[], %r{}, %s().
    if (ch === '%' && /[qQwWiIrsx]/.test(ctx.source[ctx.index + 1] || '') && /[[({<|!]/.test(ctx.source[ctx.index + 2] || '')) {
      const kind = ctx.source[ctx.index + 1];
      const open = ctx.source[ctx.index + 2];
      const close = { '[': ']', '{': '}', '(': ')', '<': '>', '|': '|', '!': '!', '#': '#' }[open as string] || open;
      ctx.index += 3;
      let depth = 1;
      while (ctx.index < ctx.source.length && depth > 0) {
        if (ctx.source[ctx.index] === '\\') { ctx.index += 2; continue; }
        if (ctx.source[ctx.index] === open) depth += 1;
        else if (ctx.source[ctx.index] === close) depth -= 1;
        ctx.index += 1;
      }
      continue;
    }
    // Heredocs: <<TAG, <<'TAG', <<"TAG", <<-TAG, <<~TAG
    if (ch === '<' && ctx.source[ctx.index + 1] === '<') {
      heredoc.lastIndex = ctx.index;
      const m = heredoc.exec(ctx.source);
      if (m && m.index === ctx.index) {
        const tag = m[2];
        const indented = /<<[-~]/.test(m[0]);
        const lineEnd = ctx.source.indexOf('\n', ctx.index);
        ctx.index = lineEnd < 0 ? ctx.source.length : lineEnd;
        ctx.pendingHeredoc = { tag, indented, raw: m[1] === "'" };
        continue;
      }
    }
    if (ch === '"' || ch === "'") {
      // Interpolation-aware single-line / triple strings.
      const triple = ch + ch + ch;
      if (matchAt(ctx.source, ctx.index, triple)) { scanTripleString(ctx, ch, false); continue; }
      scanLineString(ctx, ch, true);
      continue;
    }
    if (ch === '/' && ctx.index > 0 && /[(=,[!|&?:{;\s]/.test(ctx.source[ctx.index - 1] || ' ')) {
      // Regex literal (not division). Skip to unescaped closing slash.
      ctx.index += 1;
      while (ctx.index < ctx.source.length) {
        if (ctx.source[ctx.index] === '\\') { ctx.index += 2; continue; }
        if (ctx.source[ctx.index] === '/') { ctx.index += 1; break; }
        if (ctx.source[ctx.index] === '\n') break;
        ctx.index += 1;
      }
      continue;
    }
    if (scanFormsLine(ctx, forms, 'Ruby', (c) => {
      // `#` inside a word (foo#bar) is not a comment start.
      const prev = c.source[c.index - 1];
      return !prev || /[\s([{,;:]/.test(prev);
    })) continue;
    ctx.index += 1;
  }
}

function scanShell(ctx: ScanContext, forms: CommentForm[]): void {
  while (ctx.index < ctx.source.length) {
    const ch = ctx.source[ctx.index];
    if (ch === '\n') {
      ctx.index += 1;
      // Consume heredoc bodies as data, never as comments.
      if (ctx.pendingHeredoc) {
        const { tag, indented } = ctx.pendingHeredoc;
        while (ctx.index < ctx.source.length) {
          const lineStart = ctx.index;
          const lineEnd = ctx.source.indexOf('\n', ctx.index);
          const end = lineEnd < 0 ? ctx.source.length : lineEnd;
          const line = ctx.source.slice(lineStart, end);
          const trimmed = indented ? line.trim() : line;
          if (trimmed === tag) { ctx.index = end; break; }
          ctx.index = end;
          if (lineEnd < 0) break;
          ctx.index = lineEnd + 1;
        }
        ctx.pendingHeredoc = null;
      }
      continue;
    }
    if (ch === '#') {
      // `#` starts a comment only at a valid shell word boundary.
      const prev = ctx.source[ctx.index - 1];
      if (!prev || /[\s;|&()<>]/.test(prev)) {
        const from = ctx.index;
        const newline = ctx.source.indexOf('\n', ctx.index);
        const to = newline < 0 ? ctx.source.length : newline;
        ctx.index = to;
        pushLineRegion(ctx, from, to, '#', { open: '#' }, 'Shell');
        continue;
      }
      ctx.index += 1;
      continue;
    }
    if (ch === "'" ) { scanLineString(ctx, ch, false); continue; }
    if (ch === '"') { scanLineString(ctx, ch, true); continue; }
    if (ch === '`') {
      ctx.index += 1;
      while (ctx.index < ctx.source.length && ctx.source[ctx.index] !== '`') {
        if (ctx.source[ctx.index] === '\\') ctx.index += 1;
        ctx.index += 1;
      }
      ctx.index += 1;
      continue;
    }
    if (ch === '<' && ctx.source[ctx.index + 1] === '<') {
      const m = /^(<<-?)(['"]?)([A-Za-z_][A-Za-z0-9_]*)\2/.exec(ctx.source.slice(ctx.index));
      if (m) {
        ctx.pendingHeredoc = { tag: m[3], indented: /<<-/.test(m[1]), raw: m[2] === "'" };
        ctx.index += m[0].length;
        continue;
      }
    }
    ctx.index += 1;
  }
}

function scanDockerfile(ctx: ScanContext, forms: CommentForm[]): void {
  let sawContent = false;
  while (ctx.index < ctx.source.length) {
    const ch = ctx.source[ctx.index];
    if (ch === '\n') { ctx.index += 1; continue; }
    if (ch === '#') {
      const atLineStart = ctx.index === 0 || ctx.source[ctx.index - 1] === '\n' || /^[ \t]*$/.test(ctx.source.slice(ctx.source.lastIndexOf('\n', ctx.index - 1) + 1, ctx.index));
      if (atLineStart) {
        const from = ctx.index;
        const newline = ctx.source.indexOf('\n', ctx.index);
        const to = newline < 0 ? ctx.source.length : newline;
        const lineText = ctx.source.slice(from, to);
        // Parser directives (`# syntax=`, `# escape=`) are metadata, not prose.
        const isDirective = !sawContent && /^#\s*(?:syntax|escape)\s*=/.test(lineText);
        ctx.index = to;
        if (!isDirective) pushLineRegion(ctx, from, to, '#', { open: '#' }, 'Dockerfile');
        continue;
      }
    }
    if (!/\s/.test(ch)) sawContent = true;
    // Quoted strings in RUN/ENV etc.; heredoc bodies preserved as data.
    if (ch === '"' || ch === "'") { scanLineString(ctx, ch, true); continue; }
    if (ch === '<' && ctx.source[ctx.index + 1] === '<') {
      const m = /^(<<-?)(['"]?)([A-Za-z_][A-Za-z0-9_]*)\2/.exec(ctx.source.slice(ctx.index));
      if (m) {
        const tag = m[3];
        const indented = /<<-/.test(m[1]);
        const lineEnd = ctx.source.indexOf('\n', ctx.index);
        let cursor = lineEnd < 0 ? ctx.source.length : lineEnd + 1;
        while (cursor < ctx.source.length) {
          const end = ctx.source.indexOf('\n', cursor);
          const line = ctx.source.slice(cursor, end < 0 ? ctx.source.length : end);
          if ((indented ? line.trim() : line) === tag) { cursor = end < 0 ? ctx.source.length : end; break; }
          if (end < 0) break;
          cursor = end + 1;
        }
        ctx.index = cursor;
        continue;
      }
    }
    ctx.index += 1;
  }
}

function scanToml(ctx: ScanContext, forms: CommentForm[]): void {
  while (ctx.index < ctx.source.length) {
    const ch = ctx.source[ctx.index];
    if (ch === '\n') { ctx.index += 1; continue; }
    if (matchAt(ctx.source, ctx.index, '"""')) { scanTripleString(ctx, '"', false); continue; }
    if (matchAt(ctx.source, ctx.index, "'''")) { scanTripleString(ctx, "'", false); continue; }
    if (ch === '"' || ch === "'") { scanLineString(ctx, ch, true); continue; }
    if (scanFormsLine(ctx, forms, 'TOML', () => true)) continue;
    ctx.index += 1;
  }
}

function scanMermaid(ctx: ScanContext, forms: CommentForm[]): void {
  while (ctx.index < ctx.source.length) {
    const ch = ctx.source[ctx.index];
    if (ch === '\n') { ctx.index += 1; continue; }
    if (ch === '"' || ch === "'") { scanLineString(ctx, ch, true); continue; }
    if (matchAt(ctx.source, ctx.index, '%%{')) {
      // AccTitle/accDescr/directives `%%{...}%%` are metadata, not prose.
      const close = ctx.source.indexOf('}%%', ctx.index);
      ctx.index = close < 0 ? ctx.source.length : close + 3;
      continue;
    }
    if (matchAt(ctx.source, ctx.index, '%%')) {
      const from = ctx.index;
      const newline = ctx.source.indexOf('\n', ctx.index);
      const to = newline < 0 ? ctx.source.length : newline;
      ctx.index = to;
      pushLineRegion(ctx, from, to, '%%', { open: '%%' }, 'Mermaid');
      continue;
    }
    ctx.index += 1;
  }
}

function scanScss(ctx: ScanContext, forms: CommentForm[]): void {
  while (ctx.index < ctx.source.length) {
    const ch = ctx.source[ctx.index];
    if (ch === '\n') { ctx.index += 1; continue; }
    if (ch === '"' || ch === "'") { scanLineString(ctx, ch, true); continue; }
    if (ch === '/' && ctx.source[ctx.index + 1] === '/') {
      const from = ctx.index;
      const newline = ctx.source.indexOf('\n', ctx.index);
      const to = newline < 0 ? ctx.source.length : newline;
      ctx.index = to;
      pushLineRegion(ctx, from, to, '//', { open: '//' }, 'SCSS');
      continue;
    }
    if (scanFormsBlock(ctx, forms, 'SCSS')) continue;
    ctx.index += 1;
  }
}

function scanJsonc(ctx: ScanContext, forms: CommentForm[]): void {
  while (ctx.index < ctx.source.length) {
    const ch = ctx.source[ctx.index];
    if (ch === '\n') { ctx.index += 1; continue; }
    if (ch === '"') { scanLineString(ctx, ch, true); continue; }
    if (scanFormsBlock(ctx, forms, 'JSON')) continue;
    if (scanFormsLine(ctx, forms, 'JSON', () => true)) continue;
    ctx.index += 1;
  }
}

function scanYaml(ctx: ScanContext, forms: CommentForm[]): void {
  while (ctx.index < ctx.source.length) {
    const ch = ctx.source[ctx.index];
    if (ch === '\n') { ctx.index += 1; continue; }
    // Block scalars: | and > with optional indicators; content is data.
    if ((ch === '|' || ch === '>') && /^\s*[|>][-+]?\d*\s*(?:#.*)?$/.test(ctx.source.slice(ctx.index, ctx.source.indexOf('\n', ctx.index) < 0 ? ctx.source.length : ctx.source.indexOf('\n', ctx.index)))) {
      const lineEnd = ctx.source.indexOf('\n', ctx.index);
      let cursor = lineEnd < 0 ? ctx.source.length : lineEnd + 1;
      const indentMatch = /^[ \t]*/.exec(ctx.source.slice(cursor))?.[0].length || 0;
      if (indentMatch > 0) {
        while (cursor < ctx.source.length) {
          const end = ctx.source.indexOf('\n', cursor);
          const line = ctx.source.slice(cursor, end < 0 ? ctx.source.length : end);
          if (line.trim() === '') { if (end < 0) break; cursor = end + 1; continue; }
          const lineIndent = /^[ \t]*/.exec(line)?.[0].length || 0;
          if (lineIndent < indentMatch) break;
          if (end < 0) { cursor = ctx.source.length; break; }
          cursor = end + 1;
        }
        ctx.index = cursor;
        continue;
      }
      ctx.index = lineEnd < 0 ? ctx.source.length : lineEnd + 1;
      continue;
    }
    if (ch === '"' || ch === "'") { scanLineString(ctx, ch, true); continue; }
    if (ch === '#') {
      // `#` inside a plain scalar (key: value # not comment when in quotes handled above).
      // YAML comments require preceding whitespace or line start.
      const prev = ctx.source[ctx.index - 1];
      if (!prev || prev === '\n' || prev === ' ' || prev === '\t') {
        const from = ctx.index;
        const newline = ctx.source.indexOf('\n', ctx.index);
        const to = newline < 0 ? ctx.source.length : newline;
        ctx.index = to;
        pushLineRegion(ctx, from, to, '#', { open: '#' }, 'YAML');
        continue;
      }
    }
    ctx.index += 1;
  }
}

function scanPhpHtml(ctx: ScanContext, forms: CommentForm[]): void {
  // HTML comments plus PHP/JS/CSS comment forms. Nested regions use their own
  // grammar's delimiters; script/style strings stay data.
  while (ctx.index < ctx.source.length) {
    const ch = ctx.source[ctx.index];
    if (ch === '\n') { ctx.index += 1; continue; }
    // Quoted attribute/string values are never comments.
    if (ch === '"' || ch === "'") { scanLineString(ctx, ch, true); continue; }
    if (matchAt(ctx.source, ctx.index, '<!--')) {
      const from = ctx.index;
      const close = ctx.source.indexOf('-->', ctx.index + 4);
      ctx.index = close < 0 ? ctx.source.length : close + 3;
      pushBlockRegion(ctx, from, ctx.index, '<!--', '-->', { open: '<!--', close: '-->', doc: true }, 'HTML');
      continue;
    }
    if (matchAt(ctx.source, ctx.index, '<script') || matchAt(ctx.source, ctx.index, '<style')) {
      const tag = matchAt(ctx.source, ctx.index, '<script') ? 'script' : 'style';
      const openEnd = ctx.source.indexOf('>', ctx.index);
      if (openEnd < 0) { ctx.index += 1; continue; }
      ctx.index = openEnd + 1;
      const closeTag = `</${tag}`;
      const closeIndex = ctx.source.toLowerCase().indexOf(closeTag, ctx.index);
      const regionEnd = closeIndex < 0 ? ctx.source.length : closeIndex;
      // Scan embedded JS/CSS comments as regions with their own delimiters.
      const embeddedLanguage = tag === 'script' ? 'JavaScript' : 'CSS';
      const embeddedForms = LANGUAGE_SPECS[embeddedLanguage].forms;
      const sub: ScanContext = { source: ctx.source, index: ctx.index, regions: ctx.regions, pendingHeredoc: null };
      if (tag === 'script') scanGeneric(sub, embeddedForms, embeddedLanguage, {});
      else scanGeneric(sub, embeddedForms, embeddedLanguage, {});
      ctx.index = regionEnd;
      continue;
    }
    if (ch === '<' && ctx.source[ctx.index + 1] === '?') {
      // PHP region: use PHP comment forms inside.
      const close = ctx.source.indexOf('?>', ctx.index);
      const regionEnd = close < 0 ? ctx.source.length : close;
      const sub: ScanContext = { source: ctx.source, index: ctx.index + 2, regions: ctx.regions, pendingHeredoc: null };
      scanGeneric(sub, forms, 'PHP', {});
      ctx.index = regionEnd;
      continue;
    }
    if (scanFormsBlock(ctx, forms, 'PHP')) continue;
    if (scanFormsLine(ctx, forms, 'PHP', (c) => {
      const prev = c.source[c.index - 1];
      return !prev || /[\s;({,]/.test(prev);
    })) continue;
    ctx.index += 1;
  }
}

// ---------------------------------------------------------------------------
// Lezer-backed extractors (structure-aware comment nodes + Python docstrings)
// ---------------------------------------------------------------------------

type LezerNodeLike = { name: string; from: number; to: number; get?: (name: string) => any; firstChild?: any; nextSibling?: any; parent?: any };

interface LezerParserLike {
  parse(input: string): { iterate(spec: { enter(node: { name: string; from: number; to: number }): void }): void };
}

const lezerParserCache = new Map<string, Promise<LezerParserLike | null>>();

function loadLezerParser(language: string): Promise<LezerParserLike | null> {
  const key = advertisedLabel(language);
  const cached = lezerParserCache.get(key);
  if (cached) return cached;
  const pending = (async (): Promise<LezerParserLike | null> => {
    try {
      switch (key) {
        case 'JavaScript': case 'JavaScript JSX': case 'TypeScript': case 'TypeScript JSX': {
          const mod = await import('@lezer/javascript');
          return (mod as any).parser;
        }
        case 'Python': {
          const mod = await import('@lezer/python');
          return (mod as any).parser;
        }
        case 'C': case 'C++': case 'C/C++ header': case 'C++ header': {
          const mod = await import('@lezer/cpp');
          return (mod as any).parser;
        }
        case 'Java': {
          const mod = await import('@lezer/java');
          return (mod as any).parser;
        }
        case 'Go': {
          const mod = await import('@lezer/go');
          return (mod as any).parser;
        }
        case 'Rust': {
          const mod = await import('@lezer/rust');
          return (mod as any).parser;
        }
        case 'JSON': {
          const mod = await import('@lezer/json');
          return (mod as any).parser;
        }
        case 'YAML': {
          const mod = await import('@lezer/yaml');
          return (mod as any).parser;
        }
        case 'XML': {
          const mod = await import('@lezer/xml');
          return (mod as any).parser;
        }
        case 'HTML': {
          const mod = await import('@lezer/html');
          return (mod as any).parser;
        }
        case 'CSS': {
          const mod = await import('@lezer/css');
          return (mod as any).parser;
        }
        case 'PHP': {
          const mod = await import('@lezer/php');
          return (mod as any).parser;
        }
        default:
          return null;
      }
    } catch {
      return null;
    }
  })();
  lezerParserCache.set(key, pending);
  pending.catch(() => lezerParserCache.delete(key));
  return pending;
}

function commentNodeName(name: string): boolean {
  return /comment/i.test(name);
}

/** Map a Lezer comment node into a DocumentationRegion using the language forms. */
function regionFromLezerComment(source: string, from: number, to: number, language: string, forms: CommentForm[]): DocumentationRegion | null {
  const raw = source.slice(from, to);
  const sorted = [...forms].sort((a, b) => b.open.length - a.open.length);
  const form = sorted.find((f) => raw.startsWith(f.open));
  if (!form) return null;
  const delimiterKind: DelimiterKind = form.doc
    ? (form.close ? 'doc-block' : 'doc-line')
    : (form.close ? 'block' : (language === 'HTML' || language === 'XML' ? 'html-comment' : 'line'));
  return buildRegion(source, from, to, form.open, form.close, delimiterKind, 'comment', language, (line, at) => {
    if (form.close) {
      if (at === from + form.open.length) return 0;
      return stripBlockContinuation(line, !!form.doc);
    }
    return stripLineMarker(line, form.open, !!form.doc).skip;
  });
}

function lezerCommentRegions(source: string, language: string, forms: CommentForm[], parser: LezerParserLike): DocumentationRegion[] {
  const ranges: Array<{ from: number; to: number }> = [];
  const tree = parser.parse(source);
  tree.iterate({
    enter(node) {
      if (commentNodeName(node.name) && node.from < node.to) ranges.push({ from: node.from, to: node.to });
    },
  });
  ranges.sort((a, b) => a.from - b.from || a.to - b.to);
  const outer = ranges.filter((range) => !ranges.some((other) => other !== range && other.from <= range.from && other.to >= range.to));
  const mapped = outer.map((range) => regionFromLezerComment(source, range.from, range.to, language, forms)).filter(Boolean) as DocumentationRegion[];
  return groupAdjacentLineRegions(source, mapped, language, forms);
}

// --- Python docstrings (PEP 257 structural recognition) --------------------

const PY_STRING_PREFIX = /^(?:[rRuUfFbB]{0,2})(['"])/;

function pythonStringPrefix(raw: string): string {
  const m = /^([rRuUbBfF]*)/.exec(raw);
  return m ? m[1] : '';
}

function isPythonDocStringLiteral(raw: string): boolean {
  const trimmed = raw.trim();
  const prefix = pythonStringPrefix(trimmed);
  if (/[bB]/.test(prefix) || /[fF]/.test(prefix)) return false;
  return PY_STRING_PREFIX.test(trimmed);
}

function pythonStringInnerBounds(source: string, from: number, to: number): { contentFrom: number; contentTo: number; open: string; close: string } {
  const raw = source.slice(from, to);
  const prefixLen = pythonStringPrefix(raw).length;
  const quoteChar = raw[prefixLen];
  const triple = raw.startsWith(quoteChar.repeat(3), prefixLen);
  const openLen = prefixLen + (triple ? 3 : 1);
  const closeLen = triple ? 3 : 1;
  return {
    contentFrom: from + openLen,
    contentTo: to - closeLen,
    open: raw.slice(0, openLen),
    close: source.slice(to - closeLen, to),
  };
}

function docstringRegionFromLiteral(source: string, from: number, to: number): DocumentationRegion | null {
  const raw = source.slice(from, to);
  if (!isPythonDocStringLiteral(raw)) return null;
  const bounds = pythonStringInnerBounds(source, from, to);
  return buildRegion(source, from, to, bounds.open, bounds.close, 'docstring', 'docstring', 'Python', (line, at) => {
    if (at === bounds.contentFrom) return 0;
    return 0;
  });
}

interface PyNode {
  name: string;
  from: number;
  to: number;
}

function isPythonStatementName(name: string): boolean {
  return /Statement$/.test(name) || name === 'FunctionDefinition' || name === 'ClassDefinition' || name === 'Decorated';
}

function collectPythonDocstrings(source: string, parser: LezerParserLike): DocumentationRegion[] {
  const nodes: PyNode[] = [];
  const stack: Array<{ node: PyNode; firstStatement: PyNode | null }> = [];
  const firstStatements = new Map<PyNode, PyNode | null>();
  parser.parse(source).iterate({
    enter(node) {
      const n: PyNode = { name: node.name, from: node.from, to: node.to };
      nodes.push(n);
      const isContainer = node.name === 'Script' || node.name === 'ClassDefinition' || node.name === 'FunctionDefinition';
      if (isContainer) {
        // A nested def/class is itself the first statement of the enclosing body.
        if (stack.length && node.name !== 'Script') {
          const parent = stack[stack.length - 1];
          if (parent.firstStatement === null && isPythonStatementName(node.name)) {
            parent.firstStatement = n;
            firstStatements.set(parent.node, n);
          }
        }
        const frame = { node: n, firstStatement: null as PyNode | null };
        stack.push(frame);
        firstStatements.set(n, null);
        return;
      }
      // Structural children of a body (parameters, ':' tokens, Body itself)
      // are not statements. The first statement-like node claims the slot.
      if (node.name === 'Body' || node.name === 'ParamList' || node.name === ':' || node.name === 'VariableName'
        || node.name === 'def' || node.name === 'class' || node.name === 'async' || node.name === 'lambda'
        || node.name === '(' || node.name === ')' || node.name === '[' || node.name === ']'
        || node.name === '{' || node.name === '}' || node.name === ',' || node.name === '.'
        || node.name === 'AssignOp' || node.name === '=' || node.name === 'print' || node.name === 'return') return;
      if (stack.length && isPythonStatementName(node.name)) {
        const frame = stack[stack.length - 1];
        if (frame.firstStatement === null) {
          frame.firstStatement = n;
          firstStatements.set(frame.node, n);
        }
      }
    },
    leave(node) {
      if (node.name === 'Script' || node.name === 'ClassDefinition' || node.name === 'FunctionDefinition') stack.pop();
    },
  });

  const regions: DocumentationRegion[] = [];
  for (const [container, first] of firstStatements) {
    if (!first || first.name !== 'ExpressionStatement') continue;
    const region = docstringRegionFromExpression(source, first, nodes);
    if (region) regions.push(region);
  }
  regions.sort((a, b) => a.from - b.from || a.to - b.to);
  return regions;
}

function docstringRegionFromExpression(source: string, expr: PyNode, nodes: PyNode[]): DocumentationRegion | null {
  const inner = nodes.filter((n) => n.from >= expr.from && n.to <= expr.to
    && !(n.name === expr.name && n.from === expr.from && n.to === expr.to));
  const hasBinary = inner.some((n) => /Binary|Call|FormatString|Assignment/i.test(n.name));
  if (hasBinary) return null;
  const strings = inner.filter((n) => n.name === 'String');
  const continued = inner.find((n) => n.name === 'ContinuedString');
  const paren = inner.find((n) => n.name === 'ParenthesizedExpression');
  // Direct single String: `"doc"` or `r"""doc"""`.
  const direct = nodes.find((n) => n.name === 'String' && n.from === expr.from && n.to === expr.to);
  if (direct) return docstringRegionFromLiteral(source, direct.from, direct.to);
  // Parenthesized: `("doc")` or `("a" "b")`.
  if (paren && strings.length) {
    const nonString = inner.filter((n) => n.from >= paren.from && n.to <= paren.to
      && !['String', 'ContinuedString', '(', ')', 'ParenthesizedExpression', '( )'].includes(n.name)
      && !/^\s*$/.test(source.slice(n.from, n.to)));
    // ParenthesizedExpression contains String/ContinuedString children only.
    const contentNodes = strings.filter((s) => s.from >= paren.from && s.to <= paren.to);
    if (!contentNodes.length) return null;
    if (contentNodes.some((s) => !isPythonDocStringLiteral(source.slice(s.from, s.to)))) return null;
    // The whole parenthesized expression is the outer source.
    const firstStr = contentNodes[0];
    const lastStr = contentNodes[contentNodes.length - 1];
    const bounds = pythonStringInnerBounds(source, firstStr.from, firstStr.to);
    const contentRanges: ContentSpan[] = contentNodes.map((s) => {
      const b = pythonStringInnerBounds(source, s.from, s.to);
      return { from: b.contentFrom, to: b.contentTo };
    });
    return {
      from: paren.from, to: paren.to,
      contentFrom: bounds.contentFrom, contentTo: pythonStringInnerBounds(source, lastStr.from, lastStr.to).contentTo,
      contentRanges,
      language: 'Python', delimiterKind: 'docstring', open: source.slice(paren.from, bounds.open.length + firstStr.from - paren.from),
      close: undefined, kind: 'docstring',
      lineStart: source.lastIndexOf('\n', paren.from - 1) + 1,
      lineEnd: (() => { const n = source.indexOf('\n', paren.to); return n < 0 ? source.length : n; })(),
    };
  }
  // Implicitly adjacent strings: `"a" "b"` (ContinuedString).
  if (continued && strings.length >= 1) {
    const parts = strings.filter((s) => s.from >= continued.from && s.to <= continued.to);
    if (!parts.length) return null;
    if (parts.some((s) => !isPythonDocStringLiteral(source.slice(s.from, s.to)))) return null;
    const contentRanges: ContentSpan[] = parts.map((s) => {
      const b = pythonStringInnerBounds(source, s.from, s.to);
      return { from: b.contentFrom, to: b.contentTo };
    });
    const firstB = pythonStringInnerBounds(source, parts[0].from, parts[0].to);
    const lastB = pythonStringInnerBounds(source, parts[parts.length - 1].from, parts[parts.length - 1].to);
    return {
      from: continued.from, to: continued.to,
      contentFrom: firstB.contentFrom, contentTo: lastB.contentTo,
      contentRanges,
      language: 'Python', delimiterKind: 'docstring',
      open: source.slice(continued.from, firstB.open.length + parts[0].from - continued.from),
      close: undefined, kind: 'docstring',
      lineStart: source.lastIndexOf('\n', continued.from - 1) + 1,
      lineEnd: (() => { const n = source.indexOf('\n', continued.to); return n < 0 ? source.length : n; })(),
    };
  }
  return null;
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

function runStreamAdapter(source: string, language: string, forms: CommentForm[]): DocumentationRegion[] {
  const spec = LANGUAGE_SPECS[language];
  const ctx: ScanContext = { source, index: 0, regions: [], pendingHeredoc: null };
  const adapter = spec?.streamAdapter;
  if (adapter === 'kotlin') scanKotlin(ctx, forms);
  else if (adapter === 'csharp') scanCsharp(ctx, forms);
  else if (adapter === 'swift') scanSwift(ctx, forms);
  else if (adapter === 'ruby') scanRuby(ctx, forms);
  else if (adapter === 'shell') scanShell(ctx, forms);
  else if (adapter === 'dockerfile') scanDockerfile(ctx, forms);
  else if (adapter === 'toml') scanToml(ctx, forms);
  else if (adapter === 'mermaid') scanMermaid(ctx, forms);
  else if (adapter === 'scss') scanScss(ctx, forms);
  else if (adapter === 'jsonc') scanJsonc(ctx, forms);
  else if (language === 'YAML') scanYaml(ctx, forms);
  else if (language === 'PHP' || language === 'HTML') scanPhpHtml(ctx, forms);
  else scanGeneric(ctx, forms, language, {});
  return groupAdjacentLineRegions(source, ctx.regions, language, forms);
}

/**
 * Synchronous documentation-region scan. Uses stateful stream adapters
 * (always available). For Lezer languages prefer `documentationRegionsAsync`
 * which folds in grammar comment nodes and Python docstrings.
 */
export function documentationRegions(source: string, language?: string, options: RegionOptions = {}): DocumentationRegion[] {
  const label = advertisedLabel(language, options);
  const spec = LANGUAGE_SPECS[label];
  if (!spec) return [];
  // Strict JSON has no comment syntax; only the JSONC dialect scans comments.
  if (label === 'JSON') {
    const dialect = resolveDialect(options, language);
    if (dialect !== 'jsonc') return [];
  }
  if (label === 'Markdown' || label === 'Plain text') return [];
  return runStreamAdapter(String(source ?? ''), label, spec.forms);
}

function resolveDialect(options: RegionOptions, language?: string): string {
  const rawLanguage = String(language || '').toLowerCase();
  const rawOptions = `${options.dialect || ''} ${options.path || ''}`.toLowerCase();
  const raw = `${rawLanguage} ${rawOptions}`;
  if (raw.includes('jsonc') || raw.includes('json5')) return 'jsonc';
  if (rawOptions.includes('.json') && !rawOptions.includes('jsonc')) return 'json';
  // A bare `JSON` label with no path/dialect means strict JSON.
  if (rawLanguage === 'json' && !rawOptions.trim()) return 'json';
  return raw.trim() ? 'jsonc' : '';
}

/**
 * Grammar-aware scan: Lezer comment nodes + Python docstrings + stream
 * adapters for the eight legacy rows. Callers that only need a quick sync
 * map can use `documentationRegions`.
 */
export async function documentationRegionsAsync(source: string, language?: string, options: RegionOptions = {}): Promise<DocumentationRegion[]> {
  const label = advertisedLabel(language, options);
  const spec = LANGUAGE_SPECS[label];
  if (!spec) return [];
  if (label === 'JSON' && resolveDialect(options, language) !== 'jsonc') return [];
  if (label === 'Markdown' || label === 'Plain text') return [];
  const text = String(source ?? '');
  const streamRegions = runStreamAdapter(text, label, spec.forms);
  const parser = await loadLezerParser(label);
  if (!parser) return streamRegions;
  let regions = streamRegions;
  if (spec.docstrings) {
    const docstrings = collectPythonDocstrings(text, parser);
    // Docstrings are first-class regions; they may sit beside `#` comments.
    regions = [...docstrings, ...regions].sort((a, b) => a.from - b.from || a.to - b.to);
    // Drop any stream region that is fully inside a docstring (should not happen).
    regions = regions.filter((r) => !docstrings.some((d) => r.from >= d.from && r.to <= d.to && r !== d));
    return regions;
  }
  if (label === 'Kotlin' || label === 'C#' || label === 'Ruby' || label === 'Swift'
    || label === 'Shell' || label === 'TOML' || label === 'Mermaid' || label === 'Dockerfile'
    || label === 'SCSS' || label === 'JSON') {
    // Stream adapters are authoritative for these rows; Lezer comment names
    // (when present) must not invent regions the adapter did not bound.
    return regions;
  }
  const lezerRegions = lezerCommentRegions(text, label, spec.forms, parser);
  // Prefer the longer/more precise of the two maps per outer range; a region
  // is kept when both agree on outer bounds, otherwise the Lezer bounds win
  // for parser languages because they never split a token. Regions fully
  // contained in another region (a `//` tail of `///`) are dropped.
  if (!lezerRegions.length) return regions;
  const merged: DocumentationRegion[] = [];
  const used = new Set<DocumentationRegion>();
  for (const lr of lezerRegions) {
    const match = regions.find((r) => !used.has(r) && r.from === lr.from && r.to === lr.to);
    if (match) { used.add(match); merged.push(lr); }
    else merged.push(lr);
  }
  for (const r of regions) {
    if (used.has(r)) continue;
    // Keep stream regions that Lezer missed (JSX `{/* */}` etc.).
    if (!merged.some((m) => m.from <= r.from && m.to >= r.to)) merged.push(r);
  }
  // Drop any region fully covered by a longer region.
  const deduped = merged.filter((region) => !merged.some((other) => other !== region && other.from <= region.from && other.to >= region.to && (other.to - other.from) > (region.to - region.from)));
  deduped.sort((a, b) => a.from - b.from || a.to - b.to);
  return deduped;
}

/**
 * Choose a safe adjacent syntax boundary for inserting a new comment.
 * Never splits literals, tokens, continued directives or heredoc bodies.
 * Strict JSON reports a real format limitation instead of inventing syntax.
 */
export function safeCommentInsertion(source: string, language?: string, offset = 0, options: RegionOptions = {}): SafeInsertionResult {
  const label = advertisedLabel(language, options);
  const spec = LANGUAGE_SPECS[label];
  const text = String(source ?? '');
  const at = Math.max(0, Math.min(Number(offset) || 0, text.length));
  if (!spec) return { ok: false, error: `No comment syntax is known for ${label || 'this language'}.` };
  if (label === 'Plain text') {
    return { ok: false, error: 'Plain text has no programming comment syntax; attach the image as a file link instead.' };
  }
  if (label === 'Markdown') {
    // Markdown has no comments; images embed directly as ordinary Markdown.
    return { ok: true, from: at, to: at, indent: '', text: '', comment: '' };
  }
  if (label === 'JSON') {
    const dialect = resolveDialect(options, language);
    if (dialect !== 'jsonc') {
      return { ok: false, error: 'Strict JSON has no comment syntax. Open a .jsonc file (same JSON label) to attach images inside comments, or link the asset outside the document.' };
    }
  }
  // Inside an existing documentation region: insert at the cursor as-is.
  const regions = documentationRegions(text, language, options);
  const inside = regions.find((r) => at > r.from && at < r.to);
  if (inside) {
    return { ok: true, from: at, to: at, indent: lineIndentAndText(text, at).indent, text: '', comment: '' };
  }
  // Find the nearest valid line boundary that is not inside a string/heredoc.
  const line = lineIndentAndText(text, at);
  const indent = line.indent;
  const form = pickInsertForm(spec.forms, label);
  if (!form) return { ok: false, error: `${label} has no usable comment form for automatic insertion.` };
  // Insert on a fresh line after the current line so we never split tokens.
  const insertAt = line.lineEnd;
  return { ok: true, from: insertAt, to: insertAt, indent, text: '', comment: form.open };
}

function pickInsertForm(forms: CommentForm[], label: string): CommentForm | null {
  if (label === 'Ruby') return { open: '#' };
  if (label === 'Shell' || label === 'Dockerfile' || label === 'YAML' || label === 'TOML' || label === 'Python') return { open: '#' };
  if (label === 'SQL') return { open: '--' };
  if (label === 'XML' || label === 'HTML') return { open: '<!--', close: '-->' };
  if (label === 'CSS') return { open: '/*', close: '*/' };
  if (label === 'Mermaid') return { open: '%%' };
  const line = forms.find((f) => !f.close && f.open !== '///' && f.open !== '//!');
  return line || forms.find((f) => f.close) || null;
}

/**
 * Wrap Markdown as a complete language-appropriate comment, retaining
 * indentation. Prefer line-comment form where legal; use a block comment
 * otherwise. The comment never spans into neighboring code.
 */
export function wrapMarkdownAsComment(markdown: string, language?: string, indent = '', options: RegionOptions = {}): CommentWrapResult {
  const label = advertisedLabel(language, options);
  const spec = LANGUAGE_SPECS[label];
  const body = String(markdown ?? '');
  if (!spec) return { ok: false, error: `No comment syntax is known for ${label || 'this language'}.` };
  if (label === 'Plain text') return { ok: false, error: 'Plain text has no programming comment syntax.' };
  if (label === 'Markdown') return { ok: true, text: body };
  if (label === 'JSON') {
    const dialect = resolveDialect(options, language);
    if (dialect !== 'jsonc') return { ok: false, error: 'Strict JSON has no comment syntax; images cannot be embedded as comments in .json.' };
  }
  const form = pickInsertForm(spec.forms, label);
  if (!form) return { ok: false, error: `${label} has no usable comment form.` };
  const pad = indent || '';
  if (!form.close) {
    const lines = body.split('\n');
    const marker = form.open.endsWith(' ') ? form.open : `${form.open} `;
    const bare = form.open.replace(/ $/, '');
    const text = lines.map((line, index) => {
      const prefix = index === 0 && !pad ? marker : `${pad}${marker}`;
      return line ? `${prefix}${line}` : `${index === 0 && !pad ? bare : pad}${bare}`;
    }).join('\n');
    return { ok: true, text };
  }
  // Block comment: open marker, star-continuation, close marker.
  const lines = body.split('\n');
  if (lines.length === 1) {
    return { ok: true, text: `${pad}${form.open} ${body} ${form.close}` };
  }
  const head = `${pad}${form.open}`;
  const tail = lines.map((line, index) => {
    if (index === 0) return line ? ` ${line}` : '';
    return line ? `${pad} * ${line}` : `${pad} *`;
  }).join('\n');
  const close = `${pad}${form.close}`;
  return { ok: true, text: `${head}${tail}\n${close}` };
}

/**
 * Project the Markdown content of one region. Content spans are joined with
 * single newlines; each span is the exact source slice after delimiter
 * stripping, so escaped/raw literal spelling is preserved byte-for-byte.
 */
export function regionMarkdown(source: string, region: DocumentationRegion): string {
  return region.contentRanges.map((span) => String(source ?? '').slice(span.from, span.to)).join('\n');
}

/** Round-trip proof helper: reconstruct source from regions + gaps. */
export function reconstructFromRegions(source: string, regions: DocumentationRegion[]): string {
  const text = String(source ?? '');
  let cursor = 0;
  let out = '';
  for (const region of [...regions].sort((a, b) => a.from - b.from)) {
    if (region.from < cursor) continue;
    out += text.slice(cursor, region.from);
    out += text.slice(region.from, region.to);
    cursor = region.to;
  }
  out += text.slice(cursor);
  return out;
}
