// TableTypes — typed column values for readable Markdown tables.
// Locale affects display only; saved meaning stays ISO dates and explicit
// currency code/value pairs.

export const COLUMN_TYPES = Object.freeze(['text', 'number', 'date', 'currency']);

export const TYPE_FORMATS = Object.freeze({
  text: Object.freeze(['auto']),
  number: Object.freeze(['auto', 'locale']),
  date: Object.freeze(['iso', 'locale']),
  currency: Object.freeze(['code', 'locale']),
});

export function normalizeColumnType(type) {
  const kind = String(type || '').trim().toLowerCase();
  return COLUMN_TYPES.includes(kind) ? kind : '';
}

export function normalizeTypeFormat(type, format) {
  const kind = normalizeColumnType(type) || 'text';
  const wanted = String(format || '').trim().toLowerCase();
  const allowed = TYPE_FORMATS[kind];
  return allowed.includes(wanted) ? wanted : allowed[0];
}

export function isBlankCell(raw) {
  return String(raw ?? '').trim() === '';
}

const NUMBER_RE = /^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$/;
const ISO_DATE_RE = /^(\d{4})-(\d{2})-(\d{2})$/;
// Explicit code/value money: `USD 12.50` (code first, space, then number).
const CURRENCY_RE = /^([A-Za-z]{3})\s+([+-]?(?:\d+\.?\d*|\.\d+))$/;

export function parseNumberCell(raw) {
  const text = String(raw ?? '').trim();
  if (!NUMBER_RE.test(text)) return null;
  const value = Number(text);
  return Number.isFinite(value) ? value : null;
}

export function isIsoCalendarDate(year, month, day) {
  if (month < 1 || month > 12 || day < 1 || day > 31) return false;
  const probe = new Date(Date.UTC(year, month - 1, day));
  return probe.getUTCFullYear() === year
    && probe.getUTCMonth() === month - 1
    && probe.getUTCDate() === day;
}

export function parseDateCell(raw) {
  const text = String(raw ?? '').trim();
  const match = ISO_DATE_RE.exec(text);
  if (!match) return null;
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  if (!isIsoCalendarDate(year, month, day)) return null;
  return text;
}

export function parseCurrencyCell(raw) {
  const text = String(raw ?? '').trim();
  const match = CURRENCY_RE.exec(text);
  if (!match) return null;
  const code = match[1].toUpperCase();
  const value = Number(match[2]);
  if (!Number.isFinite(value)) return null;
  return { code, value, source: `${code} ${match[2]}` };
}

// parseTypedCell — classify one raw cell under a column type.
// status: 'blank' | 'valid' | 'invalid'. Invalids stay in the source as written.
export function parseTypedCell(raw, type) {
  const text = String(raw ?? '');
  const kind = normalizeColumnType(type);
  if (!kind || kind === 'text') {
    return isBlankCell(text)
      ? { status: 'blank', text: text.trim() }
      : { status: 'valid', text: text.trim(), sortText: text.trim() };
  }
  if (isBlankCell(text)) return { status: 'blank', text: '' };
  if (kind === 'number') {
    const value = parseNumberCell(text);
    if (value == null) return { status: 'invalid', text: text.trim() };
    return { status: 'valid', text: text.trim(), number: value };
  }
  if (kind === 'date') {
    const iso = parseDateCell(text);
    if (!iso) return { status: 'invalid', text: text.trim() };
    return { status: 'valid', text: iso, date: iso };
  }
  const money = parseCurrencyCell(text);
  if (!money) return { status: 'invalid', text: text.trim() };
  return { status: 'valid', text: text.trim(), number: money.value, currency: money };
}

function localeTag(locale) {
  if (typeof locale === 'string' && locale.trim()) return locale.trim();
  if (typeof navigator !== 'undefined' && navigator.language) return navigator.language;
  return undefined;
}

