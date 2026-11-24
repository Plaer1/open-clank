import { exportWorkbookPdf } from './workbookPdfExport.js';
import { runDeliverablePdfFiles } from './deliverablePdfFiles.js';

export function runWorkbookPdfFiles({state,source,sourceRef,sourceRevision,name,isCurrent,loadRenderer,importFile,reconcile,verifySource,makeFile,renderPdf=exportWorkbookPdf} = {}) {
  if (typeof verifySource !== 'function') throw new TypeError('Workbook source verification unavailable');
  const filename = `${String(name || 'workbook').replace(/\.clankbook$/i,'').replace(/[\\/\u0000-\u001f\u007f]/g,'_') || 'workbook'}.pdf`;
  return runDeliverablePdfFiles({state,isCurrent,reconcile,importFile,filename,receiptBinding:JSON.stringify([sourceRef,sourceRevision,source]),...(makeFile ? {makeFile} : {}),
    prepare:async () => await verifySource() === true && isCurrent()
      ? renderPdf({source,sourceRef,sourceRevision,loadRenderer,isCurrent}) : {state:'unavailable',reason:'stale_source'},
    verifyBeforeImport:verifySource,beforeDispatch:verifySource});
}
