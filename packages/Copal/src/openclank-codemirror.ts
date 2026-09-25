import { addCursorAbove, addCursorBelow, defaultKeymap, history, historyKeymap, indentSelection, indentWithTab, redo, undo } from '@codemirror/commands';
import { markdown } from '@codemirror/lang-markdown';
import { HighlightStyle, StreamLanguage, bracketMatching, defaultHighlightStyle, foldGutter, forceParsing, indentOnInput, syntaxHighlighting, syntaxTree, syntaxTreeAvailable } from '@codemirror/language';
import { Compartment, EditorSelection, EditorState, SelectionRange, StateEffect, StateField, Transaction } from '@codemirror/state';
import {
  Decoration,
  type DecorationSet,
  EditorView,
  ViewPlugin,
  type ViewUpdate,
  WidgetType,
  drawSelection,
  dropCursor,
  highlightActiveLine,
  highlightSpecialChars,
  keymap,
  lineNumbers,
  placeholder,
  rectangularSelection,
} from '@codemirror/view';
import { tags } from '@lezer/highlight';
import {
  type ContentSpan,
  type DelimiterKind,
  type DocumentationRegion,
  advertisedLabel,
  dialectForPath,
  documentationRegions,
  documentationRegionsAsync,
  regionMarkdown,
  supportsRichComments,
  safeCommentInsertion,
  wrapMarkdownAsComment,
} from './openclank-doc-regions';

type EditorMode = 'live' | 'source';

/** Persisted selection contract shared by Notes, host files, and authoring forms. */
export interface EditorSelectionRange {
  anchor: number;
  head: number;
}

export interface EditorSelectionSnapshot {
  version: 1;
  ranges: EditorSelectionRange[];
  mainIndex: number;
  /** Legacy primary-range fields. New callers should use ranges/mainIndex. */
  anchor: number;
  head: number;
  line: number;
}

type SelectionInput = Partial<EditorSelectionSnapshot> & {
  ranges?: Array<Partial<EditorSelectionRange>>;
};

function boundedOffset(value: unknown, length: number, fallback = 0) {
  const number = Number(value);
  return Number.isFinite(number) ? Math.max(0, Math.min(length, Math.trunc(number))) : fallback;
}

// CodeMirror stores line breaks as one logical document position even when
// the source serializer retains CRLF/CR bytes. Selection offsets therefore
// must be bounded against the model length, rather than String.length of the
// source payload. This matters when a save shortens a CRLF document between
// two editor mounts.
function logicalDocumentLength(value: string) {
  return String(value ?? '').replace(/\r\n?|\n/g, '\n').length;
}

/** Migrate legacy `{anchor, head}` records and discard invalid ranges safely. */
export function normalizeEditorSelection(value: SelectionInput | null | undefined, length: number): { ranges: EditorSelectionRange[]; mainIndex: number } {
  const safeLength = Math.max(0, Math.trunc(Number(length) || 0));
  const source = Array.isArray(value?.ranges) && value.ranges.length
    ? value.ranges
    : [{ anchor:value?.anchor, head:value?.head }];
  const indexed = source.map((range, index) => ({
    index,
    anchor:boundedOffset(range?.anchor, safeLength),
    head:boundedOffset(range?.head, safeLength),
  })).sort((a, b) => Math.min(a.anchor, a.head) - Math.min(b.anchor, b.head) || Math.max(a.anchor, a.head) - Math.max(b.anchor, b.head) || a.index - b.index);
  const ranges: EditorSelectionRange[] = [];
  let mainIndex = 0;
  const requestedMain = Math.max(0, Math.min(source.length - 1, Math.trunc(Number(value?.mainIndex) || 0)));
  for (const candidate of indexed) {
    const previous = ranges.at(-1);
    // CodeMirror requires sorted, non-overlapping ranges. Persisted state can
    // be hand-edited or originate from an older release, so retain the first
    // valid range when records overlap rather than failing editor creation.
    if (previous && Math.min(candidate.anchor, candidate.head) < Math.max(previous.anchor, previous.head)) continue;
    if (candidate.index === requestedMain) mainIndex = ranges.length;
    ranges.push({ anchor:candidate.anchor, head:candidate.head });
  }
  if (!ranges.length) ranges.push({ anchor:0, head:0 });
  if (mainIndex >= ranges.length) mainIndex = Math.min(ranges.length - 1, requestedMain);
  return { ranges, mainIndex };
}

function selectionSnapshot(state: EditorState): EditorSelectionSnapshot {
  const ranges = state.selection.ranges.map((range) => ({ anchor:range.anchor, head:range.head }));
  const main = ranges[state.selection.mainIndex] || ranges[0] || { anchor:0, head:0 };
  return { version:1, ranges, mainIndex:state.selection.mainIndex, anchor:main.anchor, head:main.head, line:state.doc.lineAt(main.head).number };
}

interface MarkdownEditorOptions {
  parent: HTMLElement;
  doc?: string;
  label?: string;
  placeholderText?: string;
  selection?: SelectionInput | null;
  scrollTop?: number;
  mode?: EditorMode;
  language?: string;
  lineNumbers?: boolean;
  readableLineWidth?: boolean;
  lineWrapping?: boolean;
  onChange?: (value: string, update: ViewUpdate) => void;
  onFocus?: () => void;
  onSelection?: (selection: EditorSelectionSnapshot) => void;
  onScroll?: (scrollTop: number) => void;
  onCommand?: (command: string) => void;
  /** Render a resolved Markdown image/embed source without coupling CodeMirror to resource resolution. */
  renderPreview?: (source: string) => HTMLElement | null;
  /** Opt-in source-preserving presentation for documentation comments/docstrings. */
  richComments?: boolean;
  /** Dialect under a shared label, e.g. `jsonc` under JSON. */
  languageDialect?: string;
  /** Source path used for dialect routing when the label alone is ambiguous. */
  languagePath?: string;
  /** Range-aware See source action for rendered documentation elements. */
  onSeeSource?: (range: { from:number; to:number; source:string; markdown:string }, event: Event) => void;
}

export interface CommentSourceRange {
  from: number;
  to: number;
  contentFrom: number;
  contentTo: number;
  language: string;
  /** Per-line source spans make CRLF, indentation, and Unicode mapping explicit. */
  contentRanges: Array<{ from:number; to:number }>;
  delimiterKind?: DelimiterKind;
  kind?: 'comment' | 'docstring';
  open?: string;
  close?: string;
  lineStart?: number;
  lineEnd?: number;
}

/** Return the original line-ending bytes when the logical document is unchanged. */
export function serializeEditorSource(value: string, original: string) {
  const source = String(original);
  const logical = String(value);
  if (logical === source.replace(/\r\n?|\n/g, '\n')) return source;
  const separator = source.includes('\r\n') ? '\r\n' : source.includes('\r') ? '\r' : '';
  return separator ? logical.replace(/\r?\n/g, separator) : logical;
}

// Rich comments cover every advertised comment/docstring-capable language.
// The gate is the documentation-region adapter's source round-trip fixture,
// not parser presence or highlighter color. Markdown and Plain text keep
// their applicable behaviors (full-document preview / plain editing); strict
// JSON has no comments while the JSONC dialect does.
export function isRichCommentLanguageQualified(language?: string, options: { dialect?: string; path?: string } = {}) {
  return supportsRichComments(language, options);
}

function regionToCommentSourceRange(region: DocumentationRegion, language: string): CommentSourceRange {
  return {
    from: region.from,
    to: region.to,
    contentFrom: region.contentFrom,
    contentTo: region.contentTo,
    language: region.language || String(language || ''),
    contentRanges: region.contentRanges.map((span: ContentSpan) => ({ from: span.from, to: span.to })),
    delimiterKind: region.delimiterKind,
    kind: region.kind,
    open: region.open,
    close: region.close,
    lineStart: region.lineStart,
    lineEnd: region.lineEnd,
  };
}

/** Return parser/adapter-recognized documentation regions with exact offsets. */
export function parserCommentSourceRanges(state: EditorState, language = '', options: { dialect?: string; path?: string } = {}): CommentSourceRange[] {
  if (!state || !String(language || '').trim()) return [];
  const source = state.doc.toString();
  return documentationRegions(source, language, options).map((region) => regionToCommentSourceRange(region, language));
}

/**
 * Grammar-aware regions including Python docstrings. Async because Lezer
 * parsers load on demand; stale results are the caller's revision problem.
 */
export async function parserCommentSourceRangesAsync(state: EditorState, language = '', options: { dialect?: string; path?: string } = {}): Promise<CommentSourceRange[]> {
  if (!state || !String(language || '').trim()) return [];
  const source = state.doc.toString();
  const regions = await documentationRegionsAsync(source, language, options);
  return regions.map((region) => regionToCommentSourceRange(region, language));
}

