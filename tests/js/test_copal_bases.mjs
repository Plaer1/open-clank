import assert from 'node:assert/strict';
import test from 'node:test';

import { formatBaseCell, makeDefaultBase, serializeBase, updateViewSort, flattenFilterToLines, hasNestedFilterGroups, parseFilterLines, removeBaseView, reorderBaseView, makeViewFromTemplate, VIEW_TYPES, parseDataviewQuery, reorderBaseColumn } from '../../static/js/copal/bases.js';

test('default Base is a versioned live table definition', () => {
  const base = makeDefaultBase('Projects');
  assert.equal(base.version, 1);
  assert.deepEqual(base.views[0].columns.map((column) => column.property), ['file.name', 'tags']);
  assert.equal(JSON.parse(serializeBase(base)).extensions.title, 'Projects');
});

test('sort cycles asc, desc, none and preserves source definition', () => {
  const source = makeDefaultBase();
  const asc = updateViewSort(source, 'table', 'file.name');
  const desc = updateViewSort(asc, 'table', 'file.name');
  const none = updateViewSort(desc, 'table', 'file.name');
  assert.deepEqual(source.views[0].sorts, []);
  assert.deepEqual(asc.views[0].sorts, [{ property: 'file.name', direction: 'asc' }]);
  assert.equal(desc.views[0].sorts[0].direction, 'desc');
  assert.deepEqual(none.views[0].sorts, []);
});

test('cell formatting exposes missing and compound values honestly', () => {
  assert.equal(formatBaseCell(null), '—');
  assert.equal(formatBaseCell(['a', 'b']), 'a, b');
  assert.equal(formatBaseCell(false), 'No');
});

test('hasNestedFilterGroups detects nested and/or groups', () => {
  assert.equal(hasNestedFilterGroups(null), false);
  assert.equal(hasNestedFilterGroups({ property: 'x', operator: 'eq' }), false);
  assert.equal(hasNestedFilterGroups({ and: [{ property: 'x', operator: 'eq' }] }), false);
  assert.equal(hasNestedFilterGroups({ and: [{ or: [{ property: 'x', operator: 'eq' }] }] }), true);
  assert.equal(hasNestedFilterGroups({ or: [{ and: [{ property: 'x', operator: 'eq' }] }] }), true);
  assert.equal(hasNestedFilterGroups({ not: { and: [{ property: 'x', operator: 'eq' }] } }), true);
});

test('flattenFilterToLines handles flat and nested filters', () => {
  assert.deepEqual(flattenFilterToLines(null), []);
  assert.deepEqual(flattenFilterToLines({ property: 'x', operator: 'eq' }), ['x | eq | ""']);
  const flat = { and: [{ property: 'a', operator: 'eq', value: 1 }, { property: 'b', operator: 'contains', value: 'test' }] };
  assert.deepEqual(flattenFilterToLines(flat), ['a | eq | 1', 'b | contains | "test"']);
  const nested = { and: [{ or: [{ property: 'a', operator: 'eq' }, { property: 'b', operator: 'eq' }] }] };
  const lines = flattenFilterToLines(nested);
  assert.equal(lines.length, 1);
  assert.ok(lines[0].startsWith('[nested] '));
});

test('parseFilterLines reconstructs filter from lines', () => {
  const result = parseFilterLines(['status | eq | active', 'count | gt | 5'], 'and');
  assert.deepEqual(result, { and: [{ property: 'status', operator: 'eq', value: 'active' }, { property: 'count', operator: 'gt', value: 5 }] });
  const empty = parseFilterLines([''], 'and');
  assert.equal(empty, null);
  const single = parseFilterLines(['x | eq | 1'], 'or');
  assert.deepEqual(single, { property: 'x', operator: 'eq', value: 1 });
});

test('removeBaseView removes a view and returns new definition', () => {
  const def = makeDefaultBase();
  def.views.push({ id: 'v2', name: 'Card', type: 'card', columns: [], sorts: [], summaries: {}, limit: 100 });
  const result = removeBaseView(def, 'v2');
  assert.equal(result.views.length, 1);
  assert.equal(result.views[0].id, 'table');
  assert.deepEqual(def.views.length, 2, 'original unchanged');
  assert.equal(removeBaseView(def, 'nonexistent'), null);
  // Removing one of two views leaves one, which is valid
  const single = removeBaseView(def, 'table');
  assert.equal(single.views.length, 1);
  assert.equal(single.views[0].id, 'v2');
  // Cannot remove when only one view exists
  const defSingle = makeDefaultBase();
  assert.equal(removeBaseView(defSingle, 'table'), null, 'cannot remove last view');
});

