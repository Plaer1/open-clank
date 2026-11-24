/** Host-private timing evidence. No issuer is installed until ACP bootstrap. */
import { createHash, generateKeyPairSync, randomBytes, sign } from "node:crypto"
import type { ActorExecutionIdentity } from "@/actor/turn"
import type { MessageV2 } from "./message-v2"

type Identity = { sessionID:string; rootOperationID:string; actorID:string; executionRevision:number; innerTurnID:string }
export type ToolTimingCapture = { owner:ReturnType<typeof createToolTimingIssuer>; identity:Identity; callID:string; messageID:string; partID:string; startUS:number; wallStartMS:number }
export type ToolTimingProof = { version:1; algorithm:"ed25519"; clockID:string; sessionID:string; rootOperationID:string; actorID:string; executionRevision:number; innerTurnID:string; messageID:string; partID:string; callID:string; phase:"terminal"; startUS:number; endUS:number; wallStartMS:number; wallEndMS:number; contentSHA256:string; signature:string }
export type QueuedToolTiming = { content:string; proof:ToolTimingProof }
const issuers = new WeakMap<object, ReturnType<typeof createToolTimingIssuer>>()
let issuing = false
// A weak sidecar follows only the exact in-process final clone published to
// the local bus. It is never serialized or placed in model-visible metadata.
const pending = new WeakMap<object, QueuedToolTiming>()
type AdmittedTurn = Identity & {viewControl?:{disabled:boolean;reason?:string}}
const admittedTurns = new WeakMap<object, AdmittedTurn>()
/** Called only after the host accepted and echoed the exact turn tuple. */
export function markAdmittedToolTimingTurn(identity:ActorExecutionIdentity, accepted:Identity) {
  if (identity.sessionID!==accepted.sessionID || identity.actorID!==accepted.actorID || identity.executionRevision!==accepted.executionRevision || identity.rootOperationID!==accepted.rootOperationID || identity.innerTurnID!==accepted.innerTurnID) return
  admittedTurns.set(identity,{...accepted})
}
/** Current only while the original accepted turn object and tuple remain live. */
export function isAdmittedToolTimingTurn(identity:ActorExecutionIdentity | undefined, expected:Identity):boolean {
  if (!identity) return false
  const admitted=admittedTurns.get(identity)
  return !!admitted && admitted.sessionID===expected.sessionID && admitted.rootOperationID===expected.rootOperationID && admitted.actorID===expected.actorID && admitted.executionRevision===expected.executionRevision && admitted.innerTurnID===expected.innerTurnID && identity.sessionID===expected.sessionID && identity.rootOperationID===expected.rootOperationID && identity.actorID===expected.actorID && identity.executionRevision===expected.executionRevision && identity.innerTurnID===expected.innerTurnID
}
/** On-only monotonic refusal on the existing exact admitted-turn lifetime. */
export function admittedToolViewControl(identity:ActorExecutionIdentity | undefined, requested:boolean) {
  if (!requested || !identity) return
  const admitted=admittedTurns.get(identity)
  if (!admitted || admitted.sessionID!==identity.sessionID || admitted.actorID!==identity.actorID ||
      admitted.executionRevision!==identity.executionRevision || admitted.rootOperationID!==identity.rootOperationID || admitted.innerTurnID!==identity.innerTurnID) return
  return admitted.viewControl ??= {disabled:false}
}
export function retireAdmittedToolTimingTurn(identity:ActorExecutionIdentity | undefined) {
  if (identity) admittedTurns.delete(identity)
}
const nowUS = () => Number(process.hrtime.bigint() / 1000n)
const field = (value:string) => { const bytes=Buffer.from(value,"utf8"); const length=Buffer.alloc(4); length.writeUInt32BE(bytes.length); return Buffer.concat([length,bytes]) }

