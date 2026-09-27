import { registerAdapter } from '../custom-context-menu.js';
import {
  COLUMN_TYPES, normalizeColumnType, normalizeTypeFormat,
  parseTypedCell, formatTypedCell, sortBodyRows, parseCurrencyCell,
} from './tableTypes.js';

// TableModel — Markdown table parser, serializer, mutation engine, and formula evaluator.
// Parses Markdown tables into a structured model with source-position tracking.
// All mutations produce valid Markdown and CodeMirror-compatible change arrays.
// Optional adjacent `clank-table` HTML-comment metadata carries column types
// and display formats; plain Markdown readers still see a normal table.

export { COLUMN_TYPES };

// ─── Parser ──────────────────────────────────────────────────────────────────

function splitCells(line) {
  // Split a table row by unescaped pipes, respecting escaped pipes and inline code.
  const cells = [];
  let current = '';
  let inCode = false;
  let i = 0;
  const trimmed = line.trim();
  // Strip leading/trailing pipe
  const src = trimmed.replace(/^\|/, '').replace(/\|$/, '');
  for (i = 0; i < src.length; i++) {
    const ch = src[i];
    if (ch === '`') { inCode = !inCode; current += ch; continue; }
    if (ch === '\\' && i + 1 < src.length && src[i + 1] === '|' && !inCode) { current += '|'; i++; continue; }
    if (ch === '|' && !inCode) { cells.push(current.trim()); current = ''; continue; }
    current += ch;
  }
  cells.push(current.trim());
  return cells;
}

function parseAlignment(spec) {
  const s = spec.trim();
  if (s.startsWith(':') && s.endsWith(':')) return 'center';
  if (s.startsWith(':')) return 'left';
  if (s.endsWith(':')) return 'right';
  return 'left';
}

function isSeparatorLine(line) {
  const trimmed = line.trim();
  // Must contain at least one pipe and dashes
  if (!trimmed.includes('|') || !/-{3,}/.test(trimmed)) return false;
  // Strip leading/trailing pipes, split by |, check each cell is a valid separator spec
  const cells = trimmed.replace(/^\|/, '').replace(/\|$/, '').split('|');
  return cells.every((c) => /^\s*:?-{3,}:?\s*$/.test(c));
}

function isTableHeaderLine(line) {
  return !!line && line.includes('|') && line.trim().length > 0;
}

// ─── Adjacent clank-table metadata ──────────────────────────────────────────
// Readable HTML-comment block directly above a Markdown table:
//
//   <!-- clank-table v=1 id=tbl-1
//   column id=col-1 type=date format=iso
//   column id=col-2 type=currency format=code
//   -->
//
// Unknown header keys and unknown body lines are preserved verbatim.

const META_OPEN = '<!-- clank-table';
const META_CLOSE = '-->';

function parseMetadataLine(text) {
  const line = String(text || '');
  const open = line.indexOf(META_OPEN);
  if (open < 0) return null;
  const after = line.slice(open + META_OPEN.length);
  const close = after.indexOf(META_CLOSE);
  if (close >= 0) return { body: after.slice(0, close).split('\n'), closed: true, raw: line };
  return { body: after.split('\n'), closed: false, raw: line };
}

export function parseTableMetadata(text) {
  const raw = String(text || '');
  const parsed = parseMetadataLine(raw);
  if (!parsed) return null;
  const meta = {
    version: 1,
    tableId: '',
    columns: [],
    extras: [],
  };
  const lines = [];
  for (const piece of parsed.body) lines.push(...piece.split('\n'));
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i].trim();
    if (!line) continue;
    if (line.startsWith('column ')) {
      const entry = { id: '', type: '', format: '', extras: [] };
      for (const token of line.slice('column '.length).trim().split(/\s+/)) {
        const eq = token.indexOf('=');
        if (eq < 0) { entry.extras.push(token); continue; }
        const key = token.slice(0, eq);
        const value = token.slice(eq + 1);
        if (key === 'id') entry.id = value;
        else if (key === 'type') entry.type = normalizeColumnType(value) || value;
        else if (key === 'format') entry.format = value;
        else entry.extras.push(token);
      }
      meta.columns.push(entry);
      continue;
    }
    let matched = false;
    for (const token of line.split(/\s+/)) {
      const eq = token.indexOf('=');
      if (eq < 0) continue;
      const key = token.slice(0, eq);
      const value = token.slice(eq + 1);
      if (key === 'v' || key === 'version') { meta.version = Number(value) || 1; matched = true; }
      else if (key === 'id') { meta.tableId = value; matched = true; }
    }
    if (!matched) meta.extras.push(line);
    else {
      // Preserve unknown keys from the header line.
      for (const token of line.split(/\s+/)) {
        const eq = token.indexOf('=');
        if (eq < 0) continue;
        const key = token.slice(0, eq);
        if (key !== 'v' && key !== 'version' && key !== 'id') meta.extras.push(token);
      }
    }
  }
  return meta;
}

export function serializeTableMetadata(meta) {
  if (!meta) return '';
  const parts = [`v=${meta.version || 1}`];
  if (meta.tableId) parts.push(`id=${meta.tableId}`);
  const lines = [`${META_OPEN} ${parts.join(' ')}`];
  for (const col of meta.columns) {
    const bits = [];
    if (col.id) bits.push(`id=${col.id}`);
    const type = normalizeColumnType(col.type);
    if (type) bits.push(`type=${type}`);
    const format = col.format ? normalizeTypeFormat(type || 'text', col.format) : '';
    if (format && format !== 'auto') bits.push(`format=${format}`);
    else if (col.format) bits.push(`format=${col.format}`);
    for (const extra of col.extras || []) bits.push(extra);
    lines.push(`column ${bits.join(' ')}`);
  }
  for (const extra of meta.extras || []) lines.push(extra);
  lines.push(META_CLOSE);
  return lines.join('\n');
}

function cloneMetadata(meta) {
  if (!meta) return null;
  return {
    version: meta.version || 1,
    tableId: meta.tableId || '',
    columns: (meta.columns || []).map((col) => ({
      id: col.id || '',
      type: col.type || '',
      format: col.format || '',
      extras: [...(col.extras || [])],
    })),
    extras: [...(meta.extras || [])],
  };
}

function ensureColumnIds(meta, columnCount) {
  const next = cloneMetadata(meta) || { version: 1, tableId: '', columns: [], extras: [] };
  if (!next.tableId) next.tableId = `tbl-${stableId()}`;
  while (next.columns.length < columnCount) {
    next.columns.push({ id: `col-${stableId()}`, type: '', format: '', extras: [] });
  }
  if (next.columns.length > columnCount) next.columns = next.columns.slice(0, columnCount);
  for (const col of next.columns) {
    if (!col.id) col.id = `col-${stableId()}`;
  }
  return next;
}

function stableId() {
  return Math.random().toString(36).slice(2, 8);
}

