/** Pure state and clipboard helpers for the native Editor spreadsheet leaf. */

import { canonicalSourceProperty } from './bases.js';

const DEFAULT_LABELS = Object.freeze({
  'file.name':'Name', 'file.path':'Path', 'file.ext':'Extension', tags:'Tags',
  status:'Status', created:'Created', modified:'Modified', links:'Links', kind:'Kind', 'file.tags':'Tags',
});
const DERIVED_PROPERTIES = new Set(['file.name', 'file.path', 'file.ext', 'file.folder', 'file.ctime', 'file.mtime', 'file.tags']);
const VIEW_TYPES = new Set(['table', 'card', 'list']);
const MAX_PASTE_ROWS = 500;
const MAX_PASTE_COLUMNS = 100;
const MAX_PASTE_CELLS = 1000;
const MAX_PASTE_BYTES = 1024 * 1024;

export function clone(value) {
  if (value === undefined) return undefined;
  if (typeof structuredClone === 'function') return structuredClone(value);
  return JSON.parse(JSON.stringify(value));
}

export function displayLabel(property, label = null) {
  const explicit = String(label ?? '').trim();
  if (explicit) return explicit;
  return DEFAULT_LABELS[property] || String(property || '').replace(/^file\./, '').replace(/[_-]+/g, ' ').replace(/\b\w/g, (letter) => letter.toUpperCase()) || 'Column';
}

export function normalizeSheetColumn(column = {}) {
  const property = canonicalSourceProperty(column.property || column.key);
  if (!property) throw new TypeError('A sheet column needs a property');
  const normalized = { ...clone(column), property, label:displayLabel(property, column.label) };
  if (column.width != null && Number.isFinite(Number(column.width))) normalized.width = Math.max(80, Math.min(600, Math.round(Number(column.width))));
  return normalized;
}

export function normalizeSheetView(view = {}, index = 0) {
  const source = view && typeof view === 'object' ? view : {};
  const columns = (Array.isArray(source.columns) && source.columns.length ? source.columns : [{ property:'file.name' }, { property:'tags' }, { property:'status' }]).map(normalizeSheetColumn);
  return {
    ...clone(source), id:String(source.id || `view-${index + 1}`), name:String(source.name || `View ${index + 1}`),
    type:VIEW_TYPES.has(source.type) ? source.type : 'table', columns,
    filters:source.filters == null ? null : clone(source.filters), sorts:Array.isArray(source.sorts) ? clone(source.sorts) : [],
    groupBy:source.groupBy == null ? null : String(source.groupBy), summaries:source.summaries && typeof source.summaries === 'object' ? clone(source.summaries) : {},
    limit:Math.max(1, Math.min(5000, Math.round(Number(source.limit) || 1000))),
  };
}

export function normalizeSheetDefinition(definition = {}) {
  const source = definition && typeof definition === 'object' ? definition : {};
  const views = (Array.isArray(source.views) ? source.views : []).map(normalizeSheetView);
  if (!views.length) views.push(normalizeSheetView({ id:'table', name:'Table', columns:[{ property:'file.name' }, { property:'tags' }, { property:'status' }] }, 0));
  return { ...clone(source), version:Number(source.version) || 1, views, extensions:source.extensions && typeof source.extensions === 'object' ? clone(source.extensions) : {} };
}

export function sheetTitle(name = '', fallback = 'Sheet') {
  const value = String(name || '').trim().replace(/\\/g, '/').split('/').at(-1) || '';
  return (value.replace(/\.base$/i, '') || fallback).trim();
}

export function rowKey(row = {}) {
  const key = row.resourceKey || row.resource?.key || row.documentId || row.id || row.name;
  return typeof key === 'string' ? key : JSON.stringify(key || 'row');
}

/** Preserve the provider identity carried by a query row for mutation commands. */
export function rowIdentity(row = {}) {
  return {
    documentId:row.documentId ?? row.id ?? null,
    resourceKey:clone(row.resourceKey || row.resource?.key || null),
  };
}

