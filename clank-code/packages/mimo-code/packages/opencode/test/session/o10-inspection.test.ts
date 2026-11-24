import { test, expect } from 'bun:test'
import { Layer, ManagedRuntime } from 'effect'
import { Instance } from '../../src/project/instance'
import { Session } from '../../src/session'
import { Database, sql } from '../../src/storage'
import { observeReceipts } from '../../src/session/user-input'
import { tmpdir } from '../fixture/fixture'
test('selected receipt observation is bounded, scoped and read-only', async () => {
 await using tmp=await tmpdir()
 await Instance.provide({directory:tmp.path,fn:async()=>{
  const rt=ManagedRuntime.make(Layer.mergeAll(Session.defaultLayer))
  try {
   const session=await rt.runPromise(Session.Service.use(s=>s.create()))
   Database.use(db=>{for(let i=0;i<3;i++) db.run(sql`INSERT INTO user_input_receipt
    (owner,stable_chat_id,map_revision,mapping_revision,workspace_revision,request_id,session_id,actor_id,admission_status,execution_revision,payload_hash,text,attachment_refs,question_refs,mode,state,created_at,updated_at)
    VALUES ('alice','chat',1,2,3,${'request'+i},${session.id},'actor','idle',1,'hash','SECRET','[]','[]','after-turn','persisted',1,1)`);})
   const b={owner:'alice',stableChatID:'chat',mapRevision:1,mappingRevision:2,workspaceRevision:3,sessionID:session.id,actorID:'actor'}
   const page=observeReceipts({...b,limit:2})
   expect(page.items.map(x=>x.requestID)).toEqual(['request0','request1']);expect(page.next).toBe('request1')
   expect(JSON.stringify(page)).not.toContain('SECRET')
   expect(observeReceipts({...b,after:page.next,limit:2}).items.map(x=>x.requestID)).toEqual(['request2'])
   expect(observeReceipts({...b,owner:'foreign',limit:2}).items).toEqual([])
   expect(Database.use(db=>db.get<{n:number}>(sql`SELECT count(*) AS n FROM user_input_receipt`))?.n).toBe(3)
  } finally {await rt.dispose()}
 }})
})

test('binding clones and escaped response respect the actual budget', async () => {
 const {installManagedSessionBinding,managedSessionBinding,beginManagedSessionTransition,resetManagedSessionScopesForTest}=await import('../../src/memory/session-scope')
 const {inspectionBindingCurrent,fitInspection}=await import('../../src/session/actor-inspection')
 resetManagedSessionScopesForTest()
 try {
  installManagedSessionBinding('root',{owner:'alice',stableChatID:'chat',engineSessionID:'root',engineAliases:[],memoryWorkspaceID:'global',authorityWorkspaceID:'work',copalWorkspace:'work',physicalCwd:'/tmp',bindingRevision:3,mapRevision:1,mappingRevision:2,memoryEnabled:false,transition:null})
  const first=managedSessionBinding('root')!, second=managedSessionBinding('root')!
  expect(first).not.toBe(second);expect(inspectionBindingCurrent('root',first)).toBe(true)
  beginManagedSessionTransition('root','/tmp/new','change',3)
  expect(inspectionBindingCurrent('root',first)).toBe(false)
  const messages=Array.from({length:20},(_,i)=>({id:`p${String(20-i).padStart(2,'0')}`,text:'\u0001'.repeat(2048)}))
  const page=fitInspection({actorID:'actor',sessionID:'root',executionRevision:1,status:'idle',messages,receipts:{items:[],next:undefined,coverage:'bounded_receipt_states'},coverage:'partial'})
  expect(Buffer.byteLength(JSON.stringify(page),'utf8')).toBeLessThanOrEqual(48000)
  expect(page.messages.length).toBeLessThan(20)
  expect(page.messageNext).toBe(page.messages.at(-1)!.id)
 } finally {resetManagedSessionScopesForTest()}
})
