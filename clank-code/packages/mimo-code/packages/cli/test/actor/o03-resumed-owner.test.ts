import { expect, test } from 'bun:test'
import { Effect, Layer, ManagedRuntime } from 'effect'
import { Instance } from '../../src/project/instance'
import { Session } from '../../src/session'
import { ActorRegistry } from '../../src/actor/registry'
import { tmpdir } from '../fixture/fixture'
test('C2 result sets only exact native operation owner for descendant registration',async()=>{
 const result=JSON.parse(process.env.O03_RESUMED_RESULT || '{}');expect(result.displayOwnerProof).toEqual({version:1,accountID:'host-uuid'})
 const src=await Bun.file('src/acp/managed-provider.ts').text()
 const take=(name:string)=>{const a=src.indexOf(`export function ${name}(`),b=src.indexOf('\n}',a)+2;return src.slice(a,b).replace('export function','function')}
 const code=new Bun.Transpiler({loader:'ts'}).transformSync(take('captureActorDisplayOwner')+'\n'+take('bindActorDisplayOwner'))
 const host={}, replacementHost={};let liveHost=host
 const active={timingHost:host,binding:{rootOperationID:'op',accountID:'provider-uuid'},hostContext:{accountID:'provider-uuid',ownerAccountID:undefined as string|undefined}}
 let liveOperation=active
 const {capture,bind}=new Function('activeOperation','connection','ManagedProviderError',code+';return {capture:captureActorDisplayOwner,bind:bindActorDisplayOwner}')(()=>liveOperation,()=>liveHost,Error)
 const identity={sessionID:'child',actorID:'kid',rootOperationID:'op',executionRevision:5,innerTurnID:'turn'}, capability=capture(identity)
 liveHost=replacementHost;expect(()=>bind(capability,identity,'host-uuid')).toThrow();liveHost=host
 liveOperation={...active,hostContext:{...active.hostContext}};expect(()=>bind(capability,identity,'host-uuid')).toThrow();liveOperation=active
 expect(()=>bind(capability,{...identity,innerTurnID:'other'},'host-uuid')).toThrow()
 bind(capability,identity,result.displayOwnerProof.accountID)
 await using tmp=await tmpdir();await Instance.provide({directory:tmp.path,fn:async()=>{
  const rt=ManagedRuntime.make(Layer.mergeAll(Session.defaultLayer,ActorRegistry.defaultLayer))
  try {const session=await rt.runPromise(Session.Service.use(s=>s.create()));const registry=await rt.runPromise(ActorRegistry.Service.use(s=>Effect.succeed(s)))
   const child=await rt.runPromise(registry.registerAllocated({sessionID:session.id,mode:'subagent',agent:'worker',description:'Descendant',contextMode:'none',background:false,lifecycle:'persistent',hostContext:{accountID:active.hostContext.accountID,chatID:'chat',workspaceID:'work'},originalOwnerAccountID:active.hostContext.ownerAccountID}))
   expect(await rt.runPromise(registry.getDisplayOwner!(session.id,child.actorID))).toBe('host-uuid');expect(active.hostContext.accountID).toBe('provider-uuid')
  }finally{await rt.dispose()}
 }})
})
