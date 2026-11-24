import {expect,test} from 'bun:test'
import {createHash} from 'node:crypto'
import {Effect,Layer,ManagedRuntime} from 'effect'
import {sql} from 'drizzle-orm'
import {AppFileSystem} from '@mimo-ai/shared/filesystem'
import * as CrossSpawnSpawner from '../../src/effect/cross-spawn-spawner'
import {Plugin} from '../../src/plugin'
import {Truncate} from '../../src/tool'
import {Agent} from '../../src/agent/agent'
import {GrepTool} from '../../src/tool/grep'
import {Ripgrep} from '../../src/file/ripgrep'
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
import {readGrepRows,transferGrepRows,framedGrepLineCap} from '../../src/session/tool-view-grep'
import {inspectToolView} from '../../src/session/tool-view-decision'
import {prepareToolViews} from '../../src/session/tool-view-assembly'
const runtime=ManagedRuntime.make(Layer.mergeAll(CrossSpawnSpawner.defaultLayer,AppFileSystem.defaultLayer,Ripgrep.defaultLayer,Plugin.defaultLayer,Truncate.defaultLayer,Agent.defaultLayer))

test('actual native grep cap freezes exact count and recovers full original',async()=>{
 await using tmp=await tmpdir()
 await Instance.provide({directory:tmp.path,fn:async()=>{
  const now=Date.now(),sessionID='ses_rle',messageID='msg_rle',partID='prt_rle'
  Database.use(db=>{db.insert(ProjectTable).values({id:'proj_rle' as any,worktree:tmp.path,sandboxes:[] as any,time_created:now,time_updated:now} as any).run();db.insert(SessionTable).values({id:sessionID as any,project_id:'proj_rle' as any,slug:'rle',directory:tmp.path,title:'rle',version:'1',time_created:now,time_updated:now}).run();db.insert(MessageTable).values({id:messageID as any,session_id:sessionID as any,agent_id:'main',data:{role:'assistant'} as any,time_created:now,time_updated:now}).run();if(!db.all<{name:string}>(sql`PRAGMA table_info(part)`).some(x=>x.name==='openclank_view_decision'))db.run(sql`ALTER TABLE part ADD COLUMN openclank_view_decision TEXT`)})
  const identity={sessionID,rootOperationID:'root',actorID:'main',executionRevision:1,innerTurnID:'turn'} as any
  const route={rootOperationID:'root',connectionID:'conn',providerID:'provider',modelID:'model',modelRouteID:'route'}
  const admission={version:3,lineCapRows:40,requested:true,...route,sessionID,hostAccountID:'account',holderID:'holder',generation:2,settingsRevision:'a'.repeat(64),routeMethodKey:'route-key',profiles:{jsonWhitespace:false,terminalAnsi:true,htmlMarkdown:false,structuredSearch:true,repeatedLines:false,unifiedDiff:false,schemaAnnotations:false,toon:false,sourceOutlines:false,tap13:false,base64Lines:false,ndjsonLogs:false,lineCap:true}}
  const encoded=encodeToolViewAdmission(admission,sessionID,route,{providerID:'provider',modelID:'model'})!
  const ctx={sessionID,messageID,callID:'call',agent:'build',abort:AbortSignal.any([]),messages:[],metadata:()=>Effect.void,ask:()=>Effect.void} as any
  bindToolViewProducer(ctx,enabledToolViewProfiles(encoded,'grep') as any,()=>true)
  const grep=await runtime.runPromise(GrepTool.pipe(Effect.flatMap(info=>info.init())))
  const filename=path.join(tmp.path,'sample.txt')
  await Bun.write(filename,Array.from({length:200},(_,i)=>`match entry ${i}`).join('\n')+'\n')
  const result=await runtime.runPromise(grep.execute({pattern:'match',path:tmp.path,limit:200},ctx))
  expect(readGrepRows(result)).toBe(true)
  const final={...result,metadata:{...result.metadata}};transferGrepRows(result,final)
  expect(readGrepRows(final)).toBe(true)
  const partial=await runtime.runPromise(grep.execute({pattern:'match',path:tmp.path,limit:1},ctx))
  expect(readGrepRows(partial)).toBe(false)
  const running={id:partID,sessionID,messageID,type:'tool',tool:'grep',callID:'call',state:{status:'running',input:{},time:{start:now}}} as any
  const finished={...running,state:{status:'completed',input:{},output:final.output,title:final.title,metadata:final.metadata,time:{start:now,end:now+1}}} as any
  await AppRuntime.runPromise(Session.Service.use(s=>s.updatePart(running)))
  const issuer=createToolTimingIssuer();markAdmittedToolTimingTurn(identity,identity);const timing=captureToolTiming(issuer,identity,running)!
  const capture=captureToolView(encoded,timing,finished,route,readGrepRows(final) ? 'line-cap-v1' : undefined)!
  expect(capture.admission.profile).toBe('line-cap-v1');expect(capture.admission.lineCapRows).toBe(40)
  await AppRuntime.runPromise(Session.Service.use(s=>s.updateTimedTool(finished,timing,capture)))
  const stored=JSON.parse(Database.use(db=>db.get<{v:string}>(sql`SELECT openclank_view_decision AS v FROM part WHERE id=${partID}`)!.v))
  expect(stored.version).toBe(2);expect(stored.lineCapRows).toBe(40);expect(inspectToolView({...capture.admission,lineCapRows:41}).state).toBe('out_of_scope')
  let checked=0;const digest=createHash('sha256').update(final.output).digest('hex')
  const assembled=await prepareToolViews({encoded,identity,issuer,route,control:{disabled:false},historyAllowed:true,messages:[{info:{id:messageID,role:'assistant'},parts:[finished]}] as any,current:()=>true,ack:async()=>({version:1,accountID:'account',actorID:'main',chatID:'chat',sessionID,messageID,partID,revision:1,contentHash:stored.contentSHA256,outputSHA256:digest,outputBytes:Buffer.byteLength(final.output)}),check:async(refs)=>{checked++;return refs.length===1}})
  expect(checked).toBe(1);const frame=assembled.views.get(partID)!
  expect(frame).toContain('retained 40, omitted 160, total 200');expect(frame).toContain('history(');expect(frame).toContain(stored.contentSHA256);expect(finished.state.output).toBe(final.output)
  const groups=JSON.parse(frame.slice(frame.indexOf('\n')+1)).groups;expect(groups.flatMap((g:any)=>g.rows).length).toBe(40);expect(groups[0].rows[0]).toEqual([final.metadata.items[0].line,final.metadata.items[0].text]);expect(groups.at(-1).rows.at(-1)).toEqual([final.metadata.items[39].line,final.metadata.items[39].text])
  const stale=await prepareToolViews({encoded,identity,issuer,route,control:{disabled:false},historyAllowed:true,messages:[{info:{id:messageID,role:'assistant'},parts:[finished]}] as any,current:()=>false,ack:async()=>{throw new Error('unexpected ack')},check:async()=>true});expect(stale.views.has(partID)).toBe(false)
  const unsupported={...final,metadata:{...final.metadata,unknown:true}};transferGrepRows(result,unsupported);expect(readGrepRows(unsupported)).toBe(false)
  const otherID='prt_cap_no_ack',running2={...running,id:otherID},finished2={...finished,id:otherID}
  await AppRuntime.runPromise(Session.Service.use(s=>s.updatePart(running2)))
  const timing2=captureToolTiming(issuer,identity,running2)!,capture2=captureToolView(encoded,timing2,finished2,route,'line-cap-v1')!
  await AppRuntime.runPromise(Session.Service.use(s=>s.updateTimedTool(finished2,timing2,capture2)))
  let ackCalls=0
  const missingAck=await prepareToolViews({encoded,identity,issuer,route,control:{disabled:false},historyAllowed:true,messages:[{info:{id:messageID,role:'assistant'},parts:[finished2]}] as any,current:()=>true,ack:async()=>{ackCalls++;return undefined},check:async()=>false})
  expect(ackCalls).toBe(1);expect(missingAck.views.has(otherID)).toBe(false);const denied=inspectToolView(capture2.admission);expect(denied.state==='present' && denied.decision.state).toBe('raw')
  const tiny=path.join(tmp.path,'tiny.txt');await Bun.write(tiny,'tiny-match one\ntiny-match two\n')
  const small=await runtime.runPromise(grep.execute({pattern:'tiny-match',path:tiny,limit:2},ctx));expect(readGrepRows(small)).toBe(true);expect(framedGrepLineCap(small.output,small.metadata,{messageID,partID,revision:1,contentHash:'a'.repeat(64)},1)).toBeUndefined()
  const altered={...final,output:final.output+'x'};transferGrepRows(result,altered);expect(readGrepRows(altered)).toBe(false)
  expect(enabledToolViewProfiles(undefined,'grep')).toEqual([]);expect(encodeToolViewAdmission({...admission,lineCapRows:0},sessionID,route,{providerID:'provider',modelID:'model'})).toBeUndefined();issuer.revoke()
 }})
})
