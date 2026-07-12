'use client';

import { useState } from 'react';
import { ExternalLink, GitFork, RefreshCw } from 'lucide-react';
import { useVaultIndex } from '@/hooks/useVaultIndex';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { openNoteAt } from '@/lib/noteNavigation';

export function MindMapView() {
  const { index, loading, error, refresh } = useVaultIndex();
  const [activePath, setActivePath] = useState('');
  const [query, setQuery] = useState('');
  const notes = index.notes.filter((note) => note.parsed.outline.length > 0);
  const active = notes.find((note) => note.path === activePath) ?? notes[0];

  const filtered = (() => {
    const q = query.trim().toLowerCase();
    if (!q || !active) return active?.parsed.outline ?? [];
    return active.parsed.outline.filter((item) => item.text.toLowerCase().includes(q));
  })();

  return (
    <div className="w-full h-full rounded-xl border border-slate-700/50 bg-slate-950/40 overflow-hidden grid grid-cols-1 lg:grid-cols-[280px_minmax(0,1fr)]">
      <aside className="border-b lg:border-b-0 lg:border-r border-slate-700/50 bg-slate-950/50 min-h-0 flex flex-col">
        <div className="p-3 border-b border-slate-700/50 flex items-center gap-2">
          <GitFork className="h-4 w-4 text-cyan-300" />
          <div className="text-xs font-semibold text-slate-200">Mind map</div>
          <Button size="icon" variant="outline" className="ml-auto h-7 w-7 border-slate-700 bg-slate-900" onClick={refresh}>
            <RefreshCw className="h-3.5 w-3.5" />
          </Button>
        </div>
        <div className="flex-1 overflow-y-auto">
          {notes.map((note) => (
            <button
              key={note.path}
              onClick={() => setActivePath(note.path)}
              className={`w-full text-left px-3 py-2 border-b border-slate-800/60 text-xs hover:bg-slate-900/60 ${
                active?.path === note.path ? 'text-cyan-100 bg-cyan-950/30' : 'text-slate-300'
              }`}
            >
              <div className="truncate">{note.title}</div>
              <div className="text-[10px] text-slate-600">{note.parsed.outline.length} outline items</div>
            </button>
          ))}
        </div>
      </aside>
      <section className="min-h-0 overflow-y-auto p-4">
        {error && <div className="mb-3 text-xs text-red-200 border border-red-500/40 bg-red-950/30 rounded-md p-2">{error}</div>}
        {loading && <div className="text-xs text-slate-500">Loading outlines...</div>}
        {active && (
          <>
            <div className="flex flex-wrap items-center gap-2 mb-4">
              <div>
                <div className="text-[11px] text-slate-500 font-mono">{active.path}</div>
                <h2 className="text-xl font-semibold text-slate-100">{active.title}</h2>
              </div>
              <Input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Filter outline..." className="ml-auto h-8 w-52 bg-slate-900 border-slate-700 text-xs" />
            </div>
            <div className="space-y-1">
              {filtered.map((item) => (
                <div
                  key={`${item.line}-${item.text}`}
                  className="border border-slate-800 bg-slate-900/30 rounded-md px-3 py-2"
                  style={{ marginLeft: Math.min((item.level - 1) * 24, 120) }}
                >
                  <div className="flex items-center gap-2">
                    <span className="text-[10px] text-slate-600 font-mono">L{item.line}</span>
                    <span className={item.kind === 'heading' ? 'text-cyan-100 text-sm' : 'text-slate-300 text-xs'}>
                      {item.text}
                    </span>
                    <Button size="icon" variant="outline" className="ml-auto h-6 w-6 border-slate-700 bg-slate-950" onClick={() => openNoteAt(active.path, item.line)} title="Open source">
                      <ExternalLink className="h-3 w-3" />
                    </Button>
                  </div>
                </div>
              ))}
            </div>
          </>
        )}
      </section>
    </div>
  );
}