test('reorderBaseView swaps adjacent views', () => {
  const def = makeDefaultBase();
  def.views.push({ id: 'v2', name: 'Card', type: 'card', columns: [], sorts: [], summaries: {}, limit: 100 });
  const up = reorderBaseView(def, 'v2', 'up');
  assert.equal(up.views[0].id, 'v2');
  assert.equal(up.views[1].id, 'table');
  const down = reorderBaseView(up, 'v2', 'down');
  assert.equal(down.views[0].id, 'table');
  assert.equal(down.views[1].id, 'v2');
  // Cannot move first view up or last view down
  assert.equal(reorderBaseView(down, 'table', 'up'), null, 'already first');
  assert.equal(reorderBaseView(down, 'v2', 'down'), null, 'already last');
  // Nonexistent view
  assert.equal(reorderBaseView(def, 'nonexistent', 'up'), null);
});

test('makeViewFromTemplate creates a view with correct type', () => {
  const template = makeDefaultBase().views[0];
  const card = makeViewFromTemplate(template, 'My Cards', 'card');
  assert.equal(card.type, 'card');
  assert.equal(card.name, 'My Cards');
  assert.notEqual(card.id, template.id);
  const list = makeViewFromTemplate(template, 'My List', 'list');
  assert.equal(list.type, 'list');
});

test('VIEW_TYPES includes table, card, and list', () => {
  assert.ok(VIEW_TYPES.includes('table'));
  assert.ok(VIEW_TYPES.includes('card'));
  assert.ok(VIEW_TYPES.includes('list'));
});

test('parseDataviewQuery parses TABLE with fields and WHERE', () => {
  const result = parseDataviewQuery('TABLE name, status FROM "Projects" WHERE status = "active"');
  assert.equal(result.type, 'table');
  assert.equal(result.fields.length, 2);
  assert.equal(result.fields[0].property, 'name');
  assert.equal(result.fields[1].property, 'status');
  assert.equal(result.folder, 'Projects');
  assert.deepEqual(result.filter, { property: 'status', operator: 'eq', value: 'active' });
});

test('parseDataviewQuery parses TABLE WITHOUT ID', () => {
  const result = parseDataviewQuery('TABLE WITHOUT ID file.name AS "Title", priority');
  assert.equal(result.type, 'table');
  assert.equal(result.fields.length, 2);
  assert.equal(result.fields[0].label, 'Title');
  assert.equal(result.fields[0].property, 'file.name');
});

test('parseDataviewQuery parses LIST and TASK', () => {
  const list = parseDataviewQuery('LIST FROM "Notes" WHERE contains(tags, "todo")');
  assert.equal(list.type, 'list');
  assert.equal(list.folder, 'Notes');
  assert.deepEqual(list.filter, { property: 'tags', operator: 'contains', value: 'todo' });
  const task = parseDataviewQuery('TASK WHERE !completed');
  assert.equal(task.type, 'list');
  assert.equal(task.fields[0].property, 'file.name');
});

test('parseDataviewQuery returns null for empty or invalid input', () => {
  assert.equal(parseDataviewQuery(''), null);
  assert.equal(parseDataviewQuery(null), null);
  assert.equal(parseDataviewQuery('random text'), null);
});

test('parseDataviewQuery handles AND/OR filters', () => {
  const result = parseDataviewQuery('TABLE name WHERE status = "active" AND priority > 5');
  assert.ok(result.filter.and, 'filter should have and array');
  assert.equal(result.filter.and.length, 2);
  assert.equal(result.filter.and[0].property, 'status');
  assert.equal(result.filter.and[1].property, 'priority');
  assert.equal(result.filter.and[1].operator, 'gt');
});

test('reorderBaseColumn moves a column to a new index', () => {
  const def = makeDefaultBase();
  def.views[0].columns.push({ property: 'status', label: 'Status' });
  const reordered = reorderBaseColumn(def, 'table', 2, 0);
  assert.equal(reordered.views[0].columns[0].property, 'status');
  assert.equal(reordered.views[0].columns[1].property, 'file.name');
  // Original unchanged
  assert.equal(def.views[0].columns[0].property, 'file.name');
  // Invalid indices
  assert.equal(reorderBaseColumn(def, 'table', -1, 0), null);
  assert.equal(reorderBaseColumn(def, 'table', 0, 5), null);
});
