'use client';

import { useMemo, useState } from 'react';
import { useMoveStore } from '@/store/useMoveStore';
import { useResolvedTracks } from '@/hooks/useResolvedTracks';
import { useTagFilter } from '@/hooks/useTagFilter';
import { parseDate, fmtDate } from '@/lib/dates';
import type { Task, Track } from '@/lib/types';
import { getTaskTrackIds, isSharedTask } from '@/lib/sharing';
import { isFuzzyStart, isFuzzyEnd } from '@/lib/fuzzy';
import { buildSharedChipBackground } from '@/lib/render';

interface CalendarEntry {
  task: Task;
  parentTrack: Track;
  allTrackIds: string[]; // parent + shared, filtered to enabled
  isShared: boolean;
  /** The kind of marker this entry represents on this date. */
  marker: 'start' | 'due' | 'span' | 'fuzzy-start' | 'fuzzy-end';
}

export function CalendarView({ onSelectTask }: { onSelectTask: (id: string) => void }) {
  const today = useMoveStore((s) => s.today);
  const selectedTaskId = useMoveStore((s) => s.selectedTaskId);
  const { resolvedTracks, trackById, hammockStart } = useResolvedTracks();
  const tagFilter = useTagFilter();

  const todayDate = useMemo(() => parseDate(today), [today]);

  const [viewYear, setViewYear] = useState(2026);
  const [viewMonth, setViewMonth] = useState(6); // 0-indexed; July = 6

  // Build map: YYYY-MM-DD → CalendarEntry[]
  // SHARED tasks deduplicated by task.id.
  // FUZZY tasks: anchorStart shows as "fuzzy-start" marker, anchorEnd as "fuzzy-end".
  const tasksByDate = useMemo(() => {
    const map = new Map<string, CalendarEntry[]>();
    const seenTaskIds = new Set<string>();

    for (const track of resolvedTracks) {
      if (!track.enabled) continue;

      for (const task of track.tasks) {
        if (seenTaskIds.has(task.id)) continue;
        if (!tagFilter.passes(task)) continue;
        seenTaskIds.add(task.id);

        const allTrackIds = getTaskTrackIds(task, track.id);
        const shared = isSharedTask(task);
        const visibleTrackIds = allTrackIds.filter((id) => trackById.get(id)?.enabled);
        if (visibleTrackIds.length === 0) continue;

        const fuzzyStart = isFuzzyStart(task);
        const fuzzyEnd = isFuzzyEnd(task);

        const baseEntry: Omit<CalendarEntry, 'marker'> = {
          task,
          parentTrack: track,
          allTrackIds: visibleTrackIds,
          isShared: shared,
        };

        const addEntry = (ds: string, marker: CalendarEntry['marker']) => {
          const arr = map.get(ds) ?? [];
          arr.push({ ...baseEntry, marker });
          map.set(ds, arr);
        };

        // Fuzzy markers (single-day, at the anchor).
        if (fuzzyStart && task.fuzzy?.anchorStart) addEntry(task.fuzzy.anchorStart, 'fuzzy-start');
        if (fuzzyEnd && task.fuzzy?.anchorEnd) addEntry(task.fuzzy.anchorEnd, 'fuzzy-end');

        const startIsHard =
          !!task.startDate && task.startDate !== 'AUTO' && task.startDate !== 'FUZZY';
        const dueIsHard = !!task.dueDate;

        if (startIsHard && dueIsHard && task.dueDate !== task.startDate) {
          // Multi-day hard task: populate EVERY day from start → due (start on
          // first, due on last, 'span' between) so the event fills its range
          // instead of only landing on its due date.
          const cur = parseDate(task.startDate!);
          const end = parseDate(task.dueDate!);
          for (; cur <= end; cur.setDate(cur.getDate() + 1)) {
            const ds = fmtDate(cur);
            addEntry(ds, ds === task.startDate ? 'start' : ds === task.dueDate ? 'due' : 'span');
          }
        } else {
          if (startIsHard) addEntry(task.startDate!, 'start');
          if (dueIsHard) addEntry(task.dueDate!, 'due');
        }
      }
    }
    return map;
  }, [resolvedTracks, trackById, tagFilter.passes, tagFilter.active]);

  // Build calendar grid (6 weeks × 7 days)
  const grid = useMemo(() => buildCalendarGrid(viewYear, viewMonth), [viewYear, viewMonth]);

  const monthLabel = new Date(viewYear, viewMonth, 1).toLocaleString('en-US', {
    month: 'long',
    year: 'numeric',
  });

  function goPrevMonth() {
    if (viewMonth === 0) {
      setViewMonth(11);
      setViewYear(viewYear - 1);
    } else setViewMonth(viewMonth - 1);
  }
  function goNextMonth() {
    if (viewMonth === 11) {
      setViewMonth(0);
      setViewYear(viewYear + 1);
    } else setViewMonth(viewMonth + 1);
  }

  return (
    <div className="w-full h-full flex flex-col rounded-xl border border-slate-700/50 bg-slate-950/40 overflow-hidden">
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-slate-700/50">
        <button
          onClick={goPrevMonth}
          className="px-3 py-1 text-sm rounded-md bg-slate-800 hover:bg-slate-700 text-slate-200"
        >
          ← Prev
        </button>
        <h3 className="text-lg font-semibold text-slate-100">{monthLabel}</h3>
        <button
          onClick={goNextMonth}
          className="px-3 py-1 text-sm rounded-md bg-slate-800 hover:bg-slate-700 text-slate-200"
        >
          Next →
        </button>
      </div>

      {/* Day-of-week header */}
      <div className="grid grid-cols-7 border-b border-slate-700/50 bg-slate-900/60">
        {['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'].map((d) => (
          <div
            key={d}
            className="text-center text-xs font-semibold uppercase tracking-wide text-slate-400 py-2"
          >
            {d}
          </div>
        ))}
      </div>

      {/* Grid */}
      <div className="flex-1 grid grid-rows-6 grid-cols-7 gap-px bg-slate-800/40">
        {grid.flat().map(({ date, isCurrentMonth, dateStr }) => (
          <CalendarCell
            key={dateStr}
            date={date}
            isCurrentMonth={isCurrentMonth}
            dateStr={dateStr}
            isToday={dateStr === today}
            isPast={date < todayDate}
            isHammockStart={hammockStart === dateStr}
            entries={tasksByDate.get(dateStr) ?? []}
            selectedTaskId={selectedTaskId}
            trackById={trackById}
            onSelectTask={onSelectTask}
          />
        ))}
      </div>

      {/* Legend at the bottom */}
      <div className="border-t border-slate-700/50 bg-slate-900/40 px-3 py-1.5 flex flex-wrap items-center gap-3 text-[10px] text-slate-400">
        <span className="flex items-center gap-1">
          <span className="w-2 h-2 rounded-full bg-cyan-400" />
          today
        </span>
        <span className="flex items-center gap-1">
          <span className="text-[11px]">▸</span> start
        </span>
        <span className="flex items-center gap-1">
          <span className="text-[11px]">◆</span> due
        </span>
        <span className="flex items-center gap-1">
          <span className="text-[11px]">?</span> fuzzy
        </span>
        <span className="flex items-center gap-1">
          <span className="text-[11px]">🔗</span> shared (one chip, multi-track)
        </span>
        <span className="text-slate-500 ml-auto">
          Shared events appear once and carry all their tracks&apos; colors.
        </span>
      </div>
    </div>
  );
}

