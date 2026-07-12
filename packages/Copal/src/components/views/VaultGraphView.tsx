'use client';

import { ExternalLink, Network, RefreshCw } from 'lucide-react';
import { useVaultIndex } from '@/hooks/useVaultIndex';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { openNoteAt } from '@/lib/noteNavigation';

export function VaultGraphView() {
  const { index, loading, error, refresh } = useVaultIndex();
  const nodes = index.graph.nodes.slice(0, 80);
  const edges = index.graph.edges
    .filter((edge) => nodes.some((node) => node.id === edge.from) && nodes.some((node) => node.id === edge.to))
    .slice(0, 140);

  const layout = (() => {
    const cx = 420;
    const cy = 270;
    const radius = Math.max(130, Math.min(240, nodes.length * 8));
    return new Map(
      nodes.map((node, idx) => {
        const angle = (idx / Math.max(nodes.length, 1)) * Math.PI * 2;
        const ring = node.type === 'tag' ? radius + 45 : node.type === 'missing' ? radius + 20 : radius;
        return [node.id, { x: cx + Math.cos(angle) * ring, y: cy + Math.sin(angle) * ring }];
      })
    );
  })();

  return (
    <div className="w-full h-full rounded-xl border border-slate-700/50 bg-slate-950/40 overflow-hidden flex flex-col">
      <div className="px-3 py-2 border-b border-slate-700/50 bg-slate-900/40 flex items-center gap-2">
        <Network className="h-4 w-4 text-cyan-300" />
        <div className="text-xs font-semibold text-slate-200">Vault graph</div>
        <Badge variant="outline" className="ml-auto border-slate-600 text-slate-300 text-[10px]">
          {nodes.length} nodes · {edges.length} edges
        </Badge>
        <Button size="icon" variant="outline" className="h-7 w-7 border-slate-700 bg-slate-900" onClick={refresh}>
          <RefreshCw className="h-3.5 w-3.5" />
        </Button>
      </div>

      {error && <div className="m-3 text-xs text-red-200 border border-red-500/40 bg-red-950/30 rounded-md p-2">{error}</div>}
      {loading && <div className="p-4 text-xs text-slate-500">Loading vault graph...</div>}

      <div className="flex-1 min-h-0 grid grid-cols-1 xl:grid-cols-[minmax(0,1fr)_280px]">
        <svg viewBox="0 0 840 540" className="w-full h-full min-h-[420px] bg-slate-950/20">
          {edges.map((edge) => {
            const from = layout.get(edge.from);
            const to = layout.get(edge.to);
            if (!from || !to) return null;
            return (
              <line
                key={edge.id}
                x1={from.x}
                y1={from.y}
                x2={to.x}
                y2={to.y}
                stroke={edge.type === 'tag' ? '#64748b' : '#0891b2'}
                strokeOpacity={0.35}
                strokeWidth={edge.type === 'wikilink' ? 1.5 : 1}
              />
            );
          })}
          {nodes.map((node) => {
            const pos = layout.get(node.id);
            if (!pos) return null;
            const color = node.type === 'missing' ? '#f97316' : node.type === 'tag' ? '#94a3b8' : '#22d3ee';
            return (
              <g key={node.id}>
                <circle cx={pos.x} cy={pos.y} r={node.type === 'note' ? 8 : 6} fill={color} fillOpacity={0.9} />
                <text x={pos.x + 10} y={pos.y + 4} fill="#cbd5e1" fontSize="10">
                  {node.label.slice(0, 34)}
                </text>
              </g>
            );
          })}
        </svg>

        <aside className="border-t xl:border-t-0 xl:border-l border-slate-700/50 p-3 overflow-y-auto">
          <div className="text-[11px] uppercase tracking-wide text-slate-500 mb-2">Edges</div>
          <div className="space-y-1">
            {edges.slice(0, 60).map((edge) => (
              <div key={edge.id} className="text-[11px] text-slate-400 border border-slate-800 rounded-md p-2">
                <div className="text-slate-300 truncate">{labelFor(nodes, edge.from)} → {labelFor(nodes, edge.to)}</div>
                <div className="text-slate-600">{edge.type}</div>
              </div>
            ))}
          </div>
          <div className="text-[11px] uppercase tracking-wide text-slate-500 mt-4 mb-2">Sources</div>
          <div className="space-y-1">
            {nodes.filter((node) => node.path).slice(0, 60).map((node) => (
              <button
                key={node.id}
                className="w-full flex items-center gap-2 text-left text-[11px] text-cyan-200 border border-slate-800 rounded-md p-2"
                onClick={() => node.path && openNoteAt(node.path, 1)}
              >
                <ExternalLink className="h-3 w-3 shrink-0" />
                <span className="truncate">{node.label}</span>
              </button>
            ))}
          </div>
        </aside>
      </div>
    </div>
  );
}

function labelFor(nodes: { id: string; label: string }[], id: string) {
  return nodes.find((node) => node.id === id)?.label ?? id;
}
