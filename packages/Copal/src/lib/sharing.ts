// Sharing helpers — functions that operate on a Task's sharedTrackIds.
// (Moved out of types.ts so types.ts stays type-only.)

import type { Task } from './types';

/**
 * Returns the full list of track IDs a task belongs to (parent + shared).
 * Never includes duplicates. Always non-empty.
 */
export function getTaskTrackIds(task: Task, parentTrackId: string): string[] {
  const ids = new Set<string>([parentTrackId]);
  if (task.sharedTrackIds) {
    for (const id of task.sharedTrackIds) ids.add(id);
  }
  return Array.from(ids);
}

/** Returns true if a task is shared across multiple tracks. */
export function isSharedTask(task: Task): boolean {
  return !!task.sharedTrackIds && task.sharedTrackIds.length > 0;
}
