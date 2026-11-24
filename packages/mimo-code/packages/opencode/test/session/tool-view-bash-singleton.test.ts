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
import {prepareToolViews} from '../../src/session/tool-view-assembly'
const runtime=ManagedRuntime.make(Layer.mergeAll(CrossSpawnSpawner.defaultLayer,AppFileSystem.defaultLayer,Plugin.defaultLayer,Truncate.defaultLayer,Agent.defaultLayer))

test('sole enabled Bash profiles reach original History assembly',async()=>{
 await using tmp=await tmpdir()
 await Instance.provide({directory:tmp.path,fn:async()=>{
  const now=Date.now(),sessionID='ses_rle',messageID='msg_rle',partID='prt_rle'
  Database.use(db=>{db.insert(ProjectTable).values({id:'proj_rle' as any,worktree:tmp.path,sandboxes:[] as any,time_created:now,time_updated:now} as any).run();db.insert(SessionTable).values({id:sessionID as any,project_id:'proj_rle' as any,slug:'rle',directory:tmp.path,title:'rle',version:'1',time_created:now,time_updated:now}).run();db.insert(MessageTable).values({id:messageID as any,session_id:sessionID as any,agent_id:'main',data:{role:'assistant'} as any,time_created:now,time_updated:now}).run();if(!db.all<{name:string}>(sql`PRAGMA table_info(part)`).some(x=>x.name==='openclank_view_decision'))db.run(sql`ALTER TABLE part ADD COLUMN openclank_view_decision TEXT`)})
  const identity={sessionID,rootOperationID:'root',actorID:'main',executionRevision:1,innerTurnID:'turn'} as any
  const route={rootOperationID:'root',connectionID:'conn',providerID:'provider',modelID:'model',modelRouteID:'route'}
  const base={requested:true,...route,sessionID,hostAccountID:'account',holderID:'holder',generation:2,settingsRevision:'a'.repeat(64),routeMethodKey:'route-key'}
  const allOff={jsonWhitespace:false,terminalAnsi:false,htmlMarkdown:false,structuredSearch:false,repeatedLines:false,unifiedDiff:false,schemaAnnotations:false,toon:false,sourceOutlines:false,tap13:false,base64Lines:false,ndjsonLogs:false}
  const ndjson=Array.from({length:35},(_,i)=>JSON.stringify({timestamp:`2026-01-01T00:00:${String(i%60).padStart(2,'0')}Z`,level:'INFO',message:`event ${i}`})).join('\n')+'\n'
  const json='{\n'+Array.from({length:150},(_,i)=>`  "key${i}": 12345`).join(',\n')+'\n}'
  const table=JSON.stringify(Array.from({length:70},(_,i)=>({name:`fixture${i}`,status:'repeated-value',count:i})))
  const shape=JSON.stringify({records:Array.from({length:80},(_,i)=>({name:`fixture-long-value-${i}-${'x'.repeat(30)}`,status:'a'.repeat(40)}))})
  const cases=[['ndjsonLogs','ndjson-logs-v1',ndjson],['json','json-whitespace-v1',json],['toon','toon-v1',table],['jsonShape','json-shape-v1',shape]] as const
  const bash=await runtime.runPromise(BashTool.pipe(Effect.flatMap(info=>info.init())))
  const issuer=createToolTimingIssuer();markAdmittedToolTimingTurn(identity,identity)
  for (let i=0;i<cases.length;i++) {
    const [flag,expected,body]=cases[i]!, id=`prt_single_${i}`,callID=`call_${i}`
    const admission=flag==='json' ? {...base,version:1,profile:'json-whitespace-v1'} : {...base,version:2,profiles:{...allOff,[flag]:true}}
    const encoded=encodeToolViewAdmission(admission,sessionID,route,{providerID:'provider',modelID:'model'})!
    const ctx={sessionID,messageID,callID,agent:'build',abort:AbortSignal.any([]),messages:[],metadata:()=>Effect.void,ask:()=>Effect.void} as any
    bindToolViewProducer(ctx,enabledToolViewProfiles(encoded,'bash') as any,()=>true)
    const result=await runtime.runPromise(bash.execute({command:`printf '%s' '${body}'`,description:'Singleton fixture'},ctx))
    expect(readBashView(result)).toBe(expected)
    const running={id,sessionID,messageID,type:'tool',tool:'bash',callID,state:{status:'running',input:{},time:{start:now}}} as any
    const finished={...running,state:{status:'completed',input:{},output:result.output,title:result.title,metadata:result.metadata,time:{start:now,end:now+1}}} as any
    await AppRuntime.runPromise(Session.Service.use(s=>s.updatePart(running)))
    const timing=captureToolTiming(issuer,identity,running)!,capture=captureToolView(encoded,timing,finished,route,readBashView(result))!
    expect(capture.admission.profile).toBe(expected)
    await AppRuntime.runPromise(Session.Service.use(s=>s.updateTimedTool(finished,timing,capture)))
    const stored=JSON.parse(Database.use(db=>db.get<{v:string}>(sql`SELECT openclank_view_decision AS v FROM part WHERE id=${id}`)!.v))
    const assembled=await prepareToolViews({encoded,identity,issuer,route,control:{disabled:false},historyAllowed:true,messages:[{info:{id:messageID,role:'assistant'},parts:[finished]}] as any,current:()=>true,ack:async()=>({version:1,accountID:'account',actorID:'main',chatID:'chat',sessionID,messageID,partID:id,revision:1,contentHash:stored.contentSHA256,outputSHA256:createHash('sha256').update(result.output).digest('hex'),outputBytes:Buffer.byteLength(result.output)}),check:async()=>true})
    expect(assembled.views.get(id)).toContain('history(');expect(finished.state.output).toBe(body)
    const changed={...result,output:result.output+'x'};transferBashView(result,changed);expect(readBashView(changed)).toBeUndefined()
  }
  expect(enabledToolViewProfiles(undefined,'bash')).toEqual([])
  issuer.revoke()
 }})
})
