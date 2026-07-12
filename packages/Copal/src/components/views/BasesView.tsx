'use client';

import { ExternalLink, Table2, RefreshCw } from 'lucide-react';
import { useVaultIndex } from '@/hooks/useVaultIndex';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { openNoteAt } from '@/lib/noteNavigation';

export function BasesView() {
  const { index, loading, error, refresh } = useVaultIndex();
  const tables = index.notes.flatMap((note) => note.parsed.tables.map((table) => ({ note, table })));

  return (
    <div className="w-full h-full rounded-xl border border-slate-700/50 bg-slate-950/40 overflow-hidden flex flex-col">
      <div className="px-3 py-2 border-b border-slate-700/50 bg-slate-900/40 flex items-center gap-2">
        <Table2 className="h-4 w-4 text-cyan-300" />
        <div className="text-xs font-semibold text-slate-200">Bases + tables</div>
        <Badge variant="outline" className="ml-auto border-slate-600 text-slate-300 text-[10px]">
          {index.bases.length} bases · {tables.length} tables
        </Badge>
        <Button size="icon" variant="outline" className="h-7 w-7 border-slate-700 bg-slate-900" onClick={refresh}>
          <RefreshCw className="h-3.5 w-3.5" />
        </Button>
      </div>
      <div className="flex-1 min-h-0 overflow-y-auto p-3 grid grid-cols-1 xl:grid-cols-2 gap-3">
        {error && <div className="text-xs text-red-200 border border-red-500/40 bg-red-950/30 rounded-md p-2">{error}</div>}
        {loading && <div className="text-xs text-slate-500">Loading base readers...</div>}
        {index.bases.map((base) => (
          <section key={base.sourcePath} className="rounded-md border border-slate-700/60 bg-slate-900/35 p-3">
            <div className="flex items-center gap-2">
              <div className="text-xs font-semibold text-slate-100 min-w-0 truncate">{base.sourcePath}</div>
              <Button size="icon" variant="outline" className="ml-auto h-6 w-6 border-slate-700 bg-slate-950" onClick={() => openNoteAt(base.sourcePath, 1)} title="Open source">
                <ExternalLink className="h-3 w-3" />
              </Button>
            </div>
            <div className="flex flex-wrap gap-1 mt-2">
              {base.keys.map((key) => <Badge key={key} variant="outline" className="text-[10px] border-cyan-800 text-cyan-200">{key}</Badge>)}
            </div>
            <div className="mt-3 grid grid-cols-2 gap-3 text-[11px]">
              <div>
                <div className="text-slate-500 uppercase tracking-wide mb-1">Columns</div>
                {(base.columns.length ? base.columns : ['name', 'file']).map((col) => <div key={col} className="text-slate-300 font-mono">{col}</div>)}
              </div>
              <div>
                <div className="text-slate-500 uppercase tracking-wide mb-1">Filters</div>
                {(base.filters.length ? base.filters : ['no parsed filters']).map((filter) => <div key={filter} className="text-slate-300">{filter}</div>)}
              </div>
            </div>
          </section>
        ))}
        {tables.map(({ note, table }) => (
          <section key={`${note.path}:${table.line}`} className="rounded-md border border-slate-700/60 bg-slate-900/35 p-3 overflow-x-auto">
            <div className="flex items-center gap-2">
              <div className="text-xs font-semibold text-slate-100 min-w-0 truncate">{note.title}</div>
              <Button size="icon" variant="outline" className="ml-auto h-6 w-6 border-slate-700 bg-slate-950" onClick={() => openNoteAt(note.path, table.line)} title="Open source">
                <ExternalLink className="h-3 w-3" />
              </Button>
            </div>
            <div className="text-[10px] text-slate-600 font-mono mb-2">{note.path}:L{table.line}</div>
            <table className="w-full text-[11px] border-collapse">
              <thead>
                <tr>{table.headers.map((header) => <th key={header} className="text-left border-b border-slate-700 px-2 py-1 text-slate-400">{header}</th>)}</tr>
              </thead>
              <tbody>
                {table.rows.map((row, idx) => (
                  <tr key={idx}>
                    {table.headers.map((header, cellIdx) => <td key={header} className="border-b border-slate-800 px-2 py-1 text-slate-300">{row[cellIdx] ?? ''}</td>)}
                  </tr>
                ))}
              </tbody>
            </table>
          </section>
        ))}
      </div>
    </div>
  );
}
