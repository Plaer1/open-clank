// Bounded, explicit Files-backed slide assets. This module never refreshes a
// reference or reads a host path; the caller owns account/source currentness.
import { parseSlideDeck, slideChartAssetKey } from './slideModel.js';
import { parseWorkbook, workbookChartSvg } from './workbookModel.js';

const MAX_ASSETS = 32, MAX_IMAGE = 8 * 1024 * 1024, MAX_TOTAL = 16 * 1024 * 1024;
const REF = /^rr\d+\.[A-Za-z0-9_-]+={0,2}$/;
const canonical = value => Array.isArray(value) ? value.map(canonical) : value && typeof value === 'object' ? Object.fromEntries(Object.keys(value).sort().map(key => [key, canonical(value[key])])) : value;
const same = (a,b) => a != null && b != null && JSON.stringify(canonical(a)) === JSON.stringify(canonical(b));
const revision = value => value && typeof value === 'object' && !Array.isArray(value) && Object.keys(value).length === 2
  && typeof value.kind === 'string' && value.kind.length > 0 && value.kind.length <= 128
  && typeof value.value === 'string' && value.value.length > 0 && value.value.length <= 512;
const refOk = value => typeof value === 'string' && value.length <= 16384 && REF.test(value);
const unavailable = (state,issues = []) => ({state,assets:new Map(),issues});
const errorCode = error => ['resource_changed','resource_ref_stale','policy_generation_changed','stale_revision'].includes(error?.code) ? 'stale' : 'unavailable';
function resource(value) { return value?.resource || value; }
function sameResource(a,b) { return a?.id && a.id === b?.id && a.provider === b.provider && a.kind === b.kind && a.policy_generation === b.policy_generation && same(a.revision,b.revision); }
function mimeFrom(bytes) {
  if (bytes.length >= 8 && [137,80,78,71,13,10,26,10].every((v,i)=>bytes[i]===v)) return 'image/png';
  if (bytes.length >= 3 && bytes[0]===255 && bytes[1]===216 && bytes[2]===255) return 'image/jpeg';
  const text = String.fromCharCode(...bytes.slice(0,12));
  if (text.startsWith('GIF87a') || text.startsWith('GIF89a')) return 'image/gif';
  if (text.startsWith('RIFF') && text.slice(8,12)==='WEBP') return 'image/webp';
  return null;
}
async function boundedBytes(response, limit, check) {
  if (!response?.ok || !response.body?.getReader) throw new Error('unavailable');
  const declared = response.headers?.get('content-length');
  if (declared != null && (!/^\d+$/.test(declared) || Number(declared) > limit)) throw new Error('size_limit');
  const reader = response.body.getReader(), chunks = []; let size = 0;
  try {
    while (true) {
      const {done,value} = await reader.read(); check(); if (done) break;
      const bytes = value instanceof Uint8Array ? value : new Uint8Array(value);
      size += bytes.length; if (size > limit) throw new Error('size_limit'); chunks.push(bytes);
    }
  } finally { try { await reader.cancel(); } catch (_) {} }
  if (!size || (declared != null && Number(declared) !== size)) throw new Error('size_mismatch');
  const result = new Uint8Array(size); let offset = 0;
  for (const bytes of chunks) { result.set(bytes,offset); offset += bytes.length; }
  return result;
}
function base64(bytes) {
  let text = ''; for (let i=0;i<bytes.length;i+=8192) text += String.fromCharCode(...bytes.subarray(i,i+8192));
  return btoa(text);
}

/** Acquire a finite snapshot for an already-authorized saved slide resource.
 * isCurrent(capture) must fence the originating account, workspace, source
 * revision and exact source text for the whole call, including after awaits. */
