import { aliasView, pinScope, dayScope, readUsageScope, writeUsageScope, scopeQuery, FILTERS as usageFilters, VIEWS as usageViews, RANGES as usageRanges } from './usageScope.js';
import { statsRead, usageData } from './usageData.js';
import { renderOverview } from './usageOverview.js';
import { renderMetrics } from './usageMetrics.js';
import { el as uEl, btn as uBtn } from './usageCharts.js';
import { createOpenClankWindow } from './copal/windows.js';
import { createStatsRefreshLifecycle } from './statsLifecycle.js';
import { mountNativeLogs, refreshNativeLogs, disposeNativeLogs } from './nativeLogs.js';

const VIEWS = Object.freeze(['overview', 'quota', 'usage', 'activity', 'trends', 'quality', 'logs']);
const SAFE_RANGES = new Set(['today', '3d', '7d', '30d', '90d', 'all']);
const SAFE_TIMELINE_WINDOWS = new Set(['6h', '12h', '24h']);
const PREF_PREFIX = 'openclank.usage.scope.v2:';
let instance = null;
let refreshGeneration = 0;
let refreshController = null;
const lastGoodByScope = new Map();
let refreshTimer = null;
let statsLifecycle = null;
let accountSelectionSignature = null;
let ownerPreferenceScope = 'anonymous';
let scopeStorageWarning = null;
let usageMode = 'tokens';
let activityContributionMetric = 'messages';
let activityResolution = 'day';
const excludedUsageGroups = new Set();
let trendsOptedIn = false;

function preferenceKey() {
  return `${PREF_PREFIX}${ownerPreferenceScope}`;
}

function metricText(value) {
  if (value == null) return 'Unavailable';
  if (typeof value === 'object') { if (value.value != null || value.amount != null || value.state != null) return value.value ?? value.amount ?? value.state; const entries = Object.entries(value); return entries.length ? entries.map(([currency, amount]) => `${amount} ${currency}`).join(' · ') : 'Unavailable'; }
  return value;
}

function escapeMarkup(value) {
  return String(value ?? '').replace(/[&<>\"']/g, character => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[character]));
}

function readScope() {
  const params = new URLSearchParams(location.search);
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem(preferenceKey()) || '{}'); } catch { scopeStorageWarning = 'Saved Usage preferences were invalid; safe defaults are active.'; }
  const range = SAFE_RANGES.has(params.get('range')) ? params.get('range') : (SAFE_RANGES.has(saved.range) ? saved.range : '30d');
  const view = VIEWS.includes(params.get('view')) ? params.get('view') : 'overview';
  const timelineWindow = SAFE_TIMELINE_WINDOWS.has(params.get('timeline')) ? params.get('timeline') : (SAFE_TIMELINE_WINDOWS.has(saved.timelineWindow) ? saved.timelineWindow : '24h');
  const timezone = typeof params.get('timezone') === 'string' && params.get('timezone').length < 80
    ? params.get('timezone') : (saved.timezone || Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC');
  const safeFilter = key => {
    const value = params.get(key) || saved.filters?.[key] || '';
    if (key === 'account' || key === 'provider') return /^\d{1,3}$/.test(value) || new RegExp('^' + key + '_[a-f0-9]{24}$').test(value) ? value : '';
    return typeof value === 'string' && value.length <= 120 && !/[\u0000-\u001f]/.test(value) ? value : '';
  };
  return { range, view, timezone, timelineWindow, filters: { provider: safeFilter('provider'), model: safeFilter('model'), account: safeFilter('account') } };
}

function writeScope(scope, { replace = false } = {}) {
  const params = new URLSearchParams(location.search);
  params.set('range', scope.range); params.set('view', scope.view); params.set('timezone', scope.timezone); params.set('timeline', scope.timelineWindow || '24h');
  for (const key of ['provider', 'model', 'account']) {
    if (scope.filters?.[key]) params.set(key, scope.filters[key]); else params.delete(key);
  }
  const url = `${location.pathname}?${params}`;
  history[replace ? 'replaceState' : 'pushState']({ usage: true }, '', url);
  try { localStorage.setItem(preferenceKey(), JSON.stringify({ range: scope.range, timezone: scope.timezone, timelineWindow: scope.timelineWindow || '24h', filters: scope.filters })); } catch { scopeStorageWarning = 'Usage preferences could not be saved; this view remains temporary.'; }
}

function scopeKey(scope) {
  return JSON.stringify([ownerPreferenceScope, scope.view, scope.range, scope.timezone, scope.timelineWindow, scope.filters]);
}

function resetOwnerContext() {
  disposeNativeLogs();
  refreshController?.abort(); refreshController = null; refreshGeneration += 1;
  lastGoodByScope.clear(); accountSelectionSignature = null; ownerPreferenceScope = 'anonymous';
  const params = new URLSearchParams(location.search);
  for (const key of ['range', 'timezone', 'timeline', 'provider', 'model', 'account', 'log_session']) params.delete(key);
  history.replaceState({ usage: true }, '', `${location.pathname}?${params}`);
  if (instance) { instance.body.textContent = ''; instance.show(); }
}

function adoptServerOwnerScope(data, scope) {
  const next = typeof data?.owner_scope === 'string' && /^[a-f0-9]{16}$/i.test(data.owner_scope)
    ? data.owner_scope : null;
  if (!next || next === ownerPreferenceScope) return false;
  ownerPreferenceScope = next;
  accountSelectionSignature = null;
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem(preferenceKey()) || '{}'); } catch { scopeStorageWarning = 'Saved Usage preferences were invalid; safe defaults are active.'; }
  const params = new URLSearchParams(location.search);
  if (!params.has('range') && SAFE_RANGES.has(saved.range)) scope.range = saved.range;
  if (!params.has('timezone') && typeof saved.timezone === 'string') scope.timezone = saved.timezone;
  if (!params.has('timeline') && SAFE_TIMELINE_WINDOWS.has(saved.timelineWindow)) scope.timelineWindow = saved.timelineWindow;
  for (const key of ['provider', 'model', 'account']) {
    if (!params.has(key) && typeof saved.filters?.[key] === 'string') scope.filters[key] = saved.filters[key];
  }
  return true;
}

function observedAge(value) {
  if (!value) return null;
  const seconds = Math.max(0, Math.floor((Date.now() - Date.parse(value)) / 1000));
  if (!Number.isFinite(seconds)) return null;
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  return `${Math.floor(seconds / 3600)}h ago`;
}

function resetCountdown(value) {
  if (!value) return null;
  const seconds = Math.floor((Date.parse(value) - Date.now()) / 1000);
  if (!Number.isFinite(seconds)) return null;
  if (seconds <= 0) return 'reset due';
  if (seconds < 3600) return `resets in ${Math.floor(seconds / 60)}m`;
  return `resets in ${Math.floor(seconds / 3600)}h`;
}

function safePercent(numerator, denominator) {
  const n = Number(numerator); const d = Number(denominator);
  return Number.isSafeInteger(n) && n >= 0 && Number.isSafeInteger(d) && d > 0 ? n * 100 / d : null;
}

function numericTimelineSvg(points, hours) {
  if (!points.length) return '';
  const times = points.map(point => Date.parse(point.observed_at)).filter(Number.isFinite);
  const minTime = Math.min(...times); const maxTime = Math.max(...times); const timeRange = Math.max(1, maxTime - minTime);
  const marks = points.map((point, index) => {
    const percent = safePercent(point.numerator, point.denominator) ?? 0;
    const timestamp = Date.parse(point.observed_at);
    const x = times.length <= 1 || !Number.isFinite(timestamp) ? 120 : 12 + ((timestamp - minTime) / timeRange) * 216;
    return `<circle cx="${x.toFixed(1)}" cy="${(88 - Math.min(100, percent) * 0.72).toFixed(1)}" r="4" tabindex="0" role="img" aria-label="Account ${Number(point.account_index) + 1}, ${percent.toFixed(2)} percent used" />${point.reset_at ? `<line x1="${x.toFixed(1)}" y1="8" x2="${x.toFixed(1)}" y2="92" class="stats-timeline-reset" aria-label="Reset marker" />` : ''}`;
  }).join('');
  return `<svg class="stats-usage-timeline-chart" role="img" aria-label="Numeric quota usage over the last ${hours} hours" viewBox="0 0 240 100" tabindex="0"><line x1="12" y1="88" x2="228" y2="88" class="stats-timeline-axis" />${marks}</svg>`;
}

function tokenVolumeSvg(points) {
  if (!points.length) return '<p role="status">Token volume is unavailable for this range.</p>';
  const max = Math.max(1, ...points.map(point => Number(point.tokens) || 0));
  const marks = points.map((point, index) => {
    const x = points.length === 1 ? 120 : 12 + (index / (points.length - 1)) * 216;
    const y = 88 - ((Number(point.tokens) || 0) / max) * 72;
    return `<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="3" tabindex="0" aria-label="${escapeMarkup(point.tokens)} tokens at ${escapeMarkup(point.observed_at)}" />`;
  }).join('');
  return `<svg class="stats-token-volume-chart" role="img" aria-label="Token volume over the last 24 hours" viewBox="0 0 240 100" tabindex="0"><polyline points="${points.map((point, index) => { const x = points.length === 1 ? 120 : 12 + (index / (points.length - 1)) * 216; const y = 88 - ((Number(point.tokens) || 0) / max) * 72; return `${x.toFixed(1)},${y.toFixed(1)}`; }).join(' ')}" fill="none" stroke="currentColor" /><line x1="12" y1="88" x2="228" y2="88" class="stats-timeline-axis" />${marks}</svg>`;
}

