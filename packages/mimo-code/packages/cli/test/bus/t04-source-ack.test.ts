import { afterEach, describe, expect } from "bun:test"
import { fileURLToPath } from "node:url"
import { Effect, Layer, Schedule } from "effect"
import { Database } from "../../src/storage"
import { ProjectTable } from "../../src/project/project.sql"
import { MessageTable, PartTable, SessionTable } from "../../src/session/session.sql"
import { HistoryFtsTable } from "../../src/history/fts.sql"
import { History } from "../../src/history"
import * as Writer from "../../src/history/writer"
import { Bus } from "../../src/bus"
import { MessageV2 } from "../../src/session/message-v2"
import { Instance } from "../../src/project/instance"
import { ManagedProvider } from "../../src/acp/managed-provider"
import { installManagedSessionBinding } from "../../src/memory/session-scope"
import { createToolTimingIssuer, markAdmittedToolTimingTurn, captureToolTiming, emitToolTiming, installToolTimingIssuer } from "../../src/session/tool-timing"
import * as CrossSpawnSpawner from "../../src/effect/cross-spawn-spawner"
import { provideTmpdirInstance } from "../fixture/fixture"
import { testEffect } from "../lib/effect"
const it=testEffect(Layer.mergeAll(History.defaultLayer,Bus.defaultLayer,CrossSpawnSpawner.defaultLayer))
afterEach(async () => {
  Database.use(db => { db.delete(HistoryFtsTable).run();db.delete(PartTable).run();db.delete(MessageTable).run();db.delete(SessionTable).run();db.delete(ProjectTable).run() })
  await Instance.disposeAll();ManagedProvider.resetForTest();delete process.env.OPEN_CLANK_MANAGED
})
describe("T04 source ack", () => {
  it.live("retains and invalidates an exact queued host acknowledgement", () => provideTmpdirInstance(() => Effect.gen(function* () {
    process.env.OPEN_CLANK_MANAGED="1"
    installManagedSessionBinding("ses_t",{owner:"alice",stableChatID:"chat-t",engineSessionID:"ses_t",engineAliases:[],memoryWorkspaceID:"global",authorityWorkspaceID:"authority",copalWorkspace:"copal",physicalCwd:"/work",bindingRevision:1,mapRevision:1,mappingRevision:1,memoryEnabled:true,transition:null})
    const calls:any[]=[]
    const issuer=createToolTimingIssuer()
    const host={async extMethod(method:string,params:any) {
      calls.push({method,params})
      const repo=fileURLToPath(new URL("../../../../../../", import.meta.url))
      const child=Bun.spawn({cmd:[process.env.OPEN_CLANK_PYTHON || 'python3',repo+"tests/fixtures/queued_history_host.py"],cwd:repo,stdin:"pipe",stdout:"pipe",stderr:"pipe",env:{...process.env,PYTHONPATH:".",PYTHONDONTWRITEBYTECODE:"1"}})
      child.stdin.write(JSON.stringify({publicKey:issuer.publicKey,clockID:issuer.clockID,params}));child.stdin.end()
      const text=await new Response(child.stdout).text(); const error=await new Response(child.stderr).text()
      if (await child.exited !== 0) throw new Error(error)
      return JSON.parse(text)
    }}
    installToolTimingIssuer(host,issuer);ManagedProvider.installHostConnection(host as any)
    const now=Date.now()
    Database.use(db => {
      db.insert(ProjectTable).values({id:"proj_t" as any,worktree:"/tmp",sandboxes:[] as any,time_created:now,time_updated:now} as any).run()
      db.insert(SessionTable).values({id:"ses_t" as any,project_id:"proj_t" as any,slug:"x",directory:"/tmp",title:"t",version:"1",time_created:now,time_updated:now}).run()
      db.insert(MessageTable).values({id:"msg_t" as any,session_id:"ses_t" as any,agent_id:"main",data:{role:"assistant"} as any,time_created:now,time_updated:now}).run()
    })
    const writer=yield* Writer.Service;yield* writer.init();const bus=yield* Bus.Service
    const identity={sessionID:"ses_t" as any,rootOperationID:"root",actorID:"child",executionRevision:1,innerTurnID:"turn"}
    markAdmittedToolTimingTurn(identity,identity)
    const running={id:"prt_t",sessionID:"ses_t",messageID:"msg_t",type:"tool",tool:"x",callID:"call",state:{status:"running",input:{},time:{start:now}}} as any
    const capture=captureToolTiming(issuer,identity,running)
    const final=structuredClone({...running,state:{status:"completed",input:{},output:"ok",title:"",metadata:{truncated:false},time:{start:now,end:now+1}}})
    let queued:Effect.Effect<void> | undefined
    emitToolTiming(capture,final,()=> { queued=bus.publish(MessageV2.Event.PartUpdated,{sessionID:"ses_t" as any,part:final,time:now+1}) })
    expect(queued).toBeDefined();yield* queued!
    const waitForAck = (callCount: number) => Effect.gen(function* () {
      if (calls.length < callCount) return undefined
      const digest = calls[0]?.params.events[0]?.toolTiming.contentSHA256
      if (digest) return yield* writer.sourceAck("ses_t", "child", "prt_t", digest)
    }).pipe(
      Effect.repeat({ until: ack => ack !== undefined, schedule: Schedule.spaced("25 millis") }),
      Effect.timeout("10 seconds"),
    )
    const ack = yield* waitForAck(1)
    expect(calls).toHaveLength(1)
    const event=calls[0].params.events[0]
    expect(ack?.revision).toBe(1)
    expect(yield* writer.sourceAck("ses_t","child","prt_t","0".repeat(64))).toBeUndefined()
    let again:Effect.Effect<void> | undefined
    emitToolTiming(capture,final,()=> {again=bus.publish(MessageV2.Event.PartUpdated,{sessionID:"ses_t" as any,part:final,time:now+1})})
    yield* again!; yield* waitForAck(2)
    expect(yield* writer.sourceAck("ses_t","child","prt_t",event.toolTiming.contentSHA256)).toBeDefined()
    issuer.revoke()
    expect(yield* writer.sourceAck("ses_t","child","prt_t",event.toolTiming.contentSHA256)).toBeUndefined()
    expect(event.actorID).toBe("child")
    expect(event.content).toBe(JSON.stringify(final))
    expect(event.toolTiming).toMatchObject({actorID:"child",rootOperationID:"root",innerTurnID:"turn",partID:"prt_t",callID:"call"})
  })), 25000)
})
