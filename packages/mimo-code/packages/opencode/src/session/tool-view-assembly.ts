/** On-only T04 preparation; views are ephemeral and never replace canonical parts. */
import { createHash } from 'node:crypto'
import type { MessageV2 } from './message-v2'
import type { HistorySourceAck, HistorySourceRef } from '@/acp/openclank-protocol'
import { inspectToolView, freezeToolView, type ToolViewAdmission } from './tool-view-decision'
import { toolViewScope, toonViewEnabled, outlineViewEnabled } from './tool-view-source'
import { validGcfReceipt, authenticatedOutline } from './gcf-source-proof'
import { sourceOutlineFrame } from './tool-view-source-outline'
import { encodeToonSubset } from '../util/toon-subset'
import { compactJsonTokens } from './tool-view-json'
import { jsonShape } from './tool-view-json-shape'
import { progressRedraw } from './tool-view-progress-redraw'
import { markdownSlim } from './tool-view-markdown-slim'
import { framedJsonView } from './tool-view-json'
import { framedBashJsonView } from './tool-view-json-lines'
import { stripTerminalSgr } from './tool-view-terminal-ansi'
import { framedHtmlView } from '../tool/webfetch-view'
import { framedStructuredSearch } from './tool-view-structured-search'
import { framedRepeatedLines } from './tool-view-repeated-lines'
import { elideBase64Lines } from './tool-view-base64'
import { framedGrepRows, framedGrepLineCap } from './tool-view-grep'
import { framedReadView, framedLockfileView, framedConfigReadView } from './tool-view-read'
import { compactNdjsonLogs } from './tool-view-ndjson'
import { summarizeTap13 } from './tool-view-tap13'
import { summarizeUnittest } from './tool-view-unittest'
import { summarizeCargoArtifacts } from './tool-view-cargo-artifacts'
import { summarizeUntrackedStatus } from './tool-view-untracked-status'
import { summarizeUnifiedDiff } from './tool-view-unified-diff'
import type { ActorExecutionIdentity } from '@/actor/turn'
import * as ManagedProvider from '@/acp/managed-provider'
import { admittedToolViewControl } from './tool-timing'
import type { ModelMessage } from 'ai'
import type { ContextPrefaceEvidence } from './context-inspection'

