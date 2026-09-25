import assert from 'node:assert/strict';
import test from 'node:test';

import {
  parseTable, tableToSource, applyTableEdit, evaluateFormula,
} from '../../static/js/copal/tableModel.js';

// ─── Parser ──────────────────────────────────────────────────────────────────

const SIMPLE_TABLE = `| Name | Age | City |
| :--- | :---: | ---: |
| Alice | 30 | NYC |
| Bob | 25 | LA |`;

test('parseTable parses a simple valid table', () => {
  const result = parseTable(SIMPLE_TABLE);
  assert.equal(result.valid, true);
  assert.equal(result.columns, 3);
  assert.deepEqual(result.alignments, ['left', 'center', 'right']);
  assert.equal(result.rows.length, 3); // header + 2 body
  assert.deepEqual(result.rows[0].cells, ['Name', 'Age', 'City']);
  assert.deepEqual(result.rows[1].cells, ['Alice', '30', 'NYC']);
  assert.deepEqual(result.rows[2].cells, ['Bob', '25', 'LA']);
  assert.equal(result.rows[0].isHeader, true);
  assert.equal(result.rows[1].isHeader, false);
});

test('parseTable tracks source line ranges', () => {
  const result = parseTable(SIMPLE_TABLE, 10);
  assert.equal(result.valid, true);
  assert.equal(result.sourceRange.from, 10);
  assert.equal(result.sourceRange.to, 13);
  assert.equal(result.rows[0].sourceLine, 10);
  assert.equal(result.rows[1].sourceLine, 12);
});

test('parseTable handles escaped pipes', () => {
  const table = `| A | B |
| --- | --- |
| hello \\| world | ok |`;
  const result = parseTable(table);
  assert.equal(result.valid, true);
  assert.equal(result.rows[1].cells[0], 'hello | world');
  assert.equal(result.rows[1].cells[1], 'ok');
});

test('parseTable handles inline code with pipes', () => {
  const table = `| Cmd | Desc |
| --- | --- |
| \`a | b\` | runs |`;
  const result = parseTable(table);
  assert.equal(result.valid, true);
  assert.equal(result.rows[1].cells[0], '`a | b`');
  assert.equal(result.rows[1].cells[1], 'runs');
});

test('parseTable detects missing separator', () => {
  const table = `| A | B |
| 1 | 2 |`;
  const result = parseTable(table);
  assert.equal(result.valid, false);
  assert.ok(result.malformed?.includes('missing-separator'));
});

test('parseTable detects uneven columns', () => {
  const table = `| A | B |
| --- | --- |
| 1 | 2 |
| 3 |`;
  const result = parseTable(table);
  assert.equal(result.valid, false);
  assert.ok(result.malformed?.includes('row-2-col-count'));
});

test('parseTable handles empty cells', () => {
  const table = `| A | B |
| --- | --- |
| | ok |
| hi | |`;
  const result = parseTable(table);
  assert.equal(result.valid, true);
  assert.equal(result.rows[1].cells[0], '');
  assert.equal(result.rows[1].cells[1], 'ok');
  assert.equal(result.rows[2].cells[0], 'hi');
  assert.equal(result.rows[2].cells[1], '');
});

test('parseTable returns no-table for input without pipes', () => {
  const result = parseTable('hello world');
  assert.equal(result.valid, false);
  assert.equal(result.malformed, 'no-table');
});

test('parseTable handles table without leading pipe', () => {
  const table = `A | B
--- | ---
1 | 2`;
  const result = parseTable(table);
  assert.equal(result.valid, true);
  assert.equal(result.columns, 2);
});

test('parseTable handles large table (100+ rows)', () => {
  let table = '| C1 | C2 | C3 |\n| --- | --- | --- |';
  for (let i = 1; i <= 150; i++) table += `\n| r${i} | ${i} | ${i * 2} |`;
  const result = parseTable(table);
  assert.equal(result.valid, true);
  assert.equal(result.rows.length, 151); // header + 150
});

// ─── Serializer ──────────────────────────────────────────────────────────────

