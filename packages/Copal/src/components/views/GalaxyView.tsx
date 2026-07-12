'use client';

import { useMemo, useRef, useEffect, useState } from 'react';
import { useMoveStore } from '@/store/useMoveStore';
import { useResolvedTracks } from '@/hooks/useResolvedTracks';
import { useTagFilter } from '@/hooks/useTagFilter';
import { parseDate } from '@/lib/dates';
import { daysBetween } from '@/lib/dates';
import { glyphFor } from '@/lib/types';
import type { Task, Track } from '@/lib/types';
import { getTaskTrackIds, isSharedTask } from '@/lib/sharing';
import {
  isFuzzyStart,
  isFuzzyEnd,
  hasWhisker,
  getLayoutStart,
  getLayoutEnd,
} from '@/lib/fuzzy';
import { buildConicGradient } from '@/lib/render';

interface GalaxyNode {
  id: string;
  task: Task;
  parentTrack: Track;
  allTrackIds: string[];
  isShared: boolean;
  isHammock: boolean;
  isFuzzyStartTask: boolean;
  isFuzzyEndTask: boolean;
  hasWhisker: boolean;
  x: number;
  y: number;
  radius: number;
  /** Days from today to the layout anchor (start for fuzzy-start, end otherwise). */
  anchorOffsetDays: number;
  /** True if anchor is in the past (only set for non-fuzzy-start tasks). */
  isPast: boolean;
}

const OFFSET_RANGE = 75;

