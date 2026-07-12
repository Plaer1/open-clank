// Date helpers shared across the app.
// All dates in the data model are YYYY-MM-DD strings at local midnight.

/** Parse a YYYY-MM-DD string into a Date at local midnight. */
export function parseDate(s: string): Date {
  const [y, m, d] = s.split('-').map(Number);
  return new Date(y, m - 1, d);
}

/** Format a Date as YYYY-MM-DD. */
export function fmtDate(d: Date): string {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  return `${y}-${m}-${day}`;
}

/** Add N days to a Date, returning a new Date. */
export function addDays(d: Date, n: number): Date {
  const out = new Date(d);
  out.setDate(out.getDate() + n);
  return out;
}

/** Add N months to a Date, returning a new Date. Day-of-month is clamped. */
export function addMonths(d: Date, n: number): Date {
  const out = new Date(d);
  out.setMonth(out.getMonth() + n);
  return out;
}

/** Whole-day difference between two dates (b - a), rounded. */
export function daysBetween(a: Date, b: Date): number {
  return Math.round((b.getTime() - a.getTime()) / 86400000);
}

/** Returns true if `date` is strictly before `today` (whole-day comparison). */
export function isPastDay(date: Date, today: Date): boolean {
  return daysBetween(today, date) < 0;
}

/** Format a Date as a short label like "Sep 2" for display. */
export function fmtShortLabel(d: Date): string {
  return d.toLocaleString('en-US', { month: 'short', day: 'numeric' });
}