test('tableToSource round-trips a valid table', () => {
  const result = parseTable(SIMPLE_TABLE);
  const source = tableToSource(result);
  const reparsed = parseTable(source);
  assert.equal(reparsed.valid, true);
  assert.equal(reparsed.columns, 3);
  assert.deepEqual(reparsed.rows[0].cells, ['Name', 'Age', 'City']);
  assert.deepEqual(reparsed.rows[1].cells, ['Alice', '30', 'NYC']);
  assert.deepEqual(reparsed.rows[2].cells, ['Bob', '25', 'LA']);
});

test('tableToSource preserves alignment', () => {
  const result = parseTable(SIMPLE_TABLE);
  const source = tableToSource(result);
  const reparsed = parseTable(source);
  assert.deepEqual(reparsed.alignments, ['left', 'center', 'right']);
});

// ─── Source synchronization ──────────────────────────────────────────────────

test('applyTableEdit cell edit produces correct changes', () => {
  const doc = SIMPLE_TABLE;
  const model = parseTable(doc);
  const { newText, newModel, changes } = applyTableEdit(doc, model, { type: 'cell', row: 1, col: 1, value: '31' });
  assert.equal(changes.length, 1);
  assert.ok(newModel.valid);
  assert.deepEqual(newModel.rows[1].cells, ['Alice', '31', 'NYC']);
  // Verify the change range covers the table
  assert.ok(changes[0].from >= 0);
  assert.ok(changes[0].to <= newText.length);
});

test('applyTableEdit insert row adds a row', () => {
  const model = parseTable(SIMPLE_TABLE);
  const { newModel } = applyTableEdit(SIMPLE_TABLE, model, { type: 'insertRow', afterRow: 1 });
  assert.equal(newModel.rows.length, 4);
  assert.deepEqual(newModel.rows[2].cells, ['', '', '']);
});

test('applyTableEdit delete row removes a row', () => {
  const model = parseTable(SIMPLE_TABLE);
  const { newModel } = applyTableEdit(SIMPLE_TABLE, model, { type: 'deleteRow', row: 2 });
  assert.equal(newModel.rows.length, 2);
  assert.deepEqual(newModel.rows[1].cells, ['Alice', '30', 'NYC']);
});

test('applyTableEdit insert column adds a column', () => {
  const model = parseTable(SIMPLE_TABLE);
  const { newModel } = applyTableEdit(SIMPLE_TABLE, model, { type: 'insertColumn', afterCol: 1 });
  assert.equal(newModel.columns, 4);
  // afterCol:1 inserts after column 1 (Age), so new column is at index 2
  assert.deepEqual(newModel.rows[0].cells, ['Name', 'Age', '', 'City']);
});

test('applyTableEdit delete column removes a column', () => {
  const model = parseTable(SIMPLE_TABLE);
  const { newModel } = applyTableEdit(SIMPLE_TABLE, model, { type: 'deleteColumn', col: 1 });
  assert.equal(newModel.columns, 2);
  assert.deepEqual(newModel.rows[0].cells, ['Name', 'City']);
});

test('applyTableEdit move row reorders rows', () => {
  const model = parseTable(SIMPLE_TABLE);
  const { newModel } = applyTableEdit(SIMPLE_TABLE, model, { type: 'moveRow', fromRow: 2, toRow: 1 });
  assert.deepEqual(newModel.rows[1].cells, ['Bob', '25', 'LA']);
  assert.deepEqual(newModel.rows[2].cells, ['Alice', '30', 'NYC']);
});

test('applyTableEdit move column reorders columns', () => {
  const model = parseTable(SIMPLE_TABLE);
  const { newModel } = applyTableEdit(SIMPLE_TABLE, model, { type: 'moveColumn', fromCol: 2, toCol: 0 });
  assert.deepEqual(newModel.rows[0].cells, ['City', 'Name', 'Age']);
});

test('applyTableEdit setAlignment changes alignment', () => {
  const model = parseTable(SIMPLE_TABLE);
  const { newModel } = applyTableEdit(SIMPLE_TABLE, model, { type: 'setAlignment', col: 0, alignment: 'center' });
  assert.equal(newModel.alignments[0], 'center');
});

test('applyTableEdit sort sorts rows', () => {
  const doc = `| Name | Age |
| --- | --- |
| Bob | 25 |
| Alice | 30 |`;
  const model = parseTable(doc);
  const { newModel } = applyTableEdit(doc, model, { type: 'sort', col: 1, direction: 'asc' });
  assert.deepEqual(newModel.rows[1].cells, ['Bob', '25']);
  assert.deepEqual(newModel.rows[2].cells, ['Alice', '30']);
});

