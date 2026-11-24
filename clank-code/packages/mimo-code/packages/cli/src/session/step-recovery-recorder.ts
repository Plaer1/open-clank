/** Attempt-local execution ordering only. No storage, grants or Snapshot selection. */
import type { Binding } from "./step-recovery-owner"

type Member = Readonly<{sequence:number;actionID:string;toolCallID:string;actionRef:string}>
type Proof = Readonly<{binding:Binding;actionID:string;toolCallID:string;actionRef:string;current:()=>Promise<boolean>}>
type Call = {id:string; step:Step; state:"emitted"|"running"|"succeeded"|"failed"; proof?:Proof; token?:Invocation}
type Step = {id:string;closed:boolean;invalid:boolean;calls:Call[]}
export type Invocation = Readonly<{id:string;stepID:string}>

/** The exact stream event associates a call; callback timing never guesses a step. */
export function createStepRecoveryRecorder() {
  let current:Step | undefined, gate=true, invalid=false
  const steps:Step[]=[]
  const calls=new Map<string,Call>(), tokens=new WeakMap<object,Call>()
  const fail=()=>{invalid=true; if(current) current.invalid=true}
  return Object.freeze({
    start(id:string) {
      if (!gate || !id || current && !current.closed || steps.length>=64) {fail();return}
      current={id,closed:false,invalid:false,calls:[]}; steps.push(current)
    },
    emit(id:string) {
      if (!gate || !current || current.closed || !id || calls.has(id) || calls.size>=64) {fail();return}
      const call:Call={id,step:current,state:"emitted"};current.calls.push(call);calls.set(id,call)
    },
    enter(id:string):Invocation | undefined {
      const call=calls.get(id)
      if (!gate) {fail();throw new Error("stale tool dispatch")}
      if (!call || call.step.closed || call.state!=="emitted") {fail();return}
      call.state="running"
      const token=Object.freeze({id,stepID:call.step.id});call.token=token;tokens.set(token,call);return token
    },
    invocation(id:string):Invocation | undefined { return calls.get(id)?.token },
    isCurrent(token:Invocation):boolean {
      const call=tokens.get(token)
      return !!call && gate && call.state==="running" && calls.get(call.id)===call && !call.step.closed
    },
    offer(token:Invocation, proof:Proof) {
      const call=tokens.get(token)
      if (!call || call.state!=="running" || call.proof || proof.toolCallID!==call.id || proof.binding.stepID!==call.step.id) {fail();return}
      call.proof=proof
    },
    finish(token:Invocation | undefined, succeeded:boolean) {
      if (!token) {fail();return}
      const call=tokens.get(token)
      if (!call || call.state!=="running") {fail();return}
      tokens.delete(token)
      if (!succeeded || !call.proof) {call.state="failed";call.step.invalid=true;invalid=true;return}
      call.state="succeeded"
    },
    close() { if (!current || current.closed) {fail();return} current.closed=true },
    invalidate() {invalid=true},
    abort() {gate=false;invalid=true},
    drain():readonly {binding:Binding;members:Member[];current:readonly (()=>Promise<boolean>)[]}[] {
      gate=false
      if (invalid || [...calls.values()].some(call=>call.state!=="succeeded" || !call.proof)) return []
      const results:{binding:Binding;members:Member[];current:(()=>Promise<boolean>)[]}[]=[]
      for (const step of steps) {
        if (!step.closed || step.invalid || !step.calls.length || step.calls.some(call=>call.state!=="succeeded" || !call.proof)) continue
        const proofs=step.calls.map(call=>call.proof!)
        const first=proofs[0]!.binding
        const fields=Object.keys(first) as (keyof Binding)[]
        if (proofs.some(proof=>fields.some(key=>proof.binding[key]!==first[key])) ||
            new Set(proofs.map(proof=>proof.actionID)).size!==proofs.length) continue
        results.push({binding:first,members:proofs.map((proof,sequence)=>({sequence,actionID:proof.actionID,toolCallID:proof.toolCallID,actionRef:proof.actionRef})),current:proofs.map(proof=>proof.current)})
      }
      return results
    },
  })
}

