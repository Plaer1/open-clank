import { Hono, type Context } from "hono"
import { describeRoute, resolver, validator } from "hono-openapi"
import { streamSSE } from "hono/streaming"
import { Effect } from "effect"
import z from "zod"
import { BusEvent } from "@/bus/bus-event"
import { SyncEvent } from "@/sync"
import { GlobalBus } from "@/bus/global"
import { AppRuntime } from "@/effect/app-runtime"
import { AsyncQueue } from "@/util/queue"
import { Instance } from "../../project/instance"
import { Installation } from "@/installation"
import { InstallationVersion } from "@/installation/version"
import { Log } from "../../util"
import { lazy } from "../../util/lazy"
import { Config } from "../../config"
import { ExternalImport } from "../../session/external-import"
import { selectedHistory, selectedHistoryRead } from "../../session/codex-projection-import"
import { ManagedProvider } from "../../acp/managed-provider"
import { errors } from "../error"

const log = Log.create({ service: "server" })

// Cap on buffered SSE events per connection. The bus delta firehose is pushed
// into the queue synchronously and never blocks the producer, so a slow/stalled
// consumer would otherwise grow it without limit and exhaust server memory.
// Events are best-effort telemetry (DB authoritative + heartbeat) so drop-oldest
// is safe under sustained backpressure. At ~1KB/event the default is ≈10MB
// worst-case per stalled connection. See routes/instance/event.ts for the full
// rationale (incl. heartbeat/sentinel lag under saturation). Tune via env.
const EVENT_QUEUE_CAPACITY = Number(process.env["MIMOCODE_EVENT_QUEUE_CAPACITY"]) || 10_000

export const GlobalDisposedEvent = BusEvent.define("global.disposed", z.object({}))

async function streamEvents(c: Context, subscribe: (q: AsyncQueue<string | null>) => () => void) {
  return streamSSE(c, async (stream) => {
    const q = new AsyncQueue<string | null>({ capacity: EVENT_QUEUE_CAPACITY })
    let done = false

    q.push(
      JSON.stringify({
        payload: {
          type: "server.connected",
          properties: {},
        },
      }),
    )

    // Send heartbeat every 10s to prevent stalled proxy streams.
    const heartbeat = setInterval(() => {
      q.push(
        JSON.stringify({
          payload: {
            type: "server.heartbeat",
            properties: {},
          },
        }),
      )
    }, 10_000)

    const stop = () => {
      if (done) return
      done = true
      clearInterval(heartbeat)
      unsub()
      q.push(null)
      if (q.dropped > 0) log.warn("global event dropped under backpressure", { dropped: q.dropped })
      log.info("global event disconnected", { buffered: q.size })
    }

    const unsub = subscribe(q)

    stream.onAbort(stop)

    try {
      for await (const data of q) {
        if (data === null) return
        await stream.writeSSE({ data })
      }
    } finally {
      stop()
    }
  })
}

