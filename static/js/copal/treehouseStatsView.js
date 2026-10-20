import { registerTreeHouseView } from './treehouseViews.js';
function renderStats(root,context) {
  const { h,api,ui } = context;
  // Existing instructor/current-state analytics remains available unchanged.
  context.renderAnalytics(root);
  const host = h('section',{class:'th-personal-stats copal-card','aria-label':'Observed learning history'},h('h2',{text:'Your learning history'}),h('p',{role:'status',text:'Loading saved observations…'}));root.append(host);
  api('/treehouse/stats?limit=30').then(data => {
    if (!host.isConnected) return;
    if (data.status === 'unavailable') {host.replaceChildren(h('h2',{text:'Your learning history'}),h('p',{text:'Learning history collection has not started for this workspace. Your current course progress is still shown above.'}));return;}
    const count = (family,type = null) => (data.totals || []).filter(row => row.family === family && (!type || row.courseType === type)).reduce((sum,row) => sum + Number(row.count || 0),0);
    const metrics = h('div',{class:'th-stats-metrics'});
    for (const [label,value] of [['Current learning streak',`${data.streak?.current || 0} days`],['Best observed streak',`${data.streak?.best || 0} days`],['Distinct courses completed',data.uniqueCourseCompletions || 0],['Tutorial completion observations',count('course.completed','tutorial')],['Quest completion observations',count('course.completed','quest')]]) metrics.append(h('div',{},h('strong',{text:String(value)}),h('span',{text:label})));
    const since = data.coverageStart ? new Date(typeof data.coverageStart === 'number' ? data.coverageStart * 1000 : data.coverageStart).toLocaleDateString() : 'collection began';
    const history = h('ol',{class:'th-observation-history'});
    const labels = {'course.opened':'Opened a learning path','activity.completed':'Completed a learning activity','course.completed':'Completed a course','submission.submitted':'Submitted work','submission.graded':'Work reviewed','evidence.reviewed':'Evidence reviewed','achievement.awarded':'Achievement earned','progress.reset':'Learning progress reset','achievement.reset':'Achievement progress reset'};
    const addRows = rows => {for (const row of rows || []) {const date = new Date(row.occurredAt);history.append(h('li',{},h('div',{},h('strong',{text:labels[row.family] || String(row.family || 'Learning activity').replaceAll('.',' ')}),h('small',{text:`${row.facts?.courseType === 'tutorial' ? 'Tutorial · content traversal' : row.facts?.courseType === 'quest' ? 'Quest' : 'Saved observation'} · ${Number.isFinite(date.getTime()) ? date.toLocaleString() : 'Date unavailable'}`})),h('span',{class:'th-state',text:row.trustClass === 'reviewed-evidence' ? 'Reviewed evidence' : row.trustClass === 'required-work' ? 'Required work' : row.trustClass === 'verified-source' ? 'Verified source' : 'Observation'})));}};
    addRows(data.history);
    if (!history.childNodes.length) history.append(h('li',{text:'No learning observations have been collected yet.'}));
    let cursor = data.nextBefore;
    const more = h('button',{class:'copal-btn',text:'Load earlier activity',hidden:!cursor,onclick:async () => {more.disabled = true;try {const next = await api(`/treehouse/stats?limit=30&before=${encodeURIComponent(cursor)}`);if(!host.isConnected)return;addRows(next.history);cursor = next.nextBefore;more.hidden = !cursor;}catch(error){feedback.textContent = error.message;}finally{more.disabled = false;}}});
    const feedback = h('p',{role:'status'});
    const coverage = h('details',{class:'th-diagnostics'},h('summary',{text:'Collection coverage'}),h('p',{text:'This is observed history, not a complete lifetime score. Older events, unconnected sources and delivery gaps may be absent. Streaks use observed engagement days and the UTC day boundary.'}),h('ul',{},(data.gaps || []).map(gap => h('li',{text:typeof gap === 'string' ? gap.replaceAll('_',' ') : gap.message || gap.reason || 'Some source observations are not available.'}))));
    host.replaceChildren(h('h2',{text:'Your learning history'}),h('p',{text:`Observed since ${since}. Tutorial engagement, required Quest work and reviewed evidence stay distinct.`}),metrics,history,more,feedback,coverage);
  }).catch(error => {if(host.isConnected)host.replaceChildren(h('h2',{text:'Your learning history'}),h('p',{role:'status',text:error.message || 'Saved observations are unavailable. Your current progress is preserved.'}));});
}
registerTreeHouseView('analytics',renderStats);
