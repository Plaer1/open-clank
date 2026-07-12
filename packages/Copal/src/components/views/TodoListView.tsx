'use client';

import { useMemo, useState } from 'react';
import { useMoveStore } from '@/store/useMoveStore';
import { useResolvedTracks } from '@/hooks/useResolvedTracks';
import { useTagFilter } from '@/hooks/useTagFilter';
import { parseDate, daysBetween, fmtShortLabel } from '@/lib/dates';
import { getLayoutStart, getLayoutEnd } from '@/lib/fuzzy';
import { getTaskTrackIds, isSharedTask } from '@/lib/sharing';
import { ICON_MAP, type Task, type TaskPriority, type Track } from '@/lib/types';
import { Checkbox } from '@/components/ui/checkbox';
import { Input } from '@/components/ui/input';
import { Badge } from '@/components/ui/badge';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';

type SortKey = 'start' | 'due' | 'priority';
type SourceFilter = 'all' | 'vault' | 'timeline';
type StatusFilter = 'all' | 'pending' | 'in-progress' | 'done';
type PriorityFilter = 'all' | TaskPriority;
type DateFilter = 'all' | 'due' | 'scheduled' | 'undated' | 'overdue';
type LearningFilter = 'all' | 'course' | 'skill';
const PRIORITY_RANK: Record<TaskPriority, number> = { high: 0, medium: 1, low: 2 };
const FAR = Number.MAX_SAFE_INTEGER;