type Identity={sessionID:string;rootOperationID?:string;actorID:string;innerTurnID?:string}
type Route={rootOperationID:string;connectionID:string;providerID:string;modelID:string;modelRouteID?:string}
type Control={disabled:boolean;reason?:string}
/** The pointer is the admitted turn itself, never a reconstructed model field. */
export type ToolViewContext={identity:ActorExecutionIdentity;encoded:string;issuer:NonNullable<ReturnType<typeof ManagedProvider.currentActorToolTimingIssuer>>;route:NonNullable<ReturnType<typeof ManagedProvider.currentToolViewRoute>>;control:Control;historyAllowed:boolean}
export function currentToolViewContext(identity:ActorExecutionIdentity | undefined,historyAllowed:boolean):ToolViewContext | undefined {
  if (!identity?.rootOperationID || !identity.innerTurnID) return
  const host=ManagedProvider.currentActorHostContext(identity.sessionID,identity.actorID,identity.executionRevision)
  const encoded=host?.toolViewAdmission ?? host?.jsonWhitespaceAdmission
  if (!encoded) return
  const control=admittedToolViewControl(identity,true)
  const issuer=ManagedProvider.currentActorToolTimingIssuer(identity.sessionID,identity.actorID,identity.executionRevision,identity.rootOperationID)
  const route=ManagedProvider.currentToolViewRoute(identity.sessionID,identity.actorID,identity.executionRevision)
  if (!control || !issuer?.isAlive() || !route || route.rootOperationID!==identity.rootOperationID) return
  return {identity,encoded,issuer,route,control,historyAllowed}
}
export function toolViewContextCurrent(context:ToolViewContext):boolean {
  const now=currentToolViewContext(context.identity,context.historyAllowed)
  return !!now && !now.control.disabled && now.control===context.control && now.issuer===context.issuer && now.encoded===context.encoded &&
    now.route.rootOperationID===context.route.rootOperationID && now.route.connectionID===context.route.connectionID &&
    now.route.providerID===context.route.providerID && now.route.modelID===context.route.modelID && now.route.modelRouteID===context.route.modelRouteID
}
type Item={part:MessageV2.ToolPart;scope:ToolViewAdmission;ref:HistorySourceRef;frame:string;prior:'candidate'|'view'}
const sha=(s:string)=>createHash('sha256').update(s,'utf8').digest('hex')
/** Preserve the separately attested preface while rebuilding only tool output. */
export function restoreContextPreface(messages:ModelMessage[], preface:ContextPrefaceEvidence | undefined):{messages:ModelMessage[];preface?:ContextPrefaceEvidence;reason?:string} {
  const restored=[...messages]
  if (!preface) return {messages:restored}
  const position=restored.findLastIndex(message=>message.role==='user')
  if (position<0 || sha(preface.text)!==preface.textSHA256) return {messages:restored,reason:'context_preface_unavailable'}
  const part={type:'text' as const,text:preface.text}
  Object.defineProperty(part,'id',{value:preface.contextPartID,enumerable:false})
  restored.splice(position,0,{role:'user',content:[part]})
  return {messages:restored,preface:{...preface,index:position,count:restored.length}}
}
function disable(control:Control,reason:string) {control.disabled=true;control.reason ??= reason}
function freezeRaw(scope:ToolViewAdmission,control:Control):boolean {
  const value=freezeToolView(scope,{state:'raw'})
  if (value?.state==='raw') return true
  disable(control,value ? 'decision_conflict' : 'decision_unavailable');return false
}

