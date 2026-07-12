'use client';

import { useEffect, useMemo, useRef, useState } from 'react';
import { useMoveStore } from '@/store/useMoveStore';
import { useResolvedTracks } from '@/hooks/useResolvedTracks';
import { useTagFilter } from '@/hooks/useTagFilter';
import { SingleTimelineBody } from './SingleTimelineView';
import { parseDate, daysBetween, addDays, fmtDate } from '@/lib/dates';
import { glyphFor } from '@/lib/types';
import type { Task, Track } from '@/lib/types';
import { getTaskTrackIds, isSharedTask } from '@/lib/sharing';
import {
  isFuzzyStart,
  isFuzzyEnd,
  hasWhisker,
  getLayoutStart,
  getLayoutEnd,
  getWhiskerStart,
} from '@/lib/fuzzy';

const DEFAULT_DAY_WIDTH = 18; // px per day
const MIN_DAY_WIDTH = 8;
const MAX_DAY_WIDTH = 56;
const HAMMOCK_BUFFER_DAYS = 60;
const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

export function TimelineView({ onSelectTask }: { onSelectTask: (id: string) => void }) {
  const data = useMoveStore((s) => s.data);
  const today = useMoveStore((s) => s.today);
  const selectedTaskId = useMoveStore((s) => s.selectedTaskId);
  const [dayWidth, setDayWidth] = useState(DEFAULT_DAY_WIDTH);
  const [laneHeight, setLaneHeight] = useState(56);
  const [mode, setMode] = useState<'regular' | 'condensed'>('regular');
  const [condensedStyle, setCondensedStyle] = useState<'dots' | 'waves' | 'tree'>('dots');
  const scrollRef = useRef<HTMLDivElement>(null);
  const [panning, setPanning] = useState(false);
  const panRef = useRef<{ x: number; s: number } | null>(null);
  // How many days before today the timeline extends. Grows dynamically when the
  // user scrolls near the left edge, giving infinite backward panning.
  const [backDays, setBackDays] = useState(3);
  // Edge being resize-dragged: day offset for the guide line position plus
  // the date label of the day the edge lands on.
  const [resizeGuide, setResizeGuide] = useState<{ offset: number; label: string } | null>(null);

  const { resolvedTracks, trackById, hammockStart } = useResolvedTracks();
  const tagFilter = useTagFilter();

  // Flat list of (task, parentTrack) for ALL tasks.
  const allTaskEntries = useMemo(() => {
    const out: { task: Task; parentTrack: Track }[] = [];
    for (const track of resolvedTracks) {
      for (const task of track.tasks) {
        out.push({ task, parentTrack: track });
      }
    }
    return out;
  }, [resolvedTracks]);

  const todayDate = useMemo(() => parseDate(today), [today]);

  const { rangeStart, rangeEnd, totalDays } = useMemo(() => {
    let minDate = parseDate(data.globalStart);
    let maxDate = parseDate(data.moveDeadline);
    for (const { task } of allTaskEntries) {
      const ls = getLayoutStart(task);
      const le = getLayoutEnd(task);
      if (ls && ls < minDate) minDate = ls;
      if (le && le > maxDate) maxDate = le;
    }
    if (hammockStart) {
      const ext = new Date(parseDate(hammockStart));
      ext.setDate(ext.getDate() + HAMMOCK_BUFFER_DAYS);
      if (ext > maxDate) maxDate = ext;
    }
    // Start from today - backDays so the view opens near today with a few days
    // of pre-slop. `backDays` grows when the user scrolls near the left edge.
    const back = new Date(todayDate);
    back.setDate(back.getDate() - backDays);
    const start = back < minDate ? back : minDate;
    return {
      rangeStart: start,
      rangeEnd: maxDate,
      totalDays: daysBetween(start, maxDate) + 1,
    };
  }, [allTaskEntries, hammockStart, data.globalStart, data.moveDeadline, todayDate, backDays]);

  const todayOffset = useMemo(() => daysBetween(rangeStart, todayDate), [todayDate, rangeStart]);

  // Scroll to today + 3 days pre-slop on initial mount.
  const didInitScroll = useRef(false);
  useEffect(() => {
    if (didInitScroll.current) return;
    const el = scrollRef.current;
    if (!el || todayOffset <= 0) return;
    el.scrollLeft = Math.max(0, (todayOffset - 3) * dayWidth);
    didInitScroll.current = true;
  }, [todayOffset, dayWidth]);

  const tracksWithTasks = useMemo(() => {
    return resolvedTracks.filter((t) => {
      if (!t.enabled) return false;
      return allTaskEntries.some(({ task, parentTrack }) =>
        getTaskTrackIds(task, parentTrack.id).includes(t.id)
      );
    });
  }, [resolvedTracks, allTaskEntries]);

  const monthMarkers = useMemo(() => buildMonthMarkers(rangeStart, rangeEnd), [rangeStart, rangeEnd]);
  const weekTicks = useMemo(() => buildWeekTicks(rangeStart, rangeEnd), [rangeStart, rangeEnd]);

  // Per-day header cells (offset, day-of-month, weekend?, today?).
  const dayHeaders = useMemo(() => {
    const arr: { offset: number; day: number; isWeekend: boolean; isToday: boolean }[] = [];
    const d = new Date(rangeStart);
    for (let i = 0; i < totalDays; i++) {
      arr.push({
        offset: i,
        day: d.getDate(),
        isWeekend: d.getDay() === 0 || d.getDay() === 6,
        isToday: daysBetween(todayDate, d) === 0,
      });
      d.setDate(d.getDate() + 1);
    }
    return arr;
  }, [rangeStart, totalDays, todayDate]);

  // Thin out day labels when zoomed out so they don't overlap.
  const dayStep = dayWidth < 10 ? 3 : dayWidth < 15 ? 2 : 1;

  function tasksForTrack(trackId: string) {
    return allTaskEntries
      .filter(({ task, parentTrack }) => getTaskTrackIds(task, parentTrack.id).includes(trackId))
      .filter(({ task }) => tagFilter.passes(task))
      .map(({ task, parentTrack }) => ({
        task,
        parentTrack,
        isShared: isSharedTask(task),
        allTrackIds: getTaskTrackIds(task, parentTrack.id),
      }));
  }

  function zoomIn() {
    setDayWidth((w) => Math.min(MAX_DAY_WIDTH, w + 2));
  }
  function zoomOut() {
    setDayWidth((w) => Math.max(MIN_DAY_WIDTH, w - 2));
  }
  function taller() {
    setLaneHeight((h) => Math.min(160, h + 8));
  }
  function shorter() {
    setLaneHeight((h) => Math.max(32, h - 8));
  }
  function scrollToToday() {
    const el = scrollRef.current;
    if (!el) return;
    const x = todayOffset * dayWidth + dayWidth / 2;
    el.scrollTo({ left: Math.max(0, x - el.clientWidth / 2), behavior: 'smooth' });
  }

  // Click-and-drag to pan the timeline (mouse/pen only — touch uses native scroll).
  function onPanDown(e: React.PointerEvent) {
    if (e.pointerType === 'touch') return;
    const t = e.target as HTMLElement;
    // Don't pan when grabbing a chip / button / input (those have their own drag/click).
    if (t.closest('button, [role="button"], input, select, textarea, a')) return;
    (e.currentTarget as Element).setPointerCapture(e.pointerId);
    panRef.current = { x: e.clientX, s: (e.currentTarget as HTMLElement).scrollLeft };
    setPanning(true);
  }
  function onPanMove(e: React.PointerEvent) {
    const p = panRef.current;
    if (!p) return;
    (e.currentTarget as HTMLElement).scrollLeft = p.s - (e.clientX - p.x);
  }
  function onPanUp(e: React.PointerEvent) {
    if (!panRef.current) return;
    panRef.current = null;
    setPanning(false);
    (e.currentTarget as Element).releasePointerCapture?.(e.pointerId);
  }

  // Extend the timeline backwards when scrolling near the left edge.
  // This gives infinite backward panning without a huge initial range.
  const EXTEND_THRESHOLD = 200; // px from left edge
  const EXTEND_DAYS = 30;
  const prevBackDays = useRef(backDays);
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    function onScroll() {
      if (el!.scrollLeft < EXTEND_THRESHOLD) {
        const oldBack = prevBackDays.current;
        const newBack = oldBack + EXTEND_DAYS;
        prevBackDays.current = newBack;
        setBackDays(newBack);
        // Preserve visible content position after range extends backwards.
        // The content shifts right by EXTEND_DAYS * dayWidth pixels.
        requestAnimationFrame(() => {
          if (scrollRef.current) {
            scrollRef.current.scrollLeft += EXTEND_DAYS * dayWidth;
          }
        });
      }
    }
    el.addEventListener('scroll', onScroll, { passive: true });
    return () => el.removeEventListener('scroll', onScroll);
  }, [dayWidth]);

  // Flat, de-duplicated list for the "single" consolidated timeline.
  const singleEntries = (() => {
    const seen = new Set<string>();
    const out: { task: Task; parentTrack: Track; isShared: boolean; allTrackIds: string[] }[] = [];
    for (const track of tracksWithTasks) {
      for (const e of tasksForTrack(track.id)) {
        if (seen.has(e.task.id)) continue;
        seen.add(e.task.id);
        out.push(e);
      }
    }
    return out;
  })();

  const dailyGridline = `repeating-linear-gradient(90deg, transparent, transparent ${
    dayWidth - 1
  }px, rgba(148,163,184,0.06) ${dayWidth - 1}px, rgba(148,163,184,0.06) ${dayWidth}px)`;

  return (
    <div
      ref={scrollRef}
      onPointerDown={onPanDown}
      onPointerMove={onPanMove}
      onPointerUp={onPanUp}
      onPointerCancel={onPanUp}
      className={`w-full h-full overflow-auto rounded-xl border border-slate-700/50 bg-slate-950/40 select-none ${
        panning ? 'cursor-grabbing' : 'cursor-grab'
      }`}
    >
      <div className="min-w-fit">
        {/* Header */}
        <div className="sticky top-0 z-30 flex border-b border-slate-700/50 bg-slate-950/95 backdrop-blur">
          {/* Corner: label + zoom */}
          <div className="sticky left-0 z-40 w-48 shrink-0 border-r border-slate-700/50 bg-slate-950/95 flex flex-col justify-center gap-1 px-3 py-1">
            <div className="flex items-center justify-between gap-1">
              <button
                type="button"
                onClick={scrollToToday}
                className="text-[10px] px-2 py-0.5 rounded bg-cyan-700 hover:bg-cyan-600 text-white"
                title="Scroll to today"
              >
                Today
              </button>
              <div className="flex gap-0.5">
                {(['regular', 'condensed'] as const).map((m) => (
                  <button
                    key={m}
                    type="button"
                    onClick={() => setMode(m)}
                    className={`text-[9px] px-1.5 py-0.5 rounded ${
                      mode === m
                        ? 'bg-cyan-700 text-white'
                        : 'bg-slate-800 text-slate-300 hover:bg-slate-700'
                    }`}
                  >
                    {m === 'regular' ? 'Regular' : 'Condensed'}
                  </button>
                ))}
                {mode === 'condensed' && (
                  <select
                    value={condensedStyle}
                    onChange={(e) => setCondensedStyle(e.target.value as 'dots' | 'waves' | 'tree')}
                    className="text-[9px] px-1 py-0.5 rounded bg-slate-800 text-slate-300 border-0 cursor-pointer"
                  >
                    <option value="dots">Dots</option>
                    <option value="waves">Waves</option>
                    <option value="tree">Tree</option>
                  </select>
                )}
              </div>
            </div>
            <div className="flex items-center gap-1">
              <button
                type="button"
                onClick={zoomOut}
                className="w-5 h-5 flex items-center justify-center rounded bg-slate-800 hover:bg-slate-700 text-slate-300 text-xs leading-none"
                title="Zoom out"
              >
                −
              </button>
              <span className="text-[9px] text-slate-500 w-10 text-center font-mono">
                {dayWidth}px
              </span>
              <button
                type="button"
                onClick={zoomIn}
                className="w-5 h-5 flex items-center justify-center rounded bg-slate-800 hover:bg-slate-700 text-slate-300 text-xs leading-none"
                title="Zoom in"
              >
                +
              </button>
              <span className="mx-1 text-slate-700">|</span>
              <span className="text-[9px] text-slate-500">H</span>
              <button
                type="button"
                onClick={shorter}
                className="w-5 h-5 flex items-center justify-center rounded bg-slate-800 hover:bg-slate-700 text-slate-300 text-xs leading-none"
                title="Shorter events"
              >
                −
              </button>
              <span className="text-[9px] text-slate-500 w-5 text-center font-mono">
                {laneHeight}
              </span>
              <button
                type="button"
                onClick={taller}
                className="w-5 h-5 flex items-center justify-center rounded bg-slate-800 hover:bg-slate-700 text-slate-300 text-xs leading-none"
                title="Taller events"
              >
                +
              </button>
            </div>
          </div>

          {/* Month + day rows */}
          <div className="relative" style={{ width: totalDays * dayWidth }}>
            <div className="relative border-b border-slate-700/40" style={{ height: 24 }}>
              {monthMarkers.map((m, i) => (
                <div
                  key={i}
                  className="absolute top-0 h-full flex items-center px-2 text-xs font-semibold text-slate-300 border-l border-slate-700/40"
                  style={{ left: m.offset * dayWidth }}
                >
                  {m.label}
                </div>
              ))}
            </div>
            <div className="relative" style={{ height: 18 }}>
              {dayHeaders
                .filter((d) => d.offset % dayStep === 0)
                .map((d) => (
                  <div
                    key={d.offset}
                    className={`absolute top-0 h-full flex items-center justify-center text-[9px] border-l border-slate-800/40 ${
                      d.isToday ? 'text-cyan-300 font-bold' : d.isWeekend ? 'text-slate-600' : 'text-slate-500'
                    }`}
                    style={{ left: d.offset * dayWidth, width: dayWidth * dayStep }}
                  >
                    {d.day}
                  </div>
                ))}
            </div>
            {/* Today line spanning the whole header */}
            {todayOffset >= 0 && todayOffset <= totalDays && (
              <div
                className="absolute top-0 bottom-0 w-0.5 bg-cyan-400/80 z-20 pointer-events-none"
                style={{ left: todayOffset * dayWidth + dayWidth / 2 }}
              >
                <span className="absolute -top-0 -translate-y-full text-[9px] font-bold text-cyan-300 bg-slate-900 px-1 rounded">
                  TODAY
                </span>
              </div>
            )}
          </div>
        </div>

        {/* Body */}
        {mode === 'regular' ? (
          <div className="relative">
            {tracksWithTasks.map((track) => {
              const tasksOnThisTrack = tasksForTrack(track.id);
              return (
                <div
                  key={track.id}
                  className="flex border-b border-slate-800/60 hover:bg-slate-900/30"
                  style={{ height: laneHeight }}
                >
                  <TrackLabel track={track} />
                  <TrackLane
                    track={track}
                    tasksOnThisTrack={tasksOnThisTrack}
                    rangeStart={rangeStart}
                    totalDays={totalDays}
                    todayDate={todayDate}
                    todayOffset={todayOffset}
                    weekTicks={weekTicks}
                    selectedTaskId={selectedTaskId}
                    trackById={trackById}
                    onSelectTask={onSelectTask}
                    dayWidth={dayWidth}
                    laneHeight={laneHeight}
                    dailyGridline={dailyGridline}
                    onResizeGuide={setResizeGuide}
                  />
                </div>
              );
            })}
            {/* Resize guide: full-height line + date pill at the day boundary
                the dragged edge lands on. 192px = sticky w-48 label column. */}
            {resizeGuide !== null && (
              <div
                className="absolute top-0 bottom-0 z-40 pointer-events-none"
                style={{ left: 192 + resizeGuide.offset * dayWidth }}
              >
                <div
                  className="absolute top-0 bottom-0 w-0.5 bg-amber-300"
                  style={{ boxShadow: '0 0 8px 1px rgba(252,211,77,0.9)' }}
                />
                <div className="absolute top-1 left-1.5 whitespace-nowrap text-[10px] font-bold bg-amber-300 text-slate-950 px-1.5 py-0.5 rounded shadow">
                  {resizeGuide.label}
                </div>
              </div>
            )}
          </div>
        ) : (
          <SingleTimelineBody
            title={data.title}
            style={condensedStyle}
            entries={singleEntries}
            rangeStart={rangeStart}
            totalDays={totalDays}
            dayWidth={dayWidth}
            laneHeight={laneHeight}
            todayOffset={todayOffset}
            weekTicks={weekTicks}
            dailyGridline={dailyGridline}
            selectedTaskId={selectedTaskId}
            trackById={trackById}
            onSelectTask={onSelectTask}
          />
        )}
      </div>
    </div>
  );
}

