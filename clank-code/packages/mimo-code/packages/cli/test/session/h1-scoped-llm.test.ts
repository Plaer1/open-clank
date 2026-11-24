import { expect, test } from "bun:test"
import { createAnthropic } from "@ai-sdk/anthropic"
import { Effect, Fiber, Layer, Stream } from "effect"
import { LLM } from "../../src/session/llm"
import { ProviderTest } from "../fake/provider"
import { ProviderID, ModelID } from "../../src/provider/schema"
import { Auth } from "../../src/auth"
import { Config } from "../../src/config"
import { Plugin } from "../../src/plugin"
import { ActorRegistry } from "../../src/actor/registry"
import { Memory } from "../../src/memory"
import { ManagedProvider } from "../../src/acp/managed-provider"
import { captureFetch } from "../../src/provider/logging-transport"
import { notePinnedAnthropicSDK, notePinnedAnthropicModel } from "../../src/provider/anthropic-origin"
import { observeAnthropicFailure } from "../../src/provider/fallback-terminal-witness"

test("actual LLM scoped reader closes exact synthetic pinned 401 before local take", async () => {
  const old=process.env.OPEN_CLANK_MANAGED;process.env.OPEN_CLANK_MANAGED="1"
  for (const mode of ["eligible","returned","callback_failure","prior_abort","extra_send","off"] as const) {
  const turn={sessionID:"session",rootOperationID:"root",actorID:"main",executionRevision:1,innerTurnID:"turn"}
  const bootstrap={stableChatID:"chat",owner:"alice",rootSessionID:"session",sessionID:"session",actorID:"main",rootOperationID:"root",expectedExecutionRevision:0,mapRevision:1,mappingRevision:1,workspaceRevision:1,modelRouteID:"route"}
  const binding={bindingID:"binding",bindingRevision:1,rootOperationID:"root",scopeKind:"actor_execution",sessionID:"session",actorID:"main",executionRevision:1,modelRouteID:"route",connectionID:"conn",providerID:"anthropic",billingLane:"metered_api",modelID:"fixture",credentialRequired:true,accountID:"account",credentialRevision:1,source:"round_robin",attempt:1,committed:false,pendingCrossModel:{version:1,coordinatorRevision:1}}
  if(mode==="off") delete (binding as any).pendingCrossModel
  let failedCallbacks=0
  const host:any={async extMethod(method:string){if(method.endsWith("/account/bind"))return binding;if(method.endsWith("/turn/start"))return {...turn,accepted:true};if(method.endsWith("/credential/lease"))return {...binding,leaseID:"lease",expiresAt:Date.now()+60000,credential:{type:"api",key:"fixture"},...(mode==="off"?{}:{crossModelAttempt:{version:1,attemptID:"a".repeat(64),attemptGeneration:1,coordinatorRevision:2}})};if(mode==="callback_failure" && method.endsWith("/logging/v1/admit"))return {admissionID:"log",policy:{revision:1,advanced_enabled:false,request_body_enabled:false,response_body_enabled:false,binary_body_enabled:false},context:{instanceID:"h",providerID:"anthropic",operationID:"o",rootOperationID:"root",bindingID:"binding",bindingRevision:1,identityCoverage:"complete",transportMode:"direct",persistence:{numeric:true,content:false,reason:"normal"}}};if(method.endsWith("/logging/v1/events"))failedCallbacks++;throw Error("synthetic unavailable")}}
  let entered!:()=>void;const started=new Promise<void>(resolve=>{entered=resolve})
  const sdk=createAnthropic({apiKey:"fixture",fetch:Object.assign(async (input:any,init:any)=>captureFetch(Object.assign(async(_i:any,options:any)=>{entered();if(mode==="prior_abort")return await new Promise<Response>((_,reject)=>options.signal.addEventListener("abort",()=>reject(new DOMException("aborted","AbortError")),{once:true}));if(mode==="extra_send")await observeAnthropicFailure(async()=>new Response("extra",{status:500}),"https://api.anthropic.com/v1/messages",{method:"POST"});const r=mode==="returned"?new Response('event: error\ndata: {"type":"error","error":{"type":"api_error","message":"fixture"}}\n\n',{status:200,headers:{"content-type":"text/event-stream"}}):new Response(JSON.stringify({type:"error",error:{type:"authentication_error",message:"fixture"}}),{status:401});Object.defineProperty(r,"url",{value:"https://api.anthropic.com/v1/messages"});return r},{preconnect:fetch.preconnect}) as any,ManagedProvider.currentScope("session","main",1))(input,init),{preconnect:fetch.preconnect})})
  const language=sdk("fixture");notePinnedAnthropicSDK(sdk);notePinnedAnthropicModel(sdk,language)
  const model=ProviderTest.model({providerID:ProviderID.make("conn"),id:ModelID.make("fixture"),limit:{context:32000,output:1024}})
  const provider=ProviderTest.fake({model,info:ProviderTest.info({id:model.providerID,options:{_openclankFamilyID:"anthropic",_openclankAdapterID:"anthropic-messages",_openclankBillingLane:"metered_api",_openclankConnectionID:"conn"}},model),getLanguage:()=>Effect.succeed(language)})
  const layer=LLM.layer.pipe(Layer.provide(Layer.mergeAll(provider.layer,Layer.succeed(Auth.Service,{} as any),Layer.succeed(Config.Service,{get:()=>Effect.succeed({compaction:{reserved:0}})} as any),Layer.succeed(Plugin.Service,{trigger:(_:string,_i:unknown,o:any)=>Effect.succeed(o)} as any),Layer.succeed(ActorRegistry.Service,{servesCheckpoint:()=>Effect.succeed(false)} as any),Layer.succeed(Memory.Service,{} as any))))
  ManagedProvider.installHostConnection(host)
  try {
    const activation=await ManagedProvider.activateActorOperation({sessionID:"session",actorID:"main",executionRevision:1,rootOperationID:"root"},{connectionID:"conn",providerID:"anthropic",billingLane:"metered_api",modelID:"fixture",modelRouteID:"route",bootstrap} as any,{providerID:"conn",modelID:"fixture"},{chatID:"chat"})
    await ManagedProvider.startManagedActorTurn(turn)
    let observed:unknown
    const program=Effect.gen(function*(){const llm=yield* LLM.Service;yield* llm.stream({sessionID:"session",model,agentID:"main",agent:{name:"chat",mode:"primary",options:{},permission:[{permission:"*",pattern:"*",action:"allow"}]},user:{id:"user",sessionID:"session",role:"user",time:{created:1},agent:"chat",model:{providerID:model.providerID,modelID:model.id}},system:[],prebuiltSystem:["fixture"],messages:[{role:"user",content:"fixture"}],tools:{},requestContext:{actorExecution:turn}} as any).pipe(Stream.tap((event)=>event.type==="error"?Effect.sync(()=>{observed=event.error;throw event.error}):Effect.void),Stream.runDrain)})
    if(mode==="prior_abort") {const fiber=Effect.runFork(program.pipe(Effect.provide(layer)));await started;await Effect.runPromise(Fiber.interrupt(fiber))}
    else await Effect.runPromise(program.pipe(Effect.provide(layer))).catch(()=>{})
    if(mode==="eligible") expect(observed).toBeDefined()
    if(mode==="callback_failure") expect(failedCallbacks).toBeGreaterThan(0)
    expect({mode,result:ManagedProvider.takeOwnedAnthropicOutcome(turn,observed)}).toEqual({mode,result:mode==="eligible"})
    expect(ManagedProvider.takeOwnedAnthropicOutcome(turn,observed)).toBe(false)
    await activation.release()
  } finally {ManagedProvider.resetForTest()}
  }
  if(old===undefined)delete process.env.OPEN_CLANK_MANAGED;else process.env.OPEN_CLANK_MANAGED=old
})
