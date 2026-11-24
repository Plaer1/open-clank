import { expect, test } from 'bun:test'
import { createHash } from 'node:crypto'
import { readFileSync, existsSync } from 'node:fs'
import { Effect, Layer } from 'effect'
import { GcfPreviewTool } from '../../src/tool/gcf-preview'
import { Truncate } from '../../src/tool'
import { Agent } from '../../src/agent/agent'
import * as CrossSpawnSpawner from '../../src/effect/cross-spawn-spawner'
import { testEffect } from '../lib/effect'
import { provideTmpdirInstance } from '../fixture/fixture'
import { SessionID, MessageID } from '../../src/session/schema'
import { ManagedProvider } from '../../src/acp/managed-provider'
import { createToolTimingIssuer, installToolTimingIssuer, markAdmittedToolTimingTurn, retireAdmittedToolTimingTurn } from '../../src/session/tool-timing'
import { createGcfEncoder, encodeGraphStream, graphStreamAvailable } from '../../src/session/gcf-native'

// Real addon: GCF_GRAPH_STREAM_ADDON (+ _SHA). Old addon for the unchanged-direction comparison: GCF_OLD_ADDON.
const addonPath=process.env.GCF_GRAPH_STREAM_ADDON, oldPath=process.env.GCF_OLD_ADDON
const haveAddons=!!addonPath&&!!oldPath&&existsSync(addonPath)&&existsSync(oldPath)
const load=(file:string)=>{const sha=createHash('sha256').update(readFileSync(file)).digest('hex');const mod={exports:{}} as NodeModule;process.dlopen(mod,file);return {native:mod.exports as any,sha}}
const artifactOf=(l:{native:any;sha:string},withGraph:boolean)=>createGcfEncoder(l.native.encodeV1,l.native.codecInfoV1().codecVersion,l.sha,l.native.decodeV1,l.native.encodePreviewV1,l.native.createGenericStreamV1,withGraph?l.native.createGraphStreamV1:undefined)
const graph={symbols:[
 {id:'a',kind:'function',name:'main',file:'src/m.ts',line:1,signature:'main(): void'},
 {id:'b',kind:'class',name:'B|"q"',file:null,line:null,signature:null}],
 edges:[{from:'a',to:'b',relation:'uses',line:4},{from:'b',to:'b',relation:'contains',line:null}]}
const toEvents=(g:any)=>[...g.symbols.map((s:any)=>({op:'symbol',...s})),...g.edges.map((e:any)=>({op:'edge',...e}))]
async function* source(events:unknown[]){for(const e of events) yield e}
const collect=async(events:unknown[],artifact:any)=>{const chunks:Buffer[]=[];for await(const i of encodeGraphStream(source(events),artifact)) chunks.push(i.chunk);return Buffer.concat(chunks)}

test('graph stream capability is optional: an addon without the factory reports unavailable',async()=>{
 const encoder=(input:Buffer)=>({state:'bypass',reason:'not_smaller',inputBytes:input.length,outputBytes:input.length})
 const old=createGcfEncoder(encoder,'v','a'.repeat(64))
 expect(graphStreamAvailable(old)).toBe(false)
 await expect(collect(toEvents(graph),old)).rejects.toThrow('Stream unavailable')
 let aborted=0;const fake=createGcfEncoder(encoder,'v','b'.repeat(64),undefined,undefined,undefined,()=>({push:(b:Buffer)=>({state:'provisional',chunk:Buffer.from(b)}),end:()=>({state:'complete',chunk:Buffer.from('E')}),abort:()=>{aborted++}}))
 expect(graphStreamAvailable(fake)).toBe(true)
 expect((await collect([{op:'symbol',id:'a'}],fake)).toString()).toBe('{"op":"symbol","id":"a"}\nE')
 await expect(collect([{op:'symbol',id:{x:1}}],fake)).rejects.toThrow();await expect(collect([{op:'other'}],fake)).rejects.toThrow()
 expect(aborted).toBe(2)
 fake.revoke();expect(graphStreamAvailable(fake)).toBe(false)
})