function findMetadataBlock(lines, tableStart) {
  // Walk up from the table header over blank lines to a clank-table comment.
  // The comment may be a single line or a block ending in `-->`.
  let index = tableStart - 1;
  while (index >= 0 && !String(lines[index].text ?? lines[index]).trim()) index--;
  if (index < 0) return null;
  const lineText = (i) => String(lines[i]?.text ?? lines[i] ?? '');
  const candidate = lineText(index);
  // Closing line of a multi-line comment: continue upward to the opener.
  const looksLikeClose = candidate.trim() === META_CLOSE || (candidate.includes(META_CLOSE) && !candidate.includes(META_OPEN));
  if (!candidate.includes(META_OPEN) && !looksLikeClose) return null;
  let openIndex = index;
  if (!candidate.includes(META_OPEN)) {
    openIndex = -1;
    for (let j = index; j >= 0; j--) {
      if (lineText(j).includes(META_OPEN)) { openIndex = j; break; }
    }
    if (openIndex < 0) return null;
  }
  const openText = lineText(openIndex);
  const openAt = openText.lastIndexOf(META_OPEN);
  if (openText.slice(0, openAt).trim()) return null;
  // Single-line form: open and close on the same line.
  if (openIndex === index && openText.includes(META_CLOSE, openAt)) {
    return { from: lines[openIndex].line, to: lines[openIndex].line, raw: openText };
  }
  if (!lineText(index).includes(META_CLOSE)) return null;
  const rawLines = [];
  for (let j = openIndex; j <= index; j++) rawLines.push(lineText(j));
  return {
    from: lines[openIndex].line,
    to: lines[index].line,
    raw: rawLines.join('\n'),
  };
}

export function parseTable(text, startLine = 0) {
  const source = String(text || '');
  const allLines = source.split('\n');
  const lines = [];
  for (let i = 0; i < allLines.length; i++) {
    lines.push({ text: allLines[i], line: startLine + i });
  }

  // Find first table-like line (contains |), skipping any leading metadata comment.
  let start = -1;
  let skipUntil = -1;
  for (let i = 0; i < lines.length; i++) {
    const trimmed = lines[i].text.trim();
    if (i <= skipUntil) continue;
    if (trimmed.includes(META_OPEN)) {
      // Skip the whole comment (single- or multi-line).
      if (!trimmed.includes(META_CLOSE)) {
        let j = i + 1;
        while (j < lines.length && !lines[j].text.includes(META_CLOSE)) j++;
        skipUntil = j;
      }
      continue;
    }
    if (isTableHeaderLine(lines[i].text) && i + 1 < lines.length && isSeparatorLine(lines[i + 1]?.text)) {
      start = i;
      break;
    }
    if (isTableHeaderLine(lines[i].text)) { start = i; break; }
  }
  if (start < 0) return { valid: false, malformed: 'no-table' };

  const metadataBlock = findMetadataBlock(lines, start);
  const metadata = metadataBlock ? parseTableMetadata(metadataBlock.raw) : null;

  // Check separator line
  const sepIndex = start + 1;
  if (sepIndex >= lines.length || !isSeparatorLine(lines[sepIndex].text)) {
    return {
      valid: false,
      malformed: 'missing-separator',
      sourceRange: { from: lines[start].line, to: lines[start].line },
      blockRange: metadataBlock
        ? { from: metadataBlock.from, to: lines[start].line }
        : { from: lines[start].line, to: lines[start].line },
      metadata,
    };
  }

  // Parse header
  const headerCells = splitCells(lines[start].text);
  const colCount = headerCells.length;

  // Parse alignment row
  const alignCells = splitCells(lines[sepIndex].text);
  const alignments = alignCells.map(parseAlignment);
  // Pad or truncate alignments to match header
  while (alignments.length < colCount) alignments.push('left');

  // Parse body rows
  const rows = [];

  // Header row
  const headerOffsets = computeCellOffsets(lines[start].text, headerCells);
  rows.push({
    cells: headerCells,
    isHeader: true,
    sourceLine: lines[start].line,
    sourceText: lines[start].text,
    cellOffsets: headerOffsets,
  });

  // Body rows
  let bodyEnd = sepIndex;
  for (let i = sepIndex + 1; i < lines.length; i++) {
    if (!lines[i].text.includes('|') || lines[i].text.trim() === '') break;
    const rowCells = splitCells(lines[i].text);
    const offsets = computeCellOffsets(lines[i].text, rowCells);
    rows.push({
      cells: rowCells,
      isHeader: false,
      sourceLine: lines[i].line,
      sourceText: lines[i].text,
      cellOffsets: offsets,
    });
    bodyEnd = i;
  }

  // Malformed detection
  const malformReasons = [];
  for (let i = 1; i < rows.length; i++) {
    if (rows[i].cells.length !== colCount) {
      malformReasons.push(`row-${i}-col-count`);
    }
  }
  // Check for non-pipe delimiters in separator
  const sepSpec = alignCells.join('|');
  if (/[^|\s:\-]/.test(sepSpec)) malformReasons.push('invalid-separator');

  const sourceRange = { from: lines[start].line, to: lines[bodyEnd].line };
  const blockRange = metadataBlock
    ? { from: metadataBlock.from, to: lines[bodyEnd].line }
    : { from: sourceRange.from, to: sourceRange.to };

  const model = {
    valid: malformReasons.length === 0,
    malformed: malformReasons.length ? malformReasons.join(',') : undefined,
    rows,
    columns: colCount,
    alignments: alignments.slice(0, colCount),
    sourceRange,
    blockRange,
    metadata: metadata ? alignMetadata(metadata, colCount) : null,
  };
  return model;
}

function alignMetadata(meta, columnCount) {
  const next = cloneMetadata(meta);
  while (next.columns.length < columnCount) {
    next.columns.push({ id: '', type: '', format: '', extras: [] });
  }
  if (next.columns.length > columnCount) next.columns = next.columns.slice(0, columnCount);
  return next;
}

function computeCellOffsets(lineText, cells) {
  // Compute char ranges for each cell within the raw line text.
  const offsets = [];
  let trimmed = lineText.trim();
  const src = trimmed.replace(/^\|/, '');
  let cellIdx = 0;
  let cellStart = -1;
  let inCode = false;

  for (let i = 0; i < src.length && cellIdx < cells.length; i++) {
    const ch = src[i];
    if (ch === '`') { inCode = !inCode; continue; }
    if (ch === '\\' && i + 1 < src.length && src[i + 1] === '|' && !inCode) { i++; continue; }
    if (ch === '|' && !inCode) {
      if (cellStart >= 0) {
        offsets.push({ from: cellStart, to: i });
      }
      cellStart = i + 1;
      cellIdx++;
      continue;
    }
  }
  // Last cell
  if (cellIdx < cells.length && cellStart >= 0) {
    offsets.push({ from: cellStart, to: src.length });
  }
  return offsets;
}

// ─── Serializer ──────────────────────────────────────────────────────────────

function escapeCellText(text) {
  // Re-escape pipes outside inline code so cell content round-trips.
  const s = String(text ?? '');
  let out = '';
  let inCode = false;
  for (let i = 0; i < s.length; i++) {
    const ch = s[i];
    if (ch === '`') { inCode = !inCode; out += ch; continue; }
    if (ch === '|' && !inCode) { out += '\\|'; continue; }
    out += ch;
  }
  return out;
}

export function tableToSource(model, lineEnding = '\n') {
  if (!model?.valid || !model.rows?.length) return '';
  const escaped = model.rows.map((row) => {
    const cells = [];
    for (let c = 0; c < model.columns; c++) cells.push(escapeCellText(row.cells[c] || ''));
    return cells;
  });
  const colWidths = [];
  for (let c = 0; c < model.columns; c++) {
    let max = 3; // minimum width for separator
    for (const cells of escaped) {
      if (cells[c].length > max) max = cells[c].length;
    }
    colWidths.push(max);
  }

  const pad = (s, w) => s + ' '.repeat(Math.max(0, w - s.length));
  const alignSep = (align, w) => {
    const dash = '-'.repeat(Math.max(3, w - 2));
    if (align === 'center') return `:${dash}:`;
    if (align === 'right') return `${dash}:`;
    if (align === 'left') return `:${dash}`;
    return dash;
  };

  const lines = [];
  for (let r = 0; r < escaped.length; r++) {
    const cells = [];
    for (let c = 0; c < model.columns; c++) {
      cells.push(pad(escaped[r][c] || '', colWidths[c]));
    }
    lines.push('| ' + cells.join(' | ') + ' |');
    // Insert separator after header
    if (r === 0) {
      const seps = [];
      for (let c = 0; c < model.columns; c++) {
        seps.push(alignSep(model.alignments[c] || 'left', colWidths[c]));
      }
      lines.push('| ' + seps.join(' | ') + ' |');
    }
  }
  return lines.join(lineEnding);
}

