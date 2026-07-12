'use client';

import { useEffect, useState } from 'react';
import { useMoveStore } from '@/store/useMoveStore';
import { useResolvedTracks } from '@/hooks/useResolvedTracks';
import { ICON_MAP, type Stage, type TaskPriority, type TaskStatus } from '@/lib/types';
import { getTaskTrackIds, isSharedTask } from '@/lib/sharing';
import { isFuzzyStart, isFuzzyEnd, hasWhisker } from '@/lib/fuzzy';
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
} from '@/components/ui/sheet';
import { Badge } from '@/components/ui/badge';
import { Separator } from '@/components/ui/separator';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Textarea } from '@/components/ui/textarea';
import { Label } from '@/components/ui/label';
import { Checkbox } from '@/components/ui/checkbox';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Trash2, Plus } from 'lucide-react';
import type { Task, Track } from '@/lib/types';

const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

function genStageId() {
  return `stg-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 5)}`;
}

export function TaskDetailSheet() {
  const selectedTaskId = useMoveStore((s) => s.selectedTaskId);
  const setSelectedTask = useMoveStore((s) => s.setSelectedTask);
  const today = useMoveStore((s) => s.today);
  const updateTask = useMoveStore((s) => s.updateTask);
  const deleteTask = useMoveStore((s) => s.deleteTask);
  const moveTask = useMoveStore((s) => s.moveTask);
  const { resolvedTracks, trackById } = useResolvedTracks();

  // Draft state for text fields (committed on blur).
  const [title, setTitle] = useState('');
  const [description, setDescription] = useState('');
  const [tags, setTags] = useState('');

  const found = (() => {
    if (!selectedTaskId) return null;
    for (const track of resolvedTracks) {
      for (const task of track.tasks) {
        if (task.id === selectedTaskId) return { task, track };
      }
    }
    return null;
  })();

  // Resync draft text fields when a different task is selected.
  useEffect(() => {
    if (!found) return;
    setTitle(found.task.title);
    setDescription(found.task.description ?? '');
    setTags((found.task.tags ?? []).join(', '));
  }, [selectedTaskId]);

  const task: Task | undefined = found?.task;
  const track: Track | undefined = found?.track;

  const sharedTrackIds = task && track ? getTaskTrackIds(task, track.id) : [];
  const sharedTracks = sharedTrackIds
    .map((id) => trackById.get(id))
    .filter(Boolean) as Track[];
  const isShared = task ? isSharedTask(task) : false;
  const fuzzyStart = task ? isFuzzyStart(task) : false;
  const fuzzyEnd = task ? isFuzzyEnd(task) : false;

  // ── commit helpers (no-ops if nothing selected) ──
  function patch(p: Partial<Task>) {
    if (task) updateTask(task.id, p);
  }
  function setStartMode(m: 'exact' | 'fuzzy' | 'fadein') {
    if (!task) return;
    const cur = task.startDate === 'FUZZY' ? task.fuzzy?.anchorStart : task.startDate;
    const anchor = (DATE_RE.test(cur ?? '') ? cur : '') || today;
    const fuzzyBase = task.fuzzy ? { ...task.fuzzy } : {};
    if (m === 'fuzzy' || m === 'fadein') {
      patch({ startDate: 'FUZZY', fuzzy: { ...fuzzyBase, anchorStart: anchor, fadeIn: m === 'fadein' } });
    } else {
      delete fuzzyBase.anchorStart;
      delete fuzzyBase.fadeIn;
      patch({ startDate: anchor, fuzzy: Object.keys(fuzzyBase).length ? fuzzyBase : undefined });
    }
  }
  function toggleShared(id: string) {
    if (!task) return;
    const cur = task.sharedTrackIds ?? [];
    const next = cur.includes(id) ? cur.filter((x) => x !== id) : [...cur, id];
    patch({ sharedTrackIds: next.length ? next.filter((x) => x !== task.id) : undefined });
  }
  function setStages(next: Stage[]) {
    patch({ stages: next.length ? next : undefined });
  }

  const selectableTracks = resolvedTracks.filter((t) => !t.special);
  const startMode = task
    ? task.startDate === 'FUZZY'
      ? task.fuzzy?.fadeIn
        ? 'fadein'
        : 'fuzzy'
      : 'exact'
    : 'exact';
  const anchorVal = task
    ? task.startDate === 'FUZZY'
      ? task.fuzzy?.anchorStart ?? ''
      : DATE_RE.test(task.startDate)
      ? task.startDate
      : ''
    : '';
  const dueVal = task && task.dueDate && DATE_RE.test(task.dueDate) ? task.dueDate : '';
  const fadeOut = task ? task.dueDate === null : false;
  const stagesList = task?.stages ?? [];
  const stagesDone = stagesList.filter((s) => s.done).length;

  return (
    <Sheet open={!!selectedTaskId} onOpenChange={(o) => !o && setSelectedTask(null)}>
      <SheetContent
        side="right"
        className="w-full sm:max-w-md bg-slate-950 border-slate-700 text-slate-200 overflow-y-auto"
      >
        <SheetHeader>
          <SheetTitle className="sr-only">Edit event</SheetTitle>
        </SheetHeader>

        {!found || !task || !track ? (
          <div className="p-6 text-center text-xs text-slate-500">No event selected.</div>
        ) : (
          <div className="flex flex-col gap-3 px-1 pb-6">
            {/* Header dots + badges (pretty) */}
            <div className="flex items-center gap-2 flex-wrap">
              <div className="flex items-center gap-1">
                {sharedTracks.map((t) => (
                  <span
                    key={t.id}
                    className="w-3 h-3 rounded-full"
                    style={{
                      background: t.color,
                      outline: t.id === track.id ? '2px solid white' : 'none',
                      outlineOffset: 1,
                    }}
                    title={t.name}
                  />
                ))}
              </div>
              <span className="text-lg">
                {isShared ? '🔗' : fuzzyStart || fuzzyEnd ? '❓' : ICON_MAP[track.icon as keyof typeof ICON_MAP] ?? '•'}
              </span>
              <span className="text-xs uppercase tracking-wide text-slate-400">
                {isShared ? `${sharedTracks.length} tracks` : fuzzyStart || fuzzyEnd ? 'fuzzy' : track.name}
              </span>
            </div>

            {/* Title */}
            <Input
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              onBlur={() => patch({ title: title.trim() || 'Untitled' })}
              placeholder="Title"
              className="bg-slate-900 border-slate-700 text-base font-semibold text-slate-100"
            />

            {/* Description */}
            <Textarea
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              onBlur={() => patch({ description: description.trim() || undefined })}
              placeholder="Description…"
              rows={2}
              className="bg-slate-900 border-slate-700 text-xs"
            />

            {/* Badges */}
            <div className="flex flex-wrap gap-2">
              <Badge
                variant="outline"
                className={
                  task.priority === 'high'
                    ? 'border-red-500 text-red-400 bg-red-950/30'
                    : task.priority === 'medium'
                    ? 'border-amber-500 text-amber-400 bg-amber-950/30'
                    : 'border-slate-500 text-slate-400 bg-slate-900/30'
                }
              >
                {task.priority}
              </Badge>
              <Badge variant="outline" className="border-slate-600 text-slate-300">
                {task.status}
              </Badge>
              {isShared && (
                <Badge variant="outline" className="border-fuchsia-400 text-fuchsia-200 bg-fuchsia-950/30">
                  🔗 shared
                </Badge>
              )}
              {(fuzzyStart || fuzzyEnd) && (
                <Badge variant="outline" className="border-purple-400 text-purple-200 bg-purple-950/30">
                  ❓ fuzzy
                </Badge>
              )}
              {stagesList.length > 0 && (
                <Badge variant="outline" className="border-cyan-500 text-cyan-300 bg-cyan-950/30">
                  ▦ {stagesDone}/{stagesList.length} stages
                </Badge>
              )}
            </div>

            <Separator className="bg-slate-700/50" />

            {/* Dates */}
            <div className="grid grid-cols-2 gap-3">
              <div className="flex flex-col gap-1.5">
                <Label>{startMode === 'exact' ? 'Start date' : 'Anchor date'}</Label>
                <Input
                  type="date"
                  value={anchorVal}
                  onChange={(e) => {
                    const v = e.target.value;
                    if (startMode === 'exact') patch({ startDate: v || today });
                    else patch({ fuzzy: { ...(task.fuzzy ?? {}), anchorStart: v || today } });
                  }}
                  className="bg-slate-900 border-slate-700 text-xs"
                />
                <div className="flex gap-1">
                  {(['exact', 'fuzzy', 'fadein'] as const).map((m) => (
                    <button
                      key={m}
                      type="button"
                      onClick={() => setStartMode(m)}
                      className={`flex-1 text-[9px] px-1 py-0.5 rounded border ${
                        startMode === m
                          ? 'border-cyan-400 bg-cyan-950/40 text-cyan-200'
                          : 'border-slate-600 text-slate-400 hover:bg-slate-800'
                      }`}
                    >
                      {m === 'exact' ? 'Date' : m === 'fuzzy' ? '? fuzzy' : 'Fade in'}
                    </button>
                  ))}
                </div>
              </div>
              <div className="flex flex-col gap-1.5">
                <Label>{task.dueDate ? 'Due date' : 'End'}</Label>
                <Input
                  type="date"
                  value={dueVal}
                  disabled={fadeOut}
                  onChange={(e) => patch({ dueDate: e.target.value || null })}
                  className="bg-slate-900 border-slate-700 text-xs disabled:opacity-50"
                />
                <label className="flex items-center gap-1.5 text-[10px] text-slate-300 cursor-pointer select-none">
                  <Checkbox
                    checked={fadeOut}
                    onCheckedChange={(v) => patch({ dueDate: v ? null : dueVal || today })}
                    className="border-slate-500 data-[state=checked]:bg-cyan-600 data-[state=checked]:border-cyan-600"
                  />
                  Fade out (∞)
                </label>
              </div>
            </div>

            {/* Main track */}
            <div className="flex flex-col gap-1.5">
              <Label>Main track</Label>
              <Select value={track.id} onValueChange={(id) => moveTask(task.id, id)}>
                <SelectTrigger className="bg-slate-900 border-slate-700 text-xs">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {selectableTracks.map((t) => (
                    <SelectItem key={t.id} value={t.id}>
                      {ICON_MAP[t.icon as keyof typeof ICON_MAP] ?? '•'} {t.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            {/* Shared tracks */}
            <div className="flex flex-col gap-1.5">
              <Label>
                Also on <span className="text-slate-500">(shared)</span>
              </Label>
              <div className="flex flex-wrap gap-1.5 p-2 rounded-md border border-slate-700 bg-slate-900/50 max-h-28 overflow-y-auto">
                {selectableTracks
                  .filter((t) => t.id !== track.id)
                  .map((t) => {
                    const on = (task.sharedTrackIds ?? []).includes(t.id);
                    return (
                      <button
                        key={t.id}
                        type="button"
                        onClick={() => toggleShared(t.id)}
                        className={`inline-flex items-center gap-1 text-[10px] px-2 py-0.5 rounded-full border ${
                          on
                            ? 'border-fuchsia-400 text-fuchsia-100 bg-fuchsia-950/40'
                            : 'border-slate-600 text-slate-300 bg-slate-900 hover:bg-slate-800'
                        }`}
                      >
                        <span className="w-1.5 h-1.5 rounded-full" style={{ background: t.color }} />
                        {ICON_MAP[t.icon as keyof typeof ICON_MAP] ?? '•'} {t.name}
                      </button>
                    );
                  })}
              </div>
            </div>

            {/* Priority / status */}
            <div className="grid grid-cols-2 gap-3">
              <div className="flex flex-col gap-1.5">
                <Label>Priority</Label>
                <Select value={task.priority} onValueChange={(v) => patch({ priority: v as TaskPriority })}>
                  <SelectTrigger className="bg-slate-900 border-slate-700 text-xs">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="low">low</SelectItem>
                    <SelectItem value="medium">medium</SelectItem>
                    <SelectItem value="high">high</SelectItem>
                  </SelectContent>
                </Select>
              </div>
              <div className="flex flex-col gap-1.5">
                <Label>Status</Label>
                <Select value={task.status} onValueChange={(v) => patch({ status: v as TaskStatus })}>
                  <SelectTrigger className="bg-slate-900 border-slate-700 text-xs">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="pending">pending</SelectItem>
                    <SelectItem value="in-progress">in-progress</SelectItem>
                    <SelectItem value="done">done</SelectItem>
                    <SelectItem value="ongoing">ongoing</SelectItem>
                  </SelectContent>
                </Select>
              </div>
            </div>

            {/* Tags */}
            <div className="flex flex-col gap-1.5">
              <Label>
                Tags <span className="text-slate-500">(comma separated)</span>
              </Label>
              <Input
                value={tags}
                onChange={(e) => setTags(e.target.value)}
                onBlur={() =>
                  patch({ tags: tags.split(',').map((s) => s.trim()).filter(Boolean) })
                }
                placeholder="vet, car-ride"
                className="bg-slate-900 border-slate-700 text-xs"
              />
            </div>

            <Separator className="bg-slate-700/50" />

            {/* Stages / sub-events */}
            <div className="flex flex-col gap-1.5">
              <div className="flex items-center justify-between">
                <Label>
                  Stages <span className="text-slate-500">(sub-events)</span>
                </Label>
                <button
                  type="button"
                  onClick={() => setStages([...stagesList, { id: genStageId(), title: '' }])}
                  className="text-[10px] text-cyan-300 hover:text-cyan-200 inline-flex items-center gap-0.5"
                >
                  <Plus className="w-3 h-3" /> add
                </button>
              </div>
              {stagesList.length === 0 ? (
                <p className="text-[10px] text-slate-500">
                  Break this event into ordered steps (e.g. Book → Prep → Visit). They show as a
                  checklist here and a progress bar on the timeline chip.
                </p>
              ) : (
                <div className="flex flex-col gap-1">
                  {stagesList.map((s, i) => (
                    <div key={s.id} className="flex items-center gap-1.5">
                      <Checkbox
                        checked={!!s.done}
                        onCheckedChange={() =>
                          setStages(stagesList.map((x) => (x.id === s.id ? { ...x, done: !x.done } : x)))
                        }
                        className="border-slate-500 data-[state=checked]:bg-emerald-600 data-[state=checked]:border-emerald-600"
                      />
                      <Input
                        defaultValue={s.title}
                        onBlur={(e) =>
                          setStages(
                            stagesList.map((x) => (x.id === s.id ? { ...x, title: e.target.value } : x))
                          )
                        }
                        placeholder={`Stage ${i + 1}`}
                        className={`bg-slate-900 border-slate-700 h-7 text-xs flex-1 ${
                          s.done ? 'line-through text-slate-500' : ''
                        }`}
                      />
                      <Input
                        type="date"
                        defaultValue={s.date ?? ''}
                        onBlur={(e) =>
                          setStages(
                            stagesList.map((x) =>
                              x.id === s.id ? { ...x, date: e.target.value || undefined } : x
                            )
                          )
                        }
                        className="bg-slate-900 border-slate-700 h-7 text-[10px] w-32"
                      />
                      <button
                        type="button"
                        onClick={() => setStages(stagesList.filter((x) => x.id !== s.id))}
                        className="text-slate-500 hover:text-red-400 px-1 text-sm leading-none"
                        title="Remove stage"
                      >
                        ×
                      </button>
                    </div>
                  ))}
                </div>
              )}
            </div>

            {/* Explainer */}
            {track.special ? (
              <ExplainerBlock
                color="cyan"
                title="🛶 About this line"
                body={
                  'This is the "Relax on my hammock" line. Its start auto-computes as the day after the latest visible task end across enabled non-hammock tracks.'
                }
              />
            ) : fuzzyStart || fuzzyEnd ? (
              <ExplainerBlock
                color="purple"
                title="❓ About fuzzy dates"
                body="Start and/or end are nebulous (?). Fuzzy-start tasks never show as past/missed. Replace ? with a real date when you know it."
              />
            ) : null}

            {/* Delete */}
            <Separator className="bg-slate-700/50" />
            <Button
              size="sm"
              variant="outline"
              onClick={() => {
                if (window.confirm(`Delete "${task.title}"? Removes it from every view.`)) {
                  deleteTask(task.id);
                }
              }}
              className="border-red-700/60 text-red-300 hover:bg-red-950/40 hover:text-red-200"
            >
              <Trash2 className="w-3.5 h-3.5 mr-1.5" /> Delete event
            </Button>
          </div>
        )}
      </SheetContent>
    </Sheet>
  );
}

const EXPLAINER_STYLES = {
  cyan: { box: 'bg-cyan-950/30 border-cyan-700/40', title: 'text-cyan-300', body: 'text-cyan-100/80' },
  purple: { box: 'bg-purple-950/20 border-purple-700/40', title: 'text-purple-300', body: 'text-purple-100/80' },
} as const;

function ExplainerBlock({
  color,
  title,
  body,
}: {
  color: keyof typeof EXPLAINER_STYLES;
  title: string;
  body: React.ReactNode;
}) {
  const s = EXPLAINER_STYLES[color];
  return (
    <div className={`p-3 rounded-md border ${s.box}`}>
      <div className={`text-xs font-semibold mb-1 ${s.title}`}>{title}</div>
      <div className={`text-[11px] leading-relaxed ${s.body}`}>{body}</div>
    </div>
  );
}
