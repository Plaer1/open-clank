// Reusable local feedback data and UI; reportProgress remains the storage and
// envelope authority. A checked choice is feedback, not implementation approval.
import { portableDeliverableStyle } from '../theme/deliverableAppearance.js';
import { createProgressStore, makeBackupFile, makeProgressEnvelope, parseBackupFile,
  validateProgressEnvelope, validateReportRegistration } from '../browser/reportProgress.js';

const own = (object, key) => Object.prototype.hasOwnProperty.call(object, key);
const escapeHtml = value => String(value).replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const fields = ['selected', 'notes', 'disliked', 'dislikeNotes'];
const blank = () => ({ selected:false, notes:'', disliked:false, dislikeNotes:'' });
function values(value, partial = false) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new TypeError('Feedback item is invalid.');
  const result = partial ? {} : blank();
  for (const key of fields) if (own(value, key)) {
    if ((key === 'selected' || key === 'disliked') ? typeof value[key] !== 'boolean' : typeof value[key] !== 'string' || value[key].length > 10000) throw new TypeError('Feedback field is invalid.');
    result[key] = value[key];
  }
  return result;
}

/** Construct registration from an already captured source digest and manifest. */
export function feedbackRegistration({reportId, sourceDigest, schemaVersion = 1, items}) {
  const session = { session_id:'feedback-local', generation:1, digest:sourceDigest };
  return validateReportRegistration({channel:'openclank.report.v1', type:'register', session_id:session.session_id, generation:1,
    report_id:reportId, source_digest:sourceDigest, schema_version:schemaVersion, items}, session);
}

function legacyEntries(data, registration, aliases) {
  if (!data || typeof data !== 'object' || Array.isArray(data)) throw new TypeError('Legacy feedback is invalid.');
  if (![registration.reportId, ...aliases].includes(data.reportId)) throw new TypeError('Legacy backup belongs to another report.');
  let records;
  if (data.items && typeof data.items === 'object' && !Array.isArray(data.items)) records = Object.entries(data.items);
  else if (Array.isArray(data.features) && /^OpenClank[A-Za-z]*FeatureSelections\/v[12]$/.test(data.schema || '')) {
    records = data.features.map(item => [item?.id, data.schema.endsWith('/v1') && !own(item || {},'selected') ? {...item, selected:true} : item]);
  } else throw new TypeError('Unsupported legacy backup.');
  if (records.length > 1000) throw new TypeError('Legacy backup has too many items.');
  const seen = new Set(), accepted = [], conflicts = [];
  for (const [id, raw] of records) {
    if (typeof id !== 'string' || seen.has(id)) throw new TypeError('Legacy item ID is invalid or duplicated.');
    seen.add(id);
    const value = values(raw, true), digest = registration.manifest.get(id);
    if (!digest) conflicts.push({id, reason:'unknown_item'});
    else accepted.push({id, digest, value});
  }
  return {accepted, conflicts, legacy:true, warning:'Legacy choices have no verified source or item digest; inspect them against the current content.'};
}

/** Explicit import; normal backups retain digest conflict checks. No code in a
 * legacy HTML copy executes or initiates resource loads. */