const it=testEffect(Layer.mergeAll(Truncate.defaultLayer,Agent.defaultLayer,CrossSpawnSpawner.defaultLayer))
// Runs `run` inside the same admitted actor/original-prompt context the generic stream tool test uses.
async function session(tool:any,artifact:any,run:(t:{execute:(args:any)=>Promise<any>;setLive:(v:boolean)=>void})=>Promise<void>){
  const prior=process.env.OPEN_CLANK_MANAGED;process.env.OPEN_CLANK_MANAGED='1';ManagedProvider.resetForTest()
  const sessionID=SessionID.make('session'),identity={sessionID,rootOperationID:'root',actorID:'main',executionRevision:1,innerTurnID:'turn'}
  const bootstrap={stableChatID:'chat',owner:'alice',rootSessionID:sessionID,sessionID,actorID:'main',rootOperationID:'root',expectedExecutionRevision:0,mapRevision:1,mappingRevision:1,workspaceRevision:1,modelRouteID:'route'}
  const binding={bindingID:'binding',bindingRevision:1,rootOperationID:'root',scopeKind:'actor_execution',sessionID,actorID:'main',executionRevision:1,modelRouteID:'route',connectionID:'conn',providerID:'local',billingLane:'local',modelID:'model',credentialRequired:false,source:'keyless',attempt:1,committed:false}
  const host:any={async extMethod(method:string){if(method.endsWith('/binding/read'))return {engineSessionID:sessionID,stableChatID:'chat',owner:'alice',canonicalCwd:'/tmp',workspaceRevision:1,authorityWorkspaceID:'workspace',memoryWorkspaceID:'workspace',copalWorkspace:'workspace',memoryEnabled:false,engineAliases:[],mapRevision:1,mappingRevision:1};if(method.endsWith('/account/bind'))return binding;if(method.endsWith('/turn/start'))return {...identity,accepted:true};throw new Error('synthetic unavailable')}}
  const issuer=createToolTimingIssuer();installToolTimingIssuer(host,issuer);ManagedProvider.installHostConnection(host)
  let live=true, asks=0,abortOnAsk=false
  try {
   await ManagedProvider.withOriginalPromptHostContext(sessionID,'root',host,()=>live,()=>({chatID:'chat',gcfArtifact:artifact}),async()=>{
    const activation=await ManagedProvider.activateActorOperation({sessionID,actorID:'main',executionRevision:1,rootOperationID:'root'}, {connectionID:'conn',providerID:'local',billingLane:'local',modelID:'model',modelRouteID:'route',bootstrap} as any,{providerID:'conn',modelID:'model'},{chatID:'chat',gcfArtifact:artifact})
    markAdmittedToolTimingTurn(identity,identity)
    try {
     const controller=new AbortController()
     const ctx={sessionID:SessionID.make(sessionID),messageID:MessageID.make(''),callID:'call',agent:'build',abort:controller.signal,messages:[],extra:{actorExecution:identity},metadata:()=>Effect.void,ask:()=>Effect.sync(()=>{asks++;if(abortOnAsk)controller.abort()})}
     const execute=(args:any,context:any=ctx)=>Effect.runPromise(tool.execute(args,context as any))
     await run({execute:(args:any)=>execute(args),setLive:(v:boolean)=>{live=v}})
    } finally {retireAdmittedToolTimingTurn(identity);await activation.release()}
   })
  } finally {issuer.revoke();ManagedProvider.resetForTest();if(prior===undefined)delete process.env.OPEN_CLANK_MANAGED;else process.env.OPEN_CLANK_MANAGED=prior}
}
// Skipped (never thrown, never silently passed) unless both private addon builds exist.
;(haveAddons?it.live:it.live.skip)('encode_graph_stream: real addon round trip, refusals, old addon, existing direction unchanged (needs GCF_GRAPH_STREAM_ADDON and GCF_OLD_ADDON)',()=>provideTmpdirInstance(()=>Effect.gen(function*(){
 const info=yield* GcfPreviewTool;const tool=yield* info.init()
 yield* Effect.promise(async()=>{
  const next=load(addonPath!),old=load(oldPath!)
  expect(old.native.createGraphStreamV1).toBeUndefined()
  const withGraph=artifactOf(next,true)
  // direct typed stream: decode(encode(x)) equals x, field for field
  const frame=await collect(toEvents(graph),withGraph)
  const decoded=next.native.decodeGraphStreamV1(frame,'gcf-graph-stream-v1')
  expect(JSON.parse(Buffer.from(decoded.output).toString())).toEqual(graph)
  expect(frame.toString()).toContain('## symbols');expect(frame.toString()).toContain('##! summary counts=2,2')
  expect(()=>next.native.decodeGraphStreamV1(Buffer.from(frame.toString().replace('counts=2,2','counts=2,3')),'gcf-graph-stream-v1')).toThrow()
  for(const bad of [{...graph,edges:[{from:'a',to:'zz',relation:'uses',line:null}]},{symbols:[graph.symbols[0],graph.symbols[0]],edges:[]},{symbols:[{...graph.symbols[0],kind:'gadget'}],edges:[]},{symbols:[{...graph.symbols[0],line:1.5}],edges:[]},{symbols:[{...graph.symbols[0],extra:1}],edges:[]}])
   await expect(collect(toEvents(bad),withGraph)).rejects.toThrow()
  const text=JSON.stringify(graph),rows=JSON.stringify({items:[{id:'alpha',count:1},{id:'beta',count:2}]})
  let streamOld:string,streamNew:string
  await session(tool,withGraph,async({execute,setLive})=>{
   const ok=await execute({direction:'encode_graph_stream',text})
   expect(ok.metadata.state).toBe('preview');expect(ok.metadata.policy).toBe('gcf-graph-stream-v1');expect(ok.metadata.buffered).toBe(true);expect(ok.output).toBe(frame.toString())
   for(const bad of [JSON.stringify({symbols:[],edges:[{from:'x',to:'y',relation:'calls',line:null}]}),JSON.stringify({symbols:[{...graph.symbols[0],line:1.5}],edges:[]}),JSON.stringify({symbols:[{...graph.symbols[0],kind:'gadget'}],edges:[]}),JSON.stringify({symbols:[graph.symbols[0],graph.symbols[0]],edges:[]}),'{"symbols":[],"edges":[],"x":[]}','[]'])
    { const r=await execute({direction:'encode_graph_stream',text:bad});expect(r.title).toBe('GCF preview refused');expect(r.metadata.state).toBe('refused');expect(r.metadata.outputBytes).toBe(0) }
   expect((await execute({direction:'encode_graph_stream',text:JSON.stringify({symbols:[],edges:[]})})).metadata.state).toBe('preview')
   streamNew=(await execute({direction:'encode_stream',text:rows})).output
   setLive(false);expect((await execute({direction:'encode_graph_stream',text})).metadata.state).toBe('unavailable');setLive(true)
  })
  await session(tool,artifactOf(old,false),async({execute})=>{
   const r=await execute({direction:'encode_graph_stream',text});expect(r.metadata.state).toBe('unavailable');expect(r.output).toBe('Verified GCF preview is unavailable for this context.')
   streamOld=(await execute({direction:'encode_stream',text:rows})).output
  })
  expect(streamOld!).toBe(streamNew!);expect(streamNew!).toContain('##! summary counts=2')
 })
})))