export function tableBlockToSource(model, lineEnding = '\n') {
  const table = tableToSource(model, lineEnding);
  const meta = model?.metadata ? serializeTableMetadata(model.metadata) : '';
  return meta ? `${meta.split('\n').join(lineEnding)}${lineEnding}${table}` : table;
}

// ─── Formula reference remapping ─────────────────────────────────────────────
// Structural edits rewrite A1-style references so formulas keep pointing at the
// same logical cells. Column identity is also tracked in metadata ids.

const REF_TOKEN = /([A-Z]+)(\d+)/gi;

function colToIndex(letter) {
  // A=0, B=1, ..., Z=25, AA=26
  let idx = 0;
  for (let i = 0; i < letter.length; i++) {
    idx = idx * 26 + (letter.charCodeAt(i) - 64);
  }
  return idx - 1;
}

function indexToCol(index) {
  let n = index + 1;
  let letters = '';
  while (n > 0) {
    const rem = (n - 1) % 26;
    letters = String.fromCharCode(65 + rem) + letters;
    n = Math.floor((n - 1) / 26);
  }
  return letters;
}

function mapIndex(map, index) {
  if (!map) return index;
  return map.has(index) ? map.get(index) : -1;
}

function refOrError(colIndex, rowIndex) {
  if (colIndex < 0 || rowIndex < 0) return '#REF!';
  return `${indexToCol(colIndex)}${rowIndex}`;
}

// remapFormulaReferences — rewrite one formula string.
// colMap/rowMap: Map(oldIndex -> newIndex); missing key means deleted.
// remapRangeRows: false keeps range endpoints positional (sort semantics).
export function remapFormulaReferences(formula, { colMap = null, rowMap = null, remapRangeRows = true } = {}) {
  if (typeof formula !== 'string' || !formula.startsWith('=')) return formula;
  const head = formula.slice(0, 1);
  const body = formula.slice(1);
  // Tokenize ranges first and stash them behind placeholders so the single-ref
  // pass cannot corrupt range endpoints.
  const rangeRe = /([A-Z]+)(\d+):([A-Z]+)(\d+)/gi;
  const stash = [];
  const withPlaceholders = body.replace(rangeRe, (whole, a, b, c, d) => {
    const startCol = mapIndex(colMap, colToIndex(String(a).toUpperCase()));
    const endCol = mapIndex(colMap, colToIndex(String(c).toUpperCase()));
    const startRow = remapRangeRows ? mapIndex(rowMap, parseInt(b, 10)) : parseInt(b, 10);
    const endRow = remapRangeRows ? mapIndex(rowMap, parseInt(d, 10)) : parseInt(d, 10);
    let replacement;
    if (startCol < 0 || endCol < 0 || startRow < 0 || endRow < 0) {
      replacement = '#REF!';
    } else {
      replacement = indexToCol(startCol) + startRow + ':' + indexToCol(endCol) + endRow;
    }
    stash.push(replacement);
    return '\u0001' + (stash.length - 1) + '\u0001';
  });
  let out = withPlaceholders.replace(REF_TOKEN, (whole, letters, digits) => {
    const colIndex = mapIndex(colMap, colToIndex(String(letters).toUpperCase()));
    const rowIndex = mapIndex(rowMap, parseInt(digits, 10));
    if (colIndex < 0 || rowIndex < 0) return '#REF!';
    return refOrError(colIndex, rowIndex);
  });
  out = out.replace(/\u0001(\d+)\u0001/g, (whole, index) => stash[Number(index)] ?? whole);
  return head + out;
}

function buildShiftMap(oldCount, { insertAt = -1, deleteAt = -1, moveFrom = -1, moveTo = -1, permutation = null } = {}) {
  const map = new Map();
  if (permutation) {
    // permutation[oldIndex] = newIndex (only for indices being reordered)
    for (const [oldIndex, newIndex] of permutation) {
      if (oldIndex !== newIndex) map.set(oldIndex, newIndex);
    }
    return map;
  }
  for (let i = 0; i < oldCount; i++) {
    if (deleteAt >= 0) {
      if (i === deleteAt) continue;
      map.set(i, i > deleteAt ? i - 1 : i);
      continue;
    }
    if (insertAt >= 0) {
      map.set(i, i >= insertAt ? i + 1 : i);
      continue;
    }
    if (moveFrom >= 0 && moveTo >= 0) {
      if (i === moveFrom) map.set(i, moveTo);
      else if (moveFrom < moveTo) map.set(i, (i > moveFrom && i <= moveTo) ? i - 1 : i);
      else map.set(i, (i >= moveTo && i < moveFrom) ? i + 1 : i);
      continue;
    }
    map.set(i, i);
  }
  return map;
}

function remapModelFormulas(model, { colMap, rowMap, remapRangeRows = true }) {
  for (const row of model.rows) {
    row.cells = row.cells.map((cell) => {
      if (typeof cell === 'string' && cell.startsWith('=')) {
        return remapFormulaReferences(cell, { colMap, rowMap, remapRangeRows });
      }
      return cell;
    });
  }
}

function remapMetadataColumns(meta, colMap, columnCount) {
  if (!meta) return null;
  const next = cloneMetadata(meta);
  const rebuilt = [];
  // Place surviving columns at their new indices so order tracks the table.
  const placed = new Array(columnCount).fill(null);
  for (const [oldIndex, newIndex] of colMap) {
    if (newIndex >= 0 && newIndex < columnCount && next.columns[oldIndex]) {
      placed[newIndex] = next.columns[oldIndex];
    }
  }
  for (let i = 0; i < columnCount; i++) {
    rebuilt.push(placed[i] || { id: `col-${stableId()}`, type: '', format: '', extras: [] });
  }
  for (const col of rebuilt) if (!col.id) col.id = `col-${stableId()}`;
  next.columns = rebuilt;
  return next;
}

function setColumnTypesInMetadata(meta, assignments) {
  const next = cloneMetadata(meta);
  if (!next) return null;
  for (const [colIndex, type, format] of assignments) {
    if (!next.columns[colIndex]) continue;
    next.columns[colIndex].type = normalizeColumnType(type) || type;
    if (format != null) next.columns[colIndex].format = format;
    else if (next.columns[colIndex].format) {
      next.columns[colIndex].format = normalizeTypeFormat(next.columns[colIndex].type, next.columns[colIndex].format);
    }
  }
  return next;
}

// ─── Source synchronization ──────────────────────────────────────────────────

export function applyTableEdit(doc, model, edit) {
  // doc: full document string
  // model: parsed table model
  // edit: { type, ... } — see below
  // Returns: { newText, newModel, changes }
  //   changes: array of { from, to, insert } for CodeMirror dispatch
  // Each call produces exactly one change, so one logical undo step.

  const source = String(doc || '');
  const lines = source.split('\n');

  switch (edit.type) {
    case 'cell': return editCell(lines, model, edit);
    case 'insertRow': return insertRow(lines, model, edit);
    case 'deleteRow': return deleteRow(lines, model, edit);
    case 'insertColumn': return insertColumn(lines, model, edit);
    case 'deleteColumn': return deleteColumn(lines, model, edit);
    case 'moveRow': return moveRow(lines, model, edit);
    case 'moveColumn': return moveColumn(lines, model, edit);
    case 'setAlignment': return setAlignment(lines, model, edit);
    case 'setColumnType': return setColumnType(lines, model, edit);
    case 'sort': return sortTable(lines, model, edit);
    case 'transpose': return transposeTable(lines, model, edit);
    default: return { newText: source, newModel: model, changes: [] };
  }
}