function activityMarkup(activity) {
  if (!activity) return '<p role="status">Activity is unavailable for this scope.</p>';
  const summary = activity.summary || {};
  const timeline = activity.timeline?.points || [];
  const contributions = activity.contributions?.points || [];
  const maxMessages = Math.max(1, ...timeline.map(point => Number(point.messages) || 0));
  const points = timeline.map((point, index) => `${timeline.length === 1 ? 120 : 12 + index * 216 / Math.max(1, timeline.length - 1)},${88 - (Number(point.messages) || 0) * 72 / maxMessages}`).join(' ');
  const timelineSvg = `<svg class="stats-activity-timeline" role="img" aria-label="Activity messages over time" viewBox="0 0 240 100" tabindex="0"><polyline fill="none" stroke="currentColor" points="${points}" />${timeline.map((point, index) => `<circle cx="${timeline.length === 1 ? 120 : 12 + index * 216 / Math.max(1, timeline.length - 1)}" cy="${88 - (Number(point.messages) || 0) * 72 / maxMessages}" r="4" tabindex="0" aria-label="${escapeMarkup(point.bucket)}: ${escapeMarkup(point.messages)} messages" />`).join('')}</svg>`;
  const heatPoints = activity.heatmap?.points || [];
  const weekdayLabels = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'];
  const heatmap = `<thead><tr><th scope="col">Day</th>${Array.from({ length: 24 }, (_, hour) => `<th scope="col">${hour}:00</th>`).join('')}</tr></thead><tbody>${Array.from({ length: 7 }, (_, weekday) => `<tr><th scope="row">${weekdayLabels[weekday]}</th>${Array.from({ length: 24 }, (_, hour) => { const point = heatPoints.find(item => item.weekday === weekday && item.hour === hour) || { messages: 0 }; return `<td tabindex="0" title="${escapeMarkup(point.messages)} messages" aria-label="${weekdayLabels[weekday]}, ${hour}:00: ${escapeMarkup(point.messages)} messages" style="--stats-activity-intensity:${Math.min(1, Number(point.messages) / maxMessages)}">${point.messages || ''}</td>`; }).join('')}</tr>`).join('')}</tbody>`;
  const rows = timeline.map(point => `<tr><th>${escapeMarkup(point.bucket)}</th><td>${escapeMarkup(point.messages)}</td><td>${escapeMarkup(point.user_messages)}</td><td>${escapeMarkup(point.assistant_messages)}</td><td>${escapeMarkup(point.sessions)}</td></tr>`).join('');
  const selectedContributionValues = contributions.map(point => activityContributionMetric === 'sessions' ? point.sessions : activityContributionMetric === 'output_tokens' ? point.output_tokens : point.messages).map(Number).filter(Number.isFinite);
  const contributionMaximum = Math.max(1, ...selectedContributionValues);
  const contributionRows = contributions.map(point => `<tr><th>${escapeMarkup(point.day)}</th><td>${escapeMarkup(activityContributionMetric === 'sessions' ? point.sessions : activityContributionMetric === 'output_tokens' ? metricText(point.output_tokens) : point.messages)}</td><td>${escapeMarkup(activityContributionMetric === 'sessions' ? 'sessions' : activityContributionMetric === 'output_tokens' ? (point.output_tokens_state || 'unavailable') : 'messages')}</td></tr>`).join('');
  const contributionGrid = contributions.map(point => { const value = activityContributionMetric === 'sessions' ? point.sessions : activityContributionMetric === 'output_tokens' ? point.output_tokens : point.messages; return `<button type="button" class="stats-contribution-cell" title="${escapeMarkup(point.day)}: ${escapeMarkup(metricText(value))}" aria-label="${escapeMarkup(point.day)}: ${escapeMarkup(metricText(value))}" data-stats-activity-day="${escapeMarkup(point.day)}" style="--stats-activity-intensity:${Math.min(1, (Number(value) || 0) / contributionMaximum)}"></button>`; }).join('');
  const sessions = (activity.sessions?.rows || []).map((row, index) => `<tr><th><button type="button" data-stats-open-session="${escapeMarkup(row.open_handle)}" aria-label="Open activity session ${index + 1}">Open</button> Session ${index + 1}</th><td>${escapeMarkup(metricText(row.message_count))}</td><td>${escapeMarkup(metricText(row.output_tokens))}</td><td>${escapeMarkup(row.tool_count ?? "unknown")} tools · ${escapeMarkup(row.skill_count ?? "unknown")} skills · ${escapeMarkup(metricText(row.measured_tool_ms))} measured ms</td><td>${row.automation ? 'Automated' : 'Human'}</td></tr>`).join('');
  const toolMix = ['tools', 'skills'].map(kind => { const cohort = activity.session_shapes?.[kind]; return `<article><h4>${kind === 'tools' ? 'Tools' : 'Skills'}</h4><p>${escapeMarkup(cohort?.state || 'unavailable')} · ${escapeMarkup(cohort?.calls ?? 'unknown')} observed calls · ${escapeMarkup(cohort?.terminal_calls ?? 'unknown')} terminal · ${escapeMarkup(cohort?.failures ?? 'unknown')} failures · ${escapeMarkup(cohort?.cancelled ?? 'unknown')} cancelled</p><p>Recorded elapsed sum ${escapeMarkup(metricText(cohort?.duration_ms))} ms across ${escapeMarkup(cohort?.duration_population ?? 0)} timed calls; ${escapeMarkup(cohort?.untimed_calls ?? 'unknown')} untimed.</p><small>${escapeMarkup(cohort?.duration_method || 'No timing method available')} · ${escapeMarkup(cohort?.coverage?.state || 'unavailable')}</small></article>`; }).join('');
  const velocityCohorts = ['by_actor', 'by_size'].map(kind => `<section><h4>Response timing ${kind === 'by_actor' ? 'by actor' : 'by size'}</h4><table><thead><tr><th>Cohort</th><th>p50 ms</th><th>p90 ms</th><th>Coverage</th></tr></thead><tbody>${(activity.velocity?.[kind]?.rows || []).map(row => `<tr><th>${escapeMarkup(row.label)}</th><td>${escapeMarkup(metricText(row.p50_ms))}</td><td>${escapeMarkup(metricText(row.p90_ms))}</td><td>${escapeMarkup(row.state || row.p50_ms?.state || 'unavailable')}</td></tr>`).join('') || '<tr><td colspan="4">Unavailable</td></tr>'}</tbody></table></section>`).join('');
  const actorTiming = (activity.actor_comparison || []).map(row => `<li>${escapeMarkup(row.label)} · lifecycle elapsed p50 ${escapeMarkup(metricText(row.timing?.value))} ms · ${escapeMarkup(row.timing?.population ?? 0)} measured · ${escapeMarkup(row.timing?.state || 'unavailable')}</li>`).join('');
  const measuredConcurrency = activity.concurrency?.measured_tools;
  const concurrencyClocks = (measuredConcurrency?.clocks || []).map(clock => `<li>Clock ${escapeMarkup(clock.clock_handle)} · peak ${escapeMarkup(clock.peak)} · ${escapeMarkup(clock.timed_calls)} timed calls</li>`).join('');
  const list = kind => (activity.breakdowns?.[kind] || []).map(row => `<li>${escapeMarkup(row.label)} · ${escapeMarkup(metricText(row.count ?? row.messages))}</li>`).join('');
  return `<section class="stats-activity-view" aria-labelledby="stats-activity-heading"><h3 id="stats-activity-heading">Activity</h3><div class="stats-activity-summary"><article><h4>Messages</h4><strong>${escapeMarkup(metricText(summary.messages))}</strong></article><article><h4>Sessions</h4><strong>${escapeMarkup(metricText(summary.sessions))}</strong></article><article><h4>Active days</h4><strong>${escapeMarkup(metricText(summary.active_days))}</strong></article><article><h4>Output tokens</h4><strong>${escapeMarkup(metricText(summary.output_tokens))}</strong><span>${escapeMarkup(summary.output_tokens_state || 'unavailable')}</span></article></div><label>Contribution metric <select data-stats-activity-contribution><option value="messages" ${activityContributionMetric === 'messages' ? 'selected' : ''}>Messages</option><option value="sessions" ${activityContributionMetric === 'sessions' ? 'selected' : ''}>Sessions</option><option value="output_tokens" ${activityContributionMetric === 'output_tokens' ? 'selected' : ''}>Output tokens</option></select></label><section><h4>Contribution calendar</h4><div class="stats-contribution-grid" role="group" aria-label="Latest 365 display days">${contributionGrid}</div><table><caption>Latest 365 display days · ${escapeMarkup(activityContributionMetric)}</caption><thead><tr><th>Day</th><th>Value</th><th>State/metric</th></tr></thead><tbody>${contributionRows || '<tr><td colspan="3">No activity in this range.</td></tr>'}</tbody></table></section><section><h4>Activity timeline</h4>${timelineSvg}<table><caption>Activity by time bucket</caption><thead><tr><th>Bucket</th><th>Messages</th><th>User</th><th>Assistant</th><th>Sessions</th></tr></thead><tbody>${rows || '<tr><td colspan="5">No activity in this range.</td></tr>'}</tbody></table></section><section><h4>7×24 activity heatmap</h4><table class="stats-activity-heatmap"><caption>Messages by weekday and hour</caption>${heatmap || '<tbody><tr><td>No activity</td></tr></tbody>'}</table></section><section><h4>Workspaces</h4><ul>${list('workspace') || '<li>Unavailable</li>'}</ul></section><section><h4>Models</h4><ul>${list('model') || '<li>Unavailable</li>'}</ul></section><section><h4>Actors</h4><ul>${list('actor') || '<li>Unavailable</li>'}</ul></section><section><h4>Sessions</h4><table><caption>Activity sessions</caption><thead><tr><th>Session</th><th>Messages</th><th>Output tokens</th><th>Observed tool/skill shape</th><th>Type</th></tr></thead><tbody>${sessions || '<tr><td colspan="4">No activity sessions.</td></tr>'}</tbody></table></section><p>Automation: ${escapeMarkup(metricText(activity.automation?.automation))} · Human: ${escapeMarkup(metricText(activity.automation?.human))} · Subagents: ${escapeMarkup(metricText(activity.automation?.subagent))} · Velocity p50: ${escapeMarkup(metricText(activity.velocity?.p50_ms))} ms · Concurrency peak: ${escapeMarkup(metricText(activity.concurrency?.peak))}</p><section><h4>Observed tool and skill mix</h4>${toolMix}</section>${velocityCohorts}<section><h4>Actor lifecycle timing</h4><ul>${actorTiming || "<li>Unavailable</li>"}</ul><p>Elapsed lifecycle time is not active processing time.</p></section><section><h4>Measured tool concurrency</h4><p>${escapeMarkup(measuredConcurrency?.state || "unavailable")} · ${escapeMarkup(measuredConcurrency?.method || "no measured clock")}</p><ul>${concurrencyClocks || "<li>No measured intervals</li>"}</ul></section>${activity.coverage?.state === 'partial' ? '<p role="status">Partial activity projection; bounded rows were truncated.</p>' : ''}</section>`;
}