// ── Sub-components ───────────────────────────────────────────────────────

function TrackLabel({ track }: { track: Track }) {
  return (
    <div className="sticky left-0 z-20 w-48 shrink-0 border-r border-slate-700/50 bg-slate-950/95 backdrop-blur flex items-center gap-2 px-3">
      <span
        className="w-2.5 h-2.5 rounded-full shrink-0"
        style={{ background: track.color, opacity: track.special ? 0.7 : 1 }}
      />
      <span className="text-base leading-none">
        {glyphFor(track.icon)}
      </span>
      <span
        className={`text-xs font-medium truncate ${
          track.special ? 'text-cyan-300 italic' : 'text-slate-200'
        }`}
      >
        {track.name}
      </span>
    </div>
  );
}

interface TrackLaneProps {
  track: Track;
  tasksOnThisTrack: {
    task: Task;
    parentTrack: Track;
    isShared: boolean;
    allTrackIds: string[];
  }[];
  rangeStart: Date;
  totalDays: number;
  todayDate: Date;
  todayOffset: number;
  weekTicks: number[];
  selectedTaskId: string | null;
  trackById: Map<string, Track>;
  onSelectTask: (id: string) => void;
  dayWidth: number;
  laneHeight: number;
  dailyGridline: string;
  onResizeGuide: (guide: { offset: number; label: string } | null) => void;
}

