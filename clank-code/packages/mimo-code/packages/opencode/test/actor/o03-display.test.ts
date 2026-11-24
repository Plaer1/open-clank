import { expect,test } from "bun:test"
import { Effect,Layer,ManagedRuntime } from "effect"
import { Instance } from "../../src/project/instance"
import { Session } from "../../src/session"
import { ActorRegistry } from "../../src/actor/registry"
import { ActorRegistryTable } from "../../src/actor/actor.sql"
import { Database,sql } from "../../src/storage"
import layout from "../../src/storage/openclank-profile-layout.json"
import { tmpdir } from "../fixture/fixture"

test("display reads, exact manual CAS, and legacy fallback",async()=>{
 await using tmp=await tmpdir()
 await Instance.provide({directory:tmp.path,fn:async()=>{
  const rt=ManagedRuntime.make(Layer.mergeAll(Session.defaultLayer,ActorRegistry.defaultLayer))
  try {
   const session=await rt.runPromise(Session.Service.use(s=>s.create()))
   const registry=await rt.runPromise(ActorRegistry.Service.use(s=>Effect.succeed(s)))
   const actor=await rt.runPromise(registry.registerAllocated({sessionID:session.id,mode:"subagent",agent:"worker",description:"Original description",contextMode:"none",background:false,lifecycle:"persistent",hostContext:{accountID:"provider-uuid",chatID:"chat",workspaceID:"work"},originalOwnerAccountID:"host-uuid"}))
   const read=()=>rt.runPromise(registry.pageBySession!({sessionID:session.id,limit:50}))
   const initial=(await read()).items.find(x=>x.actorID===actor.actorID)!
   expect(initial.display?.ordinal).toBe(1);expect(initial.display?.renameAvailable).toBe(true);expect(initial.description).toBe("Original description")
   const raw=Database.use(db=>db.get<{provider:string,owner:string}>(sql`SELECT host_account_id AS provider,display_owner_account_id AS owner FROM actor_registry WHERE session_id=${session.id} AND actor_id=${actor.actorID}`))
   expect(raw).toEqual({provider:"provider-uuid",owner:"host-uuid"})
   const identityBefore=await rt.runPromise(registry.get(session.id,actor.actorID))
   const base={sessionID:session.id,actorID:actor.actorID,accountID:"host-uuid",chatID:"chat",workspaceID:"work",executionRevision:actor.executionRevision,displayRevision:initial.display!.revision}
   expect(await rt.runPromise(registry.renameDisplay!({...base,accountID:"foreign",label:"Denied"}))).toBe(false)
   expect(await rt.runPromise(registry.renameDisplay!({...base,executionRevision:1,label:"Denied"}))).toBe(false)
   const outcomes=await Promise.all(["First","Second"].map(label=>rt.runPromise(registry.renameDisplay!({...base,label}))))
   expect(outcomes.filter(Boolean)).toHaveLength(1)
   const updated=(await read()).items.find(x=>x.actorID===actor.actorID)!
   expect(updated.display?.revision).toBe(1);expect(updated.display?.origin).toBe("manual");expect(updated.description).toBe("Original description");expect(updated.actorID).toBe(actor.actorID)
   const identityAfter=await rt.runPromise(registry.get(session.id,actor.actorID))
   expect(identityAfter).toEqual(identityBefore)
   expect(await rt.runPromise(registry.renameDisplay!({...base,label:"Stale"}))).toBe(false)
   // A malformed display field must not poison actor identity/authority reads.
   Database.use(db=>{db.run(sql.raw("DROP TRIGGER o03_actor_label_update"));db.run(sql`UPDATE actor_registry SET display_label=NULL WHERE session_id=${session.id} AND actor_id=${actor.actorID}`);db.run(sql.raw(layout.labelObjects.find(x=>x.startsWith("CREATE TRIGGER o03_actor_label_update"))!))})
   expect((await rt.runPromise(registry.listBySession(session.id))).find(x=>x.actorID===actor.actorID)?.executionRevision).toBe(actor.executionRevision)
   await expect(read()).rejects.toBeDefined()
   Database.use(db=>{db.run(sql.raw("DROP TRIGGER o03_actor_label_update"));db.run(sql`UPDATE actor_registry SET display_label='Repaired fixture label' WHERE session_id=${session.id} AND actor_id=${actor.actorID}`);db.run(sql.raw(layout.labelObjects.find(x=>x.startsWith("CREATE TRIGGER o03_actor_label_update"))!))})
   Database.use(db=>db.insert(ActorRegistryTable).values({session_id:session.id,actor_id:"legacy",mode:"subagent",status:"idle",execution_revision:0,lifecycle:"persistent",agent:"worker",description:"Legacy",context_mode:"none",background:false,last_turn_time:1,turn_count:0,instance_id:"fixture",time_created:1,time_updated:1}).run())
   const legacy=(await read()).items.find(x=>x.actorID==="legacy")!
   expect(legacy.display).toBeUndefined();expect(legacy.description).toBe("Legacy")
  } finally {await rt.dispose()}
 }})
})
