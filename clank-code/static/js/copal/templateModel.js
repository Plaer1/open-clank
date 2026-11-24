/* Pure first-party Templates model.
 *
 * This module deliberately has no DOM, network, provider, or script runner.
 * Notes and Editor callers can use the returned descriptors to perform an
 * authorized mutation through Files-v1.
 */

const DATE_FORMAT = /^(?:(?:YYYY|YY|MMMM|MMM|MM|M|DD|D|HH|H|mm|m|ss|s)|[-/.:_, T])+$/;
const TOKEN_NAMES = new Set(['title', 'date', 'time', 'datetime']);
const MAX_TEMPLATE_SOURCE = 262144;
const MAX_INSERTION_SELECTIONS = 256;
const MAX_INSERTION_PAYLOAD = 1048576;
const MAX_SELECTION_POSITION = 10000000;
const MAX_DESCRIPTOR_SERIALIZED = 2097152;
const MAX_DIAGNOSTICS = 32;
const MAX_OPAQUE_FIELD = 2048;
const MAX_TEMPLATE_FOLDER_PATH_BYTES = 4096;
const MAX_TEMPLATE_FOLDER_KIND_BYTES = 32;
const MAX_TEMPLATE_FOLDER_PURPOSE_BYTES = 32;
const MAX_TEMPLATE_CAPABILITY_KEY_BYTES = 64;
const TEMPLATE_FOLDER_KINDS = new Set(['folder', 'directory', 'provider_root', 'provider-root']);

function utf8Length(value, limit = Number.POSITIVE_INFINITY) {
  const text = String(value);
  // Every UTF-16 code unit contributes at least one UTF-8 byte (surrogate
  // pairs contribute four bytes), so this rejects oversized input without
  // scanning or allocating for the entire string.
  if (text.length > limit) return limit + 1;
  let bytes = 0;
  for (let index = 0; index < text.length; index += 1) {
    const code = text.charCodeAt(index);
    if (code < 0x80) bytes += 1;
    else if (code < 0x800) bytes += 2;
    else if (code >= 0xd800 && code <= 0xdbff && index + 1 < text.length) {
      const next = text.charCodeAt(index + 1);
      if (next >= 0xdc00 && next <= 0xdfff) {
        bytes += 4;
        index += 1;
      } else bytes += 3;
    } else bytes += 3;
    if (bytes > limit) return bytes;
  }
  return bytes;
}

function boundedOpaque(value, field, { nullable = true } = {}) {
  if (value == null && nullable) return null;
  if (typeof value !== 'string' || !value || utf8Length(value, MAX_OPAQUE_FIELD) > MAX_OPAQUE_FIELD) {
    throw new TypeError(`${field} must be a bounded opaque string`);
  }
  return value;
}

function assertSerializedSize(value, label) {
  let encoded;
  try { encoded = JSON.stringify(value); } catch { throw new TypeError(`${label} is not serializable`); }
  if (utf8Length(encoded) > MAX_DESCRIPTOR_SERIALIZED) throw new TypeError(`${label} exceeds 2 MiB`);
  return encoded;
}

function pad(value, width = 2) {
  return String(value).padStart(width, '0');
}

function dateParts(timestamp, timeZone) {
  const value = timestamp instanceof Date ? timestamp : new Date(timestamp || Date.now());
  if (Number.isNaN(value.getTime())) throw new TypeError('Template timestamp is invalid');
  const formatter = new Intl.DateTimeFormat('en-CA', {
    timeZone: timeZone || undefined, year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
  });
  const parts = Object.fromEntries(formatter.formatToParts(value)
    .filter(({ type }) => type !== 'literal').map(({ type, value: part }) => [type, part]));
  const hour = parts.hour === '24' ? '00' : parts.hour;
  const month = pad(parts.month || 0);
  const day = pad(parts.day || 0);
  const date = `${parts.year}-${month}-${day}`;
  const time = `${hour}:${pad(parts.minute || 0)}`;
  const timeWithSeconds = `${time}:${pad(parts.second || 0)}`;
  const monthLong = new Intl.DateTimeFormat('en-US', { timeZone: timeZone || undefined, month: 'long' }).format(value);
  const monthShort = new Intl.DateTimeFormat('en-US', { timeZone: timeZone || undefined, month: 'short' }).format(value);
  return {
    date, time, datetime: `${date}T${timeWithSeconds}`, timestamp: value.toISOString(), monthLong, monthShort,
    year: Number(parts.year), month: Number(month), day: Number(day),
    hour: Number(hour), minute: Number(parts.minute || 0), second: Number(parts.second || 0),
  };
}

