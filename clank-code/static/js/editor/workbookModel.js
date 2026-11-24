// Versioned ordinary-file workbook source. Evaluation delegates to the shared
// table engine; computed values never replace the user's raw source.
import { evaluateCell } from '../copal/tableModel.js';
import { portableDeliverableStyle } from '../theme/deliverableAppearance.js';

const ID = /^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/;
const MAX_SHEETS = 32, MAX_ROWS = 500, MAX_COLUMNS = 100, MAX_CELLS = 10000;
const clone = value => JSON.parse(JSON.stringify(value));
const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

export function parseWorkbook(source) {
  const raw = typeof source === 'string' ? source : JSON.stringify(source);
  if (typeof raw !== 'string' || raw.length > 8 * 1024 * 1024) throw new TypeError('Workbook source exceeds limit');
  const book = JSON.parse(raw);
  if (!book || book.schema !== 'openclank.workbook' || book.version !== 1 || !Array.isArray(book.sheets)
      || !book.sheets.length || book.sheets.length > MAX_SHEETS || !Array.isArray(book.ranges) || book.ranges.length > 256 || !Array.isArray(book.charts) || book.charts.length > 256) throw new TypeError('Invalid workbook schema');
  const ids = new Set(), cells = new Map(), sheets = new Set(); let total = 0;
  const take = id => { if (typeof id !== 'string' || !ID.test(id) || ids.has(id)) throw new TypeError('Invalid or duplicate workbook identity'); ids.add(id); };
  for (const sheet of book.sheets) {
    take(sheet.id); sheets.add(sheet.id);
    if (typeof sheet.name !== 'string' || !sheet.name.trim() || !Array.isArray(sheet.rows) || !sheet.rows.length || sheet.rows.length > MAX_ROWS) throw new TypeError('Invalid workbook sheet');
    const width = sheet.rows[0]?.length;
    if (!width || width > MAX_COLUMNS) throw new TypeError('Invalid workbook columns');
    for (const [r, row] of sheet.rows.entries()) {
      if (!Array.isArray(row) || row.length !== width) throw new TypeError('Workbook rows must be rectangular');
      for (const [c, cell] of row.entries()) {
        if (!cell || typeof cell !== 'object' || typeof cell.raw !== 'string' || cell.raw.length > 65536) throw new TypeError('Invalid workbook cell');
        take(cell.id); cells.set(cell.id, {sheetId:sheet.id, row:r, col:c});
        if (++total > MAX_CELLS) throw new TypeError('Workbook exceeds cell limit');
      }
    }
  }
  const ranges = new Map();
  for (const range of book.ranges) {
    take(range.id); const start = cells.get(range.startCellId), end = cells.get(range.endCellId);
    if (!sheets.has(range.sheetId) || !start || !end || start.sheetId !== range.sheetId || end.sheetId !== range.sheetId) throw new TypeError('Invalid workbook range');
    ranges.set(range.id, range);
  }
  for (const chart of book.charts) {
    take(chart.id);
    if (!ranges.has(chart.rangeId) || !['bar','line'].includes(chart.type) || typeof chart.title !== 'string' || chart.title.length > 512) throw new TypeError('Invalid workbook chart');
  }
  return book;
}

export function serializeWorkbook(book) { return JSON.stringify(parseWorkbook(book), null, 2) + '\n'; }

export function newWorkbookSource(appearance) {
  return serializeWorkbook({
    schema:'openclank.workbook',version:1,appearance,ranges:[],charts:[],
    sheets:[{id:'sheet-1',name:'Sheet 1',rows:Array.from({length:20},(_,row)=>
      Array.from({length:8},(_,column)=>({id:`r${row+1}-c${column+1}`,raw:''})))}],
  });
}

