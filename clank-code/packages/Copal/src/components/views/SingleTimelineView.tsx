'use client';

import { useMemo } from 'react';
import { daysBetween } from '@/lib/dates';
import { getLayoutStart, getLayoutEnd } from '@/lib/fuzzy';
import { glyphFor, type Task, type Track } from '@/lib/types';

interface Entry {
  task: Task;
  parentTrack: Track;
  isShared: boolean;
  allTrackIds: string[];
}

export type CondensedStyle = 'dots' | 'waves' | 'tree';

interface Props {
  entries: Entry[];
  rangeStart: Date;
  totalDays: number;
  dayWidth: number;
  laneHeight: number;
  todayOffset: number;
  weekTicks: number[];
  dailyGridline: string;
  selectedTaskId: string | null;
  trackById: Map<string, Track>;
  onSelectTask: (id: string) => void;
  style?: CondensedStyle;
  title?: string;
}

const LANE_H = 44;
const WAVE_AMP = 5; // px above/below the lane centerline
const WAVE_WAVELENGTH = 26; // px per full sine cycle

/** Build a smooth horizontal sine-wave polyline from x1→x2 at baseline y. */
function wavePath(x1: number, x2: number, y: number, amp: number, wavelength: number): string {
  if (x2 - 1 <= x1) return `M ${x1.toFixed(1)} ${y} L ${x2.toFixed(1)} ${y}`;
  const segLen = Math.max(3, wavelength / 4);
  let d = '';
  for (let x = x1; x <= x2; x += segLen) {
    const phase = ((x - x1) / wavelength) * Math.PI * 2;
    const yy = y + Math.sin(phase) * amp;
    d += `${d ? ' L' : 'M'} ${x.toFixed(1)} ${yy.toFixed(1)}`;
  }
  // Close to exact end point
  const phaseEnd = ((x2 - x1) / wavelength) * Math.PI * 2;
  d += ` L ${x2.toFixed(1)} ${(y + Math.sin(phaseEnd) * amp).toFixed(1)}`;
  return d;
}

/**
 * Condensed timeline — ONE central rail (named with the shared move title).
 *
 * Three selectable render styles for how each event's start→end is shown:
 *  - "dots":  a point on the rail at the start date + staggered label above
 *             (the original history-book timeline).
 *  - "waves": a smooth sine-wave line from the event's start to its end,
 *             floating in its lane above the rail.
 *  - "tree":  a branch leaving the central rail at the event's start and
 *             curving up/out to its end like a tree limb.
 *
 * Shared/linked tasks are already filtered out upstream (singleEntries).
 */