export async function inspectFeedbackBackup(file, registration, {legacyAliases = []} = {}) {
  const name = String(file?.name || '');
  if (/\.html?$/i.test(name)) {
    if (!file || file.size > 512 * 1024) throw new TypeError('Legacy HTML backup is unavailable or too large.');
    const text = await file.text();
    if (new TextEncoder().encode(text).length > 512 * 1024) throw new TypeError('Legacy HTML backup is too large.');
    const seeds = [...text.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script\s*>/gi)].filter(match => /\bid\s*=\s*(["'])(?:seed-state|openclank-progress)\1/i.test(match[1]) && /\btype\s*=\s*(["'])application\/json\1/i.test(match[1]));
    if (seeds.length !== 1) throw new TypeError('HTML must have one progress seed.');
    let data; try { data = JSON.parse(seeds[0][2]); } catch { throw new TypeError('Progress seed is malformed.'); }
    if (/\bid\s*=\s*(["'])openclank-progress\1/i.test(seeds[0][1])) {
      const checked = validateProgressEnvelope(data,registration,{allowSourceDigestChange:true});
      return {...checked, legacy:false, warning: checked.sourceDigestChanged ? 'Source changed; only matching item digests can be restored.' : ''};
    }
    if (!data.reportId) {
      const matches = [...text.matchAll(/\bconst\s+REPORT\s*=\s*['"]([A-Za-z0-9._:-]+)['"]/g)];
      if (matches.length !== 1) throw new TypeError('Legacy report identity is unavailable.');
      data.reportId = matches[0][1];
    }
    return legacyEntries(data,registration,legacyAliases);
  }
  const data = await parseBackupFile(file);
  if (data?.version === 1 && own(data,'source_digest')) {
    const checked = validateProgressEnvelope(data,registration,{allowSourceDigestChange:true});
    return {...checked, legacy:false, warning: checked.sourceDigestChanged ? 'Source changed; only matching item digests can be restored.' : ''};
  }
  return legacyEntries(data,registration,legacyAliases);
}

export function createDeliverableFeedback({registration, storage, storageKey, status = () => {}}) {
  if (!registration || typeof storageKey !== 'string' || !storageKey) throw new TypeError('Registration and storage key are required.');
  if (!storage) { try { storage = globalThis.localStorage; } catch { /* Report failure on save below. */ } }
  storage ||= { getItem() { return null; }, setItem() { throw new Error('Storage unavailable'); } };
  const store = createProgressStore(storage), state = new Map();
  const envelope = () => makeProgressEnvelope(registration,[...state].map(([id,value]) => ({id,digest:registration.manifest.get(id),value:values(value)})));
  const save = () => {
    const ok = store.save(storageKey,envelope());
    status(ok ? 'Saved locally. Choices are feedback, not implementation approval.' : 'Local storage failed. Export your progress before closing.', !ok);
    return ok;
  };
  const load = () => {
    const saved = store.load(storageKey);
    if (!saved) return false;
    const checked = validateProgressEnvelope(saved, registration);
    if (checked.conflicts.length) { status('Saved progress conflicts with this source; keep and review a backup.',true); return false; }
    for (const item of checked.accepted) state.set(item.id,values(item.value));
    status('Local progress restored.',false); return true;
  };
  const set = (id, patch) => {
    if (!registration.manifest.has(id)) throw new TypeError('Unknown feedback item.');
    state.set(id,{...(state.get(id) || blank()),...values(patch,true)}); return save();
  };
  const apply = checked => {
    if (!checked || !Array.isArray(checked.accepted)) throw new TypeError('Inspect the backup first.');
    for (const item of checked.accepted) {
      if (registration.manifest.get(item.id) !== item.digest) throw new TypeError('Imported item digest changed.');
      state.set(item.id,{...(state.get(item.id) || blank()),...values(item.value,true)});
    }
    const ok = save();
    if (checked.warning || checked.conflicts?.length) status(`${checked.warning || ''} ${checked.conflicts?.length || 0} conflicting item(s) skipped.${ok ? '' : ' Local storage failed; export before closing.'}`.trim(),!ok);
    return ok;
  };
  return Object.freeze({load,set,apply,save,envelope, get:id => ({...(state.get(id) || blank())}),
    backup:format => makeBackupFile(envelope(),format),
    html:(items,appearance) => feedbackProgressHtml(registration,envelope(),items,appearance)});
}

/** An inert, standalone review copy; all content is text-escaped. */
export function feedbackProgressHtml(registration,envelope,items,appearance = null) {
  const checked = validateProgressEnvelope(envelope,registration);
  if (checked.conflicts.length || !Array.isArray(items)) throw new TypeError('Progress copy does not match the report.');
  const byId = new Map(checked.accepted.map(row => [row.id,values(row.value)]));
  const portable = portableDeliverableStyle(appearance);
  const declarations = portable ? Object.entries(portable.cssVariables).map(([key,value]) => `${key}:${value}`).join(';') : '';
  const sections = items.filter(item => registration.manifest.has(item.id)).map(item => {
    const v = byId.get(item.id) || blank();
    return `<section><h2>${escapeHtml(item.title || item.id)}</h2><p>ID: ${escapeHtml(item.id)}</p><p>Consider: ${v.selected ? 'Yes':'No'} · Concern: ${v.disliked ? 'Yes':'No'}</p><h3>Adoption notes</h3><pre>${escapeHtml(v.notes)}</pre><h3>Concern notes</h3><pre>${escapeHtml(v.dislikeNotes)}</pre></section>`;
  }).join('');
  // Escape markup delimiters before placing JSON in the inert script data block.
  const seed = JSON.stringify(checked.normalized).replace(/</g, '\\u003c').replace(/>/g, '\\u003e').replace(/&/g, '\\u0026');
  return `<!doctype html><html lang="en"><meta charset="utf-8"><title>Feedback progress</title><style>:root{${declarations}}body{max-width:65rem;margin:auto;padding:2rem;background:var(--oc-bg,#191A1E);color:var(--oc-fg,#FFF4D6);font:16px/1.5 var(--oc-font-family,monospace)}section{background:var(--oc-panel,transparent);border:1px solid var(--oc-border,#555A62);border-radius:calc(4px * var(--oc-radius-scale,1));padding:1rem;margin:1rem 0}pre{white-space:pre-wrap;overflow-wrap:anywhere}</style><h1>Feedback progress</h1><p>Read-only copy. Choices are not implementation approval.</p><p>Report: ${escapeHtml(registration.reportId)} · Source: ${escapeHtml(registration.sourceDigest)}</p>${sections}<script id="openclank-progress" type="application/json">${seed}</script></html>`;
}