function charOffset(lines, line, col) {
  // Compute absolute char offset in doc for a given line and column.
  let offset = 0;
  for (let i = 0; i < line; i++) offset += lines[i].length + 1; // +1 for \n
  return offset + col;
}

function detectLineEnding(lines) {
  for (const line of lines) {
    if (typeof line === 'string' && line.endsWith('\r')) return '\r\n';
  }
  return '\n';
}

function rebuildAndDiff(lines, model, nextModel) {
  // Rebuild the table block (metadata comment + table) and compute one change.
  const lineEnding = detectLineEnding(lines);
  const newSource = tableBlockToSource(nextModel, lineEnding);
  const range = nextModel.blockRange || nextModel.sourceRange;
  const from = charOffset(lines, range.from, 0);
  // A trailing \r from a CRLF document rides inside the split line. Keep it in
  // the replaced range and re-attach it to the rebuilt block so the following
  // newline stays CRLF instead of flipping to a bare \n.
  const lastLine = lines[range.to] ?? '';
  const trailingCR = lastLine.endsWith('\r') ? '\r' : '';
  const to = charOffset(lines, range.to, 0) + lastLine.length;
  const insert = newSource + trailingCR;
  const changes = [{ from, to, insert }];
  const newModel = parseTable(newSource, range.from);
  const newLines = [
    ...lines.slice(0, range.from),
    ...insert.split('\n'),
    ...lines.slice(range.to + 1),
  ];
  return { newText: newLines.join('\n'), newModel, changes };
}

function editCell(lines, model, { row, col, value }) {
  const m = cloneModel(model);
  if (m.rows[row]) m.rows[row].cells[col] = value;
  return rebuildAndDiff(lines, model, m);
}

function insertRow(lines, model, { afterRow, values }) {
  const m = cloneModel(model);
  const newRow = {
    cells: values || Array(m.columns).fill(''),
    isHeader: false,
    sourceLine: -1,
    sourceText: '',
    cellOffsets: [],
  };
  const insertAt = afterRow != null ? afterRow + 1 : m.rows.length;
  // Row numbers in formulas are 1-based data rows (model row index).
  const rowMap = buildShiftMap(m.rows.length, { insertAt });
  remapModelFormulas(m, { colMap: null, rowMap });
  m.rows.splice(insertAt, 0, newRow);
  return rebuildAndDiff(lines, model, m);
}

function deleteRow(lines, model, { row }) {
  const m = cloneModel(model);
  if (row <= 0 || row >= m.rows.length) return rebuildAndDiff(lines, model, m);
  const rowMap = buildShiftMap(m.rows.length, { deleteAt: row });
  remapModelFormulas(m, { colMap: null, rowMap });
  m.rows.splice(row, 1);
  return rebuildAndDiff(lines, model, m);
}

function insertColumn(lines, model, { afterCol, values }) {
  const m = cloneModel(model);
  const idx = afterCol != null ? afterCol + 1 : m.columns;
  const colMap = buildShiftMap(m.columns, { insertAt: idx });
  remapModelFormulas(m, { colMap, rowMap: null });
  m.columns++;
  m.alignments.splice(idx, 0, 'left');
  for (let r = 0; r < m.rows.length; r++) {
    const val = values?.[r] || '';
    m.rows[r].cells.splice(idx, 0, val);
  }
  m.metadata = m.metadata
    ? remapMetadataColumns(m.metadata, colMap, m.columns)
    : null;
  return rebuildAndDiff(lines, model, m);
}

function deleteColumn(lines, model, { col }) {
  const m = cloneModel(model);
  if (col < 0 || col >= m.columns) return rebuildAndDiff(lines, model, m);
  const colMap = buildShiftMap(m.columns, { deleteAt: col });
  remapModelFormulas(m, { colMap, rowMap: null });
  m.columns--;
  m.alignments.splice(col, 1);
  for (let r = 0; r < m.rows.length; r++) {
    m.rows[r].cells.splice(col, 1);
  }
  m.metadata = m.metadata
    ? remapMetadataColumns(m.metadata, colMap, m.columns)
    : null;
  return rebuildAndDiff(lines, model, m);
}

function moveRow(lines, model, { fromRow, toRow }) {
  const m = cloneModel(model);
  if (fromRow < 1 || fromRow >= m.rows.length || toRow < 1 || toRow >= m.rows.length) {
    return rebuildAndDiff(lines, model, m);
  }
  const rowMap = buildShiftMap(m.rows.length, { moveFrom: fromRow, moveTo: toRow });
  remapModelFormulas(m, { colMap: null, rowMap });
  const [row] = m.rows.splice(fromRow, 1);
  m.rows.splice(toRow, 0, row);
  return rebuildAndDiff(lines, model, m);
}

function moveColumn(lines, model, { fromCol, toCol }) {
  const m = cloneModel(model);
  if (fromCol < 0 || fromCol >= m.columns || toCol < 0 || toCol >= m.columns) {
    return rebuildAndDiff(lines, model, m);
  }
  const colMap = buildShiftMap(m.columns, { moveFrom: fromCol, moveTo: toCol });
  remapModelFormulas(m, { colMap, rowMap: null });
  const [align] = m.alignments.splice(fromCol, 1);
  m.alignments.splice(toCol, 0, align);
  for (let r = 0; r < m.rows.length; r++) {
    const [cell] = m.rows[r].cells.splice(fromCol, 1);
    m.rows[r].cells.splice(toCol, 0, cell);
  }
  m.metadata = m.metadata
    ? remapMetadataColumns(m.metadata, colMap, m.columns)
    : null;
  return rebuildAndDiff(lines, model, m);
}

function setAlignment(lines, model, { col, alignment }) {
  const m = cloneModel(model);
  if (col >= 0 && col < m.columns) m.alignments[col] = alignment;
  return rebuildAndDiff(lines, model, m);
}

function setColumnType(lines, model, { col, columnType, format }) {
  const m = cloneModel(model);
  if (col < 0 || col >= m.columns) return rebuildAndDiff(lines, model, m);
  const kind = normalizeColumnType(columnType);
  m.metadata = ensureColumnIds(m.metadata, m.columns);
  m.metadata = setColumnTypesInMetadata(m.metadata, [[col, kind, format != null ? normalizeTypeFormat(kind || 'text', format) : null]]);
  return rebuildAndDiff(lines, model, m);
}

function sortTable(lines, model, { col, direction = 'asc' }) {
  const m = cloneModel(model);
  if (col < 0 || col >= m.columns) return rebuildAndDiff(lines, model, m);
  const type = normalizeColumnType(m.metadata?.columns?.[col]?.type);
  const sorted = sortBodyRows(m.rows, col, type, direction);
  // Build a complete row permutation so single-cell references follow their
  // data rows. Range references stay positional over the sorted body.
  const rowMap = new Map();
  rowMap.set(0, 0); // header stays put
  for (let newIdx = 1; newIdx < sorted.length; newIdx++) {
    const row = sorted[newIdx];
    const oldIdx = m.rows.indexOf(row);
    rowMap.set(oldIdx, newIdx);
  }
  remapModelFormulas(m, { colMap: null, rowMap, remapRangeRows: false });
  m.rows = sorted;
  return rebuildAndDiff(lines, model, m);
}

