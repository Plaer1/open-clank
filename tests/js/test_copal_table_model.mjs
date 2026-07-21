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
