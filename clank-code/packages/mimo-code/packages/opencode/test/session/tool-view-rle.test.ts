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
import {encodeRepeatedLines,decodeRepeatedLines,framedRepeatedLines} from '../../src/session/tool-view-repeated-lines'
import {prepareToolViews} from '../../src/session/tool-view-assembly'
const runtime=ManagedRuntime.make(Layer.mergeAll(CrossSpawnSpawner.defaultLayer,AppFileSystem.defaultLayer,Plugin.defaultLayer,Truncate.defaultLayer,Agent.defaultLayer))

test('actual Bash producer selects bounded exact repeated-line view and preserves original',async()=>{
 await using tmp=await tmpdir()
 await Instance.provide({directory:tmp.path,fn:async()=>{
  const now=Date.now(),sessionID='ses_rle',messageID='msg_rle',partID='prt_rle'
  Database.use(db=>{db.insert(ProjectTable).values({id:'proj_rle' as any,worktree:tmp.path,sandboxes:[] as any,time_created:now,time_updated:now} as any).run();db.insert(SessionTable).values({id:sessionID as any,project_id:'proj_rle' as any,slug:'rle',directory:tmp.path,title:'rle',version:'1',time_created:now,time_updated:now}).run();db.insert(MessageTable).values({id:messageID as any,session_id:sessionID as any,agent_id:'main',data:{role:'assistant'} as any,time_created:now,time_updated:now}).run();if(!db.all<{name:string}>(sql`PRAGMA table_info(part)`).some(x=>x.name==='openclank_view_decision'))db.run(sql`ALTER TABLE part ADD COLUMN openclank_view_decision TEXT`)})
  const identity={sessionID,rootOperationID:'root',actorID:'main',executionRevision:1,innerTurnID:'turn'} as any
  const route={rootOperationID:'root',connectionID:'conn',providerID:'provider',modelID:'model',modelRouteID:'route'}
  const admission={version:2,requested:true,...route,sessionID,hostAccountID:'account',holderID:'holder',generation:2,settingsRevision:'a'.repeat(64),routeMethodKey:'route-key',profiles:{jsonWhitespace:false,terminalAnsi:true,htmlMarkdown:false,structuredSearch:false,repeatedLines:true,unifiedDiff:true}}
  const encoded=encodeToolViewAdmission(admission,sessionID,route,{providerID:'provider',modelID:'model'})!
  const ctx={sessionID,messageID,callID:'call',agent:'build',abort:AbortSignal.any([]),messages:[],metadata:()=>Effect.void,ask:()=>Effect.void} as any
  bindToolViewProducer(ctx,enabledToolViewProfiles(encoded,'bash') as any,()=>true)
  const bash=await runtime.runPromise(BashTool.pipe(Effect.flatMap(info=>info.init())))
  const result=await runtime.runPromise(bash.execute({command:"for i in $(seq 1 120); do printf 'repeatable log line 1234567890\\n'; done",description:'Print repeated fixture lines'},ctx))
  expect(readBashView(result)).toBe('repeated-lines-v1')
  const final={...result,metadata:{...result.metadata}};transferBashView(result,final)
  expect(readBashView(final)).toBe('repeated-lines-v1')
  const representation=encodeRepeatedLines(final.output)!
  expect(decodeRepeatedLines(representation)).toBe(final.output)
  const running={id:partID,sessionID,messageID,type:'tool',tool:'bash',callID:'call',state:{status:'running',input:{},time:{start:now}}} as any
  const finished={...running,state:{status:'completed',input:{},output:final.output,title:final.title,metadata:final.metadata,time:{start:now,end:now+1}}} as any
  await AppRuntime.runPromise(Session.Service.use(s=>s.updatePart(running)))
  const issuer=createToolTimingIssuer();markAdmittedToolTimingTurn(identity,identity);const timing=captureToolTiming(issuer,identity,running)!
  const capture=captureToolView(encoded,timing,finished,route,readBashView(final))!
  expect(capture.admission.profile).toBe('repeated-lines-v1')
  await AppRuntime.runPromise(Session.Service.use(s=>s.updateTimedTool(finished,timing,capture)))
  const stored=JSON.parse(Database.use(db=>db.get<{v:string}>(sql`SELECT openclank_view_decision AS v FROM part WHERE id=${partID}`)!.v))
  let checked=0;const digest=createHash('sha256').update(final.output).digest('hex')
  const assembled=await prepareToolViews({encoded,identity,issuer,route,control:{disabled:false},historyAllowed:true,messages:[{info:{id:messageID,role:'assistant'},parts:[finished]}] as any,current:()=>true,ack:async()=>({version:1,accountID:'account',actorID:'main',chatID:'chat',sessionID,messageID,partID,revision:1,contentHash:stored.contentSHA256,outputSHA256:digest,outputBytes:Buffer.byteLength(final.output)}),check:async(refs)=>{checked++;return refs.length===1}})
  expect(checked).toBe(1);expect(assembled.views.get(partID)).toContain('Exact repeated-line view');expect(finished.state.output).toBe(final.output)
  const diff='diff --git a/a b/a\nindex 1234..5678 100644\n--- a/a\n+++ b/a\n@@ -1,3 +1,3 @@\n same\n-old\n+new\n same\n'
  expect(selectBashView(diff,{diff:true,ansi:true,repetition:true})).toBe('unified-diff-v1')
  expect(selectBashView('\u001b[31mred\u001b[0m',{diff:true,ansi:true,repetition:true})).toBe('terminal-ansi-v1')
  expect(encodeRepeatedLines('x\nx\n<protected>')).toBeUndefined();expect(encodeRepeatedLines('x\nx\n\u001b[2J')).toBeUndefined()
  expect(framedRepeatedLines('x\nx\n',{messageID,partID,revision:1,contentHash:'a'.repeat(64)})).toBeUndefined()
  expect(decodeRepeatedLines(encodeRepeatedLines('é\né\nlast')!)).toBe('é\né\nlast')
  expect(decodeRepeatedLines('{"version":1,"lines":[[999999,"x\\n"]]}')).toBeUndefined()
  const altered={...final,output:final.output+'x'};transferBashView(result,altered);expect(readBashView(altered)).toBeUndefined()
  expect(enabledToolViewProfiles(undefined,'bash')).toEqual([]);issuer.revoke()
 }})
})
