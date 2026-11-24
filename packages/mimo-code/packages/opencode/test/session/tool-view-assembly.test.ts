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
    const original={version:1,requested:true,...route,sessionID,hostAccountID:'account',holderID:'holder',generation:2,settingsRevision:'a'.repeat(64),routeMethodKey:'route-key',profile:'json-whitespace-v1'}
    const encoded=encodeToolViewAdmission(original,sessionID,route,{providerID:'provider',modelID:'model'})!
    const output='{'+' '.repeat(1000)+'"n": 123456789012345678901234567890, "n": -0 }'
    const running={id:partID,sessionID,messageID,type:'tool',tool:'mcp_tool_search',callID:'call',state:{status:'running',input:{},time:{start:now}}} as any
    const finished={...running,state:{status:'completed',input:{},output,title:'',metadata:{truncated:false},time:{start:now,end:now+1}}} as any
    await AppRuntime.runPromise(Session.Service.use(s=>s.updatePart(running)))
    const issuer=createToolTimingIssuer();markAdmittedToolTimingTurn(identity,identity)
    const timing=captureToolTiming(issuer,identity,running)!, capture=captureToolView(encoded,timing,finished,route)!
    await AppRuntime.runPromise(Session.Service.use(s=>s.updateTimedTool(finished,timing,capture)))
    const stored=JSON.parse(Database.use(db=>db.get<{v:string}>(sql`SELECT openclank_view_decision AS v FROM part WHERE id=${partID}`)!.v))
    const messages=[{info:{id:messageID,role:'assistant',providerID:'provider',modelID:'model'},parts:[finished]}] as any
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
    expect(wire).toContain('Lossless JSON whitespace view');expect(wire).toContain('123456789012345678901234567890')
    expect(finished.state.output).toBe(output)
    const frozen=JSON.stringify(rendered)
    const inherited=[{role:'user',content:'question'} as any,...rendered]
    const fork=await encodeForkContext({system:['system'],tools:{},parentPermission:[],inheritedMessages:inherited,
      watermarkMsgID:messageID as any,model:{providerID:'provider' as any,modelID:'model' as any}}, {sessionID:sessionID as any,actorID:'main'})
    live=false
    const refused=await prepareToolViews(args)
    expect(refused.views.size).toBe(0);expect(control.disabled).toBe(true);expect(control.reason).toBe('scope_unavailable')
    expect(JSON.stringify(rendered)).toBe(frozen)
    const restored=await decodeForkContext({...fork,sessionID:sessionID as any,actorID:'child',timeCreated:now})
    expect(restored.inheritedMessages).toEqual(inherited)
    const repeated=await encodeForkContext(restored,{sessionID:sessionID as any,actorID:'main'})
    expect(repeated.snapshotJSON).toBe(fork.snapshotJSON)
    const nextRunning={...running,id:'prt_late',callID:'late'}
    const nextFinished={...finished,id:'prt_late',callID:'late'}
    await AppRuntime.runPromise(Session.Service.use(s=>s.updatePart(nextRunning)))
    const nextTiming=captureToolTiming(issuer,identity,nextRunning)!
    await AppRuntime.runPromise(Session.Service.use(s=>s.updateTimedTool(nextFinished,nextTiming,captureToolView(encoded,nextTiming,nextFinished,route))))
    const nextMessages=[{...messages[0],parts:[nextFinished]}]
    let lookup=0, nextChecks=0
    const nextArgs={...args,control:{disabled:false},messages:nextMessages,current:()=>true,
      ack:async()=>{lookup++;return undefined},check:async()=>{nextChecks++;return true}}
    expect((await prepareToolViews({...nextArgs,encoded:undefined})).views.size).toBe(0)
    expect(lookup).toBe(0)
    expect((await prepareToolViews(nextArgs)).views.size).toBe(0)
    expect(lookup).toBe(1);expect(nextChecks).toBe(0)
    expect((await prepareToolViews({...nextArgs,ack:args.ack})).views.size).toBe(0)
    expect(nextChecks).toBe(0)
    const newIdentity={...identity,rootOperationID:'new-root',innerTurnID:'new-turn'}
    const newRoute={...route,rootOperationID:'new-root'}, newIssuer=createToolTimingIssuer()
    const newEncoded=encodeToolViewAdmission({...original,rootOperationID:'new-root'},sessionID,newRoute,{providerID:'provider',modelID:'model'})!
    const newRunning={...running,id:'prt_new',callID:'new'}, newFinished={...finished,id:'prt_new',callID:'new'}
    await AppRuntime.runPromise(Session.Service.use(s=>s.updatePart(newRunning)))
    markAdmittedToolTimingTurn(newIdentity,newIdentity)
    const newTiming=captureToolTiming(newIssuer,newIdentity,newRunning)!
    await AppRuntime.runPromise(Session.Service.use(s=>s.updateTimedTool(newFinished,newTiming,captureToolView(newEncoded,newTiming,newFinished,newRoute))))
    const newMarker=JSON.parse(Database.use(db=>db.get<{v:string}>(sql`SELECT openclank_view_decision AS v FROM part WHERE id=${'prt_new'}`)!.v))
    const newControl={disabled:false};let checked=0
    const mixed=await prepareToolViews({...args,identity:newIdentity,issuer:newIssuer,route:newRoute,encoded:newEncoded,control:newControl,current:()=>true,
      messages:[messages[0],{...messages[0],parts:[newFinished]}],
      ack:async()=>({...await args.ack(),partID:'prt_new',contentHash:newMarker.contentSHA256}),
      check:async(refs:any[])=>{checked=refs.length;return true}})
    expect(newControl.disabled).toBe(false);expect(checked).toBe(1)
    expect(mixed.views.has(partID)).toBe(false);expect(mixed.views.has('prt_new')).toBe(true)
    expect(mixed.reason).toBe('prior_view_admission_changed')
    expect(compactJsonTokens('{"x":"\\ud800"}')).toBeUndefined()
    expect(compactJsonTokens('{"x":"\\udc00"}')).toBeUndefined()
    expect(compactJsonTokens('{"\\ud800":1}')).toBeUndefined()
    expect(compactJsonTokens('{"x":"\\ud800","x":"valid"}')).toBeUndefined()
    expect(compactJsonTokens('{"x":"\\ud83d\\ude00"}')).toBe('{"x":"\\ud83d\\ude00"}')
    const text='independent attested context', textSHA256=createHash('sha256').update(text).digest('hex')
    const preface={contextPartID:'context-part',attestationID:'attestation',textSHA256,text,index:5,count:8,wireProfile:'openai-chat' as const}
    const restoredPreface=restoreContextPreface([{role:'user',content:'question'}],preface)
    expect(restoredPreface.preface).toEqual({...preface,index:0,count:2})
    expect(JSON.stringify(restoredPreface.messages)).toContain(text)
    expect((restoredPreface.messages[0] as any).content[0].id).toBe('context-part')
    expect(restoreContextPreface([{role:'user',content:'question'}],{...preface,text:'changed'}).reason).toBe('context_preface_unavailable')
    newIssuer.revoke()
    issuer.revoke()
  }})
})
