import {expect,test} from 'bun:test'
import {createHash} from 'node:crypto'
import {Effect,Layer,ManagedRuntime} from 'effect'
import {sql} from 'drizzle-orm'
import {AppFileSystem} from '@mimo-ai/shared/filesystem'
import * as CrossSpawnSpawner from '../../src/effect/cross-spawn-spawner'
import {Plugin} from '../../src/plugin'
import {Truncate} from '../../src/tool'
import {Agent} from '../../src/agent/agent'
import {BashTool} from '../../src/tool/bash'
import {Database} from '../../src/storage'
import {ProjectTable} from '../../src/project/project.sql'
import {SessionTable,MessageTable} from '../../src/session/session.sql'
import {Session} from '../../src/session'
import {AppRuntime} from '../../src/effect/app-runtime'
import {Instance} from '../../src/project/instance'
import {tmpdir} from '../fixture/fixture'
import {createToolTimingIssuer,markAdmittedToolTimingTurn,captureToolTiming} from '../../src/session/tool-timing'
import {encodeToolViewAdmission,captureToolView,enabledToolViewProfiles} from '../../src/session/tool-view-source'
import {bindToolViewProducer} from '../../src/session/tool-view-producer-admission'
import {readBashView,selectBashView,transferBashView} from '../../src/session/tool-view-bash-proof'
import {jsonShape} from '../../src/session/tool-view-json-shape'
import {prepareToolViews} from '../../src/session/tool-view-assembly'
const runtime=ManagedRuntime.make(Layer.mergeAll(CrossSpawnSpawner.defaultLayer,AppFileSystem.defaultLayer,Plugin.defaultLayer,Truncate.defaultLayer,Agent.defaultLayer))

