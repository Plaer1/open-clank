import { expect, test } from 'bun:test'
import path from 'node:path'
import { ManagedProvider } from '../../src/acp/managed-provider'
const repo=path.resolve(import.meta.dir,'../../../../../..')
const turn={sessionID:'session',rootOperationID:'root',actorID:'main',executionRevision:1,innerTurnID:'turn'}
const bootstrap={stableChatID:'chat',owner:'alice',rootSessionID:'session',sessionID:'session',actorID:'main',rootOperationID:'root',expectedExecutionRevision:0,mapRevision:1,mappingRevision:1,workspaceRevision:1,modelRouteID:'route-primary'}
const preflight={connectionID:'conn-primary',providerID:'anthropic',billingLane:'metered_api',modelID:'primary',modelRouteID:'route-primary',bootstrap} as any
const identity={sessionID:'session',actorID:'main',executionRevision:1,rootOperationID:'root'}
async function hostFixture(dropLease=false,holdLease=false) {
 let unblock!:()=>void;const held=new Promise<void>(resolve=>{unblock=resolve});let seen!:()=>void;const issued=new Promise<void>(resolve=>{seen=resolve})
 const proc=Bun.spawn([process.env.OPEN_CLANK_PYTHON || 'python3',path.join(repo,'tests/fixtures/provider_transaction_host.py')],{cwd:repo,env:{...process.env,PYTHONPATH:repo,PYTHONDONTWRITEBYTECODE:'1'},stdin:'pipe',stdout:'pipe',stderr:'pipe'})
 const reader=proc.stdout.getReader(),decoder=new TextDecoder();let buffer=''
 async function line():Promise<any> {while(!buffer.includes('\n')){const chunk=await reader.read();if(chunk.done) throw new Error('synthetic host closed');buffer+=decoder.decode(chunk.value,{stream:true})}const i=buffer.indexOf('\n'),one=buffer.slice(0,i);buffer=buffer.slice(i+1);return JSON.parse(one)}
 expect((await line()).ready).toBe(true);const calls:string[]=[]
 const host={async extMethod(method:string,params:any){calls.push(method);if(method==='_openclank/logging/v1/admit')throw new Error('logging unavailable');proc.stdin.write(JSON.stringify({method,params})+'\n');proc.stdin.flush();const result=await line();if(!result.ok)throw new Error('synthetic host refusal');if(method.endsWith('/credential/lease')){seen();if(holdLease)await held;if(dropLease)throw new Error('lost response')}return result.value}}
 return {host,calls,issued,unblock,close:async()=>{proc.stdin.end();await proc.exited}}
}
test('pending waits for C2 and unknown issue is never repeated',async()=>{
 process.env.OPEN_CLANK_MANAGED='1';const fixture=await hostFixture()
 try{ManagedProvider.installHostConnection(fixture.host as any);const activation=await ManagedProvider.activateActorOperation(identity,preflight,{providerID:'conn-primary',modelID:'primary'},{chatID:'chat'});expect(()=>ManagedProvider.requireScope('session','main',1)).toThrow();expect(fixture.calls.filter(x=>x.endsWith('/credential/lease'))).toHaveLength(0);await ManagedProvider.startManagedActorTurn(turn);expect(ManagedProvider.requireScope('session','main',1).credentialRequired).toBe(true);expect(fixture.calls.filter(x=>x.endsWith('/credential/lease'))).toHaveLength(1);await ManagedProvider.startManagedActorTurn(turn);expect(fixture.calls.filter(x=>x.endsWith('/credential/lease'))).toHaveLength(1);await expect(ManagedProvider.startManagedActorTurn({...turn,innerTurnID:'other'})).rejects.toThrow();expect(fixture.calls.filter(x=>x.endsWith('/credential/lease'))).toHaveLength(1);await activation.release()}finally{ManagedProvider.resetForTest();await fixture.close()}
 const ordinaryCalls:string[]=[]
 const ordinary={async extMethod(method:string){ordinaryCalls.push(method);if(method.endsWith('/account/bind'))return {bindingID:'ordinary',bindingRevision:1,rootOperationID:'root',scopeKind:'actor_execution',sessionID:'session',actorID:'main',executionRevision:1,modelRouteID:'route-primary',connectionID:'conn-primary',providerID:'anthropic',billingLane:'metered_api',modelID:'primary',credentialRequired:true,accountID:'provider-account',credentialRevision:1,source:'round_robin',attempt:1,committed:false};if(method.endsWith('/credential/lease'))return {leaseID:'fixture',connectionID:'conn-primary',accountID:'provider-account',credentialRevision:1,expiresAt:Date.now()+60000,credential:{type:'api',key:'fixture'}};throw new Error('logging unavailable')}}
 ManagedProvider.installHostConnection(ordinary as any)
 const legacy=await ManagedProvider.activateActorOperation(identity,preflight,{providerID:'conn-primary',modelID:'primary'},{chatID:'chat'})
 expect(ManagedProvider.requireScope('session','main',1).credentialRequired).toBe(true)
 expect(ordinaryCalls.filter(x=>x.endsWith('/credential/lease'))).toHaveLength(1)
 await legacy.release();ManagedProvider.resetForTest()
 const delayed=await hostFixture(false,true)
 try{ManagedProvider.installHostConnection(delayed.host as any);const activation=await ManagedProvider.activateActorOperation(identity,preflight,{providerID:'conn-primary',modelID:'primary'},{chatID:'chat'});const pending=ManagedProvider.startManagedActorTurn(turn);await delayed.issued;await activation.release();delayed.unblock();await expect(pending).rejects.toThrow();expect(()=>ManagedProvider.requireScope('session','main',1)).toThrow();expect(delayed.calls.filter(x=>x.endsWith('/credential/lease'))).toHaveLength(1)}finally{ManagedProvider.resetForTest();delayed.unblock();await delayed.close()}
 const lost=await hostFixture(true)
 try{ManagedProvider.installHostConnection(lost.host as any);const activation=await ManagedProvider.activateActorOperation(identity,preflight,{providerID:'conn-primary',modelID:'primary'},{chatID:'chat'});await expect(ManagedProvider.startManagedActorTurn(turn)).rejects.toThrow();expect(()=>ManagedProvider.requireScope('session','main',1)).toThrow();await expect(ManagedProvider.startManagedActorTurn(turn)).rejects.toThrow();expect(lost.calls.filter(x=>x.endsWith('/credential/lease'))).toHaveLength(1);await activation.release()}finally{ManagedProvider.resetForTest();await lost.close();delete process.env.OPEN_CLANK_MANAGED}
},30000)