function TrackLane(props: TrackLaneProps) {
  const {
    track,
    tasksOnThisTrack,
    rangeStart,
    totalDays,
    todayDate,
    todayOffset,
    weekTicks,
    selectedTaskId,
    trackById,
    onSelectTask,
    dayWidth,
    laneHeight,
    dailyGridline,
    onResizeGuide,
  } = props;

  return (
    <div
      className="relative"
      style={{ width: totalDays * dayWidth, backgroundImage: dailyGridline }}
    >
      {/* Stronger weekly ticks */}
      {weekTicks.map((offset, i) => (
        <div
          key={i}
          className="absolute top-0 bottom-0 w-px bg-slate-700/30"
          style={{ left: offset * dayWidth }}
        />
      ))}

      {/* Past-day greyed overlay */}
      {todayOffset > 0 && (
        <div
          className="absolute top-0 bottom-0 bg-slate-800/40 pointer-events-none"
          style={{ left: 0, width: Math.min(todayOffset, totalDays) * dayWidth }}
        />
      )}

      {/* Today line in the lane */}
      {todayOffset >= 0 && todayOffset <= totalDays && (
        <div
          className="absolute top-0 bottom-0 w-0.5 bg-cyan-400/60 pointer-events-none"
          style={{ left: todayOffset * dayWidth + dayWidth / 2 }}
        />
      )}

      {/* Tasks */}
      {tasksOnThisTrack.map(({ task, parentTrack, isShared, allTrackIds }) => (
        <TaskChip
          key={`${track.id}-${task.id}`}
          task={task}
          parentTrack={parentTrack}
          laneTrack={track}
          isShared={isShared}
          allTrackIds={allTrackIds}
          rangeStart={rangeStart}
          totalDays={totalDays}
          todayDate={todayDate}
          isSelected={task.id === selectedTaskId}
          trackById={trackById}
          onSelectTask={onSelectTask}
          dayWidth={dayWidth}
          laneHeight={laneHeight}
          onResizeGuide={onResizeGuide}
        />
      ))}
    </div>
  );
}

