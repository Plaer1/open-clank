// Core types for the move master timeline.
// Type-only file — function helpers live in src/lib/{dates,sharing,fuzzy,hammock}.ts

export type TaskStatus = 'pending' | 'in-progress' | 'done' | 'ongoing';
export type TaskPriority = 'low' | 'medium' | 'high';

// Re-export FuzzySpec so callers can import everything from one place.
import type { FuzzySpec } from './fuzzy';
export type { FuzzySpec } from './fuzzy';

/** A sub-stage of a task (multi-stage events). Optional date + done flag. */
export interface Stage {
  id: string;
  title: string;
  done?: boolean;
  date?: string; // YYYY-MM-DD, optional
}

export interface Task {
  id: string;
  title: string;
  description?: string;
  /**
   * Start date — one of:
   *   - 'YYYY-MM-DD'  : a normal hard start
   *   - 'AUTO'        : hammock-style auto-compute (resolved by the store)
   *   - 'FUZZY'       : displayed as '?', laid out from `fuzzy.anchorStart`
   */
  startDate: string;
  /**
   * Due date — one of:
   *   - 'YYYY-MM-DD' : a normal hard end
   *   - null         : no hard end. If `fuzzy.anchorEnd` is set, the chip
   *                    extends to that date and displays '?'. Otherwise it
   *                    fades into the future (hammock behavior).
   */
  dueDate: string | null;
  status: TaskStatus;
  priority: TaskPriority;

  // ── Sharing & tagging (added v2) ──────────────────────────────────────
  /** Free-form tags for grouping/filtering. */
  tags?: string[];

  /** Additional track ids this task also belongs to. */
  sharedTrackIds?: string[];

  /** Optional explicit link id for cross-track task merging. */
  linkId?: string;

  // ── Fuzzy / nebulous dates (added v3) ─────────────────────────────────
  /** Fuzzy start/end spec for tasks with uncertain dates. See fuzzy.ts. */
  fuzzy?: FuzzySpec;

  /** Optional fade tail length for open-ended timeline chips. */
  fadeDays?: number;

  /** Optional endpoint labels rendered beside timeline chip boxes. */
  titleStart?: string;
  titleEnd?: string;

  // ── Sub-events / multiple stages (added v4) ───────────────────────────
  /** Ordered sub-stages of this task. */
  stages?: Stage[];
}

export interface Track {
  id: string;
  name: string;
  color: string;
  icon: string;
  enabled: boolean;
  special?: boolean; // for relax-hammock
  tasks: Task[];
}

export interface FloatingTodo {
  id: string;
  text: string;
  done: boolean;
  notes?: string;
}

export interface MoveData {
  schemaVersion: number;
  title: string;
  originCity: string;
  destinationCity: string;
  moveDeadline: string;
  globalStart: string;
  today: string;
  aiImportHints?: Record<string, unknown>;
  tracks: Track[];
  /** To-dos that have no track, no date, no timeline presence. */
  floatingTodos?: FloatingTodo[];
}

// Icon helper — kept here (constant, not a function).
export const ICON_MAP = {
  cat: '🐱',
  dog: '🐶',
  truck: '🚚',
  car: '🚗',
  bug: '🐛',
  sun: '☀️',
  fence: '🚧',
  ant: '🐜',
  toad: '🐸',
  hammock: '🛶',
  bucket: '🪣',
  broom: '🧹',
  box: '📦',
  clown: '🤡',
} as const;

export type IconKey = keyof typeof ICON_MAP;

/**
 * Render glyph for a track icon. Backward compatible: if `icon` is a known
 * ICON_MAP key (e.g. 'cat'), returns its emoji; otherwise treats `icon` as a
 * raw emoji/unicode char (set via the emoji selector) and returns it directly.
 */
export function glyphFor(icon: string): string {
  const mapped = ICON_MAP[icon as IconKey];
  if (mapped) return mapped;
  return icon && icon.trim().length > 0 ? icon : '•';
}

/** Convenience type for the common "Map<trackId, color>" lookup. */
export type TrackColorLookup = Map<string, string>;
