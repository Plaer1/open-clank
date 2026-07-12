// Hammock-line calculation.
//
// The "Relax on my hammock" track auto-computes its start date as one day
// after the latest visible task's effective end. "Effective end" includes
// fuzzy tasks' anchorEnd, so a 6-month nebulous Solar task correctly pushes
// the hammock out by 6 months.

import type { Track, Task } from './types';
import { parseDate, fmtDate, addDays } from './dates';
import { getEffectiveEnd } from './fuzzy';

/**
 * Returns the latest effective end date across all enabled non-special tracks.
 * Uses `getEffectiveEnd` (which includes fuzzy.anchorEnd) so nebulous tasks
 * participate correctly.
 */
export function computeLatestVisibleEnd(tracks: Track[]): Date | null {
  let latest: Date | null = null;
  for (const track of tracks) {
    if (!track.enabled || track.special) continue;
    for (const task of track.tasks) {
      const end = getEffectiveEnd(task);
      if (!end) continue;
      if (!latest || end > latest) latest = end;
    }
  }
  return latest;
}

/**
 * Returns the YYYY-MM-DD string for the day AFTER the latest visible task end.
 * Returns null if there are no visible non-special tasks.
 */
export function computeHammockStart(tracks: Track[]): string | null {
  const latest = computeLatestVisibleEnd(tracks);
  if (!latest) return null;
  return fmtDate(addDays(latest, 1));
}

/**
 * Resolved tracks: returns a new array where special (hammock) tracks have
 * their AUTO startDates replaced with the computed hammock start. All other
 * tracks pass through unchanged.
 */
export function resolveTracks(tracks: Track[], hammockStart: string | null): Track[] {
  return tracks.map((t) => {
    if (!t.special) return t;
    return {
      ...t,
      tasks: t.tasks.map((task) => ({
        ...task,
        startDate: hammockStart ?? task.startDate,
      })),
    };
  });
}

/** Returns the latest due/effective-end date string for display in the sidebar. */
export function getLatestVisibleEndStr(tracks: Track[]): string | null {
  const latest = computeLatestVisibleEnd(tracks);
  return latest ? fmtDate(latest) : null;
}

/** Re-export for components that still want to work with raw task ends. */
export { getEffectiveEnd as getTaskEffectiveEnd };

/** Parse helper re-export for components that imported it from the store previously. */
export { parseDate };
