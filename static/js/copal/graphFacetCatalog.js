import { deriveFacets, mergeFacets } from './graphModel.js';

const categories = new Set(['all', 'kinds', 'folders', 'tags', 'properties']);
const values = (page) => Array.isArray(page) ? page : page?.values || [];
const more = (response) => {
  const facets = response?.facets || {};
  return [facets.kinds, facets.folders, facets.tags, ...Object.values(facets.properties || {})].some((page) => page?.hasMore === true);
};

/** Only unsearched all-category responses may become canonical catalogs. */
export function facetResponseSnapshot(response) {
  const facets = response?.facets || {};
  const canonical = response?.catalog?.kind === 'canonical';
  return {
    version:response.version || 1, generation:String(response.generation ?? ''),
    officialRoot:response.officialRoot ?? null, totalDocuments:response.totalDocuments ?? null,
    kinds:values(facets.kinds), folders:values(facets.folders), tags:values(facets.tags),
    properties:Object.fromEntries(Object.entries(facets.properties || {}).map(([key, page]) => [key, values(page)])),
    propertyKeys:response.propertyKeys || Object.keys(facets.properties || {}),
    catalog:response.catalog || { kind:'query' }, countUnit:'documents',
    incomplete:!canonical || more(response),
  };
}

export function facetRequestKey(scope, generation, parameters) {
  return JSON.stringify([scope.account, scope.workspace, String(generation ?? ''), parameters.category,
    parameters.property_key ?? null, parameters.query, parameters.offset, parameters.limit]);
}

/** Scoped discovery, independent from filter changes and graph rendering.
 * request(parameters, scope) performs the real API request. It must reject
 * failed HTTP responses (409 optionally exposed as error.status).
 * query() is one picker session: a newer query/close invalidates its late work.
 * canonical() owns a separate request fence; queried pages never prune state.
 */
