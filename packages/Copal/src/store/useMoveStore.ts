'use client';

import { create } from 'zustand';
import type { MoveData, Track, Task, FloatingTodo } from '@/lib/types';
import { getTaskTrackIds, isSharedTask } from '@/lib/sharing';
import { SEED } from '@/lib/seed';
import { fmtDate } from '@/lib/dates';

// Re-export date helpers for components that imported them from the store previously.
// (Now canonical home is src/lib/dates.ts.)
export { parseDate, fmtDate } from '@/lib/dates';

// "today" is the real current date, computed once at module load — never the
// stale value baked into the seed/data file.
function currentToday(): string {
  return fmtDate(new Date());
}

export interface TaskWithTrack {
  task: Task;
  track: Track;            // parent track
  allTrackIds: string[];   // parent + sharedTrackIds
  isShared: boolean;
}

interface MoveState {
  data: MoveData;
  today: string; // YYYY-MM-DD
  selectedTaskId: string | null;

  // tag filter
  activeTagFilter: string[];
  toggleTagFilter: (tag: string) => void;
  clearTagFilters: () => void;

  // floating to-dos
  addFloatingTodo: (text: string) => void;
  toggleFloatingTodo: (id: string) => void;
  removeFloatingTodo: (id: string) => void;
  updateFloatingTodo: (id: string, text: string) => void;

  // actions
  addTask: (parentTrackId: string, task: Task) => void;
  deleteTask: (taskId: string) => void;
  addTrack: (track: Track) => void;
  removeTrack: (trackId: string) => void;
  updateTrack: (trackId: string, patch: Partial<Track>) => void;
  moveTask: (taskId: string, newParentTrackId: string) => void;
  reorderTracks: (fromIndex: number, toIndex: number) => void;
  toggleTrack: (trackId: string) => void;
  enableAllTracks: () => void;
  disableAllTracks: (except?: string[]) => void;
  setSelectedTask: (id: string | null) => void;
  setToday: (date: string) => void;
  updateTask: (taskId: string, patch: Partial<Task>) => void;

  // selectors
  getVisibleTracks: () => Track[];
  getAllTags: () => string[];
  getAllTasksFlattened: () => TaskWithTrack[];
  getTasksForTrack: (trackId: string) => TaskWithTrack[];
  passesTagFilter: (task: Task) => boolean;
}

function flatMapTasks(tracks: Track[]): TaskWithTrack[] {
  const out: TaskWithTrack[] = [];
  for (const track of tracks) {
    for (const task of track.tasks) {
      out.push({
        task,
        track,
        allTrackIds: getTaskTrackIds(task, track.id),
        isShared: isSharedTask(task),
      });
    }
  }
  return out;
}

