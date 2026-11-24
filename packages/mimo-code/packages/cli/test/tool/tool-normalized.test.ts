import { expect, test } from "bun:test"
import { Effect, Layer, ManagedRuntime } from "effect"
import z from "zod"
import { Agent } from "../../src/agent/agent"
import { Tool, Truncate } from "../../src/tool"

test("wrapper uses one normalized argument for resources and execution", async () => {
  const runtime = ManagedRuntime.make(Layer.mergeAll(Truncate.defaultLayer, Agent.defaultLayer))
  const seen: unknown[] = []
  const schema = z.object({ count: z.number().int().positive().default(5) })
  try {
    const info = await runtime.runPromise(Tool.define("normalized", Effect.succeed({
      description: "fixture", parameters: schema,
      resources: (args: z.infer<typeof schema>) => { seen.push(args); return { reads: [] } },
      execute: (args: z.infer<typeof schema>) => { seen.push(args); return Effect.succeed({ title:"fixture", output:"ok", metadata:{truncated:false} }) },
    })))
    const wrapped = await Effect.runPromise(info.init())
    const ctx = {sessionID:"fixture",messageID:"fixture",agent:"build",abort:new AbortController().signal,messages:[],metadata:()=>Effect.void,ask:()=>Effect.void} as any
    await Effect.runPromise(wrapped.execute({} as any, ctx))
    expect(seen).toEqual([{count:5},{count:5}])
    expect(seen[0]).toBe(seen[1])
    seen.length = 0
    await Effect.runPromise(wrapped.execute({count:3}, ctx))
    expect(seen).toEqual([{count:3},{count:3}])
    seen.length = 0
    await expect(Effect.runPromise(wrapped.execute({count:-1}, ctx))).rejects.toThrow()
    expect(seen).toEqual([])
  } finally { await runtime.dispose() }
})