/** Build a renderer-ready map; calling this never mutates the source document. */
export function mapParserComments(state: EditorState, language = '', render?: (markdown:string, range:CommentSourceRange) => unknown, options: { dialect?: string; path?: string } = {}) {
  const source = state?.doc?.toString?.() || '';
  return parserCommentSourceRanges(state, language, options).map((range) => {
    const markdown = range.contentRanges.map((span) => source.slice(span.from, span.to)).join('\n');
    return { ...range, markdown, rendered:typeof render === 'function' ? render(markdown, range) : null };
  });
}

const openClankHighlightStyle = HighlightStyle.define([
  { tag: tags.meta, color: 'var(--hl-comment)' },
  { tag: tags.link, color: 'var(--hl-function)', textDecoration: 'underline' },
  { tag: tags.heading, color: 'var(--hl-function)', fontWeight: 'bold' },
  { tag: tags.emphasis, fontStyle: 'italic' },
  { tag: tags.strong, fontWeight: 'bold' },
  { tag: tags.strikethrough, textDecoration: 'line-through' },
  { tag: tags.keyword, color: 'var(--hl-keyword)' },
  { tag: [tags.atom, tags.bool, tags.null, tags.contentSeparator, tags.labelName], color: 'var(--hl-builtin)' },
  { tag: [tags.literal, tags.inserted], color: 'var(--hl-string)' },
  { tag: [tags.string, tags.deleted], color: 'var(--hl-string)' },
  { tag: [tags.regexp, tags.escape, tags.special(tags.string)], color: 'var(--hl-number)' },
  { tag: tags.comment, color: 'var(--hl-comment)', fontStyle: 'italic' },
  { tag: tags.number, color: 'var(--hl-number)' },
  { tag: tags.definition(tags.variableName), color: 'var(--hl-function)' },
  { tag: tags.function(tags.variableName), color: 'var(--hl-function)' },
  { tag: tags.variableName, color: 'var(--hl-variable)' },
  { tag: [tags.typeName, tags.namespace, tags.className, tags.macroName], color: 'var(--hl-builtin)' },
  { tag: tags.propertyName, color: 'var(--hl-variable)' },
  { tag: tags.tagName, color: 'var(--hl-keyword)' },
  { tag: tags.attributeName, color: 'var(--hl-variable)' },
  { tag: tags.operator, color: 'var(--hl-params)' },
  { tag: tags.punctuation, color: 'var(--hl-fg)' },
  { tag: tags.invalid, color: 'var(--hl-number)', textDecoration: 'underline' },
]);

function initialLanguage(language?: string) {
  const key = String(language || '').toLowerCase();
  if (key === 'markdown') return markdown();
  return [];
}

