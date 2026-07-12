'use client';

import { useMemo } from 'react';
import { useMoveStore } from '@/store/useMoveStore';
import { useVaultIndex } from '@/hooks/useVaultIndex';
import { resolveTracks, computeHammockStart, computeLatestVisibleEnd } from '@/lib/hammock';
import type { Task, Track } from '@/lib/types';
import type { VaultDerivedTask } from '@/lib/notes';

/**
 * Centralized "resolved tracks" hook. Eliminates the duplicate useMemo that
 * was copy-pasted across GalaxyView, TimelineView, CalendarView, and
 * TaskDetailSheet.
 *
 * Returns:
 *   - resolvedTracks: tracks with AUTO startDates replaced by the computed hammock start
 *   - trackById:      Map<id, Track> for quick lookups
 *   - hammockStart:   YYYY-MM-DD string | null
 *   - latestVisibleEnd: Date | null  (the latest effective end across enabled non-special tracks)
 *   - latestVisibleEndStr: string | null  (formatted YYYY-MM-DD)
 */
export function useResolvedTracks() {
  const data = useMoveStore((s) => s.data);
  const today = useMoveStore((s) => s.today);
  const { index: vaultIndex } = useVaultIndex();

  return useMemo(() => {
    const vaultTrack = buildVaultTrack(vaultIndex.tasks, today);
    const sourceTracks = vaultTrack.tasks.length > 0 ? [...data.tracks, vaultTrack] : data.tracks;
    const hammockStart = computeHammockStart(sourceTracks);
    const latestVisibleEnd = computeLatestVisibleEnd(sourceTracks);
    const resolved = resolveTracks(sourceTracks, hammockStart);
    const trackById = new Map<string, Track>();
    for (const t of resolved) trackById.set(t.id, t);

    return {
      resolvedTracks: resolved,
      trackById,
      hammockStart,
      latestVisibleEnd,
      latestVisibleEndStr: latestVisibleEnd
        ? `${latestVisibleEnd.getFullYear()}-${String(latestVisibleEnd.getMonth() + 1).padStart(2, '0')}-${String(latestVisibleEnd.getDate()).padStart(2, '0')}`
        : null,
    };
  }, [data, vaultIndex.tasks, today]);
}

function buildVaultTrack(tasks: VaultDerivedTask[], today: string): Track {
  return {
    id: 'vault-derived',
    name: 'Vault notes',
    color: '#22d3ee',
    icon: '📝',
    enabled: true,
    tasks: tasks.map((derived): Task => ({
      id: derived.id,
      title: derived.title,
      description: `${derived.noteTitle}:L${derived.line}\n${derived.text}`,
      startDate: derived.scheduledDate ?? derived.dueDate ?? today,
      dueDate: derived.dueDate ?? null,
      status: derived.status === 'done' ? 'done' : derived.status === 'in-progress' ? 'in-progress' : 'pending',
      priority: derived.priority,
      tags: ['vault', ...derived.tags],
    })),
  };
}
