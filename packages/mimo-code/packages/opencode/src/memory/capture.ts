import { Context, Effect, Layer } from "effect"
import { Bus } from "../bus"
import { Config } from "../config"
import { MessageV2 } from "../session/message-v2"
import { makeResolver } from "../history/resolve"
import { Session } from "../session"
import { Log } from "../util"
import { getSharedMcpClient } from "./mcp-client"

const log = Log.create({ service: "memory.capture" })

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
) {
  if (!userText.trim()) return
  try {
    const client = await getSharedMcpClient()
    await client.callTool({
      name: "capture",
      arguments: {
        content: userText,
        session_key: sessionID,
        session_id: sessionID,
        workspace_id: workspaceId,
        workspace_path: workspacePath,
        source: "mimo",
      },
    })
    if (assistantText.trim()) {
      await client.callTool({
        name: "capture",
        arguments: {
          content: assistantText,
          session_key: sessionID,
          session_id: sessionID,
          workspace_id: workspaceId,
          workspace_path: workspacePath,
          source: "mimo_assistant",
        },
      })
    }
  } catch (err) {
    log.warn("fm-mcp capture failed", { error: String(err) })
  }
}

export const layer: Layer.Layer<CaptureService, never, Bus.Service | Session.Service | Config.Service> = Layer.effect(
  CaptureService,
  Effect.gen(function* () {
    const config = yield* Config.Service
    const cfg = yield* config.get()
    if (cfg.memory?.provider !== "frankenmemory") {
      return CaptureService.of({ init: () => Effect.succeed(undefined) })
    }

    const bus = yield* Bus.Service
    const sessions = yield* Session.Service

    const resolver = makeResolver()

    const lastCaptured = new Map<string, string>()

    yield* bus.subscribeCallback(MessageV2.Event.PartUpdated, (evt) => {
      const part = evt.properties.part
      if (part.type !== "text") return
      if (part.synthetic) return
      if (!part.time?.end) return

      const sessionID = evt.properties.sessionID
      const messageID = part.messageID

      Effect.runPromise(
        Effect.gen(function* () {
          const role = yield* resolver.role(messageID)
          if (role !== "assistant") return

          const text = part.text ?? ""
          if (!text.trim()) return

          const key = `${sessionID}:${text.slice(0, 100)}`
          if (lastCaptured.get(sessionID) === key) return
          lastCaptured.set(sessionID, key)

          const msgs = yield* sessions.messages({ sessionID, agentID: "*" })
          const lastUser = msgs.findLast((m) => m.info.role === "user")
          const userText = lastUser?.parts
            .filter((p) => p.type === "text" && !p.synthetic)
            .map((p) => (p as MessageV2.TextPart).text ?? "")
            .join("\n") ?? ""

          const sessionInfo = yield* sessions.get(sessionID)
          const workspacePath = sessionInfo.directory
          const workspaceId = sessionInfo.projectID ?? sessionInfo.directory
          yield* Effect.promise(() =>
            captureTurn(userText, text, sessionID, workspaceId, workspacePath),
          )
        }),
      ).catch((err) => log.warn("capture wire error", { error: String(err) }))
    })

    return CaptureService.of({
      init: () => Effect.succeed(undefined),
    })
  }),
)

export const defaultLayer = layer.pipe(
  Layer.provide(Bus.defaultLayer),
  Layer.provide(Session.defaultLayer),
  Layer.provide(Config.defaultLayer),
)