export function TodoListView({ onSelectTask }: { onSelectTask: (id: string) => void }) {
  const today = useMoveStore((s) => s.today);
  const updateTask = useMoveStore((s) => s.updateTask);
  const { resolvedTracks, trackById } = useResolvedTracks();
  const tagFilter = useTagFilter();

  const [hideDone, setHideDone] = useState(false);
  const [sort, setSort] = useState<SortKey>('start');
  const [query, setQuery] = useState('');
  const [sourceFilter, setSourceFilter] = useState<SourceFilter>('all');
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('all');
  const [priorityFilter, setPriorityFilter] = useState<PriorityFilter>('all');
  const [dateFilter, setDateFilter] = useState<DateFilter>('all');
  const [learningFilter, setLearningFilter] = useState<LearningFilter>('all');

  const todayDate = useMemo(() => parseDate(today), [today]);

  const items = useMemo(() => {
    const out: { task: Task; parentTrack: Track; allTrackIds: string[]; readOnly?: boolean; source?: string }[] = [];
    const q = query.trim().toLowerCase();
    for (const tr of resolvedTracks) {
      if (!tr.enabled || tr.special) continue; // respect sidebar toggles; skip auto-hammock
      for (const task of tr.tasks) {
        const isVaultTask = task.id.startsWith('note:') || tr.id === 'vault-derived';
        if (sourceFilter === 'vault' && !isVaultTask) continue;
        if (sourceFilter === 'timeline' && isVaultTask) continue;
        if (statusFilter !== 'all' && task.status !== statusFilter) continue;
        if (priorityFilter !== 'all' && task.priority !== priorityFilter) continue;
        const tags = task.tags ?? [];
        const hasCourse = tags.some((tag) => tag.startsWith('course/'));
        const hasSkill = tags.some((tag) => tag.startsWith('skill/'));
        if (learningFilter === 'course' && !hasCourse) continue;
        if (learningFilter === 'skill' && !hasSkill) continue;
        const start = getLayoutStart(task);
        const end = getLayoutEnd(task);
        if (dateFilter === 'due' && !end) continue;
        if (dateFilter === 'scheduled' && !start) continue;
        if (dateFilter === 'undated' && (start || end)) continue;
        if (dateFilter === 'overdue' && (!end || end >= todayDate || task.status === 'done')) continue;
        if (!tagFilter.passes(task)) continue;
        if (q) {
          const hay = `${task.title} ${task.description ?? ''} ${tags.join(' ')}`.toLowerCase();
          if (!hay.includes(q)) continue;
        }
        if (hideDone && task.status === 'done') continue;
        out.push({ task, parentTrack: tr, allTrackIds: getTaskTrackIds(task, tr.id) });
      }
    }
    out.sort((a, b) => {
      if (sort === 'priority') {
        return (
          PRIORITY_RANK[a.task.priority] - PRIORITY_RANK[b.task.priority] ||
          (getLayoutStart(a.task)?.getTime() ?? 0) - (getLayoutStart(b.task)?.getTime() ?? 0)
        );
      }
      if (sort === 'due') {
        return (
          (getLayoutEnd(a.task)?.getTime() ?? FAR) - (getLayoutEnd(b.task)?.getTime() ?? FAR)
        );
      }
      return (
        (getLayoutStart(a.task)?.getTime() ?? FAR) - (getLayoutStart(b.task)?.getTime() ?? FAR)
      );
    });
    return out;
  }, [resolvedTracks, tagFilter, query, hideDone, sort, sourceFilter, statusFilter, priorityFilter, dateFilter, learningFilter, todayDate]);

  const doneCount = items.filter((i) => i.task.status === 'done').length;

  function toggleDone(task: Task) {
    updateTask(task.id, { status: task.status === 'done' ? 'pending' : 'done' });
  }

  return (
    <div className="w-full h-full flex flex-col rounded-xl border border-slate-700/50 bg-slate-950/40 overflow-hidden">
      {/* Controls */}
      <div className="flex flex-wrap items-center gap-2 px-3 py-2 border-b border-slate-700/50 bg-slate-900/40">
        <Input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search tasks…"
          className="h-8 w-44 bg-slate-900 border-slate-700 text-xs"
        />
        <Select value={sort} onValueChange={(v) => setSort(v as SortKey)}>
          <SelectTrigger className="h-8 w-36 bg-slate-900 border-slate-700 text-xs">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="start">Sort: start date</SelectItem>
            <SelectItem value="due">Sort: due date</SelectItem>
            <SelectItem value="priority">Sort: priority</SelectItem>
          </SelectContent>
        </Select>
        <Select value={sourceFilter} onValueChange={(v) => setSourceFilter(v as SourceFilter)}>
          <SelectTrigger className="h-8 w-32 bg-slate-900 border-slate-700 text-xs">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">Source: all</SelectItem>
            <SelectItem value="vault">Source: vault</SelectItem>
            <SelectItem value="timeline">Source: timeline</SelectItem>
          </SelectContent>
        </Select>
        <Select value={statusFilter} onValueChange={(v) => setStatusFilter(v as StatusFilter)}>
          <SelectTrigger className="h-8 w-32 bg-slate-900 border-slate-700 text-xs">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">Status: all</SelectItem>
            <SelectItem value="pending">Pending</SelectItem>
            <SelectItem value="in-progress">In progress</SelectItem>
            <SelectItem value="done">Done</SelectItem>
          </SelectContent>
        </Select>
        <Select value={priorityFilter} onValueChange={(v) => setPriorityFilter(v as PriorityFilter)}>
          <SelectTrigger className="h-8 w-32 bg-slate-900 border-slate-700 text-xs">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">Priority: all</SelectItem>
            <SelectItem value="high">High</SelectItem>
            <SelectItem value="medium">Medium</SelectItem>
            <SelectItem value="low">Low</SelectItem>
          </SelectContent>
        </Select>
        <Select value={dateFilter} onValueChange={(v) => setDateFilter(v as DateFilter)}>
          <SelectTrigger className="h-8 w-32 bg-slate-900 border-slate-700 text-xs">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">Date: all</SelectItem>
            <SelectItem value="due">Has due</SelectItem>
            <SelectItem value="scheduled">Has start</SelectItem>
            <SelectItem value="undated">Undated</SelectItem>
            <SelectItem value="overdue">Overdue</SelectItem>
          </SelectContent>
        </Select>
        <Select value={learningFilter} onValueChange={(v) => setLearningFilter(v as LearningFilter)}>
          <SelectTrigger className="h-8 w-32 bg-slate-900 border-slate-700 text-xs">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">Learning: all</SelectItem>
            <SelectItem value="course">Course</SelectItem>
            <SelectItem value="skill">Skill</SelectItem>
          </SelectContent>
        </Select>
        <label className="flex items-center gap-1.5 text-[11px] text-slate-300 cursor-pointer select-none ml-auto">
          <Checkbox
            checked={hideDone}
            onCheckedChange={(v) => setHideDone(v === true)}
            className="border-slate-500 data-[state=checked]:bg-cyan-600 data-[state=checked]:border-cyan-600"
          />
          Hide done
        </label>
        <span className="text-[11px] text-slate-500">
          {items.length} item{items.length === 1 ? '' : 's'}
          {doneCount > 0 && ` · ${doneCount} done`}
        </span>
      </div>

      {/* List */}
      <div className="flex-1 overflow-y-auto">
        {items.length === 0 && (
          <div className="p-6 text-center text-xs text-slate-500">
            Nothing matches the current filters.
          </div>
        )}
        {items.map(({ task, parentTrack, allTrackIds, readOnly, source }) => {
          const noteTask = readOnly || task.id.startsWith('note:');
          const start = getLayoutStart(task);
          const end = getLayoutEnd(task);
          const shared = isSharedTask(task);
          const isDone = task.status === 'done';
          const days = start ? daysBetween(todayDate, start) : null;
          const stripeColors = allTrackIds
            .map((id) => trackById.get(id)?.color)
            .filter(Boolean) as string[];

          return (
            <div
              key={task.id}
              className="group flex items-center gap-2.5 px-3 py-2 border-b border-slate-800/50 hover:bg-slate-900/40 cursor-pointer"
              onClick={() => onSelectTask(task.id)}
            >
              <Checkbox
                checked={isDone}
                disabled={noteTask}
                onCheckedChange={() => {
                  if (!noteTask) toggleDone(task);
                }}
                onClick={(e) => e.stopPropagation()}
                className="border-slate-500 data-[state=checked]:bg-emerald-600 data-[state=checked]:border-emerald-600 shrink-0"
              />

              {/* Track color dots */}
              <div className="flex items-center gap-0.5 shrink-0">
                {allTrackIds.slice(0, 4).map((id) => (
                  <span
                    key={id}
                    className="w-2 h-2 rounded-full"
                    style={{
                      background: trackById.get(id)?.color,
                      outline: id === parentTrack.id ? '1.5px solid white' : 'none',
                      outlineOffset: 0.5,
                    }}
                    title={trackById.get(id)?.name}
                  />
                ))}
                {allTrackIds.length > 4 && (
                  <span className="text-[8px] text-slate-500">+{allTrackIds.length - 4}</span>
                )}
              </div>

              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-1.5">
                  {shared && <span className="text-[10px]">🔗</span>}
                  <span
                    className={`text-xs font-medium truncate ${
                      isDone ? 'line-through text-slate-500' : 'text-slate-200'
                    }`}
                  >
                    {task.title}
                  </span>
                </div>
                <div className="flex items-center gap-1.5 text-[10px] text-slate-500 mt-0.5">
                  <span className="truncate">
                    {ICON_MAP[parentTrack.icon as keyof typeof ICON_MAP] ?? '•'} {parentTrack.name}
                  </span>
                  <span className="text-slate-600">·</span>
                  <span className="font-mono">
                    {task.startDate === 'FUZZY' ? '?' : start ? fmtShortLabel(start) : '?'}
                    {' → '}
                    {task.dueDate === null && !end ? '∞' : end ? fmtShortLabel(end) : '?'}
                  </span>
                  {days !== null && !isDone && (
                    <span
                      className={
                        days < 0
                          ? 'text-slate-600'
                          : days <= 7
                          ? 'text-red-400'
                          : days <= 14
                          ? 'text-amber-400'
                          : 'text-emerald-400'
                      }
                    >
                      · {days < 0 ? `${Math.abs(days)}d ago` : days === 0 ? 'today' : `in ${days}d`}
                    </span>
                  )}
                  {(source || noteTask) && (
                    <>
                      <span className="text-slate-600">·</span>
                      <span className="font-mono text-cyan-500">{source ?? task.description?.split('\n')[0]}</span>
                    </>
                  )}
                </div>
              </div>

              {/* Tags + priority */}
              <div className="flex items-center gap-1 shrink-0">
                {task.priority === 'high' && (
                  <Badge
                    variant="outline"
                    className="border-red-500/60 text-red-400 bg-red-950/30 text-[9px] px-1 py-0"
                  >
                    high
                  </Badge>
                )}
                {noteTask && (
                  <Badge
                    variant="outline"
                    className="border-cyan-700/60 text-cyan-300 bg-cyan-950/20 text-[9px] px-1 py-0"
                  >
                    note
                  </Badge>
                )}
                {task.tags?.slice(0, 2).map((t) => (
                  <Badge
                    key={t}
                    variant="outline"
                    className="border-slate-600 text-slate-400 bg-slate-900/60 text-[9px] font-mono px-1 py-0"
                  >
                    #{t}
                  </Badge>
                ))}
                <span
                  className="w-1.5 h-1.5 rounded-full opacity-0 group-hover:opacity-100"
                  style={{ background: stripeColors[0] ?? parentTrack.color }}
                />
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