export async function acquireSlideAssets({source,sourceRef,sourceRevision,filesClient,accountId,isCurrent,signal = null} = {}) {
  if (typeof source !== 'string' || !refOk(sourceRef) || !revision(sourceRevision) || typeof accountId !== 'string' || !accountId
      || typeof isCurrent !== 'function' || typeof filesClient?.stat !== 'function' || typeof filesClient?.openResource !== 'function'
      || typeof filesClient?.contentUrl !== 'function' || typeof filesClient?.fetchImpl !== 'function') return unavailable('unavailable');
  let deck; try { deck = parseSlideDeck(source); } catch (_) { return unavailable('unavailable'); }
  const capture = Object.freeze({accountId,sourceRef,sourceRevision:Object.freeze({...sourceRevision}),source});
  const check = () => { if (signal?.aborted || isCurrent(capture) !== true) throw Object.assign(new Error('scope_changed'),{code:'resource_changed'}); };
  const descriptors = deck.slides.flatMap(slide => slide.objects.filter(o => o.type === 'image' || o.type === 'chart')
    .map(o => ({objectId:o.id,kind:o.type,ref:o.resourceRef,revision:o.revision,chartId:o.chartId})));
  if (descriptors.length > MAX_ASSETS) return unavailable('unavailable',[{objectId:null,reason:'asset_count_limit'}]);
  if (descriptors.some(item => !refOk(item.ref) || !revision(item.revision))) return unavailable('unavailable',[{objectId:null,reason:'invalid_reference'}]);
  const issues = [], assets = new Map(); let total = 0;
  try {
    check(); const sourceBefore = resource(await filesClient.stat(sourceRef,{signal})); check();
    if (!same(sourceBefore?.revision,sourceRevision)) return unavailable('stale');
    for (const item of descriptors) {
      check();
      const assetKey = item.kind === 'chart' ? slideChartAssetKey(item.ref,item.chartId) : item.ref;
      if (assets.has(assetKey)) {
        const previous = assets.get(assetKey);
        if (previous.kind !== (item.kind === 'chart' ? 'workbook-chart' : item.kind) || !same(previous.revision,item.revision) || (item.kind === 'chart' && previous.chartId !== item.chartId)) issues.push({objectId:item.objectId,reason:'shared_reference_conflict'});
        continue;
      }
      try {
        const before = resource(await filesClient.stat(item.ref,{signal})); check();
        if (!same(before?.revision,item.revision)) { issues.push({objectId:item.objectId,reason:'stale'}); continue; }
        if (item.kind === 'image') {
          if (!Number.isSafeInteger(before.size) || before.size < 1 || before.size > MAX_IMAGE || total + before.size > MAX_TOTAL) throw new Error('size_limit');
          const response = await filesClient.fetchImpl(filesClient.contentUrl(item.ref,{purpose:'download',expectedRevision:item.revision}),{credentials:'same-origin',cache:'no-store',signal}); check();
          const bytes = await boundedBytes(response,Math.min(MAX_IMAGE,MAX_TOTAL-total),check); check();
          const mime = mimeFrom(bytes); if (!mime || bytes.length !== before.size || (before.mime_type && before.mime_type !== mime)) throw new Error('unsupported_image');
          const after = resource(await filesClient.stat(item.ref,{signal})); check();
          if (!sameResource(before,after)) { issues.push({objectId:item.objectId,reason:'stale'}); continue; }
          total += bytes.length; assets.set(item.ref,{kind:'image',revision:{...item.revision},mime,base64:base64(bytes)});
        } else {
          if (!Number.isSafeInteger(before.size) || before.size < 1 || before.size > 8*1024*1024 || total + before.size > MAX_TOTAL) throw new Error('size_limit');
          const opened = await filesClient.openResource(item.ref,{signal,expectedRevision:item.revision}); check();
          if (!sameResource(before,resource(opened)) || typeof opened?.payload?.text !== 'string') throw new Error('workbook_unavailable');
          const workbookSource = opened.payload.text, workbookBytes = new TextEncoder().encode(workbookSource).byteLength;
          if (workbookBytes > 8*1024*1024 || total + workbookBytes > MAX_TOTAL) throw new Error('size_limit');
          const book = parseWorkbook(workbookSource);
          if (!book.charts.some(entry => entry.id === item.chartId)) throw new Error('workbook_unavailable');
          workbookChartSvg(book,item.chartId); // Use the workbook producer and retain its signed values/gaps.
          const after = resource(await filesClient.stat(item.ref,{signal})); check();
          if (!sameResource(before,after)) { issues.push({objectId:item.objectId,reason:'stale'}); continue; }
          total += workbookBytes;
          assets.set(assetKey,{kind:'workbook-chart',revision:{...item.revision},chartId:item.chartId,source:workbookSource});
        }
      } catch (error) {
        check(); issues.push({objectId:item.objectId,reason:errorCode(error)==='stale' ? 'stale' : ['size_limit','size_mismatch','unsupported_image','chart_renderer_unsupported','workbook_unavailable'].includes(error?.message) ? error.message : 'unavailable'});
      }
    }
    const sourceAfter = resource(await filesClient.stat(sourceRef,{signal})); check();
    if (!sameResource(sourceBefore,sourceAfter)) return unavailable('stale');
    return {state:issues.length ? 'partial' : 'ready',assets,issues};
  } catch (error) { return unavailable(errorCode(error)==='stale' ? 'stale' : 'unavailable'); }
}