function qualityMarkup(quality) {
  if (!quality) return '<p class="stats-quality-view" role="status">Quality is unavailable for this scope.</p>';
  const families = Object.entries(quality.families || {});
  const cards = families.map(([name, family]) => { const familyMaximum = Math.max(1, ...(family.sparkline || []).map(Number).filter(Number.isFinite)); return `<article><h4>${escapeMarkup(name.replaceAll('_', ' '))}</h4><strong>${escapeMarkup(family.state || 'unavailable')}</strong> <span>${family.affected ?? 0} affected · ${family.coverage?.covered ?? 0}/${family.coverage?.total ?? 0} covered</span><svg class="stats-quality-sparkline" role="img" aria-label="${escapeMarkup(name)} trend" viewBox="0 0 120 30"><polyline fill="none" stroke="currentColor" points="${(family.sparkline || []).map((value, index, values) => `${values.length <= 1 ? 60 : index * 120 / (values.length - 1)},${28 - Math.min(1, (Number(value) || 0) / familyMaximum) * 24}`).join(' ')}" /></svg><table><caption>${escapeMarkup(name)} evidence</caption><tbody>${(family.evidence || []).map(evidence => { const item = typeof evidence === 'object' && evidence ? evidence : { id: evidence }; const handle = item.session_handle || item.source_ref?.session_handle; return `<tr><td>${escapeMarkup(item.id || item.evidence_id || 'Observed evidence')} · ${escapeMarkup(item.source || item.source_ref?.authority || 'source identity')} · body ${escapeMarkup(item.body_state || 'unavailable')} ${handle ? `<button type="button" data-stats-log-evidence="${escapeMarkup(handle)}">Open saved evidence</button>` : '<span>Saved body/source session unavailable</span>'}</td></tr>`; }).join('') || '<tr><td>Evidence unavailable</td></tr>'}</tbody></table></article>`; }).join('');
  return `<section class="stats-quality-view" aria-labelledby="stats-quality-heading"><h3 id="stats-quality-heading">Quality</h3><p>Formula ${escapeMarkup(quality.formula_version || 'version unavailable')} · ${escapeMarkup(quality.state || 'unavailable')} · Score ${quality.score == null ? 'unavailable' : escapeMarkup(quality.score)}${quality.grade ? ` (${escapeMarkup(quality.grade)})` : ''}</p><div class="stats-quality-grid">${cards || '<p role="status">No quality evidence is available for this scope.</p>'}</div><p class="stats-quality-capability">VCS history and Insights remain unavailable until their explicit capabilities are enabled.</p></section>`;
}

function trendsMarkup(trends) {
  if (!trendsOptedIn) return '<section class="stats-trends-view"><h3>Trends</h3><p>Trend analysis reads message content only after explicit opt-in. Terms are sent in a transient request body and are not saved.</p><button type="button" data-stats-trends-opt-in>Enable content trend analysis</button></section>';
  if (!trends) return '<p class="stats-trends-view" role="status">Trends are unavailable for this scope.</p>';
  const groups = trends.groups || [];
  const rows = groups.flatMap(group => (group.points || []).map(point => `<tr><th>${escapeMarkup(group.label)}</th><td>${escapeMarkup(point.bucket)}</td><td>${escapeMarkup(point.occurrences)}</td><td>${escapeMarkup(point.per_1000_messages ?? 'Unavailable')}</td></tr>`)).join('');
  return `<section class="stats-trends-view" aria-labelledby="stats-trends-heading"><h3 id="stats-trends-heading">Trends</h3><p>Content analysis is enabled for this request only. ${escapeMarkup(trends.message_count ?? 0)} owner-scoped messages scanned · ${escapeMarkup(trends.state || 'unavailable')}.</p><div class="stats-trends-charts">${groups.map(group => `<article><h4>${escapeMarkup(group.label)}</h4><svg role="img" aria-label="${escapeMarkup(group.label)} trend" viewBox="0 0 240 80"><polyline fill="none" stroke="currentColor" points="${(group.points || []).map((point, index, values) => `${values.length <= 1 ? 120 : index * 240 / (values.length - 1)},${70 - Math.min(60, Number(point.occurrences) || 0)}`).join(' ')}" /></svg><strong>${escapeMarkup(group.occurrences)} occurrences</strong></article>`).join('')}</div><table><caption>Trend values by time bucket</caption><thead><tr><th>Group</th><th>Bucket</th><th>Occurrences</th><th>Per 1,000</th></tr></thead><tbody>${rows || '<tr><td colspan="4">No matching content.</td></tr>'}</tbody></table>${trends.truncated ? '<p role="status">Partial trend scan; the bounded message window was truncated.</p>' : ''}</section>`;
}

