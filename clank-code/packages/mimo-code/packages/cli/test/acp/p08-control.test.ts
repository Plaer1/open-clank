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
 const host={async extMethod(method:string,params:any){calls.push(method);if(method==='_openclank/logging/v1/admit')throw new Error('logging unavailable');proc.stdin.write(JSON.stringify({method,params})+'\n');proc.stdin.flush();const result=await line();if(!result.ok)throw new Error('synthetic host refusal');if(method.endsWith('/account/commit')){seen();if(holdLease)await held;if(dropLease)throw new Error('lost response')}return result.value}}
 return {host,calls,issued,unblock,close:async()=>{proc.stdin.end();await proc.exited}}
}
test('qualified controls stop without retry and late replies cannot be adopted',async()=>{
 process.env.OPEN_CLANK_MANAGED='1'
 const f=await hostFixture()
 try {
  ManagedProvider.installHostConnection(f.host as any)
  const activation=await ManagedProvider.activateActorOperation(identity,preflight,{providerID:'conn-primary',modelID:'primary'},{chatID:'chat'})
  await ManagedProvider.startManagedActorTurn(turn)
  const before=await f.host.extMethod('test/status',{})
  const binding=ManagedProvider.currentBindingEvidence('session','main',1)!
  const badControl={version:1,turn,attemptID:'0'.repeat(64),attemptGeneration:1,expectedCoordinatorRevision:2}
  await expect(f.host.extMethod('_openclank/provider-store/v1/account/attempt',{bindingID:binding.bindingID,expectedRevision:binding.bindingRevision,accountID:binding.accountID,outcome:'unknown',crossModelControl:badControl})).rejects.toThrow()
  await expect(f.host.extMethod('_openclank/provider-store/v1/account/attempt',{bindingID:binding.bindingID,expectedRevision:binding.bindingRevision,accountID:binding.accountID,outcome:'auth'})).rejects.toThrow()
  await expect(f.host.extMethod('_openclank/provider-store/v1/account/attempt',{bindingID:binding.bindingID,expectedRevision:binding.bindingRevision,accountID:binding.accountID,outcome:'unknown',crossModelControl:{...badControl,turn:{...turn,innerTurnID:'other'}}})).rejects.toThrow()
  await ManagedProvider.commitSessionOperation('session','main',1)
  const committed=await f.host.extMethod('test/status',{});expect(committed.state).toBe('committed')
  const result=await ManagedProvider.recordSessionAttempt('session',new Error('fixture failure'),'main',1)
  expect(result.retry).toBe(false);expect(result.rotated).toBe(false)
  const after=await f.host.extMethod('test/status',{});expect(after.state).toBe('committed');expect(after.last.accounting).toBe('unavailable');expect(after.health).toEqual(before.health)
  await expect(ManagedProvider.recordSessionAttempt('session',new Error('duplicate'),'main',1)).rejects.toThrow()
  await activation.release()
 } finally {ManagedProvider.resetForTest();await f.close()}
 const stale=await hostFixture()
 try {
  ManagedProvider.installHostConnection(stale.host as any)
  const activation=await ManagedProvider.activateActorOperation(identity,preflight,{providerID:'conn-primary',modelID:'primary'},{chatID:'chat'})
  await ManagedProvider.startManagedActorTurn(turn)
  const before=await stale.host.extMethod('test/status',{});await stale.host.extMethod('test/stale-account',{})
  const result=await ManagedProvider.recordSessionAttempt('session',new Error('fixture failure'),'main',1)
  expect(result.retry).toBe(false);expect(result.rotated).toBe(false)
  const after=await stale.host.extMethod('test/status',{});expect(after.state).toBe('unknown');expect(after.health).toEqual(before.health)
  await expect(ManagedProvider.commitSessionOperation('session','main',1)).rejects.toThrow()
  await activation.release()
 } finally {ManagedProvider.resetForTest();await stale.close()}
 const late=await hostFixture(false,true)
 try {
  ManagedProvider.installHostConnection(late.host as any)
  const activation=await ManagedProvider.activateActorOperation(identity,preflight,{providerID:'conn-primary',modelID:'primary'},{chatID:'chat'})
  await ManagedProvider.startManagedActorTurn(turn)
  const committing=ManagedProvider.commitSessionOperation('session','main',1);await late.issued;await activation.release();late.unblock()
  await expect(committing).rejects.toThrow();expect(()=>ManagedProvider.requireScope('session','main',1)).toThrow()
 } finally {ManagedProvider.resetForTest();late.unblock();await late.close();delete process.env.OPEN_CLANK_MANAGED}
},30000)
