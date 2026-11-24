import {expect,test} from 'bun:test'
import {createHash} from 'node:crypto'
import {Effect,Layer,ManagedRuntime,Scope,Exit} from 'effect'
import {sql} from 'drizzle-orm'
import {AppFileSystem} from '@mimo-ai/shared/filesystem'
import * as CrossSpawnSpawner from '../../src/effect/cross-spawn-spawner'
import {Agent} from '../../src/agent/agent'
import {Instruction} from '../../src/session/instruction'
import {LSP} from '../../src/lsp'
import {Truncate} from '../../src/tool'
import {ReadTool} from '../../src/tool/read'
import {ProviderTest} from '../fake/provider'
import path from 'node:path'
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
import {readReadView,readReadViewProfile,transferReadView,framedConfigReadView} from '../../src/session/tool-view-read'
import {decodeRepeatedLines} from '../../src/session/tool-view-repeated-lines'
import {prepareToolViews} from '../../src/session/tool-view-assembly'
const runtime=ManagedRuntime.make(Layer.mergeAll(Agent.defaultLayer,AppFileSystem.defaultLayer,CrossSpawnSpawner.defaultLayer,Instruction.defaultLayer,LSP.defaultLayer,Truncate.defaultLayer,ProviderTest.fake().layer))

test('complete config Read reuses admitted exact codecs and History',async()=>{
 await using tmp=await tmpdir()
 await Instance.provide({directory:tmp.path,fn:async()=>{
  const now=Date.now(),sessionID='ses_read_view',messageID='msg_read_view',partID='prt_read_view'
  Database.use(db=>{db.insert(ProjectTable).values({id:'proj_read_view' as any,worktree:tmp.path,sandboxes:[] as any,time_created:now,time_updated:now} as any).run();db.insert(SessionTable).values({id:sessionID as any,project_id:'proj_read_view' as any,slug:'read-view',directory:tmp.path,title:'read-view',version:'1',time_created:now,time_updated:now}).run();db.insert(MessageTable).values({id:messageID as any,session_id:sessionID as any,agent_id:'main',data:{role:'assistant'} as any,time_created:now,time_updated:now}).run();if(!db.all<{name:string}>(sql`PRAGMA table_info(part)`).some(x=>x.name==='openclank_view_decision'))db.run(sql`ALTER TABLE part ADD COLUMN openclank_view_decision TEXT`)})
  const identity={sessionID,rootOperationID:'root',actorID:'main',executionRevision:1,innerTurnID:'turn'} as any
  const route={rootOperationID:'root',connectionID:'conn',providerID:'provider',modelID:'model',modelRouteID:'route'}
  const admission={version:1,requested:true,...route,sessionID,hostAccountID:'account',holderID:'holder',generation:2,settingsRevision:'a'.repeat(64),routeMethodKey:'route-key',profile:'json-whitespace-v1'}
  const encoded=encodeToolViewAdmission(admission,sessionID,route,{providerID:'provider',modelID:'model'})!
  const ctx={sessionID,messageID,callID:'call',agent:'build',abort:AbortSignal.any([]),messages:[],metadata:()=>Effect.void,ask:()=>Effect.void} as any
  bindToolViewProducer(ctx,enabledToolViewProfiles(encoded,'read') as any,()=>true)
  const scope=await Effect.runPromise(Scope.make())
  const reader=await runtime.runPromise(ReadTool.pipe(Effect.flatMap(info=>info.init()),Effect.provideService(Scope.Scope,scope)))
  const filename=path.join(tmp.path,'settings.json'), raw=('{\n'+Array.from({length:100},(_,i)=>`  "k${i}": 1.00,\n`).join('')+'  "k1": "literal  space"\n}')
  await Bun.write(filename,raw)
  const result=await runtime.runPromise(Effect.scoped(reader.execute({file_path:filename},ctx)));expect(readReadViewProfile(result)).toBe('read-json-whitespace-v1')
  const final={...result,metadata:{...result.metadata}};transferReadView(result,final);expect(readReadViewProfile(final)).toBe('read-json-whitespace-v1')
  const partial=await runtime.runPromise(Effect.scoped(reader.execute({file_path:filename,limit:1},ctx)));expect(readReadView(partial)).toBe(false)
  const changed={...final,output:final.output+'changed'};transferReadView(result,changed);expect(readReadView(changed)).toBe(false)
  const running={id:partID,sessionID,messageID,type:'tool',tool:'read',callID:'call',state:{status:'running',input:{},time:{start:now}}} as any
  const finished={...running,state:{status:'completed',input:{},output:final.output,title:final.title,metadata:final.metadata,time:{start:now,end:now+1}}} as any
  await AppRuntime.runPromise(Session.Service.use(s=>s.updatePart(running)))
  const issuer=createToolTimingIssuer();markAdmittedToolTimingTurn(identity,identity);const timing=captureToolTiming(issuer,identity,running)!
  const capture=captureToolView(encoded,timing,finished,route,readReadViewProfile(final))!
  expect(capture.admission.profile).toBe('read-json-whitespace-v1')
  await AppRuntime.runPromise(Session.Service.use(s=>s.updateTimedTool(finished,timing,capture)))
  const stored=JSON.parse(Database.use(db=>db.get<{v:string}>(sql`SELECT openclank_view_decision AS v FROM part WHERE id=${partID}`)!.v));expect(stored.version).toBe(1)
  const digest=createHash('sha256').update(final.output).digest('hex')
  const assembled=await prepareToolViews({encoded,identity,issuer,route,control:{disabled:false},historyAllowed:true,messages:[{info:{id:messageID,role:'assistant'},parts:[finished]}] as any,current:()=>true,ack:async()=>({version:1,accountID:'account',actorID:'main',chatID:'chat',sessionID,messageID,partID,revision:1,contentHash:stored.contentSHA256,outputSHA256:digest,outputBytes:Buffer.byteLength(final.output)}),check:async(refs)=>refs.length===1})
  const frame=assembled.views.get(partID)!;expect(frame).toContain('history(');expect(frame).toContain(stored.contentSHA256);expect(frame).toContain('"k1":1.00');expect(frame).toContain('"k1":"literal  space"');expect(finished.state.output).toBe(final.output)
  const profiles={jsonWhitespace:true,terminalAnsi:false,htmlMarkdown:false,structuredSearch:false,repeatedLines:true,unifiedDiff:false}
  const combined=encodeToolViewAdmission({...admission,version:2,profile:undefined,profiles},sessionID,route,{providerID:'provider',modelID:'model'})
  expect(combined).toBeUndefined() // v2 has no v1 profile key
  const {profile:_profile,...base}=admission
  const encoded2=encodeToolViewAdmission({...base,version:2,profiles},sessionID,route,{providerID:'provider',modelID:'model'})!
  const ctx2={...ctx,callID:'repeat'};bindToolViewProducer(ctx2,enabledToolViewProfiles(encoded2,'read') as any,()=>true)
  const repeatFile=path.join(tmp.path,'settings.yaml'),repeatBody=['value: |',...Array(120).fill('  # literal block content')].join('\n');await Bun.write(repeatFile,repeatBody)
  const repeat=await runtime.runPromise(Effect.scoped(reader.execute({file_path:repeatFile},ctx2)));expect(readReadViewProfile(repeat)).toBe('read-repeated-lines-v1')
  const repeatFrame=framedConfigReadView(repeat.output,repeat.metadata,{messageID,partID,revision:1,contentHash:'a'.repeat(64)},'read-repeated-lines-v1')!
  expect(repeatFrame).toContain('history(');expect(decodeRepeatedLines(repeatFrame.slice(repeatFrame.indexOf('\n')+1))).toBe(repeatBody)
  const repeatRunning={...running,id:'prt_repeat',callID:'repeat'} as any
  const repeatPart={...repeatRunning,state:{status:'completed',input:{},output:repeat.output,title:repeat.title,metadata:repeat.metadata,time:{start:now,end:now+1}}} as any
  await AppRuntime.runPromise(Session.Service.use(s=>s.updatePart(repeatRunning)))
  const repeatTiming=captureToolTiming(issuer,identity,repeatRunning)!,repeatCapture=captureToolView(encoded2,repeatTiming,repeatPart,route,readReadViewProfile(repeat))!
  await AppRuntime.runPromise(Session.Service.use(s=>s.updateTimedTool(repeatPart,repeatTiming,repeatCapture)))
  const repeatStored=JSON.parse(Database.use(db=>db.get<{v:string}>(sql`SELECT openclank_view_decision AS v FROM part WHERE id=${'prt_repeat'}`)!.v))
  const repeatAssembled=await prepareToolViews({encoded:encoded2,identity,issuer,route,control:{disabled:false},historyAllowed:true,messages:[{info:{id:messageID,role:'assistant'},parts:[repeatPart]}] as any,current:()=>true,ack:async()=>({version:1,accountID:'account',actorID:'main',chatID:'chat',sessionID,messageID,partID:'prt_repeat',revision:1,contentHash:repeatStored.contentSHA256,outputSHA256:createHash('sha256').update(repeat.output).digest('hex'),outputBytes:Buffer.byteLength(repeat.output)}),check:async()=>true})
  expect(decodeRepeatedLines(repeatAssembled.views.get('prt_repeat')!.split('\n').slice(1).join('\n'))).toBe(repeatBody)

  const short=path.join(tmp.path,'short.json');await Bun.write(short,'{"a": 1}')
  const shortRead=await runtime.runPromise(Effect.scoped(reader.execute({file_path:short},ctx)));expect(framedConfigReadView(shortRead.output,shortRead.metadata,{messageID,partID,revision:1,contentHash:'a'.repeat(64)},'read-json-whitespace-v1')).toBeUndefined()
  expect(enabledToolViewProfiles(undefined,'read')).toEqual([]);
issuer.revoke();await Effect.runPromise(Scope.close(scope,Exit.void))
 }})
})