export function cellKey(row, column) { return `${rowKey(row)}::${String(column?.property || '')}`; }

export function readCell(row = {}, column = {}) {
  const property = String(column.property || '');
  if (property === 'file.name' || property === 'name') return row.name ?? row.values?.[property] ?? '';
  if (property === 'file.path') return row.path ?? row.values?.[property] ?? '';
  if (property === 'file.ext') return row.extension ?? row.ext ?? row.values?.[property] ?? '';
  return row.values?.[property];
}

export function isReadOnlyColumn(column = {}, schema = {}) {
  const property = String(column.property || '');
  return column.readOnly === true || Boolean(column.formula) || property.startsWith('formula.') || DERIVED_PROPERTIES.has(property) || schema[property]?.source === 'file' || schema[property]?.computed === true;
}

export function cellEditorKind(row, column, schema = {}) {
  if (isReadOnlyColumn(column, schema)) return 'readonly';
  const property = String(column.property || '');
  const declared = String(column.type || schema[property]?.type || '').toLowerCase();
  if (['boolean', 'checkbox'].includes(declared)) return 'checkbox';
  if (['number', 'integer', 'float'].includes(declared)) return 'number';
  if (['date', 'datetime'].includes(declared)) return declared;
  if (['tags', 'list'].includes(declared)) return 'list';
  const value = readCell(row, column);
  if (typeof value === 'boolean') return 'checkbox';
  if (typeof value === 'number') return 'number';
  if (Array.isArray(value)) return 'list';
  return 'text';
}

export function createSheetState({ resourceKey = null, definition = {}, viewId = null, rows = [], counts = {}, scope = null, definitionRevision = null } = {}) {
  const normalized = normalizeSheetDefinition(definition);
  const normalizedRows = Array.isArray(rows) ? rows : [];
  const activeViewId = normalized.views.some((view) => view.id === viewId) ? viewId : normalized.views[0].id;
  return {
    resourceKey:clone(resourceKey), scope:clone(scope), definitionRevision:clone(definitionRevision), definition:normalized,
    viewId:activeViewId, rows:clone(normalizedRows), selected:null, editing:null,
    query:{ text:'', filters:null }, status:'ready', error:null, generation:0,
    counts:{ shown:normalizedRows.length, matched:counts.matched ?? normalizedRows.length, limited:counts.limited ?? null, total:counts.total ?? null, truncated:counts.truncated === true },
    density:'compact', wrap:false,
  };
}

export function activeSheetView(state) { return state?.definition?.views?.find((view) => view.id === state.viewId) || state?.definition?.views?.[0] || null; }
export function visibleColumnIndexes(view) { return (view?.columns || []).map((column, index) => column.visible === false ? -1 : index).filter((index) => index >= 0); }

export function setSheetRows(state, rows, counts = {}, { generation = state.generation, status = 'ready' } = {}) {
  const nextRows = Array.isArray(rows) ? clone(rows) : [];
  return { ...state, rows:nextRows, status, error:null, generation, counts:{ ...state.counts, ...counts, shown:nextRows.length } };
}

export function selectCell(state, rowIndex, columnIndex, { extend = false } = {}) {
  const view = activeSheetView(state);
  const row = state.rows?.[rowIndex]; const column = view?.columns?.[columnIndex];
  if (!row || !column) return state;
  const point = { rowKey:rowKey(row), columnKey:String(column.property) };
  const selected = extend && state.selected ? { anchor:state.selected.anchor, focus:point } : { anchor:point, focus:point };
  return { ...state, selected, editing:null };
}

