'use client';

import {
  forwardRef,
  useEffect,
  useImperativeHandle,
  useMemo,
  useRef,
} from 'react';
import { markdown } from '@codemirror/lang-markdown';
import { defaultKeymap, history, historyKeymap } from '@codemirror/commands';
import { bracketMatching, defaultHighlightStyle, syntaxHighlighting } from '@codemirror/language';
import { EditorSelection, EditorState, StateEffect, StateField } from '@codemirror/state';
import {
  Decoration,
  DecorationSet,
  EditorView,
  WidgetType,
  drawSelection,
  highlightActiveLine,
  highlightSpecialChars,
  keymap,
  lineNumbers,
} from '@codemirror/view';

export interface LiveMarkdownEditorHandle {
  focus: () => void;
  getSelection: () => { start: number; end: number };
  setSelectionRange: (start: number, end?: number, scroll?: boolean) => void;
  replaceSelection: (text: string) => void;
  getCursor: () => number;
  getCursorRect: () => DOMRect | null;
  focusLine: (line: number) => { start: number; end: number; line: number };
}

interface LiveMarkdownEditorProps {
  value: string;
  mode: 'live' | 'source';
  onChange: (value: string) => void;
  onCursorLineChange?: (line: number) => void;
  onCommand?: (id: string) => void;
  readOnly?: boolean;
}

const activeLineEffect = StateEffect.define<number>();

const activeLineField = StateField.define<number>({
  create: () => 0,
  update(value, tr) {
    for (const effect of tr.effects) {
      if (effect.is(activeLineEffect)) return effect.value;
    }
    if (tr.selection) return tr.state.doc.lineAt(tr.state.selection.main.head).number;
    return value;
  },
});

class CheckboxWidget extends WidgetType {
  constructor(
    private readonly checked: boolean,
    private readonly from: number,
  ) {
    super();
  }

  eq(other: CheckboxWidget) {
    return other.checked === this.checked && other.from === this.from;
  }

  toDOM(view: EditorView) {
    const input = document.createElement('input');
    input.type = 'checkbox';
    input.checked = this.checked;
    input.className = 'cm-md-task-checkbox';
    input.addEventListener('mousedown', (event) => event.preventDefault());
    input.addEventListener('click', (event) => {
      event.preventDefault();
      const token = view.state.doc.sliceString(this.from, this.from + 3);
      const next = token.toLowerCase() === '[x]' ? '[ ]' : '[x]';
      view.dispatch({ changes: { from: this.from, to: this.from + 3, insert: next } });
      view.focus();
    });
    return input;
  }

  ignoreEvent() {
    return false;
  }
}

interface FrontmatterRow {
  key: string;
  value: string;
  valueFrom: number;
  valueTo: number;
}

class FrontmatterWidget extends WidgetType {
  constructor(private readonly rows: FrontmatterRow[]) {
    super();
  }

  eq(other: FrontmatterWidget) {
    return JSON.stringify(other.rows) === JSON.stringify(this.rows);
  }

  toDOM(view: EditorView) {
    const root = document.createElement('div');
    root.className = 'cm-md-frontmatter-card';
    const title = document.createElement('div');
    title.className = 'cm-md-frontmatter-title';
    title.textContent = 'Properties';
    root.appendChild(title);
    const list = document.createElement('div');
    list.className = 'cm-md-frontmatter-list';
    for (const item of this.rows) {
      const row = document.createElement('div');
      row.className = 'cm-md-frontmatter-row';
      const keyNode = document.createElement('span');
      keyNode.className = 'cm-md-frontmatter-key';
      keyNode.textContent = item.key;
      const valueNode = document.createElement('input');
      valueNode.className = 'cm-md-frontmatter-input';
      valueNode.value = item.value;
      valueNode.placeholder = 'empty';
      valueNode.addEventListener('mousedown', (event) => event.stopPropagation());
      valueNode.addEventListener('keydown', (event) => {
        if (event.key === 'Enter') valueNode.blur();
        if (event.key === 'Escape') {
          valueNode.value = item.value;
          valueNode.blur();
        }
      });
      valueNode.addEventListener('blur', () => {
        if (valueNode.value === item.value) return;
        view.dispatch({
          changes: { from: item.valueFrom, to: item.valueTo, insert: valueNode.value },
        });
      });
      row.append(keyNode, valueNode);
      list.appendChild(row);
    }
    if (this.rows.length === 0) {
      const empty = document.createElement('div');
      empty.className = 'cm-md-frontmatter-empty';
      empty.textContent = 'No properties';
      list.appendChild(empty);
    }
    root.appendChild(list);
    return root;
  }

  ignoreEvent() {
    return false;
  }
}

interface TableCell {
  value: string;
  from: number;
  to: number;
}

