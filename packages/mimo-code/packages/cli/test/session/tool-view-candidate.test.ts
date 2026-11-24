import { expect, test } from 'bun:test'
import { sql } from 'drizzle-orm'
import { Database } from '../../src/storage'
import { ProjectTable } from '../../src/project/project.sql'
import { SessionTable, MessageTable } from '../../src/session/session.sql'
import { Session } from '../../src/session'
import { AppRuntime } from '../../src/effect/app-runtime'
import { Instance } from '../../src/project/instance'
import { tmpdir } from '../fixture/fixture'
import { createToolTimingIssuer, markAdmittedToolTimingTurn, captureToolTiming } from '../../src/session/tool-timing'
import { encodeToolViewAdmission, captureToolView } from '../../src/session/tool-view-source'
import { freezeToolView } from '../../src/session/tool-view-decision'

test('original admitted fresh tool completion persists a private candidate', async () => {
  await using tmp=await tmpdir()
  await Instance.provide({directory:tmp.path,fn:async()=>{
    const now=Date.now(), sessionID='ses_c', messageID='msg_c', partID='prt_c'
    Database.use(db=>{
      db.insert(ProjectTable).values({id:'proj_c' as any,worktree:tmp.path,sandboxes:[] as any,time_created:now,time_updated:now} as any).run()
      db.insert(SessionTable).values({id:sessionID as any,project_id:'proj_c' as any,slug:'v',directory:tmp.path,title:'v',version:'1',time_created:now,time_updated:now}).run()
      db.insert(MessageTable).values({id:messageID as any,session_id:sessionID as any,agent_id:'main',data:{role:'assistant'} as any,time_created:now,time_updated:now}).run()
      db.run(sql`ALTER TABLE part ADD COLUMN openclank_view_decision TEXT`)
    })
    const identity={sessionID,rootOperationID:'root',actorID:'main',executionRevision:1,innerTurnID:'turn'} as any
    const route={rootOperationID:'root',connectionID:'conn',providerID:'provider',modelID:'model',modelRouteID:'route'}
    const original={version:1,requested:true,...route,sessionID,hostAccountID:'account',holderID:'holder',generation:2,settingsRevision:'a'.repeat(64),routeMethodKey:'["provider","adapter","kind","lane"]',profile:'json-whitespace-v1'}
    const encoded=encodeToolViewAdmission(original,sessionID,route,{providerID:'provider',modelID:'model'})
    expect(encoded).toBeDefined()
    expect(encodeToolViewAdmission({...original,requested:false},sessionID,route,{providerID:'provider',modelID:'model'})).toBeUndefined()
    expect(encodeToolViewAdmission({...original,connectionID:'foreign'},sessionID,route,{providerID:'provider',modelID:'model'})).toBeUndefined()
    const running={id:partID,sessionID,messageID,type:'tool',tool:'mcp_tool_search',callID:'call',state:{status:'running',input:{},time:{start:now}}} as any
    const finished={...running,state:{status:'completed',input:{},output:' {"n": 1} ',title:'',metadata:{truncated:false},time:{start:now,end:now+1}}} as any
    await AppRuntime.runPromise(Session.Service.use(s=>s.updatePart(running)))
    const issuer=createToolTimingIssuer();markAdmittedToolTimingTurn(identity,identity)
    const timing=captureToolTiming(issuer,identity,running)!
    expect(captureToolView(undefined,timing,finished,route)).toBeUndefined()
    expect(captureToolView(encoded,undefined,finished,route)).toBeUndefined()
    expect(captureToolView(encoded,timing,{...finished,state:{...finished.state,status:'error'}},route)).toBeUndefined()
    expect(captureToolView(encoded,timing,{...finished,state:{...finished.state,output:'x'.repeat(51201)}},route)).toBeUndefined()
    expect(captureToolView(encoded,timing,{...finished,state:{...finished.state,metadata:{truncated:true}}},route)).toBeUndefined()
    expect(captureToolView(encoded,timing,finished,{...route,modelID:'foreign'})).toBeUndefined()
    const view=captureToolView(encoded,timing,finished,route);expect(view).toBeDefined()
    await AppRuntime.runPromise(Session.Service.use(s=>s.updateTimedTool(finished,timing,view)))
    const decision=Database.use(db=>db.get<{v:string | null}>(sql`SELECT openclank_view_decision AS v FROM part WHERE id=${partID}`)!.v)
    expect(decision).not.toBeNull();const parsed=JSON.parse(decision!)
    expect(parsed).toMatchObject({state:'candidate',hostAccountID:'account',holderID:'holder',clockID:issuer.clockID,generation:2,profile:'json-whitespace-v1'})
    expect(parsed.contentSHA256).toMatch(/^[a-f0-9]{64}$/)
    const scope=view!.admission
    expect(freezeToolView({...scope,generation:3},{state:'raw'})).toBeUndefined()
    expect(freezeToolView({...scope,holderID:'other'},{state:'raw'})).toBeUndefined()
    expect(freezeToolView({...scope,clockID:'e'.repeat(32)},{state:'raw'})).toBeUndefined()
    expect(freezeToolView(scope,{state:'raw'})?.state).toBe('raw')
    // A repeated completion is not a running->completed transition and cannot replace raw.
    await AppRuntime.runPromise(Session.Service.use(s=>s.updateTimedTool(finished,timing,view)))
    expect(freezeToolView(scope,{state:'raw'})?.state).toBe('raw')
    const changed=structuredClone(finished);changed.state.output='changed'
    await AppRuntime.runPromise(Session.Service.use(s=>s.updatePart(changed)))
    expect(freezeToolView(scope,{state:'raw'})).toBeUndefined()
    issuer.revoke()
    expect(captureToolView(encoded,timing,finished,route)).toBeUndefined()
  }})
})
