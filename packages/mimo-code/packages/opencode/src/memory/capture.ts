import { Context, Effect, Layer } from "effect"
import { Bus } from "../bus"
import { Config } from "../config"
import { MessageV2 } from "../session/message-v2"
import { Session } from "../session"
import { Provider } from "../provider"
import { Log } from "../util"
import { getSharedMcpClient } from "./mcp-client"
import { CHAT_WORKSPACE, memorySessionScope, unregisterMemorySessionScope } from "./session-scope"
import { extractSafely } from "./graph-extract"

const log = Log.create({ service: "memory.capture" })

// Messages produced by these agents never enter memory from this path:
// compaction summaries are owned by compaction-capture.ts (capturing them
// here too would double-store every summary), and any future utility agent
// that streams session parts belongs on this list as well.
const NON_CHAT_AGENTS = new Set(["compaction"])

export interface Interface {
  readonly init: () => Effect.Effect<void>
}

export class CaptureService extends Context.Service<CaptureService, Interface>()("@opencode/Memory.Capture") {}

async function captureTurn(
  userText: string,
  assistantText: string,
  sessionID: string,
  workspaceId: string,
  workspacePath: string,
  owner: string,
  userMessageID: string,
  assistantMessageID: string,
) {
  if (!userText.trim()) return false
  try {
    const client = await getSharedMcpClient()
    const result = await client.callTool({
      name: "capture",
      arguments: {
        user_text: userText,
        assistant_text: assistantText,
        capture_mode: "candidate",
        session_key: sessionID,
        session_id: sessionID,
        workspace_id: workspaceId,
        workspace_path: workspacePath,
        owner,
        source_event_id: `${sessionID}:${userMessageID}:${assistantMessageID}`,
        source_message_ids: [userMessageID, assistantMessageID],
        source: "mimo",
      },
    })
    const content = result.content as Array<{ type: string; text?: string }> | undefined
    const payload = JSON.parse(content?.[0]?.text ?? "{}") as { record_ids?: string[] }
    return (payload.record_ids ?? []).some((id) => id.startsWith("m_"))
  } catch (err) {
    log.warn("fm-mcp capture failed", { error: String(err) })
    return false
  }
}

export const layer: Layer.Layer<CaptureService, never, Bus.Service | Session.Service | Config.Service | Provider.Service> = Layer.effect(
  CaptureService,
  Effect.gen(function* () {
    const config = yield* Config.Service
    const bus = yield* Bus.Service
    const sessions = yield* Session.Service
    const providerSvc = yield* Provider.Service

    // Everything instance-dependent happens inside init(), NOT at layer
    // construction: the layer is memoized once per process, but Config and
    // Bus subscriptions are InstanceState-scoped — subscribing here would
    // register on whichever instance happened to be current at first demand
    // and every other instance's sessions would capture nothing. init() runs
    // once per instance from InstanceBootstrap (same idiom as
    // Metrics.subscribe), and its subscriptions die with the instance state.
    const init = () =>
      Effect.gen(function* () {
        const cfg = yield* config.get()
        if (cfg.memory?.provider !== "frankenmemory") {
          log.info("capture disabled", { provider: cfg.memory?.provider ?? "unset" })
          return
        }
        log.info("capture subscriber registering")

        yield* bus.subscribeCallback(Session.Event.Deleted, (evt) => {
          unregisterMemorySessionScope(evt.properties.sessionID)
        })

        yield* bus.subscribeCallback(MessageV2.Event.Updated, (evt) => {
          const sessionID = evt.properties.sessionID
          const info = evt.properties.info
          if (info.role !== "assistant" || !info.time.completed) return
          if (NON_CHAT_AGENTS.has(info.agent)) return
          const messageID = info.id

          Effect.runPromise(
            Effect.gen(function* () {
              const msgs = yield* sessions.messages({ sessionID, agentID: "*" })
              const self = msgs.find((m) => m.info.id === messageID)
              if (!self || self.info.role !== "assistant") return
              const assistantInfo = self.info
              const text = self.parts
                .filter((p): p is MessageV2.TextPart => p.type === "text" && !p.synthetic)
                .map((p) => p.text ?? "")
                .join("\n")
              if (!text.trim()) return

              const parent = msgs.find(
                (m) => m.info.id === assistantInfo.parentID && m.info.role === "user",
              )
              if (!parent) {
                log.warn("capture skipped: assistant parent user message missing", { sessionID, messageID })
                return
              }
              const userText = parent.parts
                .filter((p) => p.type === "text" && !p.synthetic)
                .map((p) => (p as MessageV2.TextPart).text ?? "")
                .join("\n")

              const sessionInfo = yield* sessions.get(sessionID)
              const scope = memorySessionScope(sessionID)
              if (!scope?.owner) {
                log.warn("capture skipped: ACP session has no Odysseus owner", { sessionID })
                return
              }
              const workspacePath = scope.workspacePath || sessionInfo.directory
              const workspaceId = scope.workspaceId || CHAT_WORKSPACE
              const accepted = yield* Effect.promise(() =>
                captureTurn(
                  userText,
                  text,
                  sessionID,
                  workspaceId,
                  workspacePath,
                  scope.owner,
                  parent.info.id,
                  messageID,
                ),
              )
              if (accepted) {
                yield* extractSafely({
                  userText,
                  assistantText: "",
                  sessionID,
                  owner: scope.owner,
                  workspaceId,
                  workspacePath,
                })
              }
            }).pipe(
              Effect.provideService(Config.Service, config),
              Effect.provideService(Provider.Service, providerSvc),
            ),
          ).catch((err) => log.warn("capture wire error", { error: String(err) }))
        })
      })

    return CaptureService.of({
      init: () => init(),
    })
  }),
)

export const defaultLayer = layer.pipe(
  Layer.provide(Bus.defaultLayer),
  Layer.provide(Session.defaultLayer),
  Layer.provide(Config.defaultLayer),
  Layer.provide(Provider.defaultLayer),
)
