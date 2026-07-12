'use client';

import { useEffect, useState } from 'react';
import { useMoveStore } from '@/store/useMoveStore';
import { glyphFor, type Track } from '@/lib/types';
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
import { Label } from '@/components/ui/label';
import { EmojiPicker } from '@/components/EmojiPicker';

const COLORS = [
  '#f97316', '#84cc16', '#ec4899', '#a855f7', '#14b8a6', '#eab308',
  '#0ea5e9', '#b45309', '#f59e0b', '#6b7280', '#dc2626', '#22c55e',
  '#10b981', '#0891b2', '#06b6d4', '#f43f5e', '#8b5cf6', '#facc15',
];

interface Props {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** id of a track to edit; null/undefined = create new. */
  trackId?: string | null;
}

function genId() {
  return `track-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 5)}`;
}

export function TrackEditorDialog({ open, onOpenChange, trackId }: Props) {
  const tracks = useMoveStore((s) => s.data.tracks);
  const addTrack = useMoveStore((s) => s.addTrack);
  const updateTrack = useMoveStore((s) => s.updateTrack);

  const existing = trackId ? tracks.find((t) => t.id === trackId) ?? null : null;

  const [name, setName] = useState('');
  const [color, setColor] = useState(COLORS[0]);
  const [icon, setIcon] = useState<string>('📦');
  const [unicodeOpen, setUnicodeOpen] = useState(false);

  useEffect(() => {
    if (!open) return;
    if (existing) {
      setName(existing.name);
      setColor(existing.color);
      setIcon(existing.icon || '📦');
    } else {
      setName('');
      setColor(COLORS[Math.floor(Math.random() * COLORS.length)]);
      setIcon('📦');
    }
    setUnicodeOpen(false);
  }, [open]);

  function save() {
    if (!name.trim()) return;
    const iconVal = glyphFor(icon) === '•' ? '📦' : icon;
    if (existing) {
      updateTrack(existing.id, { name: name.trim(), color, icon: iconVal });
    } else {
      const track: Track = {
        id: genId(),
        name: name.trim(),
        color,
        icon: iconVal,
        enabled: true,
        tasks: [],
      };
      addTrack(track);
    }
    onOpenChange(false);
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="bg-slate-950 border-slate-700 text-slate-200">
        <DialogHeader>
          <DialogTitle className="text-slate-100">
            {existing ? 'Edit track' : 'Add track'}
          </DialogTitle>
          <DialogDescription className="text-slate-400">
            Tracks are lanes on the timeline. New tracks start empty — add events to them from the
            Add-event button or the To-do view.
          </DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-3 py-2">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="tr-name">Name</Label>
            <Input
              id="tr-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. Vet & paperwork"
              className="bg-slate-900 border-slate-700"
              autoFocus
            />
          </div>

          <div className="flex flex-col gap-1.5">
            <Label>Color</Label>
            <div className="flex flex-wrap gap-1.5">
              {COLORS.map((c) => (
                <button
                  key={c}
                  type="button"
                  onClick={() => setColor(c)}
                  className={`w-6 h-6 rounded-full border-2 transition-transform ${
                    color === c ? 'border-white scale-110' : 'border-transparent'
                  }`}
                  style={{ background: c }}
                  aria-label={`color ${c}`}
                />
              ))}
            </div>
          </div>

          <div className="flex flex-col gap-1.5">
            <div className="flex items-center justify-between">
              <Label>Icon</Label>
              <span className="text-[10px] text-slate-500">
                Selected: <span className="text-base">{glyphFor(icon)}</span>
              </span>
            </div>
            <EmojiPicker value={icon} onSelect={setIcon} />
            <div className="flex items-center gap-2 mt-1">
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={() => setUnicodeOpen((v) => !v)}
                className="h-7 px-2 text-[10px] border-slate-700 text-slate-300 hover:bg-slate-800"
                title="Type or paste any unicode character"
              >
                Unicode
              </Button>
              {unicodeOpen ? (
                <Input
                  value={icon}
                  onChange={(e) => setIcon(Array.from(e.target.value).slice(0, 4).join(''))}
                  placeholder="type/paste a char (or U+1F431)"
                  className="h-7 bg-slate-900 border-slate-700 text-xs"
                />
              ) : (
                <span className="text-[10px] text-slate-500">
                  Use Unicode to enter any character directly.
                </span>
              )}
            </div>
            {unicodeOpen && /^U\+[0-9A-Fa-f]{1,6}$/.test(icon) && (
              <div className="flex items-center gap-2 text-[10px] text-slate-400">
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  className="h-6 px-2 text-[10px]"
                  onClick={() => {
                    const cp = parseInt(icon.slice(2), 16);
                    if (cp > 0) setIcon(String.fromCodePoint(cp));
                  }}
                >
                  Convert {icon} → {(() => {
                    try { return String.fromCodePoint(parseInt(icon.slice(2), 16)); } catch { return '?'; }
                  })()}
                </Button>
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
            disabled={!name.trim()}
            className="bg-cyan-600 hover:bg-cyan-500 text-white"
          >
            {existing ? 'Save' : 'Add track'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
