import type { ActorExecutionIdentity } from "@/actor/turn"
import type * as Tool from "./tool"
import { createHash } from "node:crypto"
import { callBoundOpenClankTool, projectPolicyInvocation } from "@/memory/mcp-client"

type RecoveryEvidence = Readonly<{version:1; qualification:Record<string,unknown>; policy:Record<string,unknown>; inputSHA256:string}>
type RecoveryState = {attempts:number; inFlight:number; unavailable:boolean; facts:Array<RecoveryEvidence | undefined>}
const recoveryEvidence = new WeakMap<object, RecoveryState>()
const recoveryRequested = new WeakSet<object>()
/** Private request only; it grants no authority and is not set by any current caller. */
export function requestProjectPolicyRecoveryEvidence(ctx:Tool.Context):void {
  recoveryRequested.add(ctx)
  if (!recoveryEvidence.has(ctx)) recoveryEvidence.set(ctx,{attempts:0,inFlight:0,unavailable:false,facts:[]})
}
/** Process-private facts only; later producer must independently revalidate them. */
export function projectPolicyRecoveryEvidence(ctx:Tool.Context):readonly RecoveryEvidence[] | undefined {
  const state=recoveryEvidence.get(ctx)
  return state && !state.unavailable && state.attempts>0 && state.inFlight===0 && state.facts.length===state.attempts && state.facts.every(Boolean)
    ? Object.freeze(state.facts.slice() as RecoveryEvidence[]) : undefined
}
const sha = (value:Uint8Array | string) => createHash("sha256").update(value).digest("hex")
function rememberEvidence(ctx:Tool.Context, input:Record<string,unknown>, result:unknown, original:ReturnType<typeof projectPolicyInvocation>) {
  if (!original || !result || typeof result!=="object" || Array.isArray(result)) return
  const value=result as Record<string,unknown>, q=value.qualification as Record<string,unknown>, policy=value.policy as Record<string,unknown>
  if (Object.keys(value).sort().join(",")!=="inputSHA256,policy,qualification,version" || value.version!==1 || !q || !policy ||
      typeof q!=="object" || Array.isArray(q) || typeof policy!=="object" || Array.isArray(policy)) return
  const qkeys=["version","identity","invocationID","toolName","toolCallID","owner","accountID","stableChatID","authorityWorkspaceID","mappingRevision","workspaceRevision","mapRevision","policyRevision"].sort()
  const pkeys=["version","classification","rootSHA256","rootIdentity","projectID","locatorRevision","sourceRevision","activationRevision","contractSHA256","engine","manifestSHA256","stage","candidateSHA256"].sort()
  const identity=q.identity
  const identityKeys=["sessionID","rootOperationID","actorID","executionRevision","innerTurnID"] as const
  if (!identity || typeof identity!=="object" || Array.isArray(identity) ||
      Object.keys(identity).sort().join(",")!==[...identityKeys].sort().join(",") ||
      identityKeys.some(key => (identity as Record<string,unknown>)[key]!==original.identity[key])) return
  if (Object.keys(q).sort().join(",")!==qkeys.join(",") || Object.keys(policy).sort().join(",")!==pkeys.join(",") || q.version!==1 || policy.version!==1 ||
      q.invocationID!==original.invocationID || q.toolName!==original.toolName || q.toolCallID!==original.toolCallID ||
      policy.stage!=="check" ||
      !["unregistered","inactive","active_builtins_only"].includes(String(policy.classification)) ||
      !/^[a-f0-9]{64}$/.test(String(policy.rootSHA256)) || !/^[a-f0-9]{64}$/.test(String(policy.candidateSHA256)) ||
      typeof policy.rootIdentity!=="string" || !policy.rootIdentity || typeof q.accountID!=="string" || !q.accountID ||
      typeof q.owner!=="string" || !q.owner || typeof q.stableChatID!=="string" || !q.stableChatID) return
  const bounded = (v:unknown) => typeof v === "string" && v.length > 0 && Buffer.byteLength(v,"utf8") <= 512
  const optionalNumber = (v:unknown) => v === null || (Number.isSafeInteger(v) && (v as number) >= 0)
  const optionalHash = (v:unknown) => v === null || (typeof v === "string" && /^[a-f0-9]{64}$/.test(v))
  if (![q.owner,q.accountID,q.stableChatID].every(bounded) || !(q.authorityWorkspaceID === null || bounded(q.authorityWorkspaceID)) ||
      ![q.mappingRevision,q.workspaceRevision,q.mapRevision,q.policyRevision].every(optionalNumber) ||
      !bounded(policy.rootIdentity) || !(policy.projectID === null || bounded(policy.projectID)) ||
      ![policy.locatorRevision,policy.sourceRevision,policy.activationRevision].every(optionalNumber) ||
      ![policy.contractSHA256,policy.manifestSHA256].every(optionalHash) || !(policy.engine === null || bounded(policy.engine))) return
  if (policy.classification === "unregistered" && (policy.projectID !== null || policy.contractSHA256 !== null || policy.manifestSHA256 !== null)) return
  if (policy.classification === "inactive" && (policy.projectID === null || policy.contractSHA256 !== null || policy.manifestSHA256 !== null)) return
  if (policy.classification === "active_builtins_only" && (!bounded(policy.projectID) || !optionalHash(policy.contractSHA256) || policy.contractSHA256 === null || !optionalHash(policy.manifestSHA256) || policy.manifestSHA256 === null || !bounded(policy.engine))) return
  if (Buffer.byteLength(JSON.stringify(value),"utf8") > 8192) return
  const rows=input.candidates
  if (!Array.isArray(rows) || rows.length>64) return
  try {
    const material=rows.map((row:any)=>[row.path,row.deleted===true ? null : sha(Buffer.from(row.content_base64,"base64"))])
    if (value.inputSHA256!==sha(JSON.stringify(material))) return
    if (q.identity && typeof q.identity === "object") Object.freeze(q.identity)
    Object.freeze(q); Object.freeze(policy)
    return Object.freeze(value as unknown as RecoveryEvidence)
  } catch { return }
}