/** Lighten a hex color toward white by `amt` (0..1) → pastel tint. */
function lighten(hex: string, amt: number): string {
  const h = hex.replace('#', '');
  const r = parseInt(h.slice(0, 2), 16);
  const g = parseInt(h.slice(2, 4), 16);
  const b = parseInt(h.slice(4, 6), 16);
  return `rgb(${Math.round(r + (255 - r) * amt)}, ${Math.round(g + (255 - g) * amt)}, ${Math.round(b + (255 - b) * amt)})`;
}

interface TaskChipProps {
  task: Task;
  parentTrack: Track;
  laneTrack: Track;
  isShared: boolean;
  allTrackIds: string[];
  rangeStart: Date;
  totalDays: number;
  todayDate: Date;
  isSelected: boolean;
  trackById: Map<string, Track>;
  onSelectTask: (id: string) => void;
  dayWidth: number;
  laneHeight: number;
  onResizeGuide: (guide: { offset: number; label: string } | null) => void;
}

function TaskChip(props: TaskChipProps) {
  const {
    task,
    parentTrack,
    laneTrack,
    isShared,
    allTrackIds,
    rangeStart,
    totalDays,
    todayDate,
    isSelected,
    trackById,
    onSelectTask,
    dayWidth,
    laneHeight,
    onResizeGuide,
  } = props;

  const updateTask = useMoveStore((s) => s.updateTask);

  // Hooks must run before any early return (rules-of-hooks).
  const dragRef = useRef<{
    mode: 'move' | 'l' | 'r';
    startX: number;
    origStart: number;
    origEnd: number;
    moved: boolean;
  } | null>(null);
  const chipElRef = useRef<HTMLDivElement>(null);
  const [preview, setPreview] = useState<{ s: number; e: number } | null>(null);
  const [dragging, setDragging] = useState(false);

  const layoutStart = getLayoutStart(task);
  if (task.startDate === 'AUTO' && !layoutStart) return null;
  if (!layoutStart) return null;
  const stableLayoutStart = layoutStart;

  const layoutEnd = getLayoutEnd(task);
  const whiskerStart = getWhiskerStart(task);
  const isHammock = !!laneTrack.special;
  const fuzzyStart = isFuzzyStart(task);
  const fuzzyEnd = isFuzzyEnd(task);
  const hasWhiskerZone = hasWhisker(task);
  const fadeIn = fuzzyStart && !!task.fuzzy?.fadeIn;

  const startOffset = daysBetween(rangeStart, stableLayoutStart);
  const endOffset = layoutEnd ? daysBetween(rangeStart, layoutEnd) + 1 : totalDays;

  const whiskerOffset = whiskerStart ? daysBetween(rangeStart, whiskerStart) : null;

  // ── Drag / resize gating ── only hard-date, non-fuzzy, non-hammock tasks.
  const draggable =
    !isHammock && !fuzzyStart && !fuzzyEnd && !hasWhiskerZone && DATE_RE.test(task.startDate);
  const canResizeR = draggable && !!task.dueDate && DATE_RE.test(task.dueDate);

  const activeStart = preview ? preview.s : startOffset;
  const activeEnd = preview ? preview.e : endOffset;

  function beginDrag(e: React.PointerEvent, mode: 'move' | 'l' | 'r') {
    if (mode === 'r' && !canResizeR) return;
    if (!draggable) return;
    e.stopPropagation();
    e.preventDefault();
    chipElRef.current?.setPointerCapture(e.pointerId);
    dragRef.current = {
      mode,
      startX: e.clientX,
      origStart: startOffset,
      origEnd: endOffset,
      moved: false,
    };
  }

  function onPointerMove(e: React.PointerEvent) {
    const d = dragRef.current;
    if (!d) return;
    const dx = e.clientX - d.startX;
    if (Math.abs(dx) > 3 && !d.moved) {
      d.moved = true;
      setDragging(true);
    }
    const deltaDays = Math.round(dx / dayWidth);
    let s = d.origStart;
    let en = d.origEnd;
    if (d.mode === 'move') {
      const shift = deltaDays;
      s = d.origStart + shift;
      en = d.origEnd + shift;
      if (s < 0) {
        en += -s;
        s = 0;
      }
    } else if (d.mode === 'l') {
      s = Math.max(0, Math.min(d.origStart + deltaDays, d.origEnd - 1));
    } else {
      en = Math.max(d.origStart + 1, d.origEnd + deltaDays);
    }
    setPreview({ s, e: en });
    // Guide line at the edge being resized (left edge of the start day /
    // right edge of the last day) so the user sees the day they stop on.
    if (d.mode === 'l') {
      onResizeGuide({ offset: s, label: fmtDate(addDays(rangeStart, s)) });
    } else if (d.mode === 'r') {
      // `en` is exclusive; the day the task now ends on is en - 1.
      onResizeGuide({ offset: en, label: fmtDate(addDays(rangeStart, en - 1)) });
    }
  }

  function endDrag(e: React.PointerEvent) {
    onResizeGuide(null);
    const d = dragRef.current;
    try {
      if (chipElRef.current?.hasPointerCapture(e.pointerId)) {
        chipElRef.current.releasePointerCapture(e.pointerId);
      }
    } catch {
      /* noop */
    }
    dragRef.current = null;
    setDragging(false);
    if (!d) {
      onSelectTask(task.id);
      return;
    }
    if (!d.moved) {
      setPreview(null);
      onSelectTask(task.id);
      return;
    }
    const p = preview;
    setPreview(null);
    if (!p) {
      onSelectTask(task.id);
      return;
    }
    const patch: Partial<Task> = {};
    if (d.mode === 'move') {
      const shift = p.s - d.origStart;
      patch.startDate = fmtDate(addDays(stableLayoutStart, shift));
      if (layoutEnd) patch.dueDate = fmtDate(addDays(layoutEnd, shift));
    } else if (d.mode === 'l') {
      patch.startDate = fmtDate(addDays(stableLayoutStart, p.s - d.origStart));
    } else if (layoutEnd) {
      patch.dueDate = fmtDate(addDays(layoutEnd, p.e - d.origEnd));
    }
    if (Object.keys(patch).length) updateTask(task.id, patch);
  }

  // ── Visual styling ──
  const left = activeStart * dayWidth;
  const width = Math.max((activeEnd - activeStart) * dayWidth, dayWidth);
  const isPast = !isHammock && !fuzzyStart && stableLayoutStart < todayDate;
  const chipColor = laneTrack.color;
  const stripeColors = allTrackIds
    .map((id) => trackById.get(id)?.color)
    .filter(Boolean) as string[];
  const stripeGradient =
    stripeColors.length > 1
      ? `linear-gradient(90deg, ${stripeColors.join(', ')})`
      : stripeColors[0] ?? parentTrack.color;

  // Box graph mode: start box + line + end box + pastel label bubble.
  // Applies to normal hard-date tasks; hammock/fuzzy/whisker keep the old fill.
  const boxMode = !isHammock && !fuzzyStart && !fuzzyEnd && !hasWhiskerZone;
  const openEnd = task.dueDate === null && !layoutEnd;
  const fadeDays = task.fadeDays ?? 0;
  const lineFading = openEnd || fadeDays > 0;

  // Fade-out chip: compute gradient fade percent for the connecting line.
  const totalSpan = endOffset - startOffset;
  const fadePct = fadeDays > 0 && totalSpan > 0
    ? Math.max(0, Math.min(100, ((totalSpan - fadeDays) / totalSpan) * 100))
    : 100;

  let background: string;
  if (boxMode) {
    background = 'transparent'; // box graph draws its own boxes + line
  } else if (isHammock) {
    background = `linear-gradient(90deg, ${chipColor}cc 0%, ${chipColor}55 50%, ${chipColor}00 100%)`;
  } else if (hasWhiskerZone && whiskerOffset !== null) {
    const whiskerPct = ((whiskerOffset - startOffset) / Math.max(1, endOffset - startOffset)) * 100;
    background = `linear-gradient(90deg, ${chipColor}cc 0%, ${chipColor}cc ${whiskerPct}%, ${chipColor}44 ${whiskerPct}%, ${chipColor}11 100%)`;
  } else if (fuzzyEnd) {
    background = `linear-gradient(90deg, ${chipColor}cc 0%, ${chipColor}77 70%, ${chipColor}33 100%)`;
  } else if (fadeIn) {
    background = `linear-gradient(90deg, ${chipColor}44 0%, ${chipColor}aa 50%, ${chipColor}ee 100%)`;
  } else if (isPast) {
    background = `${chipColor}55`;
  } else {
    background = `${chipColor}dd`;
  }

  const isForeignShared = isShared && parentTrack.id !== laneTrack.id;
  const labelBubbleBg = lighten(chipColor, 0.82); // pastel tint
  const labelBubbleText = '#0f172a'; // slate-950 (dark)

  const tooltipLines: string[] = [task.title];
  if (isShared) {
    tooltipLines.push(`Shared across: ${allTrackIds.map((id) => trackById.get(id)?.name ?? id).join(', ')}`);
  }
  if (fuzzyStart) tooltipLines.push(fadeIn ? 'Start: fade in (?)' : 'Start: ? (fuzzy)');
  else tooltipLines.push(`Start: ${task.startDate === 'AUTO' ? '(auto)' : task.startDate}`);
  if (fuzzyEnd) tooltipLines.push('End: ? (fuzzy)');
  else if (task.dueDate) tooltipLines.push(`End: ${task.dueDate}`);
  else tooltipLines.push('End: ∞ (fade out)');
  if (!isHammock && !fuzzyStart && !fuzzyEnd) {
    tooltipLines.push(draggable ? 'Drag to move · drag edges to resize' : '');
  }

  // Live date readout while dragging.
  const newStartDate = addDays(stableLayoutStart, activeStart - startOffset);
  const newEndDate = layoutEnd ? addDays(layoutEnd, activeEnd - endOffset) : null;

  // Box graph dimensions
  const chipH = Math.max(laneHeight - 14, 22);
  const boxW = Math.max(3, Math.min(16, Math.floor(width / 3)));
  const connH = 2;
  const connTop = (chipH - connH) / 2;

  // Connecting line background (with optional fade-out gradient).
  const connBg = lineFading
    ? fadeDays > 0
      ? `linear-gradient(90deg, ${chipColor}cc 0%, ${chipColor}cc ${fadePct}%, transparent 100%)`
      : `linear-gradient(90deg, ${chipColor}cc, transparent)` // openEnd: full fade
    : `${chipColor}88`;

  // titleStart/titleEnd display strings.
  const titleStart = task.titleStart ?? null;
  const titleEnd = task.titleEnd ?? null;

  return (
    <div
      ref={chipElRef}
      role="button"
      tabIndex={0}
      onPointerDown={(e) => beginDrag(e, 'move')}
      onPointerMove={onPointerMove}
      onPointerUp={endDrag}
      onPointerCancel={endDrag}
      onDragStart={(e) => e.preventDefault()}
      className={`absolute top-1/2 -translate-y-1/2 rounded-md text-left text-[10px] px-1.5 py-1 select-none ${
        dragging ? 'z-40 cursor-grabbing' : 'hover:z-30'
      } ${draggable && !dragging ? 'cursor-grab' : ''}`}
      style={{
        left: left + 1,
        width: width - 2,
        height: chipH,
        background: boxMode ? 'transparent' : background,
        border: boxMode
          ? 'none'
          : `1px solid ${chipColor}`,
        borderStyle: !boxMode && isShared ? 'dashed' : 'solid',
        borderWidth: boxMode ? 0 : isShared ? 1.5 : 1,
        opacity: isPast && !isHammock ? 0.7 : 1,
        boxShadow: dragging
          ? `0 0 0 2px white, 0 6px 16px rgba(0,0,0,0.5)`
          : isSelected
          ? `0 0 0 2px white, 0 0 12px ${chipColor}`
          : isShared
          ? `0 0 0 1px ${chipColor}55, 0 0 8px ${chipColor}33`
          : 'none',
        overflow: 'hidden',
        touchAction: 'none',
      }}
      title={tooltipLines.filter(Boolean).join('\n')}
    >
      {/* Drag date readout */}
      {dragging && (
        <div className="absolute -top-5 left-1/2 -translate-x-1/2 whitespace-nowrap text-[9px] bg-cyan-600 text-white px-1.5 py-0.5 rounded shadow z-50 pointer-events-none">
          {fmtDate(newStartDate)} → {newEndDate ? fmtDate(newEndDate) : '∞'}
        </div>
      )}

      {/* ── BOX GRAPH MODE ── */}
      {boxMode && (
        <>
          {/* Start box — solid full-height block at the start day (rounded left).
              Doubles as the left resize grip: it's the visible edge affordance,
              far easier to hit than the 6px sliver handle. */}
          <div
            className="absolute top-0 bottom-0 rounded-l-md"
            style={{
              left: 0,
              width: boxW,
              background: chipColor,
              cursor: draggable ? 'ew-resize' : undefined,
              touchAction: 'none',
            }}
            onPointerDown={draggable ? (e) => beginDrag(e, 'l') : undefined}
          />
          {/* End box — omitted when the end fades out (openEnd / fadeDays).
              Doubles as the right resize grip. */}
          {!lineFading && (
            <div
              className="absolute top-0 bottom-0 rounded-r-md"
              style={{
                right: 0,
                width: boxW,
                background: chipColor,
                cursor: canResizeR ? 'ew-resize' : undefined,
                touchAction: 'none',
              }}
              onPointerDown={canResizeR ? (e) => beginDrag(e, 'r') : undefined}
            />
          )}
          {/* Connecting line — thin 2px between the boxes */}
          <div
            className="absolute"
            style={{
              left: boxW,
              right: lineFading ? 0 : boxW,
              top: connTop,
              height: connH,
              background: connBg,
            }}
          />
          {/* Shared multi-color stripe on the connecting line */}
          {isShared && stripeColors.length > 1 && (
            <div
              className="absolute h-[2px]"
              style={{
                left: boxW,
                right: lineFading ? 0 : boxW,
                top: connTop,
                background: stripeGradient,
                opacity: 0.6,
              }}
            />
          )}
          {/* Label bubble — pastel pill, only behind the label text (not spanning the whole gap) */}
          <div
            className="absolute flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-medium whitespace-nowrap"
            style={{
              left: '50%',
              top: '50%',
              transform: 'translate(-50%, -50%)',
              background: labelBubbleBg,
              color: labelBubbleText,
              maxWidth: `calc(100% - ${boxW * 2 + 4}px)`,
              overflow: 'hidden',
              textOverflow: 'ellipsis',
            }}
          >
            {isShared && (
              <span className="text-[9px] shrink-0" aria-label="Shared">
                {isForeignShared ? '🔗' : '⛓'}
              </span>
            )}
            {/* titleStart near start box */}
            {titleStart && (
              <span className="text-[9px] text-slate-500 shrink-0">{titleStart}</span>
            )}
            <span className="truncate">{task.title || titleEnd || ''}</span>
            {/* titleEnd near end box */}
            {titleEnd && (
              <span className="text-[9px] text-slate-500 shrink-0">{titleEnd}</span>
            )}
          </div>
          {/* Fade-out label near the start box (when there's no end box) */}
          {lineFading && (
            <span
              className="absolute right-1 top-1/2 -translate-y-1/2 text-[8px] text-slate-500"
            >
              ∞
            </span>
          )}
        </>
      )}

      {/* ── NON-BOX-GRAPH MODE (hammock, fuzzy, whisker, etc.) ── */}
      {!boxMode && (
        <>
          {/* Multi-color top stripe for shared tasks */}
          {isShared && (
            <div
              className="absolute top-0 left-0 right-0 h-[3px] rounded-t-md"
              style={{ background: stripeGradient }}
            />
          )}

          {/* Whisker zone overlay */}
          {hasWhiskerZone && whiskerOffset !== null && (
            <WhiskerOverlay
              chipColor={chipColor}
              whiskerOffsetPx={(whiskerOffset - startOffset) * dayWidth}
              endOffsetPx={(endOffset - startOffset) * dayWidth}
            />
          )}

          {/* Fuzzy-end marker */}
          {fuzzyEnd && !hasWhiskerZone && (
            <span
              className="absolute right-0 top-1/2 -translate-y-1/2 text-[12px] font-bold px-1"
              style={{ color: chipColor, background: 'rgba(0,0,0,0.4)' }}
            >
              ?
            </span>
          )}
          {/* Fuzzy-start marker (hard '?'); fade-in uses the gradient instead. */}
          {fuzzyStart && !fadeIn && (
            <span
              className="absolute left-0 top-1/2 -translate-y-1/2 text-[12px] font-bold px-1"
              style={{ color: chipColor, background: 'rgba(0,0,0,0.4)' }}
            >
              ?
            </span>
          )}

          <div
            className={`flex items-center gap-1 truncate font-medium ${
              isPast && !isHammock ? 'text-slate-300' : 'text-slate-950'
            }`}
          >
            {isShared && (
              <span className="text-[9px] shrink-0" aria-label="Shared">
                {isForeignShared ? '🔗' : '⛓'}
              </span>
            )}
            <span className="truncate">{task.title}</span>
          </div>
          {!isHammock && (
            <div className={`text-[8px] ${isPast ? 'text-slate-400' : 'text-slate-900/70'}`}>
              {fuzzyStart ? (fadeIn ? '≈ → ' : '? → ') : `${task.startDate === 'AUTO' ? '' : task.startDate.slice(5)} → `}
              {fuzzyEnd ? '?' : task.dueDate ? task.dueDate.slice(5) : '∞'}
              {isShared && allTrackIds.length > 1 && (
                <span className="ml-1 text-slate-700/80">· {allTrackIds.length} tracks</span>
              )}
            </div>
          )}
          {isHammock && <div className="text-[8px] text-cyan-200/70 italic">fades to ∞</div>}
        </>
      )}

      {/* Stages progress bar (sub-events) — works for both modes */}
      {!!task.stages?.length && (
        <div className="absolute bottom-0 left-1 right-1 h-1 flex gap-px">
          {task.stages.map((s) => (
            <div
              key={s.id}
              className="flex-1 rounded-sm"
              style={{ background: s.done ? chipColor : `${chipColor}55` }}
              title={`${s.title}${s.done ? ' ✓' : ''}`}
            />
          ))}
        </div>
      )}

      {/* Resize handles */}
      {draggable && (
        <div
          onPointerDown={(e) => beginDrag(e, 'l')}
          className="absolute left-0 top-0 bottom-0 w-1.5 cursor-ew-resize hover:bg-white/40"
          style={{ touchAction: 'none' }}
        />
      )}
      {draggable && canResizeR && (
        <div
          onPointerDown={(e) => beginDrag(e, 'r')}
          className="absolute right-0 top-0 bottom-0 w-1.5 cursor-ew-resize hover:bg-white/40"
          style={{ touchAction: 'none' }}
        />
      )}
    </div>
  );
}