test('applyTableEdit sort with desc direction', () => {
  const doc = `| Name | Age |
| --- | --- |
| Alice | 30 |
| Bob | 25 |`;
  const model = parseTable(doc);
  const { newModel } = applyTableEdit(doc, model, { type: 'sort', col: 1, direction: 'desc' });
  assert.deepEqual(newModel.rows[1].cells, ['Alice', '30']);
  assert.deepEqual(newModel.rows[2].cells, ['Bob', '25']);
});

test('applyTableEdit sort with string values', () => {
  const doc = `| Name | Age |
| --- | --- |
| Bob | 25 |
| Alice | 30 |`;
  const model = parseTable(doc);
  const { newModel } = applyTableEdit(doc, model, { type: 'sort', col: 0, direction: 'asc' });
  assert.deepEqual(newModel.rows[1].cells[0], 'Alice');
  assert.deepEqual(newModel.rows[2].cells[0], 'Bob');
});

test('applyTableEdit transpose swaps rows and columns', () => {
  const doc = `| Name | Alice | Bob |
| --- | --- | --- |
| Age | 30 | 25 |
| City | NYC | LA |`;
  const model = parseTable(doc);
  const { newModel } = applyTableEdit(doc, model, { type: 'transpose' });
  // Input: 3 rows (header+2 data), 3 cols → Output: 3 rows, 3 cols
  // Rows become columns: each input column becomes an output row
  assert.equal(newModel.rows.length, 3);
  assert.equal(newModel.columns, 3);
});

// ─── All mutations produce valid Markdown ────────────────────────────────────

test('all mutation types produce valid markdown', () => {
  const doc = SIMPLE_TABLE;
  const model = parseTable(doc);
  const edits = [
    { type: 'cell', row: 1, col: 0, value: 'Zara' },
    { type: 'insertRow', afterRow: 1 },
    { type: 'deleteRow', row: 1 },
    { type: 'insertColumn', afterCol: 1 },
    { type: 'deleteColumn', col: 1 },
    { type: 'moveRow', fromRow: 1, toRow: 2 },
    { type: 'moveColumn', fromCol: 0, toCol: 2 },
    { type: 'setAlignment', col: 0, alignment: 'right' },
    { type: 'sort', col: 0 },
  ];
  for (const edit of edits) {
    const { newModel, changes } = applyTableEdit(doc, model, edit);
    assert.ok(changes.length > 0, `${edit.type} should produce changes`);
    // Verify the new model can be serialized back
    const source = tableToSource(newModel);
    assert.ok(source.includes('|'), `${edit.type} should produce valid table`);
  }
});

// ─── Formula engine ──────────────────────────────────────────────────────────

const FORMULA_TABLE = `| Item | Qty | Price |
| --- | --- | --- |
| A | 10 | 5 |
| B | 20 | 3 |
| C | 5 | 8 |`;

test('evaluateFormula sums a range', () => {
  const model = parseTable(FORMULA_TABLE);
  // Rows: 0=header, 1=A/10, 2=B/20, 3=C/5. B1:B3 = 10+20+5 = 35
  const result = evaluateFormula('=SUM(B1:B3)', model);
  assert.equal(result.value, '35');
});

test('evaluateFormula averages a range', () => {
  const model = parseTable(FORMULA_TABLE);
  const result = evaluateFormula('=AVERAGE(B1:B3)', model);
  assert.equal(result.value, '11.67');
});

test('evaluateFormula counts a range', () => {
  const model = parseTable(FORMULA_TABLE);
  const result = evaluateFormula('=COUNT(B1:B3)', model);
  assert.equal(result.value, '3');
});

test('evaluateFormula finds min and max', () => {
  const model = parseTable(FORMULA_TABLE);
  assert.equal(evaluateFormula('=MIN(B2:B4)', model).value, '5');
  assert.equal(evaluateFormula('=MAX(B2:B4)', model).value, '20');
});

