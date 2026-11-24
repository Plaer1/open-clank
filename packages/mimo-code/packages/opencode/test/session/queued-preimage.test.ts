import { expect, test } from "bun:test"
import { createHash } from "node:crypto"
import { Layer, ManagedRuntime } from "effect"
import { Instance } from "../../src/project/instance"
import { Session } from "../../src/session"
import { ActorRegistry } from "../../src/actor/registry"
import { Bus } from "../../src/bus"
import { Database, sql } from "../../src/storage"
import { initProjectors } from "../../src/server/projectors"
import { queuedGuardSQL } from "../../src/storage/queued-source-guards"
import * as UserInput from "../../src/session/user-input"
import { tmpdir } from "../fixture/fixture"
const sha=(s:string)=>createHash('sha256').update(s,'utf8').digest('hex')
test("enrolled preimage is exact, content-free and not upgraded from producer-only",async()=>{
 initProjectors()
 await using tmp=await tmpdir({git:true})
 await Instance.provide({directory:tmp.path,fn:async()=>{
  const rt=ManagedRuntime.make(Layer.mergeAll(Session.defaultLayer,ActorRegistry.defaultLayer,Bus.defaultLayer))
  try {
   const session=await rt.runPromise(Session.Service.use(s=>s.create()))
   Database.use(db=>db.run(sql`UPDATE actor_registry SET chat_id='chat', effective_model=${JSON.stringify({providerID:'openai',modelID:'fixture'})} WHERE session_id=${session.id} AND actor_id='main'`))
   const rev=await rt.runPromise(ActorRegistry.Service.use(r=>r.beginExecution(session.id,'main')))
   const binding={owner:'owner',stableChatID:'chat',mapRevision:3,mappingRevision:5,workspaceRevision:8}
   UserInput.admit({...binding,sessionID:session.id,actorID:'main',executionRevision:rev!,attachmentRefs:[],questionRefs:[],mode:'steer',requestID:'request',text:'fixture text',queuedSourceProof:'signed-envelope'})
   const pending=UserInput.snapshotBoundary({sessionID:session.id,actorID:'main',binding,afterTurnReady:false})
   expect(UserInput.consumeBoundary({sessionID:session.id,actorID:'main',binding,afterTurnReady:false,expectedReceipts:pending})).toBe(1)
   const input={owner:'owner',stableChatID:'chat',sessionID:session.id,requestID:'request'}
   const old=UserInput.readQueuedSource(input);expect(old.state).toBe('ready');if(old.state!=='ready')throw new Error('missing source')
   expect(old.mutationPreimage).toBeUndefined()
   expect(UserInput.pageQueuedSourceMarkers({owner:'owner',limit:1})).toEqual({state:'unavailable'})
   const source={version:1,kind:'queued_user_text',account_id:'account',chat_id:'chat',workspace_id:'workspace',request_id:'request',native_session_id:session.id,message_id:old.messageID,part_id:old.partID,archive_revision:1,archive_content_hash:'a'.repeat(64),text_sha256:sha('fixture text'),ingress_sha256:sha('signed-envelope'),root_operation_id:'root',execution_revision:1,inner_turn_id:'turn'}
   Database.use(db=>{db.run(sql`ALTER TABLE user_input_receipt ADD COLUMN source_enrollment TEXT`);db.run(sql.raw(queuedGuardSQL[0]!));db.run(sql`UPDATE user_input_receipt SET source_enrollment=${JSON.stringify({version:1,state:'enrolled',source})} WHERE request_id='request'`)})
   const first=UserInput.readQueuedSource(input);expect(first.state).toBe('ready');if(first.state!=='ready'||!first.mutationPreimage)throw new Error('missing preimage')
   expect(first.mutationPreimage.accountID).toBe('account');expect(JSON.stringify(first.mutationPreimage)).not.toContain('fixture text')
   expect(UserInput.readQueuedSource(input)).toEqual(first)
   const page=UserInput.pageQueuedSourceMarkers({owner:'owner',limit:1})
   expect(page.state).toBe('ready');if(page.state==='ready'){expect(page.items).toHaveLength(1);expect(page.items[0]).toEqual({state:'enrolled',source,preimage:first.mutationPreimage});expect(page.nextAfter).toBeNull()}
   Database.use(db=>db.run(sql`UPDATE part SET data=json_set(data,'$.text','changed') WHERE id=${old.partID}`))
   expect(UserInput.readQueuedSource(input)).toEqual({state:'unavailable'})
  } finally {await rt.dispose()}
 }})
 await Instance.disposeAll()
})