export function formatTemplateDate(format = 'YYYY-MM-DD', timestamp = new Date(), timeZone = undefined) {
  const values = dateParts(timestamp, timeZone);
  const names = {
    YYYY: String(values.year).padStart(4, '0'), YY: String(values.year).slice(-2),
    MMMM: values.monthLong, MMM: values.monthShort,
    MM: pad(values.month), M: String(values.month), DD: pad(values.day), D: String(values.day),
    HH: pad(values.hour), H: String(values.hour), mm: pad(values.minute), m: String(values.minute),
    ss: pad(values.second), s: String(values.second),
  };
  return String(format).replace(/YYYY|YY|MMMM|MMM|MM|M|DD|D|HH|H|mm|m|ss|s/g, token => names[token] ?? token);
}

function validDateFormat(value) {
  return DATE_FORMAT.test(String(value || ''));
}

export function normalizeTemplateFolder(folder, { allowRoot = false } = {}) {
  if (typeof folder !== 'string') throw new TypeError('Template folder must be a string');
  const source = folder.trim();
  if (!source && allowRoot) return '';
  if (!source || utf8Length(source, MAX_TEMPLATE_FOLDER_PATH_BYTES) > MAX_TEMPLATE_FOLDER_PATH_BYTES || source.startsWith('/') || source.includes('\\') || source.includes(':')) {
    throw new TypeError('Template folder must be a relative slash-separated path');
  }
  const parts = source.split('/').filter(Boolean);
  if (!parts.length || parts.some(part => part === '.' || part === '..')) {
    throw new TypeError('Template folder cannot contain traversal segments');
  }
  return parts.join('/');
}

export function isInTemplateFolder(logicalPath, folder) {
  let path;
  let root;
  try {
    path = normalizeTemplateFolder(logicalPath, { allowRoot: true });
    root = normalizeTemplateFolder(folder, { allowRoot: true });
  } catch {
    return false;
  }
  return !root || path === root || path.startsWith(`${root}/`);
}