test('evaluateFormula evaluates arithmetic', () => {
  const model = parseTable(FORMULA_TABLE);
  // B1=10, C1=5, so B1*C1=50
  const result = evaluateFormula('=B1*C1', model);
  assert.equal(result.value, '50');
});

test('evaluateFormula evaluates IF', () => {
  const model = parseTable(FORMULA_TABLE);
  // B1=10, 10>5 is true, so returns "Yes"
  const result = evaluateFormula('=IF(B1>5,Yes,No)', model);
  assert.equal(result.value, 'Yes');
});

test('evaluateFormula handles nested expressions', () => {
  const model = parseTable(FORMULA_TABLE);
  const result = evaluateFormula('=SUM(B1:B3) + 10', model);
  assert.equal(result.value, '45');
});

test('evaluateFormula returns 0 for out-of-range references', () => {
  const model = parseTable(FORMULA_TABLE);
  const result = evaluateFormula('=SUM(Z1:Z10)', model);
  // All cells are out of range, so sum is 0
  assert.equal(result.value, '0');
});

test('evaluateFormula returns non-formula as-is', () => {
  const model = parseTable(FORMULA_TABLE);
  const result = evaluateFormula('hello', model);
  assert.equal(result.value, 'hello');
});

test('evaluateFormula caps at 1000 cells', () => {
  let table = '| C1 |\n| --- |';
  for (let i = 0; i < 1001; i++) table += `\n| ${i} |`;
  const model = parseTable(table);
  const result = evaluateFormula('=SUM(A1:A1001)', model);
  assert.ok(result.error);
});

test('evaluateFormula handles division by zero', () => {
  const model = parseTable(FORMULA_TABLE);
  const result = evaluateFormula('=B2/0', model);
  assert.ok(result.error);
});

// ─── Cell offset tracking ───────────────────────────────────────────────────

test('parseTable computes cell offsets per row', () => {
  const result = parseTable(SIMPLE_TABLE);
  assert.ok(result.rows[0].cellOffsets.length > 0);
  // Each offset should have from/to
  for (const offset of result.rows[0].cellOffsets) {
    assert.ok(typeof offset.from === 'number');
    assert.ok(typeof offset.to === 'number');
    assert.ok(offset.to > offset.from);
  }
});

// ─── Changes are CodeMirror-compatible ───────────────────────────────────────

test('changes have from, to, insert structure', () => {
  const model = parseTable(SIMPLE_TABLE);
  const { changes } = applyTableEdit(SIMPLE_TABLE, model, { type: 'cell', row: 1, col: 0, value: 'X' });
  for (const change of changes) {
    assert.ok('from' in change);
    assert.ok('to' in change);
    assert.ok('insert' in change);
    assert.ok(typeof change.from === 'number');
    assert.ok(typeof change.to === 'number');
    assert.ok(typeof change.insert === 'string');
  }
});

// ─── S18: typed columns (text/number/date/currency) ─────────────────────────

import {
  parseTypedCell, formatTypedCell, compareTyped, sortBodyRows,
} from '../../static/js/copal/tableTypes.js';

import {
  tableBlockToSource, evaluateCell, parseTableMetadata, serializeTableMetadata,
} from '../../static/js/copal/tableModel.js';

test('parseTypedCell classifies text blanks and values', () => {
  assert.equal(parseTypedCell('', 'text').status, 'blank');
  assert.equal(parseTypedCell('  ', 'text').status, 'blank');
  assert.equal(parseTypedCell('hello', 'text').status, 'valid');
});

test('parseTypedCell classifies numbers including negatives and decimals', () => {
  assert.equal(parseTypedCell('42', 'number').status, 'valid');
  assert.equal(parseTypedCell('-4.5', 'number').status, 'valid');
  assert.equal(parseTypedCell('1.5e3', 'number').status, 'valid');
  assert.equal(parseTypedCell('abc', 'number').status, 'invalid');
  assert.equal(parseTypedCell('', 'number').status, 'blank');
  assert.equal(parseTypedCell('12.50', 'number').number, 12.5);
});

