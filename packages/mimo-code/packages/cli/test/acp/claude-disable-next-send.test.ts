import { expect, test } from 'bun:test'
import { ManagedProvider } from '../../src/acp/managed-provider'
process.env.OPEN_CLANK_MANAGED='1'
ManagedProvider.resetForTest()


test("Claude disable before next send is terminal without retry or account rotation", async () => {
  const { createManagedClaudeFetch, CLAUDE_PROFILE }=await import('../../src/plugin/claude')
  const { createAnthropic }=await import('@ai-sdk/anthropic')
  const { generateText }=await import('ai')
  const { MessageV2 }=await import('../../src/session/message-v2')
  const { SessionRetry }=await import('../../src/session/retry')
  const { ProviderID }=await import('../../src/provider/schema')
  const proof={version:1 as const,methodID:'anthropic.claude_subscription' as const,credentialOwner:'alice',policyRevision:1}
  const bootstrap={stableChatID:'chat',owner:'alice',rootSessionID:'session',sessionID:'session',actorID:'main',rootOperationID:'root',expectedExecutionRevision:0,mapRevision:1,mappingRevision:1,workspaceRevision:1,modelRouteID:'route'}
  const preflight={bootstrap,connectionID:'conn',providerID:'anthropic',billingLane:'subscription' as const,modelID:'model',modelRouteID:'route',methodPolicy:proof}
  const identity={sessionID:'session',actorID:'main',executionRevision:1,rootOperationID:'root'}
  const credential={type:'oauth' as const,access:'fake',refresh:'fake-refresh',expires:Date.now()+600_000}
  let disabled=false, sent=0, leases=0, attempts=0, refreshes=0
  const issued=Promise.withResolvers<void>(), finish=Promise.withResolvers<void>()
  ManagedProvider.installHostConnection({async extMethod(method:string) {
    if(method.endsWith('/account/bind')) return {...preflight,...identity,bindingID:'binding',bindingRevision:1,scopeKind:'actor_execution',credentialRequired:true,accountID:'account',credentialRevision:1,source:'round_robin',attempt:1,committed:false}
    if(method.endsWith('/credential/lease')) {leases++;if(disabled)throw new Error('method disabled HOST-PRIVATE');return {leaseID:'lease',connectionID:'conn',accountID:'account',credentialRevision:1,expiresAt:Date.now()+60_000,credential,methodPolicy:proof}}
    if(method.endsWith('/account/attempt')) {attempts++;throw Error('unexpected retry')}
    throw Error('unexpected callback '+method)
  }} as any)
  const active=await ManagedProvider.activateActorOperation(identity,preflight,{providerID:'conn',modelID:'model'},{chatID:'chat'})
  try {
    const context={accountUUID:'account',sessionID:'openclank-claude-session-v1:session',deviceID:'installation',platform:'Linux',architecture:'x64',runtime:'bun',runtimeVersion:'1.3.14'}
    const upstream=(async()=>{sent++;issued.resolve();await finish.promise;return new Response(JSON.stringify({id:'msg',type:'message',role:'assistant',model:'model',content:[{type:'text',text:'ok'}],stop_reason:'end_turn',stop_sequence:null,usage:{input_tokens:3,output_tokens:2}}),{status:200,headers:{'content-type':'application/json'}})}) as typeof fetch
    const fetcher=createManagedClaudeFetch(credential,upstream,async current=>{refreshes++;return current},()=>context,async current=>ManagedProvider.admitClaudeDispatch(active.scope,identity,current))
    const model=createAnthropic({apiKey:'',baseURL:CLAUDE_PROFILE.apiBaseURL,fetch:fetcher}).languageModel('model')
    const first=generateText({model,prompt:'first',maxRetries:0})
    await issued.promise
    disabled=true;finish.resolve()
    expect((await first).text).toBe('ok')
    let error:unknown
    try {await generateText({model,prompt:'next',maxRetries:0})} catch (e) {error=e}
    expect(error).toBeDefined()
    const parsed=MessageV2.fromLiveError(error,{providerID:ProviderID.make('conn')})
    expect(SessionRetry.decide(parsed,'stream','live-step').retryable).toBe(false)
    expect(JSON.stringify(parsed)).not.toContain('HOST-PRIVATE')
    const stop=new AbortController();stop.abort()
    await expect(fetcher('https://api.anthropic.com/v1/models',{signal:stop.signal})).rejects.toMatchObject({name:'AbortError'})
    expect(sent).toBe(1);expect(leases).toBe(3);expect(attempts).toBe(0);expect(refreshes).toBe(0)
  } finally {await active.release()}
})

