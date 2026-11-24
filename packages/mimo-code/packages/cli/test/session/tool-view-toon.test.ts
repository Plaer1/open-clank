import { expect, test } from 'bun:test'
import { createHash } from 'node:crypto'
import { Effect } from 'effect'
import { sql } from 'drizzle-orm'
import { Database } from '../../src/storage'
import { ProjectTable } from '../../src/project/project.sql'
import { SessionTable, MessageTable } from '../../src/session/session.sql'
import { Session } from '../../src/session'
import { MessageV2 } from '../../src/session/message-v2'
import { AppRuntime } from '../../src/effect/app-runtime'
import { Instance } from '../../src/project/instance'
import { tmpdir } from '../fixture/fixture'
import { createToolTimingIssuer, markAdmittedToolTimingTurn, captureToolTiming } from '../../src/session/tool-timing'
import { encodeToolViewAdmission, captureToolView } from '../../src/session/tool-view-source'
import { prepareToolViews, restoreContextPreface } from '../../src/session/tool-view-assembly'
import { encodeForkContext, decodeForkContext } from '../../src/actor/fork-context'
import { compactJsonTokens } from '../../src/session/tool-view-json'
import { recordMcpJsonCandidate } from '../../src/session/mcp-json-candidate'
import { gcfCandidate } from '../../src/session/gcf-source-proof'

