/** Short-lived evidence from an actual newly-created actor callback; never model input. */
import { createHash, randomBytes } from 'node:crypto'
import type { ActorExecutionIdentity } from './turn'
import type { WorkerNamingCandidate } from './registry'
import { admittedToolTimingGeneration } from '@/session/tool-timing'
import { ManagedProvider } from '@/acp/managed-provider'
import { managedSessionBinding, markerMatches } from '@/memory/session-scope'

type NamingInput = {identity:ActorExecutionIdentity;messageID:string;toolCallID:string;candidate:WorkerNamingCandidate;
  action:'spawn'|'run';agent:string;description:string;task:string}
type Source = {input:NamingInput;admission:object;scope:NonNullable<ReturnType<typeof ManagedProvider.currentScope>>;
  issuer:NonNullable<ReturnType<typeof ManagedProvider.currentActorToolTimingIssuer>>;transport:()=>boolean;phase:'pending'|'captured';bindingToken:symbol;hostAccount:string;chat:string;workspace:string}
const records=new Map<string,Source>()
const text=(value:unknown,max:number):value is string=>typeof value==='string' && value.length>0 && value.isWellFormed() &&
  !value.includes('\0') && Buffer.byteLength(value,'utf8')<=max
function pendingCurrent(source:Source):boolean {
  const {identity,candidate}=source.input
  if (source.phase!=='pending' || !identity.rootOperationID || !identity.innerTurnID || !ManagedProvider.workerNamingEnabled(identity) || !source.transport()) return false
  const turn={sessionID:identity.sessionID,rootOperationID:identity.rootOperationID,actorID:identity.actorID,
    executionRevision:identity.executionRevision,innerTurnID:identity.innerTurnID}
  const binding=managedSessionBinding(identity.sessionID), host=ManagedProvider.currentActorHostContext(identity.sessionID,identity.actorID,identity.executionRevision)
  return admittedToolTimingGeneration(identity,turn)===source.admission &&
    ManagedProvider.currentScope(identity.sessionID,identity.actorID,identity.executionRevision)===source.scope &&
    ManagedProvider.currentActorToolTimingIssuer(identity.sessionID,identity.actorID,identity.executionRevision,identity.rootOperationID)===source.issuer && source.issuer.isAlive() &&
    !!binding && !binding.authority && !binding.transition && markerMatches(identity.sessionID,binding) && binding.registrationMarker.token===source.bindingToken &&
    binding.stableChatID===source.chat && binding.authorityWorkspaceID===source.workspace &&
    host?.ownerAccountID===source.hostAccount && host.chatID===source.chat && candidate.accountID===source.hostAccount &&
    candidate.chatID===source.chat && candidate.workspaceID===source.workspace
}
/** Only the real new-child onReady owner calls this, after its candidate read. */
export function captureWorkerNamingSource(input:NamingInput): string | undefined {
  const {identity,candidate}=input
  if (!ManagedProvider.workerNamingEnabled(identity) || identity.actorID!=='main' || !identity.rootOperationID || !identity.innerTurnID ||
      candidate.sessionID!==identity.sessionID || !text(input.messageID,256) || !text(input.toolCallID,256) ||
      !text(input.agent,256) || !text(input.description,2048) || input.description!==candidate.description || !text(input.task,8192)) return
  const turn={sessionID:identity.sessionID,rootOperationID:identity.rootOperationID,actorID:identity.actorID,
    executionRevision:identity.executionRevision,innerTurnID:identity.innerTurnID}
  const transport=ManagedProvider.captureWorkerNamingTransport(identity)
  const admission=admittedToolTimingGeneration(identity,turn),scope=ManagedProvider.currentScope(identity.sessionID,identity.actorID,identity.executionRevision)
  const issuer=ManagedProvider.currentActorToolTimingIssuer(identity.sessionID,identity.actorID,identity.executionRevision,identity.rootOperationID)
  const binding=managedSessionBinding(identity.sessionID),host=ManagedProvider.currentActorHostContext(identity.sessionID,identity.actorID,identity.executionRevision)
  if (!transport || !admission || !scope || !issuer?.isAlive() || !binding || binding.authority || binding.transition || !markerMatches(identity.sessionID,binding) ||
      !host?.ownerAccountID || !host.chatID || !binding.authorityWorkspaceID) return
  for (const [token,value] of records) if (value.phase==='pending' && !pendingCurrent(value) || value.phase==='captured' && !capturedCurrent(value)) records.delete(token)
  if (records.size>=128 || [...records.values()].filter(value=>value.admission===admission).length>=8) return
  const frozen={...input,candidate:Object.freeze({...candidate})}
  const source:Source={input:Object.freeze(frozen),admission,scope,issuer,transport,phase:'pending',bindingToken:binding.registrationMarker.token,
    hostAccount:host.ownerAccountID,chat:host.chatID,workspace:binding.authorityWorkspaceID}
  if (!pendingCurrent(source)) return
  const token=randomBytes(32).toString('hex');records.set(token,source);return token
}
export function readWorkerNamingSource(token:string) {
  const source=records.get(token)
  if (!source || !pendingCurrent(source)) { if (source?.phase==='pending') records.delete(token); return }
  const {identity,candidate,messageID,toolCallID,action,agent,description,task}=source.input
  const hash=(value:string)=>createHash('sha256').update(value,'utf8').digest('hex')
  return {version:1 as const,sessionID:identity.sessionID,rootOperationID:identity.rootOperationID!,actorID:'main' as const,
    executionRevision:identity.executionRevision,innerTurnID:identity.innerTurnID!,messageID,toolCallID,childSessionID:candidate.sessionID,
    childActorID:candidate.actorID,childExecutionRevision:candidate.executionRevision,displayRevision:candidate.displayRevision,
    accountID:candidate.accountID,chatID:candidate.chatID,workspaceID:candidate.workspaceID,ordinal:candidate.ordinal,
    action,agent,description,descriptionSHA256:hash(description),task,taskSHA256:hash(task)}
}
/** Transfer once, only after the exact source and binding were rechecked. */
export function acceptWorkerNamingSource(token:string):boolean {
  const source=records.get(token)
  if (!source || !pendingCurrent(source)) { if (source?.phase==='pending') records.delete(token); return false }
  source.phase='captured';return true
}
function capturedCurrent(source:Source):boolean {
  if (source.phase!=='captured' || !source.transport()) return false
  const binding=managedSessionBinding(source.input.identity.sessionID)
  return !!binding && !binding.authority && !binding.transition && markerMatches(source.input.identity.sessionID,binding) &&
    binding.registrationMarker.token===source.bindingToken && binding.stableChatID===source.chat && binding.authorityWorkspaceID===source.workspace
}
export function workerNamingSourceCurrent(token:string):boolean { const value=records.get(token);return !!value && capturedCurrent(value) }
export function releaseWorkerNamingSource(token:string):void { records.delete(token) }
