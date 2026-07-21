export const MIN_DAY_WIDTH = 8;
export const DEFAULT_DAY_WIDTH = 18;
export const MAX_DAY_WIDTH = 56;
export const MIN_LANE_HEIGHT = 32;
export const MAX_LANE_HEIGHT = 160;
export const RANGE_CHUNK_DAYS = 60;
export const WARM_HISTORY_DAYS = 60;
export const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;
export const ICONS = { cat:'🐱', dog:'🐶', truck:'🚚', car:'🚗', bug:'🐛', sun:'☀️', fence:'🚧', ant:'🐜', toad:'🐸', hammock:'🛶', bucket:'🪣', broom:'🧹', box:'📦', clown:'🤡' };

export function parseLocalDate(value) {
  if (!DATE_RE.test(String(value || ''))) return null;
  const [year, month, day] = String(value).split('-').map(Number);
  const date = new Date(year, month - 1, day);
  return date.getFullYear() === year && date.getMonth() === month - 1 && date.getDate() === day ? date : null;
}

export function formatLocalDate(value) {
  const date = value instanceof Date ? value : parseLocalDate(value);
  if (!date) return '';
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
}

export function addDays(value, amount) {
  const date = value instanceof Date ? new Date(value) : parseLocalDate(value);
  if (!date) return null;
  date.setDate(date.getDate() + amount);
  return date;
}

export function daysBetween(from, to) {
  const a = from instanceof Date ? from : parseLocalDate(from);
  const b = to instanceof Date ? to : parseLocalDate(to);
  if (!a || !b) return 0;
  return Math.round((Date.UTC(b.getFullYear(), b.getMonth(), b.getDate()) - Date.UTC(a.getFullYear(), a.getMonth(), a.getDate())) / 86400000);
}

export function glyphFor(icon) {
  const value = String(icon || '').trim();
  return ICONS[value] || value || '•';
}

export function eventLayout(event, fallbackStart, autoStart = null) {
  let start = parseLocalDate(event.startDate);
  if (event.startDate === 'FUZZY') start = parseLocalDate(event.fuzzy?.anchorStart);
  if (event.startDate === 'AUTO') start = autoStart;
  start ||= parseLocalDate(event.fuzzy?.anchorStart) || fallbackStart;
  let end = parseLocalDate(event.dueDate) || parseLocalDate(event.fuzzy?.anchorEnd);
  end ||= start;
  return { start, end };
}

export function manipulationGate(event, track) {
  const fuzzy = event.startDate === 'FUZZY' || !!event.fuzzy?.anchorEnd || !!event.fuzzy?.whiskerStart;
  const hardStart = DATE_RE.test(String(event.startDate || ''));
  const hammock = !!track?.special;
  return {
    movable: hardStart && !fuzzy && !hammock,
    resizeLeft: hardStart && !fuzzy && !hammock,
    resizeRight: hardStart && DATE_RE.test(String(event.dueDate || '')) && !fuzzy && !hammock,
    reason: hammock ? 'Hammock events are edited in the event popup.' : fuzzy ? 'Fuzzy/whisker events are edited in the event popup.' : !hardStart ? 'Choose an exact start date in the event popup.' : '',
  };
}

export function dragPatch(event, mode, deltaDays) {
  const start = parseLocalDate(event.startDate);
  if (!start) return {};
  const due = parseLocalDate(event.dueDate);
  if (mode === 'move') return { startDate: formatLocalDate(addDays(start, deltaDays)), ...(due ? { dueDate: formatLocalDate(addDays(due, deltaDays)) } : {}) };
  if (mode === 'left') {
    const next = addDays(start, deltaDays);
    return due && next > due ? { startDate: formatLocalDate(due) } : { startDate: formatLocalDate(next) };
  }
  if (mode === 'right' && due) {
    const next = addDays(due, deltaDays);
    return { dueDate: formatLocalDate(next < start ? start : next) };
  }
  return {};
}

export const MAX_RECURRENCE_EXPANSION = 500;

function monthEnd(year, month) {
  return new Date(year, month + 1, 0).getDate();
}

function nextRecurrenceDate(current, frequency, interval) {
  const d = new Date(current);
  if (frequency === 'daily') { d.setDate(d.getDate() + interval); }
  else if (frequency === 'weekly') { d.setDate(d.getDate() + interval * 7); }
  else if (frequency === 'monthly') {
    let year = d.getFullYear() + Math.floor((d.getMonth() + interval) / 12);
    let month = (d.getMonth() + interval) % 12;
    d.setDate(Math.min(d.getDate(), monthEnd(year, month)));
    d.setFullYear(year);
    d.setMonth(month);
  }
  return d;
}

export function expandRecurrence(event, windowStart, windowEnd) {
  const rec = event.recurrence;
  if (!rec) return [];
  const seriesStart = parseLocalDate(event.startDate);
  if (!seriesStart) return [];
  const durationDays = event.dueDate ? daysBetween(event.startDate, event.dueDate) : 0;
  const exceptions = new Set(rec.exceptionDates || []);
  const occurrences = [];
  let current = new Date(seriesStart);
  let count = 0;
  const until = rec.until ? parseLocalDate(rec.until) : null;
  while (occurrences.length < MAX_RECURRENCE_EXPANSION) {
    if (rec.count != null && count >= rec.count) break;
    if (until && current > until) break;
    if (current >= windowStart && current <= windowEnd) {
      const key = formatLocalDate(current);
      if (!exceptions.has(key)) {
        const due = durationDays ? addDays(current, durationDays) : null;
        occurrences.push({
          eventId: event.id,
          occurrenceKey: key,
          startDate: key,
          dueDate: due ? formatLocalDate(due) : null,
          allDay: !!rec.allDay,
        });
      }
    }
    current = nextRecurrenceDate(current, rec.frequency || 'weekly', rec.interval || 1);
    count++;
    if (current > windowEnd && (rec.count != null || until)) break;
  }
  return occurrences;
}