// ── Sub-components ───────────────────────────────────────────────────────

interface CalendarCellProps {
  date: Date;
  isCurrentMonth: boolean;
  dateStr: string;
  isToday: boolean;
  isPast: boolean;
  isHammockStart: boolean;
  entries: CalendarEntry[];
  selectedTaskId: string | null;
  trackById: Map<string, Track>;
  onSelectTask: (id: string) => void;
}

function CalendarCell(props: CalendarCellProps) {
  const {
    date,
    isCurrentMonth,
    dateStr,
    isToday,
    isPast,
    isHammockStart,
    entries,
    selectedTaskId,
    trackById,
    onSelectTask,
  } = props;

  return (
    <div
      className={`relative p-1.5 min-h-[88px] flex flex-col gap-1 overflow-hidden ${
        isCurrentMonth ? 'bg-slate-950/60' : 'bg-slate-950/30'
      } ${isToday ? 'ring-2 ring-cyan-400 ring-inset' : ''}`}
    >
      <div className="flex items-center justify-between">
        <span
          className={`text-xs font-medium ${
            isToday
              ? 'bg-cyan-400 text-slate-950 px-1.5 py-0.5 rounded font-bold'
              : isPast
              ? 'text-slate-600'
              : isCurrentMonth
              ? 'text-slate-300'
              : 'text-slate-700'
          }`}
        >
          {date.getDate()}
        </span>
        {isHammockStart && (
          <span className="text-[9px] text-cyan-300 italic">🛶 start</span>
        )}
      </div>

      <div className="flex-1 flex flex-col gap-1 overflow-hidden">
        {entries.slice(0, 4).map((entry) => (
          <CalendarChip
            key={`${entry.task.id}-${dateStr}-${entry.marker}`}
            entry={entry}
            dateStr={dateStr}
            isPast={isPast}
            isSelected={entry.task.id === selectedTaskId}
            trackById={trackById}
            onSelectTask={onSelectTask}
          />
        ))}
        {entries.length > 4 && (
          <span className="text-[9px] text-slate-500 px-1">
            +{entries.length - 4} more
          </span>
        )}
      </div>
    </div>
  );
}

