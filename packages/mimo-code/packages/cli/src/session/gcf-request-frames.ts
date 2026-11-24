import { sourcePartStructuralDigest } from '../history/source-part-equivalence'
/** Ephemeral evidence for actual emitted full GCF or original frames; never a model grant. */
import {createHash} from 'node:crypto'
import * as MessageV2 from './message-v2'
import {validGcfReceipt} from './gcf-source-proof'
import type {GcfArtifact} from './gcf-native'
import type {HistorySourceAck,HistorySourceRef} from '@/acp/openclank-protocol'

type Identity={sessionID:string;rootOperationID:string;actorID:string;executionRevision:number;innerTurnID:string}
type Frame={part:MessageV2.ToolPart;structuralDigest:string;outputHash:string;outputBytes:number;frame:string;frameHash:string}
type Context={identity:Identity;accountID:string;artifact:GcfArtifact;artifactID:string;current:()=>boolean;
  sourceAck:(actorID:string,part:MessageV2.ToolPart)=>Promise<HistorySourceAck|undefined>;
  checkSources:(refs:HistorySourceRef[])=>Promise<boolean>;frames:Frame[];sealed:boolean;invalid:boolean}
export type GcfRequestFramesHandle=object & {readonly __gcfRequestFrames?:never}
export type GcfFrameRecorder=(part:MessageV2.ToolPart,frame:string)=>void
const owners=new WeakMap<object,Context>()
const sha=(s:string)=>createHash('sha256').update(s,'utf8').digest('hex')
const live=(ctx:Context)=>{try{return ctx.current()===true && ctx.artifact.identity()===ctx.artifactID && ctx.artifact.availability()==='available'}catch{return false}}
function exact(frame:Frame):boolean {
  try {
    const original=frame.part, message=MessageV2.get({sessionID:original.sessionID,messageID:original.messageID})
    const matches=message.parts.filter(part=>part.id===original.id)
    if(matches.length!==1)return false
    const part=matches[0]!
    return part.type==='tool' && part.sessionID===original.sessionID && part.messageID===original.messageID && part.id===original.id &&
      part.callID===original.callID && part.tool===original.tool && validGcfReceipt(part) && part.state.status==='completed' &&
      typeof part.state.output==='string' && sha(part.state.output)===frame.outputHash && Buffer.byteLength(part.state.output,'utf8')===frame.outputBytes &&
      sourcePartStructuralDigest(part)===frame.structuralDigest && sha(frame.frame)===frame.frameHash
  } catch{return false}
}
export function createGcfRequestFrames(input:{identity:Identity;accountID:string;artifact:GcfArtifact;current:()=>boolean;
  sourceAck:Context['sourceAck'];checkSources:Context['checkSources']}):{handle:GcfRequestFramesHandle;record:GcfFrameRecorder;seal:()=>void} {
  const artifactID=input.artifact?.identity()
  if(!artifactID || typeof input.current!=='function' || typeof input.sourceAck!=='function' || typeof input.checkSources!=='function' ||
      typeof input.accountID!=='string' || !input.accountID || !input.identity?.sessionID || !input.identity.rootOperationID || !input.identity.actorID ||
      !input.identity.innerTurnID || !Number.isSafeInteger(input.identity.executionRevision) || input.identity.executionRevision<0) throw new Error('frame source unavailable')
  const handle=Object.freeze({}) as GcfRequestFramesHandle
  const ctx:Context={...input,identity:{...input.identity},artifactID,frames:[],sealed:false,invalid:false};owners.set(handle,ctx)
  const record:GcfFrameRecorder=(part,frame)=>{
    if(ctx.sealed || ctx.invalid)return
    try {
      if(!live(ctx) || !validGcfReceipt(part) || part.sessionID!==ctx.identity.sessionID || part.state.status!=='completed' ||
          typeof frame!=='string' || Buffer.byteLength(frame,'utf8')>51200 ||
          ctx.frames.length>=32 || ctx.frames.some(item=>item.part.callID===part.callID || item.part.id===part.id)) {ctx.invalid=true;return}
      const source=part.state.output
      const structuralDigest=sourcePartStructuralDigest(part);if(!structuralDigest){ctx.invalid=true;return}
      ctx.frames.push({part:structuredClone(part),structuralDigest,outputHash:sha(source),outputBytes:Buffer.byteLength(source,'utf8'),frame,frameHash:sha(frame)})
    } catch {ctx.invalid=true}
  }
  return {handle,record,seal:()=>{ctx.sealed=true}}
}
export async function verifyGcfRequestFrames(handle:GcfRequestFramesHandle,expectedArtifact:GcfArtifact):Promise<readonly {callID:string;frame:string;sourceJSON:string}[]|undefined> {
  const ctx=owners.get(handle)
  if(!ctx || !ctx.sealed || ctx.invalid || ctx.frames.length<2 || expectedArtifact!==ctx.artifact || !live(ctx))return
  try {
    const refs:HistorySourceRef[]=[]
    for(const frame of ctx.frames){
      if(!live(ctx) || !exact(frame))return
      const part=frame.part,ack=await ctx.sourceAck(ctx.identity.actorID,part)
      if(!live(ctx) || !ack || ack.version!==1 || ack.accountID!==ctx.accountID || ack.actorID!==ctx.identity.actorID ||
          ack.sessionID!==part.sessionID || ack.messageID!==part.messageID || ack.partID!==part.id || !/^[a-f0-9]{64}$/.test(ack.contentHash) ||
          ack.outputSHA256!==frame.outputHash || ack.outputBytes!==frame.outputBytes || !Number.isSafeInteger(ack.revision) || ack.revision<1)return
      refs.push({messageID:ack.messageID,partID:ack.partID,revision:ack.revision,contentHash:ack.contentHash,outputSHA256:ack.outputSHA256,outputBytes:ack.outputBytes})
    }
    if(!live(ctx) || await ctx.checkSources(refs)!==true || !live(ctx) || expectedArtifact!==ctx.artifact || !ctx.frames.every(exact))return
    return Object.freeze(ctx.frames.map(item=>Object.freeze({callID:item.part.callID,frame:item.frame,sourceJSON:item.part.state.status==='completed'?item.part.state.output:''})))
  } catch{return}
}
