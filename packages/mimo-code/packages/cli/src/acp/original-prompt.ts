/** Private ACP-to-local-prompt one-shot; it is not a model or capture grant. */
import { createHash, randomBytes } from "node:crypto"
import { Context } from "effect"
import { ManagedProvider } from "./managed-provider"

export class OriginalPromptHeader extends Context.Service<OriginalPromptHeader, {token:string; rawBody:unknown}>()("@opencode/OriginalPromptHeader") {}
export type OriginalPromptReservation = object
const pending=new Map<string,State>()
const reserved=new Map<string,State>()
const bound=new Map<string,State>()
const handles=new WeakMap<object,State>()
type State={token:string;sessionID:string;rootOperationID:string;bodySHA256:string;current:()=>boolean;expires:number;
  messageID?:string;snapshotSHA256?:string;finalized:boolean}
const key=(sessionID:string,messageID:string)=>JSON.stringify([sessionID,messageID])
function canonical(value:unknown):string | undefined {
  try {
    // Normalize exactly as JSON transport does, then sort object keys only.
    const normalized=JSON.parse(JSON.stringify(value)) as unknown
    const sort=(v:unknown,depth:number):unknown=>{
      if(depth>64)throw new Error("depth")
      if(Array.isArray(v))return v.map(x=>sort(x,depth+1))
      if(v && typeof v==="object")return Object.fromEntries(Object.entries(v).sort(([a],[b])=>a<b?-1:a>b?1:0).map(([k,x])=>[k,sort(x,depth+1)]))
      return v
    }
    const encoded=JSON.stringify(sort(normalized,0))
    return Buffer.byteLength(encoded,"utf8")<=80*1024*1024 ? encoded : undefined
  } catch{return undefined}
}
function digest(value:unknown):string | undefined {const text=canonical(value);return text===undefined?undefined:createHash("sha256").update(text,"utf8").digest("hex")}
function live(state:State):boolean {try{return Date.now()<state.expires && state.current()}catch{return false}}
function cleanup(){for(const [token,state] of pending)if(!live(state))pending.delete(token);for(const [token,state] of reserved)if(!live(state))reserved.delete(token);for(const [k,state] of bound)if(!live(state))bound.delete(k)}

/** Called inside the live original ACP delivery, before its one SDK request. */
export function mintOriginalPrompt(sessionID:string,rootOperationID:string,rawBody:unknown): {token:string;revoke:()=>void} | undefined {
  cleanup()
  const current=ManagedProvider.captureOriginalPromptDelivery(sessionID,rootOperationID), bodySHA256=digest(rawBody)
  if(!current || !bodySHA256 || pending.size+reserved.size+bound.size>=256)return
  const token=randomBytes(32).toString("hex")
  const state:State={token,sessionID,rootOperationID,bodySHA256,current,expires:Date.now()+60*60*1000,finalized:false}
  pending.set(token,state)
  return {token,revoke:()=>{pending.delete(token);reserved.delete(token);if(state.messageID && bound.get(key(sessionID,state.messageID))===state)bound.delete(key(sessionID,state.messageID))}}
}
/** Consume once against the exact raw JSON body and original live delivery. */
export function reserveOriginalPrompt(token:string,sessionID:string,rawBody:unknown):OriginalPromptReservation | undefined {
  const state=pending.get(token);pending.delete(token)
  if(!state || state.sessionID!==sessionID || !live(state) || digest(rawBody)!==state.bodySHA256)return
  const handle=Object.freeze({});reserved.set(token,state);handles.set(handle,state);return handle
}
/** Bind the created message only if the plugin preserved the complete snapshot. */
export function bindOriginalPrompt(handle:OriginalPromptReservation,messageID:string,beforeSnapshot:string,afterSnapshot:string,stableSnapshot:string):boolean {
  const state=handles.get(handle)
  if(!state || reserved.get(state.token)!==state || !live(state) || state.messageID || !messageID || beforeSnapshot!==afterSnapshot ||
      typeof stableSnapshot!=="string" || Buffer.byteLength(afterSnapshot,"utf8")>80*1024*1024 ||
      Buffer.byteLength(stableSnapshot,"utf8")>80*1024*1024)return false
  state.messageID=messageID;state.snapshotSHA256=createHash("sha256").update(stableSnapshot,"utf8").digest("hex");return true
}
/** Must run only after the exact message and inbox receipt commit. */
export function finalizeOriginalPrompt(handle:OriginalPromptReservation):boolean {
  const state=handles.get(handle)
  if(!state || reserved.get(state.token)!==state || !live(state) || !state.messageID || !state.snapshotSHA256 || state.finalized || bound.has(key(state.sessionID,state.messageID)))return false
  reserved.delete(state.token);state.finalized=true;bound.set(key(state.sessionID,state.messageID),state);return true
}
export function abortOriginalPrompt(handle:OriginalPromptReservation | undefined):void {
  const state=handle && handles.get(handle)
  if(!state)return
  reserved.delete(state.token);state.finalized=false
  if(state.messageID && bound.get(key(state.sessionID,state.messageID))===state)bound.delete(key(state.sessionID,state.messageID))
  handles.delete(handle!)
}
export function currentOriginalPromptMessage(sessionID:string,rootOperationID:string,messageID:string,currentSnapshot:string):boolean {
  const state=bound.get(key(sessionID,messageID))
  return !!state && state.finalized && state.rootOperationID===rootOperationID && live(state) &&
    createHash("sha256").update(currentSnapshot,"utf8").digest("hex")===state.snapshotSHA256
}
