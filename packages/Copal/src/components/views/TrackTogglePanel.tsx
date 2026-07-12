'use client';

import { useState } from 'react';
import { useMoveStore } from '@/store/useMoveStore';
import { useResolvedTracks } from '@/hooks/useResolvedTracks';
import { glyphFor, type Track } from '@/lib/types';
import { Switch } from '@/components/ui/switch';
import { Button } from '@/components/ui/button';
import { TrackEditorDialog } from '@/components/views/TrackEditorDialog';
import { Plus, Pencil, Trash2, GripVertical } from 'lucide-react';
import {
  DndContext,
  closestCenter,
  PointerSensor,
  useSensor,
  useSensors,
  type DragEndEvent,
} from '@dnd-kit/core';
import {
  SortableContext,
  useSortable,
  verticalListSortingStrategy,
} from '@dnd-kit/sortable';
import { CSS } from '@dnd-kit/utilities';

export function TrackTogglePanel() {
  const tracks = useMoveStore((s) => s.data.tracks);
  const toggleTrack = useMoveStore((s) => s.toggleTrack);
  const enableAllTracks = useMoveStore((s) => s.enableAllTracks);
  const disableAllTracks = useMoveStore((s) => s.disableAllTracks);
  const removeTrack = useMoveStore((s) => s.removeTrack);
  const reorderTracks = useMoveStore((s) => s.reorderTracks);

  const { latestVisibleEndStr, hammockStart } = useResolvedTracks();

  const [addOpen, setAddOpen] = useState(false);
  const [editId, setEditId] = useState<string | null>(null);

  const enabledCount = tracks.filter((t) => t.enabled).length;

  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 5 } }),
  );

  function handleDragEnd(event: DragEndEvent) {
    const { active, over } = event;
    if (!over || active.id === over.id) return;
    const fromIndex = tracks.findIndex((t) => t.id === active.id);
    const toIndex = tracks.findIndex((t) => t.id === over.id);
    if (fromIndex >= 0 && toIndex >= 0) {
      reorderTracks(fromIndex, toIndex);
    }
  }

  return (
    <div className="flex flex-col gap-3 p-3 rounded-xl border border-slate-700/50 bg-slate-950/40">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold text-slate-200 uppercase tracking-wide">Tracks</h3>
        <div className="flex gap-1">
          <Button
            size="sm"
            variant="ghost"
            className="h-7 px-2 text-[10px] text-slate-300 hover:text-white hover:bg-slate-800"
            onClick={() => enableAllTracks()}
          >
            All
          </Button>
          <Button
            size="sm"
            variant="ghost"
            className="h-7 px-2 text-[10px] text-slate-300 hover:text-white hover:bg-slate-800"
            onClick={() => disableAllTracks(['relax-hammock'])}
          >
            None
          </Button>
          <Button
            size="sm"
            variant="ghost"
            className="h-7 px-2 text-[10px] text-cyan-300 hover:text-white hover:bg-slate-800"
            onClick={() => setAddOpen(true)}
            title="Add track"
          >
            <Plus className="w-3 h-3" />
          </Button>
        </div>
      </div>

      <DndContext sensors={sensors} collisionDetection={closestCenter} onDragEnd={handleDragEnd}>
        <SortableContext items={tracks.map((t) => t.id)} strategy={verticalListSortingStrategy}>
          <div className="flex flex-col gap-1">
            {tracks.map((track) => (
              <SortableTrackRow
                key={track.id}
                track={track}
                onToggle={() => toggleTrack(track.id)}
                onEdit={!track.special ? () => setEditId(track.id) : undefined}
                onDelete={
                  !track.special
                    ? () => {
                        if (
                          window.confirm(
                            `Delete track "${track.name}" and all its events? This cannot be undone.`
                          )
                        ) {
                          removeTrack(track.id);
                        }
                      }
                    : undefined
                }
              />
            ))}
          </div>
        </SortableContext>
      </DndContext>

      <div className="mt-1 px-2 py-2 rounded-md bg-slate-900/60 border border-slate-700/40">
        <div className="text-[10px] uppercase tracking-wide text-slate-500 mb-1">
          Hammock auto-update
        </div>
        {latestVisibleEndStr ? (
          <div className="text-[11px] text-slate-300 leading-relaxed">
            <div>
              <span className="text-slate-500">Latest visible end:</span>{' '}
              <span className="text-cyan-300 font-mono">{latestVisibleEndStr}</span>
            </div>
            <div>
              <span className="text-slate-500">Hammock starts:</span>{' '}
              <span className="text-cyan-300 font-mono">{hammockStart}</span>
            </div>
          </div>
        ) : (
          <div className="text-[11px] text-amber-400">
            No visible non-hammock tracks — hammock will fall back to global start.
          </div>
        )}
      </div>

      <div className="text-[10px] text-slate-500 px-1">
        {enabledCount}/{tracks.length} tracks active
      </div>

      <TrackEditorDialog open={addOpen} onOpenChange={setAddOpen} />
      <TrackEditorDialog
        open={editId !== null}
        onOpenChange={(o) => !o && setEditId(null)}
        trackId={editId}
      />
    </div>
  );
}

function SortableTrackRow({
  track,
  onToggle,
  onEdit,
  onDelete,
}: {
  track: Track;
  onToggle: () => void;
  onEdit?: () => void;
  onDelete?: () => void;
}) {
  const {
    attributes,
    listeners,
    setNodeRef,
    transform,
    transition,
    isDragging,
  } = useSortable({ id: track.id });

  const style = {
    transform: CSS.Transform.toString(transform),
    transition,
    opacity: isDragging ? 0.5 : undefined,
  };

  return (
    <div
      ref={setNodeRef}
      style={style}
      className="group flex items-center gap-1 px-2 py-1.5 rounded-md hover:bg-slate-800/50 transition-colors"
    >
      <button
        type="button"
        className="p-0.5 text-slate-600 hover:text-slate-300 cursor-grab active:cursor-grabbing shrink-0"
        title="Drag to reorder"
        {...attributes}
        {...listeners}
      >
        <GripVertical className="w-3 h-3" />
      </button>
      <span
        className="w-2.5 h-2.5 rounded-full shrink-0"
        style={{ background: track.color, opacity: track.enabled ? 1 : 0.3 }}
      />
      <span className="text-base leading-none shrink-0">
        {glyphFor(track.icon)}
      </span>
      <span
        className={`flex-1 text-xs truncate ${
          track.enabled
            ? track.special
              ? 'text-cyan-300 italic'
              : 'text-slate-200'
            : 'text-slate-500 line-through'
        }`}
      >
        {track.name}
      </span>
      {(onEdit || onDelete) && (
        <div className="flex items-center gap-0.5 opacity-0 group-hover:opacity-100 transition-opacity">
          {onEdit && (
            <button
              type="button"
              onClick={(e) => {
                e.stopPropagation();
                onEdit();
              }}
              className="p-1 text-slate-400 hover:text-cyan-300"
              title="Edit track"
            >
              <Pencil className="w-3 h-3" />
            </button>
          )}
          {onDelete && (
            <button
              type="button"
              onClick={(e) => {
                e.stopPropagation();
                onDelete();
              }}
              className="p-1 text-slate-400 hover:text-red-400"
              title="Delete track"
            >
              <Trash2 className="w-3 h-3" />
            </button>
          )}
        </div>
      )}
      <Switch checked={track.enabled} onCheckedChange={onToggle} className="scale-75 origin-right" />
    </div>
  );
}
