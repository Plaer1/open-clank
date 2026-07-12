import { defaultKeymap, history, historyKeymap, indentWithTab, redo, undo } from '@codemirror/commands';
import { markdown } from '@codemirror/lang-markdown';
import { bracketMatching, defaultHighlightStyle, foldGutter, indentOnInput, syntaxHighlighting } from '@codemirror/language';
import { Compartment, EditorSelection, EditorState, StateEffect, StateField, Transaction } from '@codemirror/state';
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

type EditorMode = 'live' | 'source';

interface MarkdownEditorOptions {
  parent: HTMLElement;
  doc?: string;
  label?: string;
  placeholderText?: string;
  selection?: { anchor?: number; head?: number } | null;
  scrollTop?: number;
  mode?: EditorMode;
  lineNumbers?: boolean;
  readableLineWidth?: boolean;
  onChange?: (value: string, update: ViewUpdate) => void;
  onFocus?: () => void;
  onSelection?: (selection: { anchor: number; head: number; line: number }) => void;
  onScroll?: (scrollTop: number) => void;
  onCommand?: (command: string) => void;
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
  ) { super(); }

  eq(other: BlockPreviewWidget) { return other.kind === this.kind && other.source === this.source && other.from === this.from; }

  toDOM(view: EditorView) {
    const root = document.createElement(this.kind === 'embed' ? 'figure' : 'div');
    root.className = `cm-md-${this.kind}-widget`;
    if (this.kind === 'hr') {
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

function blockAt(state: EditorState, lineNumber: number, active: Set<number>) {
  const line = state.doc.line(lineNumber);
  const text = line.text;
  if (/^\s{0,3}([-*_])(?:\s*\1){2,}\s*$/.test(text)) return { kind:'hr' as const, from:line.from, to:line.to, source:text, endLine:lineNumber };
  if (/^\s*\[\^[^\]]+\]:/.test(text)) return { kind:'footnote' as const, from:line.from, to:line.to, source:text, endLine:lineNumber };
  if (/^\s*!?(?:\[\[|\[[^\]]*\]\()/.test(text) && /^\s*!/.test(text)) return { kind:'embed' as const, from:line.from, to:line.to, source:text, endLine:lineNumber };
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

function buildStructuralDecorations(state: EditorState, visibleRanges: Array<{ from:number; to:number }>): DecorationSet {
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
        ranges.push({ from:block.from, to:block.to, value:Decoration.replace({ widget:new BlockPreviewWidget(block.kind, block.source, block.from), block:true }) });
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

const structuralDecorations = StateField.define<StructuralDecorationState>({
  create:(state) => {
    const ranges = [{ from:0, to:Math.min(state.doc.length, 8000) }];
    return { ranges, decorations:buildStructuralDecorations(state, ranges) };
  },
  update:(value, transaction) => {
    const effect = transaction.effects.find((candidate) => candidate.is(setStructuralViewport));
    const ranges = effect?.value || value.ranges.map((range) => ({ from:transaction.changes.mapPos(range.from), to:transaction.changes.mapPos(range.to) }));
    if (effect || transaction.docChanged || transaction.selection) return { ranges, decorations:buildStructuralDecorations(transaction.state, ranges) };
    return value;
  },
  provide:(field) => EditorView.decorations.from(field, (value) => value.decorations),
});

function buildLivePreviewDecorations(view: EditorView): DecorationSet {
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
      if (!/^\s*```/.test(line.text)) addInlineDecorations(ranges, line.from, line.text);
    }
  }
  return Decoration.set(ranges.sort((a, b) => a.from - b.from || a.to - b.to), true);
}

const livePreviewPlugin = ViewPlugin.fromClass(class {
  decorations: DecorationSet;
  viewport = '';
  constructor(view: EditorView) { this.decorations = buildLivePreviewDecorations(view); this.updateViewport(view); }
  updateViewport(view: EditorView) {
    const ranges = view.visibleRanges.map(({ from, to }) => ({ from, to }));
    const signature = ranges.map(({ from, to }) => `${from}:${to}`).join(',');
    if (signature === this.viewport) return;
    this.viewport = signature;
    queueMicrotask(() => { if (view.dom.isConnected) view.dispatch({ effects:setStructuralViewport.of(ranges) }); });
  }
  update(update: ViewUpdate) {
    if (update.docChanged || update.selectionSet || update.viewportChanged) {
      this.decorations = buildLivePreviewDecorations(update.view);
      this.updateViewport(update.view);
    }
  }
}, { decorations:(value) => value.decorations });

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

export function createMarkdownEditor(options: MarkdownEditorOptions) {
  const {
    parent, doc = '', label = 'Markdown editor', placeholderText = 'Start writing…', selection,
    onChange, onFocus, onSelection, onScroll, onCommand,
  } = options;
  let silent = false;
  let mode: EditorMode = options.mode === 'source' ? 'source' : 'live';
  let showLineNumbers = options.lineNumbers === true;
  let readableLineWidth = options.readableLineWidth !== false;
  const modeCompartment = new Compartment();
  const gutterCompartment = new Compartment();
  const widthCompartment = new Compartment();
  const anchor = Math.max(0, Math.min(String(doc).length, Number(selection?.anchor) || 0));
  const head = Math.max(0, Math.min(String(doc).length, Number(selection?.head) || anchor));
  const command = (name: string) => { onCommand?.(name); return true; };
  const state = EditorState.create({
    doc,
    selection:{ anchor, head },
    extensions:[
      gutterCompartment.of(showLineNumbers ? [lineNumbers()] : []),
      highlightSpecialChars(), history(), foldGutter(), drawSelection(), dropCursor(),
      EditorState.allowMultipleSelections.of(true), indentOnInput(),
      syntaxHighlighting(defaultHighlightStyle, { fallback:true }), bracketMatching(), rectangularSelection(),
      highlightActiveLine(), markdown(), EditorView.lineWrapping, placeholder(placeholderText),
      modeCompartment.of(mode === 'live' ? [structuralDecorations, livePreviewPlugin] : []),
      widthCompartment.of(widthExtension(readableLineWidth)),
      keymap.of([
        { key:'Mod-s', run:() => command('save') },
        { key:'Mod-o', run:() => command('quick-open') },
        { key:'Mod-p', run:() => command('palette') },
        { key:'Mod-Shift-f', run:() => command('search') },
        indentWithTab, ...defaultKeymap, ...historyKeymap,
      ]),
      EditorView.contentAttributes.of({ 'aria-label':label, spellcheck:'true' }),
      EditorView.updateListener.of((update) => {
        if (update.focusChanged && update.view.hasFocus) onFocus?.();
        if (update.docChanged && !silent) onChange?.(update.state.doc.toString(), update);
        if (update.selectionSet || update.docChanged) {
          const range = update.state.selection.main;
          onSelection?.({ anchor:range.anchor, head:range.head, line:update.state.doc.lineAt(range.head).number });
        }
      }),
      baseTheme,
    ],
  });
  parent.dataset.mode = mode;
  const view = new EditorView({ state, parent });
  if (Number.isFinite(options.scrollTop)) view.scrollDOM.scrollTop = Math.max(0, Number(options.scrollTop));
  const reportScroll = () => onScroll?.(view.scrollDOM.scrollTop);
  view.scrollDOM.addEventListener('scroll', reportScroll, { passive:true });

  function setValue(value: string) {
    const next = String(value ?? '');
    if (next === view.state.doc.toString()) return;
    silent = true;
    const cursor = Math.min(next.length, view.state.selection.main.head);
    view.dispatch({ changes:{ from:0, to:view.state.doc.length, insert:next }, selection:EditorSelection.cursor(cursor), annotations:Transaction.addToHistory.of(false) });
    silent = false;
  }

  function applyValue(value: string, selection?: { anchor?:number; head?:number }) {
    const next = String(value ?? '');
    if (next === view.state.doc.toString()) return;
    const anchor = Math.max(0, Math.min(next.length, Number(selection?.anchor) || 0));
    const head = Math.max(0, Math.min(next.length, Number(selection?.head) || anchor));
    view.dispatch({ changes:{ from:0, to:view.state.doc.length, insert:next }, selection:{ anchor, head } });
  }

  function setMode(next: EditorMode) {
    const safe = next === 'source' ? 'source' : 'live';
    if (safe === mode) return;
    mode = safe;
    parent.dataset.mode = mode;
    view.dispatch({ effects:modeCompartment.reconfigure(mode === 'live' ? [structuralDecorations, livePreviewPlugin] : []) });
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
      const count = text.split(needle).length - 1;
      if (count) view.dispatch({ changes:{ from:0, to:view.state.doc.length, insert:text.split(needle).join(String(replacement ?? '')) } });
      return count;
    }
    const selection = view.state.selection.main;
    if (text.slice(selection.from, selection.to) !== needle && !find(needle)) return 0;
    const current = view.state.selection.main;
    view.dispatch({ changes:{ from:current.from, to:current.to, insert:String(replacement ?? '') } });
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
    getValue:() => view.state.doc.toString(), setValue, applyValue, setMode, setLineNumbers, setReadableLineWidth,
    getScrollTop:() => view.scrollDOM.scrollTop,
    focus:() => view.focus(), focusLine,
    undo:() => undo(view), redo:() => redo(view), find, replace,
    getSelection:() => {
      const range = view.state.selection.main;
      return { anchor:range.anchor, head:range.head, line:view.state.doc.lineAt(range.head).number };
    },
    destroy:() => view.destroy(),
  };
}