export function GalaxyView({ onSelectTask }: { onSelectTask: (id: string) => void }) {
  const today = useMoveStore((s) => s.today);
  const selectedTaskId = useMoveStore((s) => s.selectedTaskId);
  const { resolvedTracks, trackById } = useResolvedTracks();
  const tagFilter = useTagFilter();
  const containerRef = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({ w: 800, h: 600 });

  // Track container size for responsive layout
  useEffect(() => {
    if (!containerRef.current) return;
    const el = containerRef.current;
    const ro = new ResizeObserver((entries) => {
      for (const e of entries) {
        const cr = e.contentRect;
        setSize({ w: cr.width, h: cr.height });
      }
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const todayDate = useMemo(() => parseDate(today), [today]);

  // Per-track arm offsets
  const trackArmOffset = useMemo(() => {
    const map = new Map<string, number>();
    const n = resolvedTracks.length;
    resolvedTracks.forEach((t, i) => {
      map.set(t.id, (i / Math.max(1, n)) * Math.PI * 2);
    });
    return map;
  }, [resolvedTracks]);

  const trackColorById = useMemo(() => {
    const m = new Map<string, string>();
    resolvedTracks.forEach((t) => m.set(t.id, t.color));
    return m;
  }, [resolvedTracks]);

  // Helper: arm position for a (trackId, offsetDays) pair
  function armPosition(trackId: string, dueOffsetDays: number) {
    const cx = size.w / 2;
    const cy = size.h / 2;
    const maxRadius = Math.min(size.w, size.h) / 2 - 60;
    const minRadius = 60;

    const absOffset = Math.abs(dueOffsetDays);
    const proximity = Math.min(absOffset / OFFSET_RANGE, 1);
    const baseRadius = minRadius + (maxRadius - minRadius) * Math.pow(proximity, 0.7);

    const armOffset = trackArmOffset.get(trackId) ?? 0;
    const angle = armOffset + dueOffsetDays * 0.18;

    return {
      x: cx + baseRadius * Math.cos(angle),
      y: cy + baseRadius * Math.sin(angle),
    };
  }

  const nodes = useMemo<GalaxyNode[]>(() => {
    const allTasks: { task: Task; track: Track }[] = [];
    for (const track of resolvedTracks) {
      if (!track.enabled) continue;
      for (const task of track.tasks) {
        if (!tagFilter.passes(task)) continue;
        allTasks.push({ task, track });
      }
    }

    return allTasks.map(({ task, track }) => {
      const isHammock = !!track.special;
      const fuzzyStart = isFuzzyStart(task);
      const fuzzyEnd = isFuzzyEnd(task);
      const hasWhiskerZone = hasWhisker(task);

      // Compute the anchor offset for gravity positioning.
      // For fuzzy-start tasks: use anchorStart (so the node sits at the conceptual
      // start, not at today, even if today > anchorStart).
      // For non-fuzzy tasks: use dueDate ?? layoutEnd ?? layoutStart.
      let anchorDate: Date | null = null;
      if (isHammock) {
        anchorDate = getLayoutStart(task) ?? todayDate;
      } else if (fuzzyStart) {
        anchorDate = getLayoutStart(task); // fuzzy.anchorStart
      } else if (task.dueDate) {
        anchorDate = parseDate(task.dueDate);
      } else if (fuzzyEnd) {
        anchorDate = getLayoutEnd(task); // fuzzy.anchorEnd
      } else {
        anchorDate = getLayoutStart(task);
      }

      const anchorOffsetDays = anchorDate
        ? daysBetween(todayDate, anchorDate)
        : 30;

      // Past detection: fuzzy-start tasks NEVER grey out
      const isPast = !isHammock && !fuzzyStart && anchorOffsetDays < 0;

      const allTrackIds = getTaskTrackIds(task, track.id);
      const shared = isSharedTask(task) && !isHammock;

      let x: number;
      let y: number;
      if (shared) {
        const enabledIds = allTrackIds.filter((id) => {
          const t = trackById.get(id);
          return t && t.enabled;
        });
        const ids = enabledIds.length ? enabledIds : [track.id];
        const pts = ids.map((id) => armPosition(id, anchorOffsetDays));
        x = pts.reduce((s, p) => s + p.x, 0) / pts.length;
        y = pts.reduce((s, p) => s + p.y, 0) / pts.length;
      } else {
        const p = armPosition(track.id, anchorOffsetDays);
        x = p.x;
        y = p.y;
      }

      const radius = isHammock
        ? 14
        : shared
        ? 12
        : fuzzyStart || fuzzyEnd
        ? 10 // fuzzy tasks slightly larger
        : task.priority === 'high'
        ? 9
        : task.priority === 'medium'
        ? 7
        : 5;

      return {
        id: task.id,
        task,
        parentTrack: track,
        allTrackIds,
        isShared: shared,
        isHammock,
        isFuzzyStartTask: fuzzyStart,
        isFuzzyEndTask: fuzzyEnd,
        hasWhisker: hasWhiskerZone,
        x,
        y,
        radius,
        anchorOffsetDays,
        isPast,
      };
    });
  }, [resolvedTracks, size, todayDate, trackArmOffset, trackById, tagFilter.passes, tagFilter.active]);

  // Hub edges (shared task → each non-parent track arm endpoint)
  const hubEdges = useMemo(() => {
    const edges: {
      from: { x: number; y: number };
      to: { x: number; y: number };
      color: string;
      trackId: string;
      nodeId: string;
    }[] = [];
    for (const n of nodes) {
      if (!n.isShared) continue;
      for (const trackId of n.allTrackIds) {
        const t = trackById.get(trackId);
        if (!t || !t.enabled) continue;
        if (trackId === n.parentTrack.id) continue;
        const p = armPosition(trackId, n.anchorOffsetDays);
        edges.push({
          from: { x: n.x, y: n.y },
          to: p,
          color: t.color,
          trackId,
          nodeId: n.id,
        });
      }
    }
    return edges;
  }, [nodes, trackById, trackArmOffset, size]);

  // Orbit rings
  const rings = useMemo(() => {
    const cx = size.w / 2;
    const cy = size.h / 2;
    const maxRadius = Math.min(size.w, size.h) / 2 - 60;
    const minRadius = 60;
    return [0.25, 0.5, 0.75, 1].map((f) => ({
      r: minRadius + (maxRadius - minRadius) * f,
      cx,
      cy,
    }));
  }, [size]);

  return (
    <div
      ref={containerRef}
      className="relative w-full h-full overflow-hidden rounded-xl"
      style={{
        background:
          'radial-gradient(ellipse at center, rgba(56,189,248,0.08) 0%, rgba(2,6,23,1) 70%)',
      }}
    >
      <Starfield width={size.w} height={size.h} />

      {/* SVG layer for rings + connection lines + whisker zones */}
      <svg
        className="absolute inset-0 w-full h-full pointer-events-none"
        viewBox={`0 0 ${size.w} ${size.h}`}
      >
        {/* Orbit rings */}
        {rings.map((r, i) => (
          <circle
            key={i}
            cx={r.cx}
            cy={r.cy}
            r={r.r}
            fill="none"
            stroke="rgba(148,163,184,0.15)"
            strokeWidth={1}
            strokeDasharray="2 4"
          />
        ))}

        {/* Connection lines from today to each node */}
        {nodes.map((n) => {
          const cx = size.w / 2;
          const cy = size.h / 2;
          const opacity = n.isHammock
            ? 0.18
            : n.isFuzzyStartTask || n.isFuzzyEndTask
            ? 0.2
            : n.isPast
            ? 0.08
            : 0.25;
          return (
            <line
              key={`line-${n.id}`}
              x1={cx}
              y1={cy}
              x2={n.x}
              y2={n.y}
              stroke={n.parentTrack.color}
              strokeWidth={n.isHammock ? 1.5 : n.isShared ? 1.5 : 1}
              strokeOpacity={opacity}
              strokeDasharray={n.isHammock ? '4 6' : n.isFuzzyStartTask ? '3 5' : '2 4'}
            />
          );
        })}

        {/* Whisker zone arc for fuzzy-start+whisker tasks (the Solar task) */}
        {nodes
          .filter((n) => n.hasWhisker && n.isFuzzyStartTask)
          .map((n) => {
            // Draw a faint arc from the start anchor to the end anchor
            const startPt = armPosition(n.parentTrack.id, n.anchorOffsetDays);
            const endOffsetDays = n.task.fuzzy?.anchorEnd
              ? daysBetween(todayDate, parseDate(n.task.fuzzy.anchorEnd))
              : n.anchorOffsetDays + 90;
            const endPt = armPosition(n.parentTrack.id, endOffsetDays);
            return (
              <g key={`whisker-${n.id}`}>
                <line
                  x1={startPt.x}
                  y1={startPt.y}
                  x2={endPt.x}
                  y2={endPt.y}
                  stroke={n.parentTrack.color}
                  strokeWidth={2}
                  strokeOpacity={0.18}
                  strokeDasharray="2 3"
                />
                {/* Whisker caps at end */}
                <circle
                  cx={endPt.x}
                  cy={endPt.y}
                  r={4}
                  fill="none"
                  stroke={n.parentTrack.color}
                  strokeWidth={1.2}
                  strokeOpacity={0.5}
                  strokeDasharray="2 2"
                />
              </g>
            );
          })}

        {/* Hub edges: shared task → each non-parent track arm endpoint */}
        {hubEdges.map((e, i) => (
          <line
            key={`hubedge-${e.nodeId}-${e.trackId}-${i}`}
            x1={e.from.x}
            y1={e.from.y}
            x2={e.to.x}
            y2={e.to.y}
            stroke={e.color}
            strokeWidth={1.6}
            strokeOpacity={0.55}
            strokeDasharray="3 3"
          />
        ))}

        {/* Small target rings at each shared arm endpoint */}
        {hubEdges.map((e, i) => (
          <circle
            key={`hubtarget-${e.nodeId}-${e.trackId}-${i}`}
            cx={e.to.x}
            cy={e.to.y}
            r={4}
            fill="none"
            stroke={e.color}
            strokeWidth={1.5}
            strokeOpacity={0.8}
          />
        ))}
      </svg>

      {/* Center: TODAY */}
      <div
        className="absolute z-20 flex items-center justify-center rounded-full"
        style={{
          left: size.w / 2 - 32,
          top: size.h / 2 - 32,
          width: 64,
          height: 64,
          background:
            'radial-gradient(circle, rgba(255,255,255,0.95) 0%, rgba(125,211,252,0.7) 60%, rgba(56,189,248,0) 100%)',
          boxShadow: '0 0 40px 10px rgba(56,189,248,0.5)',
        }}
      >
        <div className="flex flex-col items-center">
          <span className="text-[10px] font-semibold uppercase tracking-wider text-slate-700">
            Today
          </span>
          <span className="text-[11px] font-bold text-slate-900">{today.slice(5)}</span>
        </div>
      </div>

      {/* Task nodes */}
      {nodes.map((n) => {
        const isSelected = n.id === selectedTaskId;
        const sharedColors = n.allTrackIds
          .map((id) => trackColorById.get(id))
          .filter(Boolean) as string[];
        const conicGradient = buildConicGradient(sharedColors);

        return (
          <button
            key={n.id}
            onClick={() => onSelectTask(n.id)}
            className="absolute z-10 transition-transform duration-200 hover:scale-125 hover:z-30"
            style={{
              left: n.x - n.radius,
              top: n.y - n.radius,
              width: n.radius * 2,
              height: n.radius * 2,
            }}
            title={buildNodeTooltip(n)}
          >
            {/* Glow */}
            <div
              className="absolute inset-0 rounded-full blur-md"
              style={{
                background: n.isShared
                  ? conicGradient
                  : n.isFuzzyStartTask || n.isFuzzyEndTask
                  ? 'rgba(168,85,247,0.5)'
                  : n.parentTrack.color,
                opacity: n.isHammock
                  ? 0.3
                  : n.isPast
                  ? 0.15
                  : n.anchorOffsetDays === 0
                  ? 0.6
                  : 0.4,
              }}
            />
            {/* Core dot */}
            <div
              className="absolute inset-0 rounded-full border-2"
              style={{
                background: n.isHammock
                  ? 'transparent'
                  : n.isShared
                  ? conicGradient
                  : n.isFuzzyStartTask || n.isFuzzyEndTask
                  ? 'transparent'
                  : n.parentTrack.color,
                borderColor: n.isHammock
                  ? n.parentTrack.color
                  : n.isShared
                  ? 'white'
                  : n.isFuzzyStartTask || n.isFuzzyEndTask
                  ? 'rgba(168,85,247,0.8)'
                  : 'rgba(255,255,255,0.4)',
                borderStyle:
                  n.isFuzzyStartTask || n.isFuzzyEndTask ? 'dashed' : 'solid',
                opacity: n.isPast && !n.isHammock ? 0.4 : 1,
                outline: isSelected ? '2px solid white' : 'none',
                outlineOffset: 2,
                boxShadow: n.isShared
                  ? '0 0 12px rgba(255,255,255,0.5)'
                  : n.isFuzzyStartTask || n.isFuzzyEndTask
                  ? '0 0 8px rgba(168,85,247,0.4)'
                  : 'none',
              }}
            />
            {/* Shared hub dashed outer ring */}
            {n.isShared && (
              <div
                className="absolute rounded-full pointer-events-none"
                style={{
                  inset: -4,
                  border: '1px dashed rgba(255,255,255,0.7)',
                  opacity: 0.6,
                }}
              />
            )}
            {/* Fuzzy "?" in center */}
            {(n.isFuzzyStartTask || n.isFuzzyEndTask) && (
              <span
                className="absolute inset-0 flex items-center justify-center text-[10px] font-bold pointer-events-none"
                style={{ color: 'rgba(168,85,247,0.9)' }}
              >
                ?
              </span>
            )}
            {/* Hammock ripple */}
            {n.isHammock && (
              <div
                className="absolute inset-0 rounded-full animate-ping"
                style={{
                  border: `2px solid ${n.parentTrack.color}`,
                  opacity: 0.4,
                }}
              />
            )}
            {/* Emoji label */}
            <span
              className="absolute text-[10px] leading-none pointer-events-none"
              style={{
                left: '50%',
                top: '100%',
                transform: 'translate(-50%, 4px)',
                opacity: n.isHammock
                  ? 0.6
                  : n.isPast
                  ? 0.5
                  : 0.9,
              }}
            >
              {n.isShared
                ? '🔗'
                : n.isFuzzyStartTask || n.isFuzzyEndTask
                ? '❓'
                : glyphFor(n.parentTrack.icon)}
            </span>
          </button>
        );
      })}

      {/* Legend */}
      <div className="absolute bottom-3 left-3 z-20 flex flex-col gap-1 text-[10px] text-slate-400 bg-slate-900/60 backdrop-blur px-2 py-1.5 rounded-md border border-slate-700/50">
        <LegendRow swatch={<span className="w-2 h-2 rounded-full bg-white shadow-[0_0_8px_2px_rgba(56,189,248,0.8)]" />} label="Today" />
        <LegendRow swatch={<span className="w-2 h-2 rounded-full bg-slate-500 opacity-50" />} label="Past (greyed)" />
        <LegendRow swatch={<span className="w-2 h-2 rounded-full border-2 border-cyan-400" />} label="Hammock (never ends)" />
        <LegendRow
          swatch={
            <span
              className="w-2 h-2 rounded-full border border-white"
              style={{ background: 'conic-gradient(from 0deg, #ec4899, #84cc16, #ec4899)' }}
            />
          }
          label="Shared hub (multi-track event)"
        />
        <LegendRow
          swatch={
            <span
              className="w-2 h-2 rounded-full border border-purple-400"
              style={{ borderStyle: 'dashed' }}
            />
          }
          label="Fuzzy / nebulous (?)"
        />
        <div className="text-slate-500 mt-0.5">Gravity ∝ 1 / |anchor − today|</div>
      </div>
    </div>
  );
}

// ── Sub-components ───────────────────────────────────────────────────────

function LegendRow({ swatch, label }: { swatch: React.ReactNode; label: string }) {
  return (
    <div className="flex items-center gap-1.5">
      {swatch}
      <span>= {label}</span>
    </div>
  );
}

function buildNodeTooltip(n: GalaxyNode): string {
  const parts: string[] = [];
  parts.push(
    `${n.parentTrack.name}${n.isShared ? ` + ${n.allTrackIds.length - 1} more` : ''} · ${n.task.title}`
  );
  if (n.isHammock) return parts[0];
  if (n.isFuzzyStartTask) parts.push('Start: ? (fuzzy)');
  else if (n.task.startDate !== 'AUTO') parts.push(`Start: ${n.task.startDate}`);
  if (n.isFuzzyEndTask) parts.push('End: ? (fuzzy)');
  else if (n.task.dueDate) parts.push(`Due: ${n.task.dueDate}`);
  if (n.hasWhisker) parts.push('Has whisker zone');
  return parts.join(' · ');
}

// ── Starfield (kept local — only used here) ──────────────────────────────

function Starfield({ width, height }: { width: number; height: number }) {
  const [mounted, setMounted] = useState(false);
  useEffect(() => {
    setMounted(true);
  }, []);

  const stars = useMemo(() => {
    if (!mounted) return [];
    const arr: { x: number; y: number; r: number; o: number; tw: number }[] = [];
    const count = Math.floor((width * height) / 6000);
    for (let i = 0; i < count; i++) {
      arr.push({
        x: Math.random() * width,
        y: Math.random() * height,
        r: Math.random() * 1.4 + 0.2,
        o: Math.random() * 0.7 + 0.1,
        tw: Math.random() * 3 + 2,
      });
    }
    return arr;
  }, [width, height, mounted]);

  if (!mounted) return null;

  return (
    <svg className="absolute inset-0 w-full h-full">
      {stars.map((s, i) => (
        <circle
          key={i}
          cx={s.x}
          cy={s.y}
          r={s.r}
          fill="white"
          opacity={s.o}
          style={{ animation: `twinkle ${s.tw}s ease-in-out infinite` }}
        />
      ))}
      <style>{`
        @keyframes twinkle {
          0%, 100% { opacity: 0.2; }
          50% { opacity: 0.9; }
        }
      `}</style>
    </svg>
  );
}
