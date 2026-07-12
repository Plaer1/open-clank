export function makeDefaultBase(name = 'New Base') {
  return {
    version: 1,
    views: [{
      id: 'table', name: 'Table', type: 'table',
      columns: [
        { property: 'file.name', label: 'Name' },
        { property: 'tags', label: 'Tags' },
      ],
      filters: null, sorts: [], groupBy: null, summaries: {}, limit: 1000,
      extensions: {},
    }],
    extensions: { title: name },
  };
}

function clone(value) {
  return typeof structuredClone === 'function'
    ? structuredClone(value)
    : JSON.parse(JSON.stringify(value));
}

export function updateViewSort(definition, viewId, property, additive = false) {
  const next = clone(definition);
  const view = next.views.find((item) => item.id === viewId) || next.views[0];
  const existing = view.sorts.find((item) => item.property === property);
  let direction = 'asc';
  if (existing?.direction === 'asc') direction = 'desc';
  else if (existing?.direction === 'desc') direction = null;
  const retained = (view.sorts || []).filter((item) => item.property !== property);
  view.sorts = additive ? retained : [];
  if (direction) view.sorts.push({ property, direction });
  return next;
}

export function serializeBase(definition) {
  return `${JSON.stringify(definition, null, 2)}\n`;
}

export function formatBaseCell(value) {
  if (value == null || value === '') return '—';
  if (value === true) return 'Yes';
  if (value === false) return 'No';
  if (Array.isArray(value)) return value.join(', ');
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value);
}