/** Exact cooperative capture proof: only the original atomic owner's callback may call this. */
export async function qualifyOriginalCapture(input:{binding:Binding;history:import("@mimo-ai/shared/filesystem").AppFileSystem.HistoryContext;
  actionID:string;toolCallID:string;actionRef:string;policy:readonly unknown[];current:()=>Promise<boolean>}) {
  const {realpath,stat}=await import("node:fs/promises")
  const {createHash}=await import("node:crypto")
  const path=await import("node:path")
  const {AppFileSystem}=await import("@mimo-ai/shared/filesystem")
  const digest=(value:string)=>createHash("sha256").update(value,"utf8").digest("hex")
  const b=input.binding,h=input.history
  if (input.policy.length!==1 || h.accountId!==b.accountID || h.workspaceId!==b.workspaceID ||
      h.managedToolOrigin?.tool_call_id!==input.toolCallID || !await input.current()) return
  const evidence=input.policy[0] as any, policy=evidence?.policy, q=evidence?.qualification
  const expected={version:1,sessionID:b.sessionID,messageID:b.messageID,stepID:b.stepID,accountID:b.accountID,
    hostChatID:b.hostChatID,workspaceID:b.workspaceID,actorID:b.actorID,executionRevision:b.executionRevision,
    rootOperationID:b.rootOperationID,innerTurnID:b.innerTurnID,revision:0,coverage:"partial" as const,
    members:[{sequence:0,actionID:input.actionID,toolCallID:input.toolCallID,state:"complete" as const,actionRef:input.actionRef}]}
  const {CaptureMembership,matchesActionRef}=await import("./capture-membership")
  const parsed=CaptureMembership.safeParse(expected)
  if (!parsed.success || !matchesActionRef(input.actionRef,parsed.data,input.actionID,input.toolCallID) ||
      q?.accountID!==b.accountID || q?.stableChatID!==b.hostChatID || q?.authorityWorkspaceID!==b.workspaceID ||
      q?.toolCallID!==input.toolCallID || q?.identity?.sessionID!==b.sessionID || q?.identity?.actorID!==b.actorID ||
      q?.identity?.executionRevision!==b.executionRevision || q?.identity?.rootOperationID!==b.rootOperationID ||
      q?.identity?.innerTurnID!==b.innerTurnID || !policy || policy.stage!=="check") return
  const origin=h.managedToolOrigin
  const result=await AppFileSystem.historyActionRead(h,input.actionID,{kind:"action"})
  const action=result?.Action
  if (!action || action.state!=="Complete" || action.action_id!==input.actionID || action.actor_id!==h.actorId ||
      action.actor_account_id!==b.accountID || !origin || !action.managed_tool_origin_v1 ||
      Object.keys(origin).some(key=>action.managed_tool_origin_v1[key] !== (origin as any)[key]) ||
      Object.keys(action.managed_tool_origin_v1).length!==Object.keys(origin).length ||
      !Array.isArray(action.before_resources) || action.before_resources.length!==1 ||
      !Array.isArray(action.after_resources) || action.after_resources.length!==1) return
  const before=action.before_resources[0],after=action.after_resources[0]
  const ref=JSON.parse(Buffer.from(input.actionRef.slice("oc-history-action:v1:".length),"base64url").toString("utf8"))
  const resource=after?.resource_key, loc=after?.locator?.location_label
  if (!resource || resource.account_id!==b.accountID || resource.workspace_id!==b.workspaceID || resource.provider!=="mimo-filesystem" ||
      resource.resource_id!==ref.resourceID || before?.resource_key?.resource_id!==resource.resource_id ||
      before?.resource_key?.account_id!==b.accountID || before?.resource_key?.workspace_id!==b.workspaceID ||
      before?.resource_key?.provider!=="mimo-filesystem" ||
      before?.before?.version_id!==ref.beforeVersionID || after?.after?.version_id!==ref.afterVersionID || after?.after?.fingerprint!==after.fingerprint ||
      after.existence!=="Present" || after.resource_type!=="File" || typeof loc!=="string" || !loc ||
      path.isAbsolute(loc) || loc.split("/").some((segment:string)=>!segment || segment==="." || segment==="..")) return
  const match=typeof after.fingerprint==="string" ? /^sha256:([a-f0-9]{64}):([0-9]+)$/.exec(after.fingerprint) : null
  if (!match || !Number.isSafeInteger(Number(match[2])) || Number(match[2])>16*1024*1024 ||
      policy.candidateSHA256!==digest(JSON.stringify([[loc,match[1]]]))) return
  const root=path.resolve(h.workspaceRoot), resolvedRoot=await realpath(root)
  const target=path.resolve(root,loc), resolvedTarget=await realpath(target), rootStat=await stat(resolvedRoot,{bigint:true})
  if (root!==resolvedRoot || target!==resolvedTarget || !target.startsWith(root+path.sep) ||
      policy.rootIdentity!==`${rootStat.dev}:${rootStat.ino}` || policy.rootSHA256!==digest(root) || !await input.current()) return
  return Object.freeze({binding:b,actionID:input.actionID,toolCallID:input.toolCallID,actionRef:input.actionRef,
    current:async()=>!!await qualifyOriginalCapture(input)})
}
