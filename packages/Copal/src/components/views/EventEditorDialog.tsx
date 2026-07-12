'use client';

import { useEffect, useState } from 'react';
import { useMoveStore } from '@/store/useMoveStore';
import { ICON_MAP, type Stage, type Task, type TaskPriority, type TaskStatus } from '@/lib/types';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from '@/components/ui/dialog';
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

interface Props {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** id of a task to edit; null/undefined = create new. */
  taskId?: string | null;
}

const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

function genId() {
  return `usr-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 6)}`;
}

function genStageId() {
  return `stg-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 5)}`;
}

export function EventEditorDialog({ open, onOpenChange, taskId }: Props) {
  const tracks = useMoveStore((s) => s.data.tracks);
  const today = useMoveStore((s) => s.today);
  const addTask = useMoveStore((s) => s.addTask);
  const updateTask = useMoveStore((s) => s.updateTask);

  // Resolve the task being edited + its parent track (if any).
  const existing = (() => {
    if (!taskId) return null;
    for (const t of tracks) {
      const task = t.tasks.find((x) => x.id === taskId);
      if (task) return { task, parent: t.id };
    }
    return null;
  })();

  const [title, setTitle] = useState('');
  const [description, setDescription] = useState('');
  const [parentTrackId, setParentTrackId] = useState('');
  const [sharedTrackIds, setSharedTrackIds] = useState<string[]>([]);
  const [startDate, setStartDate] = useState('');
  const [dueDate, setDueDate] = useState('');
  const [tags, setTags] = useState('');
  const [priority, setPriority] = useState<TaskPriority>('medium');
  const [status, setStatus] = useState<TaskStatus>('pending');
  const [noEnd, setNoEnd] = useState(false);
  const [startMode, setStartMode] = useState<'exact' | 'fuzzy' | 'fadein'>('exact');
  const [stages, setStages] = useState<Stage[]>([]);

  // (Re)initialise the form each time the dialog opens.
  useEffect(() => {
    if (!open) return;
    if (existing) {
      const t = existing.task;
      setTitle(t.title);
      setDescription(t.description ?? '');
      setParentTrackId(existing.parent);
      setSharedTrackIds(t.sharedTrackIds ?? []);
      setStartDate(DATE_RE.test(t.startDate) ? t.startDate : '');
      setDueDate(t.dueDate && DATE_RE.test(t.dueDate) ? t.dueDate : '');
      setTags((t.tags ?? []).join(', '));
      setPriority(t.priority);
      setStatus(t.status);
      setNoEnd(t.dueDate === null);
      setStartMode(
        t.startDate === 'FUZZY' ? (t.fuzzy?.fadeIn ? 'fadein' : 'fuzzy') : 'exact'
      );
      setStages((t.stages ?? []).map((s) => ({ ...s })));
    } else {
      setTitle('');
      setDescription('');
      setParentTrackId(tracks.find((t) => !t.special)?.id ?? tracks[0]?.id ?? '');
      setSharedTrackIds([]);
      setStartDate(today);
      setDueDate('');
      setTags('');
      setPriority('medium');
      setStatus('pending');
      setNoEnd(false);
      setStartMode('exact');
      setStages([]);
    }
  }, [open]);

  function toggleShared(id: string) {
    setSharedTrackIds((cur) =>
      cur.includes(id) ? cur.filter((x) => x !== id) : [...cur, id]
    );
  }

  function save() {
    if (!parentTrackId || !title.trim()) return;
    const cleanShared = sharedTrackIds.filter((id) => id !== parentTrackId);
    const cleanStages = stages.filter((s) => s.title.trim()).map((s) => ({ ...s }));
    const anchor = startDate || today;
    // Preserve any existing fuzzy end/whisker fields; only the start side is edited here.
    const fuzzyBase = existing?.task.fuzzy ? { ...existing.task.fuzzy } : {};
    let startDateVal: string;
    let fuzzy: typeof fuzzyBase | undefined;
    if (startMode === 'fuzzy' || startMode === 'fadein') {
      startDateVal = 'FUZZY';
      fuzzy = { ...fuzzyBase, anchorStart: anchor, fadeIn: startMode === 'fadein' };
    } else {
      startDateVal = anchor;
      delete fuzzyBase.anchorStart;
      delete fuzzyBase.fadeIn;
      fuzzy = Object.keys(fuzzyBase).length ? fuzzyBase : undefined;
    }
    const task: Task = {
      id: existing ? existing.task.id : genId(),
      title: title.trim(),
      description: description.trim() || undefined,
      startDate: startDateVal,
      dueDate: noEnd ? null : dueDate || null,
      status,
      priority,
      tags: tags
        .split(',')
        .map((s) => s.trim())
        .filter(Boolean),
      sharedTrackIds: cleanShared.length ? cleanShared : undefined,
      ...(fuzzy ? { fuzzy } : {}),
      stages: cleanStages.length ? cleanStages : undefined,
    };
    if (existing) {
      updateTask(task.id, task);
    } else {
      addTask(parentTrackId, task);
    }
    onOpenChange(false);
  }

  // Non-special tracks you can attach an event to (excludes the auto hammock).
  const selectableTracks = tracks.filter((t) => !t.special);
  const extraChoices = selectableTracks.filter((t) => t.id !== parentTrackId);
  const sharedCount = sharedTrackIds.filter((id) => id !== parentTrackId).length;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="bg-slate-950 border-slate-700 text-slate-200 max-h-[90vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle className="text-slate-100">
            {existing ? 'Edit event' : 'Add event'}
          </DialogTitle>
          <DialogDescription className="text-slate-400">
            Saved instantly to <code className="text-cyan-300">move-data.json</code> and shown
            across Galaxy, Timeline &amp; Calendar. Add more than one track to make it a shared
            (🔗) event.
          </DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-3 py-2">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="ev-title">Title</Label>
            <Input
              id="ev-title"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder="e.g. Vet appointment — Leia & Chewie"
              className="bg-slate-900 border-slate-700"
            />
          </div>

          <div className="flex flex-col gap-1.5">
            <Label htmlFor="ev-desc">Description</Label>
            <Textarea
              id="ev-desc"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              rows={2}
              className="bg-slate-900 border-slate-700"
            />
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div className="flex flex-col gap-1.5">
              <Label>{startMode === 'exact' ? 'Start date' : 'Anchor date'}</Label>
              <Input
                type="date"
                value={startDate}
                onChange={(e) => setStartDate(e.target.value)}
                className="bg-slate-900 border-slate-700"
              />
              <div className="flex gap-1 mt-0.5">
                {(['exact', 'fuzzy', 'fadein'] as const).map((m) => (
                  <button
                    key={m}
                    type="button"
                    onClick={() => setStartMode(m)}
                    className={`flex-1 text-[10px] px-1 py-0.5 rounded border ${
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
              <Label>
                Due date <span className="text-slate-500">(optional)</span>
              </Label>
              <Input
                type="date"
                value={noEnd ? '' : dueDate}
                disabled={noEnd}
                onChange={(e) => setDueDate(e.target.value)}
                className="bg-slate-900 border-slate-700 disabled:opacity-50"
              />
            </div>
          </div>

          <label className="flex items-start gap-2 text-[11px] text-slate-300 cursor-pointer select-none -mt-1">
            <Checkbox
              checked={noEnd}
              onCheckedChange={(v) => setNoEnd(v === true)}
              className="border-slate-500 data-[state=checked]:bg-cyan-600 data-[state=checked]:border-cyan-600 mt-0.5"
            />
            <span>
              <span className="text-slate-200 font-medium">Fade out</span> — no end date. The
              event extends toward ∞ on the timeline instead of ending on a set date.
            </span>
          </label>

          <div className="flex flex-col gap-1.5">
            <Label>Main track</Label>
            <Select value={parentTrackId} onValueChange={setParentTrackId}>
              <SelectTrigger className="bg-slate-900 border-slate-700">
                <SelectValue placeholder="Pick a track" />
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

          <div className="flex flex-col gap-1.5">
            <Label>
              Also appears on{' '}
              <span className="text-slate-500">(shared / pluralistic)</span>
            </Label>
            <div className="flex flex-wrap gap-1.5 max-h-32 overflow-y-auto p-2 rounded-md border border-slate-700 bg-slate-900/50">
              {extraChoices.length === 0 && (
                <span className="text-[11px] text-slate-500">Pick a main track first.</span>
              )}
              {extraChoices.map((t) => {
                const on = sharedTrackIds.includes(t.id);
                return (
                  <button
                    key={t.id}
                    type="button"
                    onClick={() => toggleShared(t.id)}
                    className={`inline-flex items-center gap-1 text-[11px] px-2 py-1 rounded-full border transition-colors ${
                      on
                        ? 'border-fuchsia-400 text-fuchsia-100 bg-fuchsia-950/40'
                        : 'border-slate-600 text-slate-300 bg-slate-900 hover:bg-slate-800'
                    }`}
                    style={on ? { boxShadow: `inset 0 0 0 1px ${t.color}` } : undefined}
                  >
                    <span
                      className="w-1.5 h-1.5 rounded-full"
                      style={{ background: t.color }}
                    />
                    {ICON_MAP[t.icon as keyof typeof ICON_MAP] ?? '•'} {t.name}
                  </button>
                );
              })}
            </div>
            <p className="text-[10px] text-slate-500">
              {sharedCount > 0
                ? `🔗 Shared across ${sharedCount + 1} track(s) — renders as one hub in Galaxy, one chip per track in Timeline, one de-duplicated chip in Calendar.`
                : 'Select extra tracks to make this event shared (e.g. the same car ride for Leia & Chewie).'}
            </p>
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div className="flex flex-col gap-1.5">
              <Label>Priority</Label>
              <Select value={priority} onValueChange={(v) => setPriority(v as TaskPriority)}>
                <SelectTrigger className="bg-slate-900 border-slate-700">
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
              <Select value={status} onValueChange={(v) => setStatus(v as TaskStatus)}>
                <SelectTrigger className="bg-slate-900 border-slate-700">
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

          <div className="flex flex-col gap-1.5">
            <Label htmlFor="ev-tags">
              Tags <span className="text-slate-500">(comma separated)</span>
            </Label>
            <Input
              id="ev-tags"
              value={tags}
              onChange={(e) => setTags(e.target.value)}
              placeholder="vet, car-ride, shared"
              className="bg-slate-900 border-slate-700"
            />
          </div>

          <div className="flex flex-col gap-1.5">
            <div className="flex items-center justify-between">
              <Label>
                Stages <span className="text-slate-500">(sub-events)</span>
              </Label>
              <button
                type="button"
                onClick={() => setStages((s) => [...s, { id: genStageId(), title: '' }])}
                className="text-[10px] text-cyan-300 hover:text-cyan-200"
              >
                + add stage
              </button>
            </div>
            {stages.length === 0 ? (
              <p className="text-[10px] text-slate-500">
                Break this event into ordered steps (e.g. Book → Prep → Visit). They show as a
                checklist in the side panel and a progress bar on the timeline chip.
              </p>
            ) : (
              <div className="flex flex-col gap-1.5">
                {stages.map((st, i) => (
                  <div key={st.id} className="flex items-center gap-1.5">
                    <span className="text-[10px] text-slate-500 w-4 text-right">{i + 1}</span>
                    <Input
                      value={st.title}
                      onChange={(e) =>
                        setStages((s) =>
                          s.map((x) => (x.id === st.id ? { ...x, title: e.target.value } : x))
                        )
                      }
                      placeholder="Stage title"
                      className="bg-slate-900 border-slate-700 h-8 text-xs flex-1"
                    />
                    <Input
                      type="date"
                      value={st.date ?? ''}
                      onChange={(e) =>
                        setStages((s) =>
                          s.map((x) =>
                            x.id === st.id ? { ...x, date: e.target.value || undefined } : x
                          )
                        )
                      }
                      className="bg-slate-900 border-slate-700 h-8 text-xs w-36"
                    />
                    <button
                      type="button"
                      onClick={() => setStages((s) => s.filter((x) => x.id !== st.id))}
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
        </div>

        <DialogFooter>
          <Button
            variant="ghost"
            onClick={() => onOpenChange(false)}
            className="text-slate-300 hover:bg-slate-800"
          >
            Cancel
          </Button>
          <Button
            onClick={save}
            disabled={!title.trim() || !parentTrackId}
            className="bg-cyan-600 hover:bg-cyan-500 text-white"
          >
            {existing ? 'Save changes' : 'Add event'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
