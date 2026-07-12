'use client';

import { useMoveStore } from '@/store/useMoveStore';
import type { Task } from '@/lib/types';

/**
 * Tag-filter hook. Wraps the store's tag-filter state + passes() predicate
 * so views don't each have to subscribe to `activeTagFilter` and
 * `passesTagFilter` separately.
 */
export function useTagFilter() {
  const active = useMoveStore((s) => s.activeTagFilter);
  const passes = useMoveStore((s) => s.passesTagFilter);
  const toggle = useMoveStore((s) => s.toggleTagFilter);
  const clear = useMoveStore((s) => s.clearTagFilters);

  return {
    active,
    isActive: active.length > 0,
    passes: passes as (task: Task) => boolean,
    toggle,
    clear,
  };
}