function transposeTable(lines, model) {
  const m = cloneModel(model);
  // rows become columns, columns become rows
  const newRows = [];
  for (let c = 0; c < m.columns; c++) {
    const cells = [];
    for (let r = 0; r < m.rows.length; r++) {
      cells.push(m.rows[r].cells[c] || '');
    }
    newRows.push({
      cells,
      isHeader: c === 0,
      sourceLine: -1,
      sourceText: '',
      cellOffsets: [],
    });
  }
  m.rows = newRows;
  m.columns = m.rows[0]?.cells.length || 0;
  m.alignments = Array(m.columns).fill('left');
  // Transpose invalidates the old column identity map. Column types cannot
  // follow the data (old columns become rows) so they are dropped; the table
  // identity and unknown extras are kept.
  if (m.metadata) {
    m.metadata = ensureColumnIds({ ...m.metadata, columns: [] }, m.columns);
  }
  return rebuildAndDiff(lines, model, m);
}

function cloneModel(model) {
  return {
    valid: model.valid,
    rows: (model.rows || []).map((r) => ({ ...r, cells: [...r.cells] })),
    columns: model.columns || 0,
    alignments: [...(model.alignments || [])],
    sourceRange: { ...(model.sourceRange || { from: 0, to: 0 }) },
    blockRange: { ...(model.blockRange || model.sourceRange || { from: 0, to: 0 }) },
    metadata: cloneMetadata(model.metadata),
    malformed: model.malformed,
  };
}

// ─── Formula engine ──────────────────────────────────────────────────────────

// Cell references: A1, B2, etc. (column letter = 1-based, row number = 1-based from data row)
// Ranges: A1:A10
// Functions: SUM, AVERAGE, COUNT, MIN, MAX, IF
// Referenced formula cells evaluate recursively with cycle reporting.
// Errors are explicit (#REF!, #DIV/0!, #CYCLE!, #VALUE!) so a legitimate
// numeric zero is never confused with an invalid reference.

const FORMULA_MAX_CELLS = 1000;
const FORMULA_MAX_DEPTH = 32;

const FORMULA_ERRORS = Object.freeze({
  REF: '#REF!',
  DIV0: '#DIV/0!',
  CYCLE: '#CYCLE!',
  VALUE: '#VALUE!',
  NAME: '#NAME?',
  FORMULA: '#FORMULA!',
});

function formulaError(code, message) {
  const err = new Error(message || FORMULA_ERRORS[code] || code);
  err.code = code;
  err.formulaError = true;
  return err;
}

function isFormulaError(value) {
  return !!(value && typeof value === 'object' && value.formulaError);
}

function errorResult(err) {
  const code = err?.code && FORMULA_ERRORS[err.code] ? err.code : 'FORMULA';
  return {
    error: FORMULA_ERRORS[code],
    code,
    message: String(err?.message || FORMULA_ERRORS[code]),
    value: FORMULA_ERRORS[code],
  };
}

export function evaluateFormula(formula, model, options = {}) {
  // Returns { value } on success, or { error, code, message, value } on failure.
  if (!formula || !formula.startsWith('=')) return { value: formula };
  if (!model?.valid || model.rows.length < 2) return errorResult(formulaError('FORMULA', 'No data rows'));
  try {
    const result = evaluateExpression(formula, model, {
      visiting: options.visiting || new Set(),
      depth: options.depth || 0,
    });
    return { value: formatResult(result) };
  } catch (e) {
    return errorResult(e);
  }
}

// evaluateCell — value of one body cell, evaluating formulas recursively.
export function evaluateCell(model, row, col, options = {}) {
  const raw = model?.rows?.[row]?.cells?.[col] ?? '';
  if (typeof raw !== 'string' || !raw.startsWith('=')) return { value: raw, raw };
  return evaluateFormula(raw, model, options);
}

function evaluateExpression(formula, model, ctx) {
  const expr = String(formula).slice(1).trim();
  return parseExpr(expr, model, ctx);
}

function resolveCellRef(ref, model, ctx) {
  const match = /^([A-Z]+)(\d+)$/i.exec(ref);
  if (!match) throw formulaError('REF', `Invalid cell ref: ${ref}`);
  const col = colToIndex(match[1].toUpperCase());
  const row = parseInt(match[2], 10); // 1-based, row 1 = first data row (index 1 in model)
  if (col < 0 || col >= model.columns) throw formulaError('REF', `Missing reference: ${ref}`);
  if (row < 1 || row >= model.rows.length) throw formulaError('REF', `Missing reference: ${ref}`);
  return resolveCellAt(model, row, col, ctx);
}

function resolveCellAt(model, row, col, ctx) {
  const key = `${row},${col}`;
  if (ctx.visiting.has(key)) throw formulaError('CYCLE', `Circular reference at ${indexToCol(col)}${row}`);
  if (ctx.depth >= FORMULA_MAX_DEPTH) throw formulaError('CYCLE', `Formula depth exceeds ${FORMULA_MAX_DEPTH}`);
  const raw = model.rows[row]?.cells?.[col] ?? '';
  if (typeof raw === 'string' && raw.startsWith('=')) {
    ctx.visiting.add(key);
    try {
      return parseExpr(raw.slice(1).trim(), model, { ...ctx, depth: ctx.depth + 1 });
    } finally {
      ctx.visiting.delete(key);
    }
  }
  return coerceCellValue(raw);
}

function coerceCellValue(raw) {
  if (raw == null) return '';
  const text = String(raw);
  if (text.trim() === '') return '';
  // Prefer typed numeric meaning; bare numerics become numbers so arithmetic
  // and aggregates work. Explicit currency code/value pairs contribute their
  // numeric amount. Non-numeric text stays text.
  const trimmed = text.trim();
  if (/^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$/.test(trimmed)) {
    return Number(trimmed);
  }
  const money = parseCurrencyCell(trimmed);
  if (money) return money.value;
  return trimmed;
}

function resolveRange(rangeStr, model, ctx) {
  const parts = rangeStr.split(':');
  if (parts.length !== 2) throw formulaError('REF', `Invalid range: ${rangeStr}`);
  const startMatch = /^([A-Z]+)(\d+)$/i.exec(parts[0].trim());
  const endMatch = /^([A-Z]+)(\d+)$/i.exec(parts[1].trim());
  if (!startMatch || !endMatch) throw formulaError('REF', `Invalid range: ${rangeStr}`);

  const startCol = colToIndex(startMatch[1].toUpperCase());
  const startRow = parseInt(startMatch[2], 10);
  const endCol = colToIndex(endMatch[1].toUpperCase());
  const endRow = parseInt(endMatch[2], 10);

  const values = [];
  let count = 0;
  for (let r = Math.min(startRow, endRow); r <= Math.max(startRow, endRow); r++) {
    for (let c = Math.min(startCol, endCol); c <= Math.max(startCol, endCol); c++) {
      if (++count > FORMULA_MAX_CELLS) throw formulaError('VALUE', `Range exceeds ${FORMULA_MAX_CELLS} cells`);
      // Bounded range: cells outside the table are skipped, not fatal.
      if (r < 1 || r > model.rows.length - 1 || c < 0 || c >= model.columns) continue;
      values.push(resolveCellAt(model, r, c, { ...ctx, depth: ctx.depth + 1 }));
    }
  }
  return values;
}

