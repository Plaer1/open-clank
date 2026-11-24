import assert from 'node:assert/strict';
import { resolveGraphSettings } from '../../static/js/copal/graphSettings.js';
import { graphBudgetSummary } from '../../static/js/copal/graphBudgetSummary.js';

const settings = resolveGraphSettings({common:{maxEdges:0,searchRows:25},modes:{gravity:{searchHops:0}}}, 'gravity');
const summary = graphBudgetSummary(settings, {inventory_complete:false, indexed_files:3, observed_entries:9,
  total_entries:null, parse_failed_files:1, ignored_entries:2, unsupported_entries:0, unavailable_entries:1,
  dependencies:'partial'});
assert.match(summary.coverage, /3 indexed files; 9 observed \/ unknown total/);
assert.match(summary.coverage, /dependency coverage partial/);
assert.match(summary.query, /25 rows.*0 hops/);
assert.match(summary.display, /0 edges/);
assert.match(graphBudgetSummary(settings).coverage, /not available/);
const bounded = resolveGraphSettings({common:{maxNodes:1000,retainedNodes:200,maxEdges:2000,retainedEdges:800}}, 'map');
assert.match(graphBudgetSummary(bounded).display, /Effective map display: 200 nodes \/ 800 edges/);
console.log('graph budget summary preserves effective limits and unknown coverage');
