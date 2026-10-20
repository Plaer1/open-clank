import {
  activeSheetView,
  cellEditorKind,
  copySelection,
  formatCellValue,
  isReadOnlyColumn,
  readCell,
  rowKey,
  sheetTitle,
  visibleColumnIndexes,
} from './sheetModel.js';

const TOOLBAR_ACTIONS = Object.freeze([
  ['filter', 'Filter'], ['sort', 'Sort'], ['group', 'Group'], ['columns', 'Columns'],
]);

export function ensureSheetStyles(documentObject = globalThis.document) {
  if (!documentObject?.head || documentObject.getElementById('copal-sheet-styles')) return false;
  const link = documentObject.createElement('link');
  link.id = 'copal-sheet-styles'; link.rel = 'stylesheet'; link.href = '/static/js/copal/sheet.css';
  documentObject.head.append(link); return true;
}

function element(tag, attributes = {}, ...children) {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(attributes)) {
    if (name === 'text') node.textContent = String(value ?? '');
    else if (name === 'class') node.className = value;
    else if (name.startsWith('on') && typeof value === 'function') node.addEventListener(name.slice(2), value);
    else if (value === true) node.setAttribute(name, '');
    else if (value !== false && value != null) node.setAttribute(name, String(value));
  }
  node.append(...children.flat().filter((child) => child != null).map((child) => child.nodeType ? child : document.createTextNode(String(child))));
  return node;
}

export function parseTypedEditorValue(kind, raw) {
  const text = String(raw ?? ''); const value = text.trim();
  if (!value) return { ok:true, value:null };
  if (kind === 'number') {
    if (!/^[+-]?(?:\d+\.?\d*|\.\d+)(?:e[+-]?\d+)?$/iu.test(value)) return { ok:false, message:'Enter a valid number.' };
    const number = Number(value);
    return Number.isFinite(number) ? { ok:true, value:number } : { ok:false, message:'Enter a valid number.' };
  }
  if (kind === 'date') {
    if (!/^\d{4}-\d{2}-\d{2}$/u.test(value)) return { ok:false, message:'Enter a date as YYYY-MM-DD.' };
    const date = new Date(`${value}T00:00:00Z`);
    if (Number.isNaN(date.valueOf()) || date.toISOString().slice(0, 10) !== value) return { ok:false, message:'Enter a valid calendar date.' };
    return { ok:true, value };
  }
  if (kind === 'datetime') {
    const normalized = value.replace(' ', 'T');
    if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?$/u.test(normalized) || Number.isNaN(new Date(normalized).valueOf())) return { ok:false, message:'Enter a valid date and time.' };
    return { ok:true, value:normalized };
  }
  return { ok:true, value:text };
}

function selectedCell(state, rowIndex, column, focusOnly = false) {
  const selected = state.selected;
  if (!selected) return false;
  const view = activeSheetView(state);
  const row = state.rows[rowIndex];
  const anchorRow = state.rows.findIndex((item) => rowKey(item) === selected.anchor.rowKey);
  const focusRow = state.rows.findIndex((item) => rowKey(item) === selected.focus.rowKey);
  const anchorColumn = view.columns.findIndex((item) => item.property === selected.anchor.columnKey);
  const focusColumn = view.columns.findIndex((item) => item.property === selected.focus.columnKey);
  const currentColumn = view.columns.indexOf(column);
  if (focusOnly) return rowKey(row) === selected.focus.rowKey && column.property === selected.focus.columnKey;
  return rowIndex >= Math.min(anchorRow, focusRow) && rowIndex <= Math.max(anchorRow, focusRow)
    && currentColumn >= Math.min(anchorColumn, focusColumn) && currentColumn <= Math.max(anchorColumn, focusColumn);
}

function renderValue(value, wrap) {
  const output = formatCellValue(value) || '—';
  if (Array.isArray(value)) return element('span', { class:'copal-sheet-chips', title:output }, ...value.map((item) => element('span', { class:'copal-sheet-chip', text:item })));
  return element('span', { class:`copal-sheet-value${wrap ? ' wrap' : ''}`, text:output });
}

function footerText(counts = {}) {
  const shown = Number(counts.shown || 0); const matched = counts.matched == null ? null : Number(counts.matched);
  const limited = counts.limited == null ? null : Number(counts.limited);
  const parts = [`${shown} shown`];
  if (matched != null) parts.push(`${matched} matched`);
  if (limited != null) parts.push(`${limited} in view`);
  if (counts.truncated) parts.push('source limited');
  return parts.join(' · ');
}