function parseExpr(expr, model, ctx) {
  const tokens = tokenize(expr);
  let pos = 0;

  function peek() { return tokens[pos]; }
  function consume(expected) {
    const t = tokens[pos];
    if (expected && t !== expected) throw formulaError('VALUE', `Expected '${expected}', got '${t || 'EOF'}'`);
    pos++;
    return t;
  }

  function parseComparison() {
    let left = parseAddSub();
    while (peek() === '>' || peek() === '<' || peek() === '>=' || peek() === '<=' || peek() === '==' || peek() === '!=') {
      const op = consume();
      const right = parseAddSub();
      switch (op) {
        case '>': left = left > right ? 1 : 0; break;
        case '<': left = left < right ? 1 : 0; break;
        case '>=': left = left >= right ? 1 : 0; break;
        case '<=': left = left <= right ? 1 : 0; break;
        case '==': left = left === right ? 1 : 0; break;
        case '!=': left = left !== right ? 1 : 0; break;
      }
    }
    return left;
  }

  function parseAddSub() {
    let left = parseMulDiv();
    while (peek() === '+' || peek() === '-') {
      const op = consume();
      const right = parseMulDiv();
      if (typeof left === 'string' || typeof right === 'string') {
        left = op === '+' ? String(left) + String(right) : formulaErrorValue();
      } else {
        left = op === '+' ? left + right : left - right;
      }
    }
    return left;
  }

  function formulaErrorValue() {
    throw formulaError('VALUE', 'Arithmetic on text');
  }

  function parseMulDiv() {
    let left = parseUnary();
    while (peek() === '*' || peek() === '/' || peek() === '%') {
      const op = consume();
      const right = parseUnary();
      if (typeof left !== 'number' || typeof right !== 'number') {
        throw formulaError('VALUE', 'Arithmetic requires numbers');
      }
      if (op === '*') left = left * right;
      else if (op === '/') {
        if (right === 0) throw formulaError('DIV0', 'Division by zero');
        left = left / right;
      } else {
        if (right === 0) throw formulaError('DIV0', 'Division by zero');
        left = left % right;
      }
    }
    return left;
  }

  function parseUnary() {
    if (peek() === '-') { consume(); const v = parsePrimary(); return typeof v === 'number' ? -v : v; }
    if (peek() === '+') { consume(); return parsePrimary(); }
    return parsePrimary();
  }

  function parsePrimary() {
    const t = peek();
    if (!t) throw formulaError('VALUE', 'Unexpected end of expression');
    if (t === '#REF!') { consume(); throw formulaError('REF', 'Missing reference'); }

    // Parentheses
    if (t === '(') {
      consume('(');
      const val = parseComparison();
      consume(')');
      return val;
    }

    // Number
    if (/^-?\d+(\.\d+)?$/.test(t)) { consume(); return parseFloat(t); }

    // String literal
    if (t.startsWith('"') && t.endsWith('"')) { consume(); return t.slice(1, -1); }
    if (t.startsWith("'") && t.endsWith("'")) { consume(); return t.slice(1, -1); }

    // Bare word (e.g., Yes, No for IF args) — treat as string
    if (/^[A-Za-z_]+$/.test(t) && !/^(SUM|AVERAGE|COUNT|MIN|MAX|IF)$/i.test(t)) {
      consume();
      return t;
    }

    // Function call
    const funcMatch = /^(SUM|AVERAGE|COUNT|MIN|MAX|IF)$/i.exec(t);
    if (funcMatch) {
      consume();
      consume('(');
      return evalFunction(funcMatch[1].toUpperCase(), model, ctx);
    }

    // Range (A1:A10)
    if (/^[A-Z]+\d+:[A-Z]+\d+$/i.test(t)) {
      consume();
      return resolveRange(t, model, ctx);
    }

    // Cell reference
    if (/^[A-Z]+\d+$/i.test(t)) {
      consume();
      return resolveCellRef(t, model, ctx);
    }

    throw formulaError('NAME', `Unknown token: ${t}`);
  }

  function evalFunction(name, model, ctx) {
    const args = [];
    while (peek() !== ')' && peek() !== undefined) {
      args.push(parseComparison());
      if (peek() === ',') consume(',');
    }
    consume(')');

    // Flatten arrays in args
    const flat = args.flat(Infinity).filter((v) => v !== '' && v != null);
    const numbers = flat.filter((v) => typeof v === 'number');

    switch (name) {
      case 'SUM': {
        if (!numbers.length) return 0;
        return numbers.reduce((a, b) => a + b, 0);
      }
      case 'AVERAGE': return numbers.length ? numbers.reduce((a, b) => a + b, 0) / numbers.length : 0;
      case 'COUNT': return flat.length;
      case 'MIN': return numbers.length ? Math.min(...numbers) : 0;
      case 'MAX': return numbers.length ? Math.max(...numbers) : 0;
      case 'IF': {
        if (args.length < 2) throw formulaError('VALUE', 'IF needs at least 2 args');
        return args[0] ? args[1] : (args[2] ?? 0);
      }
      default: throw formulaError('NAME', `Unknown function: ${name}`);
    }
  }

  const result = parseComparison();
  if (pos < tokens.length) throw formulaError('VALUE', `Unexpected token: ${tokens[pos]}`);
  return result;
}

function tokenize(expr) {
  const tokens = [];
  let i = 0;
  while (i < expr.length) {
    // Skip whitespace
    if (/\s/.test(expr[i])) { i++; continue; }

    // Operators
    if (expr[i] === '>' && expr[i + 1] === '=') { tokens.push('>='); i += 2; continue; }
    if (expr[i] === '<' && expr[i + 1] === '=') { tokens.push('<='); i += 2; continue; }
    if (expr[i] === '!' && expr[i + 1] === '=') { tokens.push('!='); i += 2; continue; }
    if (expr[i] === '=' && expr[i + 1] === '=') { tokens.push('=='); i += 2; continue; }
    if (expr[i] === '#' && expr[i + 1] === 'R') {
      // Preserved #REF! token from a rewritten formula.
      const rest = expr.slice(i);
      if (rest.startsWith('#REF!')) { tokens.push('#REF!'); i += 5; continue; }
    }
    if ('+-*/%()><!,'.includes(expr[i])) {
      tokens.push(expr[i]); i++; continue;
    }

    // String literal
    if (expr[i] === '"' || expr[i] === "'") {
      const quote = expr[i];
      let j = i + 1;
      while (j < expr.length && expr[j] !== quote) j++;
      tokens.push(expr.slice(i, j + 1));
      i = j + 1;
      continue;
    }

    // Number or cell ref or function or range
    if (/[A-Za-z\d]/.test(expr[i])) {
      let j = i;
      while (j < expr.length && /[A-Za-z\d._:]/.test(expr[j])) j++;
      tokens.push(expr.slice(i, j));
      i = j;
      continue;
    }

    throw formulaError('VALUE', `Unexpected character: ${expr[i]}`);
  }
  return tokens;
}

function formatResult(val) {
  if (isFormulaError(val)) return String(val.message || FORMULA_ERRORS.FORMULA);
  if (typeof val === 'number') {
    return Number.isInteger(val) ? String(val) : val.toFixed(2);
  }
  return String(val);
}

function columnCurrencyCode(model, col) {
  for (let r = 1; r < model.rows.length; r++) {
    const money = parseCurrencyCell(model.rows[r]?.cells?.[col] || '');
    if (money) return money.code;
  }
  return '';
}

// ─── Interactive table widget ────────────────────────────────────────────────

const tableOwners = new WeakMap();