// One immutable calculation owns typed dependency state and a separate budget per sheet.
export function calculateWorkbook(input, { isCurrent = () => true, workLimit = 20000 } = {}) {
  const book = parseWorkbook(input), values = {}, memo = new Map(), visiting = new Set();
  const limit = Number.isSafeInteger(workLimit) ? Math.max(0, Math.min(20000, workLimit)) : 20000;
  const tables = new Map(), names = new Map(), budgets = new Map();
  for (const sheet of book.sheets) {
    tables.set(sheet.id, {valid:true, columns:sheet.rows[0].length,
      rows:[{cells:sheet.rows[0].map(() => '')}, ...sheet.rows.map(row => ({cells:row.map(cell => cell.raw)}))]});
    budgets.set(sheet.id, {remaining:limit});
    const matches = names.get(sheet.name) || []; matches.push(sheet.id); names.set(sheet.name, matches);
  }
  const resolveSheet = name => {
    const matches = names.get(name);
    if (!matches || matches.length !== 1) return;
    return {sheetId:matches[0], model:tables.get(matches[0])};
  };
  const current = () => { try { return isCurrent() === true; } catch (_) { return false; } };
  const cancelled = () => ({error:'#CANCELLED!', code:'CANCELLED', message:'Calculation was superseded', value:'#CANCELLED!', state:'error'});
  for (const sheet of book.sheets) {
    const table = tables.get(sheet.id), options = {sheetId:sheet.id, resolveSheet, budgets, memo, visiting, isCurrent:current};
    for (const [r, row] of sheet.rows.entries()) for (const [c, cell] of row.entries()) {
      if (!current()) { values[cell.id] = cancelled(); continue; }
      const result = evaluateCell(table, r + 1, c, options);
      values[cell.id] = current() ? {...result, state:result.error ? 'error' : 'ready'} : cancelled();
    }
  }
  // Do not publish any earlier values after the owning generation is superseded.
  if (!current()) for (const sheet of book.sheets) for (const row of sheet.rows) for (const cell of row) values[cell.id] = cancelled();
  return values;
}

export function chartValues(input, chartId, values = null) {
  const book = parseWorkbook(input), chart = book.charts.find(item => item.id === chartId);
  if (!chart) throw new TypeError('Unknown chart');
  const range = book.ranges.find(item => item.id === chart.rangeId), sheet = book.sheets.find(item => item.id === range.sheetId);
  const positions = new Map(); sheet.rows.forEach((row, r) => row.forEach((cell, c) => positions.set(cell.id, [r,c])));
  const [a,b] = [positions.get(range.startCellId), positions.get(range.endCellId)], computed = values || calculateWorkbook(book), rows = [];
  for (let r = Math.min(a[0],b[0]); r <= Math.max(a[0],b[0]); r++) {
    rows.push(sheet.rows[r].slice(Math.min(a[1],b[1]),Math.max(a[1],b[1])+1).map(cell => ({cellId:cell.id,...computed[cell.id]})));
  }
  return {chartId:chart.id, rangeId:range.id, sheetId:sheet.id, rows};
}

export function editWorkbookCell(input, sheetId, cellId, raw) {
  if (typeof raw !== 'string' || raw.length > 65536) throw new TypeError('Invalid cell source');
  const book = parseWorkbook(input), sheet = book.sheets.find(item => item.id === sheetId);
  const cell = sheet?.rows.flat().find(item => item.id === cellId);
  if (!cell) throw new TypeError('Unknown workbook cell');
  cell.raw = raw; return book;
}

// Existing ResourceBuffer owns revisions, undo and conflict handling.
export function applyWorkbookEdit(buffer, sheetId, cellId, raw) {
  if (!buffer || typeof buffer.apply !== 'function') throw new TypeError('Resource buffer required');
  return buffer.apply(envelope => ({...envelope, text:serializeWorkbook(editWorkbookCell(envelope.text, sheetId, cellId, raw))}));
}