/** Return a stable, authorized folder identity for S03/S01 handoff. */
export function normalizeTemplateFolderSelection(selection = {}, { purpose = 'insert' } = {}) {
  if (typeof purpose !== 'string' || utf8Length(purpose, MAX_TEMPLATE_FOLDER_PURPOSE_BYTES) > MAX_TEMPLATE_FOLDER_PURPOSE_BYTES) {
    throw new TypeError('Template folder purpose must be a bounded string');
  }
  const resourceRef = selection.resourceRef ?? selection.resource_ref;
  boundedOpaque(resourceRef, 'Template folder resource ref', { nullable: false });
  const rawCapabilities = selection.capabilities || [];
  if (!Array.isArray(rawCapabilities) && (!rawCapabilities || typeof rawCapabilities !== 'object')) {
    throw new TypeError('Template folder capabilities must be a bounded list or object');
  }
  if (Array.isArray(rawCapabilities) && rawCapabilities.length > 64) throw new TypeError('Too many template folder capabilities');
  const capabilityEntries = Array.isArray(rawCapabilities)
    ? rawCapabilities.map(capability => {
      if (typeof capability !== 'string' || utf8Length(capability, MAX_TEMPLATE_CAPABILITY_KEY_BYTES) > MAX_TEMPLATE_CAPABILITY_KEY_BYTES) throw new TypeError('Template capability names must be strings');
      return [capability, true];
    })
    : (() => {
      const entries = [];
      for (const key in rawCapabilities) {
        if (!Object.prototype.hasOwnProperty.call(rawCapabilities, key)) continue;
        if (entries.length >= 64) throw new TypeError('Too many template folder capabilities');
        entries.push([key, rawCapabilities[key]]);
      }
      return entries;
    })();
  if (capabilityEntries.length > 64) throw new TypeError('Too many template folder capabilities');
  const capabilities = Object.fromEntries(capabilityEntries.map(([key, value]) => {
    if (typeof key !== 'string' || utf8Length(key, MAX_TEMPLATE_CAPABILITY_KEY_BYTES) > MAX_TEMPLATE_CAPABILITY_KEY_BYTES || ![true, false, 0, 1, 'true', 'false'].includes(value)) {
      throw new TypeError('Template folder capabilities must be scalar booleans');
    }
    return [key, value];
  }));
  const can = name => capabilities[name] === true || capabilities[name] === 1 || capabilities[name] === 'true';
  if (!(can('read') && can('stat') && can('children'))) {
    throw new TypeError('Selected template folder must allow read, stat, and children discovery');
  }
  if (purpose === 'create' || purpose === 'update') {
    if (!(can('write') || can('edit') || can('create'))) throw new TypeError('Selected template folder is not writable');
  } else if (!['discover', 'insert', 'preview'].includes(purpose)) {
    throw new TypeError(`Unsupported template folder purpose: ${purpose}`);
  }
  const kind = selection.kind ?? 'folder';
  if (typeof kind !== 'string' || utf8Length(kind, MAX_TEMPLATE_FOLDER_KIND_BYTES) > MAX_TEMPLATE_FOLDER_KIND_BYTES || !TEMPLATE_FOLDER_KINDS.has(kind)) {
    throw new TypeError(`Unsupported template folder kind: ${kind}`);
  }
  const scoped = {};
  const scopeValue = (keys, label) => {
    const present = keys.find(key => selection[key] != null);
    if (!present || selection[present] === '') return;
    scoped[label] = boundedOpaque(selection[present], `Template ${label}`);
  };
  // Folder identity is valid only in the scope in which Files-v1 issued it.
  // Keep these optional for old Copal settings, while retaining them whenever
  // the picker supplied account/workspace or policy-generation metadata.
  scopeValue(['accountId', 'account_id', 'accountScope'], 'accountId');
  scopeValue(['workspaceId', 'workspace_id', 'workspaceScope'], 'workspaceId');
  for (const [label, keys] of [['generation', ['generation']], ['policyGeneration', ['policyGeneration', 'policy_generation']]]) {
    const present = keys.find(key => selection[key] != null);
    if (!present) continue;
    const value = Number(selection[present]);
    if (!Number.isSafeInteger(value) || value < 0) throw new TypeError(`Template ${label} must be a non-negative integer`);
    scoped[label] = value;
  }
  return {
    resourceRef,
    resourceKey: boundedOpaque(selection.resourceKey ?? selection.resource_key, 'Template resource key'),
    revision: selection.revision == null ? null : validateRevision(selection.revision),
    provider: selection.provider == null ? null : boundedOpaque(selection.provider, 'Template provider'),
    kind,
    logicalPath: normalizeTemplateFolder(selection.logicalPath ?? selection.logical_path ?? '', { allowRoot: true }),
    capabilities,
    ...scoped,
  };
}

function validateRevision(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new TypeError('Template revision must be a typed object');
  const keys = Object.keys(value);
  if (keys.some(key => !['kind', 'value'].includes(key)) || typeof value.kind !== 'string' || typeof value.value !== 'string' || utf8Length(value.kind, MAX_OPAQUE_FIELD) > MAX_OPAQUE_FIELD || utf8Length(value.value, MAX_OPAQUE_FIELD) > MAX_OPAQUE_FIELD) {
    throw new TypeError('Template revision must contain only kind and value strings');
  }
  return { kind: value.kind, value: value.value };
}

function diagnostic(diagnostics, message, offset = undefined) {
  if (diagnostics.length >= MAX_DIAGNOSTICS) return;
  const value = offset == null ? message : { message, offset };
  if (!diagnostics.some(item => JSON.stringify(item) === JSON.stringify(value))) diagnostics.push(value);
}

