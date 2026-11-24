/** Private step-recovery storage; the conservative retrospective recorder retains Git. */
import { createHash } from "node:crypto"
import { sql } from "drizzle-orm"
import { Database } from "../storage"
import { ManagedProvider } from "../acp/managed-provider"
import type { ActorExecutionIdentity } from "../actor/turn"
import { isAdmittedToolTimingTurn } from "./tool-timing"
import { CaptureMembership, matchesActionRef } from "./capture-membership"

const hash = (value:string) => createHash("sha256").update(value,"utf8").digest("hex")
const maxBytes = 65536
const capabilities = new WeakMap<object, {binding:Binding; identity:ActorExecutionIdentity; issuer:NonNullable<ReturnType<typeof ManagedProvider.currentActorToolTimingIssuer>>}>()
export type Binding = Readonly<{sessionID:string;messageID:string;stepID:string;accountID:string;hostChatID:string;workspaceID:string;actorID:string;executionRevision:number;rootOperationID:string;innerTurnID:string}>
type Member = {sequence:number;actionID:string;toolCallID:string;actionRef:string}
type Declaration = Binding & {version:1;state:"closed";profile:"direct-file-v1";dataSHA256:string;members:Member[];membersSHA256:string}
export type StepRecoveryCapability = Readonly<{readonly __stepRecoveryCapability: unique symbol}>

