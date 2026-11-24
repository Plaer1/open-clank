// Presentation only: these values do not change index authority or start work.
const shown = value => Number.isSafeInteger(value) && value >= 0 ? String(value) : 'unknown';
const minimum = (...values) => values.every(v => Number.isSafeInteger(v) && v >= 0) ? shown(Math.min(...values)) : 'unknown';
export function graphBudgetSummary(settings = {}, coverage = null) {
  const s = settings || {}, c = coverage && typeof coverage === 'object' ? coverage : null;
  const coverageText = c
    ? `Index coverage: ${shown(c.indexed_files)} indexed files; ${shown(c.observed_entries)} observed / ${shown(c.total_entries)} total entries. Inventory ${c.inventory_complete === true ? 'complete' : 'partial or unknown'}; dependency coverage ${['complete','partial','indexing'].includes(c.dependencies) ? c.dependencies : 'unknown'}. Parse failures ${shown(c.parse_failed_files)}, ignored ${shown(c.ignored_entries)}, unsupported ${shown(c.unsupported_entries)}, unavailable ${shown(c.unavailable_entries)}.`
    : 'Index coverage is not available for this view. Index resource limits are separate from graph display limits.';
  return {
    coverage: coverageText,
    query: `Evidence query: ${shown(s.searchRows)} rows, ${shown(s.searchKiB)} KiB, ${shown(s.searchTokens)} tokens, ${shown(s.searchHops)} hops; work ceiling ${shown(s.queryWork)}. Map read: ${shown(s.pageNodes)} nodes, ${shown(s.pageEdges)} edges, ${shown(s.pageGroups)} groups, ${shown(s.pageKiB)} KiB.`,
    display: `Effective map display: ${minimum(s.maxNodes,s.retainedNodes)} nodes / ${minimum(s.maxEdges,s.retainedEdges)} edges (files ≤${minimum(s.maxFiles,s.maxNodes,s.retainedNodes)}, symbols ≤${minimum(s.maxSymbols,s.maxNodes,s.retainedNodes)}); retained ${shown(s.retainedNodes)} nodes / ${shown(s.retainedEdges)} edges, with ${shown(s.cacheMiB)} MiB of accounted cache. Evidence mounted ${shown(s.evidenceMounted)} / retained ${shown(s.evidenceLoaded)} resources, mounted ${shown(s.evidenceMountedEdges)} / retained ${shown(s.evidenceLoadedEdges)} edges.`,
    expansion: `Project overview pages across the indexed checkout independently of canvas and cache limits. Automatic graph expansion: initial ${shown(s.initialDepth)} levels, next ${shown(s.expansionDepth)} levels, ${shown(s.autoReads)} reads and ${shown(s.autoNodes)} loaded nodes. These limits do not increase index coverage.`,
  };
}