type Candidate = {
  path: string
  content: string | Uint8Array | null
}

type PolicyResult = {
  enforced?: boolean
  allowed?: boolean
  reason?: string
}

function required() {
  return process.env.OPEN_CLANK_PROJECT_POLICY_BRIDGE === "required"
}

function encode(content: string | Uint8Array) {
  return typeof content === "string"
    ? Buffer.from(content).toString("base64")
    : Buffer.from(content.buffer, content.byteOffset, content.byteLength).toString("base64")
}

async function callPolicy(
  ctx: Tool.Context,
  input: Record<string, unknown>,
): Promise<PolicyResult> {
  const state = input.mode === "files" && recoveryRequested.has(ctx) ? recoveryEvidence.get(ctx) : undefined
  const index = state ? state.attempts++ : -1
  if (state) { state.inFlight++; if (state.attempts > 64) state.unavailable = true }
  let matched = false
  try {
    if (!required()) return { enforced: false, allowed: true }
    const result = await callBoundOpenClankTool(
      ctx.sessionID,
      "project_mutation_policy",
      input,
      ctx.extra?.actorExecution as ActorExecutionIdentity | undefined,
      typeof ctx.extra?.callingTool === "string" ? ctx.extra.callingTool : undefined,
      state && !state.unavailable ? ctx.callID : undefined,
    )
    const block = (result.content as Array<{ type: string; text?: string }> | undefined)?.[0]
    const parsed = JSON.parse(block?.text ?? "{}") as PolicyResult
    if (parsed.allowed !== true) throw new Error(parsed.reason || "active Open Clank project policy blocked the mutation")
    if (state && !state.unavailable) {
      const fact=rememberEvidence(ctx,input,(parsed as PolicyResult & {recovery_evidence?:unknown}).recovery_evidence,projectPolicyInvocation(result))
      if (fact) { state.facts[index]=fact; matched=true }
    }
    return parsed
  } finally {
    if (state) { state.inFlight--; if (!matched) state.unavailable=true }
  }
}

export function assertProjectFilePolicy(ctx: Tool.Context, candidates: Candidate[]) {
  return callPolicy(ctx, {
    mode: "files",
    candidates: candidates.map((candidate) => ({
      path: candidate.path,
      ...(candidate.content === null
        ? { deleted: true }
        : { content_base64: encode(candidate.content) }),
    })),
  })
}

export function assertProjectShellPolicy(ctx: Tool.Context) {
  return callPolicy(ctx, { mode: "shell" })
}
