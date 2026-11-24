import z from "zod"
import { Effect, Layer, Context } from "effect"
import { Bus } from "@/bus"
import { Snapshot } from "@/snapshot"
import { Storage } from "@/storage"
import * as Session from "./session"
import { MessageV2 } from "./message-v2"
import { CaptureMembership, matchesActionRef } from "./capture-membership"
import { SessionID, MessageID } from "./schema"
import { Flag } from "@/flag/flag"

function unquoteGitPath(input: string) {
  if (!input.startsWith('"')) return input
  if (!input.endsWith('"')) return input
  const body = input.slice(1, -1)
  const bytes: number[] = []

  for (let i = 0; i < body.length; i++) {
    const char = body[i]!
    if (char !== "\\") {
      bytes.push(char.charCodeAt(0))
      continue
    }

    const next = body[i + 1]
    if (!next) {
      bytes.push("\\".charCodeAt(0))
      continue
    }

    if (next >= "0" && next <= "7") {
      const chunk = body.slice(i + 1, i + 4)
      const match = chunk.match(/^[0-7]{1,3}/)
      if (!match) {
        bytes.push(next.charCodeAt(0))
        i++
        continue
      }
      bytes.push(parseInt(match[0], 8))
      i += match[0].length
      continue
    }

    const escaped =
      next === "n"
        ? "\n"
        : next === "r"
          ? "\r"
          : next === "t"
            ? "\t"
            : next === "b"
              ? "\b"
              : next === "f"
                ? "\f"
                : next === "v"
                  ? "\v"
                  : next === "\\" || next === '"'
                    ? next
                    : undefined

    bytes.push((escaped ?? next).charCodeAt(0))
    i++
  }

  return Buffer.from(bytes).toString()
}

/** Counts are observed metadata, not authorization or complete coverage. */
export type LoreDiffUnavailable = { code: "LORE_DIFF_UNAVAILABLE"; coverage: "partial" | "unavailable"; recordedActions: number; pendingActions: number }
function unavailable(messages: MessageV2.WithParts[]): LoreDiffUnavailable | undefined {
  let starts = 0, finishes = 0, missing = false, recordedActions = 0, pendingActions = 0
  for (const message of messages) for (const part of message.parts) {
    if (part.type === "step-start") {
      starts++
      if (!part.snapshot || part.captureMembership !== undefined) missing = true
      const parsed = CaptureMembership.safeParse(part.captureMembership)
      if (parsed.success) for (const member of parsed.data.members) {
        if (member.state === "complete" && member.actionRef && matchesActionRef(member.actionRef, parsed.data, member.actionID, member.toolCallID)) recordedActions = Math.min(4096, recordedActions + 1)
        else pendingActions = Math.min(4096, pendingActions + 1)
      }
    }
    if (part.type === "step-finish") { finishes++; if (!part.snapshot) missing = true }
  }
  if (!starts || !finishes || starts !== finishes) missing = true
  if (!missing) return undefined
  return { code: "LORE_DIFF_UNAVAILABLE", coverage: recordedActions || pendingActions ? "partial" : "unavailable", recordedActions, pendingActions }
}

export interface Interface {
  readonly summarize: (input: { sessionID: SessionID; messageID: MessageID }) => Effect.Effect<void>
  readonly diff: (input: { sessionID: SessionID; messageID?: MessageID }) => Effect.Effect<Snapshot.FileDiff[]>
  readonly diffResult: (input: { sessionID: SessionID; messageID?: MessageID }) => Effect.Effect<{ coverage: LoreDiffUnavailable } | { diffs: Snapshot.FileDiff[] }>
  readonly computeDiff: (input: { messages: MessageV2.WithParts[] }) => Effect.Effect<Snapshot.FileDiff[]>
}

export class Service extends Context.Service<Service, Interface>()("@opencode/SessionSummary") {}