test('parseTypedCell classifies ISO calendar dates and rejects opaque or invalid ones', () => {
  assert.equal(parseTypedCell('2026-09-22', 'date').status, 'valid');
  assert.equal(parseTypedCell('2026-09-22', 'date').date, '2026-09-22');
  assert.equal(parseTypedCell('2026-02-30', 'date').status, 'invalid');
  assert.equal(parseTypedCell('2026-13-01', 'date').status, 'invalid');
  assert.equal(parseTypedCell('09/22/2026', 'date').status, 'invalid');
  assert.equal(parseTypedCell('45922', 'date').status, 'invalid');
});

test('parseTypedCell classifies currency with explicit code/value', () => {
  const money = parseTypedCell('USD 12.50', 'currency');
  assert.equal(money.status, 'valid');
  assert.deepEqual({ code: money.currency.code, value: money.currency.value }, { code: 'USD', value: 12.5 });
  assert.equal(parseTypedCell('12.50', 'currency').status, 'invalid');
  assert.equal(parseTypedCell('USD abc', 'currency').status, 'invalid');
  assert.equal(parseTypedCell('US 12.50', 'currency').status, 'invalid');
});

test('formatTypedCell keeps ISO date source but can display locale', () => {
  assert.equal(formatTypedCell('2026-09-22', 'date', 'iso'), '2026-09-22');
  const localeDisplay = formatTypedCell('2026-09-22', 'date', 'locale', 'en-US');
  assert.equal(typeof localeDisplay, 'string');
  assert.ok(localeDisplay.includes('2026') || localeDisplay.includes('26'));
  assert.ok(!localeDisplay.includes('T00:00'));
});

test('formatTypedCell shows currency code or locale money without changing source', () => {
  assert.equal(formatTypedCell('USD 12.50', 'currency', 'code'), 'USD 12.5');
  const localeMoney = formatTypedCell('USD 12.50', 'currency', 'locale', 'en-US');
  assert.ok(localeMoney.includes('12.5'));
  assert.ok(localeMoney.includes('$') || localeMoney.includes('USD'));
});

test('compareTyped sorts ISO dates chronologically', () => {
  assert.ok(compareTyped('2026-01-05', '2026-09-22', 'date') < 0);
  assert.ok(compareTyped('2026-09-22', '2025-12-31', 'date') > 0);
  assert.ok(compareTyped('2026-09-22', '2026-09-22', 'date') === 0);
});

test('compareTyped puts blanks and invalids after valid values', () => {
  assert.ok(compareTyped('', '5', 'number') > 0);
  assert.ok(compareTyped('5', '', 'number') < 0);
  assert.ok(compareTyped('abc', '5', 'number') > 0);
  assert.ok(compareTyped('abc', '', 'number') < 0);
});

test('compareTyped sorts currency by code then value', () => {
  assert.ok(compareTyped('EUR 1.00', 'USD 9.00', 'currency') < 0);
  assert.ok(compareTyped('USD 1.00', 'USD 9.00', 'currency') < 0);
});

test('compareTyped on untyped columns keeps number-or-text and leaves dates as text', () => {
  assert.ok(compareTyped('9', '10', '') < 0);
  assert.ok(compareTyped('apple', 'banana', '') < 0);
  assert.ok(compareTyped('2026-09-22', '2026-09-03', '') > 0);
});

test('sortBodyRows is stable for ties and keeps blanks last in both directions', () => {
  const rows = [
    { cells: ['h'], isHeader: true },
    { cells: ['b'], isHeader: false },
    { cells: ['a'], isHeader: false },
    { cells: [''], isHeader: false },
    { cells: ['a'], isHeader: false },
    { cells: ['c'], isHeader: false },
  ];
  const asc = sortBodyRows(rows, 0, 'text', 'asc').slice(1).map((r) => r.cells[0]);
  assert.deepEqual(asc, ['a', 'a', 'b', 'c', '']);
  const desc = sortBodyRows(rows, 0, 'text', 'desc').slice(1).map((r) => r.cells[0]);
  assert.deepEqual(desc, ['c', 'b', 'a', 'a', '']);
});

