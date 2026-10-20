import { bindMenuDismiss } from '../escMenuStack.js';
import { facetSelectionId, facetSelectionLabel, normalizePropertySelection } from './graphModel.js';

const PAGE = 80;
const METADATA = new Set(['docId', 'product', 'seedVersion', 'sourcePath', 'builtin', 'title']);
const node = (tag, attrs = {}, ...children) => {
  const element = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === 'text') element.textContent = value;
    else if (key === 'class') element.className = value;
    else if (key.startsWith('on')) element.addEventListener(key.slice(2), value);
    else if (value !== undefined && value !== false) element.setAttribute(key, String(value));
  }
  element.append(...children); return element;
};
const button = (text, onclick, attrs = {}) => node('button', { type:'button', class:'copal-btn', text, onclick, ...attrs });

/** Compact, nonmodal filter forms. Opening/discovering a picker never changes
 * predicates; only an explicit choice does. The caller owns scoped persistence.
 */
export function mountGraphFilters({ element, graphState, facets = {}, mode = 'documents', catalog,
  onChange = () => {}, isCurrent = () => true }) {
  let disposed = false, popup = null, closePopup = null, opener = null, picker = null, serial = 0, debounce;
  const live = () => !disposed && isCurrent();
  let observedRoot = null;
  function boundPopup() {
    const graphRoot = element.closest('.copal-graph-wrap');
    if (graphRoot !== observedRoot) {
      if (observedRoot) boundsObserver?.unobserve(observedRoot);
      observedRoot = graphRoot; if (graphRoot) boundsObserver?.observe(graphRoot);
    }
    if (!popup || !graphRoot || disposed) return;
    const frame = graphRoot.getBoundingClientRect(), toolbar = element.getBoundingClientRect();
    const scale = element.offsetWidth ? toolbar.width / element.offsetWidth : 1;
    // DOM rectangles are physical CSS pixels; inline lengths are local pixels
    // under inherited CSS zoom. The graph may itself be partly scrolled out of
    // its applet. Intersect every clipping owner, not just its nominal bottom.
    const viewport = window.visualViewport;
    let left = Math.max(frame.left, viewport?.offsetLeft || 0);
    let top = Math.max(frame.top, viewport?.offsetTop || 0);
    let right = Math.min(frame.right, (viewport?.offsetLeft || 0) + (viewport?.width || innerWidth));
    let bottom = Math.min(frame.bottom, (viewport?.offsetTop || 0) + (viewport?.height || innerHeight));
    for (let owner = graphRoot.parentElement; owner; owner = owner.parentElement) {
      const style = getComputedStyle(owner), clip = owner.getBoundingClientRect();
      if (/auto|scroll|hidden|clip/.test(style.overflowX)) { left = Math.max(left, clip.left); right = Math.min(right, clip.right); }
      if (/auto|scroll|hidden|clip/.test(style.overflowY)) { top = Math.max(top, clip.top); bottom = Math.min(bottom, clip.bottom); }
    }
    const gutter = 4, width = Math.max(0, right - left - gutter * 2);
    const height = Math.max(0, bottom - top - gutter * 2), unit = scale || 1;
    const popupWidth = Math.min(420 * unit, width), popupHeight = Math.min(420 * unit, height);
    if (popupWidth / unit < 120 || popupHeight / unit < 64) {
      // A sliver cannot hold the padding plus a reachable header. Dismiss
      // coherently rather than exposing an overflowing or invisible form.
      popup.style.visibility = 'hidden';
      const surface = popup;
      queueMicrotask(() => { if (popup === surface) closePopup?.(); });
      return;
    }
    // Prefer below the toolbar, but use the visible graph area when a wrapped
    // high-zoom toolbar leaves no usable room below it. The whole form scrolls.
    const wantedTop = bottom - gutter - toolbar.bottom >= Math.min(160 * unit, popupHeight)
      ? toolbar.bottom : toolbar.top - popupHeight;
    const y = Math.max(top + gutter, Math.min(wantedTop, bottom - gutter - popupHeight));
    const x = Math.max(left + gutter, Math.min(toolbar.left + 8 * unit, right - gutter - popupWidth));
    Object.assign(popup.style, { insetInlineStart:'auto', insetBlockStart:'auto',
      top:`${(y - toolbar.top) / unit}px`, left:`${(x - toolbar.left) / unit}px`,
      width:`${popupWidth / unit}px`, maxHeight:`${popupHeight / unit}px`,
      visibility:width > 0 && height > 0 ? '' : 'hidden' });
  }
  const boundsObserver = typeof ResizeObserver === 'function' ? new ResizeObserver(boundPopup) : null;
  boundsObserver?.observe(element);
  let popupEvents = null;
  element.classList.add('copal-graph-toolbar');
  const search = node('input', { type:'search', class:'copal-graph-search', placeholder:'Search nodes…',
    'aria-label':'Search graph nodes', maxlength:1024 });
  search.value = graphState.searchQuery;
  const primary = node('div', { class:'copal-graph-primary-filters', 'aria-label':'Graph filters' });
  const chips = node('div', { class:'copal-graph-active-filters', 'aria-label':'Active graph filters' });
  const mobile = button('Filters', event => openChooser(event.currentTarget), { class:'copal-btn copal-graph-mobile-filters', 'aria-expanded':'false' });
  const reset = button('Reset filters', () => {
    if (!live()) return;
    graphState.searchQuery = ''; search.value = '';
    graphState.activeKinds.clear(); for (const item of facets.kinds || []) graphState.activeKinds.add(item.value);
    graphState.activeFolders.clear(); graphState.activeTags.clear();
    for (const values of Object.values(graphState.activeProperties)) values.clear();
    graphState.includeOfficial = false; changed();
  }, { 'aria-label':'Reset graph filters' });
  const categories = mode === 'galaxy' ? [['kinds','Kinds']] : [['kinds','Kinds'],['folders','Folders'],['tags','Tags']];
  const triggers = new Map(categories.map(([category, label]) => {
    const control = button(label, event => openValues(category, null, event.currentTarget), { 'data-facet-trigger':category, 'aria-expanded':'false' });
    primary.append(control); return [category, control];
  }));
  if (mode !== 'galaxy') primary.append(button('Add filter', event => openChooser(event.currentTarget), { 'data-facet-trigger':'properties', 'aria-expanded':'false' }));
  element.append(search, primary, mobile, reset, chips);
  search.addEventListener('input', () => { if (live()) { graphState.searchQuery = search.value; changed(); } });

  function selected(category, key) {
    if (category === 'properties') {
      // User-authored metadata keys may literally be __proto__ or constructor.
      if (!Object.hasOwn(graphState.activeProperties, key)) Object.defineProperty(graphState.activeProperties, key,
        { value:new Set(), enumerable:true, writable:true, configurable:true });
      return graphState.activeProperties[key];
    }
    return graphState[{ kinds:'activeKinds', folders:'activeFolders', tags:'activeTags' }[category]];
  }
  const identity = (value, property) => property ? facetSelectionId(value) : String(value);
  const choice = (item, property) => property ? normalizePropertySelection(item) : item.value;
  const label = (value, property) => property ? facetSelectionLabel(value) : value === '' ? '(root)' : String(value);
  function setChoice(set, value, checked, property) {
    for (const current of set) if (identity(current, property) === identity(value, property)) set.delete(current);
    if (checked) set.add(value);
  }
  function changed(detail) { refreshSummary(); onChange(detail); }
  function refreshSummary() {
    for (const [category, control] of triggers) {
      const count = selected(category).size;
      control.textContent = `${categories.find(row => row[0] === category)[1]} · ${count || (category === 'kinds' ? 'none' : 'all')}`;
    }
    const entries = [];
    if (graphState.activeKinds.size !== (facets.kinds || []).length || (facets.kinds || []).some(item => !graphState.activeKinds.has(item.value)))
      entries.push(['Kinds', graphState.activeKinds.size ? [...graphState.activeKinds].join(', ') : 'none', () => {
        graphState.activeKinds.clear(); for (const item of facets.kinds || []) graphState.activeKinds.add(item.value);
      }]);
    for (const [category, name] of [['folders','Folder'], ['tags','Tag']]) for (const value of selected(category))
      entries.push([name, label(value, false), () => selected(category).delete(value)]);
    for (const [key, values] of Object.entries(graphState.activeProperties)) for (const value of values)
      entries.push([key, label(value, true), () => setChoice(values, value, false, true)]);
    if (graphState.includeOfficial) entries.push(['Official docs', 'included', () => { graphState.includeOfficial = false; }]);
    chips.replaceChildren(...entries.map(([key, value, remove]) => button(`${key}: ${value} ×`, () => {
      if (!live()) return; remove(); changed(); if (picker?.render) picker.render();
    }, { class:'copal-btn copal-graph-filter-chip', title:`Remove ${key}: ${value}`, 'aria-label':`Remove ${key}: ${value}` })));
    chips.hidden = !entries.length; mobile.textContent = `Filters · ${entries.length}`;
  }
  function openShell(title, anchor) {
    if (!live()) return null;
    closePopup?.();
    serial++; catalog?.close(); clearTimeout(debounce);
    opener = anchor;
    popup = node('section', { class:'copal-graph-filter-popup', 'aria-label':title, 'data-graph-filter-popup':true });
    const titleNode = node('strong', { text:title });
    const close = button('Close', () => closePopup?.(), { 'aria-label':`Close ${title}` });
    popup.append(node('header', {}, titleNode, close)); element.append(popup);
    popupEvents = new AbortController();
    const eventOptions = { signal:popupEvents.signal, passive:true };
    window.addEventListener('resize', boundPopup, eventOptions);
    document.addEventListener('scroll', boundPopup, { ...eventOptions, capture:true });
    window.visualViewport?.addEventListener('resize', boundPopup, eventOptions);
    window.visualViewport?.addEventListener('scroll', boundPopup, eventOptions);
    boundPopup();
    anchor?.setAttribute('aria-expanded', 'true');
    const previous = anchor, surface = popup;
    closePopup = bindMenuDismiss(surface, () => {
      serial++; clearTimeout(debounce); catalog?.close();
      popupEvents?.abort(); popupEvents = null;
      surface.remove(); previous?.setAttribute('aria-expanded', 'false');
      popup = null; picker = null; closePopup = null; opener = null;
      if (!disposed && previous?.isConnected) previous.focus({ preventScroll:true });
    }, event => {
      if (surface.contains(event.target) || previous?.contains(event.target)) return false;
      // One pointer action dismisses the transient, not a control behind it.
      event.preventDefault(); event.stopPropagation(); return true;
    });
    surface.addEventListener('keydown', event => {
      if (event.key === 'Escape' && !event.isComposing) { event.preventDefault(); event.stopPropagation(); closePopup?.(); }
    });
    return surface;
  }
  function openChooser(anchor) {
    if (popup && opener === anchor && picker?.kind === 'chooser') { closePopup(); return; }
    const surface = openShell('Choose a graph filter', anchor); if (!surface) return;
    const keySearch = node('input', { type:'search', placeholder:'Find a property…', 'aria-label':'Find graph filter property', maxlength:256 });
    const choices = node('div', { class:'copal-graph-filter-choices' });
    const localCategories = node('div', { class:'copal-graph-picker-actions' }, ...categories.map(([category, title]) =>
      button(title, () => openValues(category, null, anchor))));
    let limit = PAGE;
    const render = () => {
      const keys = new Set([...(catalog?.propertyNames(keySearch.value) || []), ...Object.keys(facets.properties || {}), ...Object.keys(graphState.activeProperties)]);
      const names = [...keys].filter(key => key.toLowerCase().includes(keySearch.value.toLowerCase())).sort((a,b) => a.localeCompare(b));
      choices.replaceChildren();
      for (const [group, predicate] of [['Properties', key => !METADATA.has(key)], ['Document metadata', key => METADATA.has(key)]]) {
        const subset = names.slice(0, limit).filter(predicate); if (!subset.length) continue;
        choices.append(node('h4', { text:group }));
        for (const key of subset) choices.append(button(key, () => openValues('properties', key, anchor), { 'data-property-key':key }));
      }
      if (!names.length) choices.append(node('p', { text:'No matching property in the disclosed catalog.' }));
      if (names.length > limit) choices.append(button(`Show more properties (${names.length - limit})`, () => { limit += PAGE; render(); }));
    };
    surface.append(localCategories);
    if (mode !== 'galaxy') {
      const official = node('input', { type:'checkbox', 'aria-label':'Include official documentation' }); official.checked = graphState.includeOfficial;
      official.addEventListener('change', () => { if (live()) { graphState.includeOfficial = official.checked; changed(); } });
      surface.append(keySearch, choices, node('label', { class:'copal-graph-filter-option' }, official, 'Include official documentation'),
        node('small', { text:'A selected folder can also opt its official documents in.' }));
      keySearch.addEventListener('input', () => { limit = PAGE; render(); });
    }
    picker = { kind:'chooser', render }; render();
    (mode === 'galaxy' ? localCategories.querySelector('button') : keySearch)?.focus();
  }
  function openValues(category, key, anchor) {
    if (popup && opener === anchor && picker?.category === category && picker?.key === key) { closePopup(); return; }
    const name = category === 'properties' ? key : categories.find(row => row[0] === category)?.[1];
    const surface = openShell(`Filter ${name}`, anchor); if (!surface) return;
    const property = category === 'properties', selection = selected(category, key);
    const find = node('input', { type:'search', placeholder:'Find values…', 'aria-label':`Find ${name} values`, maxlength:256 });
    const result = node('div', { class:'copal-graph-filter-choices', 'data-facet-group':key || name });
    const status = node('p', { role:'status', 'aria-live':'polite', class:'copal-graph-picker-status' });
    const footer = node('div', { class:'copal-graph-picker-actions' });
    let rows = [], offset = 0, hasMore = false, busy = false, onlySelected = false, localLimit = PAGE, requestTicket = 0;
    const available = () => property ? (Object.hasOwn(facets.properties || {}, key) ? facets.properties[key] : []) : facets[category] || [];
    const selectedOnly = button('Selected only', () => { onlySelected = !onlySelected; selectedOnly.setAttribute('aria-pressed', String(onlySelected)); render(); }, { 'aria-pressed':'false' });
    const all = button('Select shown', () => { if (!live()) return; for (const item of displayed()) setChoice(selection, choice(item, property), true, property); changed(); render(); });
    const clear = button(category === 'kinds' ? 'None' : 'Clear selection', () => { if (!live()) return; selection.clear(); changed(); render(); });
    const more = button('Load more values', () => { localLimit += PAGE; void load(true); });
    surface.append(find, node('div', { class:'copal-graph-picker-actions' }, selectedOnly, all, clear), result, status, footer);
    function displayed() {
      if (!onlySelected) return rows;
      return [...selection].filter(value => label(value, property).toLowerCase().includes(find.value.toLowerCase())).map(value => property ? value : { value });
    }
    function render() {
      if (!live() || popup !== surface) return;
      const focused = result.contains(document.activeElement) ? document.activeElement?.dataset.facetValue : null;
      const chosenIds = new Set([...selection].map(value => identity(value, property)));
      result.replaceChildren(...displayed().map(item => {
        const value = choice(item, property), id = identity(value, property);
        const input = node('input', { type:'checkbox', 'data-facet-value':id }); input.checked = chosenIds.has(id);
        input.addEventListener('change', () => {
          if (!live()) return; setChoice(selection, value, input.checked, property);
          changed({ facet:key || name, value:label(value, property) }); refreshSelectionButtons();
        });
        const count = item.count == null ? 'count unknown' : `${item.count} documents`;
        return node('label', { class:'copal-graph-filter-option' }, input, node('span', { text:label(value, property) }), node('small', { text:count }));
      }));
      if (!displayed().length) result.append(node('p', { text:onlySelected ? 'No selected values match.' : 'No values match.' }));
      if (focused !== null) [...result.querySelectorAll('input')].find(input => input.dataset.facetValue === focused)?.focus({ preventScroll:true });
      footer.replaceChildren(); if (hasMore && !onlySelected) footer.append(more);
      refreshSelectionButtons();
    }
    function refreshSelectionButtons() { selectedOnly.textContent = `Selected only · ${selection.size}`; more.disabled = busy; all.disabled = busy || !displayed().length; }
    async function load(append = false) {
      if (!live() || popup !== surface) return;
      const ticket = ++requestTicket, epoch = serial; busy = true; refreshSelectionButtons();
      if (!append) { offset = 0; localLimit = PAGE; rows = []; }
      status.textContent = 'Loading values…';
      const fallback = available().filter(item => label(choice(item, property), property).toLowerCase().includes(find.value.toLowerCase()));
      const localRows = fallback.slice(0, localLimit);
      // Populate immediately from known vocabulary; the search field never remounts.
      if (!append) { rows = localRows; render(); }
      try {
        const response = catalog ? await catalog.query({ category, ...(property ? { property_key:key } : {}), query:find.value, offset:append ? offset : 0, limit:PAGE }) : null;
        if (!live() || popup !== surface || epoch !== serial || ticket !== requestTicket) return;
        if (response?.status === 'generation-changed') {
          rows = localRows; hasMore = fallback.length > localLimit;
          status.textContent = 'Catalog changed. Reopen this filter to refresh; selections are retained.';
        } else {
          const page = property ? response?.facets?.properties?.[key] : response?.facets?.[category];
          if (page) {
            rows = [...new Map([...(append ? rows : []), ...(page.values || [])].map(item => [identity(choice(item, property), property), item])).values()];
            offset = (page.offset || 0) + (page.values || []).length; hasMore = page.hasMore === true;
            status.textContent = `${rows.length} loaded · ${page.total ?? '?'} matching values · ${response.countScope || (response.status === 'local-only' ? 'loaded documents only' : 'corpus document counts')}`;
          } else { rows = localRows; hasMore = fallback.length > localLimit; status.textContent = `${rows.length} known values · ${facets.incomplete ? 'partial catalog; counts may be unknown' : 'corpus document counts'}`; }
        }
      } catch {
        if (!live() || popup !== surface || epoch !== serial || ticket !== requestTicket) return;
        rows = localRows; hasMore = fallback.length > localLimit; status.textContent = 'Catalog unavailable; showing known values. Selections are retained.';
      } finally {
        if (live() && popup === surface && epoch === serial && ticket === requestTicket) { busy = false; render(); }
      }
    }
    find.addEventListener('input', () => { requestTicket++; catalog?.close(); clearTimeout(debounce); debounce = setTimeout(() => void load(), 150); });
    picker = { kind:'values', category, key, render, refresh:() => void load() };
    void load(); find.focus();
  }
  refreshSummary();
  return {
    updateFacets(next) { if (!live()) return; facets = next; refreshSummary(); picker?.refresh?.(); if (picker?.kind === 'chooser') picker.render(); },
    refresh:refreshSummary,
    dispose() { if (disposed) return; disposed = true; clearTimeout(debounce); boundsObserver?.disconnect(); closePopup?.(); catalog?.close(); },
  };
}
