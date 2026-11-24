// Bounded slide adapter for the existing Document html2pdf capability.
// It does not write Files, download, fetch assets, or install a renderer.
import { parseSlideDeck, renderSlideSvg } from './slideModel.js';

const MAX_PAGES = 16, MAX_PIXELS = 16_000_000, MAX_RENDER = 32 * 1024 * 1024, MAX_PDF = 32 * 1024 * 1024;
const clone = value => JSON.parse(JSON.stringify(value));
const freeze = value => { if (value && typeof value === 'object') { Object.values(value).forEach(freeze); Object.freeze(value); } return value; };
const unavailable = reason => ({state:'unavailable', reason});

/**
 * `loadRenderer` is the existing Document PDF capability; `assets` is a Map
 * captured by the caller's Files authority. `isCurrent` must fence the exact
 * source/asset scope. The caller saves the Blob through Files/Lore separately.
 */
export async function exportSlidePdf({source, sourceRef, sourceRevision, assets = new Map(), loadRenderer,
  isCurrent = () => true, signal = null} = {}) {
  if (typeof source !== 'string' || typeof sourceRef !== 'string' || !sourceRef || sourceRef.length > 4096
      || !sourceRevision || typeof sourceRevision !== 'object' || Array.isArray(sourceRevision)
      || !(assets instanceof Map) || typeof loadRenderer !== 'function' || typeof isCurrent !== 'function') {
    throw new TypeError('Captured slide source and PDF capability are required');
  }
  let revision;
  try { revision = clone(sourceRevision); if (JSON.stringify(revision).length > 4096) throw new Error(); }
  catch (_) { throw new TypeError('Invalid source revision'); }
  const deck = parseSlideDeck(source), width = 960, height = Math.round(width * deck.size.height / deck.size.width);
  if (deck.slides.length > MAX_PAGES || height < 240 || height > 3840 || width * height * deck.slides.length > MAX_PIXELS) {
    return unavailable('pdf_size_limit');
  }
  const identity = freeze({resourceRef:sourceRef, revision, deckId:deck.id, source});
  const assetRefs = freeze(deck.slides.flatMap(slide => slide.objects
    .filter(object => object.type === 'image' || object.type === 'chart')
    .map(object => ({slideId:slide.id, objectId:object.id, type:object.type,
      resourceRef:object.resourceRef, revision:clone(object.revision), ...(object.type === 'chart' ? {chartId:object.chartId} : {})}))));
  const check = () => signal?.aborted ? 'cancelled' : !isCurrent(identity) ? 'stale_source' : null;
  let invalid = check(); if (invalid) return unavailable(invalid);
  // Resolve only the data map supplied at invocation, before yielding control.
  let rendered = [], chars = 0;
  try {
    for (const slide of deck.slides) {
      const svg = renderSlideSvg(deck,slide.id,{assets}); chars += svg.length;
      if (chars > MAX_RENDER) return unavailable('pdf_size_limit');
      rendered.push(svg);
    }
  } catch (_) { return unavailable('slide_render_unavailable'); }
  let factory;
  try { factory = await loadRenderer(); } catch (_) { return unavailable('pdf_renderer_unavailable'); }
  invalid = check(); if (invalid) return unavailable(invalid);
  if (typeof factory !== 'function' || typeof document === 'undefined' || !document.createElement) return unavailable('pdf_renderer_unavailable');
  const container = document.createElement('div');
  container.style.cssText = `width:${width}px;margin:0;padding:0`;
  for (const [index, svg] of rendered.entries()) {
    const page = document.createElement('section');
    page.style.cssText = `width:${width}px;height:${height}px;overflow:hidden;margin:0;padding:0${index + 1 < rendered.length ? ';break-after:page;page-break-after:always' : ''}`;
    page.innerHTML = svg;
    const graphic = page.querySelector('svg');
    if (!graphic) return unavailable('slide_render_unavailable');
    graphic.style.cssText += `;display:block;width:${width}px;height:${height}px`;
    container.append(page);
  }
  rendered = [];
  try {
    const pdf = await factory().set({margin:0, pagebreak:{mode:['css']},
      html2canvas:{scale:1,allowTaint:false,useCORS:false},
      jsPDF:{unit:'pt',format:[width * .75,height * .75],orientation:width >= height ? 'landscape' : 'portrait'}})
      .from(container).outputPdf('blob');
    invalid = check(); if (invalid) return unavailable(invalid);
    if (!(pdf instanceof Blob) || pdf.size <= 5 || pdf.size > MAX_PDF) return unavailable('pdf_output_invalid');
    const prefix = new Uint8Array(await pdf.slice(0,5).arrayBuffer());
    invalid = check(); if (invalid) return unavailable(invalid);
    if (String.fromCharCode(...prefix) !== '%PDF-') return unavailable('pdf_output_invalid');
    return {state:'ready', blob:pdf, source:identity, assets:assetRefs};
  } catch (_) { return unavailable('pdf_render_unavailable'); }
}
