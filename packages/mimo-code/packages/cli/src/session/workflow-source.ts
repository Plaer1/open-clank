/** Process-private checkpoint invocation evidence; no token is model input or persisted. */
import { randomBytes } from 'node:crypto'
import { Database, sql } from '@/storage'
import type { ActorExecutionIdentity } from '@/actor/turn'
import * as ManagedProvider from '@/acp/managed-provider'
import { admittedToolTimingGeneration } from './tool-timing'

type Turn = { sessionID: string; actorID: string; executionRevision: number; rootOperationID: string; innerTurnID: string }
type TaskRef = {kind:'native-task';sessionID:string;taskID:string;assignmentEventID:number}
type Source = Turn & { version: 1; source: { kind: 'root-operation'; rootOperationID: string } | TaskRef }
type TaskAssignment = {sessionID:string;actorID:string;executionRevision:number;rootOperationID:string;task:TaskRef;progressOperationID?:string}
type Witness = { identity: ActorExecutionIdentity; admission: object; turn: Turn; scope: NonNullable<ReturnType<typeof ManagedProvider.currentScope>>;
  issuer: NonNullable<ReturnType<typeof ManagedProvider.currentActorToolTimingIssuer>>; host: string }
type Record = { token: string; source: Witness; child?: { sessionID: string; actorID: string }; writer?: Witness; task?: TaskAssignment; rootSelf?: true; checkpointOperationID?: string }
export type WorkflowSource = object
const records = new Map<string, Record>()
const handles = new WeakMap<WorkflowSource, Record>()
const assignments = new Map<string,TaskAssignment>()
const assignmentKey=(sessionID:string,actorID:string)=>JSON.stringify([sessionID,actorID])
const tuple = (id: ActorExecutionIdentity): Turn | undefined => {
  if (!id.sessionID || !id.actorID || !id.rootOperationID || !id.innerTurnID ||
      !Number.isSafeInteger(id.executionRevision) || id.executionRevision < 0) return
  return { sessionID:id.sessionID, actorID:id.actorID, executionRevision:id.executionRevision,
    rootOperationID:id.rootOperationID, innerTurnID:id.innerTurnID }
}
function witness(identity: ActorExecutionIdentity): Witness | undefined {
  const turn = tuple(identity)
  if (!turn) return
  const admission = admittedToolTimingGeneration(identity, turn)
  if (!admission) return
  const scope = ManagedProvider.currentScope(turn.sessionID, turn.actorID, turn.executionRevision)
  const issuer = ManagedProvider.currentActorToolTimingIssuer(turn.sessionID, turn.actorID, turn.executionRevision, turn.rootOperationID)
  const context = ManagedProvider.currentActorHostContext(turn.sessionID, turn.actorID, turn.executionRevision)
  if (!scope || !issuer?.isAlive() || !context?.ownerAccountID || !context.chatID || !context.workspaceID) return
  return { identity, admission, turn, scope, issuer, host:JSON.stringify([context.ownerAccountID,context.chatID,context.workspaceID]) }
}
function current(value: Witness): boolean {
  const now = witness(value.identity)
  return !!now && now.admission === value.admission && now.scope === value.scope && now.issuer === value.issuer && now.host === value.host &&
    JSON.stringify(now.turn) === JSON.stringify(value.turn)
}
function sourceDTO(value: Witness): Source {
  return { version:1, ...value.turn, source:{kind:'root-operation',rootOperationID:value.turn.rootOperationID} }
}
/** The row and latest assignment event are read from one SQLite snapshot. */
function assignmentEvent(sessionID:string, actorID:string, executionRevision:number, taskID:string): number | undefined {
  try {
    const row=Database.use(db=>db.get<{eventID:number | null}>(sql`
      SELECT (SELECT MAX(e.id) FROM task_event e WHERE e.session_id=t.session_id AND e.task_id=t.id AND e.kind='started') AS eventID
      FROM task t JOIN actor_registry a ON a.session_id=t.session_id AND a.actor_id=${actorID}
      WHERE t.session_id=${sessionID} AND t.id=${taskID} AND t.status='in_progress' AND t.owner=${actorID}
        AND a.status='running' AND a.execution_revision=${executionRevision}`))
    return Number.isSafeInteger(row?.eventID) && row!.eventID!>0 ? row!.eventID! : undefined
  } catch { return }
}
function taskCurrent(item:TaskAssignment):boolean {
  return assignmentEvent(item.sessionID,item.actorID,item.executionRevision,item.task.taskID)===item.task.assignmentEventID
}
/** Spawn alone calls this, after TaskRegistry.start returned this exact assigned row. */
export function bindWorkflowTaskAssignment(input:{sessionID:string;actorID:string;executionRevision:number;rootOperationID?:string;taskID:string},
    started:{session_id:string;id:string;status:string;owner?:string}):boolean {
  const key=assignmentKey(input.sessionID,input.actorID)
  releaseWorkflowTaskAssignment(input.sessionID,input.actorID,input.executionRevision)
  if (!input.rootOperationID || !/^T[0-9]+(?:\.[0-9]+)*$/.test(input.taskID) ||
      started.session_id!==input.sessionID || started.id!==input.taskID || started.status!=='in_progress' || started.owner!==input.actorID) return false
  const eventID=assignmentEvent(input.sessionID,input.actorID,input.executionRevision,input.taskID)
  if (!eventID || assignments.size>=128) return false
  assignments.set(key,{...input,rootOperationID:input.rootOperationID,task:{kind:'native-task',sessionID:input.sessionID,taskID:input.taskID,assignmentEventID:eventID}})
  return true
}
export function releaseWorkflowTaskAssignment(sessionID:string,actorID:string,executionRevision:number):void {
  const key=assignmentKey(sessionID,actorID), item=assignments.get(key)
  if (item && item.executionRevision===executionRevision) {
    assignments.delete(key)
    for (const [token,record] of records) if (record.task===item) records.delete(token)
  }
}
/** A fresh invocation requires the original accepted turn object, not serialized identity. */
export function captureWorkflowSource(identity: ActorExecutionIdentity): WorkflowSource | undefined {
  for (const [token, item] of records) if (!current(item.source)) records.delete(token)
  if (records.size >= 128) return
  const source = witness(identity)
  if (!source) return
  const handle = Object.freeze({}), token = randomBytes(32).toString('hex')
  const record: Record = {token,source}; records.set(token,record); handles.set(handle,record)
  return handle
}
/** Called only by the checkpoint driver's pre-start actor callback. */
export function bindWorkflowChild(handle: WorkflowSource, sessionID: string, actorID: string): boolean {
  const record = handles.get(handle)
  if (record && !current(record.source)) records.delete(record.token)
  if (!record || records.get(record.token)!==record || record.child ||
      !sessionID || sessionID===record.source.turn.sessionID || !actorID ||
      [...records.values()].some((item)=>item.child?.sessionID===sessionID && item.child.actorID===actorID)) return false
  record.child={sessionID,actorID}; return true
}
/** Actual writer tool context binds once; repeat calls by that same turn reuse the binding. */
export function claimWorkflowWriter(identity: ActorExecutionIdentity): {sourceToken:string;source:Source;writer:Turn} | undefined {
  const writer = witness(identity)
  if (!writer) return
  let record = [...records.values()].find((item)=>item.child?.sessionID===writer.turn.sessionID && item.child.actorID===writer.turn.actorID)
  if (!record) {
    const item=assignments.get(assignmentKey(writer.turn.sessionID,writer.turn.actorID))
    if (!item) {
      // Only the actual admitted main turn can be its own root writer.
      if (writer.turn.actorID!=='main') return
      record=[...records.values()].find(value=>value.rootSelf===true && value.writer?.identity===identity)
      if (!record) {
        for (const [token,value] of records) if (!current(value.source)) records.delete(token)
        if (records.size>=128) return
        record={token:randomBytes(32).toString('hex'),source:writer,writer,rootSelf:true};records.set(record.token,record)
      }
      if (!current(record.source) || !current(record.writer!)) return
      return {sourceToken:record.token,source:sourceDTO(record.source),writer:{...record.writer!.turn}}
    }
    if (item.executionRevision!==writer.turn.executionRevision || item.rootOperationID!==writer.turn.rootOperationID || !taskCurrent(item)) return
    record=[...records.values()].find(value=>value.task===item && value.writer?.identity===identity)
    if (!record) {
      for (const [token,value] of records) if (!current(value.source)) records.delete(token)
      if (records.size>=128) return
      record={token:randomBytes(32).toString('hex'),source:writer,writer,task:item};records.set(record.token,record)
    }
    return {sourceToken:record.token,source:{version:1,...writer.turn,source:{...item.task}},writer:{...writer.turn}}
  }
  if (record && !current(record.source)) records.delete(record.token)
  if (record?.writer && !current(record.writer)) return
  if (!record || records.get(record.token)!==record || record.source.turn.rootOperationID!==writer.turn.rootOperationID ||
      record.source.host!==writer.host || (record.writer && record.writer.identity!==identity)) return
  if (!record.writer) record.writer=writer
  if (!current(record.writer)) return
  return {sourceToken:record.token,source:sourceDTO(record.source),writer:{...record.writer.turn}}
}
/** Source read is repeatable only while both admitted turn objects and scopes remain current. */
export function readWorkflowSource(token: string): {state:'current';source:Source;writer:Turn} | undefined {
  const record=records.get(token)
  if (!record?.writer || (!record.child && !record.task && !record.rootSelf)) return
  if (!current(record.source)) { records.delete(token); return }
  if (!current(record.writer)) return
  if (record.task) {
    if (assignments.get(assignmentKey(record.task.sessionID,record.task.actorID))!==record.task || !taskCurrent(record.task)) { records.delete(token); return }
    return {state:'current',source:{version:1,...record.source.turn,source:{...record.task.task}},writer:{...record.writer.turn}}
  }
  return {state:'current',source:sourceDTO(record.source),writer:{...record.writer.turn}}
}
/** Mint only after the tool validates an exact completed progress write and its sections. */
export function recordWorkflowTaskCommit(identity:ActorExecutionIdentity,sourceToken:string,operationID:string):boolean {
  const record=records.get(sourceToken), item=record?.task
  if (!record || !item || record.writer?.identity!==identity || !/^workflowop_[0-9a-f]{64}$/.test(operationID) ||
      !readWorkflowSource(sourceToken) || (item.progressOperationID && item.progressOperationID!==operationID)) return false
  item.progressOperationID=operationID
  return true
}
/** postStop is not a turn; it may observe only the already-proved same-execution assignment. */
export function workflowTaskAssignmentCurrent(sessionID:string,actorID:string,taskID:string):boolean {
  const item=assignments.get(assignmentKey(sessionID,actorID))
  return !!item && item.task.taskID===taskID && taskCurrent(item)
}
export function workflowTaskCommitted(sessionID:string,actorID:string,taskID:string):boolean {
  const item=assignments.get(assignmentKey(sessionID,actorID))
  return !!item?.progressOperationID && item.task.taskID===taskID && taskCurrent(item)
}
/** The first-party writer calls this only after its exact checkpoint write completed. */
export function recordWorkflowCheckpointCommit(identity: ActorExecutionIdentity, sourceToken: string, operationID: string): boolean {
  const record=records.get(sourceToken)
  if (!record || !record.child || record.task || !record.writer || record.writer.identity!==identity || !/^workflowop_[0-9a-f]{64}$/.test(operationID) ||
      !readWorkflowSource(sourceToken) || (record.checkpointOperationID && record.checkpointOperationID!==operationID)) return false
  record.checkpointOperationID=operationID
  return true
}
/** Completion is immutable once minted; the watcher may observe it after writer retirement, while the source remains current. */
export function workflowCheckpointCommitted(handle: WorkflowSource): boolean {
  const record=handles.get(handle)
  if (!record?.checkpointOperationID || records.get(record.token)!==record) return false
  if (!current(record.source)) { records.delete(record.token); return false }
  return true
}
export function releaseWorkflowSource(handle: WorkflowSource): void {
  const record=handles.get(handle)
  if (record && records.get(record.token)===record) records.delete(record.token)
  handles.delete(handle)
}