test('sortBodyRows keeps invalid and blank typed cells below valid values', () => {
  const rows = [
    { cells: ['n'], isHeader: true },
    { cells: ['2'], isHeader: false },
    { cells: ['bad'], isHeader: false },
    { cells: ['10'], isHeader: false },
    { cells: [''], isHeader: false },
    { cells: ['-3'], isHeader: false },
  ];
  const asc = sortBodyRows(rows, 0, 'number', 'asc').slice(1).map((r) => r.cells[0]);
  assert.deepEqual(asc, ['-3', '2', '10', 'bad', '']);
  const desc = sortBodyRows(rows, 0, 'number', 'desc').slice(1).map((r) => r.cells[0]);
  assert.deepEqual(desc, ['10', '2', '-3', 'bad', '']);
});

test('sortBodyRows typed date sort is chronological', () => {
  const rows = [
    { cells: ['d'], isHeader: true },
    { cells: ['2026-09-22'], isHeader: false },
    { cells: ['2026-01-05'], isHeader: false },
    { cells: ['2026-12-31'], isHeader: false },
  ];
  const asc = sortBodyRows(rows, 0, 'date', 'asc').slice(1).map((r) => r.cells[0]);
  assert.deepEqual(asc, ['2026-01-05', '2026-09-22', '2026-12-31']);
});

// ─── S18: clank-table metadata ──────────────────────────────────────────────

test('parseTable discovers adjacent clank-table metadata', () => {
  const doc = `<!-- clank-table v=1 id=tbl-9
column id=col-a type=date format=iso
column id=col-b type=currency format=code
-->
| When | Amount |
| :--- | ---: |
| 2026-09-22 | USD 12.50 |`;
  const model = parseTable(doc);
  assert.equal(model.valid, true);
  assert.ok(model.metadata, 'metadata attached');
  assert.equal(model.metadata.tableId, 'tbl-9');
  assert.equal(model.metadata.columns[0].type, 'date');
  assert.equal(model.metadata.columns[1].type, 'currency');
  assert.equal(model.blockRange.from, 0);
  assert.equal(model.sourceRange.from, 4);
});

test('parseTable preserves tables with no metadata and unknown metadata fields', () => {
  const plain = parseTable(SIMPLE_TABLE);
  assert.equal(plain.metadata, null);
  assert.equal(plain.blockRange.from, plain.sourceRange.from);

  const future = `<!-- clank-table v=2 id=tbl-x future-key=keep-me
column id=col-a type=date format=iso future-col=yes
-->
| When |
| :--- |
| 2026-09-22 |`;
  const model = parseTable(future);
  assert.equal(model.valid, true);
  assert.equal(model.metadata.version, 2);
  assert.ok(model.metadata.extras.includes('future-key=keep-me'));
  assert.ok(model.metadata.columns[0].extras.includes('future-col=yes'));
  const roundTrip = serializeTableMetadata(model.metadata);
  assert.ok(roundTrip.includes('future-key=keep-me'));
  assert.ok(roundTrip.includes('future-col=yes'));
});

test('tableBlockToSource keeps metadata adjacent to the table', () => {
  const doc = `<!-- clank-table v=1 id=tbl-1
column id=col-a type=date format=iso
-->
| When |
| :--- |
| 2026-09-22 |`;
  const model = parseTable(doc);
  const block = tableBlockToSource(model);
  assert.ok(block.startsWith('<!-- clank-table'));
  assert.ok(block.includes('When'));
  assert.ok(block.includes('2026-09-22'));
  const reparsed = parseTable(block);
  assert.equal(reparsed.valid, true);
  assert.equal(reparsed.metadata.columns[0].type, 'date');
});

test('setColumnType writes metadata without touching unrelated source', () => {
  const doc = `Intro paragraph.

| Name | Qty |
| :--- | ---: |
| A | 1 |
| B | 2 |

Outro paragraph.`;
  // Parse just the table block at its absolute document start line.
  const block = doc.split('\n').slice(2, 6).join('\n');
  const model = parseTable(block, 2);
  const { newText, newModel } = applyTableEdit(doc, model, { type: 'setColumnType', col: 1, columnType: 'number' });
  assert.ok(newText.includes('Intro paragraph.'));
  assert.ok(newText.includes('Outro paragraph.'));
  assert.equal(newModel.metadata.columns[1].type, 'number');
  assert.ok(newText.includes('column'));
});