class TableWidget extends WidgetType {
  constructor(private readonly rows: TableCell[][]) {
    super();
  }

  eq(other: TableWidget) {
    return JSON.stringify(other.rows) === JSON.stringify(this.rows);
  }

  toDOM(view: EditorView) {
    const wrapper = document.createElement('div');
    wrapper.className = 'cm-md-table-widget';
    const table = document.createElement('table');
    const [header, separator, ...body] = this.rows;
    const head = document.createElement('thead');
    const headRow = document.createElement('tr');
    for (const cell of header ?? []) {
      const th = document.createElement('th');
      th.contentEditable = 'true';
      th.textContent = cell.value || ' ';
      th.title = 'Edit table cell';
      th.addEventListener('mousedown', (event) => event.stopPropagation());
      th.addEventListener('blur', () => updateCell(view, cell, th.textContent ?? ''));
      headRow.appendChild(th);
    }
    head.appendChild(headRow);
    table.appendChild(head);
    const tbody = document.createElement('tbody');
    const rows = separator?.every((cell) => /^:?-{3,}:?$/.test(cell.value)) ? body : this.rows.slice(1);
    for (const row of rows) {
      const tr = document.createElement('tr');
      for (const cell of row) {
        const td = document.createElement('td');
        td.contentEditable = 'true';
        td.textContent = cell.value || ' ';
        td.title = 'Edit table cell';
        td.addEventListener('mousedown', (event) => event.stopPropagation());
        td.addEventListener('blur', () => updateCell(view, cell, td.textContent ?? ''));
        tr.appendChild(td);
      }
      tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    wrapper.appendChild(table);
    return wrapper;
  }

  ignoreEvent() {
    return false;
  }
}

function updateCell(view: EditorView, cell: TableCell, next: string) {
  const value = next.trim();
  if (value === cell.value) return;
  view.dispatch({ changes: { from: cell.from, to: cell.to, insert: value } });
}

class CalloutBlockWidget extends WidgetType {
  constructor(
    private readonly kind: string,
    private readonly title: string,
    private readonly body: string[],
    private readonly from: number,
  ) {
    super();
  }

  eq(other: CalloutBlockWidget) {
    return (
      other.kind === this.kind &&
      other.title === this.title &&
      other.from === this.from &&
      JSON.stringify(other.body) === JSON.stringify(this.body)
    );
  }

  toDOM(view: EditorView) {
    const root = document.createElement('details');
    root.className = 'cm-md-callout-block-widget';
    root.open = true;
    root.addEventListener('toggle', () => view.focus());
    const summary = document.createElement('summary');
    summary.className = 'cm-md-callout-summary';
    const badge = document.createElement('span');
    badge.className = 'cm-md-callout-kind';
    badge.textContent = this.kind.toUpperCase();
    const title = document.createElement('span');
    title.className = 'cm-md-callout-title';
    title.textContent = this.title || this.kind;
    summary.append(badge, title);
    const body = document.createElement('div');
    body.className = 'cm-md-callout-body';
    body.textContent = this.body.join('\n');
    const source = document.createElement('button');
    source.type = 'button';
    source.className = 'cm-md-widget-source';
    source.textContent = 'Edit source';
    source.addEventListener('click', (event) => {
      event.preventDefault();
      view.dispatch({ selection: EditorSelection.cursor(this.from), scrollIntoView: true });
      view.focus();
    });
    root.append(summary, body, source);
    return root;
  }

  ignoreEvent() {
    return true;
  }
}

class MathBlockWidget extends WidgetType {
  constructor(
    private readonly body: string,
    private readonly from: number,
  ) {
    super();
  }

  eq(other: MathBlockWidget) {
    return other.body === this.body && other.from === this.from;
  }

  toDOM(view: EditorView) {
    const root = document.createElement('div');
    root.className = 'cm-md-math-block-widget';
    const pre = document.createElement('pre');
    pre.textContent = this.body || 'math';
    const source = document.createElement('button');
    source.type = 'button';
    source.className = 'cm-md-widget-source';
    source.textContent = 'Edit source';
    source.addEventListener('click', (event) => {
      event.preventDefault();
      view.dispatch({ selection: EditorSelection.cursor(this.from), scrollIntoView: true });
      view.focus();
    });
    root.append(pre, source);
    return root;
  }

  ignoreEvent() {
    return false;
  }
}

class FootnoteWidget extends WidgetType {
  constructor(
    private readonly id: string,
    private readonly text: string,
    private readonly from: number,
  ) {
    super();
  }

  eq(other: FootnoteWidget) {
    return other.id === this.id && other.text === this.text && other.from === this.from;
  }

