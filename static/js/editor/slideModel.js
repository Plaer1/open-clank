// Ordinary Files source for a finite editable deck. This module never loads assets.
import { portableDeliverableStyle } from '../theme/deliverableAppearance.js';
import { workbookChartSvg } from './workbookModel.js';

const ID = /^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/;
const MAX_SOURCE = 4 * 1024 * 1024, MAX_SLIDES = 100, MAX_OBJECTS = 3000, MAX_IMAGE = 8 * 1024 * 1024, MAX_RENDER = 32 * 1024 * 1024;
const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const copy = value => JSON.parse(JSON.stringify(value));
const isObject = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const finite = (value, min, max) => typeof value === 'number' && Number.isFinite(value) && value >= min && value <= max;
const text = (value, max = 65536) => typeof value === 'string' && value.length <= max;
const color = (value, fallback) => /^#[0-9a-f]{6}$/i.test(value || '') ? value : fallback;
const canonical = value => Array.isArray(value) ? value.map(canonical) : isObject(value) ? Object.fromEntries(Object.keys(value).sort().map(k => [k, canonical(value[k])])) : value;
const sameRevision = (a, b) => a != null && b != null && JSON.stringify(canonical(a)) === JSON.stringify(canonical(b));

/** Parse without issuing IDs, resolving resources, or mutating the caller. */
export function parseSlideDeck(source) {
  const raw = typeof source === 'string' ? source : JSON.stringify(source);
  if (typeof raw !== 'string' || raw.length > MAX_SOURCE) throw new TypeError('Slide source exceeds limit');
  const deck = JSON.parse(raw);
  if (!isObject(deck) || deck.schema !== 'openclank.slides' || deck.version !== 1 || !ID.test(deck.id || '') || !text(deck.title, 512)
      || !isObject(deck.size) || !finite(deck.size.width, 1, 10000) || !finite(deck.size.height, 1, 10000)
      || !Array.isArray(deck.slides) || !deck.slides.length || deck.slides.length > MAX_SLIDES
      || (deck.appearance != null && !isObject(deck.appearance)) || (deck.provenance != null && !isObject(deck.provenance))
      || (deck.template != null && (!isObject(deck.template) || !text(deck.template.ref, 4096) || !deck.template.ref
           || !text(deck.template.version, 128) || !deck.template.version
           || (deck.template.recipeRef != null && !text(deck.template.recipeRef, 4096))))) throw new TypeError('Invalid slide deck');
  const ids = new Set([deck.id]); let count = 0;
  const take = id => { if (typeof id !== 'string' || !ID.test(id) || ids.has(id)) throw new TypeError('Invalid or duplicate slide identity'); ids.add(id); };
  for (const slide of deck.slides) {
    if (!isObject(slide)) throw new TypeError('Invalid slide');
    take(slide.id);
    if (!text(slide.name, 512) || !text(slide.notes, 65536) || !Array.isArray(slide.objects)) throw new TypeError('Invalid slide');
    for (const object of slide.objects) {
      if (!isObject(object)) throw new TypeError('Invalid slide object');
      take(object.id);
      if (++count > MAX_OBJECTS || !['text','vector','image','chart'].includes(object.type)
          || !finite(object.x, -10000, 10000) || !finite(object.y, -10000, 10000)
          || !finite(object.width, 0.01, 20000) || !finite(object.height, 0.01, 20000)) throw new TypeError('Invalid slide geometry');
      if (object.type === 'text' && (!text(object.text) || (object.fontSize != null && !finite(object.fontSize, 1, 1000)))) throw new TypeError('Invalid text object');
      if (object.type === 'vector' && !['rect','ellipse','line'].includes(object.shape)) throw new TypeError('Invalid vector object');
      if (['image','chart'].includes(object.type) && (!text(object.resourceRef, 4096) || !object.resourceRef || object.revision == null)) throw new TypeError('Invalid resource reference');
      if (object.type === 'image' && !text(object.alt, 1024)) throw new TypeError('Invalid image description');
      if (object.type === 'chart' && (!ID.test(object.chartId || '') || !text(object.title, 512))) throw new TypeError('Invalid chart reference');
    }
  }
  return deck;
}
export function serializeSlideDeck(deck) { return JSON.stringify(parseSlideDeck(deck), null, 2) + '\n'; }