export function moveSelection(state, rowDelta, columnDelta, { extend = false } = {}) {
  const view = activeSheetView(state); if (!view || !state.rows.length) return state;
  const selected = state.selected?.focus || state.selected?.anchor;
  const rowIndex = Math.max(0, state.rows.findIndex((row) => rowKey(row) === selected?.rowKey));
  const indexes = visibleColumnIndexes(view); if (!indexes.length) return state;
  const actual = view.columns.findIndex((column) => column.property === selected?.columnKey);
  const position = Math.max(0, indexes.indexOf(actual));
  const nextPosition = Math.min(indexes.length - 1, Math.max(0, position + columnDelta));
  return selectCell(state, Math.min(state.rows.length - 1, rowIndex + rowDelta), indexes[nextPosition], { extend });
}

export function selectionBounds(state) {
  const view = activeSheetView(state); const selection = state.selected;
  if (!view || !selection) return null;
  const rowA = state.rows.findIndex((row) => rowKey(row) === selection.anchor.rowKey);
  const rowB = state.rows.findIndex((row) => rowKey(row) === selection.focus.rowKey);
  const colA = view.columns.findIndex((column) => column.property === selection.anchor.columnKey);
  const colB = view.columns.findIndex((column) => column.property === selection.focus.columnKey);
  if ([rowA, rowB, colA, colB].some((value) => value < 0)) return null;
  return { rowStart:Math.min(rowA, rowB), rowEnd:Math.max(rowA, rowB), columnStart:Math.min(colA, colB), columnEnd:Math.max(colA, colB) };
}

export function formatCellValue(value) {
  if (value == null) return '';
  if (Array.isArray(value)) return value.join(', ');
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value);
}