  toDOM(view: EditorView) {
    const root = document.createElement('button');
    root.type = 'button';
    root.className = 'cm-md-footnote-widget';
    root.addEventListener('click', () => {
      view.dispatch({ selection: EditorSelection.cursor(this.from), scrollIntoView: true });
      view.focus();
    });
    const marker = document.createElement('span');
    marker.className = 'cm-md-footnote-marker';
    marker.textContent = `^${this.id}`;
    const text = document.createElement('span');
    text.className = 'cm-md-footnote-text';
    text.textContent = this.text;
    root.append(marker, text);
    return root;
  }

  ignoreEvent() {
    return false;
  }
}

class EmbedWidget extends WidgetType {
  constructor(
    private readonly kind: 'image' | 'embed',
    private readonly label: string,
    private readonly target: string,
  ) {
    super();
  }

  eq(other: EmbedWidget) {
    return other.kind === this.kind && other.label === this.label && other.target === this.target;
  }

  toDOM() {
    const root = document.createElement(this.kind === 'image' ? 'figure' : 'span');
    root.className = 'cm-md-embed-widget';
    if (this.kind === 'image' && isImageTarget(this.target)) {
      const image = document.createElement('img');
      image.src = `/api/vault-asset?path=${encodeURIComponent(this.target)}`;
      image.alt = this.label;
      image.loading = 'lazy';
      const caption = document.createElement('figcaption');
      caption.textContent = this.label || this.target;
      root.append(image, caption);
      return root;
    }
    const kind = document.createElement('span');
    kind.className = 'cm-md-embed-kind';
    kind.textContent = this.kind === 'image' ? 'image' : 'embed';
    const label = document.createElement('span');
    label.className = 'cm-md-embed-label';
    label.textContent = this.label;
    root.append(kind, label);
    return root;
  }

