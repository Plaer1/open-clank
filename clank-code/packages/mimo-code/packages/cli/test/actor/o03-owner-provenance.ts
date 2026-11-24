/** Disposable harness only; invoked by o03-owner-provenance.py against a prepared copy. */
import assert from "node:assert/strict"
import { initProjectors } from "../../src/server/projectors"
import { Effect, Layer, ManagedRuntime } from "effect"
import { Instance } from "../../src/project/instance"
import { Session } from "../../src/session"
import { ActorRegistry } from "../../src/actor/registry"
import { Database, sql } from "../../src/storage"
import * as ManagedProvider from "../../src/acp/managed-provider"
initProjectors()
const proof=JSON.parse(process.env.O03_OWNER_PROOF!)
await Instance.provide({directory:process.env.O03_WORKTREE!,fn:async()=>{
 const rt=ManagedRuntime.make(Layer.mergeAll(Session.defaultLayer,ActorRegistry.defaultLayer))
 try {
  const session=await rt.runPromise(Session.Service.use(s=>s.create({id:"ses_o03_owner" as any})))
  const owner=ManagedProvider.actorOwnerFromPrompt(proof,session.id,"root-op","chat")
  assert.equal(owner,"host-uuid");assert.equal(ManagedProvider.actorOwnerFromPrompt(proof,session.id,"other","chat"),undefined)
  const registry=await rt.runPromise(ActorRegistry.Service.use(s=>Effect.succeed(s)))
  // Main identity and both supported new-actor registration shapes remain
  // distinct from the original host owner carried by private context.
  const main=await rt.runPromise(registry.get(session.id,"main")) ?? await rt.runPromise(registry.register({sessionID:session.id,actorID:"main",mode:"main",agent:"main",description:"Main",contextMode:"none",background:false,lifecycle:"persistent",hostContext:{accountID:"provider-uuid",chatID:"chat",workspaceID:"work"}}))
  assert.equal(main.actorID,"main")
  const peerSession=await rt.runPromise(Session.Service.use(s=>s.create({id:"ses_o03_peer" as any,parentID:session.id})))
  const peer=await rt.runPromise(registry.register({sessionID:peerSession.id,actorID:peerSession.id,mode:"peer",parentActorID:"main",agent:"worker",description:"Peer task",contextMode:"none",background:false,lifecycle:"persistent",hostContext:{accountID:"provider-uuid",chatID:"chat",workspaceID:"work"},originalOwnerAccountID:owner}))
  const peerRaw=Database.use(db=>db.get<{provider:string,owner:string}>(sql`SELECT host_account_id AS provider,display_owner_account_id AS owner FROM actor_registry WHERE session_id=${peerSession.id} AND actor_id=${peer.actorID}`))
  assert.deepEqual(peerRaw,{provider:"provider-uuid",owner:"host-uuid"})
  const peerPage=await rt.runPromise(registry.pageBySession!({sessionID:peerSession.id,limit:50}));const peerDisplay=peerPage.items.find(x=>x.actorID===peer.actorID)!.display!
  assert.equal(await rt.runPromise(registry.renameDisplay!({sessionID:peerSession.id,actorID:peer.actorID,accountID:"host-uuid",chatID:"chat",workspaceID:"work",executionRevision:0,displayRevision:peerDisplay.revision,label:"Peer rename"})),true)
  const actor=await rt.runPromise(registry.registerAllocated({sessionID:session.id,mode:"subagent",agent:"worker",description:"Original task",contextMode:"none",background:false,lifecycle:"persistent",hostContext:{accountID:"provider-uuid",chatID:"chat",workspaceID:"work"},originalOwnerAccountID:owner}))
  const raw=Database.use(db=>db.get<{provider:string,owner:string}>(sql`SELECT host_account_id AS provider,display_owner_account_id AS owner FROM actor_registry WHERE session_id=${session.id} AND actor_id=${actor.actorID}`))
  assert.deepEqual(raw,{provider:"provider-uuid",owner:"host-uuid"})
  const prior=(await rt.runPromise(registry.pageBySession!({sessionID:"ses_o03_legacy" as any,limit:50}))).items.find(x=>x.actorID==="legacy-v1")!
  assert.equal(prior.display?.label,"Clank #1: legacy");assert.equal(prior.display?.renameAvailable,false)
  assert.equal(await rt.runPromise(registry.renameDisplay!({sessionID:"ses_o03_legacy" as any,actorID:"legacy-v1",accountID:"host-uuid",chatID:"chat",workspaceID:"work",executionRevision:0,displayRevision:0,label:"Denied"})),false)
  const before=await rt.runPromise(registry.get(session.id,actor.actorID));const page=await rt.runPromise(registry.pageBySession!({sessionID:session.id,limit:50}));const display=page.items.find(x=>x.actorID===actor.actorID)!.display!
  assert.equal(display.renameAvailable,true)
  const input={sessionID:session.id,actorID:actor.actorID,accountID:"host-uuid",chatID:"chat",workspaceID:"work",executionRevision:actor.executionRevision,displayRevision:display.revision,label:"Renamed"}
  assert.equal(await rt.runPromise(registry.renameDisplay!(input)),true);assert.equal(await rt.runPromise(registry.renameDisplay!(input)),false)
  assert.deepEqual(await rt.runPromise(registry.get(session.id,actor.actorID)),before)
  console.log("PASS native exact owner proof, v1 ownerless fallback, distinct provider pin and manual CAS")
 } finally {await rt.dispose()}
}})
Database.close()