function WhiskerOverlay({
  chipColor,
  whiskerOffsetPx,
  endOffsetPx,
}: {
  chipColor: string;
  whiskerOffsetPx: number;
  endOffsetPx: number;
}) {
  const width = Math.max(endOffsetPx - whiskerOffsetPx, 4);
  const boxStart = whiskerOffsetPx + width * 0.25;
  const boxWidth = width * 0.5;
  const median = whiskerOffsetPx + width * 0.5;

  return (
    <svg className="absolute inset-0 pointer-events-none" width="100%" height="100%" preserveAspectRatio="none">
      <line x1={whiskerOffsetPx} y1="20%" x2={endOffsetPx - 2} y2="20%" stroke={chipColor} strokeWidth="1" strokeOpacity="0.6" />
      <line x1={whiskerOffsetPx} y1="80%" x2={endOffsetPx - 2} y2="80%" stroke={chipColor} strokeWidth="1" strokeOpacity="0.6" />
      <line x1={whiskerOffsetPx} y1="20%" x2={whiskerOffsetPx} y2="80%" stroke={chipColor} strokeWidth="1" strokeOpacity="0.6" />
      <line x1={endOffsetPx - 2} y1="20%" x2={endOffsetPx - 2} y2="80%" stroke={chipColor} strokeWidth="1" strokeOpacity="0.6" />
      <rect x={boxStart} y="30%" width={boxWidth} height="40%" fill={chipColor} fillOpacity="0.25" stroke={chipColor} strokeWidth="0.8" strokeOpacity="0.6" />
      <line x1={median} y1="28%" x2={median} y2="72%" stroke={chipColor} strokeWidth="1.2" strokeOpacity="0.9" />
    </svg>
  );
}

// ── Pure helpers ─────────────────────────────────────────────────────────

function buildMonthMarkers(rangeStart: Date, rangeEnd: Date) {
  const markers: { label: string; offset: number }[] = [];
  const d = new Date(rangeStart);
  d.setDate(1);
  if (d < rangeStart) d.setMonth(d.getMonth() + 1);
  while (d <= rangeEnd) {
    const offset = daysBetween(rangeStart, d);
    markers.push({ label: d.toLocaleString('en-US', { month: 'short', year: 'numeric' }), offset });
    d.setMonth(d.getMonth() + 1);
  }
  return markers;
}

function buildWeekTicks(rangeStart: Date, rangeEnd: Date) {
  const ticks: number[] = [];
  const d = new Date(rangeStart);
  while (d.getDay() !== 0) d.setDate(d.getDate() + 1);
  while (d <= rangeEnd) {
    ticks.push(daysBetween(rangeStart, d));
    d.setDate(d.getDate() + 7);
  }
  return ticks;
}
