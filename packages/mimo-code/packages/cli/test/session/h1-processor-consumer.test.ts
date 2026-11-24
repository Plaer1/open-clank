import { expect, spyOn, test } from "bun:test"
import { createAnthropic } from "@ai-sdk/anthropic"
import { Effect, Layer } from "effect"
import { LLM } from "../../src/session/llm"
import { SessionProcessor } from "../../src/session/processor"
import * as Session from "../../src/session/session"
import { SessionStatus } from "../../src/session/status"
import { SessionSummary } from "../../src/session/summary"
import { Bus } from "../../src/bus"
import { Snapshot } from "../../src/snapshot"
import { Agent } from "../../src/agent/agent"
import { Permission } from "../../src/permission"
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

test("processor consumes same scoped 401 before conservative account stop", async () => {
  const old=process.env.OPEN_CLANK_MANAGED;process.env.OPEN_CLANK_MANAGED="1"
  const turn={sessionID:"session",rootOperationID:"root",actorID:"main",executionRevision:1,innerTurnID:"turn"}
  const bootstrap={stableChatID:"chat",owner:"alice",rootSessionID:"session",sessionID:"session",actorID:"main",rootOperationID:"root",expectedExecutionRevision:0,mapRevision:1,mappingRevision:1,workspaceRevision:1,modelRouteID:"route"}
  const binding={bindingID:"binding",bindingRevision:1,rootOperationID:"root",scopeKind:"actor_execution",sessionID:"session",actorID:"main",executionRevision:1,modelRouteID:"route",connectionID:"conn",providerID:"anthropic",billingLane:"metered_api",modelID:"fixture",credentialRequired:true,accountID:"account",credentialRevision:1,source:"round_robin",attempt:1,committed:false,pendingCrossModel:{version:1,coordinatorRevision:1}}
  let sdkError:unknown, controlCalls=0, consumedBeforeControl=false, sends=0
  const host:any={async extMethod(method:string){
    if(method.endsWith("/account/bind"))return binding
    if(method.endsWith("/turn/start"))return {...turn,accepted:true}
    if(method.endsWith("/credential/lease"))return {...binding,leaseID:"lease",expiresAt:Date.now()+60000,credential:{type:"api",key:"fixture"},crossModelAttempt:{version:1,attemptID:"a".repeat(64),attemptGeneration:1,coordinatorRevision:2}}
    if(method.endsWith("/account/attempt")){controlCalls++;expect(sdkError).toBeDefined();consumedBeforeControl=!ManagedProvider.takeOwnedAnthropicOutcome(turn,sdkError);return {...binding,bindingRevision:2,crossModelAttempt:{version:1,attemptID:"a".repeat(64),attemptGeneration:1,coordinatorRevision:3,accounting:"unavailable"}}}
    throw Error("synthetic unavailable")
  }}
  const sdk=createAnthropic({apiKey:"fixture",fetch:Object.assign(async (input:any,init:any)=>captureFetch(Object.assign(async()=>{sends++;const r=new Response(JSON.stringify({type:"error",error:{type:"authentication_error",message:"fixture"}}),{status:401});Object.defineProperty(r,"url",{value:"https://api.anthropic.com/v1/messages"});return r},{preconnect:fetch.preconnect}) as any,ManagedProvider.currentScope("session","main",1))(input,init),{preconnect:fetch.preconnect})})
  const language=sdk("fixture"), original=language.doStream.bind(language)
  ;(language as any).doStream=async (...args:any[])=>{try{return await original(...args as Parameters<typeof original>)}catch(error){sdkError=error;throw error}}
  notePinnedAnthropicSDK(sdk);notePinnedAnthropicModel(sdk,language)
  const model=ProviderTest.model({providerID:ProviderID.make("conn"),id:ModelID.make("fixture"),limit:{context:32000,output:1024}})
  const provider=ProviderTest.fake({model,info:ProviderTest.info({id:model.providerID,options:{_openclankFamilyID:"anthropic",_openclankAdapterID:"anthropic-messages",_openclankBillingLane:"metered_api",_openclankConnectionID:"conn"}},model),getLanguage:()=>Effect.succeed(language)})
  const cfg={get:()=>Effect.succeed({compaction:{reserved:0}})} as any, plugin={trigger:(_:string,_i:unknown,o:any)=>Effect.succeed(o)} as any
  const llm=LLM.layer.pipe(Layer.provide(Layer.mergeAll(provider.layer,Layer.succeed(Auth.Service,{} as any),Layer.succeed(Config.Service,cfg),Layer.succeed(Plugin.Service,plugin),Layer.succeed(ActorRegistry.Service,{servesCheckpoint:()=>Effect.succeed(false)} as any),Layer.succeed(Memory.Service,{} as any))))
  const deps=Layer.mergeAll(llm,Layer.succeed(Session.Service,{updateMessage:()=>Effect.void} as any),Layer.succeed(Config.Service,cfg),Layer.succeed(Bus.Service,{publish:()=>Effect.void} as any),Layer.succeed(Snapshot.Service,{track:()=>Effect.succeed(undefined)} as any),Layer.succeed(Agent.Service,{} as any),Permission.defaultLayer,Layer.succeed(Plugin.Service,plugin),Layer.succeed(SessionSummary.Service,{} as any),Layer.succeed(SessionStatus.Service,{set:()=>Effect.void} as any))
  const layer=SessionProcessor.layer.pipe(Layer.provide(deps))
  let positiveTakes=0
  const originalTake=ManagedProvider.takeOwnedAnthropicOutcome
  const takeSpy=spyOn(ManagedProvider,"takeOwnedAnthropicOutcome").mockImplementation((identity,error)=>{const result=originalTake(identity,error);if(result)positiveTakes++;return result})
  ManagedProvider.installHostConnection(host)
  try {
    const activation=await ManagedProvider.activateActorOperation({sessionID:"session",actorID:"main",executionRevision:1,rootOperationID:"root"},{connectionID:"conn",providerID:"anthropic",billingLane:"metered_api",modelID:"fixture",modelRouteID:"route",bootstrap} as any,{providerID:"conn",modelID:"fixture"},{chatID:"chat"})
    await ManagedProvider.startManagedActorTurn(turn)
    const user={id:"user",sessionID:"session",role:"user",time:{created:1},agent:"chat",model:{providerID:model.providerID,modelID:model.id}}
    const agent={name:"chat",mode:"primary",options:{},permission:[{permission:"*",pattern:"*",action:"allow"}]}
    const program=Effect.gen(function*(){const processors=yield* SessionProcessor.Service;const handle=yield* processors.create({sessionID:"session",model,actorExecution:turn,assistantMessage:{id:"assistant",sessionID:"session",role:"assistant",parentID:"user",agent:"chat",agentID:"main",time:{created:1},tokens:{input:0,output:0,reasoning:0,cache:{read:0,write:0}},cost:0}} as any);return yield* handle.process({sessionID:"session",model,agentID:"main",agent,user,system:[],prebuiltSystem:["fixture"],messages:[{role:"user",content:"fixture"}],tools:{},requestContext:{actorExecution:turn}} as any)})
    const result=await Effect.runPromise(program.pipe(Effect.provide(layer)))
    expect(result).toBe("stop");expect(controlCalls).toBe(1);expect(consumedBeforeControl).toBe(true);expect(positiveTakes).toBe(1);expect(sends).toBe(1)
    await activation.release()
  } finally {takeSpy.mockRestore();ManagedProvider.resetForTest();if(old===undefined)delete process.env.OPEN_CLANK_MANAGED;else process.env.OPEN_CLANK_MANAGED=old}
})