export async function prepareToolViews(input:{encoded?:string;identity:Identity;issuer?:{clockID:string;isAlive():boolean};route?:Route;control?:Control;
  historyAllowed:boolean;messages:MessageV2.WithParts[];
  ack:(scope:ToolViewAdmission,contentSHA256:string)=>Promise<HistorySourceAck | undefined>;
  check:(refs:HistorySourceRef[])=>Promise<boolean>;current:()=>boolean}):Promise<{views:Map<string,string>;reason?:string}> {
  const rawToon=new Map<string,string>()
  if (toonViewEnabled(input.encoded) || outlineViewEnabled(input.encoded)) for (const message of input.messages) for (const part of message.parts)
    if (part.type==='tool' && part.state.status==='completed' && ((toonViewEnabled(input.encoded) && validGcfReceipt(part)) || (outlineViewEnabled(input.encoded) && authenticatedOutline(part)!==undefined))) rawToon.set(part.id,part.state.output)
  const views=new Map(rawToon), control=input.control
  if (!input.encoded || !control) return {views}
  if (control.disabled) return {views,reason:control.reason ?? 'turn_disabled'}
  const current=()=>{if(input.current()) return true;disable(control,'scope_unavailable');return false}
  if (!current()) return {views,reason:control.reason}
  const pending:Item[]=[]
  let reanchor:string | undefined
  try {
    for (const message of input.messages) for (const part of message.parts) {
      if (part.type!=='tool' || part.state.status!=='completed' ||
          part.metadata?.providerExecuted || part.state.providerOutput!==undefined || part.state.time.compacted ||
          part.contextSelection?.action==='trim' || part.contextSelection?.action==='omit' || part.state.attachments?.length) continue
      const scope=toolViewScope(input.encoded,input.identity,input.issuer,input.route,part)
      if (!scope) continue
      const inspected=inspectToolView(scope)
      if (inspected.state==='missing') continue
      if (inspected.state==='out_of_scope') {
        if (inspected.priorState==='view') reanchor='prior_view_admission_changed'
        continue
      }
      if (inspected.state==='unavailable') {disable(control,'decision_unavailable');return {views:new Map(rawToon),reason:control.reason}}
      const decision=inspected.decision
      if (decision.state==='raw') continue
      if (!input.historyAllowed) {
        if (decision.state==='candidate' && !freezeRaw(scope,control)) disable(control,'decision_unavailable')
        else if (decision.state==='view') disable(control,'recovery_tool_unavailable')
        if (control.disabled) return {views:new Map(rawToon),reason:control.reason}
        continue
      }
      const ack=decision.state==='candidate' ? await input.ack(scope,decision.contentSHA256) : undefined
      if (!current()) return {views:new Map(rawToon),reason:control.reason}
      if (decision.state==='candidate' && (!ack || ack.accountID!==scope.hostAccountID || ack.actorID!==scope.actorID ||
          ack.sessionID!==scope.sessionID || ack.messageID!==scope.messageID || ack.partID!==scope.partID || ack.contentHash!==decision.contentSHA256)) {
        if (!freezeRaw(scope,control)) {disable(control,'decision_unavailable');return {views:new Map(rawToon),reason:control.reason}}
        continue
      }
      const ref:HistorySourceRef={messageID:part.messageID,partID:part.id,
        revision:decision.state==='view' ? decision.sourceRevision : ack!.revision,
        contentHash:decision.state==='view' ? decision.sourceHash : ack!.contentHash,
        outputSHA256:sha(part.state.output),outputBytes:Buffer.byteLength(part.state.output,'utf8')}
      if (ack && (ack.outputSHA256!==ref.outputSHA256 || ack.outputBytes!==ref.outputBytes)) {
        if (!freezeRaw(scope,control)) {disable(control,'decision_unavailable');return {views:new Map(rawToon),reason:control.reason}}
        continue
      }
      const invocation=JSON.stringify({operation:'exact_output',message_id:ref.messageID,part_id:ref.partID,revision:ref.revision,content_hash:ref.contentHash,byte_offset:0,byte_limit:8192})
      const diff=scope.profile==='unified-diff-v1' ? summarizeUnifiedDiff(part.state.output) : undefined
      const diffFrame=diff===undefined ? undefined : `[Review summary; unchanged context omitted; this is not an applicable patch. Original: history(${invocation}); follow nextOffset for further pages.]\n${diff}`
      const tap=scope.profile==='tap13-v1' ? summarizeTap13(part.state.output) : undefined
      const tapFrame=tap===undefined ? undefined : `[TAP review summary; ${tap.omitted} successful assertions omitted; not a replayable report. Original: history(${invocation}); follow nextOffset for further pages.]\n${tap.body}`
      const unittest=scope.profile==='unittest-success-v1' ? summarizeUnittest(part.state.output) : undefined
      const unittestFrame=unittest===undefined ? undefined : `[Unittest success summary; ${unittest.omitted} progress dots omitted; not a complete report. Original: history(${invocation}); follow nextOffset for further pages.]\n${unittest.body}`
      const cargo=scope.profile==='cargo-artifacts-v1' ? summarizeCargoArtifacts(part.state.output) : undefined
      const cargoFrame=cargo===undefined ? undefined : `[Cargo artifact summary; manifest/source paths, crate metadata, profiles and features omitted from ${cargo.omitted} artifact records; JSON spelling normalized. Original: history(${invocation}); follow nextOffset for further pages.]\n${cargo.body}`
      const untracked=scope.profile==='untracked-status-v1' ? summarizeUntrackedStatus(part.state.output) : undefined
      const untrackedFrame=untracked===undefined ? undefined : `[Untracked-status-shaped text; ?? applies to each of ${untracked.count} ordered quoted paths. This is observed text, not verified command or repository state; directory entries do not enumerate contents. Original: history(${invocation}); follow nextOffset for further pages.]\n${untracked.body}`
      const ndjson=scope.profile==='ndjson-logs-v1' ? compactNdjsonLogs(part.state.output) : undefined
      const ndjsonFrame=ndjson===undefined ? undefined : `[NDJSON semantic view; JSON lexical spelling and whitespace normalized. Original: history(${invocation}); follow nextOffset for further pages.]\n${ndjson}`
      const progress=scope.profile==='progress-redraw-v1' ? progressRedraw(part.state.output) : undefined
      const progressFrame=progress ? `[Progress view; intermediate redraw content omitted (${progress.redrawEvents} redraws, ${progress.overwrittenCharacters} overwritten characters); not a complete log. Original: history(${invocation}); follow nextOffset for further pages.]\n${progress.text}` : undefined
      const markdown=scope.profile==='markdown-slim-v1' ? markdownSlim(part.state.output) : undefined
      const markdownFrame=markdown ? `[Markdown view; ${markdown.removedCommentLines} comment lines and ${markdown.removedBlankLines} blank lines omitted; not a complete representation. Original tool output: history(${invocation}); follow nextOffset for further pages.]\n${markdown.text}` : undefined
      const base64=scope.profile==='base64-lines-v1' ? elideBase64Lines(part.state.output) : undefined
      const base64Frame=base64===undefined ? undefined : `[Lossy view; ${base64.omitted} base64-shaped lines omitted. Original: history(${invocation}); follow nextOffset for further pages.]\n${base64.body}`
      const stripped=scope.profile==='terminal-ansi-v1' ? stripTerminalSgr(part.state.output) : undefined
      const terminalFrame=stripped===undefined ? undefined : `[Terminal view; SGR styling removed. Original: history(${invocation}); follow nextOffset for further pages.]\n${stripped}`
      const shape=scope.profile==='json-shape-v1' ? jsonShape(part.state.output) : undefined
      const shapeFrame=shape ? `[JSON structure view; ${shape.omittedScalars} scalar values omitted and keys remain visible; not redaction or faithful serialization. Original: history(${invocation}); follow nextOffset for further pages.]\n${shape.text}` : undefined
      const toon = scope.profile==='toon-v1' ? encodeToonSubset(part.state.output) : undefined
      const compact = toon ? compactJsonTokens(part.state.output) : undefined
      const toonFrame=toon ? `[Restricted canonical TOON 4.4; source JSON remains in history(${invocation}); follow nextOffset for further pages.]\n${toon.text}` : undefined
      const outlineText=scope.profile==='source-outline-v1' ? authenticatedOutline(part) : undefined
      const outlineFrame=outlineText===undefined ? undefined : sourceOutlineFrame(part.state.output,JSON.parse(outlineText),part.callID,ref)
      const frame=scope.profile==='source-outline-v1' ? outlineFrame : scope.profile==='toon-v1' ? (toonFrame && compact && Buffer.byteLength(toonFrame,'utf8')<Buffer.byteLength(compact,'utf8') ? toonFrame : undefined)
        : scope.profile==='json-shape-v1' ? (shapeFrame && Buffer.byteLength(shapeFrame,'utf8')<Buffer.byteLength(part.state.output,'utf8') ? shapeFrame : undefined)
        : scope.profile==='json-whitespace-v1' ? (part.tool==='bash' ? framedBashJsonView(part.state.output,ref) : framedJsonView(part.state.output,ref))
        : scope.profile==='markdown-slim-v1' ? (markdownFrame && Buffer.byteLength(markdownFrame,'utf8')<Buffer.byteLength(part.state.output,'utf8') ? markdownFrame : undefined)
        : scope.profile==='html-markdown-v1' ? framedHtmlView(part.state.output,ref)
        : scope.profile==='line-cap-v1' && scope.lineCapRows!==undefined ? framedGrepLineCap(part.state.output,part.state.metadata,ref,scope.lineCapRows)
        : scope.profile==='grep-rows-v1' ? framedGrepRows(part.state.output,part.state.metadata,ref)
        : scope.profile==='npm-lockfile-versions-v1' ? framedLockfileView(part.state.output,part.state.metadata,ref)
        : scope.profile==='read-json-whitespace-v1' || scope.profile==='read-repeated-lines-v1' ? framedConfigReadView(part.state.output,part.state.metadata,ref,scope.profile)
        : scope.profile==='read-unnumbered-v1' ? framedReadView(part.state.output,part.state.metadata,ref)
        : scope.profile==='structured-search-v1' ? framedStructuredSearch(part.state.output,ref)
        : scope.profile==='unittest-success-v1' ? (unittestFrame && Buffer.byteLength(unittestFrame,'utf8')<Buffer.byteLength(part.state.output,'utf8') ? unittestFrame : undefined)
        : scope.profile==='cargo-artifacts-v1' ? (cargoFrame && Buffer.byteLength(cargoFrame,'utf8')<Buffer.byteLength(part.state.output,'utf8') ? cargoFrame : undefined)
        : scope.profile==='untracked-status-v1' ? (untrackedFrame && Buffer.byteLength(untrackedFrame,'utf8')<Buffer.byteLength(part.state.output,'utf8') ? untrackedFrame : undefined)
        : scope.profile==='ndjson-logs-v1' ? (ndjsonFrame && Buffer.byteLength(ndjsonFrame,'utf8')<Buffer.byteLength(part.state.output,'utf8') ? ndjsonFrame : undefined)
        : scope.profile==='progress-redraw-v1' ? (progressFrame && Buffer.byteLength(progressFrame,'utf8')<Buffer.byteLength(part.state.output,'utf8') ? progressFrame : undefined)
        : scope.profile==='base64-lines-v1' ? (base64Frame && Buffer.byteLength(base64Frame,'utf8')<Buffer.byteLength(part.state.output,'utf8') ? base64Frame : undefined)
        : scope.profile==='tap13-v1' ? (tapFrame && Buffer.byteLength(tapFrame,'utf8')<Buffer.byteLength(part.state.output,'utf8') ? tapFrame : undefined)
        : scope.profile==='repeated-lines-v1' ? framedRepeatedLines(part.state.output,ref)
        : scope.profile==='unified-diff-v1' ? (diffFrame && Buffer.byteLength(diffFrame,'utf8')<Buffer.byteLength(part.state.output,'utf8') ? diffFrame : undefined)
        : terminalFrame && Buffer.byteLength(terminalFrame,'utf8')<Buffer.byteLength(part.state.output,'utf8') ? terminalFrame : undefined
      if (!frame || (decision.state==='view' && sha(frame)!==decision.viewSHA256) || pending.length>=16) {
        if (decision.state==='view') {disable(control,'view_unavailable');return {views:new Map(rawToon),reason:control.reason}}
        if (!freezeRaw(scope,control)) {disable(control,'decision_unavailable');return {views:new Map(rawToon),reason:control.reason}}
        continue
      }
      pending.push({part,scope,ref,frame,prior:decision.state})
    }
    if (!pending.length) return {views,reason:reanchor}
    const ready=await input.check(pending.map(x=>x.ref))
    if (!current()) return {views:new Map(rawToon),reason:control.reason}
    if (!ready) {
      for (const item of pending) {
        if (item.prior==='view') {disable(control,'source_unavailable');break}
        if (!freezeRaw(item.scope,control)) {disable(control,'decision_unavailable');break}
      }
      return {views,reason:control.reason ?? reanchor}
    }
    for (const item of pending) {
      if (!current()) return {views:new Map(rawToon),reason:control.reason}
      const frozen=freezeToolView(item.scope,{state:'view',sourceRevision:item.ref.revision,sourceHash:item.ref.contentHash,viewSHA256:sha(item.frame)})
      if (!frozen) {disable(control,'decision_unavailable');return {views:new Map(rawToon),reason:control.reason}}
      if (frozen.state==='view' && frozen.sourceRevision===item.ref.revision && frozen.sourceHash===item.ref.contentHash && frozen.viewSHA256===sha(item.frame)) views.set(item.part.id,item.frame)
      else if (frozen.state!=='raw') {disable(control,'decision_conflict');return {views:new Map(rawToon),reason:control.reason}}
    }
    return {views,reason:reanchor}
  } catch {disable(control,'view_unavailable');return {views:new Map(rawToon),reason:control.reason}}
}