function current(binding:Binding, identity:ActorExecutionIdentity, issuer:NonNullable<ReturnType<typeof ManagedProvider.currentActorToolTimingIssuer>>) {
  const host=ManagedProvider.currentActorHostContext(binding.sessionID,binding.actorID,binding.executionRevision)
  return issuer.isAlive() && isAdmittedToolTimingTurn(identity,{sessionID:binding.sessionID,actorID:binding.actorID,executionRevision:binding.executionRevision,rootOperationID:binding.rootOperationID,innerTurnID:binding.innerTurnID})
    && !!host && host.accountID===binding.accountID && host.chatID===binding.hostChatID && host.workspaceID===binding.workspaceID
    && ManagedProvider.currentActorToolTimingIssuer(binding.sessionID,binding.actorID,binding.executionRevision,binding.rootOperationID)===issuer
}
/** Requires an actual live managed actor and the original timing owner; not a JSON grant. */
export function createStepRecoveryCapability(binding:Binding, identity:ActorExecutionIdentity):StepRecoveryCapability | undefined {
  const parsed=CaptureMembership.safeParse({version:1,...binding,revision:0,coverage:"open",members:[]})
  if (!parsed.success) return
  const issuer=ManagedProvider.currentActorToolTimingIssuer(binding.sessionID,binding.actorID,binding.executionRevision,binding.rootOperationID)
  const host=ManagedProvider.currentActorHostContext(binding.sessionID,binding.actorID,binding.executionRevision)
  if (!issuer || !host || !current(binding,identity,issuer)) return
  const capability=Object.freeze({}) as StepRecoveryCapability
  capabilities.set(capability,{binding:Object.freeze({...binding}),identity,issuer})
  return capability
}
function validate(value:unknown,data:string,binding:Binding):Declaration | undefined {
  if (!value || typeof value!=="object" || Array.isArray(value)) return
  const v=value as Declaration
  const keys=["sessionID","messageID","stepID","accountID","hostChatID","workspaceID","actorID","executionRevision","rootOperationID","innerTurnID","version","state","profile","dataSHA256","members","membersSHA256"].sort()
  if (Object.keys(v).sort().join(",")!==keys.join(",") || v.version!==1 || v.state!=="closed" || v.profile!=="direct-file-v1"
      || Object.keys(binding).some(key => v[key as keyof Binding]!==binding[key as keyof Binding])
      || v.dataSHA256!==hash(data) || !Array.isArray(v.members) || v.members.length>64
      || v.membersSHA256!==hash(JSON.stringify(v.members))) return
  const membership=CaptureMembership.safeParse({version:1,...binding,revision:0,coverage:"partial",members:v.members.map(m=>({...m,state:"complete"}))})
  if (!membership.success || v.members.some((m,i)=>Object.keys(m).sort().join(",")!=="actionID,actionRef,sequence,toolCallID"
      || m.sequence!==i || v.members.findIndex(n=>n.actionID===m.actionID || n.toolCallID===m.toolCallID)!==i
      || !matchesActionRef(m.actionRef,membership.data,m.actionID,m.toolCallID))) return
  try { if (JSON.parse(data)?.type!=="step-start") return } catch { return }
  return v
}
function messageMatches(db:any,b:Binding):boolean {
  const row=db.get(sql`SELECT agent_id AS actor, CASE WHEN length(CAST(data AS BLOB))<=262144 THEN data ELSE NULL END AS data FROM message WHERE id=${b.messageID} AND session_id=${b.sessionID}`) as {actor:string;data:string|null} | undefined
  if (!row?.data || row.actor!==b.actorID) return false
  try {
    const data=JSON.parse(row.data), x=data?.metadata?.managedExecution
    return data?.role==="assistant" && x?.sessionID===b.sessionID && x?.actorID===b.actorID && x?.executionRevision===b.executionRevision
      && x?.rootOperationID===b.rootOperationID && x?.innerTurnID===b.innerTurnID
  } catch { return false }
}
function supported(db:any):boolean { return db.all(sql`PRAGMA table_info('part')`).some((column:any)=>column.name==="openclank_step_recovery") }
/** Read-only availability; no caller may infer provenance from column existence. */
export function stepRecoveryProfileSupported():boolean {
  try { return Database.use(db=>supported(db)) } catch { return false }
}
/** Exact status is distinct from an empty closed declaration. */
export function readStepRecovery(binding:Binding):{state:"unsupported"|"missing"|"stale"} | {state:"closed";declaration:Declaration} {
  try { return Database.transaction(db=>{
    if (!supported(db)) return {state:"unsupported"} as const
    const row=db.get<{data:string|null;value:string|null}>(sql`SELECT CASE WHEN length(CAST(data AS BLOB))<=49152 THEN data ELSE NULL END AS data, CASE WHEN openclank_step_recovery IS NULL THEN NULL WHEN length(CAST(openclank_step_recovery AS BLOB))<=${maxBytes} THEN openclank_step_recovery ELSE '' END AS value FROM part WHERE id=${binding.stepID} AND message_id=${binding.messageID} AND session_id=${binding.sessionID}`)
    if (!row || row.value===null) return {state:"missing"} as const
    if (!messageMatches(db,binding)) return {state:"stale"} as const
    if (!row.data || !row.value) return {state:"stale"} as const
    const parsed=validate(JSON.parse(row.value),row.data,binding)
    return parsed ? {state:"closed",declaration:parsed} as const : {state:"stale"} as const
  },{behavior:"deferred"}) } catch { return {state:"stale"} }
}
/** Publish only through a live opaque capability. Receipt is local cooperative evidence. */
export function publishStepRecovery(capability:StepRecoveryCapability,members:Member[]):"published"|"identical"|"unavailable" {
  const admitted=capabilities.get(capability)
  if (!admitted || !current(admitted.binding,admitted.identity,admitted.issuer)) return "unavailable"
  try { return Database.transaction(db=>{
    const b=admitted.binding
    if (!supported(db) || !current(b,admitted.identity,admitted.issuer) || !messageMatches(db,b)) return "unavailable" as const
    const row=db.get<{data:string|null;value:string|null}>(sql`SELECT CASE WHEN length(CAST(data AS BLOB))<=49152 THEN data ELSE NULL END AS data, CASE WHEN openclank_step_recovery IS NULL THEN NULL WHEN length(CAST(openclank_step_recovery AS BLOB))<=${maxBytes} THEN openclank_step_recovery ELSE '' END AS value FROM part WHERE id=${b.stepID} AND message_id=${b.messageID} AND session_id=${b.sessionID}`)
    if (!row?.data || row.value==="") return "unavailable" as const
    const declaration:Declaration={...b,version:1,state:"closed",profile:"direct-file-v1",dataSHA256:hash(row.data),members,membersSHA256:hash(JSON.stringify(members))}
    const encoded=JSON.stringify(declaration)
    if (Buffer.byteLength(encoded,"utf8")>maxBytes || !validate(declaration,row.data,b) || !current(b,admitted.identity,admitted.issuer)) return "unavailable" as const
    if (row.value!==null) return row.value===encoded ? "identical" as const : "unavailable" as const
    db.run(sql`UPDATE part SET openclank_step_recovery=${encoded} WHERE id=${b.stepID} AND message_id=${b.messageID} AND session_id=${b.sessionID} AND data=${row.data} AND openclank_step_recovery IS NULL`)
    const after=db.get<{value:string|null}>(sql`SELECT openclank_step_recovery AS value FROM part WHERE id=${b.stepID} AND message_id=${b.messageID} AND session_id=${b.sessionID} AND data=${row.data}`)
    if (after?.value!==encoded || !current(b,admitted.identity,admitted.issuer)) throw new Error("step recovery issuance changed")
    return "published" as const
  },{behavior:"immediate"}) } catch { return "unavailable" }
}