// formatTypedCell — locale-aware display only; the source string is unchanged.
export function formatTypedCell(raw, type, format = 'auto', locale) {
  const kind = normalizeColumnType(type) || 'text';
  const fmt = normalizeTypeFormat(kind, format);
  const parsed = parseTypedCell(raw, kind);
  if (parsed.status === 'blank') return '';
  if (parsed.status === 'invalid') return parsed.text;
  if (kind === 'date') {
    if (fmt === 'iso') return parsed.date;
    const tag = localeTag(locale);
    const value = new Date(`${parsed.date}T00:00:00Z`);
    try {
      return new Intl.DateTimeFormat(tag, { year: 'numeric', month: 'numeric', day: 'numeric', timeZone: 'UTC' }).format(value);
    } catch (_) {
      return parsed.date;
    }
  }
  if (kind === 'currency') {
    const money = parsed.currency;
    if (fmt === 'code') return `${money.code} ${trimMoney(money.value)}`;
    const tag = localeTag(locale);
    try {
      return new Intl.NumberFormat(tag, { style: 'currency', currency: money.code }).format(money.value);
    } catch (_) {
      return `${money.code} ${trimMoney(money.value)}`;
    }
  }
  if (kind === 'number') {
    if (fmt !== 'locale') return parsed.text;
    const tag = localeTag(locale);
    try {
      return new Intl.NumberFormat(tag).format(parsed.number);
    } catch (_) {
      return parsed.text;
    }
  }
  return parsed.text;
}

function trimMoney(value) {
  if (Number.isInteger(value)) return String(value);
  return String(Number(value.toFixed(2)));
}

const STATUS_RANK = { valid: 0, invalid: 1, blank: 2 };

function compareStatus(a, b) {
  return STATUS_RANK[a.status] - STATUS_RANK[b.status];
}

function compareNumbers(a, b) {
  return a - b;
}

function compareStrings(a, b) {
  return a < b ? -1 : a > b ? 1 : 0;
}

// compareTyped — stable typed ordering. Blanks and invalids sink below valid
// values in both directions so a sort never invents meaning for them.
// Untyped columns keep the historical number-or-text behaviour; ambiguous
// dates stay text until a column type is selected.
export function compareTyped(aRaw, bRaw, type) {
  const kind = normalizeColumnType(type);
  const a = parseTypedCell(aRaw, kind || 'text');
  const b = parseTypedCell(bRaw, kind || 'text');
  const byStatus = compareStatus(a, b);
  if (byStatus !== 0) return byStatus;
  if (a.status !== 'valid') return 0;
  if (!kind) {
    const na = Number(a.text);
    const nb = Number(b.text);
    const aNum = a.text !== '' && !Number.isNaN(na);
    const bNum = b.text !== '' && !Number.isNaN(nb);
    if (aNum && bNum) return compareNumbers(na, nb);
    return compareStrings(a.sortText ?? a.text, b.sortText ?? b.text);
  }
  if (kind === 'number') return compareNumbers(a.number, b.number);
  if (kind === 'date') return compareStrings(a.date, b.date);
  if (kind === 'currency') {
    const byCode = compareStrings(a.currency.code, b.currency.code);
    return byCode !== 0 ? byCode : compareNumbers(a.currency.value, b.currency.value);
  }
  return compareStrings(a.sortText ?? a.text, b.sortText ?? b.text);
}

// sortBodyRows — stable typed sort of body rows by one column.
// direction: 'asc' | 'desc'. Ties keep source order; blanks/invalids stay last.
export function sortBodyRows(rows, col, type, direction = 'asc') {
  const body = rows.slice(1).map((row, index) => ({ row, index }));
  const sign = direction === 'desc' ? -1 : 1;
  body.sort((a, b) => {
    const typed = compareTyped(a.row.cells[col] || '', b.row.cells[col] || '', type);
    if (typed !== 0) {
      const aRank = STATUS_RANK[parseTypedCell(a.row.cells[col] || '', type || 'text').status];
      const bRank = STATUS_RANK[parseTypedCell(b.row.cells[col] || '', type || 'text').status];
      if (aRank !== bRank) return aRank - bRank;
      return typed * sign;
    }
    return a.index - b.index;
  });
  return [rows[0], ...body.map((entry) => entry.row)];
}
