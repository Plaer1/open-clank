import { Context, Effect, Layer } from "effect"
import { Bus } from "../bus"
import { Config } from "../config"
import { Session } from "../session"
import { SessionCompaction } from "../session/compaction"
import { Log } from "../util"
import { getSharedMcpClient } from "./mcp-client"
import { memorySessionScope } from "./session-scope"

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
      const bus = yield* Bus.Service
      const sessions = yield* Session.Service

      // Same shape as MemoryCapture: the layer is process-memoized while
      // Config and Bus subscriptions are InstanceState-scoped, so all
      // instance work waits for init(), which InstanceBootstrap runs once
      // per instance.
      const init = Effect.fn("CompactionCapture.init")(function* () {
        const cfg = yield* config.get()
        if (cfg.memory?.provider !== "frankenmemory") {
          return
        }

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
            const scope = memorySessionScope(sessionID)
            if (!scope?.owner) {
              log.warn("compaction capture skipped: session has no Odysseus owner", { sessionID })
              return
            }

            const c = yield* Effect.promise(() => getSharedMcpClient())
            yield* Effect.promise(() =>
              c.callTool({
                name: "capture",
                arguments: {
                  user_text: "",
                  assistant_text: text,
                  capture_mode: "raw_only",
                  session_key: sessionID,
                  session_id: sessionID,
                  owner: scope.owner,
                  workspace_id: scope.workspaceId,
                  workspace_path: scope.workspacePath,
                  source_event_id: `${sessionID}:compaction:${summary.info.id}`,
                  source_message_ids: [summary.info.id],
                  source: "mimo",
                  category: "compaction",
                  metadata: {
                    event: "compaction",
                    parent_message_id: "parentID" in summary.info ? summary.info.parentID : undefined,
                    agent_id: summary.info.agentID,
                  },
                },
              }),
            )
            log.info("captured compaction summary", { sessionID, agentID })
          }),
        ).catch((err) => log.warn("compaction capture failed", { error: String(err) }))
        })
      })

      return CompactionCaptureService.of({
        init: () => init(),
      })
    }),
  )

export const defaultLayer = layer.pipe(
  Layer.provide(Bus.defaultLayer),
  Layer.provide(Session.defaultLayer),
  Layer.provide(Config.defaultLayer),
)
