import { addCursorAbove, addCursorBelow, defaultKeymap, history, historyKeymap, undoDepth, redoDepth, indentSelection, indentWithTab, redo, undo } from '@codemirror/commands';
import { markdown } from '@codemirror/lang-markdown';
import { collectSpellingScope, type SpellingToken } from './openclank-spelling-scope';
import { loadSourceLanguage as loadRegisteredSourceLanguage, sourceLanguageMetadata, sourceCommentCapability, LANGUAGE_REGISTRY } from './openclank-source-languages';
import { sourceCommentRegions, serializeCommentBody, safeSourceCommentInsertion, wrapSourceComment, type SourceCommentRegion } from './openclank-comment-regions';
import { CommentInsetWidget, type CommentInsetOptions } from './openclank-comment-widget';
import { HighlightStyle, Language, LanguageDescription, LanguageSupport, bracketMatching, defaultHighlightStyle, foldGutter, forceParsing, indentOnInput, syntaxHighlighting, syntaxTree, syntaxTreeAvailable } from '@codemirror/language';
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
  documentationRegionsFromTree,
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

const sourceSnapshots = new WeakMap<object, string>();
function documentSource(state: EditorState) {
  let source = sourceSnapshots.get(state.doc);
  if (source === undefined) { source = state.doc.toString(); sourceSnapshots.set(state.doc, source); }
  return source;
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
  languageOverride?: string;
  lineNumbers?: boolean;
  readableLineWidth?: boolean;
  lineWrapping?: boolean;
  onChange?: (value: string, update: ViewUpdate, edit?:{ origin:string }) => void;
  onFocus?: () => void;
  historyOwner?: { undo:() => boolean; redo:() => boolean; state:() => { canUndo:boolean; canRedo:boolean; historyNotice?:string } };
  getLocalRevision?: () => number;
  registerPendingSourceEdit?: (edit:{ id:string; body:string; expectedSource:string; expectedLocalRevision:number; flush?:() => { outcome:string; message?:string }; discard?:() => void }) => boolean;
  getPendingSourceEdit?: (id:string) => { id:string; body:string; expectedSource:string; expectedLocalRevision:number; flush?:() => { outcome:string; message?:string }; discard?:() => void } | null;
  getPendingSourceEdits?: () => Array<{ id:string; body:string; expectedSource:string; expectedLocalRevision:number; flush?:() => { outcome:string; message?:string }; discard?:() => void }>;
  removePendingSourceEdit?: (id:string) => boolean;
  applySourceTransaction?: (transform:(source:string) => string, options?:{ origin?:string; expectedLocalRevision?:number; expectedSource?:string }) => unknown;
  onNotice?: (message:string) => void;
  onSelection?: (selection: EditorSelectionSnapshot, update?:ViewUpdate) => void;
  onScroll?: (scrollTop: number) => void;
  onCommand?: (command: string) => boolean | void;
  onSyntaxStatus?: (status: { state:string; message:string; retryable:boolean }) => void;
  /** Render a resolved Markdown image/embed source without coupling CodeMirror to resource resolution. */
  renderPreview?: (source: string) => HTMLElement | null;
  /** Full prose renderer for comment bodies; standalone embeds keep renderPreview. */
  renderComment?: (source:string, editBody?:(body:string)=>void) => HTMLElement | null;
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
// Capability comes from the mounted language metadata. Plain and strict JSON
// have no comments; unresolved syntax is never promoted by text heuristics.
export function isRichCommentLanguageQualified(language?: string, options: { dialect?: string; path?: string } = {}) {
  return sourceCommentCapability(language,options).strategy !== 'none';
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
  const source = documentSource(state);
  return documentationRegions(source, language, options).map((region) => regionToCommentSourceRange(region, language));
}

/**
 * Grammar-aware regions including Python docstrings. Async because Lezer
 * parsers load on demand; stale results are the caller's revision problem.
 */
export async function parserCommentSourceRangesAsync(state: EditorState, language = '', options: { dialect?: string; path?: string } = {}): Promise<CommentSourceRange[]> {
  if (!state || !String(language || '').trim()) return [];
  const source = documentSource(state);
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

const fencedLanguages = LANGUAGE_REGISTRY.filter(entry => entry.selectable && entry.parser !== 'plain').map(entry => LanguageDescription.of({
  name:entry.displayName, alias:[entry.id, ...entry.aliases],
  load:async () => {
    const extension = await loadRegisteredSourceLanguage(entry.id);
    if (extension instanceof LanguageSupport) return extension;
    if (extension instanceof Language) return new LanguageSupport(extension);
    throw new Error(`No embeddable syntax grammar for ${entry.displayName}`);
  },
}));
function markdownSupport() { return markdown({ codeLanguages:fencedLanguages }); }
function initialLanguage(language?: string) {
  return String(language || '').toLowerCase() === 'markdown' ? markdownSupport() : [];
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

// Only reuse an existing visible projection for cheap context-menu eligibility.
const visibleCommentProjections = new WeakMap<EditorView, { doc:EditorState['doc']; tree:unknown; language:string; ranges:CommentSourceRange[] }>();
function visibleCommentRanges(view: EditorView, language: string, regionOptions: { dialect?:string; path?:string }) {
  const tree = syntaxTree(view.state);
  const entry=sourceLanguageMetadata(language,regionOptions);
  const ranges = sourceCommentRegions(view.state,entry.id,[view.viewport],documentSource(view.state));
  visibleCommentProjections.set(view, { doc:view.state.doc, tree, language, ranges });
  return ranges;
}

const commentSourceReveal = StateEffect.define<Array<{from:number;to:number}>>();
function buildCommentDecorations(state: EditorState, language: string, renderPreview?: (source:string) => HTMLElement | null, visibleRanges: readonly { from:number; to:number }[] = [], comments: SourceCommentRegion[] = [], onSeeSource?: (range: { from:number; to:number; source:string; markdown:string }, event: Event) => void, inset?:CommentInsetOptions, revealed:readonly {from:number;to:number}[]=[]): DecorationSet {
  const active = state.selection.ranges;
  const decorations = comments.flatMap(comment => {
    if (!visibleRanges.some(visible => visible.from < comment.to && visible.to > comment.from)) return [];
    if (revealed.some(range=>range.from<=comment.to&&range.to>=comment.from) || active.some(selection => selection.from < selection.to && selection.from <= comment.to && selection.to >= comment.from)) return [];
    const attributes = { 'data-comment-source-from':String(comment.from), 'data-comment-source-to':String(comment.to) };
    if (!renderPreview) return [{ from:comment.from, to:comment.to, value:Decoration.mark({ class:'cm-rich-comment-source', attributes }) }];
    const source = state.doc.sliceString(comment.from, comment.to);
    const markdown = comment.contentRanges.map(span => state.doc.sliceString(span.from, span.to)).join('\n');
    return [{ from:comment.from, to:comment.to, value:Decoration.replace({ widget:inset?new CommentInsetWidget(comment,inset):new CommentPreviewWidget(source, markdown, comment.from, comment.to, renderPreview, onSeeSource), block:source.includes('\n') }) }];
  });
  return Decoration.set(decorations, true);
}

/** Opt-in parser-backed comment presentation with a safe source fallback. */
function createRichCommentPlugin(language: string, renderPreview?: (source:string) => HTMLElement | null, regionOptions: { dialect?:string; path?:string } = {}, onSeeSource?: (range: { from:number; to:number; source:string; markdown:string }, event: Event) => void, inset?:CommentInsetOptions) {
  const projection=StateEffect.define<{doc:EditorState['doc'];regions:SourceCommentRegion[];windows:Array<{from:number;to:number}>}>();
  const field=StateField.define<{decorations:DecorationSet;regions:SourceCommentRegion[];windows:Array<{from:number;to:number}>;revealed:Array<{from:number;to:number}>}>({
    create:()=>({decorations:Decoration.none,regions:[],windows:[],revealed:[]}),
    update(value,transaction){
      let regions=value.regions,windows=value.windows,revealed=value.revealed;
      if(transaction.docChanged){
        const source=documentSource(transaction.state);
        regions=regions.flatMap(region=>{
          const from=transaction.changes.mapPos(region.from,1),to=transaction.changes.mapPos(region.to,-1);
          if(source.slice(from,to)!==region.sourceText)return [];
          const map=<T extends {from:number;to:number}>(span:T):T=>({...span,from:transaction.changes.mapPos(span.from,1),to:transaction.changes.mapPos(span.to,-1)});
          return [{...region,from,to,sourceDocument:transaction.state.doc,contentFrom:transaction.changes.mapPos(region.contentFrom,1),contentTo:transaction.changes.mapPos(region.contentTo,-1),contentRanges:region.contentRanges.map(map),bodySpans:region.bodySpans.map(map),lineStart:transaction.state.doc.lineAt(from).from,lineEnd:transaction.state.doc.lineAt(to).to}];
        });
        windows=windows.map(range=>({from:transaction.changes.mapPos(range.from,1),to:transaction.changes.mapPos(range.to,-1)}));
        revealed=revealed.map(range=>({from:transaction.changes.mapPos(range.from,1),to:transaction.changes.mapPos(range.to,-1)}));
      }
      for(const effect of transaction.effects){
        if(effect.is(projection)&&effect.value.doc===transaction.state.doc){regions=effect.value.regions;windows=effect.value.windows;}
        if(effect.is(commentSourceReveal))revealed=effect.value;
      }
      if(transaction.selection)revealed=revealed.filter(range=>transaction.state.selection.ranges.some(selection=>selection.from<=range.to&&selection.to>=range.from));
      return {regions,windows,revealed,decorations:buildCommentDecorations(transaction.state,language,renderPreview,windows,regions,onSeeSource,inset,revealed)};
    },
    provide:field=>EditorView.decorations.from(field,value=>value.decorations),
  });
  const viewport=ViewPlugin.fromClass(class {
    tree:unknown;frame:number|null=null;stopped=false;
    constructor(view:EditorView){this.tree=syntaxTree(view.state);this.schedule(view);}
    schedule(view:EditorView){if(this.frame!==null)return;this.frame=requestAnimationFrame(()=>{this.frame=null;if(this.stopped)return;this.tree=syntaxTree(view.state);const regions=visibleCommentRanges(view,language,regionOptions);view.dispatch({effects:projection.of({doc:view.state.doc,regions,windows:[view.viewport]}),annotations:Transaction.addToHistory.of(false)});});}
    update(update:ViewUpdate){if(update.docChanged||update.viewportChanged||syntaxTree(update.state)!==this.tree||update.transactions.some(transaction=>transaction.reconfigured))this.schedule(update.view);}
    destroy(){this.stopped=true;if(this.frame!==null)cancelAnimationFrame(this.frame);}
  });
  return [field,viewport,EditorView.atomicRanges.of(view=>view.state.field(field).decorations)];
}

// Explicit checks own short-lived diagnostics; edits, selections and grammar
// reconfiguration clear them rather than mapping results onto different prose.
const spellingMarks = StateEffect.define<SpellingToken[]>();
const spellingField = StateField.define<DecorationSet>({
  create:() => Decoration.none,
  update(value, transaction) {
    if (transaction.docChanged || transaction.selection || transaction.reconfigured) value = Decoration.none;
    for (const effect of transaction.effects) if (effect.is(spellingMarks)) value = Decoration.set(effect.value.map(token => Decoration.mark({ class:'cm-spelling-error', attributes:{ title:'Possible spelling error' } }).range(token.from,token.to)),true);
    return value;
  },
  provide:field => EditorView.decorations.from(field),
});

const baseTheme = EditorView.theme({
  '&': { height:'100%', color:'var(--fg)', backgroundColor:'transparent', fontSize:'14px' },
  '.cm-scroller': { fontFamily:'var(--font-family, system-ui, sans-serif)', lineHeight:'1.72', overflow:'auto' },
  '.cm-content': { padding:'28px clamp(18px, 5vw, 72px)', caretColor:'var(--accent, #22d3ee)', width:'100%' },
  '.cm-line': { padding:'0 4px' },
  '.cm-spelling-error': { textDecoration:'underline wavy var(--danger, #dc5757)', textUnderlineOffset:'3px' },
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
  let languageOverride = options.languageOverride || null;
  let grammarOptions = { dialect:options.languageDialect, path:options.languagePath, content:String(doc).slice(0, 4096) };
  let languageMetadata = sourceLanguageMetadata(languageOverride || (options.languagePath && String(options.language).toLowerCase() !== 'markdown' ? undefined : options.language || 'Markdown'), grammarOptions);
  // Qualification follows the resolved language, never the old filename dialect.
  let regionOptions = { dialect:languageMetadata.id === 'jsonc' ? 'jsonc' : undefined, path:undefined as string | undefined };
  const loadLanguage = () => languageMetadata.id === 'markdown' ? Promise.resolve(markdownSupport()) : loadRegisteredSourceLanguage(languageMetadata.id, grammarOptions);
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
  const command = (name: string) => typeof onCommand === 'function' && onCommand(name) !== false;
  let originalSource = String(doc);
  let richComments = options.richComments === true;
  const serializeSource = (value: string) => serializeEditorSource(value, originalSource);
  const pendingPanel=document.createElement('details');pendingPanel.className='cm-rich-comment-pending';pendingPanel.hidden=true;
  function syncPendingPanel(){
    const pending=options.getPendingSourceEdits?.()||[];
    const open=pendingPanel.open;pendingPanel.replaceChildren();pendingPanel.hidden=!pending.length;pendingPanel.open=open;
    if(!pending.length)return;
    const summary=document.createElement('summary');summary.textContent=`${pending.length} pending comment edit${pending.length===1?'':'s'} — review before saving`;pendingPanel.append(summary);
    for(const edit of pending){
      const body=document.createElement('pre');body.textContent=edit.body;
      const discard=document.createElement('button');discard.type='button';discard.textContent='Discard this pending edit';discard.addEventListener('click',()=>{edit.discard?.();options.removePendingSourceEdit?.(edit.id);syncPendingPanel();});
      const copy=document.createElement('button');copy.type='button';copy.textContent='Copy pending body';copy.addEventListener('click',()=>{void navigator.clipboard?.writeText(edit.body);});
      pendingPanel.append(body,copy,discard);
    }
  }
  const state = EditorState.create({
    doc,
    selection:cmSelection,
    extensions:[
      gutterCompartment.of(showLineNumbers ? [lineNumbers()] : []),
      highlightSpecialChars(), ...(options.historyOwner ? [] : [history({ minDepth:2000 })]), foldGutter(), drawSelection(), dropCursor(), spellingField,
      EditorState.allowMultipleSelections.of(true), indentOnInput(),
      ...(lineSeparator ? [EditorState.lineSeparator.of(lineSeparator)] : []),
      // Open Clank is the owned highlighter. CodeMirror's default remains only as
      // a true fallback for syntax nodes our semantic palette does not cover; if
      // both styles are registered as fallbacks, the default can win the facet and
      // prevent the live --hl-* theme variables from reaching token spans.
      syntaxHighlighting(openClankHighlightStyle), syntaxHighlighting(defaultHighlightStyle, { fallback:true }), bracketMatching(), rectangularSelection(),
      highlightActiveLine(), languageCompartment.of(initialLanguage(languageMetadata.id)), ...(lineWrapping ? [EditorView.lineWrapping] : []), placeholder(placeholderText),
      modeCompartment.of(mode === 'live' && languageMetadata.id === 'markdown' ? [createStructuralDecorations(renderPreview), createLivePreviewPlugin(renderPreview)] : []),
      widthCompartment.of(widthExtension(readableLineWidth)),
      keymap.of([
        { key:'Mod-z', run:() => options.historyOwner ? options.historyOwner.undo() : undo(view), preventDefault:true },
        { key:'Mod-Shift-z', run:() => options.historyOwner ? options.historyOwner.redo() : redo(view), preventDefault:true },
        { key:'Ctrl-y', run:() => options.historyOwner ? options.historyOwner.redo() : redo(view), preventDefault:true },
        { key:'Mod-s', run:() => command('save') },
        { key:'Mod-o', run:() => command('quick-open') },
        { key:'Mod-p', run:() => command('palette') },
        { key:'Mod-Shift-f', run:() => command('search') },
        // Shared platform map: Mod is Cmd on macOS and Ctrl elsewhere.
        // Mod-d selects the next occurrence, Mod-Shift-l selects all, and
        // Mod-Alt-arrow adds a cursor vertically. Escape collapses extras.
        { key:'Mod-d', run:() => { runEditorCommand('select-next-match'); return true; } },
        { key:'Mod-Shift-l', run:() => { runEditorCommand('select-all-matches'); return true; } },
        { key:'Mod-Alt-ArrowUp', run:() => runEditorCommand('add-cursor-above') },
        { key:'Mod-Alt-ArrowDown', run:() => runEditorCommand('add-cursor-below') },
        { key:'Mod-Alt-\\', run:() => runEditorCommand('indent') },
        { key:'Mod-Shift-d', run:() => runEditorCommand('duplicate-line') },
        { key:'Escape', run:() => runEditorCommand('collapse-selections') },
        indentWithTab, ...defaultKeymap, ...historyKeymap,
      ]),
      EditorView.domEventHandlers({ beforeinput:(event) => {
        if (!options.historyOwner || !['historyUndo', 'historyRedo'].includes(event.inputType)) return false;
        event.preventDefault();
        event.inputType === 'historyUndo' ? options.historyOwner.undo() : options.historyOwner.redo();
        return true;
      } }),
      EditorView.contentAttributes.of({ 'aria-label':label, spellcheck:'false' }),
      EditorView.updateListener.of((update) => {
        if (update.focusChanged && update.view.hasFocus) onFocus?.();
        if (update.docChanged && !silent) onChange?.(serializeSource(documentSource(update.state)), update, { origin:update.transactions.every(transaction => transaction.isUserEvent('input.type')) ? 'typing' : 'transaction' });
        if (update.selectionSet || update.docChanged) {
          onSelection?.(selectionSnapshot(update.state), update);
        }
      }),
      baseTheme,
    ],
  });
  parent.dataset.mode = mode;
  view = new EditorView({ state, parent });
  parent.insertBefore(pendingPanel,view.dom);syncPendingPanel();
  let sourceReadiness = mode === 'source';
  let syntaxStatus = { state:sourceReadiness ? 'loading' : 'ready', message:sourceReadiness ? 'Loading syntax…' : 'Syntax ready', retryable:false };
  let syntaxGeneration = 0;
  let grammarLoaded = !sourceReadiness;
  let syntaxFrame: number | null = null;
  let syntaxDeadline: ReturnType<typeof setTimeout> | null = null;
  let focusPending = false;
  let resolveSyntax: ((value:boolean) => void) | null = null;
  const syntaxLabel = document.createElement('div');
  syntaxLabel.setAttribute('role', 'status'); syntaxLabel.setAttribute('aria-live', 'polite');
  syntaxLabel.className = 'cm-syntax-status';
  syntaxLabel.style.cssText = 'padding:8px 12px;font:inherit';
  if (sourceReadiness) { parent.insertBefore(syntaxLabel, view.dom); view.dom.style.visibility = 'hidden'; }
  function reportSyntax(next: typeof syntaxStatus) {
    if (destroyed) return;
    syntaxStatus = next; parent.dataset.syntaxReady = next.state;
    parent.setAttribute('aria-busy', String(next.state === 'loading'));
    syntaxLabel.textContent = next.message;
    syntaxLabel.hidden = next.state === 'ready';
    if (next.retryable) {
      const retry = document.createElement('button'); retry.type = 'button'; retry.textContent = 'Retry syntax';
      retry.addEventListener('click', () => { void retrySyntax(); }); syntaxLabel.append(' ', retry);
    }
    options.onSyntaxStatus?.({ ...next });
  }
  function revealSyntax(next: typeof syntaxStatus) {
    if (destroyed) return;
    if (syntaxDeadline != null) clearTimeout(syntaxDeadline); syntaxDeadline = null;
    view.dom.style.visibility = ''; reportSyntax(next);
    resolveSyntax?.(next.state === 'ready'); resolveSyntax = null;
    if (focusPending && view.dom.isConnected && (document.activeElement === document.body || parent.contains(document.activeElement))) { focusPending = false; view.focus(); }
  }
  function checkSyntax() {
    if (destroyed || !grammarLoaded || syntaxStatus.state === 'ready' || syntaxFrame != null) return;
    syntaxFrame = requestAnimationFrame(() => {
      syntaxFrame = null;
      if (destroyed || !view.dom.isConnected || !grammarLoaded) return;
      // Visibility preserves geometry. Measure the mounted/restored viewport,
      // then let CM publish its incremental tree before the reveal frame.
      const generation = syntaxGeneration;
      view.requestMeasure({ read:() => view.viewport.to, write:(to) => {
        if (destroyed || generation !== syntaxGeneration || !grammarLoaded) return;
        if (syntaxTreeAvailable(view.state, to)) revealSyntax({ state:'ready', message:'Syntax ready', retryable:false });
      } });
    });
  }
  const syntaxObserver = EditorView.updateListener.of(() => checkSyntax());
  view.dispatch({ effects:StateEffect.appendConfig.of(syntaxObserver) });
  function presentationExtensions() {
    if (!grammarLoaded) return [];
    const markdownPresentation=mode==='live'&&(languageMetadata.id==='markdown'||languageMetadata.parser.startsWith('markdown:'))?[createStructuralDecorations(renderPreview),createLivePreviewPlugin(renderPreview)]:[];
    const commentRender=options.renderComment||renderPreview;
    const inset:CommentInsetOptions={
      render:(body,editBody)=>commentRender?.(body,editBody)||null,
      createBodyEditor:(parent,body,onChange)=>createOpenClankEditor({parent,doc:body,language:'Markdown',mode:'live',lineNumbers:false,readableLineWidth:false,richComments:false,renderPreview,onChange:value=>onChange(value),onCommand:name=>{if(name==='save')return command('save');return false;}}),
      capture:target=>({expectedSource:serializeSource(documentSource(target.state)),expectedLocalRevision:options.getLocalRevision?.()||0}),
      apply:(target,region,body,capture)=>{
        const serialized=serializeCommentBody(documentSource(target.state),region,body);
        if(serialized.ok===false)return {ok:false,error:serialized.error};
        try {
          if(options.applySourceTransaction){
            const result:any=options.applySourceTransaction(raw=>{
              const logical=raw.replace(/\r\n?|\n/g,'\n');
              const edit=serializeCommentBody(logical,region,body);if(edit.ok===false)throw new Error(edit.error);
              // Translate logical UTF-16 offsets to raw offsets; only this region changes.
              const rawOffset=(offset:number)=>{let logicalOffset=0,index=0;while(index<raw.length&&logicalOffset<offset){if(raw[index]==='\r'&&raw[index+1]==='\n')index++;index++;logicalOffset++;}return index;};
              const from=rawOffset(edit.change.from),to=rawOffset(edit.change.to);
              return raw.slice(0,from)+serializeEditorSource(edit.change.insert,raw.slice(from,to))+raw.slice(to);
            },{...capture,origin:'comment'});
            if(result?.outcome==='failed')return {ok:false,error:result.message||'The comment source changed. Your pending body is retained.'};
            if(typeof result?.content==='string'&&!destroyed)applyValue(result.content);
          } else {
            if(capture.expectedSource!==serializeSource(documentSource(target.state))||capture.expectedLocalRevision!==(options.getLocalRevision?.()||0))return {ok:false,error:'The source changed. Review your retained comment body before applying.'};
            target.dispatch({changes:serialized.change,annotations:Transaction.userEvent.of('input.comment')});
          }
          syncPendingPanel();return {ok:true};
        } catch(error){return {ok:false,error:error instanceof Error?error.message:String(error)};}
      },
      getPending:id=>options.getPendingSourceEdit?.(id) as any,
      registerPending:options.registerPendingSourceEdit?edit=>{options.registerPendingSourceEdit?.(edit);syncPendingPanel();}:undefined,
      removePending:id=>{options.removePendingSourceEdit?.(id);syncPendingPanel();},
      reveal:(target,region)=>target.dispatch({effects:commentSourceReveal.of([{from:region.from,to:region.to}]),selection:EditorSelection.range(region.from,region.to),scrollIntoView:true}),
      seeSource:(region,event)=>options.onSeeSource?.({from:region.from,to:region.to,source:region.sourceText,markdown:region.body},event),
    };
    return [...markdownPresentation,...(richComments&&isRichCommentLanguageQualified(languageMetadata.id,regionOptions)?[createRichCommentPlugin(languageMetadata.id,commentRender,regionOptions,options.onSeeSource,inset)]:[])];
  }
  function clearSyntax() {
    grammarLoaded = false;
    visibleCommentProjections.delete(view);
    view.dispatch({ effects:[languageCompartment.reconfigure([]), modeCompartment.reconfigure([]), view.scrollSnapshot()], annotations:Transaction.addToHistory.of(false) });
  }
  function retrySyntax() {
    if (destroyed) return Promise.resolve(false);
    const generation = ++syntaxGeneration;
    resolveSyntax?.(false);
    const result = new Promise<boolean>(resolve => { resolveSyntax = resolve; });
    clearSyntax();
    if (syntaxFrame != null) cancelAnimationFrame(syntaxFrame); syntaxFrame = null;
    if (syntaxDeadline != null) clearTimeout(syntaxDeadline);
    // Operational failure bound, never an artificial delay before ready paint.
    syntaxDeadline = setTimeout(() => {
      if (!destroyed && generation === syntaxGeneration) { clearSyntax(); revealSyntax({ state:'degraded', message:'Syntax loading timed out; editing plain source', retryable:true }); }
    }, 4000);
    if (!syntaxLabel.isConnected) parent.insertBefore(syntaxLabel, view.dom);
    view.dom.style.visibility = 'hidden'; reportSyntax({ state:'loading', message:'Loading syntax…', retryable:false });
    void loadLanguage().then(language => {
      if (destroyed || generation !== syntaxGeneration) return false;
      const hasLanguage = Array.isArray(language) ? language.length > 0 : Boolean(language);
      if (!hasLanguage) { revealSyntax({ state:'plain', message:'Plain text; no syntax grammar', retryable:false }); return false; }
      grammarLoaded = true;
      view.dispatch({ effects:[languageCompartment.reconfigure(language), modeCompartment.reconfigure(presentationExtensions()), view.scrollSnapshot()], annotations:Transaction.addToHistory.of(false) });
      forceParsing(view, view.viewport.to, 8);
      checkSyntax();
      return true;
    }).catch(() => {
      if (!destroyed && generation === syntaxGeneration) { clearSyntax(); revealSyntax({ state:'degraded', message:'Syntax unavailable; editing plain source', retryable:true }); }
      return false;
    });
    return result;
  }
  const languageReady = sourceReadiness ? retrySyntax() : Promise.resolve(true);
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
    const logicalNext = next.replace(/\r\n?|\n/g, '\n');
    const nextLength = logicalNext.length;
    const normalized = normalizeEditorSelection(nextSelection || selectionForLength(nextLength), nextLength);
    if (logicalNext === documentSource(view.state)) {
      originalSource = next;
      if (nextSelection) setSelection(normalized);
      return;
    }
    originalSource = next;
    silent = true;
    view.dispatch({
      changes:{ from:0, to:view.state.doc.length, insert:next },
      selection:EditorSelection.create(normalized.ranges.map((range) => EditorSelection.range(range.anchor, range.head)), normalized.mainIndex),
      annotations:Transaction.addToHistory.of(false),
    });
    silent = false;
  }

  function applyChanges(changes: any) {
    if (!changes || changes.empty) return;
    silent = true;
    try { view.dispatch({ changes, annotations:Transaction.addToHistory.of(false) }); }
    finally { silent = false; }
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

  function* literalMatches(needle: string, from = 0, to = view.state.doc.length): Generator<number> {
    let offset = from, tail = '';
    const iterator = view.state.doc.iterRange(from, to);
    while (!iterator.next().done) {
      const chunk = tail + iterator.value;
      const base = offset - tail.length;
      for (let at = chunk.indexOf(needle); at >= 0; at = chunk.indexOf(needle, at + needle.length)) if (base + at >= from && base + at + needle.length <= to) yield base + at;
      offset += iterator.value.length;
      tail = chunk.slice(Math.max(0, chunk.length - needle.length + 1));
    }
  }
  let commandNotice = '';
  const spellingOwner = {};
  let spellingGeneration = 0;
  function captureSpelling() {
    return { owner:spellingOwner, state:view.state, visible:view.visibleRanges.map(range => ({ ...range })), syntaxGeneration, grammarReady:grammarLoaded };
  }
  type SpellingCapture = ReturnType<typeof captureSpelling>;
  function spellingCurrent(capture:SpellingCapture) {
    return !destroyed && view.dom.isConnected && capture?.owner === spellingOwner && view.state.doc === capture.state.doc && view.state.selection.eq(capture.state.selection) && syntaxGeneration === capture.syntaxGeneration;
  }
  function spellingScope(capture:SpellingCapture) {
    const language = languageMetadata.id === 'markdown' || languageMetadata.parser.startsWith('markdown:') ? 'markdown' : languageMetadata.id;
    return collectSpellingScope(capture.state,language,capture.visible,capture.grammarReady);
  }
  function isSpellingSelectionEligible(capture = captureSpelling()) {
    if (!spellingCurrent(capture) || capture.state.selection.ranges.length !== 1 || capture.state.selection.main.empty || capture.state.selection.main.to-capture.state.selection.main.from > 64) return false;
    const scope = spellingScope(capture), range = capture.state.selection.main;
    return !scope.truncated && scope.tokens.length === 1 && scope.tokens[0].from === range.from && scope.tokens[0].to === range.to;
  }
  async function checkSpelling(service:any = (globalThis as any).openClankSpelling, capture = captureSpelling()) {
    const request = ++spellingGeneration;
    const globalService = (globalThis as any).openClankSpelling;
    const revision = service?.revision?.();
    const stale = () => !spellingCurrent(capture) || request !== spellingGeneration || globalService !== (globalThis as any).openClankSpelling || revision !== service?.revision?.();
    const staleResult = () => ({ state:'stale' as const,message:'The spelling target changed; check the current prose again.',diagnostics:[] as SpellingToken[],truncated:false });
    if (stale()) return staleResult();
    function report(message:string, diagnostics:SpellingToken[], truncated:boolean, state:'checked'|'unavailable' = 'checked') {
      commandNotice = message;
      view.dispatch({ effects:[spellingMarks.of(diagnostics),EditorView.announce.of(message)] });
      options.onSyntaxStatus?.({ ...syntaxStatus,message });
      return { state,message,diagnostics,truncated };
    }
    try {
      view.dispatch({ effects:spellingMarks.of([]) });
      const scope = spellingScope(capture);
      if (!scope.tokens.length) return report(scope.message,[],scope.truncated);
      if (typeof service?.checkBatch !== 'function') return report('Offline spelling is unavailable. Reload the spelling service and try again.',[],scope.truncated,'unavailable');
      const words = [...new Set(scope.tokens.map(token => token.word))];
      const batch = words.slice(0,256), correct = await service.checkBatch(batch);
      if (stale()) return staleResult();
      if (!Array.isArray(correct) || correct.length !== batch.length || correct.some(value => typeof value !== 'boolean')) throw new Error('Offline spelling returned an invalid batch.');
      const checked = new Map(batch.map((word,index) => [word,correct[index]]));
      const errors = scope.tokens.filter(token => checked.get(token.word) === false);
      const diagnostics = errors.slice(0,100);
      const truncated = scope.truncated || words.length > batch.length || errors.length > diagnostics.length;
      const locale = service.locale?.();
      const fallback = locale?.fallback ? `; ${locale.requested} uses the en-US fallback` : '';
      const message = `${errors.length} possible spelling ${errors.length === 1 ? 'error' : 'errors'} in ${scope.selection ? 'selected' : 'visible'} prose (en-US${fallback}).${truncated ? ' Bounded check; select a smaller range to check more.' : ''}${scope.skipped ? ' Paths, identifiers and long words were skipped.' : ''}`;
      return report(message,diagnostics,truncated);
    } catch (error) {
      if (stale()) return staleResult();
      return report(`Offline spelling failed: ${error instanceof Error ? error.message : String(error)}`,[],false,'unavailable');
    }
  }

  function selectNextMatch(query?: string) {
    commandNotice = '';
    const main = view.state.selection.main;
    if (query === undefined && main.empty) {
      const word = view.state.wordAt(main.head);
      if (!word) return false;
      view.dispatch({ selection:EditorSelection.range(word.from, word.to), userEvent:'select' }); return true;
    }
    const needle = String(query ?? view.state.doc.sliceString(main.from, main.to));
    if (!needle) return false;
    const ranges = view.state.selection.ranges;
    const available = (index: number) => !ranges.some(range => range.from < index + needle.length && range.to > index);
    let found: number | undefined;
    for (const index of literalMatches(needle, main.to)) if (available(index)) { found = index; break; }
    if (found === undefined) for (const index of literalMatches(needle, 0, main.from)) if (available(index)) { found = index; break; }
    if (found === undefined) return false;
    const next = [...ranges, EditorSelection.range(found, found + needle.length)];
    view.dispatch({ selection:EditorSelection.create(next, next.length - 1), scrollIntoView:true, userEvent:'select' });
    view.focus(); return true;
  }
  function selectAllMatches(query?: string) {
    const main = view.state.selection.main;
    const word = main.empty ? view.state.wordAt(main.head) : main;
    const needle = String(query ?? (word ? view.state.doc.sliceString(word.from, word.to) : ''));
    if (!needle) return false;
    const ranges: SelectionRange[] = [];
    commandNotice = '';
    for (const index of literalMatches(needle)) {
      if (ranges.length === 1000) { commandNotice = 'Selected the first 1,000 matches; narrow the selection for more'; break; }
      if (!ranges.length || index >= ranges[ranges.length - 1].to) ranges.push(EditorSelection.range(index, index + needle.length));
    }
    if (!ranges.length) return false;
    const mainIndex = Math.max(0, ranges.findIndex(range => range.from === main.from && range.to === main.to));
    view.dispatch({ selection:EditorSelection.create(ranges, mainIndex), scrollIntoView:true, userEvent:'select', ...(commandNotice ? { effects:EditorView.announce.of(commandNotice) } : {}) });
    options.onSyntaxStatus?.({ ...syntaxStatus, message:commandNotice || syntaxStatus.message });
    view.focus(); return true;
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
    if (name === 'spellcheck' || name === 'check-spelling') { void checkSpelling(); return true; }
    if (name === 'undo') return options.historyOwner ? options.historyOwner.undo() : undo(view);
    if (name === 'redo') return options.historyOwner ? options.historyOwner.redo() : redo(view);
    if (name === 'select-all') { view.dispatch({ selection:EditorSelection.range(0, view.state.doc.length), userEvent:'select' }); return true; }
    if (name === 'save' || name === 'quick-open' || name === 'palette' || name === 'search') return command(name);
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

  /** Per-open-buffer grammar choice; no document/history/view reconstruction. */
  function setLanguage(idOrAuto: string): Promise<boolean> {
    if (destroyed) return Promise.resolve(false);
    const id = String(idOrAuto || '').trim().toLowerCase();
    const entry = id === 'auto' ? null : LANGUAGE_REGISTRY.find(entry => entry.id === id);
    if (id !== 'auto' && !entry) return Promise.reject(new RangeError(`Unknown registry language: ${id}`));
    languageOverride = entry?.id || null;
    const content = view.state.doc.sliceString(0, Math.min(4096, view.state.doc.length));
    grammarOptions = languageOverride ? { dialect:undefined, path:undefined, content }
      : { dialect:options.languageDialect, path:options.languagePath, content };
    languageMetadata = sourceLanguageMetadata(languageOverride || undefined, grammarOptions);
    regionOptions = { dialect:languageMetadata.id === 'jsonc' ? 'jsonc' : undefined, path:undefined };
    commandNotice = '';
    // The readiness generation also invalidates captured spelling and async maps.
    spellingGeneration += 1;
    return retrySyntax();
  }

  function peekCommentSourceRange(offset: number): CommentSourceRange | null {
    if (destroyed || !grammarLoaded || !Number.isFinite(offset)) return null;
    const cached = visibleCommentProjections.get(view);
    if (!cached || cached.doc !== view.state.doc || cached.tree !== syntaxTree(view.state) || cached.language !== languageMetadata.displayName) return null;
    if (!view.visibleRanges.some(range => range.from <= offset && offset <= range.to)) return null;
    const range = cached.ranges.find(range => range.from <= offset && offset < range.to);
    return range ? { ...range, contentRanges:range.contentRanges.map(span => ({ ...span })) } : null;
  }

  function setMode(next: EditorMode) {
    const safe = next === 'source' ? 'source' : 'live';
    if (safe === mode) return;
    mode = safe;
    parent.dataset.mode = mode;
    if (mode === 'source' && !sourceReadiness) {
      sourceReadiness = true; parent.insertBefore(syntaxLabel, view.dom); void retrySyntax();
    } else if (mode === 'live' && sourceReadiness) {
      sourceReadiness = false; // A pending language choice still owns readiness.
    }
    view.dispatch({ effects:[modeCompartment.reconfigure(presentationExtensions()), view.scrollSnapshot()], annotations:Transaction.addToHistory.of(false) });
  }

  function setRichComments(next: boolean) {
    if (richComments === next) return;
    richComments = next;
    visibleCommentProjections.delete(view);
    view.dispatch({ effects:[modeCompartment.reconfigure(presentationExtensions()), view.scrollSnapshot()], annotations:Transaction.addToHistory.of(false) });
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
    const text = documentSource(view.state);
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
    const text = documentSource(view.state);
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
    view, captureSpelling, checkSpelling, isSpellingSelectionEligible,
    getValue:() => serializeSource(documentSource(view.state)), setValue, applyValue, applyChanges, setSelection, insertText, replaceSelections, formatSelections,
    pasteText, replaceRange, selectedTexts, getSelectedText:() => selectedTexts().join('\n'), selectNextMatch, selectAllMatches,
    duplicateLines, runCommand:runEditorCommand, setMode, setLanguage, peekCommentSourceRange, setRichComments, setLineNumbers, setReadableLineWidth,
    getScrollTop:() => view.scrollDOM.scrollTop,
    focus:() => { if (syntaxStatus.state === 'loading') focusPending = true; else view.focus(); }, focusLine,
    undo:() => options.historyOwner ? options.historyOwner.undo() : undo(view), redo:() => options.historyOwner ? options.historyOwner.redo() : redo(view), find, replace,
    getSelection:() => selectionSnapshot(view.state),
    wrapMarkdownAsComment:(markdown: string, indent = '') => wrapSourceComment(view.state,view.state.selection.main.from,markdown,indent),
    safeCommentInsertion:(offset: number) => safeSourceCommentInsertion(view.state,languageMetadata.id,offset),
    revealCommentSource:(from: number, to: number) => {
      view.dispatch({ effects:commentSourceReveal.of([{from,to}]),selection:EditorSelection.range(from, to), scrollIntoView:true });
      view.focus();
    },
    getCommentSourceMap:() => visibleCommentRanges(view, languageMetadata.displayName, regionOptions).map(range => ({ ...range, markdown:range.contentRanges.map(span => view.state.doc.sliceString(span.from, span.to)).join('\n') })),
    getCommentSourceMapAsync:async () => {
      const state = view.state, generation = syntaxGeneration;
      await languageReady;
      if (!forceParsing(view,state.doc.length,20) || !syntaxTreeAvailable(view.state,state.doc.length)) throw new Error('Comment syntax is still loading; retry the source map.');
      const ranges = sourceCommentRegions(view.state,languageMetadata.id,[{from:0,to:state.doc.length}]);
      if (destroyed || view.state.doc !== state.doc || generation !== syntaxGeneration) throw new Error('The source changed while comment syntax loaded');
      return ranges.map(range => ({ ...range, markdown:range.contentRanges.map(span => state.doc.sliceString(span.from, span.to)).join('\n') }));
    },
    getSyntaxStatus:() => ({ ...syntaxStatus, message:commandNotice || syntaxStatus.message }), retrySyntax,
    getStatus:() => ({ language:{ id:languageMetadata.id, name:languageMetadata.displayName, supportLevel:languageMetadata.supportLevel, mode:languageOverride ? 'override' : 'auto', override:languageOverride }, syntax:{ ...syntaxStatus, message:commandNotice || syntaxStatus.message }, selection:selectionSnapshot(view.state), canUndo:options.historyOwner ? options.historyOwner.state().canUndo : undoDepth(view.state) > 0, canRedo:options.historyOwner ? options.historyOwner.state().canRedo : redoDepth(view.state) > 0, notice:commandNotice || options.historyOwner?.state().historyNotice || '' }),
    languageReady,
    destroy:() => { destroyed = true; syntaxGeneration += 1; spellingGeneration += 1; resolveSyntax?.(false); resolveSyntax = null; if (syntaxDeadline != null) clearTimeout(syntaxDeadline); if (syntaxFrame != null) cancelAnimationFrame(syntaxFrame); syntaxLabel.remove();pendingPanel.remove(); view.destroy(); },
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
    language:options.language || 'Plain text',
    mode:'source',
    readableLineWidth:false,
    lineWrapping:false,
  });
}

// Documentation-region adapters are part of this bundle's public surface so
// Code Editor, Copal Editor and image-paste insertion share one authority for
// comment boundaries, docstrings and safe insertion points.
export {
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

export { LANGUAGE_REGISTRY, ADVERTISED_LANGUAGE_LABELS, sourceLanguageMetadata } from './openclank-source-languages';
