import { describe, test, expect } from "bun:test"
import { Effect, Layer, ManagedRuntime } from "effect"
import z from "zod"
import { Agent } from "../../src/agent/agent"
import { Tool } from "../../src/tool"
import { Truncate } from "../../src/tool"

const runtime = ManagedRuntime.make(Layer.mergeAll(Truncate.defaultLayer, Agent.defaultLayer))

const params = z.object({ input: z.string() })

function makeTool(id: string, executeFn?: () => void) {
  return {
    description: "test tool",
    parameters: params,
    execute() {
      executeFn?.()
      return Effect.succeed({ title: "test", output: "ok", metadata: {} })
    },
  }
}

describe("Tool.define", () => {
  test("object-defined tool does not mutate the original init object", async () => {
    const original = makeTool("test")
    const originalExecute = original.execute

    const info = await runtime.runPromise(Tool.define("test-tool", Effect.succeed(original)))

    await Effect.runPromise(info.init())
    await Effect.runPromise(info.init())
    await Effect.runPromise(info.init())

    expect(original.execute).toBe(originalExecute)
  })

  test("effect-defined tool returns fresh objects and is unaffected", async () => {
    const info = await runtime.runPromise(
      Tool.define(
        "test-fn-tool",
        Effect.succeed(() => Effect.succeed(makeTool("test"))),
      ),
    )

    const first = await Effect.runPromise(info.init())
    const second = await Effect.runPromise(info.init())

    expect(first).not.toBe(second)
  })

  test("object-defined tool returns distinct objects per init() call", async () => {
    const info = await runtime.runPromise(Tool.define("test-copy", Effect.succeed(makeTool("test"))))

    const first = await Effect.runPromise(info.init())
    const second = await Effect.runPromise(info.init())

    expect(first).not.toBe(second)
  })

  test("resource declarations schedule wrapped invocations by canonical path", async () => {
    const calls: string[] = []
    let releaseFirst!: () => void
    const firstRelease = new Promise<void>((resolve) => {
      releaseFirst = resolve
    })
    let firstStarted!: () => void
    const firstReady = new Promise<void>((resolve) => {
      firstStarted = resolve
    })
    let disjointStarted!: () => void
    const disjointReady = new Promise<void>((resolve) => {
      disjointStarted = resolve
    })
    const scheduledParams = z.object({
      path: z.string(),
      access: z.enum(["read", "write"]),
      hold: z.boolean().optional(),
    })
    const info = await runtime.runPromise(
      Tool.define(
        "scheduled-tool",
        Effect.succeed({
          description: "scheduled",
          parameters: scheduledParams,
          resources: (args: z.infer<typeof scheduledParams>) =>
            args.access === "read" ? { reads: [args.path] } : { writes: [args.path] },
          execute: (args: z.infer<typeof scheduledParams>) =>
            Effect.promise(async () => {
              calls.push(args.path)
              if (args.hold) {
                firstStarted()
                await firstRelease
              }
              if (args.path.endsWith("second")) disjointStarted()
              return { title: "scheduled", output: "ok", metadata: { truncated: false } }
            }),
        }),
      ),
    )
    const tool = await Effect.runPromise(info.init())
    const ctx = {
      sessionID: "ses_scheduler",
      messageID: "msg_scheduler",
      agent: "build",
      abort: AbortSignal.any([]),
      messages: [],
      metadata: () => Effect.void,
      ask: () => Effect.void,
    } as any

    const first = Effect.runPromise(
      tool.execute({ path: "/tmp/open-clank-scheduler-first", access: "read", hold: true }, ctx),
    )
    await firstReady
    const blocked = Effect.runPromise(
      tool.execute({ path: "/tmp/open-clank-scheduler-first", access: "write" }, ctx),
    )
    const disjoint = Effect.runPromise(
      tool.execute({ path: "/tmp/open-clank-scheduler-second", access: "write" }, ctx),
    )
    await disjointReady
    expect(calls.filter((item) => item.endsWith("first"))).toHaveLength(1)

    releaseFirst()
    await Promise.all([first, blocked, disjoint])
    expect(calls.filter((item) => item.endsWith("first"))).toHaveLength(2)
  })
})