test('actual Bash JSON shape preserves original recovery and rejects unsupported input',async()=>{
 await using tmp=await tmpdir()
 await Instance.provide({directory:tmp.path,fn:async()=>{
  const now=Date.now(),sessionID='ses_rle',messageID='msg_rle',partID='prt_rle'
  Database.use(db=>{db.insert(ProjectTable).values({id:'proj_rle' as any,worktree:tmp.path,sandboxes:[] as any,time_created:now,time_updated:now} as any).run();db.insert(SessionTable).values({id:sessionID as any,project_id:'proj_rle' as any,slug:'rle',directory:tmp.path,title:'rle',version:'1',time_created:now,time_updated:now}).run();db.insert(MessageTable).values({id:messageID as any,session_id:sessionID as any,agent_id:'main',data:{role:'assistant'} as any,time_created:now,time_updated:now}).run();if(!db.all<{name:string}>(sql`PRAGMA table_info(part)`).some(x=>x.name==='openclank_view_decision'))db.run(sql`ALTER TABLE part ADD COLUMN openclank_view_decision TEXT`)})
  const identity={sessionID,rootOperationID:'root',actorID:'main',executionRevision:1,innerTurnID:'turn'} as any
  const route={rootOperationID:'root',connectionID:'conn',providerID:'provider',modelID:'model',modelRouteID:'route'}
  const admission={version:2,requested:true,...route,sessionID,hostAccountID:'account',holderID:'holder',generation:2,settingsRevision:'a'.repeat(64),routeMethodKey:'route-key',profiles:{jsonWhitespace:false,terminalAnsi:true,htmlMarkdown:false,structuredSearch:false,repeatedLines:false,unifiedDiff:false,schemaAnnotations:false,toon:false,sourceOutlines:false,tap13:false,base64Lines:false,ndjsonLogs:false,jsonShape:true}}
  const encoded=encodeToolViewAdmission(admission,sessionID,route,{providerID:'provider',modelID:'model'})!
  const ctx={sessionID,messageID,callID:'call',agent:'build',abort:AbortSignal.any([]),messages:[],metadata:()=>Effect.void,ask:()=>Effect.void} as any
  bindToolViewProducer(ctx,enabledToolViewProfiles(encoded,'bash') as any,()=>true)
  const bash=await runtime.runPromise(BashTool.pipe(Effect.flatMap(info=>info.init())))
  const line=JSON.stringify({records:Array.from({length:30},(_,i)=>({id:i,name:`record ${i}`}))},null,4)+'\n'
  const result=await runtime.runPromise(bash.execute({command:`printf '%s' '${line}'`,description:'Print JSON fixture'},ctx))
  expect(readBashView(result)).toBe('json-shape-v1')
  const final={...result,metadata:{...result.metadata}};transferBashView(result,final)
  expect(readBashView(final)).toBe('json-shape-v1')
  expect(jsonShape(final.output)?.omittedScalars).toBe(60)
  const running={id:partID,sessionID,messageID,type:'tool',tool:'bash',callID:'call',state:{status:'running',input:{},time:{start:now}}} as any
  const finished={...running,state:{status:'completed',input:{},output:final.output,title:final.title,metadata:final.metadata,time:{start:now,end:now+1}}} as any
  await AppRuntime.runPromise(Session.Service.use(s=>s.updatePart(running)))
  const issuer=createToolTimingIssuer();markAdmittedToolTimingTurn(identity,identity);const timing=captureToolTiming(issuer,identity,running)!
  expect(captureToolView(encoded,timing,finished,route,undefined)).toBeUndefined()
  const capture=captureToolView(encoded,timing,finished,route,readBashView(final))!
  expect(capture.admission.profile).toBe('json-shape-v1')
  await AppRuntime.runPromise(Session.Service.use(s=>s.updateTimedTool(finished,timing,capture)))
  const stored=JSON.parse(Database.use(db=>db.get<{v:string}>(sql`SELECT openclank_view_decision AS v FROM part WHERE id=${partID}`)!.v))
  let checked=0;const digest=createHash('sha256').update(final.output).digest('hex')
  const assembled=await prepareToolViews({encoded,identity,issuer,route,control:{disabled:false},historyAllowed:true,messages:[{info:{id:messageID,role:'assistant'},parts:[finished]}] as any,current:()=>true,ack:async()=>({version:1,accountID:'account',actorID:'main',chatID:'chat',sessionID,messageID,partID,revision:1,contentHash:stored.contentSHA256,outputSHA256:digest,outputBytes:Buffer.byteLength(final.output)}),check:async(refs)=>{checked++;return refs.length===1}})
  expect(checked).toBe(1);const frame=assembled.views.get(partID)!
  expect(frame).toContain('scalar values omitted and keys remain visible');expect(frame).toContain('not redaction or faithful serialization');expect(frame).toContain('history(');expect(frame).toContain(stored.contentSHA256);expect(frame).not.toContain('record 0');expect(finished.state.output).toBe(final.output)
  const shape=JSON.parse(frame.slice(frame.indexOf('\n')+1));expect(shape[0]).toBe('object');expect(shape[1][0][0]).toBe('records');expect(shape[1][0][1][1]).toBe(30)
  const stale=await prepareToolViews({encoded,identity,issuer,route,control:{disabled:false},historyAllowed:true,messages:[{info:{id:messageID,role:'assistant'},parts:[finished]}] as any,current:()=>false,ack:async()=>{throw new Error('unexpected ack')},check:async()=>true});expect(stale.views.has(partID)).toBe(false)
  expect(jsonShape('{"missing":')).toBeUndefined();expect(jsonShape('{"a":1,"a":2}')).toBeUndefined();expect(jsonShape('{"a":-0}')).toBeUndefined();expect(jsonShape('{"a":1.5}')).toBeUndefined();expect(jsonShape('{"a":1e2}')).toBeUndefined()
  expect(jsonShape(line.slice(0,-2))).toBeUndefined()
  const short='{"a":1}'
  const smallRunning={...running,id:'prt_small',callID:'small'} as any
  const smallCtx={...ctx,callID:'small'};bindToolViewProducer(smallCtx,enabledToolViewProfiles(encoded,'bash') as any,()=>true)
  const small=await runtime.runPromise(bash.execute({command:`printf '%s' '${short}'`,description:'Print short JSON'},smallCtx))
  const smallPart={...smallRunning,state:{status:'completed',input:{},output:small.output,title:small.title,metadata:small.metadata,time:{start:now,end:now+1}}} as any
  await AppRuntime.runPromise(Session.Service.use(s=>s.updatePart(smallRunning)))
  const smallTiming=captureToolTiming(issuer,identity,smallRunning)!
  const smallCapture=captureToolView(encoded,smallTiming,smallPart,route,readBashView(small))!
  await AppRuntime.runPromise(Session.Service.use(s=>s.updateTimedTool(smallPart,smallTiming,smallCapture)))
  const smallStored=JSON.parse(Database.use(db=>db.get<{v:string}>(sql`SELECT openclank_view_decision AS v FROM part WHERE id=${'prt_small'}`)!.v))
  const smallView=await prepareToolViews({encoded,identity,issuer,route,control:{disabled:false},historyAllowed:true,messages:[{info:{id:messageID,role:'assistant'},parts:[smallPart]}] as any,current:()=>true,ack:async()=>({version:1,accountID:'account',actorID:'main',chatID:'chat',sessionID,messageID,partID:'prt_small',revision:1,contentHash:smallStored.contentSHA256,outputSHA256:createHash('sha256').update(small.output).digest('hex'),outputBytes:Buffer.byteLength(small.output)}),check:async()=>true})
  expect(smallView.views.has('prt_small')).toBe(false);expect(smallPart.state.output).toBe(short)
  expect(selectBashView(final.output,{diff:false,ansi:false,repetition:false,shape:true})).toBe('json-shape-v1')
  const altered={...final,output:final.output+'x'};transferBashView(result,altered);expect(readBashView(altered)).toBeUndefined()
  const partial={...final,metadata:{...final.metadata,truncated:true}};transferBashView(result,partial);expect(readBashView(partial)).toBeUndefined()
  expect(enabledToolViewProfiles(undefined,'bash')).toEqual([])
  const {jsonShape:_shape,...oldProfiles}=admission.profiles;const old={...admission,profiles:oldProfiles}
  expect(encodeToolViewAdmission(old,sessionID,route,{providerID:'provider',modelID:'model'})).toBe(JSON.stringify(old))
  expect(encodeToolViewAdmission({...admission,profiles:{...admission.profiles,jsonShape:false}},sessionID,route,{providerID:'provider',modelID:'model'})).toBeUndefined()
  expect(encodeToolViewAdmission({...admission,profiles:{...admission.profiles,unknown:true}},sessionID,route,{providerID:'provider',modelID:'model'})).toBeUndefined()
  const both={...admission,version:3,lineCapRows:40,profiles:{...admission.profiles,lineCap:true}}
  const bothEncoded=encodeToolViewAdmission(both,sessionID,route,{providerID:'provider',modelID:'model'});expect(bothEncoded).toBe(JSON.stringify(both));expect(enabledToolViewProfiles(bothEncoded,'bash')).toContain('json-shape-v1');expect(enabledToolViewProfiles(bothEncoded,'grep')).toContain('line-cap-v1');issuer.revoke()
 }})
})
