import { afterEach, expect, test } from "bun:test"
import { Effect, Layer, ManagedRuntime } from "effect"
import { Instance } from "../../src/project/instance"
import { Server } from "../../src/server/server"
import { Session } from "../../src/session"
import { ActorRegistry } from "../../src/actor/registry"
import { tmpdir } from "../fixture/fixture"

afterEach(async () => { await Instance.disposeAll() })

test("bounded peer observation limits raw children before visible filtering", async () => {
  await using tmp = await tmpdir({git:true})
  await Instance.provide({directory:tmp.path, fn:async()=>{
    const runtime=ManagedRuntime.make(Layer.mergeAll(Session.defaultLayer,ActorRegistry.defaultLayer))
    try {
      const root=await runtime.runPromise(Session.Service.use(s=>s.create({})))
      const other=await runtime.runPromise(Session.Service.use(s=>s.create({})))
      for(let i=0;i<33;i++) await runtime.runPromise(Session.Service.use(s=>s.create({parentID:root.id})))
      const peer=await runtime.runPromise(Session.Service.use(s=>s.create({parentID:root.id})))
      await runtime.runPromise(ActorRegistry.Service.use(r=>r.register({sessionID:peer.id,actorID:peer.id,mode:'peer',agent:'general',description:'peer',contextMode:'none',background:false,lifecycle:'persistent'})))
      const legacy=await runtime.runPromise(Session.Service.use(s=>s.create({parentID:root.id})))
      await runtime.runPromise(ActorRegistry.Service.use(r=>r.register({sessionID:legacy.id,actorID:'legacy-peer',mode:'peer',agent:'general',description:'legacy',contextMode:'none',background:false,lifecycle:'persistent'})))
      const nested=await runtime.runPromise(Session.Service.use(s=>s.create({parentID:peer.id})))
      await runtime.runPromise(ActorRegistry.Service.use(r=>r.register({sessionID:nested.id,actorID:nested.id,mode:'peer',agent:'general',description:'nested',contextMode:'none',background:false,lifecycle:'persistent'})))
      const foreign=await runtime.runPromise(Session.Service.use(s=>s.create({parentID:other.id})))
      await runtime.runPromise(ActorRegistry.Service.use(r=>r.register({sessionID:foreign.id,actorID:foreign.id,mode:'peer',agent:'general',description:'foreign',contextMode:'none',background:false,lifecycle:'persistent'})))
      const app=Server.Default().app
      const first=await app.request(`/session/${root.id}/observation-page/peer?limit=4`);expect(first.status).toBe(200)
      const page=await first.json() as any;expect(page.items).toEqual([]);expect(page.nextCursor).toMatch(/^[1-9][0-9]*$/)
      const second=await app.request(`/session/${root.id}/observation-page/peer?limit=4&after=${page.nextCursor}`);expect(second.status).toBe(200)
      const later=await second.json() as any;expect(later.items).toEqual([{sessionId:peer.id,parentSessionId:root.id}]);expect(later.nextCursor).toBeUndefined()
      const proof=await app.request(`/session/${root.id}/observation-page/peer?limit=1&target=${peer.id}`);expect(proof.status).toBe(200)
      expect((await proof.json() as any).items).toEqual([{sessionId:peer.id,parentSessionId:root.id,directory:tmp.path}])
      for(const id of [legacy.id,nested.id,foreign.id]) expect((await app.request(`/session/${root.id}/observation-page/peer?limit=1&target=${id}`)).status).toBe(404)
      expect((await app.request(`/session/${root.id}/observation-page/peer?limit=5`)).status).toBe(400)
      expect((await app.request(`/session/${root.id}/observation-page/peer?limit=4&after=0`)).status).toBe(400)
    } finally { await runtime.dispose() }
  }})
})