// Mermaid has no maintained Lezer grammar in the focused bundle. This small
// StreamLanguage parser colors source vocabulary only; it never renders a
// diagram or claims semantic validation. Unknown constructs remain editable.
const mermaidStreamParser: any = {
  startState: () => ({ lineStart: true }),
  token(stream: any, state: any) {
    if (stream.sol()) state.lineStart = true;
    if (stream.match(/^%%.*$/)) return 'comment';
    if (stream.match(/^\s*%%\{.*?\}%%/)) return 'meta';
    if (stream.match(/^(?:flowchart|graph|sequenceDiagram|classDiagram|stateDiagram(?:-v2)?|erDiagram|gantt|pie|mindmap|timeline|gitGraph|journey|quadrantChart|xychart-beta|block-beta)\b/i)) return 'keyword';
    if (stream.match(/^(?:subgraph|end|participant|actor|title|section|class|state|Note|direction|todayMarker|dateFormat|axisFormat|accTitle|accDescr)\b/i)) return 'keyword';
    if (stream.match(/^(?:-->|-.->|==>|-->>|->>|-\.|--|==|\+\+|--)/)) return 'operator';
    if (stream.match(/^"(?:\\.|[^"\\])*"/)) return 'string';
    if (stream.match(/^'(?:\\.|[^'\\])*'/)) return 'string';
    if (stream.match(/^\b\d+(?:\.\d+)?\b/)) return 'number';
    if (stream.match(/^[A-Za-z_][A-Za-z0-9_-]*(?=\s*[\[({:])/)) return 'variableName';
    if (stream.match(/^[A-Za-z_][A-Za-z0-9_-]*/)) return state.lineStart ? 'definition(variableName)' : 'variableName';
    state.lineStart = false;
    stream.next();
    return null;
  },
};

const sourceLanguageLoads = new Map<string, Promise<any>>();

// JSONC keeps the JSON label but adds real comment tokens. Strict JSON stays
// on the Lezer JSON grammar and never receives invented comment syntax.
const jsoncStreamParser: any = {
  startState: () => ({}),
  token(stream: any, _state: any) {
    if (stream.match(/^\/\/.*/)) return 'comment';
    if (stream.match(/^\/\*[\s\S]*?\*\//)) return 'comment';
    if (stream.match(/^"(?:\\.|[^"\\])*"?/)) return 'string';
    if (stream.match(/^-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?/)) return 'number';
    if (stream.match(/^(?:true|false|null)\b/)) return 'atom';
    if (stream.match(/^[{}[\],:]/)) return 'punctuation';
    stream.next();
    return null;
  },
};

function loadSourceLanguage(language?: string, options: { dialect?: string; path?: string } = {}) {
  const key = String(language || '').toLowerCase();
  if (!key || key === 'plain text') return Promise.resolve([]);
  if (key === 'markdown') return Promise.resolve(markdown());
  const dialect = String(options.dialect || options.path || '').toLowerCase();
  if (key === 'json' && (dialect.includes('jsonc') || /(?:^|[/\\.])jsonc$/i.test(String(options.path || '')) || key.includes('jsonc'))) {
    return Promise.resolve(StreamLanguage.define(jsoncStreamParser));
  }
  const cached = sourceLanguageLoads.get(key);
  if (cached) return cached;
  let pending: Promise<any>;
  if (['javascript', 'javascript jsx', 'jsx', 'typescript', 'typescript jsx'].includes(key)) {
    pending = import('@codemirror/lang-javascript').then(({ javascript }) => javascript({ jsx:key.includes('jsx') || key === 'jsx', typescript:key.includes('typescript') }));
  } else if (key === 'json') pending = import('@codemirror/lang-json').then(({ json }) => json());
  else if (key === 'jsonc') pending = Promise.resolve(StreamLanguage.define(jsoncStreamParser));
  else if (key === 'html') pending = import('@codemirror/lang-html').then(({ html }) => html());
  else if (key === 'xml') pending = import('@codemirror/lang-xml').then(({ xml }) => xml());
  else if (key === 'scss') pending = import('@codemirror/lang-sass').then(({ sass }) => sass());
  else if (key === 'css') pending = import('@codemirror/lang-css').then(({ css }) => css());
  else if (['c', 'c++', 'c/c++ header', 'c++ header'].includes(key)) pending = import('@codemirror/lang-cpp').then(({ cpp }) => cpp());
  else if (key === 'c#') pending = import('@codemirror/legacy-modes/mode/clike').then(({ csharp }) => StreamLanguage.define(csharp));
  else if (key === 'java') pending = import('@codemirror/lang-java').then(({ java }) => java());
  else if (key === 'kotlin') pending = import('@codemirror/legacy-modes/mode/clike').then(({ kotlin }) => StreamLanguage.define(kotlin));
  else if (key === 'go') pending = import('@codemirror/lang-go').then(({ go }) => go());
  else if (key === 'python') pending = import('@codemirror/lang-python').then(({ python }) => python());
  else if (key === 'php') pending = import('@codemirror/lang-php').then(({ php }) => php());
  else if (key === 'rust') pending = import('@codemirror/lang-rust').then(({ rust }) => rust());
  else if (key === 'ruby') pending = import('@codemirror/legacy-modes/mode/ruby').then(({ ruby }) => StreamLanguage.define(ruby));
  else if (key === 'swift') pending = import('@codemirror/legacy-modes/mode/swift').then(({ swift }) => StreamLanguage.define(swift));
  else if (key === 'shell') pending = import('@codemirror/legacy-modes/mode/shell').then(({ shell }) => StreamLanguage.define(shell));
  else if (key === 'dockerfile') pending = import('@codemirror/legacy-modes/mode/dockerfile').then(({ dockerFile }) => StreamLanguage.define(dockerFile));
  else if (key === 'yaml') pending = import('@codemirror/lang-yaml').then(({ yaml }) => yaml());
  else if (key === 'toml') pending = import('@codemirror/legacy-modes/mode/toml').then(({ toml }) => StreamLanguage.define(toml));
  else if (key === 'sql') pending = import('@codemirror/lang-sql').then(({ sql }) => sql());
  else if (key === 'mermaid') pending = Promise.resolve(StreamLanguage.define(mermaidStreamParser));
  else return Promise.resolve([]);
  // A failed parser load must never make the buffer uneditable or poison later
  // attempts after an updated static asset is deployed.
  sourceLanguageLoads.set(key, pending);
  pending.catch(() => sourceLanguageLoads.delete(key));
  return pending;
}

interface DecorationRange {
  from: number;
  to: number;
  value: Decoration;
}

class CheckboxWidget extends WidgetType {
  constructor(private readonly checked: boolean, private readonly from: number) { super(); }

  eq(other: CheckboxWidget) { return other.checked === this.checked && other.from === this.from; }

  toDOM(view: EditorView) {
    const input = document.createElement('input');
    input.type = 'checkbox';
    input.checked = this.checked;
    input.className = 'cm-md-task-checkbox';
    input.setAttribute('aria-label', this.checked ? 'Mark task incomplete' : 'Mark task complete');
    input.addEventListener('mousedown', (event) => event.preventDefault());
    input.addEventListener('click', (event) => {
      event.preventDefault();
      const token = view.state.doc.sliceString(this.from, this.from + 3);
      view.dispatch({ changes:{ from:this.from, to:this.from + 3, insert:token.toLowerCase() === '[x]' ? '[ ]' : '[x]' } });
      view.focus();
    });
    return input;
  }

  ignoreEvent() { return false; }
}

class FrontmatterWidget extends WidgetType {
  constructor(private readonly rows: Array<{ key:string; value:string; from:number; to:number }>) { super(); }

  eq(other: FrontmatterWidget) { return JSON.stringify(other.rows) === JSON.stringify(this.rows); }

  toDOM(view: EditorView) {
    const card = document.createElement('section');
    card.className = 'cm-md-frontmatter-card';
    const heading = document.createElement('strong');
    heading.className = 'cm-md-frontmatter-title';
    heading.textContent = 'Properties';
    card.append(heading);
    const list = document.createElement('div');
    list.className = 'cm-md-frontmatter-list';
    for (const row of this.rows) {
      const wrapper = document.createElement('label');
      wrapper.className = 'cm-md-frontmatter-row';
      const key = document.createElement('span');
      key.className = 'cm-md-frontmatter-key';
      key.textContent = row.key;
      const input = document.createElement('input');
      input.className = 'cm-md-frontmatter-input';
      input.value = row.value;
      input.addEventListener('mousedown', (event) => event.stopPropagation());
      input.addEventListener('keydown', (event) => {
        if (event.key === 'Enter') input.blur();
        if (event.key === 'Escape') { input.value = row.value; input.blur(); }
      });
      input.addEventListener('blur', () => {
        if (input.value !== row.value) view.dispatch({ changes:{ from:row.from, to:row.to, insert:input.value } });
      });
      wrapper.append(key, input);
      list.append(wrapper);
    }
    card.append(list);
    return card;
  }

  ignoreEvent() { return false; }
}

class BlockPreviewWidget extends WidgetType {
  constructor(
    private readonly kind: 'table' | 'callout' | 'math' | 'embed' | 'footnote' | 'hr',
    private readonly source: string,
    private readonly from: number,
    private readonly renderPreview?: (source: string) => HTMLElement | null,
    private readonly inline = false,
  ) { super(); }

  eq(other: BlockPreviewWidget) { return other.kind === this.kind && other.source === this.source && other.from === this.from && other.inline === this.inline; }

  toDOM(view: EditorView) {
    const root = document.createElement(this.inline ? 'span' : (this.kind === 'embed' ? 'figure' : 'div'));
    root.className = `cm-md-${this.kind}-widget${this.inline ? ' cm-md-inline-preview-widget' : ''}`;
    if (this.kind === 'embed' && this.renderPreview) {
      try {
        const preview = this.renderPreview(this.source);
        if (preview) root.append(preview);
        else root.textContent = this.source.trim().replace(/^!/, '');
      } catch (_) {
        root.textContent = this.source.trim().replace(/^!/, '');
      }
    } else if (this.kind === 'hr') {
      root.append(document.createElement('hr'));
    } else if (this.kind === 'table') {
      const rows = this.source.split('\n').map((line) => line.trim().replace(/^\||\|$/g, '').split('|').map((cell) => cell.trim()));
      const table = document.createElement('table');
      rows.filter((_, index) => index !== 1).forEach((row, rowIndex) => {
        const tr = document.createElement('tr');
        row.forEach((value) => {
          const cell = document.createElement(rowIndex === 0 ? 'th' : 'td');
          cell.textContent = value;
          tr.append(cell);
        });
        table.append(tr);
      });
      root.append(table);
    } else if (this.kind === 'callout') {
      const [first, ...body] = this.source.split('\n');
      const match = /^>\s*\[!([^\]]+)\][+-]?\s*(.*)$/.exec(first);
      const title = document.createElement('strong');
      title.textContent = match?.[2] || match?.[1] || 'Callout';
      const content = document.createElement('div');
      content.textContent = body.map((line) => line.replace(/^>\s?/, '')).join('\n');
      root.append(title, content);
    } else if (this.kind === 'math') {
      const pre = document.createElement('pre');
      pre.textContent = this.source.replace(/^\$\$\s*|\s*\$\$$/g, '');
      root.append(pre);
    } else if (this.kind === 'footnote') {
      const match = /^\s*\[\^([^\]]+)\]:\s*(.*)$/.exec(this.source);
      const marker = document.createElement('span');
      marker.className = 'cm-md-footnote-marker'; marker.textContent = match?.[1] || 'note';
      const content = document.createElement('span');
      content.textContent = match?.[2] || '';
      root.append(marker, content);
    } else {
      const kind = document.createElement('span');
      kind.className = 'cm-md-embed-kind'; kind.textContent = 'embed';
      const label = document.createElement('span');
      label.className = 'cm-md-embed-label'; label.textContent = this.source.trim().replace(/^!/, '');
      root.append(kind, label);
    }
    root.tabIndex = 0;
    root.title = 'Press Enter to edit source';
    root.addEventListener('keydown', (event) => {
      if ((event as KeyboardEvent).key !== 'Enter') return;
      view.dispatch({ selection:EditorSelection.cursor(this.from), scrollIntoView:true });
      view.focus();
    });
    root.addEventListener('dblclick', () => {
      view.dispatch({ selection:EditorSelection.cursor(this.from), scrollIntoView:true });
      view.focus();
    });
    return root;
  }

  ignoreEvent() { return false; }
}

function activeLineNumbers(state: EditorState) {
  const numbers = new Set<number>();
  for (const range of state.selection.ranges) {
    const start = state.doc.lineAt(range.from).number;
    const end = state.doc.lineAt(range.to).number;
    for (let line = start; line <= end; line += 1) numbers.add(line);
  }
  return numbers;
}

function addInlineDecorations(ranges: DecorationRange[], from: number, text: string) {
  const syntax = Decoration.mark({ class:'cm-md-syntax-hidden' });
  const patterns: Array<[RegExp, string, number]> = [
    [/\*\*([^*\n]+)\*\*/g, 'cm-md-strong', 2],
    [/~~([^~\n]+)~~/g, 'cm-md-strike', 2],
    [/==([^=\n]+)==/g, 'cm-md-highlight', 2],
    [/(?<!\*)\*([^*\n]+)\*(?!\*)/g, 'cm-md-emphasis', 1],
    [/_([^_\n]+)_/g, 'cm-md-emphasis', 1],
    [/`([^`\n]+)`/g, 'cm-md-inline-code', 1],
    [/\$([^$\n]+)\$/g, 'cm-md-inline-math', 1],
  ];
  for (const [pattern, className, marker] of patterns) {
    for (const match of text.matchAll(pattern)) {
      if (match.index == null) continue;
      const start = from + match.index;
      const end = start + match[0].length;
      ranges.push({ from:start, to:start + marker, value:syntax });
      ranges.push({ from:start + marker, to:end - marker, value:Decoration.mark({ class:className }) });
      ranges.push({ from:end - marker, to:end, value:syntax });
    }
  }
  for (const match of text.matchAll(/!?\[\[([^\]|\n]+)(?:\|([^\]\n]+))?\]\]/g)) {
    if (match.index == null) continue;
    const start = from + match.index;
    const end = start + match[0].length;
    const prefix = match[0].startsWith('!') ? 3 : 2;
    ranges.push({ from:start, to:start + prefix, value:syntax });
    ranges.push({ from:start + prefix, to:end - 2, value:Decoration.mark({ class:'cm-md-wikilink' }) });
    ranges.push({ from:end - 2, to:end, value:syntax });
  }
  for (const match of text.matchAll(/\[([^\]\n]+)\]\(([^)\n]+)\)/g)) {
    if (match.index == null) continue;
    const start = from + match.index;
    const labelStart = start + 1;
    const labelEnd = labelStart + match[1].length;
    const end = start + match[0].length;
    ranges.push({ from:start, to:labelStart, value:syntax });
    ranges.push({ from:labelStart, to:labelEnd, value:Decoration.mark({ class:'cm-md-link' }) });
    ranges.push({ from:labelEnd, to:end, value:syntax });
  }
  for (const match of text.matchAll(/%%[^%\n]*(?:%(?!%)[^%\n]*)*%%/g)) {
    if (match.index != null) ranges.push({ from:from + match.index, to:from + match.index + match[0].length, value:syntax });
  }
  for (const match of text.matchAll(/\\(?=[\\`*_[\]{}()#+.!|~-])/g)) {
    if (match.index != null) ranges.push({ from:from + match.index, to:from + match.index + 1, value:syntax });
  }
  for (const match of text.matchAll(/(^|[\s(])#([A-Za-z0-9_/-]+)/g)) {
    if (match.index == null) continue;
    const start = from + match.index + match[1].length;
    ranges.push({ from:start, to:start + match[0].length - match[1].length, value:Decoration.mark({ class:'cm-md-tag' }) });
  }
}

function frontmatterBlock(state: EditorState, active: Set<number>) {
  if (state.doc.lines < 2 || state.doc.line(1).text.trim() !== '---') return null;
  let end = 0;
  for (let line = 2; line <= state.doc.lines; line += 1) {
    if (state.doc.line(line).text.trim() === '---') { end = line; break; }
  }
  if (!end || [...active].some((line) => line <= end)) return null;
  const rows: Array<{ key:string; value:string; from:number; to:number }> = [];
  for (let line = 2; line < end; line += 1) {
    const current = state.doc.line(line);
    const match = /^(\s*)([A-Za-z0-9_.-]+):(.*)$/.exec(current.text);
    if (!match) continue;
    const raw = match[3];
    const leading = raw.length - raw.trimStart().length;
    const value = raw.trim();
    const from = current.from + current.text.indexOf(':') + 1 + leading;
    rows.push({ key:match[2], value, from, to:from + value.length });
  }
  return { from:state.doc.line(1).from, to:state.doc.line(end).to, rows, endLine:end };
}

function imageSyntax(text: string) {
  const markdownImage = /^!\[([^\]\n]*)\]\(([^)\n]+)\)$/.exec(text.trim());
  if (markdownImage) return { source:text.trim() };
  const wikiImage = /^!\[\[([^\]|\n]+)(?:\|([^\]\n]+))?\]\]$/.exec(text.trim());
  if (wikiImage) return { source:text.trim() };
  return null;
}

