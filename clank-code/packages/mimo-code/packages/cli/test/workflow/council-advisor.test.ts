import { describe, expect, test } from "bun:test"
import { readFileSync, appendFileSync } from "node:fs"
import path from "node:path"
import { parseMeta } from "../../src/workflow/meta"
import { evalScript } from "../../src/workflow/sandbox"
import { WorkflowPersistence } from "../../src/workflow/persistence"
import { Global } from "../../src/global"

const script = (name: string) => readFileSync(path.join(import.meta.dir, "../../src/workflow/builtin", name + ".js"), "utf8")
const body = (name: string) => { const parsed = parseMeta(script(name)); if (!parsed.ok) throw new Error(parsed.error); return parsed.body }

describe("strict council/advisor profile", () => {
  test("tool-free bounded participants, inheritance, explicit model and partial result", async () => {
    const calls: any[] = []
    const result: any = await evalScript(body("council"), {
      phase: () => undefined,
      agent: async (_prompt, opts) => { calls.push(opts); return calls.length === 2 ? null : "view" },
    }, {args: {question:"review", participants:3, models:["provider/model"]}})
    expect(result.status).toBe("partial")
    expect(result.views.map((v: any) => v.status)).toEqual(["available", "unavailable", "available"])
    expect(calls).toHaveLength(4)
    expect(calls.every((c) => Array.isArray(c.tools) && c.tools.length === 0 && c.timeoutMs === 120000)).toBe(true)
    expect(calls[0].model).toBe("provider/model")
    expect(calls[1].model).toBeUndefined()
    expect(calls[3].model).toBeUndefined()
    await expect(evalScript(body("council"), {phase:()=>{}, agent:()=>"x"}, {args:{question:"x",participants:6}})).rejects.toThrow()
    const advisorCalls: any[] = []
    await evalScript(body("advisor"), {phase:()=>{}, agent:(_p,o)=>{advisorCalls.push(o); return "ok"}}, {args:{question:"x"}})
    expect(advisorCalls.every(c => c.tools.length === 0 && c.model === undefined)).toBe(true)
  })

  test("same durable journal preserves successes, nulls and unknown intents without respawn", () => {
    const run = "wf_strictfixture", binding = "identity", key = "a".repeat(64) + ":0", failed = "b".repeat(64) + ":0", unknown = "c".repeat(64) + ":0"
    WorkflowPersistence.strictJournal(run,binding,false,300000)
    WorkflowPersistence.strictWrite(run,{t:"strict-intent",key})
    WorkflowPersistence.strictWrite(run,{t:"strict-actor",key,actorID:"actor-1"})
    WorkflowPersistence.strictWrite(run,{t:"strict-outcome",key,result:"ok"})
    WorkflowPersistence.strictWrite(run,{t:"strict-intent",key:failed})
    WorkflowPersistence.strictWrite(run,{t:"strict-outcome",key:failed,result:null})
    WorkflowPersistence.strictWrite(run,{t:"strict-intent",key:unknown})
    const replay = WorkflowPersistence.strictJournal(run,binding,true,900000)
    let spawnCount = 0
    const resume = (k: string) => { const slot = replay.slots.get(k); if (!slot) return null; return slot.settled ? slot.result : null }
    expect(resume(key)).toBe("ok"); expect(resume(failed)).toBeNull(); expect(resume(unknown)).toBeNull(); expect(resume("d".repeat(64)+":0")).toBeNull()
    expect(spawnCount).toBe(0); expect(replay.slots.get(key)?.actorID).toBe("actor-1"); expect(replay.budget).toBe(300000)
    expect(()=>WorkflowPersistence.strictJournal(run,"foreign",true,300000)).toThrow()
    appendFileSync(path.join(Global.Path.data,"workflow",run+".jsonl"),"{torn")
    expect(()=>WorkflowPersistence.strictJournal(run,binding,true,300000)).toThrow()
    expect(()=>WorkflowPersistence.strictWrite("../bad",{t:"strict-intent",key})).toThrow()
    expect(spawnCount).toBe(0)
  })
})

import { Effect, Deferred } from "effect"
import { WorkflowRuntime } from "../../src/workflow/runtime"
import { Session } from "../../src/session"
import { spawnRef } from "../../src/actor/spawn-ref"
import { provideTmpdirInstance } from "../fixture/fixture"
import { testEffect } from "../lib/effect"
import { makeLayer } from "./lib"
import { writeFileSync } from "node:fs"

