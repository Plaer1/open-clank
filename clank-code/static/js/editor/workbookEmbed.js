// Read-only rendering of a caller-authorized, revision-bound Files snapshot.
// This module never resolves a ref, reads a file, or writes workbook state.
import {parseWorkbook, calculateWorkbook, chartValues, workbookChartSvg} from './workbookModel.js';
import {portableDeliverableStyle} from '../theme/deliverableAppearance.js';

const ID=/^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/;
const ROWS=20, COLUMNS=20, CHART_CELLS=200;
const copy=value=>JSON.parse(JSON.stringify(value));
const freeze=value=>{if(value && typeof value==='object'){Object.values(value).forEach(freeze);Object.freeze(value);}return value;};
const el=(tag,text)=>{const node=document.createElement(tag);if(text!=null)node.textContent=String(text);return node;};

/** `isCurrent` and `onOpenEditor` are supplied by the existing Files owner. */
export function mountWorkbookEmbed(root,{source,resourceRef,resourceId,revision,selector,isCurrent,onOpenEditor}={}) {
  if(!root || typeof source!=='string' || typeof resourceRef!=='string' || !resourceRef || resourceRef.length>16384
      || typeof resourceId!=='string' || !resourceId || resourceId.length>256 || !revision || typeof revision!=='object' || Array.isArray(revision)
      || !selector || typeof selector!=='object' || typeof isCurrent!=='function' || typeof onOpenEditor!=='function') throw new TypeError('Authorized workbook snapshot required');
  let identity;
  try {const r=copy(revision),s=copy(selector);if(JSON.stringify(r).length>4096||JSON.stringify(s).length>1024)throw new Error();identity=freeze({resourceRef,resourceId,revision:r,selector:s});}
  catch(_){throw new TypeError('Workbook snapshot identity unavailable');}
  let disposed=false,owned=null,state='loading',book=null,values=null;
  const alive=()=>!disposed && (!owned || root.contains(owned));
  const current=()=>{try{return alive() && isCurrent(identity)===true;}catch(_){return false;}};
  function message(next,text,allowOpen=false) {if(!alive())return;state=next;const section=el('section'),p=el('p',text);p.setAttribute('role','status');section.append(p);if(allowOpen && current()){const open=el('button','Open in Editor');open.type='button';open.addEventListener('click',()=>{if(current())onOpenEditor(identity);else message('stale','Workbook snapshot is no longer current. Reopen the source.');});section.append(open);}root.replaceChildren(section);owned=section;}
  function validSelector(s) {
    if(s.kind==='sheet')return typeof s.sheetId==='string' && ID.test(s.sheetId)
      && (s.rowStart==null || Number.isSafeInteger(s.rowStart)&&s.rowStart>=0)
      && (s.colStart==null || Number.isSafeInteger(s.colStart)&&s.colStart>=0)
      && Object.keys(s).every(k=>['kind','sheetId','rowStart','colStart'].includes(k));
    if(s.kind==='range')return typeof s.rangeId==='string' && ID.test(s.rangeId) && Object.keys(s).every(k=>['kind','rangeId'].includes(k));
    if(s.kind==='chart')return typeof s.chartId==='string' && ID.test(s.chartId) && Object.keys(s).every(k=>['kind','chartId'].includes(k));
    return false;
  }
  function render() {
    if(!alive())return;
    if(!current()){message('stale','Workbook snapshot is no longer current. Reopen the source.');return;}
    if(!validSelector(identity.selector)){message('missing','Workbook selection is unavailable.');return;}
    if(!book){message('unavailable','Workbook source is unavailable.');return;}
    const selected=identity.selector;let sheet, rowStart=0,colStart=0,rowEnd=0,colEnd=0,chart=null;
    if(selected.kind==='chart') {
      chart=book.charts.find(item=>item.id===selected.chartId);
      if(!chart){message('missing','Workbook chart is unavailable.');return;}
      const data=chartValues(book,chart.id,values),count=data.rows.reduce((sum,row)=>sum+row.length,0);
      if(count>CHART_CELLS){message('oversize','Chart range exceeds the 200-cell display limit. Open in Editor.',true);return;}
    } else if(selected.kind==='sheet') {
      sheet=book.sheets.find(item=>item.id===selected.sheetId);
      if(!sheet){message('missing','Workbook sheet is unavailable.');return;}
      rowStart=selected.rowStart||0;colStart=selected.colStart||0;
      if(rowStart>=sheet.rows.length||colStart>=sheet.rows[0].length){message('missing','Workbook slice is unavailable.');return;}
      rowEnd=sheet.rows.length;colEnd=sheet.rows[0].length;
    } else {
      const range=book.ranges.find(item=>item.id===selected.rangeId);
      sheet=range&&book.sheets.find(item=>item.id===range.sheetId);
      if(!sheet){message('missing','Workbook range is unavailable.');return;}
      let start=null,end=null;
      sheet.rows.forEach((row,r)=>row.forEach((cell,c)=>{if(cell.id===range.startCellId)start=[r,c];if(cell.id===range.endCellId)end=[r,c];}));
      if(!start||!end){message('missing','Workbook range is unavailable.');return;}
      rowStart=Math.min(start[0],end[0]);rowEnd=Math.max(start[0],end[0])+1;
      colStart=Math.min(start[1],end[1]);colEnd=Math.max(start[1],end[1])+1;
    }
    if(!current()){message('stale','Workbook snapshot is no longer current. Reopen the source.');return;}
    const section=el('section');section.setAttribute('aria-label','Read-only workbook');
    const appearance=portableDeliverableStyle(book.appearance),vars=appearance?.cssVariables || {};
    if(section.style){section.style.background=vars['--oc-bg']||'#ffffff';section.style.color=vars['--oc-fg']||'#161616';section.style.fontFamily=appearance?.typography?.family||'sans-serif';}
    if(chart){const heading=el('h3',chart.title), graphic=el('div');graphic.innerHTML=workbookChartSvg(book,chart.id,values);section.append(heading,graphic);state='ready';}
    else {
      section.append(el('h3',sheet.name));const table=el('table');table.setAttribute('aria-label',`${sheet.name} cells`);
      const stopRow=Math.min(rowEnd,rowStart+ROWS),stopCol=Math.min(colEnd,colStart+COLUMNS);
      for(let r=rowStart;r<stopRow;r++){const tr=el('tr');for(let c=colStart;c<stopCol;c++){
        const cell=sheet.rows[r][c],result=values[cell.id],td=el('td',result?.value ?? '');td.dataset.cellId=cell.id;
        td.setAttribute('aria-label',`${cell.id}: ${String(result?.value ?? '')}`);if(result?.error)td.setAttribute('title',String(result.message||result.value));tr.append(td);
      }table.append(tr);}
      section.append(table);const total=(rowEnd-rowStart)*(colEnd-colStart),shown=(stopRow-rowStart)*(stopCol-colStart);
      state=shown<total?'oversize':'ready';if(shown<total)section.append(el('p',`Showing ${shown} of ${total} cells; open in Editor for the rest.`));
    }
    const open=el('button','Open in Editor');open.type='button';open.addEventListener('click',()=>{if(!current()){message('stale','Workbook snapshot is no longer current. Reopen the source.');return;}onOpenEditor(identity);});section.append(open);
    if(!current()){message('stale','Workbook snapshot is no longer current. Reopen the source.');return;}
    root.replaceChildren(section);owned=section;
  }
  if(!current())message('stale','Workbook snapshot is no longer current. Reopen the source.');
  else {
    try {book=parseWorkbook(source);values=calculateWorkbook(book,{isCurrent:current});if(!current())message('stale','Workbook snapshot is no longer current. Reopen the source.');else render();}
    catch(error){message(String(error?.message||'').includes('exceeds limit')?'oversize':'unavailable','Workbook source is unavailable or exceeds supported limits.');}
  }
  return {refresh:render,getState(){return alive()?{state,identity}:null;},dispose(){if(disposed)return;disposed=true;book=null;values=null;if(owned&&root.contains(owned))root.replaceChildren();owned=null;}};
}