export const layer = Layer.effect(
  Service,
  Effect.gen(function* () {
    const sessions = yield* Session.Service
    const snapshot = yield* Snapshot.Service
    const storage = yield* Storage.Service
    const bus = yield* Bus.Service

    const computeDiff = Effect.fn("SessionSummary.computeDiff")(function* (input: { messages: MessageV2.WithParts[] }) {
      if (unavailable(input.messages)) return yield* Effect.die(new Error("Lore diff unavailable"))
      let from: string | undefined
      let to: string | undefined
      for (const item of input.messages) {
        if (!from) {
          for (const part of item.parts) {
            if (part.type === "step-start" && part.snapshot) {
              from = part.snapshot
              break
            }
          }
        }
        for (const part of item.parts) {
          if (part.type === "step-finish" && part.snapshot) to = part.snapshot
        }
      }
      if (from && to) return yield* snapshot.diffFull(from, to)
      return []
    })

    const summarize = Effect.fn("SessionSummary.summarize")(function* (input: {
      sessionID: SessionID
      messageID: MessageID
    }) {
      const all = yield* sessions.messages({ sessionID: input.sessionID, agentID: "main" })
      if (!all.length) return

      if (unavailable(all)) return
      const diffs = yield* computeDiff({ messages: all })
      yield* sessions.setSummary({
        sessionID: input.sessionID,
        summary: {
          additions: diffs.reduce((sum, x) => sum + x.additions, 0),
          deletions: diffs.reduce((sum, x) => sum + x.deletions, 0),
          files: diffs.length,
        },
      })
      if (!Flag.OPEN_CLANK_PRIVATE_RUNTIME) yield* storage.write(["session_diff", input.sessionID], diffs).pipe(Effect.ignore)
      yield* bus.publish(Session.Event.Diff, { sessionID: input.sessionID, diff: diffs })

      const messages = all.filter(
        (m) => m.info.id === input.messageID || (m.info.role === "assistant" && m.info.parentID === input.messageID),
      )
      const target = messages.find((m) => m.info.id === input.messageID)
      if (!target || target.info.role !== "user") return
      const msgDiffs = yield* computeDiff({ messages })
      target.info.summary = { ...target.info.summary, diffs: msgDiffs }
      yield* sessions.updateMessage(target.info)
    })

    const diffResult = Effect.fn("SessionSummary.diffResult")(function* (input: { sessionID: SessionID; messageID?: MessageID }) {
      const all = yield* sessions.messages({ sessionID: input.sessionID, agentID: "*" })
      const messages = input.messageID ? all.filter((item) => item.info.id === input.messageID ||
        (item.info.role === "assistant" && item.info.parentID === input.messageID)) : all
      if (input.messageID && !messages.some((item) => item.info.id === input.messageID && item.info.role === "user"))
        return { coverage: { code: "LORE_DIFF_UNAVAILABLE", coverage: "unavailable", recordedActions: 0, pendingActions: 0 } } as const
      const coverage = unavailable(messages)
      if (coverage) return { coverage }
      const diffs = yield* computeDiff({ messages })
      return { diffs: diffs.map((item) => ({ ...item, file: unquoteGitPath(item.file) })) }
    })
    const diff = Effect.fn("SessionSummary.diff")(function* (input: { sessionID: SessionID; messageID?: MessageID }) {
      const result = yield* diffResult(input)
      if ("coverage" in result) return yield* Effect.die(new Error("Lore diff unavailable"))
      return result.diffs
    })

    return Service.of({ summarize, diff, diffResult, computeDiff })
  }),
)

export const defaultLayer = Layer.suspend(() =>
  layer.pipe(
    Layer.provide(Session.defaultLayer),
    Layer.provide(Snapshot.defaultLayer),
    Layer.provide(Storage.defaultLayer),
    Layer.provide(Bus.layer),
  ),
)

export const DiffInput = z.object({
  sessionID: SessionID.zod,
  messageID: MessageID.zod.optional(),
})

export * as SessionSummary from "./summary"