const it = testEffect(makeLayer())
it.live("actual runtime replays strict journal without respawning failed or interrupted slots", () =>
  provideTmpdirInstance(() => Effect.gen(function* () {
    const runtime = yield* WorkflowRuntime.Service
    const sessions = yield* Session.Service
    const parent = yield* sessions.create({title:"strict profile", permission:[]})
    const old = spawnRef.current
    let count = 0, hanging = false
    const calls: any[] = []
    spawnRef.current = {
      spawn: (input: any) => Effect.gen(function* () {
        count++; calls.push(input)
        const actorID = `actor-${count}`
        input.onActorID(actorID)
        const outcome = yield* Deferred.make<any>()
        if (!hanging) yield* Deferred.succeed(outcome, count === 2 ? {status:"failed"} : {status:"success", finalText:"ok"})
        return {actorID, outcome} as any
      }),
      cancel: () => Effect.void,
    } as any
    try {
      const started = yield* runtime.start({script:script("council"), sessionID:parent.id, parentActorID:"main", args:{question:"x",participants:2}, notifyOnTerminal:false})
      const outcome = yield* runtime.wait({runID:started.runID})
      expect(outcome.status).toBe("completed")
      expect(count).toBe(3)
      expect(calls.every(x => Array.isArray(x.tools) && x.tools.length === 0)).toBe(true)
      const journal = path.join(Global.Path.data,"workflow",started.runID+".jsonl")
      const lines = readFileSync(journal,"utf8").trimEnd().split("\n")
      const first = lines.findIndex(x => JSON.parse(x).t === "strict-outcome")
      lines.splice(first,1)
      writeFileSync(journal, lines.join("\n")+"\n")
      const resumed = yield* runtime.resume({runID:started.runID})
      expect(resumed.resumed).toBe(true)
      const again = yield* runtime.wait({runID:started.runID})
      expect(again.status).toBe("completed")
      expect(count).toBe(3)
      const nodes = (yield* runtime.structure({runID:started.runID})).nodes.filter(n => n.type === "agent")
      expect(nodes.some(n => n.type === "agent" && n.actorID === "actor-1")).toBe(true)
      const originalWrite = WorkflowPersistence.strictWrite
      WorkflowPersistence.strictWrite = ((runID: string, event: any, create?: boolean) => {
        if (event.t === "strict-intent") throw new Error("synthetic journal failure")
        return originalWrite(runID, event, create)
      }) as typeof originalWrite
      try {
        const failed = yield* runtime.start({script:script("advisor"), sessionID:parent.id, parentActorID:"main", args:{question:"x"}, notifyOnTerminal:false})
        const failResult = yield* runtime.wait({runID:failed.runID})
        expect(failResult.status).toBe("failed")
        expect(count).toBe(3)
      } finally { WorkflowPersistence.strictWrite = originalWrite }
      hanging = true
      const cancelRun = yield* runtime.start({script:script("advisor"), sessionID:parent.id, parentActorID:"main", args:{question:"cancel"}, notifyOnTerminal:false})
      yield* Effect.sleep("100 millis")
      expect(count).toBe(4)
      yield* runtime.cancel({runID:cancelRun.runID})
      expect((yield* runtime.wait({runID:cancelRun.runID})).status).toBe("cancelled")
      hanging = false
      expect((yield* runtime.resume({runID:cancelRun.runID})).resumed).toBe(true)
      expect((yield* runtime.wait({runID:cancelRun.runID})).status).toBe("completed")
      expect(count).toBe(4)
    } finally { spawnRef.current = old }
  }), {git:true}),
)

it.live("same-named legacy workflow retains its existing journal replay", () =>
  provideTmpdirInstance(() => Effect.gen(function* () {
    const runtime = yield* WorkflowRuntime.Service
    const sessions = yield* Session.Service
    const parent = yield* sessions.create({title:"legacy council", permission:[]})
    const old = spawnRef.current
    let count = 0
    spawnRef.current = {spawn: (input: any) => Effect.gen(function* () {
      count++; const actorID = `legacy-${count}`; input.onActorID(actorID)
      const outcome = yield* Deferred.make<any>()
      yield* Deferred.succeed(outcome, {status:"success", finalText:"ok"})
      return {actorID, outcome} as any
    })} as any
    try {
      const legacy = 'export const meta = {name:"council",description:"custom legacy"}; return await agent("legacy")'
      const started = yield* runtime.start({script:legacy,sessionID:parent.id,parentActorID:"main",notifyOnTerminal:false})
      expect((yield* runtime.wait({runID:started.runID})).status).toBe("completed")
      expect(WorkflowPersistence.strictJournalKind(started.runID)).toBe("legacy")
      expect((yield* runtime.resume({runID:started.runID})).resumed).toBe(true)
      expect((yield* runtime.wait({runID:started.runID})).status).toBe("completed")
      expect(count).toBe(1)
    } finally { spawnRef.current = old }
  }), {git:true}),
)
