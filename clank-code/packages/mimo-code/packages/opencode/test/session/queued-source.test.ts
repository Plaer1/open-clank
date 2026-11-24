import { expect, test } from "bun:test"
import { Layer, ManagedRuntime } from "effect"
import { Instance } from "../../src/project/instance"
import { Session } from "../../src/session"
import { ActorRegistry } from "../../src/actor/registry"
import { Bus } from "../../src/bus"
import { Database, sql } from "../../src/storage"
import * as UserInput from "../../src/session/user-input"
import { tmpdir } from "../fixture/fixture"

test("queued source proof and exact text part survive one consume without retroactive upgrade", async () => {
  await using tmp=await tmpdir({git:true})
  await Instance.provide({directory:tmp.path,fn:async()=>{
    const rt=ManagedRuntime.make(Layer.mergeAll(Session.defaultLayer,ActorRegistry.defaultLayer,Bus.defaultLayer))
    try {
      const session=await rt.runPromise(Session.Service.use(s=>s.create()))
      Database.use(db=>db.run(sql`UPDATE actor_registry SET chat_id='chat', effective_model=${JSON.stringify({providerID:'openai',modelID:'fixture'})} WHERE session_id=${session.id} AND actor_id='main'`))
      const revision=await rt.runPromise(ActorRegistry.Service.use(r=>r.beginExecution(session.id,'main')))
      const binding={owner:'owner',stableChatID:'chat',mapRevision:3,mappingRevision:5,workspaceRevision:8}
      const base={...binding,sessionID:session.id,actorID:'main',executionRevision:revision!,attachmentRefs:[],questionRefs:[],mode:'steer' as const}
      UserInput.admit({...base,requestID:'proven',text:'synthetic text',queuedSourceProof:'signed-envelope'})
      UserInput.admit({...base,requestID:'old',text:'legacy input'})
      UserInput.admit({...base,requestID:'old',text:'legacy input',queuedSourceProof:'must-not-upgrade'})
      const original=UserInput.readQueuedSource({owner:binding.owner,stableChatID:binding.stableChatID,sessionID:session.id,requestID:'proven'})
      expect(original.state).toBe('ready');if(original.state==='ready'){expect(original.proof).toBe('signed-envelope');expect(original.partID).toBeNull()}
      const pending=UserInput.snapshotBoundary({sessionID:session.id,actorID:'main',binding,afterTurnReady:false})
      expect(pending.find(x=>x.requestID==='proven')?.queuedSourceProof).toBe('signed-envelope')
      expect(pending.find(x=>x.requestID==='old')?.queuedSourceProof).toBeUndefined()
      expect(UserInput.consumeBoundary({sessionID:session.id,actorID:'main',binding,afterTurnReady:false,expectedReceipts:pending})).toBe(2)
      const refs=UserInput.consumedQueuedSources({owner:binding.owner,stableChatID:binding.stableChatID,sessionID:session.id,requestIDs:['proven','old']})
      expect(refs).toHaveLength(1);expect(refs[0]?.requestID).toBe('proven');expect(refs[0]?.proof).toBe('signed-envelope')
      const messages=await rt.runPromise(Session.Service.use(s=>s.messages({sessionID:session.id,agentID:'main'})))
      const message=messages.find(x=>x.info.id===refs[0]?.messageID)
      expect(message?.parts.map(x=>String(x.id))).toEqual([String(refs[0]?.partID)])
      expect(message?.parts[0]?.type==='text' && message.parts[0].text).toBe('synthetic text')
      const consumed=UserInput.readQueuedSource({owner:binding.owner,stableChatID:binding.stableChatID,sessionID:session.id,requestID:'proven'})
      expect(consumed.state).toBe('ready');if(consumed.state==='ready'){expect(consumed.messageID).toBe(refs[0]?.messageID);expect(consumed.partID).toBe(refs[0]?.partID)}
      expect(UserInput.readQueuedSource({owner:'other',stableChatID:binding.stableChatID,sessionID:session.id,requestID:'proven'}).state).toBe('unavailable')
      Database.use(db=>db.run(sql`UPDATE part SET data=json_set(data,'$.text','changed') WHERE id=${refs[0]!.partID}`))
      expect(UserInput.readQueuedSource({owner:binding.owner,stableChatID:binding.stableChatID,sessionID:session.id,requestID:'proven'}).state).toBe('unavailable')
      expect(UserInput.consumedQueuedSources({owner:binding.owner,stableChatID:binding.stableChatID,sessionID:session.id,requestIDs:['proven']})).toEqual([])
      const raw=Database.use(db=>db.get<{source_proof:string|null}>(sql`SELECT source_proof FROM user_input_receipt WHERE request_id='old'`))
      expect(raw?.source_proof).toBeNull()
      Database.use(db=>{for(const name of ['source_proof','source_message_id','source_part_id']) db.run(sql.raw(`ALTER TABLE user_input_receipt DROP COLUMN ${name}`))})
      UserInput.admit({...base,requestID:'legacy-profile',text:'plain steering',queuedSourceProof:'ignored'})
      const legacy=UserInput.snapshotBoundary({sessionID:session.id,actorID:'main',binding,afterTurnReady:false})
      expect(legacy[0]?.queuedSourceProof).toBeUndefined()
      expect(UserInput.consumeBoundary({sessionID:session.id,actorID:'main',binding,afterTurnReady:false,expectedReceipts:legacy})).toBe(1)
      expect(UserInput.consumedQueuedSources({owner:binding.owner,stableChatID:binding.stableChatID,sessionID:session.id,requestIDs:['legacy-profile']})).toEqual([])
    } finally {await rt.dispose()}
  }})
  await Instance.disposeAll()
})