export function SingleTimelineBody(props: Props) {
  const {
    title,
    style = 'dots',
    entries,
    rangeStart,
    totalDays,
    dayWidth,
    todayOffset,
    weekTicks,
    dailyGridline,
    selectedTaskId,
    onSelectTask,
  } = props;

  const { items, numLanes } = useMemo(() => {
    type Item = Entry & { startOffset: number; endOffset: number; lane: number; labelW: number };
    const raw: Item[] = [];
    for (const e of entries) {
      const ls = getLayoutStart(e.task);
      if (!ls) continue;
      const le = getLayoutEnd(e.task);
      const startOffset = daysBetween(rangeStart, ls);
      const endOffset = le ? daysBetween(rangeStart, le) + 1 : startOffset + 2;
      const labelW = Math.max(96, e.task.title.length * 6 + 40);
      raw.push({
        ...e,
        startOffset,
        endOffset: Math.max(endOffset, startOffset + 1),
        lane: 0,
        labelW,
      });
    }
    raw.sort((a, b) => a.startOffset - b.startOffset || a.endOffset - b.endOffset);

    // Pack labels into lanes by x-range so they don't overlap.
    const laneEnd: number[] = [];
    for (const it of raw) {
      const startX = it.startOffset * dayWidth;
      let placed = false;
      for (let i = 0; i < laneEnd.length; i++) {
        if (laneEnd[i] + 10 <= startX) {
          it.lane = i;
          laneEnd[i] = startX + it.labelW;
          placed = true;
          break;
        }
      }
      if (!placed) {
        it.lane = laneEnd.length;
        laneEnd.push(startX + it.labelW);
      }
    }
    return { items: raw, numLanes: Math.max(laneEnd.length, 1) };
  }, [entries, rangeStart, dayWidth]);

  const railY = numLanes * LANE_H + 22;
  const height = railY + 34;
  const width = totalDays * dayWidth;

  // In dots mode the central rail is overpainted by the LONGEST event's color
  // (tie → label closest to the rail = highest lane index). Other modes keep
  // the neutral slate rail (waves/tree cover it themselves).
  const railColor =
    style === 'dots' && items.length
      ? items.reduce((best, it) => {
          const dur = it.endOffset - it.startOffset;
          const bd = best.endOffset - best.startOffset;
          return dur > bd || (dur === bd && it.lane > best.lane) ? it : best;
        }).parentTrack.color
      : null;
  const railBg = railColor ?? 'rgba(71,85,105,0.6)';

  return (
    <div className="flex">
      {/* Left spacer keeps the rail aligned under the header's date axis.
          It also carries the rail's name — the move title (same string the
          page header uses, passed in as `title`, never hardcoded here). */}
      <div className="sticky left-0 z-20 w-48 shrink-0 self-stretch border-r border-slate-700/50 bg-slate-950/95">
        <div
          className="absolute right-2 max-w-[176px] text-right text-[11px] font-bold leading-tight text-slate-100"
          style={{ top: railY, transform: 'translateY(-50%)' }}
        >
          {title}
        </div>
      </div>
      <div className="relative" style={{ width, height, backgroundImage: dailyGridline }}>
        {/* weekly ticks */}
        {weekTicks.map((o, i) => (
          <div
            key={i}
            className="absolute top-0 bottom-0 w-px bg-slate-700/15"
            style={{ left: o * dayWidth }}
          />
        ))}
        {/* today line */}
        {todayOffset >= 0 && todayOffset <= totalDays && (
          <div
            className="absolute top-0 bottom-0 w-0.5 bg-cyan-400/60 pointer-events-none z-10"
            style={{ left: todayOffset * dayWidth + dayWidth / 2 }}
          />
        )}
        {/* the central rail (one line) — in dots mode it takes the longest event's color */}
        <div
          className="absolute left-0 right-0"
          style={{ top: railY, height: railColor ? 3 : 2, background: railBg }}
        />

        {style === 'dots' &&
          items.map(({ task, parentTrack, isShared, startOffset, lane, labelW }) => {
            const x = startOffset * dayWidth;
            const labelTop = lane * LANE_H;
            const color = parentTrack.color;
            const selected = task.id === selectedTaskId;
            const dateStr = task.startDate === 'FUZZY' ? '?' : task.startDate.slice(5);
            return (
              <div key={task.id} className="absolute" style={{ left: x, top: 0 }}>
                <button
                  type="button"
                  onClick={() => onSelectTask(task.id)}
                  className="absolute text-left"
                  style={{ top: labelTop, width: labelW }}
                >
                  <span className="block text-[10px] font-medium truncate" style={{ color }}>
                    {task.title}
                  </span>
                  <span className="block text-[9px] text-slate-500 truncate">
                    {dateStr} · {parentTrack.name}
                  </span>
                </button>
                <div
                  className="absolute w-px"
                  style={{
                    top: labelTop + 30,
                    height: railY - (labelTop + 30),
                    background: color,
                    opacity: 0.5,
                  }}
                />
                <button
                  type="button"
                  onClick={() => onSelectTask(task.id)}
                  className="absolute rounded-full"
                  style={{
                    top: railY - 5,
                    left: -5,
                    width: 12,
                    height: 12,
                    background: color,
                    boxShadow: selected ? '0 0 0 3px white' : `0 0 0 2px ${color}55`,
                  }}
                  title={task.title}
                />
              </div>
            );
          })}

        {style === 'waves' && (
          <>
            <svg
              className="absolute inset-0"
              width={width}
              height={height}
              style={{ pointerEvents: 'none' }}
            >
              {items.map(({ task, parentTrack, startOffset, endOffset, lane }) => {
                const x1 = startOffset * dayWidth;
                const x2 = Math.max(endOffset * dayWidth, x1 + dayWidth);
                const y = railY; // waves ride the master timeline line
                const color = parentTrack.color;
                const selected = task.id === selectedTaskId;
                const d = wavePath(x1, x2, y, WAVE_AMP, WAVE_WAVELENGTH);
                return (
                  <g key={task.id}>
                    {/* wide invisible hit area */}
                    <path
                      d={d}
                      stroke="transparent"
                      strokeWidth={12}
                      fill="none"
                      strokeLinecap="round"
                      style={{ pointerEvents: 'stroke', cursor: 'pointer' }}
                      onClick={() => onSelectTask(task.id)}
                    >
                      <title>{task.title}</title>
                    </path>
                    {selected && (
                      <path d={d} stroke="white" strokeWidth={6} fill="none" strokeLinecap="round" opacity={0.4} />
                    )}
                    <path
                      d={d}
                      stroke={color}
                      strokeWidth={selected ? 3 : 2}
                      fill="none"
                      strokeLinecap="round"
                    />
                  </g>
                );
              })}
            </svg>
            {items.map(({ task, parentTrack, isShared, startOffset, lane, labelW }) => {
              const x = startOffset * dayWidth;
              const labelTop = lane * LANE_H;
              const color = parentTrack.color;
              const dateStr = task.startDate === 'FUZZY' ? '?' : task.startDate.slice(5);
              return (
                <button
                  key={task.id}
                  type="button"
                  onClick={() => onSelectTask(task.id)}
                  className="absolute text-left"
                  style={{ top: labelTop, left: x, width: labelW }}
                >
                  <span className="block text-[10px] font-medium truncate" style={{ color }}>
                    {task.title}
                  </span>
                  <span className="block text-[9px] text-slate-500 truncate">
                    {dateStr} · {parentTrack.name}
                  </span>
                </button>
              );
            })}
          </>
        )}

        {style === 'tree' && (
          <>
            <svg
              className="absolute inset-0"
              width={width}
              height={height}
              style={{ pointerEvents: 'none' }}
            >
              {items.map(({ task, parentTrack, startOffset, endOffset, lane }) => {
                const x1 = startOffset * dayWidth;
                const x2 = Math.max(endOffset * dayWidth, x1 + dayWidth);
                const y2 = lane * LANE_H + LANE_H / 2;
                const color = parentTrack.color;
                const selected = task.id === selectedTaskId;
                const d = `M ${x1.toFixed(1)} ${railY} Q ${x1.toFixed(1)} ${y2} ${x2.toFixed(1)} ${y2}`;
                return (
                  <g key={task.id}>
                    <path
                      d={d}
                      stroke="transparent"
                      strokeWidth={12}
                      fill="none"
                      strokeLinecap="round"
                      style={{ pointerEvents: 'stroke', cursor: 'pointer' }}
                      onClick={() => onSelectTask(task.id)}
                    >
                      <title>{task.title}</title>
                    </path>
                    {selected && (
                      <path d={d} stroke="white" strokeWidth={6} fill="none" strokeLinecap="round" opacity={0.4} />
                    )}
                    <path
                      d={d}
                      stroke={color}
                      strokeWidth={selected ? 3 : 2}
                      fill="none"
                      strokeLinecap="round"
                    />
                    {/* end bud */}
                    <circle cx={x2} cy={y2} r={selected ? 4 : 3} fill={color} />
                  </g>
                );
              })}
            </svg>
            {items.map(({ task, parentTrack, isShared, startOffset, lane, labelW, endOffset }) => {
              const x = startOffset * dayWidth;
              const labelTop = lane * LANE_H;
              const color = parentTrack.color;
              const dateStr = task.startDate === 'FUZZY' ? '?' : task.startDate.slice(5);
              const endStr = task.dueDate ? task.dueDate.slice(5) : task.fuzzy?.anchorEnd ? '?' : '∞';
              return (
                <button
                  key={task.id}
                  type="button"
                  onClick={() => onSelectTask(task.id)}
                  className="absolute text-left bg-slate-950/85 px-1 rounded z-10"
                  style={{ top: labelTop, left: x, width: labelW }}
                >
                  <span className="block text-[10px] font-medium truncate" style={{ color }}>
                    {task.title}
                  </span>
                  <span className="block text-[9px] text-slate-500 truncate">
                    {dateStr}→{endStr} · {parentTrack.name}
                  </span>
                </button>
              );
            })}
          </>
        )}
      </div>
    </div>
  );
}
