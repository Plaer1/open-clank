import { sourcePartStructuralDigest } from '../history/source-part-equivalence'
/** Request-local Browser evidence; source and target checks are delegated to existing owners. */
import { createHash } from 'node:crypto'
import * as MessageV2 from './message-v2'
import { validBrowserSnapshotReceipt } from './browser-snapshot-source-proof'
import { selectBrowserSnapshotRepeat, type BrowserSnapshotFrame } from './browser-snapshot-repeat'
import type {HistorySourceAck,HistorySourceRef} from '@/acp/openclank-protocol'

type Frame={part:MessageV2.ToolPart;frame:string;structuralDigest:string;outputHash:string;outputBytes:number;argsHash:string}
type Selector={version:1;settingsRevision:string;targetID:string;connectionGeneration:string;navigationGeneration:number;latestObservationGeneration:number}
type Context={sessionID:string;actorID:string;accountID:string;settingsRevision:string;current:()=>boolean;frames:Frame[];sealed:boolean;invalid:boolean;
  sourceAck:(actorID:string,part:MessageV2.ToolPart)=>Promise<HistorySourceAck|undefined>;
  check:(sources:HistorySourceRef[],selector:Selector)=>Promise<boolean>}
export type BrowserSnapshotFramesHandle=object & {readonly __browserSnapshotFrames?:never}
const owners=new WeakMap<object,Context>(),sha=(s:string)=>createHash('sha256').update(s,'utf8').digest('hex')
function argsHash(part:MessageV2.ToolPart):string|undefined {
  const v=part.state.input as any
  if(!v || typeof v!=='object' || Array.isArray(v) || Object.keys(v).sort().join(',')!=='action,arguments,connection_generation,navigation_generation,observation_generation,target_id' ||
      v.action!=='snapshot' || !v.arguments || typeof v.arguments!=='object' || Array.isArray(v.arguments) ||
      typeof v.target_id!=='string' || !v.target_id || typeof v.connection_generation!=='string' || !v.connection_generation ||
      !Number.isSafeInteger(v.navigation_generation) || v.navigation_generation<0 || !Number.isSafeInteger(v.observation_generation) || v.observation_generation<0)return
  const keys=Object.keys(v.arguments).sort()
  if(keys.some(key=>!['target','depth','boxes'].includes(key)))return
  const args=Object.fromEntries(keys.map(key=>[key,v.arguments[key]]))
  return sha(JSON.stringify([v.action,v.target_id,v.connection_generation,v.navigation_generation,args]))
}
function exact(f:Frame):boolean {
  try {const p=f.part,m=MessageV2.get({sessionID:p.sessionID,messageID:p.messageID}),hits=m.parts.filter(item=>item.id===p.id)
    if(hits.length!==1)return false
    const actual=hits[0]!
    return actual.type==='tool' && actual.callID===p.callID && actual.sessionID===p.sessionID && actual.messageID===p.messageID &&
      validBrowserSnapshotReceipt(actual) && actual.state.status==='completed' && sourcePartStructuralDigest(actual)===f.structuralDigest &&
      sha(actual.state.output)===f.outputHash && Buffer.byteLength(actual.state.output,'utf8')===f.outputBytes && actual.state.output===f.frame && argsHash(actual)===f.argsHash
  }catch{return false}
}
export function createBrowserSnapshotFrames(input:Omit<Context,'frames'|'sealed'|'invalid'>) {
  if(!input.sessionID || !input.actorID || !input.accountID || !/^[a-f0-9]{64}$/.test(input.settingsRevision) || typeof input.current!=='function' || typeof input.sourceAck!=='function' || typeof input.check!=='function')throw new Error('Browser source unavailable')
  const handle=Object.freeze({}) as BrowserSnapshotFramesHandle,ctx:Context={...input,frames:[],sealed:false,invalid:false};owners.set(handle,ctx)
  return {handle,record:(part:MessageV2.ToolPart,frame:string)=>{
    if(ctx.sealed || ctx.invalid || !validBrowserSnapshotReceipt(part))return
    try {const args=argsHash(part)
      if(!ctx.current() || !args || part.sessionID!==ctx.sessionID || part.state.status!=='completed' || part.state.output!==frame ||
          Buffer.byteLength(frame,'utf8')>51200 || ctx.frames.length>=32 || ctx.frames.some(item=>item.part.id===part.id || item.part.callID===part.callID)) {ctx.invalid=true;return}
      const structuralDigest=sourcePartStructuralDigest(part);if(!structuralDigest){ctx.invalid=true;return}
      ctx.frames.push({part:structuredClone(part),frame,argsHash:args,structuralDigest,outputHash:sha(frame),outputBytes:Buffer.byteLength(frame,'utf8')})
    }catch{ctx.invalid=true}
  },seal:()=>{ctx.sealed=true}}
}
export async function deriveVerifiedBrowserSnapshot(handle:BrowserSnapshotFramesHandle,body:string,modelID:string) {
  const ctx=owners.get(handle)
  if(!ctx || !ctx.sealed || ctx.invalid || ctx.frames.length<2 || !ctx.current())return
  const candidates:BrowserSnapshotFrame[]=ctx.frames.map(f=>({callID:f.part.callID,frame:f.frame,argsHash:f.argsHash}))
  const selected=selectBrowserSnapshotRepeat(body,candidates,modelID)
  if(!selected)return
  const chosen=[ctx.frames.find(f=>f.part.callID===selected.baseCallID),ctx.frames.find(f=>f.part.callID===selected.targetCallID)]
  if(!chosen[0] || !chosen[1] || chosen[0]===chosen[1] || !chosen.every(f=>f && exact(f)))return
  let timer:ReturnType<typeof setTimeout>|undefined
  const verify=async()=>{try {
    const frames=chosen as [Frame,Frame], target=JSON.parse(frames[1].frame)
    for(const frame of frames) {
      const result=JSON.parse(frame.frame), input=frame.part.state.input as any
      if(result.targetId!==input.target_id || result.connectionGeneration!==input.connection_generation ||
         result.navigationGeneration!==input.navigation_generation || result.observationGeneration!==input.observation_generation+1)return
    }
    const acknowledgements=await Promise.all(frames.map(f=>ctx.sourceAck(ctx.actorID,f.part)))
    if(!ctx.current())return
    const refs:HistorySourceRef[]=[]
    for(let i=0;i<2;i++) {const f=frames[i]!,ack=acknowledgements[i]
      if(!ack || ack.version!==1 || ack.accountID!==ctx.accountID || ack.actorID!==ctx.actorID || ack.sessionID!==f.part.sessionID ||
         ack.messageID!==f.part.messageID || ack.partID!==f.part.id || !/^[a-f0-9]{64}$/.test(ack.contentHash) || ack.outputSHA256!==f.outputHash || ack.outputBytes!==f.outputBytes || !Number.isSafeInteger(ack.revision) || ack.revision<1)return
      refs.push({messageID:ack.messageID,partID:ack.partID,revision:ack.revision,contentHash:ack.contentHash,outputSHA256:ack.outputSHA256,outputBytes:ack.outputBytes})
    }
    const selector:Selector={version:1,settingsRevision:ctx.settingsRevision,targetID:target.targetId,connectionGeneration:target.connectionGeneration,
      navigationGeneration:target.navigationGeneration,latestObservationGeneration:target.observationGeneration}
    if(await ctx.check(refs,selector)!==true || !ctx.current() || !frames.every(exact))return
    return selected
  }catch{return}}
  try {return await Promise.race([verify(),new Promise<undefined>(resolve=>{timer=setTimeout(()=>resolve(undefined),750)})])}
  finally {if(timer)clearTimeout(timer)}
}
