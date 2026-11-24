import { parseWorkbook, calculateWorkbook, chartValues, editWorkbookCell, serializeWorkbook, putWorkbookRange, putWorkbookChart, workbookChartSvg } from './workbookModel.js';

// One registered view over an existing document transaction owner. No source,
// save, or revision state is retained here; the buffer remains authoritative.
export function mountWorkbookView(root, { getSource, transact, readOnly = false, sharedCellMode = false, captureAppearance = () => null, isCurrent = () => true, onOpenSource = null, bindContext = null } = {}) {
  let sheetId = null, page = 0, disposed = false, refreshTimer = null, appearanceKnown = false, selection = null, selectionGeneration = 0;
  const scheduleRefresh = () => {
    if (disposed) return;
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(() => { refreshTimer = null; refresh(); }, 120);
  };
  const el = (tag, text) => { const node = document.createElement(tag); if (text != null) node.textContent = String(text); return node; };
  function refresh() {
    if (disposed || !isCurrent()) return;
    const active = document.activeElement;
    const focus = active && root.contains(active) && active.dataset?.cellId
      ? {id:active.dataset.cellId, start:active.selectionStart, end:active.selectionEnd, direction:active.selectionDirection} : null;
    const activeEditing = active && root.contains(active) && (active.tagName === 'INPUT' || active.tagName === 'TEXTAREA');
    let book, values;
    try { book = parseWorkbook(getSource()); values = calculateWorkbook(book, {isCurrent:() => !disposed && isCurrent()}); }
    catch (error) {
      if (!activeEditing) {
        const message = el('p', `Workbook source unavailable: ${error.message}`);
        if (typeof onOpenSource === 'function') { const button = el('button', 'Open source'); button.type = 'button'; button.addEventListener('click', onOpenSource); root.replaceChildren(message, button); }
        else root.replaceChildren(message);
      }
      return;
    }
    appearanceKnown = book.appearance != null;
    let sheet = book.sheets.find(item => item.id === sheetId) || book.sheets[0]; sheetId = sheet.id;
    if (activeEditing) {
      // Never replace the focused input (especially during IME composition).
      // Its source was already committed on input; refresh derived values and
      // other visible cells without moving the caret or stealing focus.
      const cells = new Map(sheet.rows.flat().map(cell => [cell.id, cell]));
      for (const node of root.querySelectorAll('[data-workbook-input]')) {
        const cell = cells.get(node.dataset.workbookInput);
        if (node !== active && cell) node.value = cell.raw;
      }
      for (const node of root.querySelectorAll('[data-workbook-value]')) {
        const result = values[node.dataset.workbookValue];
        if (result) {
          node.textContent = String(result.value ?? '');
          if (node.tagName === 'OUTPUT') node.setAttribute('aria-label', `Calculated value: ${node.textContent}`);
        }
      }
      for (const node of root.querySelectorAll('[data-workbook-chart]')) {
        const chart = book.charts.find(item => item.id === node.dataset.workbookChart);
        if (chart) node.innerHTML = workbookChartSvg(book,chart.id,values);
      }
      return;
    }
    const container = el('section'); container.setAttribute('aria-label', 'Workbook');
    const label = el('label', 'Sheet '), select = el('select'); select.setAttribute('aria-label', 'Workbook sheet');
    for (const item of book.sheets) { const option = el('option', item.name); option.value = item.id; option.selected = item.id === sheetId; select.append(option); }
    select.addEventListener('change', () => { sheetId = select.value; selection = null; selectionGeneration++; page = 0; refresh(); }); label.append(select); container.append(label);
    const savedRanges=book.ranges.filter(item=>item.sheetId===sheet.id);
    if (savedRanges.length) {
      const rangeLabel=el('label','Feedback range '), rangeSelect=el('select');
      rangeSelect.setAttribute('aria-label','Workbook feedback range');
      const none=el('option','Select a saved range'); none.value=''; rangeSelect.append(none);
      for (const range of savedRanges) { const option=el('option',`${range.id}: ${range.startCellId} – ${range.endCellId}`); option.value=range.id; rangeSelect.append(option); }
      if (selection?.rangeId && savedRanges.some(item=>item.id===selection.rangeId)) rangeSelect.value=selection.rangeId;
      rangeSelect.addEventListener('change',()=>{
        const range=savedRanges.find(item=>item.id===rangeSelect.value);
        selection=range ? {sheetId:sheet.id,rangeId:range.id,startCellId:range.startCellId,endCellId:range.endCellId,generation:++selectionGeneration} : null;
      });
      rangeLabel.append(rangeSelect); container.append(rangeLabel);
    }
    if (sharedCellMode) container.append(el('p', 'Shared workbook: edit existing cell values or formulas. Source and structural changes cannot be saved here.'));
    container.append(el('p', "Formulas can reference another sheet by its display name, for example Inputs!A1 or 'Sales Data'!A1:B3. Names are case-sensitive; missing or duplicate names show #REF!. If a sheet is renamed or removed, update its formulas."));
    const pageCount = Math.ceil(sheet.rows.length / 50); page = Math.min(page, pageCount - 1);
    if (pageCount > 1) {
      const previous = el('button', 'Previous rows'), next = el('button', 'Next rows'); previous.type = next.type = 'button'; previous.disabled = page === 0; next.disabled = page + 1 >= pageCount;
      previous.addEventListener('click', () => { page--; refresh(); }); next.addEventListener('click', () => { page++; refresh(); });
      container.append(previous, el('span', ` Rows ${page * 50 + 1}–${Math.min((page + 1) * 50, sheet.rows.length)} `), next);
    }
    const table = el('table'); table.setAttribute('aria-label', `${sheet.name} cells`);
    for (const [offset, row] of sheet.rows.slice(page * 50, (page + 1) * 50).entries()) {
      const tr = el('tr');
      for (const [column, cell] of row.entries()) {
        const td = el('td'), input = el('input'); input.type = 'text'; input.value = cell.raw; input.readOnly = readOnly;
        input.setAttribute('aria-label', `Row ${page * 50 + offset + 1} column ${column + 1}`); input.dataset.cellId = cell.id; input.dataset.workbookInput = cell.id;
        const result = el('output', values[cell.id].value); result.dataset.workbookValue = cell.id; result.setAttribute('aria-label', `Calculated value: ${values[cell.id].value}`);
        const commit = () => {
          if (disposed || !isCurrent() || readOnly) return;
          let appearance = null;
          try { if (!sharedCellMode && !appearanceKnown) appearance = captureAppearance(); } catch (_) { /* theme owner unavailable */ }
          try {
            const outcome = transact(source => {
              const next = editWorkbookCell(source, sheet.id, cell.id, input.value);
              if (next.appearance == null && appearance) next.appearance = appearance;
              return serializeWorkbook(next);
            });
            if (outcome?.outcome === 'failed') throw new Error(outcome.message || 'Edit unavailable');
            if (appearance) appearanceKnown = true;
            input.setCustomValidity('');
            scheduleRefresh();
          } catch (error) { input.setCustomValidity(error.message || 'Edit unavailable'); input.reportValidity(); }
        };
        // Every input event, including composition input, reaches the canonical
        // buffer before Save can run. The final composition event is idempotent.
        input.addEventListener('focus', () => { selection = {sheetId:sheet.id, cellId:cell.id, generation:++selectionGeneration}; });
        input.addEventListener('input', commit);
        input.addEventListener('compositionend', commit);
        input.addEventListener('blur', scheduleRefresh);
        bindContext?.(td, {sheetId:sheet.id, cellId:cell.id});
        td.append(input, result); tr.append(td);
      }
      table.append(tr);
    }
    container.append(table);
    let controlStatus = null;
    const mutate = (update, control = null) => {
      if (disposed || !isCurrent() || readOnly || sharedCellMode) return;
      let appearance = null;
      try { if (!appearanceKnown) appearance = captureAppearance(); } catch (_) { /* theme owner unavailable */ }
      try {
        const outcome = transact(source => {
          if (disposed || !isCurrent()) throw new Error('Workbook view is no longer current');
          const next = update(source);
          if (next.appearance == null && appearance) next.appearance = appearance;
          return serializeWorkbook(next);
        });
        if (outcome?.outcome === 'failed') throw new Error(outcome.message || 'Edit unavailable');
        if (appearance) appearanceKnown = true;
        control?.setCustomValidity?.(''); if (controlStatus) controlStatus.textContent = ''; scheduleRefresh();
      } catch (error) { if (controlStatus) controlStatus.textContent = error.message || 'Edit unavailable'; if (control) {control.setCustomValidity(error.message || 'Edit unavailable'); control.reportValidity();} }
    };
    const newId = prefix => {
      if (!globalThis.crypto?.randomUUID) throw new Error('Stable identity generation unavailable');
      return `${prefix}-${globalThis.crypto.randomUUID()}`;
    };
    const controls = el('section'); controls.setAttribute('aria-label','Ranges and charts');
    controlStatus = el('p'); controlStatus.setAttribute('role','status'); controls.append(controlStatus);
    const button = (title, handler) => { const node=el('button',title); node.type='button'; node.disabled=readOnly || sharedCellMode; node.addEventListener('click', () => {try {handler();} catch (error) {if (controlStatus) controlStatus.textContent = error.message || 'Edit unavailable'; }}); controls.append(node); };
    const field = (label,value,apply) => {
      const wrapper=el('label',label), input=el('input'); input.type='text'; input.value=String(value); input.readOnly=readOnly || sharedCellMode; input.setAttribute('aria-label',label);
      const commit=()=>{const captured=input.value; mutate(source=>apply(source,captured),input);};
      input.addEventListener('input',commit); input.addEventListener('compositionend',commit); input.addEventListener('blur',scheduleRefresh); wrapper.append(input); controls.append(wrapper);
    };
    const choice = (label,current,options,apply) => {
      const wrapper=el('label',label), input=el('select'); input.setAttribute('aria-label',label); input.disabled=readOnly || sharedCellMode;
      for (const [id,name] of options) {const option=el('option',name);option.value=id;option.selected=id===current;input.append(option);}
      input.value=current; input.addEventListener('change',()=>{const captured=input.value;mutate(source=>apply(source,captured),input);}); wrapper.append(input);controls.append(wrapper);
    };
    const ranges = book.ranges.filter(range => range.sheetId === sheet.id);
    button('Add range',()=>{const id=newId('range'), first=sheet.rows[0][0].id;mutate(source=>putWorkbookRange(source,{id,sheetId:sheet.id,startCellId:first,endCellId:first}));});
    for (const range of ranges.slice(0,64)) {
      controls.append(el('h3',`Range ${range.id}`));
      for (const key of ['startCellId','endCellId']) field(`${range.id} ${key}`,range[key],(source,value)=>putWorkbookRange(source,{id:range.id,[key]:value}));
    }
    if (ranges.length > 64) controls.append(el('p','Additional ranges omitted from this view.'));
    button('Add chart',()=>{if (!ranges.length) return; const id=newId('chart');mutate(source=>putWorkbookChart(source,{id,rangeId:ranges[0].id,type:'bar',title:'Chart'}));});
    const visibleCharts = book.charts.filter(chart=>ranges.some(range=>range.id===chart.rangeId));
    for (const chart of visibleCharts.slice(0,64)) {
      controls.append(el('h3',`Chart ${chart.id}`));
      field(`${chart.id} title`,chart.title,(source,value)=>putWorkbookChart(source,{id:chart.id,title:value}));
      choice(`${chart.id} type`,chart.type,[['bar','Bar'],['line','Line']],(source,value)=>putWorkbookChart(source,{id:chart.id,type:value}));
      choice(`${chart.id} range`,chart.rangeId,ranges.map(range=>[range.id,range.id]),(source,value)=>putWorkbookChart(source,{id:chart.id,rangeId:value}));
    }
    if (visibleCharts.length > 64) controls.append(el('p','Additional charts omitted from this view.'));
    container.append(controls);
    for (const chart of visibleCharts.slice(0,64)) {
      const data = chartValues(book, chart.id, values); if (data.sheetId !== sheet.id) continue;
      const figure = el('figure'), caption = el('figcaption', `Chart data: ${chart.title}`), dataTable = el('table');
      dataTable.setAttribute('aria-label', `${chart.title} chart data`);
      for (const row of data.rows) { const tr = el('tr'); for (const item of row) { const td = el('td', item.value); td.dataset.workbookValue = item.cellId; bindContext?.(td, {sheetId:sheet.id, cellId:item.cellId}); tr.append(td); } dataTable.append(tr); }
      const graphic = el('div'); graphic.dataset.workbookChart = chart.id; graphic.innerHTML = workbookChartSvg(book,chart.id,values);
      const range = ranges.find(item => item.id === chart.rangeId);
      if (range) bindContext?.(graphic, {sheetId:sheet.id, rangeId:range.id, startCellId:range.startCellId, endCellId:range.endCellId});
      figure.append(caption, graphic, dataTable); container.append(figure);
    }
    root.replaceChildren(container);
    if (focus) {
      const replacement = [...root.querySelectorAll('input[data-cell-id]')].find(input => input.dataset.cellId === focus.id);
      if (replacement) {
        replacement.focus({preventScroll:true});
        if (Number.isInteger(focus.start) && Number.isInteger(focus.end)) replacement.setSelectionRange(focus.start, focus.end, focus.direction || 'none');
      }
    }
  }
  refresh();
  return {refresh:scheduleRefresh, getSelection() { return !disposed && selection ? {...selection} : null; }, dispose() { disposed = true; selection = null; clearTimeout(refreshTimer); refreshTimer = null; root.replaceChildren(); }};
}