/** Expand only core Templates variables; all unsupported source remains intact. */
export function expandTemplate(source, { title = '', now = new Date(), timeZone = undefined } = {}) {
  const input = String(source ?? '');
  if (utf8Length(input) > MAX_TEMPLATE_SOURCE) throw new TypeError('Template source exceeds 256 KiB');
  const parts = dateParts(now, timeZone);
  const values = { title: String(title ?? ''), ...parts };
  const diagnostics = [];
  const text = input.replace(/\{\{\s*([^}]+?)\s*\}\}/g, (whole, expression) => {
    const separator = String(expression).indexOf(':');
    const rawName = separator < 0 ? expression : String(expression).slice(0, separator);
    const rawFormat = separator < 0 ? null : String(expression).slice(separator + 1);
    const name = String(rawName).trim().toLowerCase();
    if (!TOKEN_NAMES.has(name)) {
      diagnostic(diagnostics, `Unsupported template variable: {{${String(rawName).trim()}${rawFormat == null ? '' : `:${rawFormat}`}}}`);
      return whole;
    }
    if (rawFormat != null) {
      const format = String(rawFormat).trim();
      if (!validDateFormat(format)) {
        diagnostic(diagnostics, `Unsupported template date format: {{${name}:${format}}}`);
        return whole;
      }
      return formatTemplateDate(format, now, timeZone);
    }
    return values[name] ?? whole;
  });
  if (/<%[\s\S]*?%>/.test(text)) diagnostic(diagnostics, 'Executable template expressions are unsupported and were left unchanged.');
  return { text, diagnostics, timestamp: parts.timestamp };
}

export function expandTemplateVariables(source, options = {}) {
  return expandTemplate(source, options).text;
}

/** Expand one insertion for every captured selection in deterministic order. */
export function createInsertionDescriptors(source, selections = [], options = {}) {
  const input = String(source ?? '');
  if (utf8Length(input) > MAX_TEMPLATE_SOURCE) throw new TypeError('Template source exceeds 256 KiB');
  if (!Array.isArray(selections) || selections.length > MAX_INSERTION_SELECTIONS) throw new TypeError('Too many template selections');
  const ordered = selections.map((selection, index) => ({
    selection,
    index,
    from: Number(selection?.from ?? selection?.anchor ?? 0),
    to: Number(selection?.to ?? selection?.head ?? selection?.from ?? selection?.anchor ?? 0),
  })).map(item => {
    if (!item.selection || typeof item.selection !== 'object') throw new TypeError('Template selections must be objects');
    if (!Number.isFinite(item.from) || !Number.isFinite(item.to) || !Number.isInteger(item.from) || !Number.isInteger(item.to)) {
      throw new TypeError('Template selections require finite integer positions');
    }
    if (item.from < 0 || item.to < 0 || item.from > MAX_SELECTION_POSITION || item.to > MAX_SELECTION_POSITION) {
      throw new TypeError('Template selection is outside the source bounds');
    }
    const from = Math.min(item.from, item.to);
    const to = Math.max(item.from, item.to);
    return { ...item, from, to };
  })
    .sort((a, b) => a.from - b.from || a.to - b.to || a.index - b.index);
  for (let index = 1; index < ordered.length; index += 1) {
    if (ordered[index].from < ordered[index - 1].to || (ordered[index].from === ordered[index - 1].from && ordered[index].to === ordered[index - 1].to)) {
      throw new TypeError('Template selections must be distinct and non-overlapping');
    }
  }
  const expanded = expandTemplate(input, options);
  if (utf8Length(expanded.text) * ordered.length > MAX_INSERTION_PAYLOAD) throw new TypeError('Template insertion payload exceeds 1 MiB');
  return ordered.map((selection, descriptorIndex) => ({
    descriptorVersion: 1,
    descriptorIndex,
    selectionIndex: selection.index,
    from: selection.from,
    to: selection.to,
    text: expanded.text,
    diagnostics: [...expanded.diagnostics],
  }));
}