function renderLegacy(windowApi, scope, state) {
  const nav = VIEWS.map(view => `<button type="button" class="stats-usage-tab${scope.view === view ? ' is-active' : ''}" data-stats-view="${view}" aria-current="${scope.view === view ? 'page' : 'false'}">${view[0].toUpperCase() + view.slice(1)}</button>`).join('');
  const status = state.loading ? 'Refreshing…' : state.error ? (state.data ? 'Showing last available data · Retry available' : 'Unavailable · Retry') : state.partial ? 'Partial data' : 'Ready';
  const quotaState = state.quota?.account_status;
  const message = state.error || (quotaState === 'unavailable' ? 'The requested provider account is unavailable or unsupported.' : state.quota?.truncation?.windows || state.quota?.timeline_truncated ? 'Partial adapter data: the bounded quota projection was truncated.' : state.data ? 'Usage data is available through the owner-scoped Stats API. Current-session tokens and cost are unavailable until an attributed native fact exists; cost remains unpriced without an admitted schedule.' : 'No recent activity is available for this view yet.');
  const quotaObservations = state.quota?.observations || [];
  const providerIds = [...new Set(quotaObservations.map(item => item.provider_id).filter(Boolean))].sort();
  const providerLabel = providerId => { const item = quotaObservations.find(row => row.provider_id === providerId && row.provider_label); return item?.provider_label || `Provider ${Math.max(0, providerIds.indexOf(providerId)) + 1}`; };
  const officialUsageLinks = providerIds.map(providerId => {
    const label = providerLabel(providerId).toLowerCase();
    const url = label.includes('claude') || label.includes('anthropic') ? 'https://claude.ai/settings/usage' : label.includes('codex') || label.includes('openai') ? 'https://chatgpt.com/codex/settings/usage' : null;
    return url ? `<button type="button" data-stats-official-usage="${escapeMarkup(url)}">Open ${escapeMarkup(providerLabel(providerId))} usage settings</button>` : '';
  }).join('');
  const selectedProviderIndex = /^\d{1,3}$/.test(scope.filters.provider) ? Number(scope.filters.provider) : -1;
  const selectedProviderId = selectedProviderIndex >= 0 ? providerIds[selectedProviderIndex] || null : quotaObservations.find(row => row.provider_handle === scope.filters.provider)?.provider_id || null;
  // Quota observations have no model identity; do not render a misleading
  // model selector until the provider adapter supplies one.
  const modelIds = [];
  const selectedModelId = modelIds.includes(scope.filters.model) ? scope.filters.model : null;
  const accountIds = [...new Set(quotaObservations.map(item => item.account_id).filter(Boolean))].sort();
  const accountSignature = `${ownerPreferenceScope}:${accountIds.join('\u001f')}`;
  if (['quota', 'overview'].includes(scope.view) && accountSelectionSignature !== null && accountSelectionSignature !== accountSignature && scope.filters.account) {
    scope.filters.account = '';
    writeScope(scope, { replace: true });
  }
  accountSelectionSignature = accountSignature;
  const selectedAccountId = /^\d{1,3}$/.test(scope.filters.account) ? accountIds[Number(scope.filters.account)] : quotaObservations.find(row => row.account_handle === scope.filters.account)?.account_id || null;
  const quota = (scope.view === 'quota' || scope.view === 'overview') && state.quota
    ? quotaObservations.filter(item => (!selectedProviderId || item.provider_id === selectedProviderId) && (!selectedModelId || item.model_id === selectedModelId) && (!selectedAccountId || item.account_id === selectedAccountId)) : [];
  const headline = quota.filter(item => item.is_headline === true).reduce((best, item) => {
    const denominator = Number(item.utilization_denominator);
    const numerator = Number(item.utilization_numerator);
    if (!Number.isSafeInteger(denominator) || denominator <= 0 || !Number.isSafeInteger(numerator) || numerator < 0) return best;
    const percent = numerator * 100 / denominator;
    return !best || percent > best.percent ? { percent, item } : best;
  }, null);
  const accountFilter = (scope.view === 'quota' || scope.view === 'overview') && accountIds.length
    ? `<label>Account <select data-stats-filter="account" aria-label="Filter by account"><option value="">All accounts</option>${accountIds.map((id, index) => { const row = quotaObservations.find(item => item.account_id === id); return `<option value="${index}" ${scope.filters.account === String(index) ? 'selected' : ''}>${escapeMarkup(row?.account_label || `Account ${index + 1}`)}</option>`; }).join('')}</select></label>` : '';
  const activeFilters = Object.entries(scope.filters || {}).filter(([, value]) => value).map(([key, value]) => { const label = key === 'account' ? (quotaObservations.find(item => item.account_id === accountIds[Number(value)])?.account_label || (value.startsWith('account_') ? 'Selected account' : `Account ${Number(value) + 1}`)) : key === 'provider' ? (selectedProviderId ? providerLabel(selectedProviderId) : 'Selected provider') : key === 'model' ? 'Selected model' : value; return `<button type="button" class="stats-filter-chip" data-stats-clear-filter="${key}" aria-label="Clear ${key} filter">${escapeMarkup(key)}: ${escapeMarkup(label)} ×</button>`; }).join('');
  const unsupportedKeys = [...new URLSearchParams(location.search).keys()].filter(key => !['range', 'view', 'timezone', 'timeline', 'provider', 'model', 'account', 'log_session'].includes(key));
  const scopeNotice = `${unsupportedKeys.length ? `<p class="stats-scope-warning" role="status">Unsupported URL filter${unsupportedKeys.length > 1 ? 's' : ''} ignored: ${escapeMarkup(unsupportedKeys.join(', '))}.</p>` : ''}${scopeStorageWarning ? `<p class="stats-scope-warning" role="status">${escapeMarkup(scopeStorageWarning)}</p>` : ''}`;
  const quotaMarkup = quota.length ? `${officialUsageLinks ? `<aside class="stats-official-usage-links" aria-label="Official usage settings">${officialUsageLinks}<small>Opens the provider’s official usage page in a new tab.</small></aside>` : ''}<div class="stats-quota-grid" role="list">${quota.map(item => {
    const denominator = Number.isSafeInteger(item.utilization_denominator) && item.utilization_denominator > 0 ? item.utilization_denominator : null;
    const numerator = Number.isSafeInteger(item.utilization_numerator) && item.utilization_numerator >= 0 ? item.utilization_numerator : null;
    const percent = safePercent(numerator, denominator);
    const clamped = percent === null ? 0 : Math.max(0, Math.min(100, percent));
    const stateLabel = item.state || 'unavailable';
    const threshold = percent === null ? 'unavailable' : percent >= 100 ? 'critical' : percent >= 90 ? 'warning' : percent >= 70 ? 'watch' : 'normal';
    const delta = item.signed_delta ? ` · Local delta ${item.signed_delta.numerator >= 0 ? '+' : ''}${item.signed_delta.numerator}/${item.signed_delta.denominator}` : '';
    const freshness = observedAge(item.observed_at);
    const countdown = resetCountdown(item.reset_at);
    const sourceLabel = item.state === 'official' ? 'Official API capacity' : item.state === 'local' ? 'Local diagnostic estimate' : item.state === 'stale' ? 'Stale last-good' : item.state === 'unsupported' ? 'Unsupported' : item.state === 'expired' ? 'Expired' : item.state === 'permission' || item.error_class === 'forbidden' ? 'Permission denied' : item.state === 'error' && item.error_class === 'unavailable' ? 'Offline' : item.state === 'error' ? 'Provider error' : 'Unavailable';
    const accountLabel = item.account_label || (accountIds.indexOf(item.account_id) >= 0 ? `Account ${accountIds.indexOf(item.account_id) + 1}` : 'Unattributed account');
    const amount = item.limit_value != null ? ` · Limit ${escapeMarkup(item.limit_value)}${item.remaining_value != null ? ` · Remaining ${escapeMarkup(item.remaining_value)}` : ''}` : '';
    return `<article class="stats-quota-card stats-quota-${threshold}" role="listitem"><h3>${escapeMarkup(accountLabel)} · ${escapeMarkup(providerLabel(item.provider_id))} · ${escapeMarkup(item.label || item.window_kind || 'Provider window')}</h3><div class="stats-quota-ring" role="img" aria-label="${percent === null ? 'Usage unavailable' : `${percent.toFixed(2)} percent used; ${threshold}`}" style="--stats-quota-progress:${clamped}%"></div><p><strong>${percent === null ? 'Unavailable' : `${percent.toFixed(2)}% used`}</strong> <span>${escapeMarkup(stateLabel)}</span></p><p class="stats-quota-detail">${amount}${item.reset_at ? ` · Reset ${escapeMarkup(item.reset_at)}${countdown ? ` (${countdown})` : ''}` : ' · No reset supplied'} · ${freshness || 'No observation'} · ${sourceLabel}${delta}</p>${item.diagnostics?.length ? `<details><summary>Diagnostics (${item.diagnostics.length})</summary><p>Official headline remains authoritative; local/error observations are diagnostic.</p></details>` : ''}</article>`;
  }).join('')}</div>` : ((scope.view === 'quota' || scope.view === 'overview') ? '<p class="stats-quota-empty">No configured provider quota is currently available. Passive API metadata is required.</p>' : '');
  const timelineHours = Number.parseInt(scope.timelineWindow || '24', 10);
  const timelineCutoff = Date.now() - timelineHours * 3600 * 1000;
  const timelinePoints = [...new Map((state.quota?.timeline || []).filter(point => Date.parse(point.observed_at) >= timelineCutoff && (!selectedProviderId || point.provider_id === selectedProviderId) && (!selectedAccountId || accountIds[Number(point.account_index)] === selectedAccountId)).map(point => [`${point.account_index}:${point.provider_id}:${point.window_id || 'unknown'}:${point.observed_at}`, point])).values()];
  const timelineRows = timelinePoints.map(point => { const ratio = Number(point.denominator) > 0 ? Number(point.numerator) / Number(point.denominator) : 0; return `<tr><th scope="row">Account ${Number(point.account_index) + 1}</th><td>${escapeMarkup(providerLabel(point.provider_id))}</td><td>${point.numerator}/${point.denominator}</td><td><span class="stats-timeline-bar" role="img" aria-label="${Math.round(ratio * 100)} percent used" style="--stats-timeline-progress:${Math.max(0, Math.min(100, ratio * 100))}%"></span></td><td>${point.reset_at ? `Reset ${escapeMarkup(point.reset_at)}` : '—'}</td><td>${escapeMarkup(observedAge(point.observed_at) || point.observed_at)}</td></tr>`; }).join('');
  const chartMarkup = numericTimelineSvg(timelinePoints, timelineHours);
  const timelineMarkup = timelineRows ? `<section aria-labelledby="stats-timeline-heading"><h3 id="stats-timeline-heading">${timelineHours}-hour usage timeline</h3><p>Each attributed observation is shown once; shared usage remains unattributed.</p>${chartMarkup}<table><thead><tr><th scope="col">Account</th><th scope="col">Provider</th><th scope="col">Used</th><th scope="col">Numeric trend</th><th scope="col">Reset marker</th><th scope="col">Observed</th></tr></thead><tbody>${timelineRows}</tbody></table></section>` : '<p class="stats-usage-timeline" role="status">24-hour timeline and explicitly attributed current-session usage are unavailable until qualified observations exist.</p>';
  const tokenVolumeMarkup = tokenVolumeSvg(state.quota?.token_volume || []);
  const isQuotaResource = scope.view === 'quota' || scope.view === 'overview';
  const modelFilter = modelIds.length ? `<label>Model <select data-stats-filter="model" aria-label="Filter by model"><option value="">All models</option>${modelIds.map(model => `<option value="${escapeMarkup(model)}" ${scope.filters.model === model ? 'selected' : ''}>Model ${modelIds.indexOf(model) + 1}</option>`).join('')}</select></label>` : '';
  const providerFilter = providerIds.length ? `<label>Provider <select data-stats-filter="provider" aria-label="Filter by provider"><option value="">All providers</option>${providerIds.map((provider, index) => `<option value="${index}" ${selectedProviderIndex === index ? 'selected' : ''}>${escapeMarkup(providerLabel(provider))}</option>`).join('')}</select></label>` : '';
  const overallRing = headline ? `<div class="stats-overall-ring" role="img" aria-label="Overall maximum ${headline.percent.toFixed(2)} percent used" style="--stats-quota-progress:${Math.max(0, Math.min(100, headline.percent))}%"></div>` : '<div class="stats-overall-ring" role="img" aria-label="Overall usage unavailable"></div>';
  const overviewStrip = scope.view === 'overview' ? `<section class="stats-overview-quota-strip" aria-label="Quota overview"><strong>Quota overview</strong>${overallRing}<span>${headline ? `Displayed headline ${headline.percent.toFixed(2)}%` : 'Provider-designated headline unavailable for these multiple windows'}</span><small>Aggregation: one provider-designated window per account; unlike windows are not merged.</small></section>` : '';
  const session = state.quota?.current_session;
  const sessionMarkup = session?.attributed ? `<section class="stats-current-session" aria-labelledby="stats-current-session-heading"><h3 id="stats-current-session-heading">Current session</h3><p>${session.tokens != null ? `Tokens ${escapeMarkup(session.tokens)}` : 'Token usage unavailable'} · ${session.cost?.state === 'unpriced' ? 'Cost unpriced' : session.cost?.value != null ? `Cost ${escapeMarkup(session.cost.value)}` : 'Cost unavailable'}${session.truncated ? ' · Partial: newest bounded observations shown' : ''}</p></section>` : '<p class="stats-current-session" role="status">Current-session tokens and cost are unavailable until a native operation is explicitly attributed; cost remains unpriced without an admitted schedule.</p>';
  const usage = state.usage || {};
  const usageTotals = usage.summary?.totals || usage.summary?.total || {};
  const usageBuckets = Array.isArray(usage.buckets?.buckets) ? usage.buckets.buckets : [];
  const usageGroups = Array.isArray(usage.groups?.groups) ? usage.groups.groups : [];
  const usageModeButtons = scope.view === "usage" ? `<div class="stats-usage-modes" role="group" aria-label="Usage mode"><button type="button" data-stats-mode="tokens" aria-pressed="${usageMode === "tokens"}">Tokens</button><button type="button" data-stats-mode="cost" aria-pressed="${usageMode === "cost"}">Cost</button></div>` : "";
  const metricCard = (label, value, state = "reported") => `<article class="stats-usage-metric"><h4>${escapeMarkup(label)}</h4><strong>${escapeMarkup(metricText(value))}</strong><span>${escapeMarkup(state)}</span></article>`;
  const inputTotal = usageTotals.input_tokens, outputTotal = usageTotals.output_tokens;
  const cacheRead = usageTotals.cache_read_tokens, cacheWrite = usageTotals.cache_write_tokens, reasoning = usageTotals.reasoning_tokens;
  const groupsForDisplay = usageGroups.slice(0, 5);
  const groupOther = usageGroups.slice(5).reduce((n, row) => n + (Number(row.value?.value ?? row.value) || 0), 0);
  const costSources = usageMode === 'cost' ? `<details><summary>Price source and coverage</summary><p>Admitted pricing schedule revisions · formula ${escapeMarkup(usage.cost?.formula_revision || 'unavailable')}. Source as-of date is unavailable in this projection.</p><table><thead><tr><th>Category</th><th>Exact amount</th><th>State</th><th>Schedule revision</th></tr></thead><tbody>${(usage.cost?.components || []).slice(0, 100).map(component => `<tr><th>${escapeMarkup(component.category)}</th><td>${escapeMarkup(component.amount ?? 'Unavailable')} ${escapeMarkup(component.currency || '')}</td><td>${escapeMarkup(component.state)}${component.reason ? ' · ' + escapeMarkup(component.reason) : ''}</td><td>${escapeMarkup(component.revision || 'unavailable')}</td></tr>`).join('') || '<tr><td colspan="4">No admitted price components</td></tr>'}</tbody></table>${(usage.cost?.components || []).length > 100 ? '<p>First 100 components shown; totals cover the bounded admitted projection.</p>' : ''}</details>` : '';
  const priorSummary = usage.prior?.summary;
  const priorTotals = priorSummary?.totals || priorSummary?.total || {};
  const priorMarkup = `<section><h4>Previous equal period</h4><p>${escapeMarkup(usage.prior?.coverage || (scope.range === 'all' ? 'Unavailable for all retained time; choose a bounded range.' : 'Unavailable'))}</p>${priorSummary ? `<table><thead><tr><th>Measure</th><th>Current</th><th>Previous</th></tr></thead><tbody>${['input_tokens', 'output_tokens', 'cache_read_tokens', 'cache_write_tokens', 'reasoning_tokens'].map(key => `<tr><th>${escapeMarkup(key.replaceAll('_', ' '))}</th><td>${escapeMarkup(metricText(usageTotals[key]))}</td><td>${escapeMarkup(metricText(priorTotals[key]))}</td></tr>`).join('')}</tbody></table><p>Previous estimated cost by currency: ${escapeMarkup(JSON.stringify(usage.prior?.cost?.totals?.estimated || {}))} · ${escapeMarkup(usage.prior?.cost?.coverage?.state || 'unpriced')}</p>` : ''}</section>`;
  const comparisonChoices = usage.sessions?.choices || {};
  const compareOptions = dimension => (comparisonChoices[dimension] || []).map(choice => `<option value="${escapeMarkup(choice.handle)}">${escapeMarkup(choice.label)}</option>`).join('');
  const comparisonMarkup = `<section class="stats-comparison"><h4>Compare</h4><label>Group <select data-stats-compare-dimension><option value="actual_model">Model</option><option value="workspace_id">Workspace</option></select></label><label>Metric <select data-stats-compare-metric><option value="tokens">Tokens</option><option value="cost">Cost</option></select></label><label>Normalize <select data-stats-compare-normalization><option value="total">Total</option><option value="per_session">Per session</option></select></label><label>Left <select data-stats-compare-side="left">${compareOptions('actual_model')}</select></label><label>Right <select data-stats-compare-side="right">${compareOptions('actual_model')}</select></label><button type="button" data-stats-compare ${(comparisonChoices.actual_model || []).length < 2 ? 'disabled' : ''}>Compare</button><p data-stats-compare-result role="status">Choose two observed groups. Token comparisons do not require pricing.</p></section>`;
  const usageMarkup = scope.view === "usage" ? `${usageModeButtons}<section class="stats-usage-breakdown" aria-labelledby="stats-usage-breakdown-heading"><h3 id="stats-usage-breakdown-heading">${usageMode === "cost" ? "Cost" : "Token"} usage</h3><div class="stats-usage-metrics">${usageMode === "cost" ? metricCard("Billed", usage.cost?.totals?.billed || usage.cost?.totals?.estimated, usage.cost?.coverage?.state || "unpriced") + metricCard("Estimated", usage.cost?.totals?.estimated, "estimated") + metricCard("Unpriced", usage.cost?.coverage?.unpriced, usage.cost?.coverage?.unpriced ? "partial_unpriced" : "reported") : metricCard("Input", inputTotal?.value ?? inputTotal, inputTotal?.state || "unavailable") + metricCard("Output", outputTotal?.value ?? outputTotal, outputTotal?.state || "unavailable") + metricCard("Cache read", cacheRead?.value ?? cacheRead, cacheRead?.state || "unavailable") + metricCard("Cache write", cacheWrite?.value ?? cacheWrite, cacheWrite?.state || "unavailable") + metricCard("Reasoning", reasoning?.value ?? reasoning, reasoning?.state || "unavailable")}</div><p>Events ${escapeMarkup(metricText(usageTotals.event_count ?? (usageGroups.reduce((n, row) => n + (Number(row.count) || 0), 0) || "Unavailable")))} · Active days ${escapeMarkup(metricText(usageTotals.active_days))} · Peak day ${escapeMarkup(metricText(usageTotals.peak_day))}</p><p>Cache rate ${escapeMarkup(metricText(usage.cache?.cache_rate?.value ?? usage.cache?.value))} · ${escapeMarkup(usage.cache?.state || usage.cache?.coverage?.state || "unavailable")}</p><h4>Attribution</h4>${groupsForDisplay.length ? `<ol>${groupsForDisplay.map((row, index) => { const excluded = excludedUsageGroups.has(String(index)); const value = Number(row.value?.value ?? row.value) || 0; return excluded ? "" : `<li><button type="button" data-stats-exclude="${index}" aria-pressed="false">Exclude</button> ${escapeMarkup(row.label || (row.key ? `Model ${index + 1}` : row.group || "Unknown"))} · ${escapeMarkup(metricText(row.value))} · ${escapeMarkup(row.state || "reported")}</li>`; }).join("")}${usageGroups.length > 5 ? `<li>Other · ${escapeMarkup(groupOther)}</li>` : ""}</ol><div class="stats-attribution-treemap" role="img" aria-label="Attribution treemap">${groupsForDisplay.map((row, index) => excludedUsageGroups.has(String(index)) ? "" : `<span style="--stats-group-share:${Math.max(1, Number(row.value?.value ?? row.value) || 1)}" title="${escapeMarkup(row.label || `Model ${index + 1}`)}">${escapeMarkup(row.label || `Model ${index + 1}`)}</span>`).join("")}</div>${excludedUsageGroups.size ? '<button type="button" data-stats-clear-exclusions>Restore excluded attribution</button>' : ""}` : "<p role=\"status\">Attribution is unavailable for this scope.</p>"}<section class="stats-top-sessions" aria-labelledby="stats-top-sessions-heading"><h4 id="stats-top-sessions-heading">Top Sessions</h4><label>Rank by <select data-stats-session-rank><option value="tokens">Tokens</option><option value="cost">Cost</option></select></label>${usage.sessions?.sessions?.length ? `<ol>${usage.sessions.sessions.map((row, index) => `<li><button type="button" data-stats-open-session="${escapeMarkup(row.handle)}" aria-label="Open session ${index + 1}">Open</button> Session ${index + 1} · ${escapeMarkup(metricText(row.tokens))} · ${escapeMarkup(row.coverage?.state || "unavailable")}</li>`).join("")}</ol>` : '<p role="status">Top Sessions unavailable: owner-checked projection has no rows.</p>'}</section>${costSources}${comparisonMarkup}${priorMarkup}<h4>Token trend</h4>${usageBuckets.length ? `<table><caption>Bounded usage buckets</caption><thead><tr><th>Bucket</th><th>Value</th><th>State</th></tr></thead><tbody>${usageBuckets.slice(0, 1000).map(row => `<tr><th>${escapeMarkup(row.start || row.bucket || "")}</th><td>${escapeMarkup(metricText(row.value ?? row.output_tokens))}</td><td>${escapeMarkup(row.state || "reported")}</td></tr>`).join("")}</tbody></table>` : "<p role=\"status\">Token trend is unavailable for this scope.</p>"}</section>` : "";
  const panel = scope.view === 'logs' ? '<div data-native-log-browser></div>' : scope.view === 'activity' ? activityMarkup(state.data) : scope.view === 'quality' ? qualityMarkup(state.data) : scope.view === 'trends' ? trendsMarkup(state.data) : `${overviewStrip}${usageMarkup}${quotaMarkup}<p>${escapeMarkup(message)}</p>${sessionMarkup}${timelineMarkup}<section aria-labelledby="stats-token-volume-heading"><h3 id="stats-token-volume-heading">Token volume</h3>${tokenVolumeMarkup}${state.quota?.token_volume_truncated ? '<p role="status">Token volume is partial because the bounded event window was truncated.</p>' : ''}</section>`;
  windowApi.body.innerHTML = `<div class="stats-usage-shell"><nav class="stats-usage-nav" aria-label="Usage sections">${nav}</nav><div class="stats-usage-toolbar"><label>Range <select data-stats-range aria-label="Usage range">${['today','3d','7d','30d','90d','all'].map(r => `<option value="${r}" ${r === scope.range ? 'selected' : ''}>${r}</option>`).join('')}</select></label><label>Timezone <select data-stats-timezone aria-label="Timezone"><option value="${escapeMarkup(scope.timezone)}">${escapeMarkup(scope.timezone)}</option><option value="UTC">UTC</option><option value="America/New_York">America/New_York</option><option value="Europe/London">Europe/London</option></select></label>${scope.view === 'activity' ? `<label>Timeline resolution <select data-stats-activity-resolution aria-label="Activity timeline resolution">${['day','week','month'].map(value => `<option value="${value}" ${value === activityResolution ? 'selected' : ''}>${value}</option>`).join('')}</select></label>` : ''}<label>Timeline <select data-stats-timeline-window aria-label="Timeline duration">${['6h','12h','24h'].map(value => `<option value="${value}" ${value === scope.timelineWindow ? 'selected' : ''}>${value}</option>`).join('')}</select></label>${accountFilter}${providerFilter}${modelFilter}<span class="stats-filter-chips">${activeFilters}</span>${activeFilters ? '<button type="button" data-stats-clear-all>Clear all</button>' : ''}<button type="button" data-stats-alert-preferences aria-pressed="false">Quota alert preferences</button><span class="stats-usage-status" role="status">${escapeMarkup(status)}</span>${state.error ? '<button type="button" data-stats-retry>Retry</button>' : ''}</div>${scopeNotice}<section class="stats-usage-panel" aria-live="polite"><h2>${escapeMarkup(scope.view[0].toUpperCase() + scope.view.slice(1))}</h2>${panel}</section></div>`;
  if (scope.view === 'logs') mountNativeLogs(windowApi.body.querySelector('[data-native-log-browser]'), scope);
  else disposeNativeLogs();
  windowApi.body.querySelectorAll('[data-stats-mode]').forEach(button => button.addEventListener('click', () => { usageMode = button.dataset.statsMode; renderLegacy(windowApi, scope, state); }));
  windowApi.body.querySelectorAll('[data-stats-official-usage]').forEach(button => button.addEventListener('click', () => { const url = button.dataset.statsOfficialUsage; if (/^https:\/\/(claude\.ai\/settings\/usage|chatgpt\.com\/codex\/settings\/usage)$/.test(url)) window.open(url, '_blank', 'noopener,noreferrer'); }));
  windowApi.body.querySelector('[data-stats-trends-opt-in]')?.addEventListener('click', () => { trendsOptedIn = true; refresh(scope, windowApi, { allowPriorGood: false }); });
  windowApi.body.querySelector('[data-stats-activity-contribution]')?.addEventListener('change', event => { activityContributionMetric = event.target.value; renderLegacy(windowApi, scope, state); });
  windowApi.body.querySelector('[data-stats-activity-resolution]')?.addEventListener('change', event => { activityResolution = event.target.value; refresh(scope, windowApi); });
  windowApi.body.querySelector('[data-stats-alert-preferences]')?.addEventListener('click', async event => {
    event.currentTarget.setAttribute('aria-busy', 'true');
    try {
      const current = await fetch('/api/stats/v1/preferences', { credentials: 'same-origin' }).then(response => response.ok ? response.json() : null);
      const preferences = current?.preferences || { quota_alerts: false, quota_thresholds: [80, 90, 100] };
      preferences.quota_alerts = !preferences.quota_alerts;
      const savedResponse = await fetch('/api/stats/v1/preferences', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, credentials: 'same-origin', body: JSON.stringify(preferences) });
      if (!savedResponse.ok) throw new Error('Preferences save failed');
      event.currentTarget.setAttribute('aria-pressed', String(preferences.quota_alerts));
      event.currentTarget.textContent = preferences.quota_alerts ? 'Quota alerts enabled' : 'Quota alerts disabled';
    } catch { event.currentTarget.textContent = 'Quota alert preferences unavailable'; }
    event.currentTarget.removeAttribute('aria-busy');
  });
  windowApi.body.querySelectorAll('[data-stats-log-evidence]').forEach(button => button.addEventListener('click', () => { const params = new URLSearchParams(location.search); params.set('log_session', button.dataset.statsLogEvidence); history.replaceState(history.state, '', `${location.pathname}?${params}`); openStatsUsage({ view: 'logs' }); }));
  windowApi.body.querySelectorAll('[data-stats-open-session]').forEach(button => button.addEventListener('click', () => { window.dispatchEvent(new CustomEvent('openclank:session-open', { detail: { handle: button.dataset.statsOpenSession } })); }));
  windowApi.body.querySelector('[data-stats-session-rank]')?.addEventListener('change', async event => {
    if (!state.usage) return;
    try {
      const response = await fetch(`/api/stats/v1/sessions/top?period=${encodeURIComponent(scope.range)}&timezone=${encodeURIComponent(scope.timezone)}&rank=${encodeURIComponent(event.target.value)}`, { credentials: 'same-origin' });
      if (!response.ok) throw new Error('session ranking unavailable');
      state.usage.sessions = await response.json();
      renderLegacy(windowApi, scope, state);
    } catch { renderLegacy(windowApi, scope, { ...state, partial: true }); }
  });
  windowApi.body.querySelector('[data-stats-compare-dimension]')?.addEventListener('change', event => { const choices = comparisonChoices[event.target.value] || []; for (const side of windowApi.body.querySelectorAll('[data-stats-compare-side]')) side.innerHTML = compareOptions(event.target.value); windowApi.body.querySelector('[data-stats-compare]').disabled = choices.length < 2; const right = windowApi.body.querySelector('[data-stats-compare-side="right"]'); if (right.options.length > 1) right.selectedIndex = 1; });
  const initialRight = windowApi.body.querySelector('[data-stats-compare-side="right"]'); if (initialRight?.options.length > 1) initialRight.selectedIndex = 1;
  windowApi.body.querySelector('[data-stats-compare]')?.addEventListener('click', async () => {
    const left = windowApi.body.querySelector('[data-stats-compare-side="left"]').value; const right = windowApi.body.querySelector('[data-stats-compare-side="right"]').value; const result = windowApi.body.querySelector('[data-stats-compare-result]');
    if (!left || !right || left === right) { result.textContent = 'Choose two different observed groups.'; return; }
    result.textContent = 'Comparing…';
    try { const response = await fetch('/api/stats/v1/compare', { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ handles: [left, right], dimension: windowApi.body.querySelector('[data-stats-compare-dimension]').value, metric: windowApi.body.querySelector('[data-stats-compare-metric]').value, normalization: windowApi.body.querySelector('[data-stats-compare-normalization]').value, period: scope.range === '90d' ? 'custom' : scope.range, ...(scope.range === '90d' ? { start: new Date(Date.now() - 90 * 86400000).toISOString(), end: new Date().toISOString() } : {}), timezone: scope.timezone }) }); const data = await response.json(); if (!response.ok) throw new Error(data.detail || `Comparison failed (${response.status})`); if (!result.isConnected) return; const comparison = data.comparison || {}; result.textContent = comparison.state === 'unavailable' ? `Unavailable: ${comparison.reason || 'coverage unavailable'}` : `${comparison.state}: right − left ${comparison.delta} ${comparison.currency || comparison.unit || ''}${comparison.normalization === 'per_session' ? ' per session' : ''} · ratio ${comparison.ratio ? comparison.ratio.numerator + '/' + comparison.ratio.denominator : 'undefined (zero left)'}${data.coverage?.truncated ? ' · bounded partial coverage' : ''}`; } catch (error) { if (result.isConnected) result.textContent = error.message; }
  });
  windowApi.body.querySelectorAll('[data-stats-exclude]').forEach(button => button.addEventListener('click', () => { excludedUsageGroups.add(button.dataset.statsExclude); renderLegacy(windowApi, scope, state); }));
  windowApi.body.querySelector('[data-stats-clear-exclusions]')?.addEventListener('click', () => { excludedUsageGroups.clear(); renderLegacy(windowApi, scope, state); });
  windowApi.body.querySelectorAll('[data-stats-view]').forEach(button => button.addEventListener('click', () => navigate({view:aliasView(button.dataset.statsView)})));
  windowApi.body.querySelector('[data-stats-range]')?.addEventListener('change', event => navigate({range:event.target.value,start:null,end:null}));
  windowApi.body.querySelector('[data-stats-timezone]')?.addEventListener('change', event => navigate({timezone:event.target.value,start:null,end:null}));
  windowApi.body.querySelector('[data-stats-timeline-window]')?.addEventListener('change', event => { scope.timelineWindow = event.target.value; writeScope(scope); renderLegacy(windowApi, scope, state); });
  const timelinePointsForKeyboard = [...windowApi.body.querySelectorAll('.stats-usage-timeline-chart circle')];
  timelinePointsForKeyboard.forEach((point, index) => point.addEventListener('keydown', event => {
    if (event.key !== 'ArrowRight' && event.key !== 'ArrowLeft') return;
    event.preventDefault(); const next = index + (event.key === 'ArrowRight' ? 1 : -1);
    timelinePointsForKeyboard[(next + timelinePointsForKeyboard.length) % timelinePointsForKeyboard.length]?.focus();
  }));
  const heatmapPoints = [...windowApi.body.querySelectorAll('.stats-activity-heatmap td[tabindex]')];
  heatmapPoints.forEach((point, index) => point.addEventListener('keydown', event => {
    if (!['ArrowRight', 'ArrowLeft', 'ArrowDown', 'ArrowUp'].includes(event.key)) return;
    event.preventDefault(); const delta = event.key === 'ArrowRight' ? 1 : event.key === 'ArrowLeft' ? -1 : event.key === 'ArrowDown' ? 24 : -24;
    heatmapPoints[(index + delta + heatmapPoints.length) % heatmapPoints.length]?.focus();
  }));
  windowApi.body.querySelector('[data-stats-retry]')?.addEventListener('click', () => refresh(scope, windowApi, { allowPriorGood: true }));
  windowApi.body.querySelector('[data-stats-clear-all]')?.addEventListener('click', () => navigate({filters:{provider:'',model:'',account:'',workspace:''}}));
  windowApi.body.querySelectorAll('[data-stats-clear-filter]').forEach(button => button.addEventListener('click', () => navigate({filters:{[button.dataset.statsClearFilter]:''}})));
  windowApi.body.querySelectorAll('[data-stats-filter]').forEach(input => input.addEventListener('change', event => {
    navigate({filters:{[event.target.dataset.statsFilter]:event.target.value.slice(0,160)}});
  }));
}


