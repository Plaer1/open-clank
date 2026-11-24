'use client';

// Shared rendering helpers for tasks. Extracted so TimelineView and the
// future "task chip on calendar" code don't duplicate the same logic.
//
// Everything here is a pure function of its arguments — no React, no store.
// Components pass in the pre-resolved data and get back rendering specs.

import type { Task, Track } from '@/lib/types';
import { getTaskTrackIds, isSharedTask } from '@/lib/sharing';
import {
  isFuzzyStart,
  isFuzzyEnd,
  hasWhisker,
  getLayoutStart,
  getLayoutEnd,
  getWhiskerStart,
  getHardPortionStart,
} from '@/lib/fuzzy';
import type { TrackColorLookup } from './types';

export interface TaskRenderSpec {
  task: Task;
  parentTrack: Track;
  allTrackIds: string[];
  isShared: boolean;
  isHammock: boolean;
  isFuzzyStartTask: boolean;
  isFuzzyEndTask: boolean;
  hasWhisker: boolean;
  layoutStart: Date | null;
  layoutEnd: Date | null;
  whiskerStart: Date | null;
  hardPortionStart: Date | null;
}

/**
 * Build a TaskRenderSpec from a (task, parentTrack) pair. Includes all the
 * derived booleans + dates that any view needs to render the task correctly.
 */
export function buildTaskRenderSpec(
  task: Task,
  parentTrack: Track,
  today: Date,
  isHammock = false,
): TaskRenderSpec {
  return {
    task,
    parentTrack,
    allTrackIds: getTaskTrackIds(task, parentTrack.id),
    isShared: isSharedTask(task) && !isHammock,
    isHammock,
    isFuzzyStartTask: isFuzzyStart(task),
    isFuzzyEndTask: isFuzzyEnd(task),
    hasWhisker: hasWhisker(task),
    layoutStart: getLayoutStart(task),
    layoutEnd: getLayoutEnd(task),
    whiskerStart: getWhiskerStart(task),
    hardPortionStart: getHardPortionStart(task, today),
  };
}

/**
 * Returns true if a task should display as "past" (greyed).
 * Fuzzy-start tasks NEVER display as past — that's the whole point of the
 * fuzzy flag (no feeling of failure when today drifts past the anchor).
 */
export function shouldGreyOut(spec: TaskRenderSpec, today: Date): boolean {
  if (spec.isHammock) return false;
  if (spec.isFuzzyStartTask) return false;
  if (!spec.layoutStart) return false;
  return spec.layoutStart < today;
}

/**
 * Build a CSS multi-color conic-gradient string from a list of colors.
 * Used for shared hub nodes in Galaxy view (and any other multi-track chip).
 * Single-color → returns the color directly.
 */
export function buildConicGradient(colors: string[]): string {
  if (!colors.length) return '#94a3b8';
  if (colors.length === 1) return colors[0];
  const step = 100 / colors.length;
  const stops: string[] = [];
  colors.forEach((c, i) => {
    stops.push(`${c} ${i * step}% ${(i + 1) * step}%`);
  });
  return `conic-gradient(from 0deg, ${stops.join(', ')})`;
}

/**
 * Build a CSS linear-gradient string for a shared chip's top stripe.
 * Same colors as the conic gradient but laid out horizontally.
 */
export function buildStripeGradient(colors: string[], fallback: string): string {
  if (colors.length === 0) return fallback;
  if (colors.length === 1) return colors[0];
  return `linear-gradient(90deg, ${colors.join(', ')})`;
}

/**
 * Build a multi-color horizontal gradient (for shared chip on due date)
 * or a striped repeating gradient (for shared chip on non-due date).
 */
export function buildSharedChipBackground(
  colors: string[],
  isDue: boolean,
  fallback: string,
): string {
  if (colors.length === 0) return isDue ? `${fallback}cc` : `${fallback}33`;
  if (colors.length === 1) return isDue ? `${colors[0]}cc` : `${colors[0]}33`;
  if (isDue) {
    const stops = colors
      .map((c, i) => {
        const start = Math.round((i / colors.length) * 100);
        const end = Math.round(((i + 1) / colors.length) * 100);
        return `${c} ${start}% ${end}%`;
      })
      .join(', ');
    return `linear-gradient(90deg, ${stops})`;
  }
  return `repeating-linear-gradient(45deg, ${colors
    .map((c) => `${c}22`)
    .join(', ')} 0 6px, transparent 6px 12px)`;
}

/**
 * Returns the list of track colors for a shared task's track-id list,
 * filtered to only those present in the lookup.
 */
export function colorsForTrackIds(
  trackIds: string[],
  trackColorById: TrackColorLookup,
): string[] {
  return trackIds
    .map((id) => trackColorById.get(id))
    .filter((c): c is string => !!c);
}

export type { TrackColorLookup };
