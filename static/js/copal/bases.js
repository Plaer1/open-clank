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

// --- Filter tree helpers ---

export function hasNestedFilterGroups(rule) {
  if (!rule) return false;
  if (rule.and) return rule.and.some((child) => child?.and || child?.or || child?.not);
  if (rule.or) return rule.or.some((child) => child?.and || child?.or || child?.not);
  if (rule.not) {
    const inner = rule.not;
    if (inner?.and || inner?.or || inner?.not) return true;
    return false;
  }
  return false;
}

export function flattenFilterToLines(rule) {
  if (!rule) return [];
  if (rule.and) {
    return rule.and.flatMap((child) => {
      if (child?.and || child?.or || child?.not) return [`[nested] ${JSON.stringify(child)}`];
      return child?.property ? [`${child.property} | ${child.operator} | ${JSON.stringify(child.value ?? '')}`] : [];
    });
  }
  if (rule.or) {
    return rule.or.flatMap((child) => {
      if (child?.and || child?.or || child?.not) return [`[nested] ${JSON.stringify(child)}`];
      return child?.property ? [`${child.property} | ${child.operator} | ${JSON.stringify(child.value ?? '')}`] : [];
    });
  }
  if (rule.not) return [`[not] ${JSON.stringify(rule.not)}`];
  return rule.property ? [`${rule.property} | ${rule.operator} | ${JSON.stringify(rule.value ?? '')}`] : [];
}

export function parseFilterLines(lines, mode = 'and') {
  const rules = [];
  for (const line of lines) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    if (trimmed.startsWith('[nested] ') || trimmed.startsWith('[not] ')) {
      try { rules.push(JSON.parse(trimmed.replace(/^\[[^\]]+\]\s*/, ''))); } catch { /* skip invalid */ }
      continue;
    }
    const idx1 = trimmed.indexOf('|');
    if (idx1 < 0) continue;
    const property = trimmed.slice(0, idx1).trim();
    const rest = trimmed.slice(idx1 + 1);
    const idx2 = rest.indexOf('|');
    const operator = (idx2 < 0 ? rest : rest.slice(0, idx2)).trim() || 'eq';
    const rawValue = idx2 < 0 ? '' : rest.slice(idx2 + 1).trim();
    const rule = { property, operator };
    if (!['exists', 'missing'].includes(operator)) {
      try { rule.value = JSON.parse(rawValue); } catch { rule.value = rawValue || ''; }
    }
    rules.push(rule);
  }
  if (rules.length === 0) return null;
  if (rules.length === 1) return rules[0];
  return { [mode]: rules };
}

// --- View management helpers ---

export function removeBaseView(definition, viewId) {
  const next = clone(definition);
  const idx = next.views.findIndex((item) => item.id === viewId);
  if (idx < 0 || next.views.length <= 1) return null;
  next.views.splice(idx, 1);
  return next;
}

export function reorderBaseView(definition, viewId, direction) {
  const next = clone(definition);
  const idx = next.views.findIndex((item) => item.id === viewId);
  if (idx < 0) return null;
  const target = direction === 'up' ? idx - 1 : idx + 1;
  if (target < 0 || target >= next.views.length) return null;
  [next.views[idx], next.views[target]] = [next.views[target], next.views[idx]];
  return next;
}

export function makeViewFromTemplate(template, name, type = 'table') {
  const view = clone(template);
  view.id = `${name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '') || 'view'}-${Date.now().toString(36)}`;
  view.name = name;
  view.type = type;
  return view;
}

export const VIEW_TYPES = ['table', 'card', 'list'];

// --- Dataview query parser ---

function parseDataviewWhere(expr) {
  const trimmed = expr.trim();
  const andParts = [];
  const orParts = [];
  // Split on AND/OR (case-insensitive, whole word)
  const orSplit = trimmed.split(/\s+OR\s+/i);
  if (orSplit.length > 1) {
    for (const part of orSplit) {
      const andSplit = part.split(/\s+AND\s+/i);
      for (const a of andSplit) orParts.push(parseDataviewCondition(a.trim()));
    }
    return orParts.length === 1 ? orParts[0] : { or: orParts };
  }
  const andSplit = trimmed.split(/\s+AND\s+/i);
  for (const part of andSplit) {
    const cond = parseDataviewCondition(part.trim());
    if (cond) andParts.push(cond);
  }
  if (andParts.length === 0) return null;
  if (andParts.length === 1) return andParts[0];
  return { and: andParts };
}