function inlineImageMatches(text: string) {
  const matches: Array<{ from:number; to:number; source:string }> = [];
  const pattern = /!\[[^\]\n]*\]\(([^)\n]+)\)|!\[\[[^\]|\n]+(?:\|[^\]\n]+)?\]\]/g;
  for (const match of text.matchAll(pattern)) {
    if (match.index == null) continue;
    matches.push({ from:match.index, to:match.index + match[0].length, source:match[0] });
  }
  return matches;
}

function blockAt(state: EditorState, lineNumber: number, active: Set<number>) {
  const line = state.doc.line(lineNumber);
  const text = line.text;
  if (/^\s{0,3}([-*_])(?:\s*\1){2,}\s*$/.test(text)) return { kind:'hr' as const, from:line.from, to:line.to, source:text, endLine:lineNumber };
  if (/^\s*\[\^[^\]]+\]:/.test(text)) return { kind:'footnote' as const, from:line.from, to:line.to, source:text, endLine:lineNumber };
  if (/^\s*!/.test(text) && imageSyntax(text)) return { kind:'embed' as const, from:line.from, to:line.to, source:text, endLine:lineNumber };
  if (/^\s*>\s*\[!/.test(text)) {
    let end = lineNumber;
    while (end < state.doc.lines && /^\s*>/.test(state.doc.line(end + 1).text)) end += 1;
    if ([...active].some((number) => number >= lineNumber && number <= end)) return null;
    return { kind:'callout' as const, from:line.from, to:state.doc.line(end).to, source:state.doc.sliceString(line.from, state.doc.line(end).to), endLine:end };
  }
  if (text.includes('|') && lineNumber < state.doc.lines && /^\s*\|?\s*:?-{3,}/.test(state.doc.line(lineNumber + 1).text)) {
    let end = lineNumber + 1;
    while (end < state.doc.lines && state.doc.line(end + 1).text.includes('|')) end += 1;
    if ([...active].some((number) => number >= lineNumber && number <= end)) return null;
    return { kind:'table' as const, from:line.from, to:state.doc.line(end).to, source:state.doc.sliceString(line.from, state.doc.line(end).to), endLine:end };
  }
  if (text.trim().startsWith('$$')) {
    let end = lineNumber;
    if (!text.trim().endsWith('$$') || text.trim() === '$$') {
      while (end < state.doc.lines) { end += 1; if (state.doc.line(end).text.trim().endsWith('$$')) break; }
    }
    if ([...active].some((number) => number >= lineNumber && number <= end)) return null;
    return { kind:'math' as const, from:line.from, to:state.doc.line(end).to, source:state.doc.sliceString(line.from, state.doc.line(end).to), endLine:end };
  }
  return null;
}

interface StructuralDecorationState {
  decorations: DecorationSet;
  ranges: Array<{ from:number; to:number }>;
}

function structuralWindows(state: EditorState, ranges: Array<{ from:number; to:number }>) {
  const candidates = ranges.map((range) => ({
    start:Math.max(1, state.doc.lineAt(Math.max(0, Math.min(state.doc.length, range.from))).number - 32),
    end:Math.min(state.doc.lines, state.doc.lineAt(Math.max(0, Math.min(state.doc.length, range.to))).number + 32),
  })).sort((a, b) => a.start - b.start);
  const windows: Array<{ start:number; end:number }> = [];
  for (const candidate of candidates) {
    const previous = windows.at(-1);
    if (!previous || candidate.start > previous.end + 1) windows.push(candidate);
    else previous.end = Math.max(previous.end, candidate.end);
  }
  return windows;
}