function showPastePreview(target, preview, state, apply, execute = (operation) => operation()) {
  const documentObject = target.ownerDocument || document;
  documentObject.querySelector('.copal-sheet-paste-preview')?.remove();
  const rejected = preview.rejected || [];
  const summary = `${preview.cells?.length || 0} cells ready${rejected.length ? ` · ${rejected.length} skipped` : ''}`;
  const detail = rejected.length
    ? element('ul', { class:'copal-sheet-paste-rejections' }, ...rejected.slice(0, 8).map((item) => element('li', { text:`${item.columnKey || 'cell'}: ${item.reason}` })))
    : element('ul', { class:'copal-sheet-paste-rejections' });
  const status = element('p', { class:'copal-sheet-paste-status', role:'status' });
  const cancel = element('button', { type:'button', class:'copal-btn', text:'Cancel' });
  const applyButton = element('button', { type:'button', class:'copal-btn primary', text:'Apply', disabled:!(preview.cells?.length) });
  const conflictButton = element('button', { type:'button', class:'copal-btn', text:'Review conflict', hidden:true });
  const dialog = element('dialog', { class:'copal-dialog copal-sheet-paste-preview', 'aria-labelledby':'copal-sheet-paste-preview-title' },
    element('h2', { id:'copal-sheet-paste-preview-title', text:'Paste into sheet' }),
    element('p', { class:'copal-sheet-paste-summary', text:summary }),
    status,
    preview.truncated ? element('p', { class:'copal-sheet-message error', role:'alert', text:'Paste was truncated at the supported size or cell limit.' }) : null,
    detail,
    element('div', { class:'copal-dialog-actions' },
      cancel, conflictButton, applyButton,
    ),
  );
  let running = false; let result = null; let reviewedConflict = false;
  cancel.addEventListener('click', () => { if (!running) dialog.close('cancel'); });
  const showFailures = (failures = []) => {
    const conflicts = failures.filter((item) => item.outcome === 'conflict' || item.retryKind === 'conflict');
    const valueText = (value) => value === undefined ? '(missing)' : JSON.stringify(value);
    detail.replaceChildren(...failures.slice(0, 8).map((item) => {
      if (item.outcome !== 'conflict') return element('li', { text:`${item.sourceId || item.rowKey || 'row'}: ${item.message || item.outcome || 'write failed'}` });
      const intended = (item.cells || []).map((cell) => `${cell.columnKey}: intended ${valueText(cell.value)}, latest ${valueText(item.remote?.properties?.[cell.columnKey])}`).join('; ');
      return element('li', { text:`${item.sourceId || item.rowKey || 'row'}: conflict at saved head ${item.remote?.head || 'unknown'}; ${intended || 'your cell changes are preserved'}` });
    }));
    status.textContent = conflicts.length
      ? `${conflicts.length} row${conflicts.length === 1 ? '' : 's'} conflict with a newer saved version. Review it before retrying with latest.`
      : `${failures.length} row${failures.length === 1 ? '' : 's'} failed. Retry only the failed rows or cancel.`;
    conflictButton.hidden = conflicts.length === 0;
    conflictButton.disabled = false;
    conflictButton.textContent = reviewedConflict ? 'Retry with latest' : 'Review conflict';
  };
  const run = async (mode = 'apply') => {
    if (running) return;
    running = true; cancel.disabled = true; applyButton.disabled = true; applyButton.textContent = 'Applying…'; status.textContent = 'Applying changes…';
    try {
      const operation = mode === 'replay' ? result?.retry : mode === 'rebase' ? result?.reviewConflict : apply;
      const invoke = typeof operation === 'function' ? operation : () => ({ outcome:'failed', message:'This retry action is no longer available.' });
      result = await execute(invoke);
    }
    catch (error) { result = { outcome:'failed', message:error.message }; }
    running = false;
    if (result?.outcome === 'partial' && result.failures?.length) {
      if (mode === 'rebase') reviewedConflict = false;
      showFailures(result.failures); cancel.disabled = false; applyButton.disabled = typeof result.retry !== 'function'; applyButton.textContent = result.retry ? 'Retry failed' : 'Close'; return;
    }
    if (!result || !['applied', 'unchanged', 'queued'].includes(result.outcome)) {
      if (mode === 'rebase') reviewedConflict = false;
      showFailures([{ message:result?.message || 'The paste could not be saved.', outcome:result?.outcome || 'failed', remote:result?.remote, reviewConflict:result?.reviewConflict }]); cancel.disabled = false; applyButton.disabled = typeof result?.retry !== 'function'; applyButton.textContent = result?.retry ? 'Retry failed' : 'Close'; return;
    }
    status.textContent = 'Paste applied.'; dialog.close('apply');
  };
  applyButton.addEventListener('click', () => {
    if (result?.retry) void run('replay');
    else if (result) dialog.close('failed');
    else void run('apply');
  });
  conflictButton.addEventListener('click', () => {
    if (!reviewedConflict) {
      reviewedConflict = true;
      conflictButton.textContent = 'Retry with latest';
      status.textContent = 'The latest saved values are listed above. Retry with latest will reapply only these failed cell changes at that saved head.';
      return;
    }
    void run('rebase');
  });
  dialog.addEventListener('cancel', (event) => { if (running) event.preventDefault(); });
  dialog.addEventListener('close', () => dialog.remove(), { once:true });
  documentObject.body.append(dialog);
  if (typeof dialog.showModal === 'function') dialog.showModal(); else dialog.setAttribute('open', '');
  return dialog;
}

