/** Disposable v1-label compatibility check, invoked by the owner provenance harness. */
import assert from "node:assert/strict"
import { Effect,Layer,ManagedRuntime } from "effect"
import { Instance } from "../../src/project/instance"
import { Session } from "../../src/session"
import { ActorRegistry } from "../../src/actor/registry"
import { Database } from "../../src/storage"
await Instance.provide({directory:process.env.O03_WORKTREE!,fn:async()=>{
 const rt=ManagedRuntime.make(Layer.mergeAll(Session.defaultLayer,ActorRegistry.defaultLayer))
 try {
  const reg=await rt.runPromise(ActorRegistry.Service.use(s=>Effect.succeed(s)))
  const actor=(await rt.runPromise(reg.pageBySession!({sessionID:"ses_o03_legacy" as any,limit:50}))).items.find(x=>x.actorID==="legacy-v1")!
  assert.equal(actor.display?.label,"Clank #1: legacy");assert.equal(actor.display?.renameAvailable,false)
  assert.equal(await rt.runPromise(reg.renameDisplay!({sessionID:"ses_o03_legacy" as any,actorID:"legacy-v1",accountID:"host-uuid",chatID:"chat",workspaceID:"work",executionRevision:0,displayRevision:0,label:"Denied"})),false)
  console.log('PASS native O03 v1 label readable, ownerless rename unavailable')
 } finally {await rt.dispose()}
}})
Database.close()
