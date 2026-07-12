import assert from 'node:assert/strict';
import test from 'node:test';

import { formatBaseCell, makeDefaultBase, serializeBase, updateViewSort } from '../../static/js/copal/bases.js';

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
