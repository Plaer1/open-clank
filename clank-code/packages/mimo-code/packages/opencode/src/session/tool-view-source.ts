/** Host-private, process-local T04 candidate provenance; never model metadata. */
import type { MessageV2 } from './message-v2'
import type { ToolTimingCapture } from './tool-timing'
import type { ToolViewAdmission } from './tool-view-decision'
import { selectBashView } from './tool-view-bash-proof'
import { selectReadView } from './tool-view-read'
import { validGcfReceipt, authenticatedOutline } from './gcf-source-proof'

type Original = Omit<ToolViewAdmission,'on'|'clockID'|'messageID'|'partID'|'actorID'|'innerTurnID'|'profile'> & {version:1|2|3;requested:true;profile?:ToolViewAdmission['profile'];profiles?:Record<string,boolean>;lineCapRows?:number}
const fields=['version','requested','sessionID','rootOperationID','hostAccountID','holderID','generation','settingsRevision','providerID','modelID','routeMethodKey','connectionID','modelRouteID','profile']
const profileKeys=['jsonWhitespace','terminalAnsi','htmlMarkdown','structuredSearch']
const additionalProfileKeys=['repeatedLines','unifiedDiff']
const schemaProfileKey='schemaAnnotations'
const v3ProfileKeys=[...profileKeys,...additionalProfileKeys,schemaProfileKey,'toon','sourceOutlines','tap13','base64Lines','ndjsonLogs','lineCap']
const validCap=(v:unknown):v is number=>Number.isSafeInteger(v) && (v as number)>=1 && (v as number)<=100
/** Preserve each historical closed set; shape is the only optional true key. */
function validProfiles(version:unknown,profiles:unknown):boolean {
  if (!profiles || typeof profiles!=='object' || Array.isArray(profiles)) return false
  const p=profiles as Record<string,unknown>, keys=Object.keys(p)
  const optionalOn=['jsonShape','readUnnumbered','progressRedraw','lockfileVersions','markdownSlim','unittestSuccess','cargoArtifacts','untrackedStatus']
  if (optionalOn.some(key=>Object.prototype.hasOwnProperty.call(p,key) && p[key]!==true)) return false
  const actual=keys.filter(key=>!optionalOn.includes(key)).sort().join(',')
  const optional=[schemaProfileKey,'toon','sourceOutlines','tap13','base64Lines','ndjsonLogs']
  const variants=[profileKeys,[...profileKeys,...additionalProfileKeys],...optional.map((_,i)=>[...profileKeys,...additionalProfileKeys,...optional.slice(0,i+1)])]
  const expected=version===3 ? [v3ProfileKeys] : version===2 ? variants : []
  if (!expected.some(set=>[...set].sort().join(',')===actual)) return false
  return keys.every(key=>typeof p[key]==='boolean') && (version!==3 || p.lineCap===true)
}
export function outlineViewEnabled(encoded:string | undefined):boolean { try { const v=JSON.parse(encoded ?? ''); return (v.version===2 || v.version===3) && v.profiles?.sourceOutlines===true } catch { return false } }
export function toonViewEnabled(encoded:string | undefined):boolean { try { const v=JSON.parse(encoded ?? ''); return (v.version===2 || v.version===3) && v.profiles?.toon===true } catch { return false } }
export function encodeToolViewAdmission(value:unknown, sessionID:string, route:Record<string,unknown> | undefined, model:{providerID:string;modelID:string}):string | undefined {
  if (!value || typeof value!=='object' || Array.isArray(value) || !route) return
  const v=value as Record<string,unknown>
  const expected=v.version===1 ? fields : v.version===2 ? [...fields.filter(k=>k!=='profile'),'profiles'] : v.version===3 ? [...fields.filter(k=>k!=='profile'),'profiles','lineCapRows'] : []
  const profiles=v.profiles as Record<string,unknown> | undefined
  if (!expected.length || Object.keys(v).length!==expected.length || expected.some(k=>!(k in v)) || v.requested!==true ||
      (v.version===1 ? v.profile!=='json-whitespace-v1' : !validProfiles(v.version,profiles) || (v.version===3 && !validCap(v.lineCapRows))) ||
      v.sessionID!==sessionID || v.rootOperationID!==route.rootOperationID || v.providerID!==route.providerID ||
      v.modelID!==route.modelID || v.connectionID!==route.connectionID || v.modelRouteID!==route.modelRouteID ||
      (model.providerID!==v.providerID && model.providerID!==v.connectionID) || model.modelID!==v.modelID ||
      !Number.isSafeInteger(v.generation) || (v.generation as number)<0 ||
      typeof v.settingsRevision!=='string' || !/^[a-f0-9]{64}$/.test(v.settingsRevision) ||
      ['sessionID','rootOperationID','hostAccountID','holderID','providerID','modelID','routeMethodKey','connectionID','modelRouteID'].some(k=>typeof v[k]!=='string' || !(v[k] as string) || (v[k] as string).length>256)) return
  return JSON.stringify(v)
}
export function enabledToolViewProfiles(encoded:string | undefined, tool:string):ToolViewAdmission['profile'][] {
  if (!encoded) return []
  try {
    const value=JSON.parse(encoded) as Original
    if (value.version===1) return value.profile!=='json-whitespace-v1' ? [] : tool==='read' ? ['read-json-whitespace-v1'] : (tool==='mcp_tool_search' || tool==='bash') ? [value.profile] : []
    if ((value.version!==2 && value.version!==3) || !value.profiles) return []
    if (tool==='bash') return [...(value.profiles.unifiedDiff ? ['unified-diff-v1' as const] : []),...(value.profiles.terminalAnsi ? ['terminal-ansi-v1' as const] : []),...(value.profiles.tap13 ? ['tap13-v1' as const] : []),...(value.profiles.unittestSuccess ? ['unittest-success-v1' as const] : []),...(value.profiles.cargoArtifacts ? ['cargo-artifacts-v1' as const] : []),...(value.profiles.untrackedStatus ? ['untracked-status-v1' as const] : []),...(value.profiles.ndjsonLogs ? ['ndjson-logs-v1' as const] : []),...(value.profiles.progressRedraw ? ['progress-redraw-v1' as const] : []),...(value.profiles.toon ? ['toon-v1' as const] : []),...(value.profiles.jsonShape ? ['json-shape-v1' as const] : []),...(value.profiles.jsonWhitespace ? ['json-whitespace-v1' as const] : []),...(value.profiles.base64Lines ? ['base64-lines-v1' as const] : []),...(value.profiles.repeatedLines ? ['repeated-lines-v1' as const] : [])]
    if (tool==='grep') return [...(value.version===3 && value.profiles.lineCap && validCap(value.lineCapRows) ? ['line-cap-v1' as const] : []),...(value.profiles.structuredSearch ? ['grep-rows-v1' as const] : [])]
    if (tool==='read') return [...(value.profiles.lockfileVersions ? ['npm-lockfile-versions-v1' as const] : []),...(value.profiles.jsonWhitespace ? ['read-json-whitespace-v1' as const] : []),...(value.profiles.repeatedLines ? ['read-repeated-lines-v1' as const] : []),...(value.profiles.readUnnumbered ? ['read-unnumbered-v1' as const] : [])]
    if (tool==='webfetch') return [...(value.profiles.htmlMarkdown ? ['html-markdown-v1' as const] : []),...(value.profiles.markdownSlim ? ['markdown-slim-v1' as const] : [])]
    if (tool==='mcp_tool_search') return value.profiles.structuredSearch ? ['structured-search-v1'] : value.profiles.jsonWhitespace ? ['json-whitespace-v1'] : []
  } catch { /* unsupported admission */ }
  return []
}
export function selectedToolViewProfile(encoded:string | undefined, tool:string, output?:string, metadata?:Record<string,unknown>, input?:unknown):ToolViewAdmission['profile'] | undefined {
  const enabled=enabledToolViewProfiles(encoded,tool)
  if (tool==='webfetch') {
    if (!input || typeof input!=='object' || Array.isArray(input)) return
    const format=(input as Record<string,unknown>).format
    return (format===undefined || format==='markdown') ? (enabled.includes('markdown-slim-v1') ? 'markdown-slim-v1' : undefined) : format==='html' && enabled.includes('html-markdown-v1') ? 'html-markdown-v1' : undefined
  }
  if (tool==='read') return typeof output==='string' ? selectReadView({output,metadata},enabled) : undefined
  if (tool!=='bash') return enabled[0]
  if (typeof output!=='string') return
  return selectBashView(output,{diff:enabled.includes('unified-diff-v1'),ansi:enabled.includes('terminal-ansi-v1'),repetition:enabled.includes('repeated-lines-v1'),tap13:enabled.includes('tap13-v1'),unittest:enabled.includes('unittest-success-v1'),cargo:enabled.includes('cargo-artifacts-v1'),untracked:enabled.includes('untracked-status-v1'),base64:enabled.includes('base64-lines-v1'),ndjson:enabled.includes('ndjson-logs-v1'),json:enabled.includes('json-whitespace-v1'),toon:enabled.includes('toon-v1'),shape:enabled.includes('json-shape-v1'),progress:enabled.includes('progress-redraw-v1')})
}
export type ToolViewCapture = {admission:ToolViewAdmission; timing:ToolTimingCapture}
export function toolViewScope(encoded:string | undefined, identity:{sessionID:string;rootOperationID?:string;actorID:string;innerTurnID?:string}, issuer:{clockID:string;isAlive():boolean} | undefined,
  route:{rootOperationID:string;connectionID:string;providerID:string;modelID:string;modelRouteID?:string} | undefined, part:MessageV2.ToolPart):ToolViewAdmission | undefined {
  if (!encoded || !issuer?.isAlive() || !route || !identity.rootOperationID || !identity.innerTurnID) return
  try {
    const v=JSON.parse(encoded) as Original
    const profile=outlineViewEnabled(encoded) && authenticatedOutline(part)!==undefined ? 'source-outline-v1' : toonViewEnabled(encoded) && validGcfReceipt(part) ? 'toon-v1' : selectedToolViewProfile(encoded,part.tool,part.state?.status==='completed' ? part.state.output : undefined,part.state?.status==='completed' ? part.state.metadata : undefined,part.state.input)
    if (v.requested!==true || !profile || v.sessionID!==identity.sessionID ||
        v.rootOperationID!==identity.rootOperationID || v.rootOperationID!==route.rootOperationID ||
        v.connectionID!==route.connectionID || v.providerID!==route.providerID || v.modelID!==route.modelID || v.modelRouteID!==route.modelRouteID) return
    const {version:_version,requested:_requested,profile:_profile,profiles:_profiles,lineCapRows,...base}=v
    if (profile==='line-cap-v1' && !validCap(lineCapRows)) return
    return {...base,...(profile==='line-cap-v1' ? {lineCapRows} : {}),profile,on:true,clockID:issuer.clockID,messageID:part.messageID,partID:part.id,actorID:identity.actorID,innerTurnID:identity.innerTurnID}
  } catch { return }
}
function wellFormed(value:string):boolean {
  for (let i=0;i<value.length;i++) {
    const c=value.charCodeAt(i)
    if (c>=0xd800 && c<=0xdbff) {const next=value.charCodeAt(++i);if (!(next>=0xdc00 && next<=0xdfff)) return false}
    else if (c>=0xdc00 && c<=0xdfff) return false
  }
  return true
}
export function captureToolView(encoded:string | undefined,timing:ToolTimingCapture | undefined,part:MessageV2.ToolPart,route:{rootOperationID:string;connectionID:string;providerID:string;modelID:string;modelRouteID?:string} | undefined, verified?:ToolViewAdmission['profile']):ToolViewCapture | undefined {
  if (!encoded || !timing || !timing.owner.isAlive() || !route || part.state.status!=='completed' ||
      part.metadata?.providerExecuted || part.state.metadata?.truncated!==false || part.state.metadata?.outputPath!==undefined ||
      part.state.attachments?.length || part.state.providerOutput!==undefined || part.contextSelection?.action==='trim' || part.contextSelection?.action==='omit') return
  try {
    const v=JSON.parse(encoded) as Original, id=timing.identity
    const profile=verified==='source-outline-v1' && outlineViewEnabled(encoded) ? 'source-outline-v1' : verified==='toon-v1' && toonViewEnabled(encoded) ? 'toon-v1' : selectedToolViewProfile(encoded,part.tool,part.state.output,part.state.metadata,part.state.input)
    if (v.requested!==true || !profile || ((profile!=='json-whitespace-v1' || part.tool!=='mcp_tool_search') && profile!==verified) || v.sessionID!==id.sessionID || v.rootOperationID!==id.rootOperationID ||
        v.rootOperationID!==route.rootOperationID || v.connectionID!==route.connectionID || v.providerID!==route.providerID ||
        v.modelID!==route.modelID || v.modelRouteID!==route.modelRouteID ||
        timing.partID!==part.id || timing.messageID!==part.messageID || timing.callID!==part.callID || part.sessionID!==id.sessionID ||
        !wellFormed(part.state.output) || Buffer.byteLength(part.state.output,'utf8')>51200 || Buffer.byteLength(JSON.stringify(part),'utf8')>262144) return
    const {version:_version,requested:_requested,profile:_profile,profiles:_profiles,lineCapRows,...base}=v
    if (profile==='line-cap-v1' && !validCap(lineCapRows)) return
    return {timing,admission:{...base,...(profile==='line-cap-v1' ? {lineCapRows} : {}),profile,on:true,clockID:timing.owner.clockID,messageID:part.messageID,partID:part.id,actorID:id.actorID,innerTurnID:id.innerTurnID}}
  } catch { return }
}
const pending=new WeakMap<object,ToolViewCapture>()
/** Only the exact final clone has a synchronous projector sidecar. */
export function emitToolView(capture:ToolViewCapture | undefined,part:MessageV2.ToolPart,publish:()=>void) {
  if (!capture) return publish()
  pending.set(part,capture)
  try { publish() } finally { pending.delete(part) }
}
export function currentToolView(part:MessageV2.Part):ToolViewCapture | undefined { return pending.get(part) }