export function createTableWidget(model, onEdit, { h, editable = true, locale, onStatus } = {}) {
  // h: hyperscript helper (optional, falls back to DOM APIs)
  const el = (tag, attrs, ...children) => {
    const node = document.createElement(tag);
    if (attrs) for (const [k, v] of Object.entries(attrs)) {
      if (k === 'class') node.className = v;
      else if (k === 'text') node.textContent = v;
      else if (k.startsWith('on')) node.addEventListener(k.slice(2).toLowerCase(), v);
      else node.setAttribute(k, v);
    }
    for (const child of children) {
      if (typeof child === 'string') node.append(document.createTextNode(child));
      else if (child) node.append(child);
    }
    return node;
  };

  let activeRow = -1;
  let activeCol = -1;
  let editing = false;
  let editInput = null;
  const widgetModel = cloneModel(model);
  const container = el('div', {
    class: 'copal-table-widget',
    tabindex: '0',
    'data-table-editable': editable ? 'true' : 'false',
  });
  const formulaBar = el('div', { class: 'copal-table-formula-bar', text: '' });
  const statusLine = el('div', { class: 'copal-table-status', text: '' });

  function notify(message, isError = false) {
    statusLine.textContent = message || '';
    statusLine.classList.toggle('error', !!isError);
    if (typeof onStatus === 'function') onStatus(message, isError);
  }

  function columnType(c) {
    return normalizeColumnType(widgetModel.metadata?.columns?.[c]?.type);
  }

  function columnFormat(c) {
    const meta = widgetModel.metadata?.columns?.[c];
    return normalizeTypeFormat(columnType(c) || 'text', meta?.format || 'auto');
  }

  function displayFor(raw, c) {
    const type = columnType(c);
    if (!type) {
      if (typeof raw === 'string' && raw.startsWith('=')) {
        const result = evaluateFormula(raw, widgetModel);
        return result.error ? result.error : (result.value ?? raw);
      }
      return raw;
    }
    if (typeof raw === 'string' && raw.startsWith('=')) {
      const result = evaluateFormula(raw, widgetModel);
      // Computed results still respect the column display format when numeric.
      if (result.error) return result.error;
      const value = result.value ?? raw;
      if (type === 'currency') {
        const code = columnCurrencyCode(widgetModel, c);
        if (code) return formatTypedCell(`${code} ${value}`, type, columnFormat(c), locale);
        return value;
      }
      return formatTypedCell(value, type, columnFormat(c), locale);
    }
    return formatTypedCell(raw, type, columnFormat(c), locale);
  }

  function getCellEl(r, c) {
    return container.querySelector(`[data-row="${r}"][data-col="${c}"]`);
  }

  function setActiveCell(r, c) {
    const prev = container.querySelector('.copal-table-cell-active');
    if (prev) prev.classList.remove('copal-table-cell-active');
    activeRow = r;
    activeCol = c;
    const cell = getCellEl(r, c);
    if (cell) {
      cell.classList.add('copal-table-cell-active');
      cell.focus();
      // Inspect the formula source when the cell holds one.
      const raw = widgetModel.rows[r]?.cells[c] || '';
      if (raw.startsWith('=')) {
        formulaBar.textContent = raw;
        formulaBar.dataset.kind = 'formula';
      } else {
        formulaBar.textContent = raw;
        formulaBar.dataset.kind = 'literal';
      }
    }
  }

  function refuseMutation() {
    if (!editable) {
      notify('This document is read-only. Table structure and cells cannot be changed.', true);
      return true;
    }
    return false;
  }

  function startEdit(r, c) {
    if (refuseMutation()) return;
    editing = true;
    const cell = getCellEl(r, c);
    if (!cell) return;
    const raw = widgetModel.rows[r].cells[c] || '';
    editInput = el('input', { class: 'copal-table-edit-input', type: 'text', value: raw });
    editInput.addEventListener('keydown', onEditKeydown);
    editInput.addEventListener('blur', () => commitEdit());
    cell.textContent = '';
    cell.append(editInput);
    editInput.focus();
    editInput.select();
  }

  function commitEdit() {
    if (!editing || !editInput) return;
    const value = editInput.value;
    editing = false;
    const input = editInput;
    editInput = null;
    void input;
    onEdit({ type: 'cell', row: activeRow, col: activeCol, value });
  }

  function cancelEdit() {
    if (!editing) return;
    editing = false;
    editInput = null;
    renderBody();
    setActiveCell(activeRow, activeCol);
  }

  function onEditKeydown(e) {
    if (e.key === 'Enter') { e.preventDefault(); commitEdit(); }
    if (e.key === 'Escape') { e.preventDefault(); cancelEdit(); }
    if (e.key === 'Tab') {
      e.preventDefault();
      commitEdit();
      navigate(e.shiftKey ? -1 : 1, 0, true);
    }
  }

  function navigate(dc, dr, wrap) {
    let r = activeRow + dr;
    let c = activeCol + dc;
    if (wrap) {
      if (c >= widgetModel.columns) { c = 0; r++; }
      if (c < 0) { c = widgetModel.columns - 1; r--; }
    }
    if (r < 0 || r >= widgetModel.rows.length || c < 0 || c >= widgetModel.columns) return;
    setActiveCell(r, c);
  }

  function onKeydown(e) {
    if (editing) return; // edit input handles its own keys
    if (e.key === 'Tab') { e.preventDefault(); navigate(e.shiftKey ? -1 : 1, 0, true); }
    else if (e.key === 'ArrowRight') { e.preventDefault(); navigate(1, 0); }
    else if (e.key === 'ArrowLeft') { e.preventDefault(); navigate(-1, 0); }
    else if (e.key === 'ArrowDown') { e.preventDefault(); navigate(0, 1); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); navigate(0, -1); }
    else if (e.key === 'Enter') { e.preventDefault(); startEdit(activeRow, activeCol); }
    else if (e.key === 'Escape') { container.blur(); }
    else if (e.key === 'Delete' || e.key === 'Backspace') {
      if (refuseMutation()) return;
      if (activeRow > 0) { // don't delete header
        e.preventDefault();
        onEdit({ type: 'deleteRow', row: activeRow });
      }
    }
    // Alt+Arrow for move
    else if (e.altKey && e.key === 'ArrowUp' && activeRow > 1) {
      if (refuseMutation()) return;
      e.preventDefault(); onEdit({ type: 'moveRow', fromRow: activeRow, toRow: activeRow - 1 });
    }
    else if (e.altKey && e.key === 'ArrowDown' && activeRow < widgetModel.rows.length - 1) {
      if (refuseMutation()) return;
      e.preventDefault(); onEdit({ type: 'moveRow', fromRow: activeRow, toRow: activeRow + 1 });
    }
    else if (e.altKey && e.key === 'ArrowLeft' && activeCol > 0) {
      if (refuseMutation()) return;
      e.preventDefault(); onEdit({ type: 'moveColumn', fromCol: activeCol, toCol: activeCol - 1 });
    }
    else if (e.altKey && e.key === 'ArrowRight' && activeCol < widgetModel.columns - 1) {
      if (refuseMutation()) return;
      e.preventDefault(); onEdit({ type: 'moveColumn', fromCol: activeCol, toCol: activeCol + 1 });
    }
  }

  function renderBody() {
    const table = el('table', { class: 'copal-table-grid' });
    const thead = el('thead');
    const hr = el('tr');
    for (let c = 0; c < widgetModel.columns; c++) {
      const th = el('th', {
        'data-row': '0', 'data-col': String(c),
        'data-copal-context-object': 'table',
        'data-column-type': columnType(c) || '',
        tabindex: '-1',
        onclick: () => setActiveCell(0, c),
        ondblclick: () => startEdit(0, c),
      });
      const align = widgetModel.alignments[c] || 'left';
      th.style.textAlign = align;
      th.append(el('span', { class: 'copal-table-cell-content', text: widgetModel.rows[0].cells[c] || '' }));
      const type = columnType(c);
      if (type) {
        th.append(el('span', { class: 'copal-table-type-indicator', text: type, title: `Column type: ${type}` }));
      }
      const indicator = el('span', { class: 'copal-table-align-indicator', text: align === 'center' ? '↔' : align === 'right' ? '→' : '←' });
      indicator.addEventListener('click', (e) => {
        e.stopPropagation();
        if (refuseMutation()) return;
        const next = align === 'left' ? 'center' : align === 'center' ? 'right' : 'left';
        onEdit({ type: 'setAlignment', col: c, alignment: next });
      });
      th.append(indicator);
      hr.append(th);
    }
    thead.append(hr);
    table.append(thead);

    const tbody = el('tbody');
    for (let r = 1; r < widgetModel.rows.length; r++) {
      const tr = el('tr');
      for (let c = 0; c < widgetModel.columns; c++) {
        const raw = widgetModel.rows[r].cells[c] || '';
        const parsed = parseTypedCell(raw, columnType(c));
        const td = el('td', {
          'data-row': String(r), 'data-col': String(c),
          'data-copal-context-object': 'table',
          'data-cell-status': parsed.status,
          tabindex: '-1',
          onclick: () => setActiveCell(r, c),
          ondblclick: () => startEdit(r, c),
        });
        const display = displayFor(raw, c);
        const isError = typeof display === 'string' && display.startsWith('#');
        if (isError) td.classList.add('copal-table-cell-error');
        if (parsed.status === 'blank') td.classList.add('copal-table-cell-blank');
        if (parsed.status === 'invalid') td.classList.add('copal-table-cell-invalid');
        td.style.textAlign = widgetModel.alignments[c] || 'left';
        td.append(el('span', { class: 'copal-table-cell-content', text: display }));
        if (raw.startsWith('=')) {
          td.title = raw;
          td.classList.add('copal-table-cell-formula');
        }
        tr.append(td);
      }
      tbody.append(tr);
    }
    table.append(tbody);

    const wrap = el('div', { class: 'copal-table-grid-wrap' });
    wrap.append(table);
    container.replaceChildren(formulaBar, wrap, statusLine);
    if (activeRow >= 0 && activeCol >= 0) setActiveCell(activeRow, activeCol);
  }

  container.addEventListener('keydown', onKeydown);
  container.addEventListener('focus', () => {
    if (activeRow < 0) setActiveCell(0, 0);
  });

  function columnCommands() {
    const c = activeCol;
    const type = columnType(c);
    return [
      { label: 'Insert column left', id: 'table-insert-column-left', disabled: !editable },
      { label: 'Insert column right', id: 'table-insert-column-right', disabled: !editable },
      { label: 'Delete column', id: 'table-delete-column', disabled: !editable || widgetModel.columns <= 1 },
      { label: 'Move column left', id: 'table-move-column-left', disabled: !editable || c <= 0 },
      { label: 'Move column right', id: 'table-move-column-right', disabled: !editable || c >= widgetModel.columns - 1 },
      { label: 'Sort ascending', id: 'table-sort-asc', disabled: !editable },
      { label: 'Sort descending', id: 'table-sort-desc', disabled: !editable },
      { label: `Type: text${type === 'text' ? ' ✓' : ''}`, id: 'table-type-text', disabled: !editable },
      { label: `Type: number${type === 'number' ? ' ✓' : ''}`, id: 'table-type-number', disabled: !editable },
      { label: `Type: date${type === 'date' ? ' ✓' : ''}`, id: 'table-type-date', disabled: !editable },
      { label: `Type: currency${type === 'currency' ? ' ✓' : ''}`, id: 'table-type-currency', disabled: !editable },
      { label: 'Align left', id: 'table-align-left', disabled: !editable },
      { label: 'Align center', id: 'table-align-center', disabled: !editable },
      { label: 'Align right', id: 'table-align-right', disabled: !editable },
    ];
  }

  function rowCommands() {
    return [
      { label: 'Insert row above', id: 'table-insert-row-above', disabled: !editable },
      { label: 'Insert row below', id: 'table-insert-row-below', disabled: !editable },
      { label: 'Delete row', id: 'table-delete-row', disabled: !editable || widgetModel.rows.length <= 2 },
      { label: 'Move row up', id: 'table-move-row-up', disabled: !editable || activeRow <= 1 },
      { label: 'Move row down', id: 'table-move-row-down', disabled: !editable || activeRow >= widgetModel.rows.length - 1 },
    ];
  }

  const tableAdapter = {
    capture: (target) => ({
      row: Number(target?.dataset?.row),
      col: Number(target?.dataset?.col),
      editable,
    }),
    commands: (request) => {
      const row = Number(request.adapterContext?.row);
      const col = Number(request.adapterContext?.col);
      if (!Number.isInteger(row) || !Number.isInteger(col)) return [];
      activeRow = row; activeCol = col;
      if (row === 0) return columnCommands();
      return [...rowCommands(), ...columnCommands().filter((entry) => entry.id.startsWith('table-sort-') || entry.id.startsWith('table-type-'))];
    },
    execute: (command, request) => {
      const row = Number(request.adapterContext?.row);
      const col = Number(request.adapterContext?.col);
      if (!Number.isInteger(row) || !Number.isInteger(col)) return false;
      if (!editable && String(command).startsWith('table-') && command !== 'table-inspect') {
        notify('This document is read-only. Table structure and cells cannot be changed.', true);
        return true;
      }
      activeRow = row; activeCol = col;
      if (command === 'table-insert-column-left') onEdit({ type: 'insertColumn', afterCol: col - 1 });
      else if (command === 'table-insert-column-right') onEdit({ type: 'insertColumn', afterCol: col });
      else if (command === 'table-delete-column' && widgetModel.columns > 1) onEdit({ type: 'deleteColumn', col });
      else if (command === 'table-move-column-left' && col > 0) onEdit({ type: 'moveColumn', fromCol: col, toCol: col - 1 });
      else if (command === 'table-move-column-right' && col < widgetModel.columns - 1) onEdit({ type: 'moveColumn', fromCol: col, toCol: col + 1 });
      else if (command === 'table-insert-row-above' && row > 0) onEdit({ type: 'insertRow', afterRow: row - 1 });
      else if (command === 'table-insert-row-below') onEdit({ type: 'insertRow', afterRow: row });
      else if (command === 'table-delete-row' && widgetModel.rows.length > 2) onEdit({ type: 'deleteRow', row });
      else if (command === 'table-move-row-up' && row > 1) onEdit({ type: 'moveRow', fromRow: row, toRow: row - 1 });
      else if (command === 'table-move-row-down' && row < widgetModel.rows.length - 1) onEdit({ type: 'moveRow', fromRow: row, toRow: row + 1 });
      else if (command === 'table-sort-asc') onEdit({ type: 'sort', col, direction: 'asc' });
      else if (command === 'table-sort-desc') onEdit({ type: 'sort', col, direction: 'desc' });
      else if (command === 'table-type-text') onEdit({ type: 'setColumnType', col, columnType: 'text' });
      else if (command === 'table-type-number') onEdit({ type: 'setColumnType', col, columnType: 'number' });
      else if (command === 'table-type-date') onEdit({ type: 'setColumnType', col, columnType: 'date' });
      else if (command === 'table-type-currency') onEdit({ type: 'setColumnType', col, columnType: 'currency' });
      else if (command === 'table-align-left') onEdit({ type: 'setAlignment', col, alignment: 'left' });
      else if (command === 'table-align-center') onEdit({ type: 'setAlignment', col, alignment: 'center' });
      else if (command === 'table-align-right') onEdit({ type: 'setAlignment', col, alignment: 'right' });
      else return false;
      return true;
    },
  };
  tableOwners.set(container, tableAdapter);
  registerAdapter(container, tableAdapter);
  window.__openClankTableContextCommand = (command, target) => {
    const owner = target?.closest?.('.copal-table-widget');
    const adapter = owner ? tableOwners.get(owner) : null;
    if (!adapter) return false;
    const request = { adapterContext: adapter.capture(target) };
    return adapter.execute(command, request);
  };

  renderBody();
  return container;
}