export function copySelection(state) {
  const bounds = selectionBounds(state); const view = activeSheetView(state);
  if (!bounds || !view) return '';
  const lines = [];
  for (let rowIndex = bounds.rowStart; rowIndex <= bounds.rowEnd; rowIndex += 1) {
    const row = state.rows[rowIndex]; const values = [];
    for (const columnIndex of visibleColumnIndexes(view).filter((index) => index >= bounds.columnStart && index <= bounds.columnEnd)) {
      const value = formatCellValue(readCell(row, view.columns[columnIndex]));
      values.push(/[\t\r\n"]/.test(value) ? `"${value.replace(/"/g, '""')}"` : value);
    }
    lines.push(values.join('\t'));
  }
  return lines.join('\n');
}

export function parseTsv(value, { maxRows = MAX_PASTE_ROWS, maxColumns = MAX_PASTE_COLUMNS } = {}) {
  const raw = String(value ?? '');
  const encoded = new TextEncoder().encode(raw);
  const bounded = encoded.length > MAX_PASTE_BYTES ? new TextDecoder().decode(encoded.slice(0, MAX_PASTE_BYTES)) : raw;
  const rows = []; let row = []; let field = ''; let quoted = false; let truncated = encoded.length > MAX_PASTE_BYTES;
  const pushField = () => { row.push(field); field = ''; if (row.length > maxColumns) { row.length = maxColumns; truncated = true; } };
  const pushRow = () => { if (rows.length < maxRows) rows.push(row); else truncated = true; row = []; };
  for (let index = 0; index < bounded.length; index += 1) {
    const character = bounded[index];
    if (quoted) {
      if (character === '"' && bounded[index + 1] === '"') { field += '"'; index += 1; }
      else if (character === '"') quoted = false;
      else field += character;
    } else if (character === '"' && field === '') quoted = true;
    else if (character === '\t') pushField();
    else if (character === '\n' || character === '\r') { if (character === '\r' && bounded[index + 1] === '\n') index += 1; pushField(); pushRow(); }
    else field += character;
  }
  if (field || row.length) { pushField(); pushRow(); }
  const cellCount = rows.reduce((total, cells) => total + cells.length, 0);
  if (cellCount > MAX_PASTE_CELLS) {
    truncated = true; let remaining = MAX_PASTE_CELLS;
    for (const cells of rows) { if (remaining <= 0) { cells.length = 0; continue; } if (cells.length > remaining) cells.length = remaining; remaining -= cells.length; }
  }
  return { rows, truncated };
}

function validatePasteValue(value, kind) {
  const raw = String(value ?? '').trim();
  if (kind === 'number') {
    if (!raw) return { ok:true, value:null };
    const parsed = Number(raw);
    return Number.isFinite(parsed) ? { ok:true, value:parsed } : { ok:false, reason:'invalid number' };
  }
  if (kind === 'date') {
    if (!raw) return { ok:true, value:null };
    const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(raw);
    if (!match) return { ok:false, reason:'invalid date' };
    const parsed = new Date(`${raw}T00:00:00Z`);
    return !Number.isNaN(parsed.valueOf()) && parsed.getUTCFullYear() === Number(match[1]) && parsed.getUTCMonth() + 1 === Number(match[2]) && parsed.getUTCDate() === Number(match[3])
      ? { ok:true, value:raw } : { ok:false, reason:'invalid date' };
  }
  if (kind === 'datetime') {
    if (!raw) return { ok:true, value:null };
    return Number.isNaN(Date.parse(raw)) ? { ok:false, reason:'invalid datetime' } : { ok:true, value:raw };
  }
  return { ok:true, value };
}

export function pastePreview(state, text, { schema = {} } = {}) {
  const bounds = selectionBounds(state); const view = activeSheetView(state); const matrix = parseTsv(text);
  if (!bounds || !view || !matrix.rows.length) return { cells:[], rejected:[], truncated:matrix.truncated };
  const origin = state.selected.focus || state.selected.anchor; const rowStart = state.rows.findIndex((row) => rowKey(row) === origin.rowKey); const visibleIndexes = visibleColumnIndexes(view); const colStart = visibleIndexes.findIndex((index) => view.columns[index].property === origin.columnKey);
  const cells = []; const rejected = [];
  matrix.rows.forEach((values, rowOffset) => values.forEach((value, columnOffset) => {
    const row = state.rows[rowStart + rowOffset]; const column = view.columns[visibleIndexes[colStart + columnOffset]];
    if (!row || !column) { rejected.push({ rowOffset, columnOffset, reason:'outside loaded grid' }); return; }
    const key = cellKey(row, column);
    const identity = rowIdentity(row);
    if (isReadOnlyColumn(column, schema)) { rejected.push({ key, ...identity, rowKey:rowKey(row), columnKey:column.property, value, reason:'read-only' }); return; }
    const editorKind = cellEditorKind(row, column, schema);
    const validated = validatePasteValue(value, editorKind);
    if (!validated.ok) { rejected.push({ key, ...identity, rowKey:rowKey(row), columnKey:column.property, value, editorKind, reason:validated.reason }); return; }
    cells.push({ key, ...identity, rowKey:rowKey(row), columnKey:column.property, value:validated.value, editorKind });
  }));
  return { cells, rejected, truncated:matrix.truncated };
}

export function clearSelection(state, { schema = {} } = {}) {
  const bounds = selectionBounds(state); const view = activeSheetView(state); const cells = [];
  if (!bounds || !view) return cells;
  for (let rowIndex = bounds.rowStart; rowIndex <= bounds.rowEnd; rowIndex += 1) for (const columnIndex of visibleColumnIndexes(view).filter((index) => index >= bounds.columnStart && index <= bounds.columnEnd)) {
    const row = state.rows[rowIndex]; const column = view.columns[columnIndex];
    if (!isReadOnlyColumn(column, schema)) cells.push({ key:cellKey(row, column), ...rowIdentity(row), rowKey:rowKey(row), columnKey:column.property, value:null, clear:true });
  }
  return cells;
}

export { DEFAULT_LABELS, MAX_PASTE_BYTES, MAX_PASTE_CELLS, MAX_PASTE_COLUMNS, MAX_PASTE_ROWS };
