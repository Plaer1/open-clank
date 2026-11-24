/** Local descriptor only: MessageV2 and History retain the original and authority. */
import { createHash } from 'node:crypto'
import * as MessageV2 from './message-v2'
import { SessionID, MessageID } from './schema'
import { sourcePartStructuralDigest } from '../history/source-part-equivalence'
import { validGcfReceipt } from './gcf-source-proof'
import type { HistorySourceAck, HistorySourceRef } from '@/acp/openclank-protocol'

const MAX_ENTRIES=64, MAX_METADATA=4096, TTL=15*60*1000
const digest=(value:string)=>createHash('sha256').update(value,'utf8').digest('hex')
const id=(value:unknown):value is string=>typeof value==='string' && value.length>0 && Buffer.byteLength(value,'utf8')<=256 && !/[\x00-\x1f\x7f]/.test(value)
type Scope={accountID:string;sessionID:string;actorID:string;executionRevision:number;rootOperationID:string;admission:string;artifactID:string}
type Descriptor={scope:Scope;messageID:string;partID:string;callID:string;structuralDigest:string;outputSHA256:string;outputBytes:number;source:HistorySourceRef;expiresAt:number}
type Context={scope:Scope;current:()=>boolean;sourceAck:(actor:string,part:MessageV2.ToolPart)=>Promise<HistorySourceAck|undefined>;checkSources:(sources:HistorySourceRef[])=>Promise<boolean>}
type Checked={part:MessageV2.ToolPart;source:HistorySourceRef}
const entries=new Map<string,Descriptor>()
// Invalidates in-flight opens when a lifecycle clear or explicit reset wins.
let invalidationEpoch=0
const key=(scope:Scope)=>JSON.stringify([scope.accountID,scope.sessionID,scope.actorID,scope.executionRevision,scope.rootOperationID,scope.admission,scope.artifactID])
function validScope(scope:Scope):boolean {return !!scope && [scope.accountID,scope.sessionID,scope.actorID,scope.rootOperationID,scope.admission,scope.artifactID].every(id) && Number.isSafeInteger(scope.executionRevision) && scope.executionRevision>=0}
function live(ctx:Context):boolean {try{return validScope(ctx.scope) && ctx.current()===true}catch{return false}}
function read(ctx:Context,messageID:string,partID:string):MessageV2.ToolPart|undefined {
  try {if(!live(ctx)||!id(messageID)||!id(partID))return
    const message=MessageV2.get({sessionID:SessionID.make(ctx.scope.sessionID),messageID:MessageID.make(messageID)})
    const matches=message.parts.filter(part=>part.id===partID)
    if(matches.length!==1)return
    const part=matches[0]!
    if(part.type!=='tool'||part.sessionID!==ctx.scope.sessionID||part.messageID!==messageID||part.id!==partID||!validGcfReceipt(part)||part.state.status!=='completed')return
    return part
  } catch{return}
}
async function check(ctx:Context,messageID:string,partID:string,expected?:Descriptor):Promise<Checked|undefined> {
  try {
    const part=read(ctx,messageID,partID);if(!part||part.state.status!=='completed'||!id(part.callID)||typeof part.state.output!=='string')return
    const output=part.state.output, outputSHA256=digest(output), outputBytes=Buffer.byteLength(output,'utf8'), structuralDigest=sourcePartStructuralDigest(part)
    if(!structuralDigest||outputBytes<100||outputBytes>51200)return
    const ack=await ctx.sourceAck(ctx.scope.actorID,part)
    if(!live(ctx)||!ack||ack.version!==1||ack.accountID!==ctx.scope.accountID||ack.actorID!==ctx.scope.actorID||ack.sessionID!==ctx.scope.sessionID||ack.messageID!==messageID||ack.partID!==partID||ack.outputSHA256!==outputSHA256||ack.outputBytes!==outputBytes||!Number.isSafeInteger(ack.revision)||ack.revision<1||typeof ack.contentHash!=='string'||!/^[a-f0-9]{64}$/.test(ack.contentHash))return
    const source:HistorySourceRef={messageID:ack.messageID,partID:ack.partID,revision:ack.revision,contentHash:ack.contentHash,outputSHA256:ack.outputSHA256,outputBytes:ack.outputBytes}
    if(expected && (expected.expiresAt<=Date.now()||expected.callID!==part.callID||expected.structuralDigest!==structuralDigest||expected.outputSHA256!==outputSHA256||expected.outputBytes!==outputBytes||JSON.stringify(expected.source)!==JSON.stringify(source)))return
    if(await ctx.checkSources([source])!==true||!live(ctx))return
    const again=read(ctx,messageID,partID)
    if(!again||again.state.status!=='completed'||again.callID!==part.callID||sourcePartStructuralDigest(again)!==structuralDigest||digest(again.state.output)!==outputSHA256)return
    const final=await ctx.sourceAck(ctx.scope.actorID,again)
    if(!live(ctx)||!final||final.version!==1||final.accountID!==ctx.scope.accountID||final.actorID!==ctx.scope.actorID||final.sessionID!==ctx.scope.sessionID||JSON.stringify({messageID:final.messageID,partID:final.partID,revision:final.revision,contentHash:final.contentHash,outputSHA256:final.outputSHA256,outputBytes:final.outputBytes})!==JSON.stringify(source)||await ctx.checkSources([source])!==true||!live(ctx))return
    return {part:again,source}
  }catch{return}
}
export function clearGcfSessionViews(sessionID:string, actorID?:string):void {invalidationEpoch++;for(const [k,v] of entries)if(v.scope.sessionID===sessionID && (!actorID||v.scope.actorID===actorID))entries.delete(k)}
export async function openGcfSessionView(ctx:Context,messageID:string,partID:string):Promise<{state:'opened'|'unavailable'}> {
  const epoch=invalidationEpoch
  const checked=await check(ctx,messageID,partID);if(epoch!==invalidationEpoch||!checked||!live(ctx)||checked.part.state.status!=='completed')return {state:'unavailable'}
  for(const [k,v] of entries)if(v.expiresAt<=Date.now())entries.delete(k)
  const k=key(ctx.scope);if(!entries.has(k)&&entries.size>=MAX_ENTRIES)return {state:'unavailable'}
  const output=checked.part.state.output,structuralDigest=sourcePartStructuralDigest(checked.part)
  if(!structuralDigest)return {state:'unavailable'}
  const entry:Descriptor={scope:{...ctx.scope},messageID,partID,callID:checked.part.callID,structuralDigest,outputSHA256:digest(output),outputBytes:Buffer.byteLength(output,'utf8'),source:checked.source,expiresAt:Date.now()+TTL}
  if(Buffer.byteLength(JSON.stringify(entry),'utf8')>MAX_METADATA||epoch!==invalidationEpoch||!live(ctx))return {state:'unavailable'}
  entries.set(k,entry);return {state:'opened'}
}
export async function inspectGcfSessionView(ctx:Context):Promise<{state:'reanchor';original:string;source:HistorySourceRef}|{state:'unavailable'}> {
  if(!live(ctx))return {state:'unavailable'}
  const k=key(ctx.scope),entry=entries.get(k);if(!entry)return {state:'unavailable'}
  const checked=await check(ctx,entry.messageID,entry.partID,entry)
  if(!checked||entries.get(k)!==entry||!live(ctx)||checked.part.state.status!=='completed'){if(entries.get(k)===entry)entries.delete(k);return {state:'unavailable'}}
  return {state:'reanchor',original:checked.part.state.output,source:checked.source}
}
export function resetGcfSessionView(ctx:Context):{state:'cleared'|'unavailable'} {if(!live(ctx))return {state:'unavailable'};invalidationEpoch++;entries.delete(key(ctx.scope));return {state:'cleared'}}
