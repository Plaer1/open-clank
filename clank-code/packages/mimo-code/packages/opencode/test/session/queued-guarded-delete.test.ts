import { expect, test } from "bun:test"
import { Layer, ManagedRuntime } from "effect"
import { Instance } from "../../src/project/instance"
import { Session } from "../../src/session"
import { ActorRegistry } from "../../src/actor/registry"
import { Bus } from "../../src/bus"
import { Database, sql } from "../../src/storage"
import { queuedGuardSQL } from "../../src/storage/queued-source-guards"
import { initProjectors } from "../../src/server/projectors"
import * as UserInput from "../../src/session/user-input"
import { tmpdir } from "../fixture/fixture"

test("guarded subtree deletion refuses before touching any child", async () => {
  initProjectors()
  await using tmp=await tmpdir({git:true})
  await Instance.provide({directory:tmp.path,fn:async()=>{
    const rt=ManagedRuntime.make(Layer.mergeAll(Session.defaultLayer,ActorRegistry.defaultLayer,Bus.defaultLayer))
    try {
      const parent=await rt.runPromise(Session.Service.use(s=>s.create()))
      const child=await rt.runPromise(Session.Service.use(s=>s.create({parentID:parent.id})))
      Database.use(db=>{
        db.run(sql`ALTER TABLE user_input_receipt ADD COLUMN source_enrollment TEXT`)
        for(const guard of queuedGuardSQL) db.run(sql.raw(guard))
      })
      const binding={owner:'owner',stableChatID:'chat',mapRevision:3,mappingRevision:5,workspaceRevision:8}
      Database.use(db=>db.run(sql`UPDATE actor_registry SET chat_id='chat', effective_model=${JSON.stringify({providerID:'openai',modelID:'fixture'})} WHERE session_id=${parent.id} AND actor_id='main'`))
      const revision=await rt.runPromise(ActorRegistry.Service.use(r=>r.beginExecution(parent.id,'main')))
      UserInput.admit({...binding,sessionID:parent.id,actorID:'main',executionRevision:revision!,attachmentRefs:[],questionRefs:[],mode:'steer',requestID:'protected',text:'fixture'})
      Database.use(db=>db.run(sql`UPDATE user_input_receipt SET source_enrollment='fixture' WHERE request_id='protected'`))
      await expect(rt.runPromise(Session.Service.use(s=>s.remove(parent.id)))).rejects.toThrow('host fence')
      expect(await rt.runPromise(Session.Service.use(s=>s.get(parent.id)))).toBeTruthy()
      expect(await rt.runPromise(Session.Service.use(s=>s.get(child.id)))).toBeTruthy()
      const clean=await rt.runPromise(Session.Service.use(s=>s.create()))
      const cleanChild=await rt.runPromise(Session.Service.use(s=>s.create({parentID:clean.id})))
      await rt.runPromise(Session.Service.use(s=>s.remove(clean.id)))
      const rows=Database.use(db=>db.all<{id:string}>(sql`SELECT id FROM session WHERE id IN (${clean.id},${cleanChild.id})`))
      expect(rows).toHaveLength(0)
    } finally {await rt.dispose()}
  }})
  await Instance.disposeAll()
})