test('final producer, synthetic ACK/source check and actual model conversion preserve raw canonical output', async () => {
  await using tmp=await tmpdir()
  await Instance.provide({directory:tmp.path,fn:async()=>{
    const now=Date.now(), sessionID='ses_view',messageID='msg_view',partID='prt_view'
    Database.use(db=>{
      db.insert(ProjectTable).values({id:'proj_view' as any,worktree:tmp.path,sandboxes:[] as any,time_created:now,time_updated:now} as any).run()
      db.insert(SessionTable).values({id:sessionID as any,project_id:'proj_view' as any,slug:'v',directory:tmp.path,title:'v',version:'1',time_created:now,time_updated:now}).run()
      db.insert(MessageTable).values({id:messageID as any,session_id:sessionID as any,agent_id:'main',data:{role:'assistant'} as any,time_created:now,time_updated:now}).run()
      if (!db.all<{name:string}>(sql`PRAGMA table_info(part)`).some(x=>x.name==='openclank_view_decision'))
        db.run(sql`ALTER TABLE part ADD COLUMN openclank_view_decision TEXT`)
    })
    const identity={sessionID,rootOperationID:'root',actorID:'main',executionRevision:1,innerTurnID:'turn'} as any
    const route={rootOperationID:'root',connectionID:'conn',providerID:'provider',modelID:'model',modelRouteID:'route'}
    const original={version:2,requested:true,...route,sessionID,hostAccountID:'account',holderID:'holder',generation:2,settingsRevision:'a'.repeat(64),routeMethodKey:'route-key',profiles:{jsonWhitespace:false,terminalAnsi:false,htmlMarkdown:false,structuredSearch:false,repeatedLines:false,unifiedDiff:false,schemaAnnotations:false,toon:true}}
    const encoded=encodeToolViewAdmission(original,sessionID,route,{providerID:'provider',modelID:'model'})!
    const output=JSON.stringify({items:Array.from({length:80},(_,i)=>({long_repeated_field_name:i,another_repeated_field_name:'abc'}))})
    let recorded:{value:string; current?:()=>boolean}|undefined, originalOn=true
    const producer={callID:'call',output,normalizedOutput:output,gcfOn:false,toonCurrent:()=>originalOn,
      aborted:false,truncated:false,attachmentCount:0,isError:false,content:[{type:'text'}],hasStructuredContent:false,
      record:(_id:string,value:string,current?:()=>boolean)=>{recorded={value,current}}}
    recordMcpJsonCandidate({...producer,toonCurrent:()=>false});expect(recorded).toBeUndefined()
    recordMcpJsonCandidate({...producer,hasStructuredContent:true});expect(recorded).toBeUndefined()
    recordMcpJsonCandidate({...producer,content:[{type:'resource'}]});expect(recorded).toBeUndefined()
    recordMcpJsonCandidate(producer);expect(gcfCandidate(recorded?.value)?.sha256).toBe(createHash('sha256').update(output).digest('hex'))
    expect(recorded?.current?.()).toBe(true)
    const running={id:partID,sessionID,messageID,type:'tool',tool:'mcp_tool_search',callID:'call',state:{status:'running',input:{},time:{start:now}}} as any
    const finished={...running,state:{status:'completed',input:{},output,title:'',metadata:{truncated:false,mcp:{isError:false}},time:{start:now,end:now+1}}} as any
    await AppRuntime.runPromise(Session.Service.use(s=>s.updatePart(running)))
    const issuer=createToolTimingIssuer();markAdmittedToolTimingTurn(identity,identity)
    const timing=captureToolTiming(issuer,identity,running)!, capture=captureToolView(encoded,timing,finished,route,'toon-v1')!
    const issued=await AppRuntime.runPromise(Session.Service.use(s=>s.updateGcfCompleted(finished,timing,capture)))
    const stored=JSON.parse(Database.use(db=>db.get<{v:string}>(sql`SELECT openclank_view_decision AS v FROM part WHERE id=${partID}`)!.v))
    const messages=[{info:{id:messageID,role:'assistant',providerID:'provider',modelID:'model'},parts:[issued]}] as any
    const control:{disabled:boolean;reason?:string}={disabled:false}, digest=createHash('sha256').update(output).digest('hex')
    let checks=0, live=true
    const args={encoded,identity,issuer,route,control,historyAllowed:true,messages,current:()=>live,
      ack:async()=>({version:1 as const,accountID:'account',actorID:'main',chatID:'chat',sessionID,messageID,partID,revision:3,contentHash:stored.contentSHA256,outputSHA256:digest,outputBytes:Buffer.byteLength(output)}),
      check:async(refs:any[])=>{checks++;return refs.length===1 && refs[0].contentHash===stored.contentSHA256}}
    const prepared=await prepareToolViews(args)
    expect(checks).toBe(1);expect(prepared.views.size).toBe(1)
    const model={id:'model',providerID:'provider',api:{npm:'@ai-sdk/anthropic'},capabilities:{input:{image:false}}} as any
    const rendered=await Effect.runPromise(MessageV2.toModelMessagesEffect(messages,model,{toolViews:prepared.views}))
    const wire=JSON.stringify(rendered)
    expect(wire).toContain('Restricted canonical TOON 4.4');expect(wire).toContain('another_repeated_field_name')
    expect(finished.state.output).toBe(output)
    const unsupportedRunning={...running,id:'prt_unsupported',callID:'unsupported'}
    const unsupportedOutput='{"x":1.5,"padding":"'+'z'.repeat(140)+'"}'
    const unsupportedFinished={...finished,id:'prt_unsupported',callID:'unsupported',state:{...finished.state,output:unsupportedOutput}}
    await AppRuntime.runPromise(Session.Service.use(s=>s.updatePart(unsupportedRunning)))
    const unsupportedTiming=captureToolTiming(issuer,identity,unsupportedRunning)!
    const unsupportedCapture=captureToolView(encoded,unsupportedTiming,unsupportedFinished,route,'toon-v1')!
    const unsupportedIssued=await AppRuntime.runPromise(Session.Service.use(s=>s.updateGcfCompleted(unsupportedFinished,unsupportedTiming,unsupportedCapture)))
    const unsupportedStored=JSON.parse(Database.use(db=>db.get<{v:string}>(sql`SELECT openclank_view_decision AS v FROM part WHERE id=${'prt_unsupported'}`)!.v))
    const unsupportedMessages=[{...messages[0],parts:[unsupportedIssued]}]
    const unsupportedHash=createHash('sha256').update(unsupportedOutput).digest('hex')
    const unsupportedResult=await prepareToolViews({...args,control:{disabled:false},messages:unsupportedMessages,
      ack:async()=>({...await args.ack(),partID:'prt_unsupported',contentHash:unsupportedStored.contentSHA256,outputSHA256:unsupportedHash,outputBytes:Buffer.byteLength(unsupportedOutput)})})
    expect(unsupportedResult.views.get('prt_unsupported')).toBe(unsupportedOutput)
    const frozen=JSON.stringify(rendered)
    const inherited=[{role:'user',content:'question'} as any,...rendered]
    const fork=await encodeForkContext({system:['system'],tools:{},parentPermission:[],inheritedMessages:inherited,
      watermarkMsgID:messageID as any,model:{providerID:'provider' as any,modelID:'model' as any}}, {sessionID:sessionID as any,actorID:'main'})
    originalOn=false; expect(recorded?.current?.()).toBe(false)
    live=false
    const refused=await prepareToolViews(args)
    expect(refused.views.get(partID)).toBe(output);expect(control.disabled).toBe(true);expect(control.reason).toBe('scope_unavailable')
    expect(JSON.stringify(rendered)).toBe(frozen)
    const restored=await decodeForkContext({...fork,sessionID:sessionID as any,actorID:'child',timeCreated:now})
    expect(restored.inheritedMessages).toEqual(inherited)
    const repeated=await encodeForkContext(restored,{sessionID:sessionID as any,actorID:'main'})
    expect(repeated.snapshotJSON).toBe(fork.snapshotJSON)
    issuer.revoke()
  }})
})