export function renderSheet(target, state, { controller = null, onToolbar = null, onOverflow = null, onOpenSource = null, onColumnMenu = null, onColumnResize = null, onColumnReorder = null, onViewCommand = null, onContextCommand = null, onClear = null, onClearOperation = null, onCopy = null, onValidationError = null, schema = {} } = {}) {
  if (!target) throw new TypeError('A sheet target is required');
  const view = activeSheetView(state);
  const root = element('section', { class:`copal-sheet-surface density-${state.density || 'compact'}`, 'data-sheet-status':state.status, 'aria-busy':state.status === 'loading' });
  root.__openClankSheetContextCommand = async (command, node) => {
    if (command === 'edit-sheet-cell') { node.dispatchEvent(new MouseEvent('dblclick', { bubbles:true })); return true; }
    if (command === 'clear-sheet-cell') {
      const cells = controller?.clear?.() || []; const current = controller?.getState?.();
      const result = onClearOperation ? await onClearOperation(cells, current) : await onClear?.(cells, current);
      return result !== false;
    }
    if (command === 'copy-sheet-cell') {
      const text = copySelection(controller?.getState?.() || state); onCopy?.(text);
      if (typeof navigator !== 'undefined' && navigator.clipboard?.writeText) {
        try { await navigator.clipboard.writeText(text); }
        catch (error) { onValidationError?.({ outcome:'failed', code:'clipboard-denied', message:'Clipboard access was denied.', error }); return false; }
      }
      return true;
    }
    return onContextCommand?.(command, node, state, controller) || false;
  };
  const exactTitle = state.definition?.extensions?.title || state.definition?.name || 'Sheet'; const title = sheetTitle(exactTitle);
  const errorCode = String(state.error?.code || state.error?.detail?.code || '').toLowerCase();
  const stateLabel = state.status === 'error' ? (state.error?.status === 403 || errorCode.includes('permission') ? 'Permission denied' : errorCode.includes('invalid') ? 'Invalid sheet' : state.error?.message || 'Query failed') : state.status === 'indexing' ? 'Indexing…' : state.status === 'loading' ? 'Loading…' : 'Ready';
  const heading = element('header', { class:'copal-sheet-header', title:String(exactTitle) }, element('div', { class:'copal-sheet-title' }, element('h2', { text:title })), element('span', { class:'copal-sheet-state', role:'status', text:stateLabel }));
  const tabs = element('div', { class:'copal-sheet-tabs', role:'tablist', 'aria-label':'Saved sheet views' }, ...(state.definition?.views || []).map((item, index) => {
    const tab = element('button', { type:'button', role:'tab', class:`copal-sheet-tab${item.id === state.viewId ? ' active' : ''}`, 'aria-selected':item.id === state.viewId ? 'true' : 'false', tabindex:item.id === state.viewId ? '0' : '-1', 'data-sheet-view-id':item.id, text:item.name, title:`${item.name} view · double-click to rename`, onclick:() => controller?.setView(item.id), ondblclick:(event) => { event.preventDefault(); onViewCommand?.('rename-view', item, state); }, oncontextmenu:(event) => { event.preventDefault(); onViewCommand?.('view-menu', item, state); } });
    tab.addEventListener('keydown', (event) => { if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return; event.preventDefault(); const targetIndex = event.key === 'Home' ? 0 : event.key === 'End' ? state.definition.views.length - 1 : Math.max(0, Math.min(state.definition.views.length - 1, index + (event.key === 'ArrowRight' ? 1 : -1))); controller?.setView(state.definition.views[targetIndex].id); });
    return tab;
  }));
  const toolbar = element('nav', { class:'copal-sheet-toolbar', 'aria-label':'Sheet actions' }, ...TOOLBAR_ACTIONS.map(([action, label]) => element('button', { type:'button', class:'copal-btn copal-sheet-tool', 'data-sheet-action':action, text:label, onclick:() => onToolbar?.(action, state) })), element('button', { type:'button', class:'copal-btn copal-sheet-tool', text:'More', 'aria-haspopup':'menu', onclick:() => onOverflow?.(state) }));
  const search = element('input', { type:'search', class:'copal-sheet-search', placeholder:'Search loaded rows…', 'aria-label':'Search loaded sheet rows', value:state.query?.text || '' });
  search.addEventListener('input', () => {
    document.querySelectorAll('.copal-sheet-toolbar-menu:not([open])').forEach((dialog) => dialog.remove());
    controller?.setQuery({ text:search.value }, { debounce:150 });
    search.focus({ preventScroll:true });
  });
  toolbar.append(search);
  const viewAdd = element('button', { type:'button', class:'copal-btn copal-sheet-add-view', 'aria-label':'Add saved sheet view', text:'＋ View', onclick:() => onViewCommand?.('add-view', null, state) });
  root.append(heading, element('div', { class:'copal-sheet-view-controls' }, tabs, viewAdd), toolbar);
  if (state.error) root.append(element('div', { class:'copal-sheet-message error', role:'alert', text:state.error.message || 'The sheet could not be loaded.' }));
  if (state.status === 'loading' && !state.rows.length) root.append(element('div', { class:'copal-sheet-message', role:'status', text:'Loading sheet rows…' }));
  if (state.status === 'indexing') root.append(element('div', { class:'copal-sheet-message', role:'status', text:'Rows are being indexed. Results will appear when indexing finishes.' }));
  const visibleColumns = (view?.columns || []).filter((column) => column.visible !== false);
  const table = element('table', { class:'copal-sheet-grid', role:'grid', 'aria-label':title, 'aria-rowcount':state.counts?.total ?? state.counts?.matched ?? state.rows.length, 'aria-colcount':visibleColumns.length });
  if (view?.type === 'card' || view?.type === 'list') {
    const surface = element('div', { class:`copal-sheet-${view.type}s`, role:'list', 'aria-label':`${title} ${view.type} view` });
    for (const [rowIndex, row] of state.rows.entries()) {
      const item = element(view.type === 'card' ? 'article' : 'div', { class:`copal-sheet-${view.type}`, role:'listitem', 'data-sheet-row-key':rowKey(row) });
      for (const [visibleIndex, column] of visibleColumns.entries()) {
        const columnIndex = view.columns.indexOf(column); const value = readCell(row, column); const readonly = isReadOnlyColumn(column, schema); const selected = selectedCell(state, rowIndex, column); const focus = selectedCell(state, rowIndex, column, true) || (!state.selected && rowIndex === 0 && visibleIndex === 0);
        item.append(element('div', { class:'copal-sheet-field' }, element('span', { class:'copal-sheet-field-label', text:column.label }), element('button', { type:'button', class:`copal-sheet-cell${selected ? ' selected' : ''}${readonly ? ' readonly' : ''}`, role:'gridcell', tabindex:focus ? '0' : '-1', 'aria-selected':selected ? 'true' : 'false', 'aria-readonly':readonly ? 'true' : 'false', 'data-copal-context-object':'base-cell', 'data-sheet-row-index':rowIndex, 'data-sheet-column-index':columnIndex, 'data-sheet-row-key':rowKey(row), 'data-sheet-column-key':column.property, title:readonly ? `Open source for ${row.name || rowKey(row)}` : `Edit ${column.label}` }, renderValue(value, state.wrap))));
      }
      surface.append(item);
    }
    root.append(surface, !state.rows.length && state.status === 'ready' ? element('div', { class:'copal-sheet-message', role:'status', text:'No matching rows.' }) : null, element('footer', { class:'copal-sheet-footer', role:'status', text:footerText(state.counts) }));
    return root;
  }
  const headRow = element('tr', { role:'row' }, ...visibleColumns.map((column, index) => {
    const sort = (view?.sorts || []).find((item) => item.property === column.property);
    const header = element('th', { role:'columnheader', 'aria-colindex':index + 1, 'aria-sort':sort ? sort.direction === 'asc' ? 'ascending' : 'descending' : 'none', scope:'col', style:column.width ? `width:${column.width}px` : '', draggable:'true', 'data-sheet-column-key':column.property });
    const label = sort ? `${column.label} · ${sort.direction === 'asc' ? 'ascending' : 'descending'}${(view.sorts || []).length > 1 ? ` · priority ${(view.sorts || []).findIndex((item) => item.property === column.property) + 1}` : ''}` : column.label;
    const menu = element('button', { type:'button', class:'copal-sheet-column-menu', 'aria-label':`${column.label} column menu`, 'aria-haspopup':'menu', text:label, onclick:() => onColumnMenu?.(column, state) });
    const resize = element('span', { class:'copal-sheet-column-resize', role:'separator', tabindex:'0', 'aria-label':`Resize ${column.label} column`, 'data-sheet-column-resize':column.property });
    const startResize = (event) => {
      event.preventDefault(); event.stopPropagation();
      const startWidth = header.getBoundingClientRect().width || Number(column.width) || 120; const startX = event.clientX; let previewWidth = startWidth;
      const move = (nextEvent) => { previewWidth = Math.max(80, Math.min(600, startWidth + nextEvent.clientX - startX)); header.style.width = `${previewWidth}px`; };
      const finish = (commit) => { resize.removeEventListener('pointermove', move); resize.removeEventListener('pointerup', up); resize.removeEventListener('pointercancel', cancel); if (!commit) header.style.width = column.width ? `${column.width}px` : ''; else onColumnResize?.(column, Math.round(previewWidth), state); };
      const up = () => finish(true); const cancel = () => finish(false);
      resize.addEventListener('pointermove', move); resize.addEventListener('pointerup', up, { once:true }); resize.addEventListener('pointercancel', cancel, { once:true }); resize.setPointerCapture?.(event.pointerId);
    };
    resize.addEventListener('pointerdown', startResize);
    resize.addEventListener('keydown', (event) => { if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return; event.preventDefault(); const width = event.key === 'Home' ? 80 : event.key === 'End' ? 600 : Math.max(80, Math.min(600, (Number(column.width) || 120) + (event.key === 'ArrowRight' ? 16 : -16))); onColumnResize?.(column, width, state); });
    header.addEventListener('dragstart', (event) => { event.dataTransfer?.setData('application/x-copal-sheet-column', JSON.stringify({ resourceKey:state.resourceKey, scope:state.scope, viewId:state.viewId, property:column.property })); event.dataTransfer.effectAllowed = 'move'; header.classList.add('dragging'); });
    header.addEventListener('dragend', () => header.classList.remove('dragging'));
    header.addEventListener('dragover', (event) => { if (Array.from(event.dataTransfer?.types || []).includes('application/x-copal-sheet-column')) { event.preventDefault(); header.classList.add('drop-target'); } });
    header.addEventListener('dragleave', () => header.classList.remove('drop-target'));
    header.addEventListener('drop', (event) => { event.preventDefault(); header.classList.remove('drop-target'); let payload = null; try { payload = JSON.parse(event.dataTransfer?.getData('application/x-copal-sheet-column') || 'null'); } catch (_) {} if (!payload || payload.viewId !== state.viewId || JSON.stringify(payload.resourceKey) !== JSON.stringify(state.resourceKey) || JSON.stringify(payload.scope) !== JSON.stringify(state.scope) || payload.property === column.property) return; onColumnReorder?.(payload.property, column.property, state, payload.scope); });
    header.append(menu, resize); return header;
  }));
  table.append(element('thead', {}, headRow));
  const body = element('tbody');
  for (const [rowIndex, row] of state.rows.entries()) {
    const tr = element('tr', { role:'row', 'aria-rowindex':rowIndex + 2, 'data-sheet-row-key':rowKey(row) });
    for (const [visibleIndex, column] of visibleColumns.entries()) {
      const columnIndex = view.columns.indexOf(column); const value = readCell(row, column); const readonly = isReadOnlyColumn(column, schema); const selected = selectedCell(state, rowIndex, column); const focus = selectedCell(state, rowIndex, column, true) || (!state.selected && rowIndex === 0 && visibleIndex === 0);
      const cell = element('td', {
        role:'gridcell', tabindex:focus ? '0' : '-1', 'aria-colindex':visibleIndex + 1, 'aria-selected':selected ? 'true' : 'false',
        'aria-readonly':readonly ? 'true' : 'false', class:`copal-sheet-cell${selected ? ' selected' : ''}${readonly ? ' readonly' : ''}`,
        'data-copal-context-object':'base-cell',
        'data-sheet-row-index':rowIndex, 'data-sheet-column-index':columnIndex, 'data-sheet-row-key':rowKey(row), 'data-sheet-column-key':column.property,
        title:readonly ? `Open source for ${row.name || rowKey(row)}` : `Edit ${column.label}`,
      }, renderValue(value, state.wrap));
      tr.append(cell);
    }
    body.append(tr);
  }
  table.append(body); root.append(element('div', { class:'copal-sheet-grid-wrap' }, table));
  if (state.status === 'ready' && !state.rows.length && !state.error) root.append(element('div', { class:'copal-sheet-message', role:'status', text:'No matching rows.' }));
  root.append(element('footer', { class:'copal-sheet-footer', role:'status', text:footerText(state.counts) }));
  return root;
}