// Upserts stay in the ordinary workbook transaction; callers supply stable IDs.
export function putWorkbookRange(input, range) {
  if (!range || typeof range !== 'object' || !ID.test(range.id || '')) throw new TypeError('Invalid range identity');
  const book = parseWorkbook(input), index = book.ranges.findIndex(item => item.id === range.id);
  if (index < 0 && book.ranges.length >= 64) throw new RangeError('Workbook range limit reached');
  if (index < 0) book.ranges.push({...range}); else book.ranges[index] = {...book.ranges[index], ...range, id:range.id};
  return parseWorkbook(book);
}
export function putWorkbookChart(input, chart) {
  if (!chart || typeof chart !== 'object' || typeof chart.id !== 'string' || !ID.test(chart.id)) throw new TypeError('Invalid chart specification');
  const book = parseWorkbook(input), index = book.charts.findIndex(item => item.id === chart.id);
  if (index < 0 && book.charts.length >= 64) throw new RangeError('Workbook chart limit reached');
  if (index < 0) book.charts.push({...chart}); else book.charts[index] = {...book.charts[index], ...chart, id:chart.id};
  return parseWorkbook(book);
}

/** Bounded row-major chart; gaps remain visible and never become zero. */
export function workbookChartSvg(input, chartId, computed = null) {
  const book = parseWorkbook(input), chart = book.charts.find(item => item.id === chartId);
  if (!chart) throw new TypeError('Unknown chart');
  const data = chartValues(book, chartId, computed), cells = data.rows.flat();
  const appearance = portableDeliverableStyle(book.appearance), fg = appearance?.cssVariables?.['--oc-fg'] || '#1b2838';
  const bg = appearance?.cssVariables?.['--oc-panel'] || '#ffffff', accent = appearance?.cssVariables?.['--oc-accent'] || '#315caa';
  const head = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 640 260" role="img" aria-label="${escapeHtml(chart.title)}" data-chart-id="${escapeHtml(chart.id)}" data-range-id="${escapeHtml(chart.rangeId)}"><rect width="640" height="260" fill="${escapeHtml(bg)}"/><text x="16" y="23" fill="${escapeHtml(fg)}" font-size="16">${escapeHtml(chart.title)}</text>`;
  if (cells.length > 200) return `${head}<text x="16" y="55" fill="${escapeHtml(fg)}">Chart unavailable: range exceeds 200 cells</text></svg>`;
  const number = item => {
    if (item?.error || item?.state === 'error') return null;
    const value = item?.value;
    if (typeof value === 'number') return Number.isFinite(value) ? value : null;
    if (typeof value !== 'string' || !/^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/.test(value.trim())) return null;
    const parsed = Number(value.trim()); return Number.isFinite(parsed) ? parsed : null;
  };
  const nums = cells.map(number), finiteValues = nums.filter(value => value !== null);
  if (!finiteValues.length) return `${head}<text x="16" y="55" fill="${escapeHtml(fg)}">Chart unavailable: no finite values</text></svg>`;
  const magnitude = Math.max(...finiteValues.map(Math.abs), 1), normalized = nums.map(v => v === null ? null : v/magnitude);
  let lo = Math.min(0,...normalized.filter(v => v !== null)), hi = Math.max(0,...normalized.filter(v => v !== null));
  if (lo === hi) {lo = -1; hi = 1;}
  const top=40, bottom=216, left=40, width=580, y = v => bottom - (v-lo)/(hi-lo)*(bottom-top), zero=y(0), step=width/Math.max(cells.length,1);
  const lower = Math.min(0,...finiteValues), upper = Math.max(0,...finiteValues);
  let output = `<line x1="${left}" y1="${zero}" x2="${left+width}" y2="${zero}" stroke="${escapeHtml(fg)}"/><text x="3" y="${top+4}" fill="${escapeHtml(fg)}" font-size="9">${escapeHtml(upper)}</text><text x="3" y="${bottom}" fill="${escapeHtml(fg)}" font-size="9">${escapeHtml(lower)}</text>`;
  const points = [];
  for (let i=0;i<cells.length;i++) {
    const x=left+(i+.5)*step, v=normalized[i], cell=cells[i];
    if (cells.length <= 20) output += `<text x="${x}" y="231" fill="${escapeHtml(fg)}" font-size="9" text-anchor="middle">${escapeHtml(cell.cellId.slice(0,12))}</text>`;
    if (v === null) { points.push(null); output += `<text x="${x}" y="244" fill="${escapeHtml(fg)}" font-size="10" text-anchor="middle" data-cell-id="${escapeHtml(cell.cellId)}">!<title>${escapeHtml(cell.cellId)}: ${escapeHtml(cell.value ?? 'missing')}</title></text>`; continue; }
    const py=y(v); points.push([x,py]);
    if (chart.type === 'bar') output += `<rect x="${left+i*step+step*.1}" y="${Math.min(zero,py)}" width="${step*.8}" height="${Math.abs(zero-py)}" fill="${escapeHtml(accent)}" data-cell-id="${escapeHtml(cell.cellId)}"><title>${escapeHtml(cell.cellId)}: ${escapeHtml(cell.value)}</title></rect>`;
    else output += `<circle cx="${x}" cy="${py}" r="3" fill="${escapeHtml(accent)}" data-cell-id="${escapeHtml(cell.cellId)}"><title>${escapeHtml(cell.cellId)}: ${escapeHtml(cell.value)}</title></circle>`;
  }
  if (chart.type === 'line') for (let i=1;i<points.length;i++) if (points[i-1] && points[i]) output += `<line x1="${points[i-1][0]}" y1="${points[i-1][1]}" x2="${points[i][0]}" y2="${points[i][1]}" stroke="${escapeHtml(accent)}"/>`;
  if (nums.some(v => v === null)) output += `<text x="16" y="257" fill="${escapeHtml(fg)}" font-size="10">! = missing, nonnumeric, or formula error; see source table</text>`;
  return `${head}${output}</svg>`;
}

