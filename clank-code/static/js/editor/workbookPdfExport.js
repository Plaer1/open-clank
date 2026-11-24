// Bounded workbook pagination using the existing Document PDF renderer.
import { parseWorkbook, calculateWorkbook, chartValues, workbookChartSvg } from './workbookModel.js';
import { portableDeliverableStyle } from '../theme/deliverableAppearance.js';

const ROWS = 10, COLUMNS = 5, MAX_PAGES = 12, MAX_TEXT = 80, WIDTH = 960, HEIGHT = 1240, MAX_PDF = 32 * 1024 * 1024;
const unavailable = reason => ({state:'unavailable',reason});
const limited = (value, limit = MAX_TEXT) => Array.from(String(value ?? '')).length <= limit && !/[\u0000-\u001f\u007f]/.test(String(value ?? ''));

export async function exportWorkbookPdf({source,sourceRef,sourceRevision,loadRenderer,isCurrent = () => true,signal = null} = {}) {
  if (typeof source !== 'string' || typeof sourceRef !== 'string' || !sourceRef || !sourceRevision || typeof sourceRevision !== 'object'
      || typeof loadRenderer !== 'function' || typeof isCurrent !== 'function') throw new TypeError('Captured workbook source is required');
  const revision = JSON.parse(JSON.stringify(sourceRevision));
  const book = parseWorkbook(source), identity = Object.freeze({resourceRef:sourceRef,revision:Object.freeze(revision),workbookId:book.id,source});
  const check = () => signal?.aborted ? 'cancelled' : !isCurrent(identity) ? 'stale_source' : null;
  let invalid = check(); if (invalid) return unavailable(invalid);
  const count = book.sheets.reduce((total,sheet) => total + Math.ceil(sheet.rows.length / ROWS) * Math.ceil(sheet.rows[0].length / COLUMNS),0) + book.charts.length;
  if (count > MAX_PAGES || book.sheets.some(sheet => !limited(sheet.name)) || book.charts.some(chart => !limited(chart.title,30))) return unavailable('workbook_pdf_size_limit');
  const values = calculateWorkbook(book,{isCurrent:() => !check()}), appearance = portableDeliverableStyle(book.appearance);
  if (book.sheets.some(sheet => sheet.rows.some(row => row.some(cell => !limited(values[cell.id]?.value))))) return unavailable('workbook_pdf_cell_too_long');
  for (const chart of book.charts) if (chartValues(book,chart.id,values).rows.flat().length > 200) return unavailable('workbook_pdf_chart_too_large');
  let factory; try { factory = await loadRenderer(); } catch (_) { return unavailable('pdf_renderer_unavailable'); }
  invalid = check(); if (invalid) return unavailable(invalid);
  if (typeof factory !== 'function' || typeof document === 'undefined') return unavailable('pdf_renderer_unavailable');
  const vars = appearance?.cssVariables || {}, container = document.createElement('div');
  container.style.cssText = `width:${WIDTH}px;margin:0;padding:0`;
  function page(title) {
    const section=document.createElement('section'); section.style.cssText=`box-sizing:border-box;width:${WIDTH}px;height:${HEIGHT}px;padding:36px;overflow:hidden;break-after:page;page-break-after:always;background:${vars['--oc-bg'] || '#ffffff'};color:${vars['--oc-fg'] || '#161616'};font-family:${appearance?.typography?.family || 'sans-serif'};font-size:12px`;
    const heading=document.createElement('h2'); heading.textContent=title; heading.style.cssText='font-size:18px;margin:0 0 18px'; section.append(heading);container.append(section);return section;
  }
  for (const sheet of book.sheets) {
    for (let r=0;r<sheet.rows.length;r+=ROWS) for (let c=0;c<sheet.rows[0].length;c+=COLUMNS) {
      const section=page(`${sheet.name} · rows ${r+1}–${Math.min(r+ROWS,sheet.rows.length)}, columns ${c+1}–${Math.min(c+COLUMNS,sheet.rows[0].length)}`);
      const table=document.createElement('table');table.style.cssText=`width:100%;table-layout:fixed;border-collapse:collapse;background:${vars['--oc-panel'] || '#ffffff'}`;
      for (const row of sheet.rows.slice(r,r+ROWS)) {
        const tr=document.createElement('tr');
        for (const cell of row.slice(c,c+COLUMNS)) { const td=document.createElement('td'); td.dataset.cellId=cell.id;td.textContent=String(values[cell.id]?.value ?? '');td.style.cssText=`border:1px solid ${vars['--oc-border'] || '#777777'};padding:7px;vertical-align:top;overflow-wrap:anywhere;word-break:break-all;white-space:pre-wrap;line-height:16px`;tr.append(td); }
        table.append(tr);
      }
      section.append(table);
    }
    for (const chart of book.charts.filter(item => book.ranges.find(range => range.id === item.rangeId)?.sheetId === sheet.id)) {
      const section=page(`${sheet.name} · ${chart.title}`), holder=document.createElement('div');
      holder.innerHTML=workbookChartSvg(book,chart.id,values); const svg=holder.querySelector('svg');
      if (!svg) return unavailable('workbook_chart_unavailable'); svg.style.cssText='display:block;width:100%;height:auto'; section.append(holder);
    }
  }
  if (container.children.length !== count) return unavailable('workbook_pdf_incomplete');
  if (!document.body) return unavailable('pdf_renderer_unavailable');
  // Measure the actual renderer fonts at page width; never silently crop data.
  container.style.position='fixed'; container.style.left='-100000px'; container.style.top='0';
  document.body.append(container);
  let overflow;
  try { overflow = [...container.children].some(section => section.scrollHeight > HEIGHT || section.scrollWidth > WIDTH); }
  finally { container.remove(); container.style.position=''; container.style.left=''; container.style.top=''; }
  if (overflow) return unavailable('workbook_pdf_page_overflow');
  if (container.lastElementChild) { container.lastElementChild.style.breakAfter='auto';container.lastElementChild.style.pageBreakAfter='auto'; }
  invalid = check(); if (invalid) return unavailable(invalid);
  try {
    const pdf=await factory().set({margin:0,pagebreak:{mode:[]},html2canvas:{scale:1,allowTaint:false,useCORS:false},jsPDF:{unit:'pt',format:[WIDTH*.75,HEIGHT*.75],orientation:'portrait'}}).from(container).outputPdf('blob');
    invalid=check(); if (invalid) return unavailable(invalid);
    if (!(pdf instanceof Blob) || pdf.size <= 5 || pdf.size > MAX_PDF) return unavailable('pdf_output_invalid');
    const prefix=new Uint8Array(await pdf.slice(0,5).arrayBuffer()); invalid=check(); if (invalid) return unavailable(invalid);
    if (String.fromCharCode(...prefix) !== '%PDF-') return unavailable('pdf_output_invalid');
    return {state:'ready',blob:pdf,source:identity,pageCount:count};
  } catch (_) { return unavailable('pdf_render_unavailable'); }
}
