// TableModel — Markdown table parser, serializer, mutation engine, and formula evaluator.
// Parses Markdown tables into a structured model with source-position tracking.
// All mutations produce valid Markdown and CodeMirror-compatible change arrays.

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

export function parseTable(text, startLine = 0) {
  const source = String(text || '');
  const allLines = source.split('\n');
  const lines = [];
  for (let i = 0; i < allLines.length; i++) {
    lines.push({ text: allLines[i], line: startLine + i });
  }

  // Find first table-like line (contains |)
  let start = -1;
  for (let i = 0; i < lines.length; i++) {
    if (lines[i].text.includes('|') && lines[i].text.trim().length > 0) { start = i; break; }
  }
  if (start < 0) return { valid: false, malformed: 'no-table' };

  // Check separator line
  const sepIndex = start + 1;
  if (sepIndex >= lines.length || !isSeparatorLine(lines[sepIndex].text)) {
    return { valid: false, malformed: 'missing-separator', sourceRange: { from: lines[start].line, to: lines[start].line } };
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
  let lastRowLine = sepIndex;

  // Header row
  const headerOffsets = computeCellOffsets(lines[start].text, headerCells);
  rows.push({
    cells: headerCells,
    isHeader: true,
    sourceLine: lines[start].line,
    sourceText: lines[start].text,
    cellOffsets: headerOffsets,
  });

  // Separator row (tracked for source but not as data)
  const sepLine = lines[sepIndex].line;

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

  if (malformReasons.length > 0) {
    return {
      valid: false,
      malformed: malformReasons.join(','),
      rows,
      columns: colCount,
      alignments: alignments.slice(0, colCount),
      sourceRange,
    };
  }

  return {
    valid: true,
    rows,
    columns: colCount,
    alignments: alignments.slice(0, colCount),
    sourceRange,
  };
}

function computeCellOffsets(lineText, cells) {
  // Compute char ranges for each cell within the raw line text.
  const offsets = [];
  let pos = 0;
  const trimmed = lineText.trim();
  const src = trimmed.replace(/^\|/, '');
  let cellIdx = 0;
  let cellStart = -1;
  let inCode = false;
  let current = '';

  for (let i = 0; i < src.length && cellIdx < cells.length; i++) {
    const ch = src[i];
    if (ch === '`') { inCode = !inCode; current += ch; continue; }
    if (ch === '\\' && i + 1 < src.length && src[i + 1] === '|' && !inCode) { current += '|'; i++; continue; }
    if (ch === '|' && !inCode) {
      if (cellStart >= 0) {
        offsets.push({ from: cellStart, to: i });
      }
      cellStart = i + 1;
      current = '';
      cellIdx++;
      continue;
    }
    current += ch;
  }
  // Last cell
  if (cellIdx < cells.length && cellStart >= 0) {
    offsets.push({ from: cellStart, to: src.length });
  }
  return offsets;
}

// ─── Serializer ──────────────────────────────────────────────────────────────

export function tableToSource(model) {
  if (!model?.valid || !model.rows?.length) return '';
  const colWidths = [];
  for (let c = 0; c < model.columns; c++) {
    let max = 3; // minimum width for separator
    for (const row of model.rows) {
      const cell = row.cells[c] || '';
      if (cell.length > max) max = cell.length;
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
  for (let r = 0; r < model.rows.length; r++) {
    const row = model.rows[r];
    const cells = [];
    for (let c = 0; c < model.columns; c++) {
      cells.push(pad(row.cells[c] || '', colWidths[c]));
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
  return lines.join('\n');
}

// ─── Source synchronization ──────────────────────────────────────────────────

export function applyTableEdit(doc, model, edit) {
  // doc: full document string
  // model: parsed table model
  // edit: { type, ... } — see below
  // Returns: { newText, newModel, changes }
  //   changes: array of { from, to, insert } for CodeMirror dispatch

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

function rebuildAndDiff(lines, model, newTextFn) {
  // Generic helper: rebuild table source, compute changes.
  const newSource = newTextFn();
  const tableStart = model.sourceRange.from;
  const tableEnd = model.sourceRange.to;
  const from = charOffset(lines, tableStart, 0);
  const to = charOffset(lines, tableEnd, 0) + lines[tableEnd].length;
  const changes = [{ from, to, insert: newSource }];
  const newModel = parseTable(newSource, tableStart);
  const newLines = [...lines.slice(0, tableStart), ...newSource.split('\n'), ...lines.slice(tableEnd + 1)];
  return { newText: newLines.join('\n'), newModel, changes };
}

function editCell(lines, model, { row, col, value }) {
  return rebuildAndDiff(lines, model, () => {
    const m = cloneModel(model);
    if (m.rows[row]) m.rows[row].cells[col] = value;
    return tableToSource(m);
  });
}

function insertRow(lines, model, { afterRow, values }) {
  return rebuildAndDiff(lines, model, () => {
    const m = cloneModel(model);
    const newRow = {
      cells: values || Array(m.columns).fill(''),
      isHeader: false,
      sourceLine: -1,
      sourceText: '',
      cellOffsets: [],
    };
    const idx = afterRow != null ? afterRow + 1 : m.rows.length;
    m.rows.splice(idx, 0, newRow);
    return tableToSource(m);
  });
}

function deleteRow(lines, model, { row }) {
  return rebuildAndDiff(lines, model, () => {
    const m = cloneModel(model);
    if (row > 0 && row < m.rows.length) m.rows.splice(row, 1);
    return tableToSource(m);
  });
}

function insertColumn(lines, model, { afterCol, values }) {
  return rebuildAndDiff(lines, model, () => {
    const m = cloneModel(model);
    const idx = afterCol != null ? afterCol + 1 : m.columns;
    m.columns++;
    m.alignments.splice(idx, 0, 'left');
    for (let r = 0; r < m.rows.length; r++) {
      const val = values?.[r] || '';
      m.rows[r].cells.splice(idx, 0, val);
    }
    return tableToSource(m);
  });
}

function deleteColumn(lines, model, { col }) {
  return rebuildAndDiff(lines, model, () => {
    const m = cloneModel(model);
    if (col < 0 || col >= m.columns) return tableToSource(m);
    m.columns--;
    m.alignments.splice(col, 1);
    for (let r = 0; r < m.rows.length; r++) {
      m.rows[r].cells.splice(col, 1);
    }
    return tableToSource(m);
  });
}

function moveRow(lines, model, { fromRow, toRow }) {
  return rebuildAndDiff(lines, model, () => {
    const m = cloneModel(model);
    if (fromRow < 1 || fromRow >= m.rows.length || toRow < 1 || toRow >= m.rows.length) return tableToSource(m);
    const [row] = m.rows.splice(fromRow, 1);
    m.rows.splice(toRow, 0, row);
    return tableToSource(m);
  });
}

function moveColumn(lines, model, { fromCol, toCol }) {
  return rebuildAndDiff(lines, model, () => {
    const m = cloneModel(model);
    if (fromCol < 0 || fromCol >= m.columns || toCol < 0 || toCol >= m.columns) return tableToSource(m);
    const [align] = m.alignments.splice(fromCol, 1);
    m.alignments.splice(toCol, 0, align);
    for (let r = 0; r < m.rows.length; r++) {
      const [cell] = m.rows[r].cells.splice(fromCol, 1);
      m.rows[r].cells.splice(toCol, 0, cell);
    }
    return tableToSource(m);
  });
}

function setAlignment(lines, model, { col, alignment }) {
  return rebuildAndDiff(lines, model, () => {
    const m = cloneModel(model);
    if (col >= 0 && col < m.columns) m.alignments[col] = alignment;
    return tableToSource(m);
  });
}

function sortTable(lines, model, { col, direction = 'asc' }) {
  return rebuildAndDiff(lines, model, () => {
    const m = cloneModel(model);
    if (col < 0 || col >= m.columns) return tableToSource(m);
    const header = m.rows[0];
    const body = m.rows.slice(1);
    body.sort((a, b) => {
      const va = a.cells[col] || '';
      const vb = b.cells[col] || '';
      const na = Number(va);
      const nb = Number(vb);
      if (!isNaN(na) && !isNaN(nb)) return direction === 'asc' ? na - nb : nb - na;
      return direction === 'asc' ? va.localeCompare(vb) : vb.localeCompare(va);
    });
    m.rows = [header, ...body];
    return tableToSource(m);
  });
}

function transposeTable(lines, model) {
  return rebuildAndDiff(lines, model, () => {
    const m = cloneModel(model);
    // rows become columns, columns become rows
    // First row (header) becomes first column
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
    return tableToSource(m);
  });
}

function cloneModel(model) {
  return {
    valid: model.valid,
    rows: (model.rows || []).map((r) => ({ ...r, cells: [...r.cells] })),
    columns: model.columns || 0,
    alignments: [...(model.alignments || [])],
    sourceRange: { ...(model.sourceRange || { from: 0, to: 0 }) },
  };
}

// ─── Formula engine ──────────────────────────────────────────────────────────

// Cell references: A1, B2, etc. (column letter = 1-based, row number = 1-based from data row)
// Ranges: A1:A10
// Functions: SUM, AVERAGE, COUNT, MIN, MAX, IF

const FORMULA_MAX_CELLS = 1000;

export function evaluateFormula(formula, model) {
  // Returns { value, error? }
  if (!formula || !formula.startsWith('=')) return { value: formula };
  if (!model?.valid || model.rows.length < 2) return { error: 'No data rows' };

  const expr = formula.slice(1).trim();
  try {
    const result = parseExpr(expr, model);
    return { value: formatResult(result) };
  } catch (e) {
    return { error: e.message };
  }
}

function colToIndex(letter) {
  // A=0, B=1, ..., Z=25, AA=26
  let idx = 0;
  for (let i = 0; i < letter.length; i++) {
    idx = idx * 26 + (letter.charCodeAt(i) - 64);
  }
  return idx - 1;
}

function resolveCellRef(ref, model) {
  const match = /^([A-Z]+)(\d+)$/i.exec(ref);
  if (!match) throw new Error(`Invalid cell ref: ${ref}`);
  const col = colToIndex(match[1].toUpperCase());
  const row = parseInt(match[2], 10); // 1-based, row 1 = first data row (index 1 in model)
  if (col < 0 || col >= model.columns) throw new Error(`Column out of range: ${ref}`);
  if (row < 1 || row >= model.rows.length) throw new Error(`Row out of range: ${ref}`);
  const raw = model.rows[row].cells[col] || '';
  const num = Number(raw);
  return isNaN(num) ? raw : num;
}

function resolveRange(rangeStr, model) {
  const parts = rangeStr.split(':');
  if (parts.length !== 2) throw new Error(`Invalid range: ${rangeStr}`);
  const startMatch = /^([A-Z]+)(\d+)$/i.exec(parts[0].trim());
  const endMatch = /^([A-Z]+)(\d+)$/i.exec(parts[1].trim());
  if (!startMatch || !endMatch) throw new Error(`Invalid range: ${rangeStr}`);

  const startCol = colToIndex(startMatch[1].toUpperCase());
  const startRow = parseInt(startMatch[2], 10);
  const endCol = colToIndex(endMatch[1].toUpperCase());
  const endRow = parseInt(endMatch[2], 10);

  const values = [];
  let count = 0;
  for (let r = Math.min(startRow, endRow); r <= Math.max(startRow, endRow); r++) {
    for (let c = Math.min(startCol, endCol); c <= Math.max(startCol, endCol); c++) {
      if (++count > FORMULA_MAX_CELLS) throw new Error(`Range exceeds ${FORMULA_MAX_CELLS} cells`);
      if (r < 1 || r > model.rows.length - 1 || c < 0 || c >= model.columns) continue;
      const raw = model.rows[r].cells[c] || '';
      const num = Number(raw);
      values.push(isNaN(num) ? raw : num);
    }
  }
  return values;
}

function parseExpr(expr, model) {
  const tokens = tokenize(expr);
  let pos = 0;

  function peek() { return tokens[pos]; }
  function consume(expected) {
    const t = tokens[pos];
    if (expected && t !== expected) throw new Error(`Expected '${expected}', got '${t || 'EOF'}'`);
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
      left = op === '+' ? left + right : left - right;
    }
    return left;
  }

  function parseMulDiv() {
    let left = parseUnary();
    while (peek() === '*' || peek() === '/' || peek() === '%') {
      const op = consume();
      const right = parseUnary();
      if (op === '*') left = left * right;
      else if (op === '/') { if (right === 0) throw new Error('Division by zero'); left = left / right; }
      else left = left % right;
    }
    return left;
  }

  function parseUnary() {
    if (peek() === '-') { consume(); return -parsePrimary(); }
    if (peek() === '+') { consume(); return parsePrimary(); }
    return parsePrimary();
  }

  function parsePrimary() {
    const t = peek();
    if (!t) throw new Error('Unexpected end of expression');

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
      return evalFunction(funcMatch[1].toUpperCase(), model);
    }

    // Range (A1:A10)
    if (/^[A-Z]+\d+:[A-Z]+\d+$/i.test(t)) {
      consume();
      return resolveRange(t, model);
    }

    // Cell reference
    if (/^[A-Z]+\d+$/i.test(t)) {
      consume();
      return resolveCellRef(t, model);
    }

    throw new Error(`Unknown token: ${t}`);
  }

  function evalFunction(name, model) {
    const args = [];
    while (peek() !== ')' && peek() !== undefined) {
      args.push(parseComparison());
      if (peek() === ',') consume(',');
    }
    consume(')');

    // Flatten arrays in args
    const flat = args.flat(Infinity).filter((v) => typeof v === 'number' || (typeof v === 'string' && v !== ''));
    const numbers = flat.filter((v) => typeof v === 'number');

    switch (name) {
      case 'SUM': return numbers.reduce((a, b) => a + b, 0);
      case 'AVERAGE': return numbers.length ? numbers.reduce((a, b) => a + b, 0) / numbers.length : 0;
      case 'COUNT': return flat.length;
      case 'MIN': return numbers.length ? Math.min(...numbers) : 0;
      case 'MAX': return numbers.length ? Math.max(...numbers) : 0;
      case 'IF': {
        if (args.length < 2) throw new Error('IF needs at least 2 args');
        return args[0] ? args[1] : (args[2] ?? 0);
      }
      default: throw new Error(`Unknown function: ${name}`);
    }
  }

  const result = parseComparison();
  if (pos < tokens.length) throw new Error(`Unexpected token: ${tokens[pos]}`);
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
    if ('+-*/%()><!,'.includes(expr[i])) {
      // Single-char operators (multi-char >=, <=, !=, == already handled above)
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
      // Could be: number, cell ref (A1), range (A1:B2), function (SUM)
      while (j < expr.length && /[A-Za-z\d._:]/.test(expr[j])) j++;
      tokens.push(expr.slice(i, j));
      i = j;
      continue;
    }

    throw new Error(`Unexpected character: ${expr[i]}`);
  }
  return tokens;
}

function formatResult(val) {
  if (typeof val === 'number') {
    return Number.isInteger(val) ? String(val) : val.toFixed(2);
  }
  return String(val);
}

// ─── Interactive table widget ────────────────────────────────────────────────

export function createTableWidget(model, onEdit, { h } = {}) {
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
  let contextMenu = null;
  const widgetModel = cloneModel(model);
  const container = el('div', { class: 'copal-table-widget', tabindex: '0' });
  const formulaBar = el('div', { class: 'copal-table-formula-bar', text: '' });

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
      // Update formula bar
      const raw = widgetModel.rows[r]?.cells[c] || '';
      formulaBar.textContent = raw.startsWith('=') ? raw : '';
    }
  }

  function startEdit(r, c) {
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
      if (activeRow > 0) { // don't delete header
        e.preventDefault();
        onEdit({ type: 'deleteRow', row: activeRow });
      }
    }
    // Alt+Arrow for move
    else if (e.altKey && e.key === 'ArrowUp' && activeRow > 1) {
      e.preventDefault(); onEdit({ type: 'moveRow', fromRow: activeRow, toRow: activeRow - 1 });
    }
    else if (e.altKey && e.key === 'ArrowDown' && activeRow < widgetModel.rows.length - 1) {
      e.preventDefault(); onEdit({ type: 'moveRow', fromRow: activeRow, toRow: activeRow + 1 });
    }
    else if (e.altKey && e.key === 'ArrowLeft' && activeCol > 0) {
      e.preventDefault(); onEdit({ type: 'moveColumn', fromCol: activeCol, toCol: activeCol - 1 });
    }
    else if (e.altKey && e.key === 'ArrowRight' && activeCol < widgetModel.columns - 1) {
      e.preventDefault(); onEdit({ type: 'moveColumn', fromCol: activeCol, toCol: activeCol + 1 });
    }
  }

  function showContextMenu(e, r, c) {
    e.preventDefault();
    hideContextMenu();
    contextMenu = el('div', { class: 'copal-table-context-menu' });
    const items = [];
    if (r === 0) {
      // Column operations
      items.push({ label: 'Insert column left', action: () => onEdit({ type: 'insertColumn', afterCol: c - 1 }) });
      items.push({ label: 'Insert column right', action: () => onEdit({ type: 'insertColumn', afterCol: c }) });
      items.push({ label: 'Delete column', action: () => onEdit({ type: 'deleteColumn', col: c }) });
      items.push({ label: 'Sort ascending', action: () => onEdit({ type: 'sort', col: c, direction: 'asc' }) });
      items.push({ label: 'Sort descending', action: () => onEdit({ type: 'sort', col: c, direction: 'desc' }) });
      const align = widgetModel.alignments[c] || 'left';
      const next = align === 'left' ? 'center' : align === 'center' ? 'right' : 'left';
      items.push({ label: `Align ${next}`, action: () => onEdit({ type: 'setAlignment', col: c, alignment: next }) });
    } else {
      items.push({ label: 'Insert row above', action: () => onEdit({ type: 'insertRow', afterRow: r - 1 }) });
      items.push({ label: 'Insert row below', action: () => onEdit({ type: 'insertRow', afterRow: r }) });
      items.push({ label: 'Delete row', action: () => onEdit({ type: 'deleteRow', row: r }) });
    }
    items.push({ label: 'Transpose', action: () => onEdit({ type: 'transpose' }) });

    for (const item of items) {
      const btn = el('button', { class: 'copal-table-context-item', text: item.label, onclick: () => { hideContextMenu(); item.action(); } });
      contextMenu.append(btn);
    }
    contextMenu.style.left = `${e.clientX}px`;
    contextMenu.style.top = `${e.clientY}px`;
    document.body.append(contextMenu);
    const dismiss = (ev) => { if (!contextMenu?.contains(ev.target)) { hideContextMenu(); document.removeEventListener('mousedown', dismiss); } };
    setTimeout(() => document.addEventListener('mousedown', dismiss), 0);
  }

  function hideContextMenu() {
    if (contextMenu) { contextMenu.remove(); contextMenu = null; }
  }

  function renderBody() {
    const table = el('table', { class: 'copal-table-grid' });
    const thead = el('thead');
    const hr = el('tr');
    for (let c = 0; c < widgetModel.columns; c++) {
      const th = el('th', {
        'data-row': '0', 'data-col': String(c),
        tabindex: '-1',
        onclick: () => setActiveCell(0, c),
        ondblclick: () => startEdit(0, c),
        oncontextmenu: (e) => showContextMenu(e, 0, c),
      });
      const align = widgetModel.alignments[c] || 'left';
      th.style.textAlign = align;
      th.append(el('span', { class: 'copal-table-cell-content', text: widgetModel.rows[0].cells[c] || '' }));
      const indicator = el('span', { class: 'copal-table-align-indicator', text: align === 'center' ? '↔' : align === 'right' ? '→' : '←' });
      indicator.addEventListener('click', (e) => {
        e.stopPropagation();
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
        const td = el('td', {
          'data-row': String(r), 'data-col': String(c),
          tabindex: '-1',
          onclick: () => setActiveCell(r, c),
          ondblclick: () => startEdit(r, c),
          oncontextmenu: (e) => showContextMenu(e, r, c),
        });
        const raw = widgetModel.rows[r].cells[c] || '';
        const display = raw.startsWith('=') ? evaluateFormula(raw, widgetModel).value || raw : raw;
        td.style.textAlign = widgetModel.alignments[c] || 'left';
        td.append(el('span', { class: 'copal-table-cell-content', text: display }));
        tr.append(td);
      }
      tbody.append(tr);
    }
    table.append(tbody);

    const wrap = el('div', { class: 'copal-table-grid-wrap' });
    wrap.append(table);
    container.replaceChildren(formulaBar, wrap);
  }

  container.addEventListener('keydown', onKeydown);
  container.addEventListener('focus', () => {
    if (activeRow < 0) setActiveCell(0, 0);
  });

  renderBody();
  return container;
}