  ignoreEvent() {
    return true;
  }
}

function parseFrontmatter(state: EditorState) {
  if (!state.doc.lines || state.doc.line(1).text.trim() !== '---') return null;
  const rows: FrontmatterRow[] = [];
  for (let lineNo = 2; lineNo <= state.doc.lines; lineNo++) {
    const line = state.doc.line(lineNo);
    const trimmed = line.text.trim();
    if (trimmed === '---') {
      return { from: state.doc.line(1).from, to: line.to, startLine: 1, endLine: lineNo, rows };
    }
    const match = /^(\s*)([A-Za-z0-9_-]+):(\s*)(.*)$/.exec(line.text);
    if (match) {
      const valueStart = line.from + match[1].length + match[2].length + 1 + match[3].length;
      rows.push({ key: match[2], value: match[4], valueFrom: valueStart, valueTo: line.to });
    }
  }
  return null;
}

function isTableLine(text: string) {
  return text.includes('|') && /^\s*\|?[^|]+\|/.test(text);
}

function tableCells(text: string) {
  return text
    .trim()
    .replace(/^\|/, '')
    .replace(/\|$/, '')
    .split('|')
    .map((cell) => cell.trim());
}

function tableBlockAt(state: EditorState, lineNumber: number) {
  const line = state.doc.line(lineNumber);
  if (!isTableLine(line.text)) return null;
  let startLine = lineNumber;
  let endLine = lineNumber;
  while (startLine > 1 && isTableLine(state.doc.line(startLine - 1).text)) startLine--;
  while (endLine < state.doc.lines && isTableLine(state.doc.line(endLine + 1).text)) endLine++;
  if (endLine <= startLine) return null;
  const start = state.doc.line(startLine);
  const end = state.doc.line(endLine);
  const rows: TableCell[][] = [];
  for (let current = startLine; current <= endLine; current++) {
    const currentLine = state.doc.line(current);
    const ranges = tableCellRanges(currentLine.text);
    rows.push(
      tableCells(currentLine.text).map((value, index) => ({
        value,
        from: currentLine.from + (ranges[index]?.start ?? 0),
        to: currentLine.from + (ranges[index]?.end ?? currentLine.text.length),
      })),
    );
  }
  return { from: start.from, to: end.to, startLine, endLine, rows };
}

function calloutBlockAt(state: EditorState, lineNumber: number) {
  const start = state.doc.line(lineNumber);
  const header = calloutHeader(start.text);
  if (!header) return null;
  let endLine = lineNumber;
  while (endLine < state.doc.lines && /^\s*>/.test(state.doc.line(endLine + 1).text)) endLine++;
  const end = state.doc.line(endLine);
  const body: string[] = [];
  for (let current = lineNumber + 1; current <= endLine; current++) {
    body.push(state.doc.line(current).text.replace(/^\s*>\s?/, ''));
  }
  return { from: start.from, to: end.to, startLine: lineNumber, endLine, ...header, body };
}

function mathBlockAt(state: EditorState, lineNumber: number) {
  const start = state.doc.line(lineNumber);
  if (start.text.trim() !== '$$') return null;
  let endLine = lineNumber + 1;
  while (endLine <= state.doc.lines) {
    const line = state.doc.line(endLine);
    if (line.text.trim() === '$$') {
      const body: string[] = [];
      for (let current = lineNumber + 1; current < endLine; current++) body.push(state.doc.line(current).text);
      return { from: start.from, to: line.to, startLine: lineNumber, endLine, body: body.join('\n') };
    }
    endLine++;
  }
  return null;
}

function lineClasses(line: string, inFence: boolean): string[] {
  const trimmed = line.trim();
  const classes: string[] = [];
  const heading = /^(#{1,6})\s+/.exec(line);
  if (heading) {
    classes.push('cm-md-heading', `cm-md-h${heading[1].length}`);
  }
  if (/^\s*[-*]\s+\[[ xX/-]\]\s+/.test(line)) classes.push('cm-md-task-line');
  if (/^\s*>/.test(line)) classes.push('cm-md-quote-line');
  if (/^\s*>\s*\[!/.test(line)) classes.push('cm-md-callout-line');
  if (line.includes('|') && /^\s*\|?[^|]+\|/.test(line)) classes.push('cm-md-table-line');
  if (inFence || /^\s*```/.test(line)) classes.push('cm-md-code-line');
  if (trimmed === '---' || /^[A-Za-z0-9_-]+:\s*/.test(trimmed)) classes.push('cm-md-frontmatter-line');
  return classes;
}

function addInlineMarks(builder: { add: (from: number, to: number, value: Decoration) => void }, from: number, text: string) {
  const syntax = Decoration.mark({ class: 'cm-md-syntax-hidden' });
  const strong = Decoration.mark({ class: 'cm-md-strong' });
  const emphasis = Decoration.mark({ class: 'cm-md-emphasis' });
  const inlineCode = Decoration.mark({ class: 'cm-md-inline-code' });
  const inlineMath = Decoration.mark({ class: 'cm-md-inline-math' });
  const link = Decoration.mark({ class: 'cm-md-wikilink' });
  const tag = Decoration.mark({ class: 'cm-md-tag' });

  for (const match of text.matchAll(/\*\*([^*\n]+)\*\*/g)) {
    if (match.index === undefined) continue;
    builder.add(from + match.index, from + match.index + 2, syntax);
    builder.add(from + match.index + 2, from + match.index + match[0].length - 2, strong);
    builder.add(from + match.index + match[0].length - 2, from + match.index + match[0].length, syntax);
  }
  for (const match of text.matchAll(/(^|[^\w])_([^_\n]+)_/g)) {
    if (match.index === undefined) continue;
    const start = from + match.index + match[1].length;
    builder.add(start, start + 1, syntax);
    builder.add(start + 1, start + match[0].length - match[1].length - 1, emphasis);
    builder.add(start + match[0].length - match[1].length - 1, start + match[0].length - match[1].length, syntax);
  }
  for (const match of text.matchAll(/`([^`\n]+)`/g)) {
    if (match.index === undefined) continue;
    builder.add(from + match.index, from + match.index + 1, syntax);
    builder.add(from + match.index + 1, from + match.index + match[0].length - 1, inlineCode);
    builder.add(from + match.index + match[0].length - 1, from + match.index + match[0].length, syntax);
  }
  for (const match of text.matchAll(/(^|[^$])\$([^$\n]+)\$/g)) {
    if (match.index === undefined) continue;
    const start = from + match.index + match[1].length;
    builder.add(start, start + 1, syntax);
    builder.add(start + 1, start + match[0].length - match[1].length - 1, inlineMath);
    builder.add(start + match[0].length - match[1].length - 1, start + match[0].length - match[1].length, syntax);
  }
  for (const match of text.matchAll(/!?\[\[([^\]\n]+)\]\]/g)) {
    if (match.index === undefined) continue;
    builder.add(from + match.index, from + match.index + match[0].length, link);
  }
  for (const match of text.matchAll(/(^|[\s(])#([A-Za-z0-9_/-]+)/g)) {
    if (match.index === undefined) continue;
    const start = from + match.index + match[1].length;
    builder.add(start, start + match[0].length - match[1].length, tag);
  }
}

function calloutHeader(text: string) {
  const match = /^\s*>\s*\[!([A-Za-z0-9_-]+)\][+-]?\s*(.*)$/.exec(text);
  if (!match) return null;
  return { kind: match[1], title: match[2].trim() };
}

function lineEmbed(text: string) {
  const wikilink = /^\s*!\[\[([^\]\n]+)\]\]\s*$/.exec(text);
  if (wikilink) {
    const target = wikilink[1].split('|')[0].trim();
    return { kind: isImageTarget(target) ? 'image' as const : 'embed' as const, label: wikilink[1].trim(), target };
  }
  const markdownImage = /^\s*!\[([^\]\n]*)\]\(([^)\n]+)\)\s*$/.exec(text);
  if (markdownImage) {
    const target = markdownImage[2].trim();
    return { kind: 'image' as const, label: markdownImage[1].trim() || target, target };
  }
  return null;
}

function isImageTarget(target: string) {
  return /\.(png|jpe?g|gif|webp|svg)$/i.test(target);
}

function replaceSelection(view: EditorView, text: string, selectFrom?: number, selectTo?: number) {
  const range = view.state.selection.main;
  const cursor = range.from + text.length;
  view.dispatch({
    changes: { from: range.from, to: range.to, insert: text },
    selection:
      selectFrom === undefined || selectTo === undefined
        ? EditorSelection.cursor(cursor)
        : EditorSelection.range(range.from + selectFrom, range.from + selectTo),
    scrollIntoView: true,
  });
  return true;
}

function continueMarkdownLine(view: EditorView) {
  if (view.state.readOnly) return false;
  const range = view.state.selection.main;
  if (!range.empty) return false;
  const line = view.state.doc.lineAt(range.head);
  const before = line.text.slice(0, range.head - line.from);
  const after = line.text.slice(range.head - line.from);
  if (after.trim()) return false;

  const task = /^(\s*)([-*+])\s+\[[ xX/-]\]\s*(.*)$/.exec(before);
  if (task) {
    if (!task[3].trim()) {
      view.dispatch({
        changes: { from: line.from, to: range.head, insert: '' },
        selection: EditorSelection.cursor(line.from),
        scrollIntoView: true,
      });
      return true;
    }
    return replaceSelection(view, `\n${task[1]}${task[2]} [ ] `);
  }

  const bullet = /^(\s*)([-*+])\s+(.*)$/.exec(before);
  if (bullet) {
    if (!bullet[3].trim()) {
      view.dispatch({
        changes: { from: line.from, to: range.head, insert: '' },
        selection: EditorSelection.cursor(line.from),
        scrollIntoView: true,
      });
      return true;
    }
    return replaceSelection(view, `\n${bullet[1]}${bullet[2]} `);
  }

  const numbered = /^(\s*)(\d+)\.\s+(.*)$/.exec(before);
  if (numbered) {
    if (!numbered[3].trim()) {
      view.dispatch({
        changes: { from: line.from, to: range.head, insert: '' },
        selection: EditorSelection.cursor(line.from),
        scrollIntoView: true,
      });
      return true;
    }
    return replaceSelection(view, `\n${numbered[1]}${Number(numbered[2]) + 1}. `);
  }

  const quote = /^(\s*>\s+)(.*)$/.exec(before);
  if (quote) {
    if (!quote[2].trim()) {
      view.dispatch({
        changes: { from: line.from, to: range.head, insert: '' },
        selection: EditorSelection.cursor(line.from),
        scrollIntoView: true,
      });
      return true;
    }
    return replaceSelection(view, `\n${quote[1]}`);
  }

  return false;
}

function changeLineIndent(view: EditorView, direction: 'in' | 'out') {
  if (view.state.readOnly) return false;
  const selection = view.state.selection.main;
  const startLine = view.state.doc.lineAt(selection.from);
  const endLine = view.state.doc.lineAt(selection.to);
  const changes: Array<{ from: number; to?: number; insert: string }> = [];
  let fromDelta = 0;
  let toDelta = 0;

  for (let lineNo = startLine.number; lineNo <= endLine.number; lineNo++) {
    const line = view.state.doc.line(lineNo);
    if (direction === 'in') {
      changes.push({ from: line.from, insert: '  ' });
      if (line.from <= selection.from) fromDelta += 2;
      if (line.from < selection.to || selection.empty) toDelta += 2;
    } else {
      const remove = line.text.startsWith('  ') ? 2 : line.text.startsWith('\t') || line.text.startsWith(' ') ? 1 : 0;
      if (!remove) continue;
      changes.push({ from: line.from, to: line.from + remove, insert: '' });
      if (line.from < selection.from) fromDelta -= Math.min(remove, selection.from - line.from);
      if (line.from < selection.to || selection.empty) toDelta -= remove;
    }
  }

  if (changes.length === 0) return true;
  const nextFrom = Math.max(0, selection.from + fromDelta);
  const nextTo = Math.max(nextFrom, selection.to + toDelta);
  view.dispatch({
    changes,
    selection: selection.empty ? EditorSelection.cursor(nextTo) : EditorSelection.range(nextFrom, nextTo),
    scrollIntoView: true,
  });
  return true;
}

function tableCellRanges(text: string) {
  const pipes: number[] = [];
  for (let index = 0; index < text.length; index++) {
    if (text[index] === '|') pipes.push(index);
  }
  if (pipes.length === 0) return [];
  const boundaries = [text.startsWith('|') ? 0 : -1, ...pipes.filter((pipe) => pipe !== 0), text.endsWith('|') ? text.length - 1 : text.length];
  return boundaries.slice(0, -1).map((left, index) => {
    const right = boundaries[index + 1];
    const rawStart = left + 1;
    const rawEnd = right;
    const leading = /^\s*/.exec(text.slice(rawStart, rawEnd))?.[0].length ?? 0;
    const trailing = /\s*$/.exec(text.slice(rawStart, rawEnd))?.[0].length ?? 0;
    return {
      start: rawStart + leading,
      end: Math.max(rawStart + leading, rawEnd - trailing),
    };
  });
}

function moveTableCell(view: EditorView, direction: 'next' | 'previous') {
  const head = view.state.selection.main.head;
  const line = view.state.doc.lineAt(head);
  if (!isTableLine(line.text)) return false;
  const ranges = tableCellRanges(line.text);
  if (ranges.length === 0) return false;
  const column = Math.max(
    0,
    ranges.findIndex((range) => head - line.from >= range.start && head - line.from <= range.end),
  );
  const nextColumn = direction === 'next' ? column + 1 : column - 1;
  if (nextColumn >= 0 && nextColumn < ranges.length) {
    const target = ranges[nextColumn];
    view.dispatch({
      selection: EditorSelection.range(line.from + target.start, line.from + target.end),
      scrollIntoView: true,
    });
    return true;
  }
  const nextLineNumber = direction === 'next' ? line.number + 1 : line.number - 1;
  if (nextLineNumber < 1 || nextLineNumber > view.state.doc.lines) return true;
  const nextLine = view.state.doc.line(nextLineNumber);
  if (!isTableLine(nextLine.text)) return true;
  const nextRanges = tableCellRanges(nextLine.text);
  const target = direction === 'next' ? nextRanges[0] : nextRanges[nextRanges.length - 1];
  if (!target) return true;
  view.dispatch({
    selection: EditorSelection.range(nextLine.from + target.start, nextLine.from + target.end),
    scrollIntoView: true,
  });
  return true;
}

function buildLivePreviewDecorations(state: EditorState) {
  const activeLine = state.field(activeLineField);
  const decorations: Array<RangeLike> = [];
  const frontmatter = parseFrontmatter(state);
  let inFence = false;

  for (let lineNumber = 1; lineNumber <= state.doc.lines; lineNumber++) {
    const line = state.doc.line(lineNumber);
    const text = line.text;
    const trimmed = text.trim();
    const isActive = line.number === activeLine;
    const currentFence = inFence;
    if (trimmed.startsWith('```')) inFence = !inFence;
    const inFrontmatter = Boolean(frontmatter && line.number >= frontmatter.startLine && line.number <= frontmatter.endLine);

    if (isActive) continue;

    if (frontmatter && inFrontmatter) {
      if (line.number === frontmatter.startLine) {
        decorations.push({
          from: frontmatter.from,
          to: frontmatter.to,
          value: Decoration.replace({
            widget: new FrontmatterWidget(frontmatter.rows),
            block: true,
          }),
        });
        lineNumber = frontmatter.endLine;
      }
      continue;
    }

    const table = tableBlockAt(state, line.number);
    if (table && (activeLine < table.startLine || activeLine > table.endLine) && line.number === table.startLine) {
      decorations.push({
        from: table.from,
        to: table.to,
        value: Decoration.replace({
          widget: new TableWidget(table.rows),
          block: true,
        }),
      });
      lineNumber = table.endLine;
      continue;
    }

    const callout = calloutBlockAt(state, line.number);
    if (callout && (activeLine < callout.startLine || activeLine > callout.endLine) && line.number === callout.startLine) {
      decorations.push({
        from: callout.from,
        to: callout.to,
        value: Decoration.replace({
          widget: new CalloutBlockWidget(callout.kind, callout.title, callout.body, callout.from),
          block: true,
        }),
      });
      lineNumber = callout.endLine;
      continue;
    }

    const math = mathBlockAt(state, line.number);
    if (math && (activeLine < math.startLine || activeLine > math.endLine) && line.number === math.startLine) {
      decorations.push({
        from: math.from,
        to: math.to,
        value: Decoration.replace({
          widget: new MathBlockWidget(math.body, math.from),
          block: true,
        }),
      });
      lineNumber = math.endLine;
      continue;
    }

    const footnote = /^\s*\[\^([^\]\n]+)\]:\s*(.*)$/.exec(text);
    if (footnote) {
      decorations.push({
        from: line.from,
        to: line.to,
        value: Decoration.replace({
          widget: new FootnoteWidget(footnote[1].trim(), footnote[2].trim(), line.from),
          block: true,
        }),
      });
      continue;
    }

    const embed = lineEmbed(text);
    if (embed) {
      decorations.push({
        from: line.from,
        to: line.to,
        value: Decoration.replace({
          widget: new EmbedWidget(embed.kind, embed.label, embed.target),
        }),
      });
      continue;
    }

    const classes = lineClasses(text, currentFence);
    for (const className of classes) {
      decorations.push({ from: line.from, to: line.from, value: Decoration.line({ class: className }) });
    }

    const heading = /^(#{1,6})\s+/.exec(text);
    if (heading) {
      decorations.push({ from: line.from, to: line.from + heading[0].length, value: Decoration.mark({ class: 'cm-md-syntax-hidden' }) });
    }

    const task = /^(\s*[-*]\s+)(\[([ xX/-])\])\s+/.exec(text);
    if (task) {
      const checkboxFrom = line.from + task[1].length;
      const checkboxTo = checkboxFrom + task[2].length;
      decorations.push({ from: line.from, to: checkboxFrom, value: Decoration.mark({ class: 'cm-md-syntax-hidden' }) });
      decorations.push({
        from: checkboxFrom,
        to: checkboxTo,
        value: Decoration.replace({
          widget: new CheckboxWidget(task[3].toLowerCase() === 'x', checkboxFrom),
        }),
      });
      decorations.push({ from: checkboxTo, to: line.from + task[0].length, value: Decoration.mark({ class: 'cm-md-syntax-hidden' }) });
    }

    addInlineMarks(
      { add: (markFrom, markTo, value) => decorations.push({ from: markFrom, to: markTo, value }) },
      line.from,
      text,
    );
  }

  return Decoration.set(decorations.sort((a, b) => a.from - b.from || a.to - b.to), true);
}

interface RangeLike {
  from: number;
  to: number;
  value: Decoration;
}

const livePreviewField = StateField.define<DecorationSet>({
  create(state) {
    return buildLivePreviewDecorations(state);
  },
  update(value, transaction) {
    if (transaction.docChanged || transaction.selection || transaction.effects.some((effect) => effect.is(activeLineEffect))) {
      return buildLivePreviewDecorations(transaction.state);
    }
    return value;
  },
  provide: (field) => EditorView.decorations.from(field),
});

export const LiveMarkdownEditor = forwardRef<LiveMarkdownEditorHandle, LiveMarkdownEditorProps>(
  function LiveMarkdownEditor({ value, mode, onChange, onCursorLineChange, onCommand, readOnly }, ref) {
    const hostRef = useRef<HTMLDivElement | null>(null);
    const viewRef = useRef<EditorView | null>(null);
    const onChangeRef = useRef(onChange);
    const onCursorLineChangeRef = useRef(onCursorLineChange);
    const onCommandRef = useRef(onCommand);
    const valueRef = useRef(value);

    useEffect(() => {
      onChangeRef.current = onChange;
      onCursorLineChangeRef.current = onCursorLineChange;
      onCommandRef.current = onCommand;
      valueRef.current = value;
    }, [onChange, onCursorLineChange, onCommand, value]);

    const commandKeymap = useMemo(
      () => keymap.of([
        { key: 'Enter', run: continueMarkdownLine },
        { key: 'Tab', run: (view) => changeLineIndent(view, 'in') },
        { key: 'Shift-Tab', run: (view) => changeLineIndent(view, 'out') },
        { key: 'Mod-Alt-ArrowRight', run: (view) => moveTableCell(view, 'next') },
        { key: 'Mod-Alt-ArrowLeft', run: (view) => moveTableCell(view, 'previous') },
        { key: 'Mod-s', run: () => command('save') },
        { key: 'Mod-b', run: () => command('bold') },
        { key: 'Mod-i', run: () => command('italic') },
        { key: 'Mod-k', run: () => command('wikilink') },
        { key: 'Mod-Enter', run: () => command('task') },
        { key: 'Mod-Shift-7', run: () => command('numbered') },
        { key: 'Mod-Shift-8', run: () => command('bullet') },
        { key: 'Mod-p', run: () => command('palette') },
        { key: 'Mod-o', run: () => command('quick-open') },
      ]),
      [],
    );

    function command(id: string) {
      onCommandRef.current?.(id);
      return true;
    }

    useEffect(() => {
      if (!hostRef.current || viewRef.current) return;
      const state = EditorState.create({
        doc: valueRef.current,
        extensions: [
          lineNumbers(),
          highlightSpecialChars(),
          history(),
          drawSelection(),
          EditorState.allowMultipleSelections.of(true),
          markdown(),
          bracketMatching(),
          syntaxHighlighting(defaultHighlightStyle, { fallback: true }),
          highlightActiveLine(),
          activeLineField,
          EditorView.lineWrapping,
          EditorView.editable.of(!readOnly),
          EditorState.readOnly.of(Boolean(readOnly)),
          commandKeymap,
          keymap.of([...historyKeymap, ...defaultKeymap]),
          EditorView.updateListener.of((update) => {
            if (update.docChanged) {
              const next = update.state.doc.toString();
              valueRef.current = next;
              onChangeRef.current(next);
            }
            if (update.selectionSet || update.docChanged) {
              const line = update.state.doc.lineAt(update.state.selection.main.head).number;
              onCursorLineChangeRef.current?.(line);
              update.view.dispatch({ effects: activeLineEffect.of(line) });
            }
          }),
        ],
      });
      viewRef.current = new EditorView({ state, parent: hostRef.current });
      onCursorLineChangeRef.current?.(1);
      return () => {
        viewRef.current?.destroy();
        viewRef.current = null;
      };
    }, [commandKeymap, readOnly]);

    useEffect(() => {
      const view = viewRef.current;
      if (!view) return;
      const current = view.state.doc.toString();
      if (current === value) return;
      view.dispatch({
        changes: { from: 0, to: current.length, insert: value },
      });
    }, [value]);

    useEffect(() => {
      const view = viewRef.current;
      if (!view) return;
      const field = mode === 'live' ? livePreviewField : [];
      // Recreate mode-specific decorations by rebuilding the whole view; CM6 compartments
      // would be cleaner, but this keeps the editor deterministic across static export.
      const parent = hostRef.current;
      const selection = view.state.selection;
      const doc = view.state.doc.toString();
      view.destroy();
      if (!parent) return;
      const state = EditorState.create({
        doc,
        selection,
        extensions: [
          lineNumbers(),
          highlightSpecialChars(),
          history(),
          drawSelection(),
          EditorState.allowMultipleSelections.of(true),
          markdown(),
          bracketMatching(),
          syntaxHighlighting(defaultHighlightStyle, { fallback: true }),
          highlightActiveLine(),
          activeLineField,
          EditorView.lineWrapping,
          EditorView.editable.of(!readOnly),
          EditorState.readOnly.of(Boolean(readOnly)),
          commandKeymap,
          keymap.of([...historyKeymap, ...defaultKeymap]),
          ...(Array.isArray(field) ? field : [field]),
          EditorView.updateListener.of((update) => {
            if (update.docChanged) {
              const next = update.state.doc.toString();
              valueRef.current = next;
              onChangeRef.current(next);
            }
            if (update.selectionSet || update.docChanged) {
              const line = update.state.doc.lineAt(update.state.selection.main.head).number;
              onCursorLineChangeRef.current?.(line);
              update.view.dispatch({ effects: activeLineEffect.of(line) });
            }
          }),
        ],
      });
      viewRef.current = new EditorView({ state, parent });
    }, [commandKeymap, mode, readOnly]);

    useImperativeHandle(ref, () => ({
      focus() {
        viewRef.current?.focus();
      },
      getSelection() {
        const range = viewRef.current?.state.selection.main;
        return { start: range?.from ?? valueRef.current.length, end: range?.to ?? valueRef.current.length };
      },
      setSelectionRange(start, end = start, scroll = true) {
        const view = viewRef.current;
        if (!view) return;
        const boundedStart = Math.max(0, Math.min(start, view.state.doc.length));
        const boundedEnd = Math.max(0, Math.min(end, view.state.doc.length));
        view.dispatch({
          selection: EditorSelection.range(boundedStart, boundedEnd),
          scrollIntoView: scroll,
        });
        view.focus();
      },
      replaceSelection(text) {
        const view = viewRef.current;
        if (!view) return;
        const range = view.state.selection.main;
        view.dispatch({
          changes: { from: range.from, to: range.to, insert: text },
          selection: EditorSelection.cursor(range.from + text.length),
          scrollIntoView: true,
        });
        view.focus();
      },
      getCursor() {
        return viewRef.current?.state.selection.main.head ?? valueRef.current.length;
      },
      getCursorRect() {
        const view = viewRef.current;
        if (!view) return null;
        const rect = view.coordsAtPos(view.state.selection.main.head);
        return rect ? new DOMRect(rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top) : null;
      },
      focusLine(line) {
        const view = viewRef.current;
        if (!view) return { start: 0, end: 0, line: 1 };
        const bounded = Math.max(1, Math.min(line, view.state.doc.lines));
        const docLine = view.state.doc.line(bounded);
        view.dispatch({
          selection: EditorSelection.range(docLine.from, docLine.to),
          scrollIntoView: true,
        });
        view.focus();
        return { start: docLine.from, end: docLine.to, line: bounded };
      },
    }));

    return <div ref={hostRef} className="copal-live-markdown-editor" data-mode={mode} />;
  },
);