export function createFacetCatalog({ request, getScope, getDocuments = () => [], maxCanonicalPages = 25, pageLimit = 200 }) {
  let identity = null;
  let generation = '';
  let pickerEpoch = 0;
  let catalogEpoch = 0;
  let canonical = null;
  let disposed = false;
  const pages = new Map();
  function scopeNow() {
    if (disposed) return null;
    const scope = getScope();
    if (!scope?.account || !scope?.workspace) {
      identity = null; generation = ''; canonical = null; pages.clear(); pickerEpoch += 1; catalogEpoch += 1;
      return null;
    }
    const key = JSON.stringify([scope.account, scope.workspace]);
    if (key !== identity) {
      identity = key; generation = ''; canonical = null; pages.clear(); pickerEpoch += 1; catalogEpoch += 1;
    }
    return { account:String(scope.account), workspace:String(scope.workspace) };
  }
  function current(scope, epoch, kind) {
    const active = scopeNow();
    return active?.account === scope.account && active.workspace === scope.workspace
      && epoch === (kind === 'picker' ? pickerEpoch : catalogEpoch);
  }
  function acceptGeneration(next) {
    if (generation !== next) { canonical = null; pages.clear(); pickerEpoch += 1; catalogEpoch += 1; }
    generation = next;
  }
  function local() {
    return { ...deriveFacets(getDocuments()), incomplete:true, status:'local-only', countScope:'loaded documents' };
  }
  function validate(parameters) {
    const p = { category:'all', query:'', offset:0, limit:pageLimit, ...parameters };
    if (!categories.has(p.category) || !Number.isSafeInteger(p.offset) || p.offset < 0
      || !Number.isSafeInteger(p.limit) || p.limit < 1 || p.limit > 1000
      || typeof p.query !== 'string' || p.query.length > 256
      || p.property_key !== undefined && (p.category !== 'properties' || typeof p.property_key !== 'string' || p.property_key.length > 1024)) {
      throw new TypeError('Invalid facet request');
    }
    return { ...p, facet_version:2 };
  }
  function localPage(p) {
    const snapshot = local();
    const paginate = (items) => {
      const needle = p.query.trim().toLowerCase();
      const matched = items.filter((item) => !needle || String(item.label ?? item.value).toLowerCase().includes(needle));
      return { values:matched.slice(p.offset, p.offset + p.limit), total:matched.length, offset:p.offset,
        limit:p.limit, hasMore:p.offset + p.limit < matched.length };
    };
    const facets = {};
    for (const category of ['kinds', 'folders', 'tags']) if (p.category === 'all' || p.category === category) facets[category] = paginate(snapshot[category]);
    if (p.category === 'all' || p.category === 'properties') facets.properties = Object.fromEntries(
      (p.property_key !== undefined ? [p.property_key] : snapshot.propertyKeys).map((key) => [key, paginate(snapshot.properties[key] || [])]));
    return { ...snapshot, facets, catalog:{ kind:'query', category:p.category, propertyKey:p.property_key ?? null, query:p.query } };
  }
  return {
    close() { pickerEpoch += 1; },
    dispose() { disposed = true; pickerEpoch += 1; catalogEpoch += 1; identity = null; canonical = null; pages.clear(); },
    propertyNames(query = '') {
      if (!scopeNow()) return [];
      const keys = canonical?.propertyKeys || local().propertyKeys;
      return keys.filter((key) => key.toLowerCase().includes(String(query).toLowerCase()));
    },
    snapshot() { return scopeNow() ? canonical : null; },
    async query(parameters = {}) {
      const p = validate(parameters);
      const scope = scopeNow();
      if (!scope) return null;
      const epoch = ++pickerEpoch;
      const expected = p.expected_generation || generation;
      const key = facetRequestKey(scope, expected, p);
      if (pages.has(key)) return pages.get(key);
      try {
        const response = await request({ ...p, ...(expected ? { expected_generation:expected } : {}) }, scope);
        if (!current(scope, epoch, 'picker')) return null;
        const next = String(response?.generation ?? '');
        if (!next || expected && next !== expected) return { status:'generation-changed', incomplete:true };
        acceptGeneration(next);
        const page = { ...response, catalog:{ kind:'query', category:p.category, propertyKey:p.property_key ?? null, query:p.query }, incomplete:true };
        pages.set(facetRequestKey(scope, next, p), page);
        return page;
      } catch (error) {
        if (!current(scope, epoch, 'picker')) return null;
        if (error?.status === 409) { canonical = null; pages.clear(); generation = ''; return { status:'generation-changed', incomplete:true }; }
        return localPage(p);
      }
    },
    async canonical({ force = false, isCurrent = () => true } = {}) {
      const scope = scopeNow();
      if (!scope) return null;
      const epoch = ++catalogEpoch;
      const active = () => isCurrent() && current(scope, epoch, 'catalog');
      try {
        for (let attempt = 0; attempt < 3; attempt += 1) {
          if (!active()) return null;
          const first = await request(validate({}), scope);
          if (!active()) return null;
          const next = String(first?.generation ?? '');
          if (!next || first.version !== 2 || first.catalog?.kind !== 'canonical') throw new Error('Canonical typed catalog unavailable');
          if (!force && canonical?.generation === next) return canonical;
          let snapshot = facetResponseSnapshot(first);
          let response = first;
          let drift = false;
          let offset = pageLimit;
          for (let page = 1; more(response) && page < maxCanonicalPages; page += 1) {
            try { response = await request(validate({ offset, expected_generation:next }), scope); }
            catch (error) { if (error?.status === 409) { drift = true; break; } throw error; }
            if (!active()) return null;
            if (String(response?.generation) !== next || response?.totalDocuments !== first.totalDocuments) { drift = true; break; }
            snapshot = mergeFacets(snapshot, facetResponseSnapshot(response));
            offset += pageLimit;
          }
          if (drift) continue;
          snapshot.incomplete = more(response);
          snapshot.catalog = { kind:'canonical' };
          snapshot.status = snapshot.incomplete ? 'page limit reached' : 'complete';
          snapshot.continuation = snapshot.incomplete ? { offset, limit:pageLimit, generation:next } : null;
          if (!active()) return null;
          acceptGeneration(next); canonical = snapshot;
          return snapshot;
        }
        return { ...local(), status:'generation changed', incomplete:true };
      } catch (error) { return active() ? local() : null; }
    },
  };
}