function focusSelected(target, state = null) {
  const focus = state?.selected?.focus;
  const cell = focus
    ? [...target.querySelectorAll('.copal-sheet-cell')].find((item) => item.dataset.sheetRowKey === focus.rowKey && item.dataset.sheetColumnKey === focus.columnKey)
    : null;
  (cell || target.querySelector('.copal-sheet-cell.selected') || target.querySelector('.copal-sheet-cell[tabindex="0"]'))?.focus();
}

function patchSelection(root, previous, next) {
  const before = previous?.selected; const after = next?.selected;
  const single = selection => selection && selection.anchor?.rowKey === selection.focus?.rowKey && selection.anchor?.columnKey === selection.focus?.columnKey;
  if (!single(before) || !single(after)) return false;
  const points = [before.anchor, after.anchor];
  for (const point of points) {
    const cell = [...root.querySelectorAll('.copal-sheet-cell')].find((item) => item.dataset.sheetRowKey === point.rowKey && item.dataset.sheetColumnKey === point.columnKey);
    if (!cell) continue;
    const rowIndex = Number(cell.dataset.sheetRowIndex); const columnIndex = Number(cell.dataset.sheetColumnIndex);
    const selected = selectedCell(next, rowIndex, activeSheetView(next).columns[columnIndex]);
    const focus = selectedCell(next, rowIndex, activeSheetView(next).columns[columnIndex], true);
    cell.classList.toggle('selected', selected); cell.setAttribute('aria-selected', selected ? 'true' : 'false'); cell.tabIndex = focus ? 0 : -1;
  }
  return true;
}

