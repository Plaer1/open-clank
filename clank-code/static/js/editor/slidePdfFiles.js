import { parseSlideDeck, slideChartAssetKey } from './slideModel.js';
import { exportSlidePdf } from './slidePdfExport.js';
import { runDeliverablePdfFiles } from './deliverablePdfFiles.js';

// Assets stay scoped to this slide producer; Files receipt handling is shared.
export function runSlidePdfFiles({state, source, sourceRef, sourceRevision, name, isCurrent,
  loadRenderer, importFile, reconcile, acquireAssets = null, verifyAssets = null, makeFile,
  renderPdf = exportSlidePdf} = {}) {
  if (typeof loadRenderer !== 'function') throw new TypeError('Slide PDF owner is unavailable');
  const filename = `${String(name || 'slides').replace(/\.clankslides$/i,'').replace(/[\\/\u0000-\u001f\u007f]/g,'_') || 'slides'}.pdf`;
  return runDeliverablePdfFiles({state,isCurrent,reconcile,importFile,filename,receiptBinding:JSON.stringify([sourceRef,sourceRevision,source]),
    ...(typeof verifyAssets === 'function' ? {beforeDispatch:async () => isCurrent() && await verifyAssets() === true && isCurrent()} : {}),...(makeFile ? {makeFile} : {}),
    prepare:async () => {
      const deck = parseSlideDeck(source);
      const linked = deck.slides.flatMap(slide => slide.objects.filter(object => object.type === 'image' || object.type === 'chart'));
      let assets = new Map();
      if (linked.length) {
        if (typeof acquireAssets !== 'function') return {state:'unavailable',reason:'linked_assets_unavailable'};
        const captured = await acquireAssets();
        if (!isCurrent()) return {state:'unavailable',reason:'stale_source'};
        if (captured?.state !== 'ready' || !(captured.assets instanceof Map)) return {state:'unavailable',reason:captured?.state === 'stale' ? 'stale_source' : 'linked_assets_unavailable'};
        assets = captured.assets;
        for (const object of linked) {
          const key = object.type === 'chart' ? slideChartAssetKey(object.resourceRef,object.chartId) : object.resourceRef;
          const asset = assets.get(key);
          if (!asset || asset.revision?.kind !== object.revision?.kind || asset.revision?.value !== object.revision?.value
              || (object.type === 'chart' ? asset.kind !== 'workbook-chart' || asset.chartId !== object.chartId : asset.kind !== 'image'))
            return {state:'unavailable',reason:'linked_assets_unavailable'};
        }
      }
      const rendered = await renderPdf({source,sourceRef,sourceRevision,assets,loadRenderer,isCurrent});
      if (!isCurrent()) return {state:'unavailable',reason:'stale_source'};
      if (rendered?.state !== 'ready') return {state:'unavailable',reason:rendered?.reason || 'pdf_renderer_unavailable'};
      if (linked.length && (typeof verifyAssets !== 'function' || await verifyAssets() !== true || !isCurrent())) return {state:'unavailable',reason:'linked_assets_stale'};
      return rendered;
    }});
}