export function workbookHtml(input) {
  const book = parseWorkbook(input), values = calculateWorkbook(book), appearance = portableDeliverableStyle(book.appearance);
  const vars = appearance?.cssVariables || {}, bg = vars['--oc-bg'] || '#ffffff', fg = vars['--oc-fg'] || '#161616', panel = vars['--oc-panel'] || '#ffffff', border = vars['--oc-border'] || '#777777';
  const sheets = book.sheets.map(sheet => {
    const charts = book.charts.filter(chart => book.ranges.find(range => range.id === chart.rangeId)?.sheetId === sheet.id);
    const rendered = charts.slice(0,64).map(chart => workbookChartSvg(book,chart.id,values)).join('');
    return `<section><h2>${escapeHtml(sheet.name)}</h2><table>${sheet.rows.map(row => `<tr>${row.map(cell => `<td data-cell-id="${escapeHtml(cell.id)}" title="${escapeHtml(cell.raw)}">${escapeHtml(values[cell.id].value)}</td>`).join('')}</tr>`).join('')}</table>${rendered}${charts.length>64?'<p>Additional charts omitted from this preview.</p>':''}</section>`;
  }).join('');
  const embedded = JSON.stringify(book).replace(/</g,'\\u003c').replace(/\u2028/g,'\\u2028').replace(/\u2029/g,'\\u2029');
  const style = `body{background:${bg};color:${fg};font-family:${appearance?.typography?.family || 'sans-serif'}}table{background:${panel};border-collapse:collapse}td{border:1px solid ${border};padding:.25rem}svg{display:block;width:min(100%,640px);height:auto}`;
  return `<!doctype html><html><head><meta charset="utf-8"><title>Workbook</title><style>${style}</style></head><body>${sheets}<script type="application/json" id="openclank-workbook-source">${embedded}</script></body></html>`;
}