export const GlobalRoutes = lazy(() =>
  new Hono()
    .post("/selected-history", async (c) => {
      c.header("Cache-Control", "private, no-store")
      if (!ManagedProvider.enabled() || !c.req.raw.body) return c.json({state:"unavailable"}, 403)
      const reader = c.req.raw.body.getReader()
      const chunks: Uint8Array[] = []; let total = 0
      try {
        while (true) {
          const part = await reader.read(); if (part.done) break
          total += part.value.byteLength
          if (total > 2 * 1024 * 1024) { await reader.cancel(); return c.json({state:"unavailable"}, 413) }
          chunks.push(part.value)
        }
        const bytes = Buffer.concat(chunks.map(chunk => Buffer.from(chunk)))
        const input = JSON.parse(new TextDecoder("utf-8", {fatal:true}).decode(bytes))
        return c.json(input?.phase === "reconcile" ? selectedHistoryRead(input) : selectedHistory(input))
      } catch {
        return c.json({state:"unavailable"}, 409)
      } finally { reader.releaseLock() }
    })
    .get(
      "/health",
      describeRoute({
        summary: "Get health",
        description: "Get health information about the OpenCode server.",
        operationId: "global.health",
        responses: {
          200: {
            description: "Health information",
            content: {
              "application/json": {
                schema: resolver(z.object({ healthy: z.literal(true), version: z.string() })),
              },
            },
          },
        },
      }),
      async (c) => {
        return c.json({ healthy: true, version: InstallationVersion })
      },
    )
    .get(
      "/event",
      describeRoute({
        summary: "Get global events",
        description: "Subscribe to global events from the OpenCode system using server-sent events.",
        operationId: "global.event",
        responses: {
          200: {
            description: "Event stream",
            content: {
              "text/event-stream": {
                schema: resolver(
                  z
                    .object({
                      directory: z.string(),
                      project: z.string().optional(),
                      workspace: z.string().optional(),
                      payload: z.union([...BusEvent.payloads(), ...SyncEvent.payloads()]),
                    })
                    .meta({
                      ref: "GlobalEvent",
                    }),
                ),
              },
            },
          },
        },
      }),
      async (c) => {
        log.info("global event connected")
        c.header("Cache-Control", "no-cache, no-transform")
        c.header("X-Accel-Buffering", "no")
        c.header("X-Content-Type-Options", "nosniff")

        return streamEvents(c, (q) => {
          async function handler(event: any) {
            q.push(JSON.stringify(event))
          }
          GlobalBus.on("event", handler)
          return () => GlobalBus.off("event", handler)
        })
      },
    )
    .get(
      "/config",
      describeRoute({
        summary: "Get global configuration",
        description: "Retrieve the current global OpenCode configuration settings and preferences.",
        operationId: "global.config.get",
        responses: {
          200: {
            description: "Get global config info",
            content: {
              "application/json": {
                schema: resolver(Config.Info),
              },
            },
          },
        },
      }),
      async (c) => {
        return c.json(await AppRuntime.runPromise(Config.Service.use((cfg) => cfg.getGlobal())))
      },
    )
    .patch(
      "/config",
      describeRoute({
        summary: "Update global configuration",
        description: "Update global OpenCode configuration settings and preferences.",
        operationId: "global.config.update",
        responses: {
          200: {
            description: "Successfully updated global config",
            content: {
              "application/json": {
                schema: resolver(Config.Info),
              },
            },
          },
          ...errors(400),
        },
      }),
      validator("json", Config.Info),
      async (c) => {
        const config = c.req.valid("json")
        const next = await AppRuntime.runPromise(Config.Service.use((cfg) => cfg.updateGlobal(config)))
        return c.json(next)
      },
    )
    .post(
      "/dispose",
      describeRoute({
        summary: "Dispose instance",
        description: "Clean up and dispose all OpenCode instances, releasing all resources.",
        operationId: "global.dispose",
        responses: {
          200: {
            description: "Global disposed",
            content: {
              "application/json": {
                schema: resolver(z.boolean()),
              },
            },
          },
        },
      }),
      async (c) => {
        await Instance.disposeAll()
        GlobalBus.emit("event", {
          directory: "global",
          payload: {
            type: GlobalDisposedEvent.type,
            properties: {},
          },
        })
        return c.json(true)
      },
    )
    .post(
      "/upgrade",
      describeRoute({
        summary: "Upgrade opencode",
        description: "Upgrade opencode to the specified version or latest if not specified.",
        operationId: "global.upgrade",
        responses: {
          200: {
            description: "Upgrade result",
            content: {
              "application/json": {
                schema: resolver(
                  z.union([
                    z.object({
                      success: z.literal(true),
                      version: z.string(),
                    }),
                    z.object({
                      success: z.literal(false),
                      error: z.string(),
                    }),
                  ]),
                ),
              },
            },
          },
          ...errors(400),
        },
      }),
      validator(
        "json",
        z.object({
          target: z.string().optional(),
        }),
      ),
      async (c) => {
        const result = await AppRuntime.runPromise(
          Installation.Service.use((svc) =>
            Effect.gen(function* () {
              const method = yield* svc.method()
              if (method === "unknown") {
                return { success: false as const, status: 400 as const, error: "Unknown installation method" }
              }

              const target = c.req.valid("json").target || (yield* svc.latest(method))
              const result = yield* Effect.catch(
                svc.upgrade(method, target).pipe(Effect.as({ success: true as const, version: target })),
                (err) =>
                  Effect.succeed({
                    success: false as const,
                    status: 500 as const,
                    error: err instanceof Error ? err.message : String(err),
                  }),
              )
              if (!result.success) return result
              return { ...result, status: 200 as const }
            }),
          ),
        )
        if (!result.success) {
          return c.json({ success: false, error: result.error }, result.status)
        }
        const target = result.version
        GlobalBus.emit("event", {
          directory: "global",
          payload: {
            type: Installation.Event.Updated.type,
            properties: { version: target },
          },
        })
        return c.json({ success: true, version: target })
      },
    )
    .get(
      "/import/scan",
      describeRoute({
        summary: "Scan external session sources",
        description:
          "Detect availability and session counts of external AI tool session stores (Claude Code, Codex, opencode). Read-only.",
        operationId: "global.import.scan",
        responses: {
          200: {
            description: "Per-source availability and counts",
            content: {
              "application/json": {
                schema: resolver(
                  z.record(
                    z.enum(["cc", "codex", "opencode"]),
                    z.object({ available: z.boolean(), sessions: z.number(), imported: z.number() }),
                  ),
                ),
              },
            },
          },
        },
      }),
      async (c) => {
        return c.json(await ExternalImport.scan())
      },
    )
    .post(
      "/import/run",
      describeRoute({
        summary: "Import external sessions",
        description:
          "Import sessions from external AI tools (Claude Code, Codex, opencode) into mimocode. Idempotent; pass force to re-sync. Per-source failures are not thrown as HTTP errors — they are collected into the corresponding stats.errors[] while other sources continue.",
        operationId: "global.import.run",
        responses: {
          200: {
            description: "Per-source import stats",
            content: {
              "application/json": {
                schema: resolver(
                  z.record(
                    z.enum(["cc", "codex", "opencode"]),
                    z.object({
                      scanned: z.number(),
                      imported: z.number(),
                      resynced: z.number(),
                      skipped: z.number(),
                      errors: z.array(z.string()),
                    }),
                  ),
                ),
              },
            },
          },
          ...errors(400),
        },
      }),
      validator(
        "json",
        z.object({
          sources: z.array(z.enum(["cc", "codex", "opencode"])).optional(),
          force: z.boolean().optional(),
        }),
      ),
      async (c) => {
        const { sources, force } = c.req.valid("json")
        return c.json(await ExternalImport.runAll({ sources, force }))
      },
    ),
)