function buildStructuralDecorations(state: EditorState, visibleRanges: Array<{ from:number; to:number }>, renderPreview?: (source:string) => HTMLElement | null): DecorationSet {
  const ranges: DecorationRange[] = [];
  const active = activeLineNumbers(state);
  const visited = new Set<number>();
  const windows = structuralWindows(state, visibleRanges);
  const frontmatter = frontmatterBlock(state, active);
  if (frontmatter && windows.some((window) => window.start <= frontmatter.endLine && window.end >= 1)) {
    ranges.push({ from:frontmatter.from, to:frontmatter.to, value:Decoration.replace({ widget:new FrontmatterWidget(frontmatter.rows), block:true }) });
    for (let line = 1; line <= frontmatter.endLine; line += 1) visited.add(line);
  }
  for (const window of windows) {
    for (let lineNumber = window.start; lineNumber <= window.end; lineNumber += 1) {
      if (visited.has(lineNumber)) continue;
      visited.add(lineNumber);
      const line = state.doc.line(lineNumber);
      if (active.has(lineNumber)) continue;
      const block = blockAt(state, lineNumber, active);
      if (block) {
        ranges.push({ from:block.from, to:block.to, value:Decoration.replace({ widget:new BlockPreviewWidget(block.kind, block.source, block.from, renderPreview), block:true }) });
        for (let line = lineNumber; line <= block.endLine; line += 1) visited.add(line);
        continue;
      }
      const heading = /^(#{1,6})\s+/.exec(line.text);
      if (heading) ranges.push({ from:line.from, to:line.from, value:Decoration.line({ class:`cm-md-heading cm-md-h${heading[1].length}` }) });
      if (/^\s*>/.test(line.text)) ranges.push({ from:line.from, to:line.from, value:Decoration.line({ class:'cm-md-quote-line' }) });
      if (/^\s*```/.test(line.text)) ranges.push({ from:line.from, to:line.from, value:Decoration.line({ class:'cm-md-code-line' }) });
      if (/^(\s*[-*+]\s+)(\[([ xX/-])\])\s+/.test(line.text)) ranges.push({ from:line.from, to:line.from, value:Decoration.line({ class:'cm-md-task-line' }) });
    }
  }
  return Decoration.set(ranges.sort((a, b) => a.from - b.from || a.to - b.to), true);
}

const setStructuralViewport = StateEffect.define<Array<{ from:number; to:number }>>();

function buildLivePreviewDecorations(view: EditorView, renderPreview?: (source:string) => HTMLElement | null): DecorationSet {
  const ranges: DecorationRange[] = [];
  const active = activeLineNumbers(view.state);
  const visited = new Set<number>();
  const frontmatter = frontmatterBlock(view.state, active);
  if (frontmatter) for (let line = 1; line <= frontmatter.endLine; line += 1) visited.add(line);
  for (const visible of view.visibleRanges) {
    const start = view.state.doc.lineAt(visible.from).number;
    const end = view.state.doc.lineAt(visible.to).number;
    for (let lineNumber = start; lineNumber <= end; lineNumber += 1) {
      if (visited.has(lineNumber)) continue;
      visited.add(lineNumber);
      const line = view.state.doc.line(lineNumber);
      if (active.has(lineNumber)) continue;
      const block = blockAt(view.state, lineNumber, active);
      if (block) {
        for (let line = lineNumber; line <= block.endLine; line += 1) visited.add(line);
        continue;
      }
      const heading = /^(#{1,6})\s+/.exec(line.text);
      if (heading) ranges.push({ from:line.from, to:line.from + heading[0].length, value:Decoration.mark({ class:'cm-md-syntax-hidden' }) });
      const task = /^(\s*[-*+]\s+)(\[([ xX/-])\])\s+/.exec(line.text);
      if (task) {
        const checkboxFrom = line.from + task[1].length;
        ranges.push({ from:line.from, to:checkboxFrom, value:Decoration.mark({ class:'cm-md-syntax-hidden' }) });
        ranges.push({ from:checkboxFrom, to:checkboxFrom + 3, value:Decoration.replace({ widget:new CheckboxWidget(task[3].toLowerCase() === 'x', checkboxFrom) }) });
        ranges.push({ from:checkboxFrom + 3, to:line.from + task[0].length, value:Decoration.mark({ class:'cm-md-syntax-hidden' }) });
      }
      const list = task ? null : /^(\s*)(?:[-*+]|\d+[.)])\s+/.exec(line.text);
      if (list) ranges.push({ from:line.from + list[1].length, to:line.from + list[0].length, value:Decoration.mark({ class:'cm-md-list-marker' }) });
      const fence = /^\s*```(?:\S+)?\s*$/.exec(line.text);
      if (fence) ranges.push({ from:line.from, to:line.to, value:Decoration.mark({ class:'cm-md-fence-marker' }) });
      if (!/^\s*```/.test(line.text)) {
        addInlineDecorations(ranges, line.from, line.text);
        for (const image of inlineImageMatches(line.text)) ranges.push({ from:line.from + image.from, to:line.from + image.to, value:Decoration.replace({ widget:new BlockPreviewWidget('embed', image.source, line.from + image.from, renderPreview, true) }) });
      }
    }
  }
  return Decoration.set(ranges.sort((a, b) => a.from - b.from || a.to - b.to), true);
}

function createStructuralDecorations(renderPreview?: (source:string) => HTMLElement | null) {
  return StateField.define<StructuralDecorationState>({
    create:(state) => {
      const ranges = [{ from:0, to:Math.min(state.doc.length, 8000) }];
      return { ranges, decorations:buildStructuralDecorations(state, ranges, renderPreview) };
    },
    update:(value, transaction) => {
      const effect = transaction.effects.find((candidate) => candidate.is(setStructuralViewport));
      const ranges = effect?.value || value.ranges.map((range) => ({ from:transaction.changes.mapPos(range.from), to:transaction.changes.mapPos(range.to) }));
      if (effect || transaction.docChanged || transaction.selection) return { ranges, decorations:buildStructuralDecorations(transaction.state, ranges, renderPreview) };
      return value;
    },
    provide:(field) => EditorView.decorations.from(field, (value) => value.decorations),
  });
}

function createLivePreviewPlugin(renderPreview?: (source:string) => HTMLElement | null) {
  return ViewPlugin.fromClass(class {
    decorations: DecorationSet;
    viewport = '';
    constructor(view: EditorView) { this.decorations = buildLivePreviewDecorations(view, renderPreview); this.updateViewport(view); }
    updateViewport(view: EditorView) {
      const ranges = view.visibleRanges.map(({ from, to }) => ({ from, to }));
      const signature = ranges.map(({ from, to }) => `${from}:${to}`).join(',');
      if (signature === this.viewport) return;
      this.viewport = signature;
      queueMicrotask(() => { if (view.dom.isConnected) view.dispatch({ effects:setStructuralViewport.of(ranges) }); });
    }
    update(update: ViewUpdate) {
      if (update.docChanged || update.selectionSet || update.viewportChanged) {
        this.decorations = buildLivePreviewDecorations(update.view, renderPreview);
        this.updateViewport(update.view);
      }
    }
  }, { decorations:(value) => value.decorations });
}

class CommentPreviewWidget extends WidgetType {
  constructor(
    private readonly source: string,
    private readonly markdown: string,
    private readonly from: number,
    private readonly to: number,
    private readonly renderPreview?: (source:string) => HTMLElement | null,
    private readonly onSeeSource?: (range: { from:number; to:number; source:string; markdown:string }, event: Event) => void,
  ) { super(); }

  eq(other: CommentPreviewWidget) {
    return other.source === this.source && other.markdown === this.markdown && other.from === this.from && other.to === this.to;
  }

  toDOM(view: EditorView) {
    const block = this.source.includes('\n');
    const root = document.createElement(block ? 'div' : 'span');
    root.className = `cm-rich-comment-widget${block ? ' cm-rich-comment-block' : ''}`;
    root.dataset.commentSourceFrom = String(this.from);
    root.dataset.commentSourceTo = String(this.to);
    root.dataset.commentRevision = String(view.state.doc.length);
    root.setAttribute('aria-label', 'Rendered source comment; press Enter or choose See source to edit raw comment');
    let preview: HTMLElement | null = null;
    try { preview = this.renderPreview?.(this.markdown) || null; } catch (_) { preview = null; }
    if (preview) root.append(preview);
    else {
      const raw = document.createElement('code');
      raw.textContent = this.source;
      root.append(raw);
    }
    root.tabIndex = 0;
    const reveal = () => {
      view.dispatch({ selection:EditorSelection.range(this.from, this.to), scrollIntoView:true });
      view.focus();
    };
    root.addEventListener('keydown', (event) => {
      if ((event as KeyboardEvent).key === 'Enter') { event.preventDefault(); reveal(); }
      // Range-aware See source from the keyboard.
      if ((event as KeyboardEvent).key === 'F10' && (event as KeyboardEvent).shiftKey) {
        event.preventDefault();
        this.onSeeSource?.({ from:this.from, to:this.to, source:this.source, markdown:this.markdown }, event);
      }
    });
    root.addEventListener('dblclick', reveal);
    root.addEventListener('contextmenu', (event) => {
      // Always offer an explicit See source action on rendered elements.
      const target = { from:this.from, to:this.to, source:this.source, markdown:this.markdown };
      if (typeof this.onSeeSource === 'function') {
        this.onSeeSource(target, event);
      }
    });
    return root;
  }

  ignoreEvent() { return false; }
}

function buildCommentDecorations(state: EditorState, language: string, renderPreview?: (source:string) => HTMLElement | null, visibleRanges: readonly { from:number; to:number }[] = [], comments = parserCommentSourceRanges(state, language), onSeeSource?: (range: { from:number; to:number; source:string; markdown:string }, event: Event) => void): DecorationSet {
  const source = state.doc.toString();
  const active = state.selection.ranges;
  const decorations = comments.map((comment) => ({
    ...comment,
    markdown:comment.contentRanges.map((span) => source.slice(span.from, span.to)).join('\n'),
  })).flatMap((comment) => {
    // Rendering is a viewport concern. The source map API remains available
    // for callers that need raw offsets, while rich widgets stay bounded on
    // large files and are rebuilt only for visible comments.
    if (visibleRanges.length && !visibleRanges.some((visible) => visible.from < comment.to && visible.to > comment.from)) return [];
    // A cursor or selection inside a comment always exposes its source so
    // editing and keyboard shortcuts continue to address exact source bytes.
    if (active.some((selection) => selection.from <= comment.to && selection.to >= comment.from)) return [];
    const attributes = {
      'data-comment-source-from':String(comment.from),
      'data-comment-source-to':String(comment.to),
    };
    if (!renderPreview) return [{ from:comment.from, to:comment.to, value:Decoration.mark({ class:'cm-rich-comment-source', attributes }) }];
    return [{
      from:comment.from,
      to:comment.to,
      value:Decoration.replace({ widget:new CommentPreviewWidget(source.slice(comment.from, comment.to), comment.markdown, comment.from, comment.to, renderPreview, onSeeSource), block:source.slice(comment.from, comment.to).includes('\n') }),
    }];
  });
  return Decoration.set(decorations, true);
}

/** Opt-in parser-backed comment presentation with a safe source fallback. */
function createRichCommentPlugin(language: string, renderPreview?: (source:string) => HTMLElement | null, regionOptions: { dialect?: string; path?: string } = {}, onSeeSource?: (range: { from:number; to:number; source:string; markdown:string }, event: Event) => void) {
  return ViewPlugin.fromClass(class {
    decorations: DecorationSet;
    comments: CommentSourceRange[];
    rawSelectionActive = false;
    /** Monotonic revision tag; stale async parses are discarded. */
    parseRevision = 0;
    parsePending = false;
    constructor(view: EditorView) {
      this.comments = parserCommentSourceRanges(view.state, language, regionOptions);
      this.rawSelectionActive = view.state.selection.ranges.some((selection) => this.comments.some((comment) => selection.from <= comment.to && selection.to >= comment.from));
      this.decorations = buildCommentDecorations(view.state, language, renderPreview, view.visibleRanges, this.comments, onSeeSource);
    }
    scheduleParse(view: EditorView) {
      const revision = ++this.parseRevision;
      if (this.parsePending) return;
      this.parsePending = true;
      queueMicrotask(() => {
        this.parsePending = false;
        if (revision !== this.parseRevision || !view.dom.isConnected) return;
        this.comments = parserCommentSourceRanges(view.state, language, regionOptions);
        this.decorations = buildCommentDecorations(view.state, language, renderPreview, view.visibleRanges, this.comments, onSeeSource);
        view.dispatch({});
      });
    }
    update(update: ViewUpdate) {
      let delimiterIntroduced = false;
      if (update.docChanged) update.changes.iterChanges((_fromA, _toA, _fromB, _toB, inserted) => {
        if (/(?:\/\/|\/\*|\*\/|<!--|-->|^\s*#|^\s*--|^\s*%%)/m.test(inserted.toString())) delimiterIntroduced = true;
      });
      const changedComment = update.docChanged && this.comments.some((comment) => update.changes.touchesRange(comment.from, comment.to));
      const visibleChanged = update.docChanged && update.view.visibleRanges.some((range) => update.changes.touchesRange(range.from, range.to));
      const reparseChanged = visibleChanged && (changedComment || delimiterIntroduced);
      if (update.docChanged) {
        this.comments = this.comments.map((comment) => ({
          ...comment,
          from:update.changes.mapPos(comment.from, 1),
          to:update.changes.mapPos(comment.to, -1),
          contentFrom:update.changes.mapPos(comment.contentFrom, 1),
          contentTo:update.changes.mapPos(comment.contentTo, -1),
          contentRanges:comment.contentRanges.map((range) => ({ from:update.changes.mapPos(range.from, 1), to:update.changes.mapPos(range.to, -1) })),
        }));
        // Edit latency stays on the mapPos path. A full reparse is scheduled
        // asynchronously so a 1 MiB file cannot blow the p95 budget.
        this.decorations = this.decorations.map(update.changes);
      }
      let selectionMovedAcrossWidget = false;
      if (update.selectionSet) {
        const rawSelection = update.state.selection.ranges.some((selection) => this.comments.some((comment) => selection.from <= comment.to && selection.to >= comment.from));
        selectionMovedAcrossWidget = rawSelection !== this.rawSelectionActive;
        this.rawSelectionActive = rawSelection;
      }
      if (selectionMovedAcrossWidget || update.viewportChanged || update.transactions.some((transaction) => transaction.reconfigured)) {
        this.decorations = buildCommentDecorations(update.state, language, renderPreview, update.view.visibleRanges, this.comments, onSeeSource);
      }
      if (reparseChanged || update.viewportChanged || update.transactions.some((transaction) => transaction.reconfigured)) {
        this.scheduleParse(update.view);
      }
    }
  }, { decorations:(value) => value.decorations });
}

const baseTheme = EditorView.theme({
  '&': { height:'100%', color:'var(--fg)', backgroundColor:'transparent', fontSize:'14px' },
  '.cm-scroller': { fontFamily:'var(--font-family, system-ui, sans-serif)', lineHeight:'1.72', overflow:'auto' },
  '.cm-content': { padding:'28px clamp(18px, 5vw, 72px)', caretColor:'var(--accent, #22d3ee)', width:'100%' },
  '.cm-line': { padding:'0 4px' },
  '.cm-cursor, .cm-dropCursor': { borderLeftColor:'var(--accent, #22d3ee)' },
  '&.cm-focused': { outline:'none' },
  '.cm-selectionBackground, ::selection': { backgroundColor:'color-mix(in srgb, var(--accent, #22d3ee) 25%, transparent) !important' },
  '.cm-gutters': { backgroundColor:'transparent', color:'color-mix(in srgb, var(--fg) 36%, transparent)', border:'0' },
  '.cm-activeLine': { backgroundColor:'color-mix(in srgb, var(--accent, #22d3ee) 4%, transparent)' },
});

function widthExtension(readable: boolean) {
  return EditorView.theme({ '.cm-content':readable ? { maxWidth:'900px', margin:'0 auto' } : { maxWidth:'none', margin:'0' } });
}

function createOpenClankEditor(options: MarkdownEditorOptions) {
  const {
    parent, doc = '', label = 'Markdown editor', placeholderText = 'Start writing…', selection,
    onChange, onFocus, onSelection, onScroll, onCommand, renderPreview,
  } = options;
  const regionOptions = { dialect: options.languageDialect, path: options.languagePath };
  let silent = false;
  let mode: EditorMode = options.mode === 'source' ? 'source' : 'live';
  let showLineNumbers = options.lineNumbers === true;
  let readableLineWidth = options.readableLineWidth !== false;
  let lineWrapping = options.lineWrapping !== false;
  let destroyed = false;
  const modeCompartment = new Compartment();
  const gutterCompartment = new Compartment();
  const widthCompartment = new Compartment();
  const languageCompartment = new Compartment();
  const initialSelection = normalizeEditorSelection(selection, logicalDocumentLength(String(doc)));
  const cmSelection = EditorSelection.create(initialSelection.ranges.map((range) => EditorSelection.range(range.anchor, range.head)), initialSelection.mainIndex);
  // Keep the source's newline bytes in CodeMirror's document model. Without
  // this facet CodeMirror normalizes CRLF input to LF, which makes a raw
  // source round-trip silently rewrite files even when the editor is idle.
  const lineSeparator = String(doc).includes('\r\n') ? '\r\n' : String(doc).includes('\r') ? '\r' : undefined;
  let view: EditorView;
  let runEditorCommand: (name: string) => boolean = () => false;
  const command = (name: string) => { onCommand?.(name); return true; };
  const originalSource = String(doc);
  const serializeSource = (value: string) => serializeEditorSource(value, originalSource);
  const state = EditorState.create({
    doc,
    selection:cmSelection,
    extensions:[
      gutterCompartment.of(showLineNumbers ? [lineNumbers()] : []),
      highlightSpecialChars(), history(), foldGutter(), drawSelection(), dropCursor(),
      EditorState.allowMultipleSelections.of(true), indentOnInput(),
      ...(lineSeparator ? [EditorState.lineSeparator.of(lineSeparator)] : []),
      // Open Clank is the owned highlighter. CodeMirror's default remains only as
      // a true fallback for syntax nodes our semantic palette does not cover; if
      // both styles are registered as fallbacks, the default can win the facet and
      // prevent the live --hl-* theme variables from reaching token spans.
      syntaxHighlighting(openClankHighlightStyle), syntaxHighlighting(defaultHighlightStyle, { fallback:true }), bracketMatching(), rectangularSelection(),
      highlightActiveLine(), languageCompartment.of(initialLanguage(options.language || 'markdown')), ...(lineWrapping ? [EditorView.lineWrapping] : []), placeholder(placeholderText),
      modeCompartment.of(mode === 'live' ? [createStructuralDecorations(renderPreview), createLivePreviewPlugin(renderPreview)] : options.richComments === true && isRichCommentLanguageQualified(options.language, regionOptions) ? [createRichCommentPlugin(options.language || '', renderPreview, regionOptions, options.onSeeSource)] : []),
      widthCompartment.of(widthExtension(readableLineWidth)),
      keymap.of([
        { key:'Mod-s', run:() => command('save') },
        { key:'Mod-o', run:() => command('quick-open') },
        { key:'Mod-p', run:() => command('palette') },
        { key:'Mod-Shift-f', run:() => command('search') },
        // Shared platform map: Mod is Cmd on macOS and Ctrl elsewhere.
        // Mod-d selects the next occurrence, Mod-Shift-l selects all, and
        // Mod-Alt-arrow adds a cursor vertically. Escape collapses extras.
        { key:'Mod-d', run:() => runEditorCommand('select-next-match') },
        { key:'Mod-Shift-l', run:() => runEditorCommand('select-all-matches') },
        { key:'Mod-Alt-ArrowUp', run:() => runEditorCommand('add-cursor-above') },
        { key:'Mod-Alt-ArrowDown', run:() => runEditorCommand('add-cursor-below') },
        { key:'Mod-Alt-\\', run:() => runEditorCommand('indent') },
        { key:'Mod-Shift-d', run:() => runEditorCommand('duplicate-line') },
        { key:'Escape', run:() => runEditorCommand('collapse-selections') },
        indentWithTab, ...defaultKeymap, ...historyKeymap,
      ]),
      EditorView.contentAttributes.of({ 'aria-label':label, spellcheck:'true' }),
      EditorView.updateListener.of((update) => {
        if (update.focusChanged && update.view.hasFocus) onFocus?.();
        if (update.docChanged && !silent) onChange?.(serializeSource(update.state.doc.toString()), update);
        if (update.selectionSet || update.docChanged) {
          onSelection?.(selectionSnapshot(update.state));
        }
      }),
      baseTheme,
    ],
  });
  parent.dataset.mode = mode;
  view = new EditorView({ state, parent });
  const sourceReadiness = mode === 'source';
  if (sourceReadiness) {
    parent.dataset.syntaxReady = 'loading';
    parent.setAttribute('aria-busy', 'true');
    parent.style.visibility = 'hidden';
  }
  const reveal = (ready: boolean) => {
    if (destroyed || !sourceReadiness) return;
    const show = () => {
      if (destroyed) return;
      parent.dataset.syntaxReady = ready ? 'ready' : 'plain';
      parent.setAttribute('aria-busy', 'false');
      parent.style.visibility = '';
      // A source view can finish parsing while its host is still hidden. A
      // direct DOM focus attempt during that window is discarded by the
      // browser; restore focus only when the user has not moved focus to a
      // different control in the meantime.
      if (typeof document !== 'undefined'
        && (document.activeElement === document.body || parent.contains(document.activeElement))) {
        view.focus();
      }
    };
    if (typeof requestAnimationFrame === 'function') requestAnimationFrame(show);
    else setTimeout(show, 0);
  };
  const languageReady = mode === 'source'
    ? loadSourceLanguage(options.language, regionOptions).then((language) => {
      if (destroyed) return false;
      const hasLanguage = Array.isArray(language) ? language.length > 0 : Boolean(language);
      if (hasLanguage) {
        view.dispatch({ effects:languageCompartment.reconfigure(language) });
        forceParsing(view, view.viewport.to, 120);
      }
      const ready = !hasLanguage || syntaxTreeAvailable(view.state, view.viewport.to);
      reveal(ready);
      return ready;
    }).catch(() => {
      reveal(false);
      return false;
    })
    : Promise.resolve(true);
  if (Number.isFinite(options.scrollTop)) view.scrollDOM.scrollTop = Math.max(0, Number(options.scrollTop));
  const reportScroll = () => onScroll?.(view.scrollDOM.scrollTop);
  view.scrollDOM.addEventListener('scroll', reportScroll, { passive:true });

  function selectionForLength(length: number) {
    const current = selectionSnapshot(view.state);
    return normalizeEditorSelection(current, length);
  }

  function setSelection(value: SelectionInput | null | undefined) {
    const normalized = normalizeEditorSelection(value, view.state.doc.length);
    view.dispatch({ selection:EditorSelection.create(normalized.ranges.map((range) => EditorSelection.range(range.anchor, range.head)), normalized.mainIndex) });
    view.focus();
    return true;
  }

  function setValue(value: string, nextSelection?: SelectionInput | null) {
    const next = String(value ?? '');
    const nextLength = logicalDocumentLength(next);
    const normalized = normalizeEditorSelection(nextSelection || selectionForLength(nextLength), nextLength);
    if (next === view.state.doc.toString()) {
      if (nextSelection) setSelection(normalized);
      return;
    }
    silent = true;
    view.dispatch({
      changes:{ from:0, to:view.state.doc.length, insert:next },
      selection:EditorSelection.create(normalized.ranges.map((range) => EditorSelection.range(range.anchor, range.head)), normalized.mainIndex),
      annotations:Transaction.addToHistory.of(false),
    });
    silent = false;
  }

  function applyValue(value: string, nextSelection?: SelectionInput | null) {
    const next = String(value ?? '');
    const nextLength = logicalDocumentLength(next);
    const normalized = normalizeEditorSelection(nextSelection || selectionForLength(nextLength), nextLength);
    view.dispatch({
      changes:{ from:0, to:view.state.doc.length, insert:next },
      selection:EditorSelection.create(normalized.ranges.map((range) => EditorSelection.range(range.anchor, range.head)), normalized.mainIndex),
    });
  }

  function replaceSelections(replacement: string | ((text: string, range: SelectionRange, index: number) => string), userEvent = 'input') {
    let rangeIndex = 0;
    const result = view.state.changeByRange((range) => {
      const index = rangeIndex++;
      const text = view.state.doc.sliceString(range.from, range.to);
      const insert = typeof replacement === 'function' ? String(replacement(text, range, index) ?? '') : String(replacement ?? '');
      const anchor = range.anchor <= range.head ? range.from + insert.length : range.from;
      const head = range.anchor <= range.head ? range.from + insert.length : range.from;
      return { changes:{ from:range.from, to:range.to, insert }, range:EditorSelection.range(anchor, head) };
    });
    if (result.changes.empty) return false;
    view.dispatch({ ...result, userEvent });
    view.focus();
    return true;
  }

  function insertText(text: string) {
    return replaceSelections(String(text ?? ''));
  }

  function formatSelections(prefix: string, suffix = prefix) {
    const left = String(prefix ?? '');
    const right = String(suffix ?? '');
    const result = view.state.changeByRange((range) => {
      const text = view.state.doc.sliceString(range.from, range.to);
      const insert = `${left}${text}${right}`;
      const forward = range.anchor <= range.head;
      const anchor = forward ? range.from + left.length : range.from + left.length + text.length;
      const head = forward ? range.from + left.length + text.length : range.from + left.length;
      return { changes:{ from:range.from, to:range.to, insert }, range:EditorSelection.range(anchor, head) };
    });
    if (result.changes.empty) return false;
    view.dispatch({ ...result, userEvent:'input.format' });
    view.focus();
    return true;
  }

  function selectedTexts() {
    return view.state.selection.ranges.map((range) => view.state.doc.sliceString(range.from, range.to));
  }

  function pasteText(text: string) {
    const value = String(text ?? '');
    const ranges = view.state.selection.ranges;
    const pieces = ranges.length > 1 && value.includes('\n') && value.split(/\r?\n/).length === ranges.length
      ? value.split(/\r?\n/) : ranges.map(() => value);
    let rangeIndex = 0;
    const result = view.state.changeByRange((range) => {
      const index = rangeIndex++;
      const insert = pieces[index] ?? value;
      return { changes:{ from:range.from, to:range.to, insert }, range:EditorSelection.cursor(range.from + insert.length) };
    });
    if (result.changes.empty) return false;
    view.dispatch({ ...result, userEvent:'input.paste' });
    view.focus();
    return true;
  }

  function replaceRange(from: number, to: number, text: string) {
    const start = boundedOffset(from, view.state.doc.length);
    const end = Math.max(start, boundedOffset(to, view.state.doc.length));
    const value = String(text ?? '');
    const changes = { from:start, to:end, insert:value };
    const changeSet = view.state.changes(changes);
    const sourceRanges = view.state.selection.ranges;
    // Locate the edited selection in the pre-change coordinate space. Mapping
    // first moves a nonempty target to the replacement's new endpoint, so
    // comparing mapped offsets with `start`/`end` loses that target and leaves
    // the primary selection pointing at an unrelated cursor.
    const target = sourceRanges.findIndex((range) => range.from === start && range.to === end);
    const ranges = sourceRanges.map((range) => EditorSelection.range(range.anchor, range.head).map(changeSet));
    if (target >= 0) ranges[target] = EditorSelection.cursor(start + value.length);
    view.dispatch({ changes, selection:EditorSelection.create(ranges, target >= 0 ? target : view.state.selection.mainIndex), userEvent:'input' });
    view.focus();
    return true;
  }

  function selectNextMatch(query?: string) {
    const needle = String(query ?? selectedTexts()[view.state.selection.mainIndex] ?? '');
    if (!needle) return false;
    const text = view.state.doc.toString();
    const existing = new Set(view.state.selection.ranges.map((range) => `${range.from}:${range.to}`));
    const main = view.state.selection.main;
    let index = text.indexOf(needle, Math.max(main.to, main.from + (main.empty ? 1 : 0)));
    if (index < 0) index = text.indexOf(needle, 0);
    while (index >= 0 && existing.has(`${index}:${index + needle.length}`)) {
      index = text.indexOf(needle, index + Math.max(1, needle.length));
    }
    if (index < 0) return false;
    const ranges = view.state.selection.ranges.map((range) => EditorSelection.range(range.anchor, range.head));
    ranges.push(EditorSelection.range(index, index + needle.length));
    view.dispatch({ selection:EditorSelection.create(ranges), scrollIntoView:true });
    view.focus();
    return true;
  }

  function selectAllMatches(query?: string) {
    const needle = String(query ?? selectedTexts()[view.state.selection.mainIndex] ?? '');
    if (!needle) return false;
    const text = view.state.doc.toString();
    const ranges: Array<ReturnType<typeof EditorSelection.range>> = [];
    for (let index = 0; index <= text.length - needle.length;) {
      const found = text.indexOf(needle, index);
      if (found < 0) break;
      ranges.push(EditorSelection.range(found, found + needle.length));
      index = found + Math.max(1, needle.length);
    }
    if (!ranges.length) return false;
    const current = view.state.selection.main;
    const mainIndex = Math.max(0, ranges.findIndex((range) => range.from === current.from && range.to === current.to));
    view.dispatch({ selection:EditorSelection.create(ranges, mainIndex), scrollIntoView:true });
    view.focus();
    return true;
  }

  function duplicateLines() {
    const lines = new Set<number>();
    for (const range of view.state.selection.ranges) {
      const first = view.state.doc.lineAt(range.from).number;
      const last = view.state.doc.lineAt(range.to).number;
      for (let number = first; number <= last; number += 1) lines.add(number);
    }
    const changes = [...lines].sort((a, b) => a - b).map((number) => {
      const line = view.state.doc.line(number);
      // Insert after the line so its existing terminator remains the first
      // separator; replacing the text before that terminator creates a blank
      // line and loses the duplicated content.
      return { from:line.to, to:line.to, insert:`${view.state.lineBreak}${line.text}` };
    });
    if (!changes.length) return false;
    const changeSet = view.state.changes(changes);
    const selection = view.state.selection;
    const mappedRanges = selection.ranges.map((range) => EditorSelection.range(range.anchor, range.head).map(changeSet));
    view.dispatch({ changes, selection:EditorSelection.create(mappedRanges, selection.mainIndex), userEvent:'input.duplicate' });
    view.focus();
    return true;
  }

  runEditorCommand = (name) => {
    if (name === 'add-cursor-above') return addCursorAbove(view);
    if (name === 'add-cursor-below') return addCursorBelow(view);
    if (name === 'select-next-match') return selectNextMatch();
    if (name === 'select-all-matches') return selectAllMatches();
    if (name === 'indent') return indentSelection({ state:view.state, dispatch:(transaction) => view.dispatch(transaction) });
    if (name === 'duplicate-line') return duplicateLines();
    if (name === 'collapse-selections') {
      if (view.state.selection.ranges.length < 2) return false;
      const main = view.state.selection.main;
      view.dispatch({ selection:EditorSelection.create([EditorSelection.range(main.anchor, main.head)], 0) });
      view.focus();
      return true;
    }
    return false;
  };

  function setMode(next: EditorMode) {
    const safe = next === 'source' ? 'source' : 'live';
    if (safe === mode) return;
    mode = safe;
    parent.dataset.mode = mode;
    view.dispatch({ effects:modeCompartment.reconfigure(mode === 'live'
      ? [createStructuralDecorations(renderPreview), createLivePreviewPlugin(renderPreview)]
      : options.richComments === true && isRichCommentLanguageQualified(options.language, regionOptions) ? [createRichCommentPlugin(options.language || '', renderPreview, regionOptions, options.onSeeSource)] : []) });
  }

  function setLineNumbers(next: boolean) {
    if (next === showLineNumbers) return;
    showLineNumbers = next;
    view.dispatch({ effects:gutterCompartment.reconfigure(showLineNumbers ? [lineNumbers()] : []) });
  }

  function setReadableLineWidth(next: boolean) {
    if (next === readableLineWidth) return;
    readableLineWidth = next;
    view.dispatch({ effects:widthCompartment.reconfigure(widthExtension(readableLineWidth)) });
  }

  function find(query: string, backwards = false) {
    const needle = String(query || '');
    if (!needle) return false;
    const text = view.state.doc.toString();
    const cursor = view.state.selection.main;
    let index = backwards ? text.lastIndexOf(needle, Math.max(0, cursor.from - 1)) : text.indexOf(needle, cursor.to);
    if (index < 0) index = backwards ? text.lastIndexOf(needle) : text.indexOf(needle);
    if (index < 0) return false;
    view.dispatch({ selection:{ anchor:index, head:index + needle.length }, effects:EditorView.scrollIntoView(index, { y:'center' }) });
    view.focus();
    return true;
  }

  function replace(query: string, replacement: string, all = false) {
    const needle = String(query || '');
    if (!needle) return 0;
    const text = view.state.doc.toString();
    if (all) {
      const changes: Array<{ from:number; to:number; insert:string }> = [];
      for (let index = text.indexOf(needle); index >= 0; index = text.indexOf(needle, index + Math.max(1, needle.length))) {
        changes.push({ from:index, to:index + needle.length, insert:String(replacement ?? '') });
      }
      const count = changes.length;
      if (count) view.dispatch({ changes, userEvent:'input.replace' });
      return count;
    }
    const ranges = view.state.selection.ranges;
    if (ranges.length > 1 && ranges.every((range) => text.slice(range.from, range.to) === needle)) {
      replaceSelections(String(replacement ?? ''), 'input.replace');
      return ranges.length;
    }
    const selection = view.state.selection.main;
    if (text.slice(selection.from, selection.to) !== needle && !find(needle)) return 0;
    const current = view.state.selection.main;
    view.dispatch({ changes:{ from:current.from, to:current.to, insert:String(replacement ?? '') }, userEvent:'input.replace' });
    return 1;
  }

  function focusLine(line: number) {
    const bounded = Math.max(1, Math.min(Number(line) || 1, view.state.doc.lines));
    const target = view.state.doc.line(bounded);
    view.dispatch({ selection:EditorSelection.range(target.from, target.to), scrollIntoView:true });
    view.focus();
    return target;
  }

  return {
    view,
    getValue:() => serializeSource(view.state.doc.toString()), setValue, applyValue, setSelection, insertText, replaceSelections, formatSelections,
    pasteText, replaceRange, selectedTexts, getSelectedText:() => selectedTexts().join('\n'), selectNextMatch, selectAllMatches,
    duplicateLines, runCommand:runEditorCommand, setMode, setLineNumbers, setReadableLineWidth,
    getScrollTop:() => view.scrollDOM.scrollTop,
    focus:() => view.focus(), focusLine,
    undo:() => undo(view), redo:() => redo(view), find, replace,
    getSelection:() => selectionSnapshot(view.state),
    wrapMarkdownAsComment:(markdown: string, indent = '') => wrapMarkdownAsComment(markdown, options.language || '', indent, regionOptions),
    safeCommentInsertion:(offset: number) => safeCommentInsertion(view.state.doc.toString(), options.language || '', offset, regionOptions),
    revealCommentSource:(from: number, to: number) => {
      view.dispatch({ selection:EditorSelection.range(from, to), scrollIntoView:true });
      view.focus();
    },
    getCommentSourceMap:() => mapParserComments(view.state, options.language || '', undefined, regionOptions),
    getCommentSourceMapAsync:() => parserCommentSourceRangesAsync(view.state, options.language || '', regionOptions).then((ranges) => ranges.map((range) => ({ ...range, markdown:range.contentRanges.map((span) => view.state.doc.sliceString(span.from, span.to)).join('\n') }))),
    languageReady,
    destroy:() => { destroyed = true; view.destroy(); },
  };
}

// Compatibility adapter for the existing Copal Notes surface. Keeping this
// public name lets Notes evolve independently while both products share one
// CodeMirror runtime and dependency graph.
export function createMarkdownEditor(options: MarkdownEditorOptions) {
  return createOpenClankEditor(options);
}

// Source-specific adapter for Code Editor. Product code still owns buffers,
// CAS, paths, and permissions; this adapter owns only editor presentation and
// state. Explicit defaults keep source files unwrapped and free of Markdown's
// live structural widgets without creating a second CodeMirror copy.
export function createSourceEditor(options: MarkdownEditorOptions) {
  return createOpenClankEditor({
    ...options,
    mode:'source',
    readableLineWidth:false,
    lineWrapping:false,
  });
}

// Documentation-region adapters are part of this bundle's public surface so
// Code Editor, Copal Editor and image-paste insertion share one authority for
// comment boundaries, docstrings and safe insertion points.
export {
  ADVERTISED_LANGUAGE_LABELS,
  advertisedLabel,
  dialectForPath,
  documentationRegions,
  documentationRegionsAsync,
  languageMatrix,
  regionMarkdown,
  reconstructFromRegions,
  safeCommentInsertion,
  supportsRichComments,
  wrapMarkdownAsComment,
} from './openclank-doc-regions';
export type { ContentSpan, DelimiterKind, DocumentationRegion, SafeInsertionResult, CommentWrapResult } from './openclank-doc-regions';