// Persistent native shell. Legacy secondary presenters retain their full evidence/actions.
let shell = null, currentScope = null, nativeController = null, dashboardRoot = null, pageState = null;
let usageGroup = 'actual_model', usageMetric = 'tokens', chartStyle = 'area', attributionView = 'treemap';
let collections = 'all', windowObserver = null, forceNativeRefresh = false;
const localNavigation = []; let navigationPosition = -1;
function refresh(scope){if(scope)navigate({view:aliasView(scope.view),range:scope.range,timezone:scope.timezone,timelineWindow:scope.timelineWindow,filters:{...scope.filters}},{replace:true});else requestRefresh('control');}
function actualVisible(){return Boolean(instance && !instance.root.classList.contains('hidden') && !instance.root.classList.contains('modal-minimized') && !document.hidden);}
function mountShell(api){
 if(shell)return;
 if(!document.getElementById('usage-style')){const style=document.createElement('link');style.id='usage-style';style.rel='stylesheet';style.href='/static/css/usage.css';document.head.append(style);}
 const root=uEl('div','','usage-app'); const nav=uEl('nav','','usage-main-nav');nav.setAttribute('aria-label','Usage pages');
 for(const [view,label]of [['sessions','Sessions'],['usage','Usage'],['activity','Activity'],['trends','Trends'],['quality','Quality']]){const b=uBtn(label,()=>navigate({view}),'usage-nav-tab');b.dataset.usageView=view;nav.append(b);}
 const compact=uEl('select','','usage-compact-nav');compact.setAttribute('aria-label','Usage page');for(const v of usageViews)compact.append(Object.assign(uEl('option',v[0].toUpperCase()+v.slice(1)),{value:v}));compact.addEventListener('change',()=>navigate({view:compact.value}));nav.append(compact);
 const more=uEl('details','','usage-more');const actions=uEl('div','','usage-more-menu');more.append(uEl('summary','More'),actions);
 for(const [label,view,collection]of [['Account quota','quota'],['Pinned conversations','sessions','pinned'],['Archived conversations','sessions','archived'],['Advanced inspector','advanced']])actions.append(uBtn(label,()=>{collections=collection||'all';more.open=false;navigate({view});}));
 actions.append(uBtn('Logging settings',()=>{more.open=false;void import('./settings.js').then(m=>m.open('logging'));}));
 for(const[label,url]of [['Claude account usage','https://claude.ai/settings/usage'],['Codex account usage','https://chatgpt.com/codex/settings/usage']]){const link=uEl('a',label);link.href=url;link.target='_blank';link.rel='noopener noreferrer';actions.append(link);}nav.append(more);
 const toolbar=uEl('div','','usage-scope-toolbar');const modes=uEl('div','','usage-segments');for(const mode of ['cost','tokens']){const b=uBtn(mode==='cost'?'Cost':'Tokens',()=>{usageMode=mode;usageMetric=mode==='cost'?'cost':'tokens';requestRefresh('control');});b.dataset.usageMode=mode;modes.append(b);}toolbar.append(modes);
 const tokenType=uEl('select');tokenType.setAttribute('aria-label','Token type');for(const[v,label]of [['tokens','Input + output'],['input_tokens','Input'],['output_tokens','Output'],['cache_read_tokens','Cache read'],['cache_write_tokens','Cache write'],['reasoning_tokens','Reasoning']])tokenType.append(Object.assign(uEl('option',label),{value:v}));tokenType.addEventListener('change',()=>{usageMetric=tokenType.value;requestRefresh('control');});toolbar.append(tokenType);
 const range=uEl('select');range.setAttribute('aria-label','Usage date range');for(const v of usageRanges)range.append(Object.assign(uEl('option',v==='today'?'Today':v==='all'?'All time':v==='custom'?'Selected day / custom':'Last '+parseInt(v)+' days'),{value:v}));range.addEventListener('change',()=>navigate({range:range.value,start:null,end:null}));toolbar.append(range);
 const zone=uEl('select');zone.setAttribute('aria-label','Usage timezone');zone.addEventListener('change',()=>navigate({timezone:zone.value,start:null,end:null}));toolbar.append(zone);
 const catalogs={};for(const [alias,label]of [['workspace','Workspace'],['provider','Provider'],['model','Model'],['account','Account']]){const select=uEl('select');select.setAttribute('aria-label',label+' filter');select.dataset.usageFilter=alias;select.append(Object.assign(uEl('option',label+': All'),{value:''}));select.addEventListener('change',()=>navigate({filters:{[alias]:select.value}}));catalogs[alias]=select;toolbar.append(select);}
 toolbar.append(uBtn('↻',()=>requestRefresh('manual'),'usage-refresh'),uBtn('Export CSV',()=>{const url='/api/stats/v1/export.csv?'+scopeQuery(currentScope);const a=uEl('a');a.href=url;a.download='open-clank-usage.csv';a.click();}));
 const chips=uEl('div','','usage-filter-chips');const host=uEl('main','','usage-page-host');const footer=uEl('footer','','usage-footer');footer.setAttribute('role','status');root.append(nav,toolbar,chips,host,footer);api.body.replaceChildren(root);shell={root,nav,compact,toolbar,modes,tokenType,range,zone,catalogs,chips,host,footer};
 api.setNavigationAdapter({canGoBack:()=>navigationPosition>0,canGoForward:()=>navigationPosition<localNavigation.length-1,back:()=>stepNavigation(-1),forward:()=>stepNavigation(1)});
}
function stepNavigation(delta){const next=navigationPosition+delta;if(next<0||next>=localNavigation.length)return;navigationPosition=next;currentScope=localNavigation[next];writeUsageScope(currentScope,ownerPreferenceScope,{replace:true});selectPage();requestRefresh('navigation');instance?.inputContext?.navigationRefresh?.();}
function requestRefresh(reason='manual'){if(['manual','navigation','control','open','owner-change'].includes(reason))forceNativeRefresh=true;if(!statsLifecycle)return;refreshController?.abort();refreshGeneration++;return statsLifecycle.request(reason);}
function navigate(patch={}, {replace=false}={}){
 currentScope=pinScope({...currentScope,...patch,view:aliasView(patch.view||currentScope.view),filters:{...currentScope.filters,...patch.filters}});
 writeUsageScope(currentScope,ownerPreferenceScope,{replace});if(!replace){localNavigation.splice(navigationPosition+1);localNavigation.push(currentScope);navigationPosition=localNavigation.length-1;}
 selectPage();instance?.inputContext?.navigationRefresh?.();requestRefresh('navigation');
}
function selectPage(){
 if(!shell||!currentScope)return;const view=currentScope.view;
 shell.nav.querySelectorAll('[data-usage-view]').forEach(b=>b.setAttribute('aria-current',b.dataset.usageView===view?'page':'false'));shell.compact.value=view;shell.range.value=currentScope.range;
 shell.zone.replaceChildren();for(const value of [...new Set([currentScope.timezone,'UTC',Intl.DateTimeFormat().resolvedOptions().timeZone||'UTC','America/New_York','Europe/London'])])shell.zone.append(Object.assign(uEl('option',value),{value}));shell.zone.value=currentScope.timezone;
 shell.modes.hidden=view!=='usage';shell.tokenType.hidden=view!=='usage'||usageMode==='cost';shell.tokenType.value=usageMetric;for(const b of shell.modes.children)b.setAttribute('aria-pressed',String(b.dataset.usageMode===usageMode));
 const unsupported=view==='quality'||view==='trends';for(const select of Object.values(shell.catalogs))select.hidden=unsupported;shell.range.hidden=view==='trends';shell.zone.hidden=view==='trends';shell.catalogs.model.hidden=unsupported||view==='quota';shell.catalogs.workspace.hidden=unsupported||view==='quota';shell.catalogs.provider.hidden=unsupported||view==='quota';
 shell.chips.replaceChildren();if(view==='quality'||view==='trends')shell.chips.append(uEl('span',view==='quality'?'Quality uses the selected dates across all identities.':'Trends uses opted-in content across the owner archive; date and identity filters do not apply.','usage-coverage'));if(currentScope.timezoneWarning)shell.chips.append(uEl('span',currentScope.timezoneWarning,'usage-coverage'));for(const [alias,value]of Object.entries((view==='quality'||view==='trends')?{}:currentScope.filters||{})){if(!value)continue;const select=shell.catalogs[alias];const name=[...(select?.options||[])].find(o=>o.value===value)?.textContent||'Selected '+alias;shell.chips.append(uBtn(name+' ×',()=>navigate({filters:{[alias]:''}}),'usage-filter-chip'));}if(currentScope.range==='custom')shell.chips.append(uBtn(currentScope.start.slice(0,10)+' ×',()=>navigate({range:'30d',start:null,end:null}),'usage-filter-chip'));
 const changed=shell.host.dataset.view!==view;if(!changed)return;
 disposeNativeLogs();nativeController=null;dashboardRoot=null;shell.host.replaceChildren();shell.host.dataset.view=view;pageState=null;
 if(view==='sessions'||view==='advanced')nativeController=mountNativeLogs(shell.host,currentScope,{mode:view==='advanced'?'advanced':'sessions',collection:collections,renderDashboard:container=>{dashboardRoot=container;renderOverview(container,pageState?.data||{},overviewOptions());},onSessionChange:handle=>{if(handle)dashboardRoot=null;},onScopeChange:patch=>navigate(patch)});
}
function openSaved(handle){if(!handle)return;const params=new URLSearchParams(location.search);params.set('log_session',handle);history.replaceState(history.state,'',location.pathname+'?'+params);if(currentScope.view!=='sessions'){currentScope=pinScope({...currentScope,view:'sessions'});writeUsageScope(currentScope,ownerPreferenceScope,{replace:true});selectPage();requestRefresh('navigation');}nativeController?.openSession(handle);}
function onDay(day){if(!/^\d{4}-\d{2}-\d{2}$/.test(day||''))return;const d=new Date(day+'T12:00:00Z');const next=new Date(d);next.setUTCDate(next.getUTCDate()+1);navigate({view:'sessions',...dayScope(day,currentScope.timezone)});}
function overviewOptions(){return{contribution:activityContributionMetric,resolution:activityResolution,onContribution:value=>{activityContributionMetric=value;renderPage();},onResolution:value=>{activityResolution=value;requestRefresh('control');},onDay,onOpen:openSaved,onFilter:(alias,value)=>{if(value)navigate({filters:{[alias]:value}});}};}
function metricsOptions(){return{scope:currentScope,mode:usageMode,metric:usageMetric,group:usageGroup,chartStyle,attribution:attributionView,excluded:excludedUsageGroups,onOpen:openSaved,onDay,onGroup:value=>{usageGroup=value;excludedUsageGroups.clear();requestRefresh('control');},onChartStyle:value=>{chartStyle=value;renderPage();},onAttribution:value=>{attributionView=value;renderPage();},onExclude:key=>{excludedUsageGroups.has(key)?excludedUsageGroups.delete(key):excludedUsageGroups.add(key);renderPage();},onRestore:()=>{excludedUsageGroups.clear();renderPage();},onCompare:payload=>statsRead('compare',currentScope,refreshController?.signal,{}, {...payload,period:currentScope.start?'custom':currentScope.range,timezone:currentScope.timezone,start:currentScope.start,end:currentScope.end,filters:Object.fromEntries(Object.entries(usageFilters).map(([alias,field])=>[field,currentScope.filters?.[alias]]).filter(([,v])=>v))})};}
function renderPage(){if(!shell||!pageState)return;const scroll=shell.host.scrollTop;const focus=document.activeElement;const focusText=focus&&shell.host.contains(focus)?focus.getAttribute('aria-label')||focus.textContent:null;
 if(currentScope.view==='sessions'){if(dashboardRoot?.isConnected)renderOverview(dashboardRoot,{...(pageState.data||{}),error:pageState.error},overviewOptions());}
 else if(currentScope.view==='usage')renderMetrics(shell.host,pageState.data,metricsOptions());
 else if(currentScope.view==='activity')renderOverview(shell.host,{...(pageState.data||{}),error:pageState.error},overviewOptions());
 else if(currentScope.view!=='advanced'){renderLegacy({body:shell.host},{...currentScope,filters:{...currentScope.filters}},{loading:false,data:pageState.data,quota:currentScope.view==='quota'?pageState.data:null,error:pageState.error});shell.host.querySelectorAll('[data-stats-log-evidence],[data-stats-open-session]').forEach(b=>b.addEventListener('click',event=>{event.stopImmediatePropagation();openSaved(b.dataset.statsLogEvidence||b.dataset.statsOpenSession);},{capture:true}));}
 shell.host.scrollTop=scroll;if(focusText){const match=[...shell.host.querySelectorAll('button,select,input')].find(n=>(n.getAttribute('aria-label')||n.textContent)===focusText);match?.focus({preventScroll:true});}
}
async function loadCatalogs(scope,signal){const results=await Promise.allSettled(Object.entries(usageFilters).map(async([alias,field])=>[alias,await statsRead('filters/'+field,scope,signal)]));return results.map((r,i)=>r.status==='fulfilled'?r.value:[Object.keys(usageFilters)[i],{error:r.reason?.message}]);}
async function runRefresh(scope,api,{signal,reason='manual'}={}){
 if(!shell||!actualVisible())return false;refreshController?.abort();const controller=new AbortController();refreshController=controller;signal?.addEventListener('abort',()=>controller.abort(),{once:true});const generation=++refreshGeneration;const snapshot=pinScope({...scope,filters:{...scope.filters}});const key=JSON.stringify([ownerPreferenceScope,snapshot,usageMode,usageMetric,usageGroup,activityResolution]);const cached=lastGoodByScope.get(key);if(cached){pageState=cached;renderPage();}else if(pageState?.scopeKey!==key){pageState=null;if(!nativeController)shell.host.replaceChildren(uEl('p','Loading selected scope…','usage-empty'));}shell.footer.textContent='Refreshing…';
 try{
  if(nativeController)await nativeController.update(snapshot,{collection:collections,force:forceNativeRefresh||['manual','navigation','control','open'].includes(reason)});
  forceNativeRefresh=false;let data;if(snapshot.view==='sessions'||snapshot.view==='activity')data=await statsRead('activity',snapshot,controller.signal,{resolution:activityResolution});
  else if(snapshot.view==='usage')data=await usageData(snapshot,controller.signal,{mode:usageMode,metric:usageMetric,group:usageGroup});
  else if(snapshot.view==='quality')data=await statsRead('analysis/quality',snapshot,controller.signal);
  else if(snapshot.view==='trends'){if(!trendsOptedIn){data=null;}else data=await statsRead('analysis/trends/query',snapshot,controller.signal,{}, {content_opt_in:true,resolution:activityResolution});}
  else if(snapshot.view==='quota'){const query=new URLSearchParams({period:snapshot.range==='custom'?'30d':snapshot.range,timezone:snapshot.timezone});if(snapshot.filters.account)query.set('account_id',snapshot.filters.account);const current=window.sessionModule?.getCurrentSessionId?.();if(current){const r=await fetch('/api/stats/v1/session-handle',{method:'POST',headers:{'Content-Type':'application/json'},credentials:'same-origin',signal:controller.signal,body:JSON.stringify({session_id:current})});if(r.ok){const h=await r.json();if(h.handle)query.set('session_id',h.handle);}}const response=await fetch('/api/stats/v1/quota?'+query,{credentials:'same-origin',signal:controller.signal});if(!response.ok)throw new Error('Account quota unavailable ('+response.status+')');data=await response.json();}
  else data=null;
  const catalogs=await loadCatalogs(snapshot,controller.signal);if(generation!==refreshGeneration||controller.signal.aborted||!shell)return false;
  const owner=data?.summary?.owner_scope||data?.owner_scope;if(typeof owner==='string'&&owner!==ownerPreferenceScope){ownerPreferenceScope=owner;lastGoodByScope.clear();}
  for(const[alias,result]of catalogs){const select=shell.catalogs[alias];const value=currentScope.filters?.[alias]||'';select.replaceChildren(Object.assign(uEl('option',alias[0].toUpperCase()+alias.slice(1)+': All'),{value:''}));for(const choice of result.choices||[])select.append(Object.assign(uEl('option',choice.label),{value:choice.handle}));if(value&&![...select.options].some(o=>o.value===value))select.append(Object.assign(uEl('option','Selected '+alias+' · unavailable'),{value}));select.value=value;select.title=result.error||'';}
  pageState={data,error:null,scopeKey:JSON.stringify([ownerPreferenceScope,snapshot,usageMode,usageMetric,usageGroup,activityResolution])};lastGoodByScope.set(JSON.stringify([ownerPreferenceScope,snapshot,usageMode,usageMetric,usageGroup,activityResolution]),pageState);selectPage();renderPage();shell.footer.textContent='Updated just now'+(data?.coverage?.state&&data.coverage.state!=='complete'&&data.coverage.state!=='reported'?' · '+data.coverage.state:'');return true;
 }catch(error){if(error.name==='AbortError'||generation!==refreshGeneration)return false;pageState=cached||(pageState?.scopeKey===key?pageState:null)||{data:null,scopeKey:key};pageState={...pageState,error:error.message};renderPage();shell.footer.replaceChildren(document.createTextNode((cached?'Showing last available data · ':'')+error.message+' '),uBtn('Retry',()=>requestRefresh('manual')));return false;}
}
export function openStatsUsage({view=null,range=null,timezone=null,mode=null}={}){
 if(mode==='tokens'||mode==='cost'){usageMode=mode;usageMetric=mode==='cost'?'cost':'tokens';}currentScope=readUsageScope(ownerPreferenceScope);if(view||range||timezone)currentScope=pinScope({...currentScope,view:aliasView(view||currentScope.view),...(range?{range,start:null,end:null}:{}),...(timezone?{timezone,start:null,end:null}:{})});
 if(!instance){instance=createOpenClankWindow({id:'stats-usage-window',label:'Usage',subtitle:'Open Clank',minWidth:340,minHeight:360,className:'stats-usage-window',onClosed:()=>{refreshController?.abort();disposeNativeLogs();statsLifecycle?.stop();statsLifecycle=null;windowObserver?.disconnect();windowObserver=null;const closed=instance;instance=null;shell=null;dashboardRoot=null;nativeController=null;closed?.destroy?.();}});}
 mountShell(instance);instance.show();document.title='Usage · Open Clank';writeUsageScope(currentScope,ownerPreferenceScope,{replace:true});if(!localNavigation.length){localNavigation.push(currentScope);navigationPosition=0;}selectPage();
 if(!statsLifecycle){statsLifecycle=createStatsRefreshLifecycle({cadenceSeconds:60,isVisible:actualVisible,refresh:({signal,reason})=>runRefresh(currentScope,instance,{signal,reason})});statsLifecycle.start({immediate:false});windowObserver=new MutationObserver(()=>statsLifecycle?.request('window-visibility'));windowObserver.observe(instance.root,{attributes:true,attributeFilter:['class']});}
 requestRefresh('open');return instance;
}
window.addEventListener('popstate',()=>{if(!instance)return;currentScope=readUsageScope(ownerPreferenceScope);selectPage();requestRefresh('navigation');});
window.addEventListener('openclank:auth-context-changed',()=>{refreshController?.abort();refreshGeneration++;disposeNativeLogs();nativeController=null;lastGoodByScope.clear();ownerPreferenceScope='anonymous';if(!instance)return;currentScope=pinScope({...currentScope,filters:{}});writeUsageScope(currentScope,ownerPreferenceScope,{replace:true});localNavigation.splice(0);localNavigation.push(currentScope);navigationPosition=0;shell.host.dataset.view='';pageState=null;selectPage();requestRefresh('owner-change');});
window.addEventListener('openclank:provider-updated',()=>statsLifecycle?.request('provider-update'));