export const makeInsertionDescriptors = createInsertionDescriptors;

function normalizeDiagnostic(item) {
  if (typeof item === 'string') {
    if (item.length > 2048) throw new TypeError('Template diagnostic is too long');
    return item;
  }
  if (!item || typeof item !== 'object' || Array.isArray(item) || typeof item.message !== 'string' || item.message.length > 2048) {
    throw new TypeError('Template diagnostics must contain a message');
  }
  if (Object.keys(item).some(key => !['message', 'offset'].includes(key))) throw new TypeError('Template diagnostic contains unsupported fields');
  if (item.offset != null && (!Number.isInteger(item.offset) || item.offset < 0 || item.offset > MAX_SELECTION_POSITION)) {
    throw new TypeError('Template diagnostic offset is invalid');
  }
  return item.offset == null ? { message: item.message } : { message: item.message, offset: item.offset };
}

function normalizeInsertionDescriptors(descriptors) {
  if (!Array.isArray(descriptors) || descriptors.length > MAX_INSERTION_SELECTIONS) throw new TypeError('Too many insertion descriptors');
  const normalized = descriptors.map((item, index) => {
    if (!item || typeof item !== 'object' || Array.isArray(item) || item.descriptorVersion !== 1) throw new TypeError('Unsupported insertion descriptor version');
    for (const field of ['descriptorIndex', 'selectionIndex', 'from', 'to']) {
      if (!Number.isInteger(item[field]) || item[field] < 0 || item[field] > MAX_SELECTION_POSITION) throw new TypeError(`Invalid descriptor ${field}`);
    }
    if (item.descriptorIndex !== index || typeof item.text !== 'string' || utf8Length(item.text) > MAX_TEMPLATE_SOURCE) throw new TypeError('Invalid insertion descriptor payload');
    if (item.from > item.to) throw new TypeError('Insertion descriptor range is inverted');
    if (!Array.isArray(item.diagnostics) || item.diagnostics.length > MAX_DIAGNOSTICS) throw new TypeError('Invalid insertion descriptor diagnostics');
    return {
      descriptorVersion: 1,
      descriptorIndex: item.descriptorIndex,
      selectionIndex: item.selectionIndex,
      from: item.from,
      to: item.to,
      text: item.text,
      diagnostics: item.diagnostics.map(normalizeDiagnostic),
    };
  });
  const selectionIndexes = new Set();
  for (let index = 0; index < normalized.length; index += 1) {
    const item = normalized[index];
    if (selectionIndexes.has(item.selectionIndex)) throw new TypeError('Insertion descriptor selection indexes must be unique');
    selectionIndexes.add(item.selectionIndex);
    if (index > 0) {
      const previous = normalized[index - 1];
      if (item.from < previous.to || (item.from === previous.from && item.to === previous.to)) throw new TypeError('Insertion descriptor ranges overlap or are duplicated');
      if (item.from < previous.from || (item.from === previous.from && item.to < previous.to)) throw new TypeError('Insertion descriptor ranges are out of order');
    }
  }
  const payload = normalized.reduce((total, item) => total + utf8Length(item.text), 0);
  if (payload > MAX_INSERTION_PAYLOAD) throw new TypeError('Template insertion payload exceeds 1 MiB');
  return normalized;
}

export function serializeInsertionDescriptors(descriptors = []) {
  return assertSerializedSize({ version: 1, descriptors: normalizeInsertionDescriptors(descriptors) }, 'Insertion descriptors');
}

export function deserializeInsertionDescriptors(serialized) {
  if (typeof serialized === 'string' && utf8Length(serialized) > MAX_DESCRIPTOR_SERIALIZED) throw new TypeError('Insertion descriptors exceed 2 MiB');
  let value;
  try { value = typeof serialized === 'string' ? JSON.parse(serialized) : serialized; } catch { throw new TypeError('Insertion descriptors are not valid JSON'); }
  if (!value || value.version !== 1 || !Array.isArray(value.descriptors)) throw new TypeError('Unsupported insertion descriptor version');
  return normalizeInsertionDescriptors(value.descriptors);
}