function parseDataviewCondition(expr) {
  if (!expr) return null;
  // Function-call style: contains(prop, "val"), startswith(prop, "val")
  let m = expr.match(/^(contains|does\s+not\s+contain|startswith|endswith|in)\s*\(\s*(\S+)\s*,\s*"?([^"]*)"?/i);
  if (m) {
    const opMap = { contains: 'contains', 'does not contain': 'not_contains', startswith: 'starts_with', endswith: 'ends_with', in: 'in' };
    return { property: m[2].replace(/^file\./, ''), operator: opMap[m[1].toLowerCase()] || 'contains', value: m[3].trim() };
  }
  // Negation: !prop
  m = expr.match(/^!(\S+)$/);
  if (m) return { property: m[1].replace(/^file\./, ''), operator: 'missing' };
  // Property comparisons: prop = "val", prop != "val", prop >, <, >=, <=
  m = expr.match(/^(\S+)\s*(!=|>=|<=|=>|=|>|<)\s*"?([^"]*)"?\s*$/i);
  if (m) {
    const prop = m[1].replace(/^file\./, '');
    const opMap = { '=': 'eq', '!=': 'ne', '>': 'gt', '>=': 'gte', '<': 'lt', '<=': 'lte', '=>': 'gte' };
    return { property: prop, operator: opMap[m[2].toLowerCase()] || 'eq', value: m[3].trim() };
  }
  // file.hasProperty("prop")
  m = expr.match(/file\.hasProperty\s*\(\s*"?([^"]+)"?\s*\)/i);
  if (m) return { property: m[1], operator: 'exists' };
  return null;
}

export function parseDataviewQuery(query) {
  const text = (query || '').trim();
  if (!text) return null;
  // TABLE [WITHOUT ID] field1, field2, ... FROM "folder" WHERE ...
  const tableMatch = text.match(/^TABLE\s+(?:WITHOUT\s+ID\s+)?(.+?)(?:\s+FROM\s+"([^"]*)")?(?:\s+WHERE\s+(.+))?$/is);
  if (tableMatch) {
    const fields = tableMatch[1].split(',').map((f) => {
      const trimmed = f.trim();
      const asMatch = trimmed.match(/^(.+?)\s+AS\s+"(.+)"$/i);
      if (asMatch) return { property: asMatch[1].trim(), label: asMatch[2] };
      return { property: trimmed, label: trimmed };
    });
    const filter = tableMatch[3] ? parseDataviewWhere(tableMatch[3]) : null;
    return { type: 'table', fields, folder: tableMatch[2] || null, filter };
  }
  // LIST FROM "folder" WHERE ...
  const listMatch = text.match(/^LIST\s+(?:FROM\s+"([^"]*)"\s*)?(?:WHERE\s+(.+))?$/is);
  if (listMatch) {
    return { type: 'list', fields: [{ property: 'file.name', label: 'Name' }], folder: listMatch[1] || null, filter: listMatch[2] ? parseDataviewWhere(listMatch[2]) : null };
  }
  // TASK FROM "folder" WHERE ...
  const taskMatch = text.match(/^TASK\s+(?:FROM\s+"([^"]*)"\s*)?(?:WHERE\s+(.+))?$/is);
  if (taskMatch) {
    return { type: 'list', fields: [{ property: 'file.name', label: 'Task' }], folder: taskMatch[1] || null, filter: taskMatch[2] ? parseDataviewWhere(taskMatch[2]) : null };
  }
  return null;
}

// --- Column reorder ---

export function reorderBaseColumn(definition, viewId, fromIndex, toIndex) {
  const next = clone(definition);
  const view = next.views.find((item) => item.id === viewId) || next.views[0];
  if (fromIndex < 0 || fromIndex >= view.columns.length) return null;
  if (toIndex < 0 || toIndex >= view.columns.length) return null;
  const [col] = view.columns.splice(fromIndex, 1);
  view.columns.splice(toIndex, 0, col);
  return next;
}
