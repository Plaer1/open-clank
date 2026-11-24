import { expect, spyOn, test } from 'bun:test'
import { ManagedProvider } from '../../src/acp/managed-provider'
import { markAdmittedToolTimingTurn, retireAdmittedToolTimingTurn } from '../../src/session/tool-timing'
import { bindWorkflowChild, captureWorkflowSource, claimWorkflowWriter, readWorkflowSource, releaseWorkflowSource, recordWorkflowCheckpointCommit, workflowCheckpointCommitted } from '../../src/session/workflow-source'

test('checkpoint source requires both exact current turns and pre-start child binding', () => {
 const parent={sessionID:'root-session',actorID:'main',executionRevision:3,rootOperationID:'root',innerTurnID:'source-turn'} as any
 const writer={sessionID:'child',actorID:'writer',executionRevision:1,rootOperationID:'root',innerTurnID:'writer-turn'} as any
 const scope={} as any, issuer={isAlive:()=>true} as any
 const spies=[spyOn(ManagedProvider,'currentScope').mockReturnValue(scope),
  spyOn(ManagedProvider,'currentActorToolTimingIssuer').mockReturnValue(issuer),
  spyOn(ManagedProvider,'currentActorHostContext').mockReturnValue({ownerAccountID:'account',chatID:'chat',workspaceID:'workspace'})]
 markAdmittedToolTimingTurn(parent,parent); markAdmittedToolTimingTurn(writer,writer)
 try {
  const handle=captureWorkflowSource(parent)!
  expect(handle).toBeDefined()
  expect(claimWorkflowWriter(writer)).toBeUndefined()
  expect(bindWorkflowChild(handle,'child','writer')).toBe(true)
  expect(bindWorkflowChild(handle,'other','writer')).toBe(false)
  const grant=claimWorkflowWriter(writer)!
  expect(grant.source.source).toEqual({kind:'root-operation',rootOperationID:'root'})
  expect(grant.writer.innerTurnID).toBe('writer-turn')
  expect(claimWorkflowWriter(writer)?.sourceToken).toBe(grant.sourceToken)
  expect(readWorkflowSource(grant.sourceToken)?.state).toBe('current')
  const operationID='workflowop_'+'a'.repeat(64)
  expect(workflowCheckpointCommitted(handle)).toBe(false)
  expect(recordWorkflowCheckpointCommit({...writer},grant.sourceToken,operationID)).toBe(false)
  expect(recordWorkflowCheckpointCommit(writer,grant.sourceToken,operationID)).toBe(true)
  expect(recordWorkflowCheckpointCommit(writer,grant.sourceToken,'workflowop_'+'b'.repeat(64))).toBe(false)
  expect(workflowCheckpointCommitted(handle)).toBe(true)
  retireAdmittedToolTimingTurn(writer)
  expect(readWorkflowSource(grant.sourceToken)).toBeUndefined()
  expect(workflowCheckpointCommitted(handle)).toBe(true)
  markAdmittedToolTimingTurn(writer,writer)
  expect(recordWorkflowCheckpointCommit(writer,grant.sourceToken,operationID)).toBe(false)
  expect(workflowCheckpointCommitted(handle)).toBe(true)
  const forged={...writer}; markAdmittedToolTimingTurn(forged,forged)
  expect(claimWorkflowWriter(forged)).toBeUndefined()
  retireAdmittedToolTimingTurn(forged)
  retireAdmittedToolTimingTurn(parent)
  markAdmittedToolTimingTurn(parent,parent)
  expect(readWorkflowSource(grant.sourceToken)).toBeUndefined()
  expect(workflowCheckpointCommitted(handle)).toBe(false)
  releaseWorkflowSource(handle)
  expect(readWorkflowSource(grant.sourceToken)).toBeUndefined()
 } finally { retireAdmittedToolTimingTurn(parent);retireAdmittedToolTimingTurn(writer);spies.forEach(s=>s.mockRestore()) }
})
