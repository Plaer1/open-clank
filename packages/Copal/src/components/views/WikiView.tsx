'use client';

import { useEffect, useMemo, useState } from 'react';
import type { KeyboardEvent } from 'react';
import { ArrowDown, ArrowUp, Blocks, ExternalLink, FileText, Pin, PinOff, RefreshCw, Search } from 'lucide-react';
import { useVaultIndex } from '@/hooks/useVaultIndex';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Input } from '@/components/ui/input';
import { openNoteAt } from '@/lib/noteNavigation';

export function WikiView() {
  const { index, loading, error, refresh } = useVaultIndex();
  const [query, setQuery] = useState('');
  const [pinned, setPinned] = useState<string[]>([]);
  const [boardOrder, setBoardOrder] = useState<string[]>([]);
  const [selectedId, setSelectedId] = useState('');

  useEffect(() => {
    fetch('/api/ui-state')
      .then((res) => (res.ok ? res.json() : { pinnedInfantecimemes: [], wikiBlockOrder: [] }))
      .then((state) => {
        setPinned(Array.isArray(state.pinnedInfantecimemes) ? state.pinnedInfantecimemes : []);
        setBoardOrder(Array.isArray(state.wikiBlockOrder) ? state.wikiBlockOrder : []);
      })
      .catch(() => {
        setPinned([]);
        setBoardOrder([]);
      });
  }, []);

  async function persistBoard(nextPinned = pinned, nextOrder = boardOrder) {
    await fetch('/api/ui-state', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ pinnedInfantecimemes: nextPinned, wikiBlockOrder: nextOrder }),
    });
  }

  async function setPinnedIds(next: string[]) {
    setPinned(next);
    await persistBoard(next, boardOrder);
  }

  function togglePin(id: string) {
    const next = pinned.includes(id) ? pinned.filter((item) => item !== id) : [id, ...pinned];
    setPinnedIds(next).catch(() => setPinned(next));
  }

  const blocks = useMemo(() => {
    const q = query.trim().toLowerCase();
    const order = new Map(boardOrder.map((id, index) => [id, index]));
    const filtered = q
      ? index.infantecimemes.filter((block) =>
          `${block.title} ${block.text} ${block.tags.join(' ')} ${block.sourcePath}`.toLowerCase().includes(q),
        )
      : index.infantecimemes;
    return [...filtered].sort((a, b) => {
      const pinSort = Number(pinned.includes(b.id)) - Number(pinned.includes(a.id));
      if (pinSort) return pinSort;
      const orderA = order.get(a.id);
      const orderB = order.get(b.id);
      if (orderA !== undefined || orderB !== undefined) return (orderA ?? Number.MAX_SAFE_INTEGER) - (orderB ?? Number.MAX_SAFE_INTEGER);
      return a.title.localeCompare(b.title);
    });
  }, [index.infantecimemes, query, pinned, boardOrder]);

  const pinnedBlocks = useMemo(() => blocks.filter((block) => pinned.includes(block.id)), [blocks, pinned]);
  const streamBlocks = useMemo(() => blocks.filter((block) => !pinned.includes(block.id)), [blocks, pinned]);
  const selected = useMemo(() => blocks.find((block) => block.id === selectedId) ?? blocks[0], [blocks, selectedId]);
  const selectedIndex = selected ? blocks.findIndex((block) => block.id === selected.id) : -1;
  const selectedEdges = useMemo(() => {
    if (!selected) return { incoming: [], outgoing: [] };
    return {
      incoming: index.graph.edges.filter((edge) => edge.to === selected.sourcePath),
      outgoing: index.graph.edges.filter((edge) => edge.from === selected.sourcePath),
    };
  }, [index.graph.edges, selected]);

  useEffect(() => {
    if (!selectedId && blocks[0]) setSelectedId(blocks[0].id);
    if (selectedId && blocks.length > 0 && !blocks.some((block) => block.id === selectedId)) setSelectedId(blocks[0].id);
  }, [blocks, selectedId]);

  async function moveBlock(id: string, direction: -1 | 1) {
    const ids = blocks.map((block) => block.id);
    const current = ids.indexOf(id);
    const next = current + direction;
    if (current < 0 || next < 0 || next >= ids.length) return;
    const reordered = [...ids];
    const [item] = reordered.splice(current, 1);
    reordered.splice(next, 0, item);
    setBoardOrder(reordered);
    await persistBoard(pinned, reordered);
  }

  function navigateBoard(event: KeyboardEvent<HTMLDivElement>) {
    if (blocks.length === 0) return;
    const columnJump = window.innerWidth >= 1536 ? 3 : window.innerWidth >= 768 ? 2 : 1;
    let next = selectedIndex < 0 ? 0 : selectedIndex;
    if (event.key === 'ArrowRight') next += 1;
    else if (event.key === 'ArrowLeft') next -= 1;
    else if (event.key === 'ArrowDown') next += columnJump;
    else if (event.key === 'ArrowUp') next -= columnJump;
    else if (event.key === 'Home') next = 0;
    else if (event.key === 'End') next = blocks.length - 1;
    else if (event.key === 'Enter' && selected) openNoteAt(selected.sourcePath, selected.line);
    else if (event.key === ' ' && selected) togglePin(selected.id);
    else return;
    event.preventDefault();
    setSelectedId(blocks[Math.max(0, Math.min(blocks.length - 1, next))].id);
  }

  return (
    <div className="copal-workspace w-full h-full grid grid-cols-1 lg:grid-cols-[230px_minmax(0,1fr)_320px]">
      <aside className="copal-pane border-b lg:border-b-0 lg:border-r border-slate-800">
        <div className="copal-pane-header space-y-2">
          <div className="flex items-center gap-2">
            <Blocks className="h-4 w-4 text-cyan-300" />
            <div className="min-w-0">
              <div className="text-xs font-semibold text-slate-200">Building block memes</div>
              <div className="text-[10px] text-slate-500">{blocks.length} visible · {pinned.length} pinned</div>
            </div>
            <Button size="icon" variant="outline" className="ml-auto h-7 w-7 border-slate-700 bg-slate-900" onClick={refresh} title="Refresh blocks">
              <RefreshCw className="h-3.5 w-3.5" />
            </Button>
          </div>
          <div className="relative">
            <Search className="pointer-events-none absolute left-2 top-2 h-3.5 w-3.5 text-slate-600" />
            <Input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Filter blocks..." className="h-8 bg-slate-900 border-slate-700 pl-7 text-xs" />
          </div>
        </div>

        {error && <div className="m-3 text-xs text-red-200 border border-red-500/40 bg-red-950/30 rounded-md p-2">{error}</div>}
        {loading && <div className="p-4 text-xs text-slate-500">Loading blocks...</div>}

        <div className="flex-1 min-h-0 overflow-y-auto p-2">
          <div className="mb-2 px-1 text-[10px] uppercase tracking-wide text-slate-600">Pinned</div>
          <div className="space-y-1">
            {pinnedBlocks.map((block) => (
              <button
                key={block.id}
                onClick={() => setSelectedId(block.id)}
                className={`w-full rounded-md border px-2 py-2 text-left ${selected?.id === block.id ? 'border-cyan-700 bg-cyan-950/30' : 'border-slate-800 bg-slate-900/35 hover:border-slate-700'}`}
              >
                <div className="flex items-center gap-1.5">
                  <Pin className="h-3 w-3 text-cyan-300" />
                  <span className="truncate text-[11px] text-slate-200">{block.title}</span>
                </div>
                <div className="mt-1 truncate text-[10px] text-slate-600">{block.sourcePath}:L{block.line}</div>
              </button>
            ))}
            {pinnedBlocks.length === 0 && <div className="rounded-md border border-dashed border-slate-800 p-3 text-[11px] text-slate-600">Pin blocks to keep them in this lane.</div>}
          </div>
        </div>
      </aside>

      <main className="copal-editor-leaf">
        <div className="copal-editor-titlebar">
          <div className="min-w-0 mr-auto">
            <div className="text-[11px] text-slate-500">Wiki mode</div>
            <div className="truncate text-sm font-semibold text-slate-100">Infantecimeme board</div>
          </div>
          <Badge variant="outline" className="border-cyan-800/70 text-cyan-200 text-[10px]">{blocks.length} blocks</Badge>
          <Badge variant="outline" className="border-slate-700 text-slate-300 text-[10px]">{index.notes.length} sources</Badge>
        </div>

        <div className="flex-1 min-h-0 overflow-y-auto p-3" tabIndex={0} onKeyDown={navigateBoard}>
          <div className="grid gap-3 md:grid-cols-2 2xl:grid-cols-3">
            {[...pinnedBlocks, ...streamBlocks].map((block) => (
              <article key={block.id} className={`copal-block-card ${selected?.id === block.id ? 'copal-block-card-active' : ''}`} onClick={() => setSelectedId(block.id)}>
                <div className="flex items-center gap-2 mb-2">
                  <Badge variant="outline" className="border-cyan-800 text-cyan-200 text-[10px]">{block.kind}</Badge>
                  <span className="text-[10px] text-slate-600 font-mono truncate">{block.sourcePath}:L{block.line}</span>
                  <Button
                    size="icon"
                    variant="outline"
                    className="ml-auto h-6 w-6 border-slate-700 bg-slate-950"
                    onClick={(event) => {
                      event.stopPropagation();
                      openNoteAt(block.sourcePath, block.line);
                    }}
                    title="Open source"
                  >
                    <ExternalLink className="h-3 w-3" />
                  </Button>
                  <Button
                    size="icon"
                    variant="outline"
                    className="h-6 w-6 border-slate-700 bg-slate-950"
                    onClick={(event) => {
                      event.stopPropagation();
                      togglePin(block.id);
                    }}
                    title={pinned.includes(block.id) ? 'Unpin block' : 'Pin block'}
                  >
                    {pinned.includes(block.id) ? <PinOff className="h-3 w-3" /> : <Pin className="h-3 w-3" />}
                  </Button>
                  <Button
                    size="icon"
                    variant="outline"
                    className="h-6 w-6 border-slate-700 bg-slate-950"
                    onClick={(event) => {
                      event.stopPropagation();
                      void moveBlock(block.id, -1);
                    }}
                    title="Move up"
                  >
                    <ArrowUp className="h-3 w-3" />
                  </Button>
                  <Button
                    size="icon"
                    variant="outline"
                    className="h-6 w-6 border-slate-700 bg-slate-950"
                    onClick={(event) => {
                      event.stopPropagation();
                      void moveBlock(block.id, 1);
                    }}
                    title="Move down"
                  >
                    <ArrowDown className="h-3 w-3" />
                  </Button>
                </div>
                <h3 className="mb-1 text-sm font-semibold text-slate-100">{block.title}</h3>
                {block.text && <pre className="max-h-44 overflow-hidden whitespace-pre-wrap font-sans text-[11px] leading-relaxed text-slate-300">{block.text}</pre>}
                <div className="flex flex-wrap gap-1 mt-2">
                  {block.tags.slice(0, 6).map((tag) => <Badge key={tag} variant="outline" className="border-slate-700 text-slate-400 text-[10px]">#{tag}</Badge>)}
                </div>
              </article>
            ))}
          </div>
          {blocks.length === 0 && !loading && <div className="grid h-full place-items-center text-xs text-slate-500">No blocks match current filter.</div>}
        </div>

        <div className="copal-statusbar">
          <span>{query ? 'filtered' : 'all blocks'}</span>
          <span>{pinnedBlocks.length} pinned</span>
          <span>{streamBlocks.length} stream</span>
        </div>
      </main>

      <aside className="copal-pane border-t lg:border-t-0 lg:border-l border-slate-800">
        <div className="copal-pane-header">
          <div className="text-xs font-semibold text-slate-200">Block detail</div>
          <div className="mt-1 truncate text-[10px] text-slate-500">{selected?.sourcePath ?? 'no selection'}</div>
        </div>
        <div className="flex-1 min-h-0 overflow-y-auto p-3">
          {selected ? (
            <div className="space-y-3">
              <div className="copal-block-detail">
                <div className="mb-2 flex items-center gap-2">
                  <Badge variant="outline" className="border-cyan-800 text-cyan-200 text-[10px]">{selected.kind}</Badge>
                  <span className="font-mono text-[10px] text-slate-600">L{selected.line}</span>
                </div>
                <h2 className="text-base font-semibold text-slate-100">{selected.title}</h2>
                {selected.text && <pre className="mt-3 whitespace-pre-wrap font-sans text-[12px] leading-relaxed text-slate-300">{selected.text}</pre>}
              </div>

              <div className="copal-block-detail">
                <div className="mb-2 text-[10px] uppercase tracking-wide text-slate-500">Source</div>
                <button onClick={() => openNoteAt(selected.sourcePath, selected.line)} className="flex w-full items-center gap-2 rounded-md border border-slate-800 bg-slate-900/50 px-2 py-2 text-left text-[11px] text-cyan-200 hover:border-cyan-900">
                  <FileText className="h-3.5 w-3.5 shrink-0" />
                  <span className="min-w-0 truncate">{selected.sourcePath}:L{selected.line}</span>
                </button>
              </div>

              <div className="copal-block-detail">
                <div className="mb-2 text-[10px] uppercase tracking-wide text-slate-500">Tags</div>
                <div className="flex flex-wrap gap-1">
                  {selected.tags.map((tag) => <Badge key={tag} variant="outline" className="border-slate-700 text-slate-300 text-[10px]">#{tag}</Badge>)}
                  {selected.tags.length === 0 && <span className="text-[11px] text-slate-500">No tags.</span>}
                </div>
              </div>

              <div className="copal-block-detail">
                <div className="mb-2 text-[10px] uppercase tracking-wide text-slate-500">Graph context</div>
                <div className="grid gap-2 text-[11px] text-slate-400">
                  <div>
                    <div className="mb-1 text-slate-500">Outgoing</div>
                    {selectedEdges.outgoing.slice(0, 8).map((edge) => <div key={edge.id} className="truncate">{edge.label} {'->'} {edge.to}</div>)}
                    {selectedEdges.outgoing.length === 0 && <div className="text-slate-600">No outgoing graph edges.</div>}
                  </div>
                  <div>
                    <div className="mb-1 text-slate-500">Incoming</div>
                    {selectedEdges.incoming.slice(0, 8).map((edge) => <div key={edge.id} className="truncate">{edge.from} {'->'} {edge.label}</div>)}
                    {selectedEdges.incoming.length === 0 && <div className="text-slate-600">No incoming graph edges.</div>}
                  </div>
                </div>
              </div>

              <div className="flex gap-2">
                <Button size="sm" variant="outline" className="h-8 border-slate-700 bg-slate-900" onClick={() => openNoteAt(selected.sourcePath, selected.line)}>
                  <ExternalLink className="h-3.5 w-3.5 mr-1.5" />
                  Open
                </Button>
                <Button size="sm" variant="outline" className="h-8 border-slate-700 bg-slate-900" onClick={() => togglePin(selected.id)}>
                  {pinned.includes(selected.id) ? <PinOff className="h-3.5 w-3.5 mr-1.5" /> : <Pin className="h-3.5 w-3.5 mr-1.5" />}
                  {pinned.includes(selected.id) ? 'Unpin' : 'Pin'}
                </Button>
                <Button size="icon" variant="outline" className="h-8 w-8 border-slate-700 bg-slate-900" onClick={() => void moveBlock(selected.id, -1)} title="Move up">
                  <ArrowUp className="h-3.5 w-3.5" />
                </Button>
                <Button size="icon" variant="outline" className="h-8 w-8 border-slate-700 bg-slate-900" onClick={() => void moveBlock(selected.id, 1)} title="Move down">
                  <ArrowDown className="h-3.5 w-3.5" />
                </Button>
              </div>
            </div>
          ) : (
            <div className="text-[11px] text-slate-500">Select a block to inspect it.</div>
          )}
        </div>
      </aside>
    </div>
  );
}