export function newSlideDeckSource(title, appearance) {
  return serializeSlideDeck({
    schema:'openclank.slides',version:1,id:`deck-${crypto.randomUUID()}`,title,appearance,size:{width:1280,height:720},
    slides:[{id:'slide-1',name:'Title',notes:'',objects:[
      {id:'title',type:'text',x:96,y:190,width:1088,height:130,fontSize:64,text:title},
      {id:'subtitle',type:'text',x:96,y:340,width:1088,height:100,fontSize:32,text:''},
    ]}],
  });
}
export function editSlideObject(source, slideId, objectId, patch) {
  if (!isObject(patch) || 'id' in patch || 'type' in patch) throw new TypeError('Object identity cannot change');
  const deck = parseSlideDeck(source), slide = deck.slides.find(item => item.id === slideId), index = slide?.objects.findIndex(item => item.id === objectId) ?? -1;
  if (index < 0) throw new TypeError('Unknown slide object');
  slide.objects[index] = {...slide.objects[index], ...copy(patch)};
  return parseSlideDeck(deck);
}
/** Copy keeps slide/object IDs scoped to the new document; caller supplies new Files identity. */
export function remixSlideDeck(source, { id, title, sourceRef } = {}) {
  const deck = parseSlideDeck(source);
  if (!ID.test(id || '') || id === deck.id || !text(sourceRef, 4096) || !sourceRef || (title != null && !text(title, 512))) throw new TypeError('Invalid remix identity');
  const previous = deck.id; deck.id = id; if (title != null) deck.title = title;
  deck.provenance = {...(deck.provenance || {}), remix: {sourceRef, sourceDeckId: previous}};
  return parseSlideDeck(deck);
}
function imageData(object, assets) {
  const asset = assets instanceof Map ? assets.get(object.resourceRef) : undefined;
  if (!isObject(asset) || asset.kind !== 'image' || !sameRevision(asset.revision, object.revision)
      || !['image/png','image/jpeg','image/gif','image/webp'].includes(asset.mime) || typeof asset.base64 !== 'string'
      || asset.base64.length > Math.ceil(MAX_IMAGE / 3) * 4 || !/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(asset.base64)) return null;
  try {
    const bytes = atob(asset.base64); if (!bytes.length || bytes.length > MAX_IMAGE || btoa(bytes) !== asset.base64) return null;
    const prefix = Array.from(bytes.slice(0, 12), c => c.charCodeAt(0));
    const signature = asset.mime === 'image/png' ? [137,80,78,71,13,10,26,10].every((v,i) => prefix[i] === v)
      : asset.mime === 'image/jpeg' ? prefix[0] === 255 && prefix[1] === 216 && prefix[2] === 255
      : asset.mime === 'image/gif' ? bytes.startsWith('GIF87a') || bytes.startsWith('GIF89a')
      : bytes.startsWith('RIFF') && bytes.slice(8,12) === 'WEBP';
    return signature ? `data:${asset.mime};base64,${asset.base64}` : null;
  } catch (_) { return null; }
}
function placeholder(o, label) {
  return `<g><rect x="${o.x}" y="${o.y}" width="${o.width}" height="${o.height}" fill="#eeeeee" stroke="#555555" stroke-dasharray="6 4"/><text x="${o.x+8}" y="${o.y+20}" fill="#333333" font-size="14">${escape(label)}</text></g>`;
}
export function slideChartAssetKey(resourceRef, chartId) {
  return JSON.stringify([resourceRef, chartId]);
}
function chartSvg(o, assets) {
  const a = assets instanceof Map ? (assets.get(slideChartAssetKey(o.resourceRef, o.chartId)) || assets.get(o.resourceRef)) : undefined;
  if (isObject(a) && a.kind === 'workbook-chart' && a.chartId === o.chartId && sameRevision(a.revision,o.revision)
      && typeof a.source === 'string' && a.source.length <= 8 * 1024 * 1024) {
    try {
      const svg = workbookChartSvg(a.source, o.chartId);
      return svg.replace('<svg ', `<svg x="${o.x}" y="${o.y}" width="${o.width}" height="${o.height}" `);
    } catch (_) { return placeholder(o, `Chart unavailable: ${o.title}`); }
  }
  if (!isObject(a) || a.kind !== 'chart' || a.chartId !== o.chartId || !sameRevision(a.revision,o.revision)
      || !Array.isArray(a.values) || !a.values.length || a.values.length > 40
      || a.values.some(v => !finite(v, 0, 1e12))) return placeholder(o, `Chart unavailable: ${o.title}`);
  const max = Math.max(...a.values, 1), bar = o.width / a.values.length;
  return `<g><rect x="${o.x}" y="${o.y}" width="${o.width}" height="${o.height}" fill="#ffffff" stroke="#777777"/>${a.values.map((v,i) => {
    const h = Math.max(0, (o.height-24)*v/max); return `<rect x="${o.x+i*bar+bar*.1}" y="${o.y+o.height-h}" width="${bar*.8}" height="${h}" fill="#315caa"/>`;
  }).join('')}<text x="${o.x+6}" y="${o.y+16}" font-size="13" fill="#222222">${escape(o.title)}</text></g>`;
}
/** Renders only owned, revision-matched data supplied by the caller; no network or Files calls. */
export function renderSlideSvg(source, slideId, { assets = new Map(), identifyObjects = false } = {}) {
  const deck = parseSlideDeck(source), slide = deck.slides.find(item => item.id === slideId);
  if (!slide) throw new TypeError('Unknown slide');
  const appearance = portableDeliverableStyle(deck.appearance);
  const vars = appearance?.cssVariables || {};
  const style = Object.entries(vars).map(([key,value]) => `${key}:${value}`).join(';');
  const background = vars['--oc-bg'] || '#ffffff', foreground = vars['--oc-fg'] || '#161616';
  const font = appearance?.typography?.family || 'sans-serif';
  const renderObject = o => {
    if (o.type === 'text') {
      const size = o.fontSize || 24, fill = color(o.color, foreground);
      return `<text x="${o.x}" y="${o.y+size}" fill="${escape(fill)}" font-size="${size}" font-family="${escape(font)}">${o.text.split('\n').map((line,i) => `<tspan x="${o.x}" dy="${i ? size*1.2 : 0}">${escape(line)}</tspan>`).join('')}</text>`;
    }
    if (o.type === 'vector') {
      const fill = color(o.fill, '#dbe6f5'), stroke = color(o.stroke, '#1c3250');
      if (o.shape === 'ellipse') return `<ellipse cx="${o.x+o.width/2}" cy="${o.y+o.height/2}" rx="${o.width/2}" ry="${o.height/2}" fill="${fill}" stroke="${stroke}"/>`;
      if (o.shape === 'line') return `<line x1="${o.x}" y1="${o.y}" x2="${o.x+o.width}" y2="${o.y+o.height}" stroke="${stroke}"/>`;
      return `<rect x="${o.x}" y="${o.y}" width="${o.width}" height="${o.height}" fill="${fill}" stroke="${stroke}"/>`;
    }
    if (o.type === 'image') { const data=imageData(o, assets); return data ? `<image x="${o.x}" y="${o.y}" width="${o.width}" height="${o.height}" preserveAspectRatio="xMidYMid meet" href="${data}"><title>${escape(o.alt)}</title></image>` : placeholder(o, `Image unavailable: ${o.alt}`); }
    return chartSvg(o, assets);
  };
  const parts = []; let length = 0;
  for (const object of slide.objects) {
    const rendered = renderObject(object);
    const part = identifyObjects ? `<g data-slide-object="${escape(object.id)}" data-slide-type="${escape(object.type)}">${rendered}</g>` : rendered;
    length += part.length;
    if (length > MAX_RENDER) throw new RangeError('Rendered slide exceeds limit');
    parts.push(part);
  }
  const output = parts.join('');
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${deck.size.width} ${deck.size.height}" style="${escape(style)}" role="img" aria-label="${escape(slide.name)}"><rect width="100%" height="100%" fill="${escape(background)}"/>${output}</svg>`;
}
/** Self-contained presentation plus the exact editable JSON source; scripts never execute. */
export function slideDeckHtml(source, { assets = new Map() } = {}) {
  const deck = parseSlideDeck(source);
  const embedded = JSON.stringify(deck).replace(/</g,'\\u003c').replace(/\u2028/g,'\\u2028').replace(/\u2029/g,'\\u2029');
  const sections = []; let length = embedded.length;
  for (const slide of deck.slides) {
    const section = `<section aria-label="${escape(slide.name)}">${renderSlideSvg(deck, slide.id, {assets})}</section>`;
    length += section.length; if (length > MAX_RENDER) throw new RangeError('Slide export exceeds limit');
    sections.push(section);
  }
  return `<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>${escape(deck.title)}</title><style>body{margin:0;background:#222}section{width:min(100%,1200px);margin:1rem auto}svg{display:block;width:100%;height:auto}@media print{body{background:#fff}section{width:100%;margin:0;break-after:page}section:last-child{break-after:auto}}</style></head><body>${sections.join('')}<script type="application/json" id="openclank-slide-source">${embedded}</script></body></html>`;
}