test('column insert/delete/move rewrites metadata column identity', () => {
  const doc = `<!-- clank-table v=1 id=tbl-1
column id=col-a type=date format=iso
column id=col-b type=currency format=code
-->
| When | Amount |
| :--- | ---: |
| 2026-09-22 | USD 12.50 |`;
  const model = parseTable(doc);
  const afterInsert = applyTableEdit(doc, model, { type: 'insertColumn', afterCol: 0 });
  assert.equal(afterInsert.newModel.columns, 3);
  assert.equal(afterInsert.newModel.metadata.columns.length, 3);
  assert.equal(afterInsert.newModel.metadata.columns[1].type, '');
  assert.equal(afterInsert.newModel.metadata.columns[2].type, 'currency');

  const afterDelete = applyTableEdit(doc, model, { type: 'deleteColumn', col: 0 });
  assert.equal(afterDelete.newModel.metadata.columns[0].type, 'currency');
});

// ─── S18: recursive formulas, cycles, and explicit errors ───────────────────

test('evaluateFormula evaluates referenced formula cells recursively', () => {
  const doc = `| Item | Qty | Total |
| :--- | ---: | ---: |
| A | 10 | =B1*2 |
| B | 20 | =B2*2 |
| Sum | | =SUM(C1:C2) |`;
  const model = parseTable(doc);
  assert.equal(evaluateCell(model, 1, 2).value, '20');
  assert.equal(evaluateCell(model, 2, 2).value, '40');
  const sum = evaluateFormula('=SUM(C1:C2)', model);
  assert.equal(sum.value, '60');
});

test('evaluateFormula reports cycles instead of hanging', () => {
  const doc = `| A | B |
| :--- | ---: |
| =B1 | =A1 |`;
  const model = parseTable(doc);
  const result = evaluateFormula('=A1', model);
  assert.equal(result.error, '#CYCLE!');
  assert.equal(result.code, 'CYCLE');
});

test('evaluateFormula distinguishes missing references from numeric zero', () => {
  const doc = `| A | B |
| :--- | ---: |
| 0 | =A1 |`;
  const model = parseTable(doc);
  const zero = evaluateFormula('=A1', model);
  assert.equal(zero.error, undefined);
  assert.equal(zero.value, '0');

  const missing = evaluateFormula('=Z9', model);
  assert.equal(missing.error, '#REF!');
  assert.equal(missing.code, 'REF');
  assert.notEqual(missing.value, '0');
});

test('evaluateFormula division by zero is not a legitimate zero', () => {
  const doc = `| A |
| :--- |
| 5 |`;
  const model = parseTable(doc);
  const result = evaluateFormula('=A1/0', model);
  assert.equal(result.error, '#DIV/0!');
  assert.notEqual(result.value, '0');
});

test('evaluateFormula propagates errors through dependent formulas', () => {
  const doc = `| A | B |
| :--- | ---: |
| =Z1 | =A1+1 |`;
  const model = parseTable(doc);
  const dependent = evaluateFormula('=B1', model);
  assert.equal(dependent.error, '#REF!');
});

// ─── S18: reference repair on structural edits and sort ─────────────────────

test('insertColumn shifts formula references', () => {
  const doc = `| A | B | C |
| :--- | ---: | ---: |
| 1 | 2 | =B1*2 |`;
  const model = parseTable(doc);
  const { newModel } = applyTableEdit(doc, model, { type: 'insertColumn', afterCol: 0 });
  assert.equal(newModel.rows[1].cells[3], '=C1*2');
});

test('deleteColumn rewrites references to the removed column as #REF!', () => {
  const doc = `| A | B | C |
| :--- | ---: | ---: |
| 1 | 2 | =B1*2 |`;
  const model = parseTable(doc);
  const { newModel } = applyTableEdit(doc, model, { type: 'deleteColumn', col: 1 });
  assert.equal(newModel.rows[1].cells[1], '=#REF!*2');
});

test('insertRow and deleteRow rewrite row references', () => {
  const doc = `| A | B |
| :--- | ---: |
| 1 | =A2+1 |
| 2 | =A1+1 |`;
  const model = parseTable(doc);
  const inserted = applyTableEdit(doc, model, { type: 'insertRow', afterRow: 1 });
  // New empty row lands at index 2; old row 2 becomes index 3 (A2 → A3).
  assert.equal(inserted.newModel.rows[1].cells[1], '=A3+1');
  assert.equal(inserted.newModel.rows[3].cells[1], '=A1+1');

  const deleted = applyTableEdit(doc, model, { type: 'deleteRow', row: 1 });
  // Row 1 is removed; the formula that referenced A1 is now a missing reference.
  assert.equal(deleted.newModel.rows[1].cells[1], '=#REF!+1');
});