export function mountSheet(target, controller, options = {}) {
  if (!target || !controller) throw new TypeError('A target and sheet controller are required');
  ensureSheetStyles(target.ownerDocument);
  let root = null; let editing = null; let editSessionToken = 0; let searchTimer = null; let renderedState = null; let searchCaret = null;
  let gridScrollTop = 0; let gridScrollLeft = 0;
  let gridOperation = null;
  const runGridOperation = (operation) => {
    if (gridOperation) return gridOperation;
    root?.setAttribute('aria-busy', 'true');
    gridOperation = Promise.resolve().then(operation).catch((error) => { options.onValidationError?.({ message:error?.message || 'The sheet operation failed.', outcome:'failed' }); return { outcome:'failed', error }; }).finally(() => { gridOperation = null; if (root) root.setAttribute('aria-busy', controller.getState().status === 'loading' ? 'true' : 'false'); });
    return gridOperation;
  };
  const refreshAfterMutation = async (result) => {
    const applied = result === true || ['applied', 'unchanged', 'partial', 'queued'].includes(result?.outcome);
    if (!applied) return result;
    const refreshed = await controller.refresh('cell-edit');
    if (refreshed?.outcome === 'failed') {
      const failure = { outcome:'failed', message:'The sheet changed, but its rows could not be refreshed. Retry the operation.' };
      options.onValidationError?.(failure);
      return failure;
    }
    return result;
  };
  const performClear = async (cells, current) => refreshAfterMutation(await options.onClear?.(cells, current));
  const executePaste = (operation) => runGridOperation(async () => refreshAfterMutation(await operation()));
  const performPaste = async (preview, current) => executePaste(() => options.onPaste?.(preview, current));
  const render = (state, { force = false, focus = false } = {}) => {
    // Once an editor owns a cell, a subscription refresh must never replace
    // its input. The edit completion/cancel path renders the newest state.
    if (editing && !force) return;
    if (!force && root && renderedState && state.rows === renderedState.rows && state.definition === renderedState.definition && state.query === renderedState.query && state.status === renderedState.status && state.error === renderedState.error && state.viewId === renderedState.viewId && patchSelection(root, renderedState, state)) { renderedState = state; if (focus) focusSelected(root, state); return; }
    const previousGrid = root?.querySelector('.copal-sheet-grid-wrap');
    if (previousGrid) { gridScrollTop = previousGrid.scrollTop; gridScrollLeft = previousGrid.scrollLeft; }
    const active = typeof document !== 'undefined' ? document.activeElement : null;
    const ownedFocus = active && target.contains(active) ? active : null;
    const focusedTab = ownedFocus?.classList?.contains('copal-sheet-tab') ? { id:ownedFocus.dataset.sheetViewId || null, text:ownedFocus.textContent } : null;
    const focusedCell = ownedFocus?.classList?.contains('copal-sheet-cell') ? { rowKey:ownedFocus.dataset.sheetRowKey, columnKey:ownedFocus.dataset.sheetColumnKey } : null;
    const focusedAction = ownedFocus && !focusedTab && !focusedCell && !ownedFocus.classList?.contains('copal-sheet-search') && (ownedFocus.classList?.contains('copal-sheet-tool') || ownedFocus.dataset?.sheetAction || ownedFocus.getAttribute?.('aria-label'))
      ? { sheetAction:ownedFocus.dataset?.sheetAction || null, ariaLabel:ownedFocus.getAttribute?.('aria-label') || null, text:ownedFocus.textContent || '' } : null;
    const searching = ownedFocus?.classList?.contains('copal-sheet-search'); if (searching) searchCaret = [active.selectionStart, active.selectionEnd]; const preserveSearch = searching; const searchStart = preserveSearch ? searchCaret?.[0] : null; const searchEnd = preserveSearch ? searchCaret?.[1] : null;
    root = renderSheet(target, state, { ...options, controller, onClearOperation:(cells, current) => runGridOperation(() => performClear(cells, current)), onPaste:(preview, current) => performPaste(preview, current) }); target.replaceChildren(root);
    renderedState = state;
    const nextGrid = root.querySelector('.copal-sheet-grid-wrap'); if (nextGrid) { nextGrid.scrollTop = gridScrollTop; nextGrid.scrollLeft = gridScrollLeft; }
    if (focusedTab != null) { const tabs = [...root.querySelectorAll('.copal-sheet-tab')]; (tabs.find((tab) => tab.getAttribute('aria-selected') === 'true') || tabs.find((tab) => (focusedTab.id && tab.dataset.sheetViewId === focusedTab.id) || (!focusedTab.id && tab.textContent === focusedTab.text)))?.focus({ preventScroll:true }); }
    else if (preserveSearch) { const nextSearch = root.querySelector('.copal-sheet-search'); nextSearch?.focus({ preventScroll:true }); if (searchStart != null) nextSearch?.setSelectionRange(searchStart, searchEnd); }
    else if (!editing && focus) focusSelected(root, state);
    else if (focusedCell) { [...root.querySelectorAll('.copal-sheet-cell')].find((cell) => cell.dataset.sheetRowKey === focusedCell.rowKey && cell.dataset.sheetColumnKey === focusedCell.columnKey)?.focus({ preventScroll:true }); }
    else if (focusedAction) {
      const match = focusedAction.sheetAction ? root.querySelector(`[data-sheet-action="${CSS.escape(focusedAction.sheetAction)}"]`) : focusedAction.ariaLabel ? root.querySelector(`[aria-label="${CSS.escape(focusedAction.ariaLabel)}"]`) : [...root.querySelectorAll('.copal-sheet-tool')].find((button) => button.textContent === focusedAction.text);
      match?.focus({ preventScroll:true });
    }
  };
  const stopEdit = (expectedSession = null) => {
    if (expectedSession && editing !== expectedSession) return false;
    // A controller emission can queue a zero-delay render just before F2
    // gives the input ownership. Cancel that callback when the edit ends so
    // it cannot replace the restored cell after Escape and disconnect the
    // cell that receives an immediate Shift+Enter reopen.
    clearTimeout(searchTimer); searchTimer = null; editSessionToken += 1;
    editing?.input?.remove?.(); editing = null; return true;
  };
  const resolveCell = (cell, state = controller.getState()) => {
    if (!cell || !target.contains(cell) || cell.isConnected === false) return null;
    const view = activeSheetView(state); const rowIndex = Number(cell.dataset.sheetRowIndex); const columnIndex = Number(cell.dataset.sheetColumnIndex);
    const row = state.rows[rowIndex]; const column = view?.columns?.[columnIndex];
    if (!row || !column || column.visible === false || rowKey(row) !== cell.dataset.sheetRowKey || column.property !== cell.dataset.sheetColumnKey) return null;
    return { state, view, rowIndex, columnIndex, row, column };
  };
  const beginEdit = (cell) => {
    const position = resolveCell(cell); if (!position) return false;
    const { state, view, rowIndex, columnIndex, row, column } = position;
    if (isReadOnlyColumn(column, options.schema)) { options.onOpenSource?.(row, column); return false; }
    // Editing is addressed by the cell identity that opened the session.
    // Focus can move without the controller receiving its capture-phase focus
    // event (for example during a direct keyboard or pointer gesture), so
    // make the session's row/column selection authoritative before replacing
    // the cell with its editor.
    controller.select(rowIndex, columnIndex);
    stopEdit(); editSessionToken += 1;
    const kind = cellEditorKind(row, column, options.schema); const value = readCell(row, column);
      const input = kind === 'checkbox' ? element('input', { type:'checkbox', class:'copal-sheet-editor', 'aria-label':column.label }) : element('input', { type:'text', inputmode:kind === 'number' ? 'decimal' : kind === 'date' || kind === 'datetime' ? 'numeric' : null, placeholder:kind === 'number' ? 'Number' : kind === 'date' ? 'YYYY-MM-DD' : kind === 'datetime' ? 'YYYY-MM-DDTHH:MM' : null, class:'copal-sheet-editor', 'aria-label':column.label, value:Array.isArray(value) ? value.join(', ') : value ?? '' });
    if (kind === 'checkbox') input.checked = value === true;
    let cancelled = false; let committed = false; let suppressBlur = false; let session = null;
    const moveAfter = (rowDelta, columnDelta) => {
      const nextState = controller.getState(); const nextView = activeSheetView(nextState); const indexes = visibleColumnIndexes(nextView);
      const currentRow = nextState.rows.findIndex((item) => rowKey(item) === rowKey(row)); const currentColumn = indexes.indexOf(nextView.columns.findIndex((item) => item.property === column.property));
      const nextRow = currentRow + rowDelta; const nextColumn = currentColumn + columnDelta;
      if (currentRow < 0 || currentColumn < 0 || nextRow < 0 || nextRow >= nextState.rows.length || nextColumn < 0 || nextColumn >= indexes.length) {
        render(nextState);
        setTimeout(() => root?.querySelector(rowDelta < 0 || columnDelta < 0 ? '.copal-sheet-tool' : '.copal-sheet-search, .copal-sheet-tool')?.focus({ preventScroll:true }), 0);
        return;
      }
      controller.select(nextRow, indexes[nextColumn]); render(controller.getState(), { focus:true });
    };
    const finish = async (commit, rowDelta = 0, columnDelta = 0) => {
      if (editing !== session || committed || cancelled) return;
      if (!commit) { cancelled = true; if (stopEdit(session)) { controller.select(rowIndex, columnIndex); renderedState = null; render(controller.getState(), { force:true, focus:true }); [...target.querySelectorAll('.copal-sheet-cell')].find((item) => item.dataset.sheetRowKey === rowKey(row) && item.dataset.sheetColumnKey === column.property)?.focus({ preventScroll:true }); } return; }
      let next = kind === 'checkbox' ? input.checked : input.value;
      if (kind === 'number' || kind === 'date' || kind === 'datetime') {
        const parsed = parseTypedEditorValue(kind, input.value);
        if (!parsed.ok) { input.setAttribute('aria-invalid', 'true'); options.onValidationError?.({ row, column, code:'invalid-value', message:parsed.message }); return; }
        input.removeAttribute('aria-invalid'); next = parsed.value;
      } else if (kind === 'list') next = input.value.split(',').map((item) => item.trim()).filter(Boolean);
      committed = true;
      const result = await controller.editCell(row, column, next);
      if (editing !== session) return result;
      if (!result || !['applied', 'unchanged', 'queued'].includes(result.outcome)) { committed = false; suppressBlur = false; input.setAttribute('aria-invalid', 'true'); options.onValidationError?.({ row, column, outcome:result?.outcome || 'failed', message:result?.message || (result?.outcome === 'conflict' ? 'The source changed. Compare the saved version and retry.' : 'Value could not be saved.') }); return; }
      stopEdit(session); render(controller.getState(), { focus:true });
      if (rowDelta || columnDelta) moveAfter(rowDelta, columnDelta);
    };
    input.addEventListener('change', () => { if (kind === 'checkbox') void finish(true); });
    input.addEventListener('keydown', (event) => {
      event.stopPropagation();
      if (event.key === 'Escape') { event.preventDefault(); suppressBlur = true; void finish(false); }
      else if (event.key === 'Enter') { event.preventDefault(); suppressBlur = true; void finish(true, event.shiftKey ? -1 : 1, 0); }
      else if (event.key === 'Tab') { event.preventDefault(); suppressBlur = true; void finish(true, 0, event.shiftKey ? -1 : 1); }
    });
    input.addEventListener('blur', () => { if (!cancelled && !suppressBlur) void finish(true); });
    cell.replaceChildren(input); session = { input, cell, row, column }; editing = session; input.focus(); if (input.select && kind !== 'checkbox') input.select(); return true;
  };
  let suppressClickUntil = 0;
  const onClick = (event) => { const cell = event.target.closest?.('[data-sheet-row-index][data-sheet-column-index]'); const position = resolveCell(cell); if (!position) return; if (target.__sheetDragActive || target.__sheetDragCompleted) { target.__sheetDragActive = false; target.__sheetDragCompleted = false; return; } if (suppressClick || performance.now() < suppressClickUntil) { suppressClick = false; return; } controller.select(position.rowIndex, position.columnIndex, { extend:event.shiftKey }); focusSelected(target, controller.getState()); };
  const onDoubleClick = (event) => { const cell = event.target.closest?.('[data-sheet-row-index][data-sheet-column-index]'); if (cell) beginEdit(cell); };
  const onFocus = (event) => {
    if (editing || target.__sheetDragActive || target.__sheetDragCompleted) return;
    const cell = event.target.closest?.('[data-sheet-row-index][data-sheet-column-index]');
    if (!cell || !target.contains(cell)) return;
    searchCaret = null;
    const position = resolveCell(cell); if (!position) return;
    const { state, rowIndex, columnIndex, row, column } = position; const selected = state.selected?.focus;
    if (selected?.rowKey !== rowKey(row) || selected?.columnKey !== column.property) controller.select(rowIndex, columnIndex);
  };
  const onKeyDown = (event) => {
    if (editing) return;
    const cell = event.target.closest?.('[data-sheet-row-index][data-sheet-column-index]'); if (!cell) return;
    const position = resolveCell(cell); if (!position) return;
    const { state, view, rowIndex, columnIndex } = position;
    const visibleIndexes = visibleColumnIndexes(view);
    if (event.key === 'Tab') {
      event.preventDefault(); const position = Math.max(0, visibleIndexes.indexOf(columnIndex)); const nextPosition = position + (event.shiftKey ? -1 : 1); const nextRow = rowIndex + (event.shiftKey && nextPosition < 0 ? -1 : !event.shiftKey && nextPosition >= visibleIndexes.length ? 1 : 0); const targetPosition = nextPosition < 0 ? visibleIndexes.length - 1 : nextPosition >= visibleIndexes.length ? 0 : nextPosition;
      const exiting = nextRow < 0 || nextRow >= state.rows.length;
      if (exiting) {
        const selector = event.shiftKey ? '.copal-sheet-tool' : '.copal-sheet-search, .copal-sheet-tool';
        const focusOutside = () => root?.querySelector(selector)?.focus({ preventScroll:true });
        focusOutside();
        setTimeout(focusOutside, 0);
        return;
      }
      controller.select(nextRow, visibleIndexes[targetPosition]);
      render(controller.getState(), { focus:true });
      return;
    }
    if (event.key === 'Home' || event.key === 'End') { event.preventDefault(); controller.select(rowIndex, event.key === 'Home' ? visibleIndexes[0] : visibleIndexes.at(-1), { extend:event.shiftKey }); render(controller.getState(), { focus:true }); return; }
    if (event.key === 'PageDown' || event.key === 'PageUp') {
      event.preventDefault();
      const grid = root?.querySelector('.copal-sheet-grid-wrap');
      const rowHeight = 36;
      const visibleRows = Math.max(1, Math.floor((grid?.clientHeight || rowHeight * 10) / rowHeight));
      controller.move(event.key === 'PageDown' ? visibleRows : -visibleRows, 0, { extend:event.shiftKey });
      render(controller.getState(), { focus:true });
      return;
    }
    const step = { ArrowUp:[-1, 0], ArrowDown:[1, 0], ArrowLeft:[0, -1], ArrowRight:[0, 1] }[event.key];
    if (step) { event.preventDefault(); controller.move(step[0], step[1], { extend:event.shiftKey }); render(controller.getState(), { focus:true }); return; }
    if (event.key === 'Enter' || event.key === 'F2') { event.preventDefault(); beginEdit(cell); return; }
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'a') { event.preventDefault(); const state = controller.getState(); const indexes = visibleColumnIndexes(activeSheetView(state)); if (state.rows.length && indexes.length) { controller.select(0, indexes[0]); controller.select(state.rows.length - 1, indexes.at(-1), { extend:true }); render(controller.getState(), { focus:true }); } return; }
    if (event.key === 'Delete' || event.key === 'Backspace') { event.preventDefault(); runGridOperation(() => performClear(controller.clear(), controller.getState())); return; }
    if (event.key === 'Escape' && state.selected && (state.selected.anchor.rowKey !== state.selected.focus.rowKey || state.selected.anchor.columnKey !== state.selected.focus.columnKey)) { event.preventDefault(); event.stopPropagation(); const restoreKey = { rowKey:cell.dataset.sheetRowKey, columnKey:cell.dataset.sheetColumnKey }; controller.select(rowIndex, columnIndex); render(controller.getState(), { focus:true }); [...target.querySelectorAll('.copal-sheet-cell')].find((item) => item.dataset.sheetRowKey === restoreKey.rowKey && item.dataset.sheetColumnKey === restoreKey.columnKey)?.focus({ preventScroll:true }); return; }
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'c') {
      event.preventDefault(); const text = copySelection(controller.getState()); options.onCopy?.(text);
      if (typeof navigator !== 'undefined' && navigator.clipboard?.writeText) void navigator.clipboard.writeText(text).catch((error) => options.onValidationError?.({ outcome:'failed', code:'clipboard-denied', message:'Clipboard access was denied.', error }));
    }
  };
  const onPaste = (event) => {
    const cell = event.target.closest?.('[data-sheet-row-index][data-sheet-column-index]');
    const position = resolveCell(cell); if (!position) return;
    event.preventDefault();
    controller.select(position.rowIndex, position.columnIndex);
    render(controller.getState(), { focus:true });
    const preview = controller.previewPaste(event.clipboardData?.getData('text/plain') || '', options);
    showPastePreview(target, preview, controller.getState(), () => options.onPaste?.(preview, controller.getState()), executePaste);
  };
  const onGridScroll = (event) => {
    const grid = event.target?.closest?.('.copal-sheet-grid-wrap');
    if (!grid || !target.contains(grid)) return;
    gridScrollTop = grid.scrollTop; gridScrollLeft = grid.scrollLeft;
  };
  let dragging = false; let dragMoved = false; let suppressClick = false; let dragFrame = 0; let dragPoint = null;
  let disposed = false;
  const applyDragPoint = () => {
    dragFrame = 0; if (!dragging || !dragPoint) return;
    const hovered = document.elementFromPoint(dragPoint.clientX, dragPoint.clientY)?.closest?.('[data-sheet-row-index][data-sheet-column-index]');
    if (!hovered || !target.contains(hovered)) return;
    const state = controller.getState(); const position = resolveCell(hovered, state); if (!position) return;
    const { rowIndex:row, columnIndex:column } = position; const focus = state.selected?.focus;
    if (focus?.rowKey === rowKey(state.rows[row]) && focus?.columnKey === state.definition.views.find((view) => view.id === state.viewId)?.columns[column]?.property) return;
    dragMoved = true; controller.select(row, column, { extend:true }); if (!dragging) render(controller.getState(), { focus:true });
  };
  const onPointerDown = (event) => { if (event.button !== 0) return; const cell = event.target.closest?.('[data-sheet-row-index][data-sheet-column-index]'); const position = resolveCell(cell); if (!position) return; target.__sheetDragActive = false; dragging = true; dragMoved = false; try { cell.setPointerCapture?.(event.pointerId); } catch (_) { /* synthetic or already released pointers still select */ } controller.select(position.rowIndex, position.columnIndex); };
  const onPointerMove = (event) => { if (!dragging) return; target.__sheetDragActive = true; dragPoint = event; if (!dragFrame) dragFrame = requestAnimationFrame(applyDragPoint); };
  const onPointerUp = () => { if (dragFrame) { cancelAnimationFrame(dragFrame); dragFrame = 0; } applyDragPoint(); if (dragMoved) { target.__sheetDragCompleted = true; suppressClick = true; suppressClickUntil = performance.now() + 250; } dragging = false; dragPoint = null; if (dragMoved) render(controller.getState(), { focus:true }); };
  target.addEventListener('click', onClick); target.addEventListener('dblclick', onDoubleClick); target.addEventListener('focus', onFocus, true); target.addEventListener('keydown', onKeyDown); target.addEventListener('paste', onPaste); target.addEventListener('scroll', onGridScroll, true); target.addEventListener('pointerdown', onPointerDown); target.addEventListener('pointermove', onPointerMove); target.addEventListener('pointerup', onPointerUp); target.addEventListener('pointercancel', onPointerUp);
  const unsubscribe = controller.subscribe(() => {
    clearTimeout(searchTimer);
    if (dragging || editing) return;
    const token = editSessionToken;
    searchTimer = setTimeout(() => {
      searchTimer = null;
      if (editing || token !== editSessionToken) return;
      render(controller.getState());
    }, 0);
  }); render(controller.getState(), { focus:true });
  return () => {
    if (disposed) return;
    disposed = true;
    clearTimeout(searchTimer); if (dragFrame) cancelAnimationFrame(dragFrame); stopEdit(); unsubscribe();
    target.removeEventListener('click', onClick); target.removeEventListener('dblclick', onDoubleClick); target.removeEventListener('focus', onFocus, true); target.removeEventListener('keydown', onKeyDown); target.removeEventListener('paste', onPaste); target.removeEventListener('scroll', onGridScroll, true); target.removeEventListener('pointerdown', onPointerDown); target.removeEventListener('pointermove', onPointerMove); target.removeEventListener('pointerup', onPointerUp); target.removeEventListener('pointercancel', onPointerUp);
    controller.close(); target.replaceChildren();
  };
}
