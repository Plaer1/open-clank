'use client';

import { useMemo } from 'react';
import { useMoveStore } from '@/store/useMoveStore';
import { useTagFilter } from '@/hooks/useTagFilter';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Tag } from 'lucide-react';

export function TagFilterPanel() {
  const data = useMoveStore((s) => s.data);
  const { active, isActive, toggle, clear } = useTagFilter();

  // Build a { tag → count } map across all tasks on all tracks.
  const tagCounts = useMemo(() => {
    const m = new Map<string, number>();
    for (const track of data.tracks) {
      for (const task of track.tasks) {
        if (!task.tags) continue;
        for (const t of task.tags) {
          m.set(t, (m.get(t) ?? 0) + 1);
        }
      }
    }
    return Array.from(m.entries()).sort((a, b) => {
      if (b[1] !== a[1]) return b[1] - a[1];
      return a[0].localeCompare(b[0]);
    });
  }, [data]);

  if (tagCounts.length === 0) {
    return null; // hide panel entirely when no tags exist
  }

  return (
    <div className="flex flex-col gap-2 p-3 rounded-xl border border-slate-700/50 bg-slate-950/40">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-1.5">
          <Tag className="w-3.5 h-3.5 text-slate-400" />
          <h3 className="text-sm font-semibold text-slate-200 uppercase tracking-wide">
            Tags
          </h3>
          {isActive && (
            <span className="text-[10px] text-cyan-300 font-mono">
              ({active.length} active)
            </span>
          )}
        </div>
        {isActive && (
          <Button
            size="sm"
            variant="ghost"
            className="h-7 px-2 text-[10px] text-slate-300 hover:text-white hover:bg-slate-800"
            onClick={clear}
          >
            Clear
          </Button>
        )}
      </div>

      <div className="flex flex-wrap gap-1.5 max-h-40 overflow-y-auto pr-1">
        {tagCounts.map(([tag, count]) => {
          const tagActive = active.includes(tag);
          return (
            <button
              key={tag}
              onClick={() => toggle(tag)}
              className="transition-all"
              title={tagActive ? `Click to remove filter: #${tag}` : `Click to filter by #${tag}`}
            >
              <Badge
                variant="outline"
                className={`text-[10px] font-mono cursor-pointer select-none ${
                  tagActive
                    ? 'border-cyan-400 text-cyan-200 bg-cyan-950/50'
                    : 'border-slate-600 text-slate-400 bg-slate-900/60 hover:border-slate-400 hover:text-slate-200'
                }`}
              >
                #{tag}
                <span className="ml-1 text-[9px] opacity-70">×{count}</span>
              </Badge>
            </button>
          );
        })}
      </div>

      {isActive ? (
        <div className="text-[10px] text-slate-400 px-1 leading-relaxed">
          Showing only tasks matching <span className="text-cyan-300">ALL</span> selected tags.
        </div>
      ) : (
        <div className="text-[10px] text-slate-500 px-1 leading-relaxed">
          Click a tag to filter. Tasks with no tags are always visible.
        </div>
      )}
    </div>
  );
}