function stableDescriptor(item = {}) {
  if (!item || typeof item !== 'object' || Array.isArray(item)) throw new TypeError('Template descriptors must be objects');
  const descriptor = {};
  for (const key of ['itemId', 'item_id', 'resourceRef', 'resource_ref', 'resourceKey', 'resource_key', 'kind', 'name', 'target']) {
    if (item[key] == null) continue;
    const value = boundedOpaque(item[key], `Template descriptor ${key}`);
    // S01 owns opaque refs and attachment receipts. A URL, absolute path, or
    // host separator in a descriptor would turn this pure handoff into an
    // authority bypass, so reject it before it can be serialized.
    if ((key === 'target' && /^(?:[a-z][a-z0-9+.-]*:|[\\/])/iu.test(value))
      || ['itemId', 'item_id', 'resourceRef', 'resource_ref', 'resourceKey', 'resource_key'].includes(key)
        && /^(?:[a-z][a-z0-9+.-]*:\/\/|(?:javascript|data|vbscript):)/iu.test(value)) {
      throw new TypeError(`Template descriptor ${key} must be an opaque relative target`);
    }
    descriptor[key] = value;
  }
  if (item.revision != null) descriptor.revision = validateRevision(item.revision);
  return descriptor;
}

/** Serialize S01-owned asset/link descriptors without inventing provider URLs. */
export function serializeTemplateDescriptors({ assets = [], links = [] } = {}) {
  if (!Array.isArray(assets) || !Array.isArray(links) || assets.length > MAX_INSERTION_SELECTIONS || links.length > MAX_INSERTION_SELECTIONS) throw new TypeError('Too many template descriptors');
  return assertSerializedSize({ version: 1, assets: assets.map(stableDescriptor), links: links.map(stableDescriptor) }, 'Template descriptors');
}

export function deserializeTemplateDescriptors(serialized) {
  if (typeof serialized === 'string' && utf8Length(serialized) > MAX_DESCRIPTOR_SERIALIZED) throw new TypeError('Template descriptors exceed 2 MiB');
  let value;
  try { value = typeof serialized === 'string' ? JSON.parse(serialized) : serialized; } catch { throw new TypeError('Template descriptors are not valid JSON'); }
  if (!value || value.version !== 1 || !Array.isArray(value.assets) || !Array.isArray(value.links)) {
    throw new TypeError('Unsupported template descriptor version');
  }
  if (value.assets.length > MAX_INSERTION_SELECTIONS || value.links.length > MAX_INSERTION_SELECTIONS) throw new TypeError('Too many template descriptors');
  return { version: 1, assets: value.assets.map(stableDescriptor), links: value.links.map(stableDescriptor) };
}

export function serializeTemplateHandoff(folder, { operationId = null, destinationRef = null, purpose = 'insert' } = {}) {
  const selected = normalizeTemplateFolderSelection(folder, { purpose });
  return assertSerializedSize({ version: 1, operationId: boundedOpaque(operationId, 'Template operation id'), destinationRef: boundedOpaque(destinationRef, 'Template destination ref'), folder: selected }, 'Template handoff');
}

export function deserializeTemplateHandoff(serialized, { purpose = 'insert' } = {}) {
  if (typeof serialized === 'string' && utf8Length(serialized) > MAX_DESCRIPTOR_SERIALIZED) throw new TypeError('Template handoff exceeds 2 MiB');
  let value;
  try { value = typeof serialized === 'string' ? JSON.parse(serialized) : serialized; } catch { throw new TypeError('Template handoff is not valid JSON'); }
  if (!value || value.version !== 1 || !value.folder || typeof value.folder !== 'object') throw new TypeError('Unsupported template handoff version');
  return {
    version: 1,
    operationId: boundedOpaque(value.operationId, 'Template operation id'),
    destinationRef: boundedOpaque(value.destinationRef, 'Template destination ref'),
    folder: normalizeTemplateFolderSelection(value.folder, { purpose }),
  };
}
