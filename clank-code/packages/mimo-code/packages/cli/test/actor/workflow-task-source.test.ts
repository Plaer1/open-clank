import { expect, spyOn, test } from 'bun:test'
import { Layer, ManagedRuntime } from 'effect'
import { Instance } from '../../src/project/instance'
import { Session } from '../../src/session'
import { TaskRegistry } from '../../src/task/registry'
import { Database, sql } from '../../src/storage'
import { ManagedProvider } from '../../src/acp/managed-provider'
import { markAdmittedToolTimingTurn, retireAdmittedToolTimingTurn } from '../../src/session/tool-timing'
import { bindWorkflowTaskAssignment, claimWorkflowWriter, readWorkflowSource, releaseWorkflowTaskAssignment, recordWorkflowTaskCommit, workflowTaskCommitted, workflowTaskAssignmentCurrent } from '../../src/session/workflow-source'
import { tmpdir } from '../fixture/fixture'

test('task progress needs actual start and rejects reassignment away and back', async () => {
 await using tmp=await tmpdir()
 await Instance.provide({directory:tmp.path,fn:async()=>{
  const rt=ManagedRuntime.make(Layer.mergeAll(Session.defaultLayer,TaskRegistry.defaultLayer))
  const spies:any[]=[]
  try {
   const session=await rt.runPromise(Session.Service.use(s=>s.create()))
   const task=await rt.runPromise(TaskRegistry.Service.use(s=>s.create({session_id:session.id,summary:'work'})))
   Database.use(db=>db.run(sql`INSERT OR REPLACE INTO actor_registry (session_id,actor_id,mode,parent_actor_id,status,execution_revision,lifecycle,agent,description,context_mode,background,last_turn_time,turn_count,instance_id,time_created,time_updated) VALUES (${session.id},'writer','subagent','main','running',2,'ephemeral','build','test','none',1,1,0,'test',1,1)`))
   const actor={sessionID:session.id,actorID:'writer',executionRevision:2,rootOperationID:'root',innerTurnID:'turn'} as any
   const scope={} as any,issuer={isAlive:()=>true} as any
   spies.push(spyOn(ManagedProvider,'currentScope').mockReturnValue(scope),spyOn(ManagedProvider,'currentActorToolTimingIssuer').mockReturnValue(issuer),spyOn(ManagedProvider,'currentActorHostContext').mockReturnValue({ownerAccountID:'account',chatID:'chat',workspaceID:'workspace'}))
   markAdmittedToolTimingTurn(actor,actor)
   expect(bindWorkflowTaskAssignment({...actor,taskID:task.id},{...task,owner:'writer'})).toBe(false)
   const started=await rt.runPromise(TaskRegistry.Service.use(s=>s.start({session_id:session.id,id:task.id,owner:'writer'})))
   expect(bindWorkflowTaskAssignment({...actor,taskID:task.id},started)).toBe(true)
   const grant=claimWorkflowWriter(actor)!
   expect(grant.source.source).toMatchObject({kind:'native-task',sessionID:session.id,taskID:task.id})
   expect(readWorkflowSource(grant.sourceToken)?.state).toBe('current')
   const operationID='workflowop_'+'a'.repeat(64)
   expect(workflowTaskAssignmentCurrent(session.id,'writer',task.id)).toBe(true)
   expect(workflowTaskCommitted(session.id,'writer',task.id)).toBe(false)
   expect(recordWorkflowTaskCommit(actor,grant.sourceToken,operationID)).toBe(true)
   retireAdmittedToolTimingTurn(actor)
   expect(workflowTaskCommitted(session.id,'writer',task.id)).toBe(true)
   await rt.runPromise(TaskRegistry.Service.use(s=>s.start({session_id:session.id,id:task.id,owner:'other'})))
   await rt.runPromise(TaskRegistry.Service.use(s=>s.start({session_id:session.id,id:task.id,owner:'writer'})))
   expect(readWorkflowSource(grant.sourceToken)).toBeUndefined()
   expect(claimWorkflowWriter(actor)).toBeUndefined()
   expect(workflowTaskAssignmentCurrent(session.id,'writer',task.id)).toBe(false)
   expect(workflowTaskCommitted(session.id,'writer',task.id)).toBe(false)
   releaseWorkflowTaskAssignment(session.id,'writer',2);retireAdmittedToolTimingTurn(actor)
  } finally {spies.forEach(s=>s.mockRestore());await rt.dispose()}
 }})
})