interface CalendarChipProps {
  entry: CalendarEntry;
  dateStr: string;
  isPast: boolean;
  isSelected: boolean;
  trackById: Map<string, Track>;
  onSelectTask: (id: string) => void;
}

function CalendarChip(props: CalendarChipProps) {
  const { entry, isPast, isSelected, trackById, onSelectTask } = props;
  const { task, parentTrack, allTrackIds, isShared, marker } = entry;

  const colors = allTrackIds
    .map((id) => trackById.get(id)?.color)
    .filter(Boolean) as string[];

  const isDue = marker === 'due';
  const isSpan = marker === 'span';
  const isFuzzyMarker = marker === 'fuzzy-start' || marker === 'fuzzy-end';
  const chipColor = parentTrack.color;

  // Background: shared → multi-color, fuzzy → faint, span → solid, due → solid, start → faint
  let bg: string;
  if (isShared) {
    bg = buildSharedChipBackground(colors, isDue, chipColor);
  } else if (isFuzzyMarker) {
    bg = `${chipColor}33`;
  } else if (isSpan) {
    bg = `${chipColor}99`;
  } else if (isDue) {
    bg = `${chipColor}cc`;
  } else {
    bg = `${chipColor}33`;
  }

  // Prefix glyph
  let prefix: React.ReactNode = null;
  if (isFuzzyMarker) {
    prefix = <span className="font-bold mr-0.5">?</span>;
  } else if (isShared) {
    prefix = <span className="font-bold mr-0.5">🔗</span>;
  } else if (marker === 'start') {
    prefix = <span>▸ </span>;
  } else if (marker === 'due') {
    prefix = <span>◆ </span>;
  }

  // Tooltip
  const sharedLabel = isShared
    ? ` · shared: ${allTrackIds.map((id) => trackById.get(id)?.name ?? id).join(' + ')}`
    : '';
  const fuzzyLabel = isFuzzyMarker
    ? marker === 'fuzzy-start'
      ? ' (fuzzy start)'
      : ' (fuzzy end)'
    : marker === 'start'
    ? ' (start)'
    : marker === 'due'
    ? ' (due)'
    : '';
  const title = `${parentTrack.name}${
    isShared ? ` + ${allTrackIds.length - 1} more` : ''
  } · ${task.title}${fuzzyLabel}${sharedLabel}`;

  return (
    <button
      onClick={() => onSelectTask(task.id)}
      className="text-left text-[10px] px-1 py-0.5 rounded truncate transition-all hover:scale-[1.02] relative"
      style={{
        background: bg,
        color: isDue ? '#0f172a' : chipColor,
        border: `1px solid ${isShared ? 'white' : chipColor}`,
        borderStyle: isFuzzyMarker ? 'dashed' : 'solid',
        opacity: isPast && !isFuzzyMarker ? 0.7 : 1,
        outline: isSelected ? '1.5px solid white' : 'none',
        outlineOffset: '-1px',
        boxShadow: isShared && isDue ? '0 0 0 1px rgba(255,255,255,0.4)' : 'none',
      }}
      title={title}
    >
      {!isSpan && (
        <>
          {prefix}
          <span className="font-medium truncate">{task.title}</span>
          {isShared && (
            <span className="ml-1 inline-flex gap-px align-middle">
              {allTrackIds.slice(0, 5).map((id) => (
                <span
                  key={id}
                  className="w-1.5 h-1.5 rounded-full inline-block"
                  style={{
                    background: trackById.get(id)?.color ?? '#888',
                    border: '0.5px solid rgba(0,0,0,0.4)',
                  }}
                />
              ))}
              {allTrackIds.length > 5 && (
                <span className="text-[8px] ml-0.5">+{allTrackIds.length - 5}</span>
              )}
            </span>
          )}
        </>
      )}
    </button>
  );
}

// ── Pure helpers ─────────────────────────────────────────────────────────

function buildCalendarGrid(year: number, month: number) {
  const firstOfMonth = new Date(year, month, 1);
  const startDay = firstOfMonth.getDay();
  const gridStart = new Date(firstOfMonth);
  gridStart.setDate(gridStart.getDate() - startDay);

  const weeks: { date: Date; isCurrentMonth: boolean; dateStr: string }[][] = [];
  for (let w = 0; w < 6; w++) {
    const week: { date: Date; isCurrentMonth: boolean; dateStr: string }[] = [];
    for (let d = 0; d < 7; d++) {
      const date = new Date(gridStart);
      date.setDate(gridStart.getDate() + w * 7 + d);
      week.push({
        date,
        isCurrentMonth: date.getMonth() === month,
        dateStr: fmtDate(date),
      });
    }
    weeks.push(week);
  }
  return weeks;
}
