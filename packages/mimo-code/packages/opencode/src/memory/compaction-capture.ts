import { Context, Effect, Layer } from "effect"
import { Bus } from "../bus"
import { Config } from "../config"
import { Session } from "../session"
import { SessionCompaction } from "../session/compaction"
import { Log } from "../util"
import { getSharedMcpClient } from "./mcp-client"

const log = Log.create({ service: "memory.compaction-capture" })

export interface Interface {
  readonly init: () => Effect.Effect<void>
}

export class CompactionCaptureService extends Context.Service<CompactionCaptureService, Interface>()(
  "@opencode/Memory.CompactionCapture",
) {}

export const layer: Layer.Layer<CompactionCaptureService, never, Bus.Service | Session.Service | Config.Service> =
  Layer.effect(
    CompactionCaptureService,
    Effect.gen(function* () {
      const config = yield* Config.Service
      const cfg = yield* config.get()
      if (cfg.memory?.provider !== "frankenmemory") {
        return CompactionCaptureService.of({ init: () => Effect.succeed(undefined) })
      }

      const bus = yield* Bus.Service
      const sessions = yield* Session.Service

      yield* bus.subscribeCallback(SessionCompaction.Event.Compacted, (evt) => {
        const sessionID = evt.properties.sessionID
        const agentID = evt.properties.agentID

        Effect.runPromise(
          Effect.gen(function* () {
            const msgs = yield* sessions.messages({ sessionID, agentID: "*" })
            const summary = msgs.findLast(
              (m) =>
                m.info.role === "assistant" &&
                m.info.summary === true &&
                m.info.agent === "compaction" &&
                (agentID ? m.info.agentID === agentID : true),
            )
            if (!summary) {
              log.warn("no compaction summary found", { sessionID, agentID })
              return
            }

            const text = summary.parts
              .filter((p) => p.type === "text")
              .map((p) => p.text ?? "")
              .join("\n")
            if (!text.trim()) return

            const c = yield* Effect.promise(() => getSharedMcpClient())
            yield* Effect.promise(() =>
              c.callTool({
                name: "capture",
                arguments: {
                  content: text,
                  session_key: sessionID,
                  session_id: sessionID,
                  source: "mimo",
                  category: "compaction",
                  kind: "episodic",
                  metadata: JSON.stringify({
                    event: "compaction",
                    parent_message_id: "parentID" in summary.info ? summary.info.parentID : undefined,
                    agent_id: summary.info.agentID,
                  }),
                },
              }),
            )
            log.info("captured compaction summary", { sessionID, agentID })
          }),
        ).catch((err) => log.warn("compaction capture failed", { error: String(err) }))
      })

      return CompactionCaptureService.of({
        init: () => Effect.succeed(undefined),
      })
    }),
  )

export const defaultLayer = layer.pipe(
  Layer.provide(Bus.defaultLayer),
  Layer.provide(Session.defaultLayer),
  Layer.provide(Config.defaultLayer),
)