test('sort rewrites single-cell references to follow their data rows', () => {
  const doc = `| Name | Qty | Doubled |
| :--- | ---: | ---: |
| Bob | 2 | =B1*2 |
| Alice | 1 | =B2*2 |`;
  const model = parseTable(doc);
  const { newModel } = applyTableEdit(doc, model, { type: 'sort', col: 0, direction: 'asc' });
  // Alice moves to row 1, Bob to row 2; formulas follow their own rows.
  assert.equal(newModel.rows[1].cells[0], 'Alice');
  assert.equal(newModel.rows[1].cells[2], '=B1*2');
  assert.equal(newModel.rows[2].cells[0], 'Bob');
  assert.equal(newModel.rows[2].cells[2], '=B2*2');
});

test('sort keeps range references positional over the sorted body', () => {
  const doc = `| Name | Qty |
| :--- | ---: |
| Bob | 2 |
| Alice | 1 |
| Total | =SUM(B1:B2) |`;
  const model = parseTable(doc);
  const { newModel } = applyTableEdit(doc, model, { type: 'sort', col: 0, direction: 'asc' });
  const totalRow = newModel.rows.find((row) => row.cells[0] === 'Total');
  assert.equal(totalRow.cells[1], '=SUM(B1:B2)');
});

test('moveColumn rewrites formula references consistently', () => {
  const doc = `| A | B | C |
| :--- | ---: | ---: |
| 1 | 2 | =B1*2 |`;
  const model = parseTable(doc);
  const { newModel } = applyTableEdit(doc, model, { type: 'moveColumn', fromCol: 1, toCol: 2 });
  // B moves after C, so the old B1 is now C1 and the formula travels with its cell.
  assert.equal(newModel.rows[0].cells.join(','), 'A,C,B');
  assert.equal(newModel.rows[1].cells[1], '=C1*2');
});

test('each structural edit is one change transaction for one-step undo', () => {
  const doc = `| A | B |
| :--- | ---: |
| 1 | =A1+1 |
| 2 | =A2+1 |`;
  const model = parseTable(doc);
  const { changes } = applyTableEdit(doc, model, { type: 'insertRow', afterRow: 1 });
  assert.equal(changes.length, 1);
});

test('CRLF and escaped pipes round-trip through structural edits', () => {
  const doc = '| A | B |\r\n| :--- | ---: |\r\n| x \\| y | 1 |';
  const model = parseTable(doc);
  assert.equal(model.valid, true);
  assert.equal(model.rows[1].cells[0], 'x | y');
  const { newModel } = applyTableEdit(doc, model, { type: 'insertRow', afterRow: 1 });
  assert.equal(newModel.valid, true);
  assert.equal(newModel.rows[1].cells[0], 'x | y');
});

test('demo typed table computes totals', () => {
  const doc = `<!-- clank-table v=1 id=tbl-demo
column id=col-when type=date format=locale
column id=col-item type=text format=auto
column id=col-amount type=currency format=locale
column id=col-qty type=number format=locale
-->
| When | Item | Amount | Qty |
| :--- | :--- | ---: | ---: |
| 2026-09-22 | Hosting | USD 12.50 | 2 |
| 2026-10-01 | Stickers | USD 4.00 | 5 |
| 2026-10-15 | Lunch | USD 9.25 | 1 |
| | Total | =SUM(C1:C3) | =SUM(D1:D3) |`;
  const model = parseTable(doc);
  assert.equal(model.valid, true);
  assert.equal(model.metadata.columns[0].type, 'date');
  const amountTotal = evaluateFormula('=SUM(C1:C3)', model);
  assert.equal(amountTotal.value, '25.75');
  const qtyTotal = evaluateFormula('=SUM(D1:D3)', model);
  assert.equal(qtyTotal.value, '8');
  const sorted = sortBodyRows(model.rows, 0, 'date', 'asc');
  assert.equal(sorted[1].cells[0], '2026-09-22');
});