export const useMoveStore = create<MoveState>((set, get) => ({
  data: SEED,
  today: currentToday(),
  selectedTaskId: null,
  activeTagFilter: [],

  toggleTagFilter: (tag) =>
    set((s) => {
      const exists = s.activeTagFilter.includes(tag);
      return {
        activeTagFilter: exists
          ? s.activeTagFilter.filter((t) => t !== tag)
          : [...s.activeTagFilter, tag],
      };
    }),

  clearTagFilters: () => set({ activeTagFilter: [] }),

  addFloatingTodo: (text) =>
    set((s) => ({
      data: {
        ...s.data,
        floatingTodos: [
          ...(s.data.floatingTodos ?? []),
          {
            id: `ft-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 6)}`,
            text,
            done: false,
          },
        ],
      },
    })),

  toggleFloatingTodo: (id) =>
    set((s) => ({
      data: {
        ...s.data,
        floatingTodos: (s.data.floatingTodos ?? []).map((t) =>
          t.id === id ? { ...t, done: !t.done } : t
        ),
      },
    })),

  removeFloatingTodo: (id) =>
    set((s) => ({
      data: {
        ...s.data,
        floatingTodos: (s.data.floatingTodos ?? []).filter((t) => t.id !== id),
      },
    })),

  updateFloatingTodo: (id, text) =>
    set((s) => ({
      data: {
        ...s.data,
        floatingTodos: (s.data.floatingTodos ?? []).map((t) =>
          t.id === id ? { ...t, text } : t
        ),
      },
    })),

  addTask: (parentTrackId, task) =>
    set((s) => ({
      data: {
        ...s.data,
        tracks: s.data.tracks.map((t) =>
          t.id === parentTrackId ? { ...t, tasks: [...t.tasks, task] } : t
        ),
      },
    })),

  deleteTask: (taskId) =>
    set((s) => ({
      data: {
        ...s.data,
        // A task lives once (on its parent track); sharedTrackIds only reference it,
        // so filtering every track by id removes it everywhere.
        tracks: s.data.tracks.map((t) => ({
          ...t,
          tasks: t.tasks.filter((task) => task.id !== taskId),
        })),
      },
      selectedTaskId: s.selectedTaskId === taskId ? null : s.selectedTaskId,
    })),

  addTrack: (track) =>
    set((s) => {
      // Keep special (hammock) tracks last.
      const others = s.data.tracks.filter((t) => !t.special);
      const special = s.data.tracks.filter((t) => t.special);
      return { data: { ...s.data, tracks: [...others, track, ...special] } };
    }),

  removeTrack: (trackId) =>
    set((s) => ({
      data: {
        ...s.data,
        // Drop the track AND strip any dangling sharedTrackIds refs to it.
        tracks: s.data.tracks
          .filter((t) => t.id !== trackId)
          .map((t) => ({
            ...t,
            tasks: t.tasks.map((task) =>
              task.sharedTrackIds
                ? { ...task, sharedTrackIds: task.sharedTrackIds.filter((id) => id !== trackId) }
                : task
            ),
          })),
      },
    })),

  updateTrack: (trackId, patch) =>
    set((s) => ({
      data: {
        ...s.data,
        tracks: s.data.tracks.map((t) => (t.id === trackId ? { ...t, ...patch } : t)),
      },
    })),

  moveTask: (taskId, newParentTrackId) =>
    set((s) => {
      let moved: Task | undefined;
      const stripped = s.data.tracks.map((t) => {
        const idx = t.tasks.findIndex((x) => x.id === taskId);
        if (idx >= 0) {
          moved = t.tasks[idx];
          return { ...t, tasks: t.tasks.filter((x) => x.id !== taskId) };
        }
        return t;
      });
      if (!moved) return {};
      return {
        data: {
          ...s.data,
          tracks: stripped.map((t) =>
            t.id === newParentTrackId ? { ...t, tasks: [...t.tasks, moved as Task] } : t
          ),
        },
      };
    }),

  reorderTracks: (fromIndex, toIndex) =>
    set((s) => {
      const tracks = [...s.data.tracks];
      const [moved] = tracks.splice(fromIndex, 1);
      if (moved) tracks.splice(toIndex, 0, moved);
      return { data: { ...s.data, tracks } };
    }),

  toggleTrack: (trackId) =>
    set((s) => ({
      data: {
        ...s.data,
        tracks: s.data.tracks.map((t) =>
          t.id === trackId ? { ...t, enabled: !t.enabled } : t
        ),
      },
    })),

  enableAllTracks: () =>
    set((s) => ({
      data: { ...s.data, tracks: s.data.tracks.map((t) => ({ ...t, enabled: true })) },
    })),

  disableAllTracks: (except = []) =>
    set((s) => ({
      data: {
        ...s.data,
        tracks: s.data.tracks.map((t) =>
          except.includes(t.id) ? t : { ...t, enabled: false }
        ),
      },
    })),

  setSelectedTask: (id) => set({ selectedTaskId: id }),

  setToday: (date) => set({ today: date }),

  updateTask: (taskId, patch) =>
    set((s) => ({
      data: {
        ...s.data,
        tracks: s.data.tracks.map((t) => ({
          ...t,
          tasks: t.tasks.map((task) =>
            task.id === taskId ? { ...task, ...patch } : task
          ),
        })),
      },
    })),

  getVisibleTracks: () => get().data.tracks.filter((t) => t.enabled),

  getAllTags: () => {
    const tags = new Set<string>();
    for (const track of get().data.tracks) {
      for (const task of track.tasks) {
        if (task.tags) for (const t of task.tags) tags.add(t);
      }
    }
    return Array.from(tags).sort();
  },

  getAllTasksFlattened: () => flatMapTasks(get().data.tracks),

  getTasksForTrack: (trackId) =>
    flatMapTasks(get().data.tracks).filter((entry) =>
      entry.allTrackIds.includes(trackId)
    ),

  passesTagFilter: (task) => {
    const { activeTagFilter } = get();
    if (activeTagFilter.length === 0) return true;
    if (!task.tags) return false;
    return activeTagFilter.every((t) => task.tags!.includes(t));
  },
}));

// Type re-export for callers that imported TaskWithTrack from the store.
export type { FloatingTodo };