/** Created per ACP Agent; registration and revocation belong to its bootstrap. */
export function createToolTimingIssuer() {
  const keys=generateKeyPairSync("ed25519"), clockID=randomBytes(16).toString("hex")
  let alive=true
  return {
    publicKey:keys.publicKey.export({type:"spki",format:"der"}).toString("base64"), clockID,
    isAlive() { return alive },
    revoke() { alive=false },
    emit(capture:ToolTimingCapture, part:MessageV2.ToolPart, publish:()=>void) {
      if (!alive || issuing || part.id!==capture.partID || part.messageID!==capture.messageID || part.sessionID!==capture.identity.sessionID || part.callID!==capture.callID || !["completed","error"].includes(part.state.status)) { publish(); return }
      const endUS=nowUS(), wallEndMS=part.state.status === "completed" || part.state.status === "error" ? part.state.time.end : 0
      if (!Number.isSafeInteger(endUS) || endUS<capture.startUS || !Number.isSafeInteger(wallEndMS)) { publish(); return }
      let content:string
      try { content=JSON.stringify(part) } catch { publish(); return }
      const contentSHA256=createHash("sha256").update(content,"utf8").digest("hex")
      const body={version:1 as const,algorithm:"ed25519" as const,clockID,...capture.identity,messageID:capture.messageID,partID:capture.partID,callID:capture.callID,phase:"terminal" as const,startUS:capture.startUS,endUS,wallStartMS:capture.wallStartMS,wallEndMS,contentSHA256}
      const values=["openclank.tool-interval.v1",...Object.values(body).map(String)]
      let signature:string
      try { signature=sign(null,Buffer.concat(values.map(field)),keys.privateKey).toString("base64") } catch { publish(); return }
      pending.set(part,{content,proof:{...body,signature}})
      issuing=true
      try { publish() } catch (error) { pending.delete(part); throw error } finally { issuing=false }
    },
  }
}
/** The key follows the exact host connection, never a model-provided identifier. */
export function installToolTimingIssuer(connection:object, value:ReturnType<typeof createToolTimingIssuer>) { if (issuers.has(connection)) throw new Error("tool timing issuer already installed"); issuers.set(connection,value) }
export function toolTimingIssuerFor(connection:object) { return issuers.get(connection) }
export function captureToolTiming(owner:ReturnType<typeof createToolTimingIssuer> | undefined, identity:ActorExecutionIdentity | undefined, part:MessageV2.ToolPart):ToolTimingCapture | undefined {
  const admitted=identity && admittedTurns.get(identity)
  if (!admitted || admitted.sessionID!==identity?.sessionID || admitted.actorID!==identity.actorID || admitted.executionRevision!==identity.executionRevision || admitted.rootOperationID!==identity.rootOperationID || admitted.innerTurnID!==identity.innerTurnID) return
  if (!owner || !identity?.rootOperationID || !identity.innerTurnID || identity.sessionID!==part.sessionID || !Number.isSafeInteger(identity.executionRevision) || part.state.status!=="running") return
  const startUS=nowUS(), wallStartMS=part.state.time.start
  if (!Number.isSafeInteger(startUS) || !Number.isSafeInteger(wallStartMS)) return
  return {owner,identity:{sessionID:identity.sessionID,rootOperationID:identity.rootOperationID,actorID:identity.actorID,executionRevision:identity.executionRevision,innerTurnID:identity.innerTurnID},callID:part.callID,messageID:part.messageID,partID:part.id,startUS,wallStartMS}
}
export function emitToolTiming(capture:ToolTimingCapture | undefined, part:MessageV2.ToolPart, publish:()=>void) { if (capture) capture.owner.emit(capture,part,publish); else publish() }
/** The history subscriber takes the sidecar from the exact locally published clone. */
export function currentToolTiming(part:MessageV2.Part):QueuedToolTiming | undefined {
  const value=pending.get(part)
  if (!value) return
  pending.delete(part)
  if (JSON.stringify(part)!==value.content) return
  return structuredClone(value)
}
/** Projector-only non-consuming view of the exact final-clone proof. */
export function peekToolTiming(part:MessageV2.Part):QueuedToolTiming | undefined {
  const value=pending.get(part)
  if (!value || JSON.stringify(part)!==value.content) return
  return value
}
