// Fuzzy task helpers.
//
// A "fuzzy" task is one whose start and/or end date isn't known yet.
// The user wants to see it on the timeline without committing to a hard date
// (and without the timeline greying it out as "past" if today drifts past
// the conceptual anchor date).
//
// Two flavors:
//
// 1. Fuzzy start + fuzzy end with whisker zone (the Solar task):
//    startDate: 'FUZZY'
//    dueDate: null
//    fuzzy: {
//      anchorStart: '2026-09-02',   // conceptual start, displayed as '?'
//      anchorEnd:   '2027-03-02',   // conceptual end, displayed as '?'
//      whiskerStart:'2026-12-02',   // first 3mo are 'hard', next 3mo are 'whisker'
//    }
//
// 2. Hard start + fuzzy end (Gene's clean space, water tank):
//    startDate: '2026-09-02'
//    dueDate: null
//    fuzzy: {
//      anchorEnd: '2026-09-16',     // 14 days out, displayed as '?'
//    }
//
// Rules:
// - If startDate === 'FUZZY', the task never displays as "past" — even if
//   today > anchorStart. The user will replace 'FUZZY' with a real date
//   when they know it; until then, no feeling of failure.
// - If dueDate is null and fuzzy.anchorEnd is set, the chip extends to
//   anchorEnd visually and displays '?' for the end.
// - For hammock calculation, the task's "effective end" is
//   dueDate ?? fuzzy.anchorEnd ?? null.

import type { Task } from './types';
import { parseDate } from './dates';

export interface FuzzySpec {
  /** Conceptual start when the user hasn't set a real one. Display '?' but layout from this date. */
  anchorStart?: string;
  /** Conceptual end when the user hasn't set a real one. Display '?' but extend the chip to this date. */
  anchorEnd?: string;
  /** Date at which the chip transitions from solid to whisker graphic. Defaults to midpoint. */
  whiskerStart?: string;
  /** If true (with anchorStart), the fuzzy start renders as a left "fade-in" gradient instead of a hard '?'. */
  fadeIn?: boolean;
}

/** Returns true if this task's fuzzy start should render as a fade-in gradient. */
export function isFadeIn(task: Task): boolean {
  return task.startDate === 'FUZZY' && !!task.fuzzy?.fadeIn && !!task.fuzzy?.anchorStart;
}

/** Returns true if this task's start is fuzzy (displayed as '?'). */
export function isFuzzyStart(task: Task): boolean {
  return task.startDate === 'FUZZY' || (!!task.fuzzy?.anchorStart && task.startDate === 'FUZZY');
}

/** Returns true if this task's end is fuzzy (displayed as '?'). */
export function isFuzzyEnd(task: Task): boolean {
  return task.dueDate === null && !!task.fuzzy?.anchorEnd;
}

/** Returns true if this task has a whisker zone (solid → whisker graphic transition). */
export function hasWhisker(task: Task): boolean {
  return !!task.fuzzy?.whiskerStart;
}

/** The conceptual start date for layout. Falls back to startDate if not fuzzy. */
export function getLayoutStart(task: Task): Date | null {
  if (task.startDate === 'AUTO') return null; // hammock, handled elsewhere
  if (task.startDate === 'FUZZY') {
    if (task.fuzzy?.anchorStart) return parseDate(task.fuzzy.anchorStart);
    return null;
  }
  return parseDate(task.startDate);
}

/** The conceptual end date for layout. Falls back to dueDate if set, then fuzzy.anchorEnd. */
export function getLayoutEnd(task: Task): Date | null {
  if (task.dueDate) return parseDate(task.dueDate);
  if (task.fuzzy?.anchorEnd) return parseDate(task.fuzzy.anchorEnd);
  return null;
}

/** The "effective end" used for hammock calculation: dueDate ?? fuzzy.anchorEnd ?? null. */
export function getEffectiveEnd(task: Task): Date | null {
  if (task.dueDate) return parseDate(task.dueDate);
  if (task.fuzzy?.anchorEnd) return parseDate(task.fuzzy.anchorEnd);
  return null;
}

/** The whisker transition date, or null if no whisker. */
export function getWhiskerStart(task: Task): Date | null {
  if (!task.fuzzy?.whiskerStart) return null;
  return parseDate(task.fuzzy.whiskerStart);
}

/**
 * The display string for the start date. Returns '?' if fuzzy.
 * (Does NOT resolve 'AUTO' — that's the hammock's job, handled by the store.)
 */
export function displayStart(task: Task, resolvedStart?: string | null): string {
  if (task.startDate === 'FUZZY') return '?';
  if (task.startDate === 'AUTO') return resolvedStart ?? '(auto)';
  return task.startDate;
}

/** The display string for the end date. Returns '?' if fuzzy, '∞' if null with no anchor. */
export function displayEnd(task: Task): string {
  if (task.dueDate) return task.dueDate;
  if (task.fuzzy?.anchorEnd) return '?';
  return '∞';
}

/**
 * "Shrink" behavior for fuzzy-start tasks: when today is past anchorStart,
 * the visible "hard" portion of the chip shrinks (less remaining certain time).
 * Returns the date from which the hard portion should be rendered solid.
 * If today < anchorStart, returns anchorStart. Otherwise returns today.
 *
 * (This implements the user's "if i load up the timeline on september 3 it
 * hasn't 'gone over' the start but the total timeline shrinks" requirement.)
 */
export function getHardPortionStart(task: Task, today: Date): Date | null {
  const layoutStart = getLayoutStart(task);
  if (!layoutStart) return null;
  if (!isFuzzyStart(task)) return layoutStart;
  // For fuzzy-start tasks, the hard portion "starts" at max(anchorStart, today).
  // This means as today advances, the hard portion's left edge slides forward
  // (the chip's overall extent stays the same; only the solid part shrinks).
  return layoutStart < today ? today : layoutStart;
}
