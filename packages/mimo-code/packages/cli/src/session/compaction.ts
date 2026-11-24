import { clearGcfSessionViews } from './gcf-session-view'
import BUILTIN_COMPACTION_INSTRUCTIONS from "../agent/prompt/compaction.txt"
import { createHash } from "node:crypto"
import type { MaintenanceWireCandidate } from "./context-inspection"
import { BusEvent } from "@/bus/bus-event"
import { Bus } from "@/bus"
import * as Session from "./session"
import { SessionID, MessageID, PartID } from "./schema"
import { Provider } from "../provider"
import { MessageV2 } from "./message-v2"
import z from "zod"
import { Token } from "../util"
import { Log } from "../util"
import { SessionProcessor } from "./processor"
import { Agent, resolveStoredAgent } from "@/agent/agent"
import { Plugin } from "@/plugin"
import { Config } from "@/config"
import { ModelID, ProviderID } from "@/provider/schema"
import { Cause, Effect, Exit, Layer, Context } from "effect"
import { InstanceState } from "@/effect"
import { isOverflow as overflow, usable } from "./overflow"
import { makeRuntime } from "@/effect/run-service"
import { fn } from "@/util/fn"
import { errorMessage } from "@/util/error"
import { NamedError } from "@mimo-ai/shared/util/error"
import path from "path"
import { SessionPrefixSnapshot } from "./prefix-snapshot"

const log = Log.create({ service: "session.compaction" })

export const Event = {
  Compacted: BusEvent.define(
    "session.compacted",
    z.object({
      sessionID: SessionID.zod,
      // Optional: identifies which agent slice was compacted. undefined or
      // "main" means the main-agent compaction; any other value is a subagent
      // slice. Subscribers that only care about the main context (e.g. the
      // cron-bridge sentinel cache) can filter on this.
      agentID: z.string().optional(),
      summaryMessageID: MessageID.zod.optional(),
      summaryPartIDs: z.array(PartID.zod).max(128).optional(),
      sourceCoverage: z.enum(["complete","incomplete"]).optional(),
      maintenanceWire: z.object({ attestation:z.string().max(8192), dispatchID:z.string().max(256), bodySHA256:z.string().regex(/^[0-9a-f]{64}$/), promptSHA256:z.string().regex(/^[0-9a-f]{64}$/), wireProfile:z.enum(["openai-chat","codex-responses"]), instructionsSHA256:z.string().regex(/^[0-9a-f]{64}$/).optional(), instructionsClass:z.literal("builtin-compaction-v1").optional(), sourceMessageID:MessageID.zod, sourcePartID:PartID.zod }).optional(),
    }),
  ),
}

export const FailedError = NamedError.create(
  "SessionCompactionFailedError",
  z.object({
    sessionID: SessionID.zod,
    message: z.string(),
  }),
)

export const COMPACTION_TAIL_BUDGET = 40_000
export const COMPACTION_TOOL_RESULT_LIMIT = 8_000
const DEFAULT_TAIL_TURNS = 2
export function tailTurnsFor(cfg: Config.Info): number {
  return cfg.compaction?.tail_turns ?? DEFAULT_TAIL_TURNS
}
export function preserveRecentBudgetFor(cfg: Config.Info, model: Provider.Model): number {
  return cfg.compaction?.preserve_recent_tokens ?? Math.min(8_000, Math.max(2_000, Math.floor(usable({cfg, model}) * 0.25)))
}
const FILE_MANIFEST_LIMIT = 5
const TAIL_SHRINK_METADATA = "compaction_tail_shrunk_tokens"

type CompactionTrigger = "manual" | "automatic" | "provider-overflow"

function record(value: unknown): Record<string, unknown> | undefined {
  if (!value || typeof value !== "object" || Array.isArray(value)) return
  return value as Record<string, unknown>
}

function string(value: unknown) {
  return typeof value === "string" ? value : undefined
}

function number(value: unknown) {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined
}

function relativeFile(file: string, worktree: string) {
  return (path.isAbsolute(file) ? path.relative(worktree, file) : file).replaceAll("\\", "/")
}

function xmlText(value: string) {
  return value.replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
}

function readLabel(part: MessageV2.ToolPart) {
  if (part.state.status !== "completed") return "read"
  const offset = number(part.state.input.offset) ?? 1
  const range = part.state.output.match(/Showing lines (\d+)-(\d+)/)
  const contentEnd = Array.from(part.state.output.matchAll(/(?:^|\n)(\d+):/g)).at(-1)?.[1]
  const end = range?.[2] ?? contentEnd
  if (offset === 1 && part.state.metadata.truncated !== true && (part.state.output.includes("(End of file") || !end))
    return "read: full"
  return end ? `read: lines ${offset}-${end}` : `read: from line ${offset}`
}

export function buildFileManifest(messages: MessageV2.WithParts[], env: { worktree: string }) {
  const touched = new Map<string, { path: string; events: string[] }>()
  const add = (file: string | undefined, event: string) => {
    if (!file) return undefined
    const normalized = relativeFile(file, env.worktree)
    if (!normalized) return undefined
    const current = touched.get(normalized) ?? { path: normalized, events: [] }
    if (current.events.at(-1) !== event) current.events.push(event)
    touched.delete(normalized)
    touched.set(normalized, current)
    return normalized
  }

  for (const message of messages) {
    const mutatedFiles = new Set<string>()
    for (const part of message.parts) {
      if (part.type === "patch") {
        for (const file of part.files) {
          if (mutatedFiles.has(relativeFile(file, env.worktree))) continue
          add(file, "edited")
        }
        continue
      }
      if (part.type !== "tool" || part.state.status !== "completed") continue
      const inputPath =
        string(part.state.input.file_path) ?? string(part.state.input.path) ?? string(part.state.input.notebook_path)
      if (part.tool === "read") {
        add(inputPath, readLabel(part))
        continue
      }
      if (part.tool === "apply_patch" && Array.isArray(part.state.metadata.files)) {
        for (const item of part.state.metadata.files) {
          const file = record(item)
          if (!file) continue
          const normalized = add(
            string(file.relativePath) ?? string(file.filePath),
            file.type === "add" ? "written" : "edited",
          )
          if (normalized) mutatedFiles.add(normalized)
        }
        continue
      }
      if (!["write", "edit", "multiedit", "notebook_edit", "str_replace"].includes(part.tool)) continue
      const metadataPath = string(part.state.metadata.filepath) ?? string(record(part.state.metadata.filediff)?.file)
      const written =
        (part.tool === "write" && part.state.metadata.exists === false) ||
        (part.tool === "edit" && part.state.input.old_string === "")
      const file = add(inputPath ?? metadataPath, written ? "written" : "edited")
      if (file) mutatedFiles.add(file)
    }
  }

  const files = Array.from(touched.values()).slice(-FILE_MANIFEST_LIMIT)
  if (!files.length) return
  return [
    "## Attachments",
    "",
    "<files-touched>",
    "These files were read or edited earlier in this session. Their contents have been",
    "removed from context — re-read any file you need before editing it.",
    "",
    ...files.map((file) => `- ${xmlText(file.path)} (${file.events.slice(-3).join(", then ")})`),
    "</files-touched>",
  ].join("\n")
}

export function groupByApiRound(messages: MessageV2.WithParts[]) {
  return messages.reduce<MessageV2.WithParts[][]>((rounds, message) => {
    if (message.info.role === "user" || rounds.length === 0) {
      rounds.push([message])
      return rounds
    }
    rounds.at(-1)!.push(message)
    return rounds
  }, [])
}

export function shrinkLargeToolResults(messages: MessageV2.WithParts[]) {
  return messages.map(
    (message): MessageV2.WithParts => ({
      info: message.info,
      parts: message.parts.map((part) => {
        if (part.type !== "tool" || part.state.status !== "completed") return part
        const tokens = Token.estimate(part.state.output)
        if (tokens <= COMPACTION_TOOL_RESULT_LIMIT) return part
        return {
          ...part,
          state: {
            ...part.state,
            output: `[Tool result omitted during compaction: ${tokens} tokens. Re-run "${part.tool}" if this result is needed.]`,
            providerOutput: undefined,
            attachments: undefined,
            metadata: { ...part.state.metadata, [TAIL_SHRINK_METADATA]: tokens },
          },
        }
      }),
    }),
  )
}

export const buildTail = Effect.fn("SessionCompaction.buildTail")(function* (input: {
  messages: MessageV2.WithParts[]
  model: Provider.Model
  budget?: number
  languageProvider?: string
}) {
  const rounds = groupByApiRound(input.messages)
  const kept: MessageV2.WithParts[][] = []
  let used = 0
  for (let i = rounds.length - 1; i >= 0; i--) {
    const round = shrinkLargeToolResults(rounds[i])
    const cost = Token.estimate(
      JSON.stringify(yield* Effect.promise(() => MessageV2.toModelMessages(round, input.model, {
        languageProvider: input.languageProvider,
      }))),
    )
    if (used + cost > (input.budget ?? COMPACTION_TAIL_BUDGET)) break
    kept.unshift(round)
    used += cost
  }
  return kept.flat()
})

function compactedToolCalls(messages: MessageV2.WithParts[]) {
  return messages.flatMap((message) =>
    message.parts.flatMap((part) => {
      if (part.type !== "tool" || part.state.status !== "completed") return []
      const tokens = number(part.state.metadata[TAIL_SHRINK_METADATA])
      return tokens === undefined ? [] : [{ call_id: part.callID, tokens }]
    }),
  )
}

export function buildSummaryMessage(summary: string, trigger: CompactionTrigger, hasTail: boolean) {
  return [
    `<conversation-summary trigger="${trigger}">`,
    "The earlier conversation has been compacted into the summary below.",
    hasTail
      ? "Complete API rounds that arrived while compaction was running follow this summary."
      : "No additional API rounds arrived while compaction was running.",
    "",
    summary.trim(),
    "</conversation-summary>",
  ].join("\n")
}

export interface Interface {
  readonly isOverflow: (input: {
    tokens: MessageV2.Assistant["tokens"]
    model: Provider.Model
  }) => Effect.Effect<boolean>
  readonly process: (input: {
    parentID: MessageID
    messages: MessageV2.WithParts[]
    sessionID: SessionID
    auto: boolean
    overflow?: boolean
    agentID?: string
    cfg?: Config.Info
    requestContext?: import("./llm").StreamInput["requestContext"]
  }) => Effect.Effect<"continue" | "stop" | "text-repeat">
  readonly create: (input: {
    sessionID: SessionID
    agent: string
    model: { providerID: ProviderID; modelID: ModelID }
    auto: boolean
    overflow?: boolean
    agentID?: string
  }) => Effect.Effect<MessageID>
}

export class Service extends Context.Service<Service, Interface>()("@opencode/SessionCompaction") {}

export const layer: Layer.Layer<
  Service,
  never,
  | Bus.Service
  | Config.Service
  | Session.Service
  | Agent.Service
  | Plugin.Service
  | SessionProcessor.Service
  | Provider.Service
> = Layer.effect(
  Service,
  Effect.gen(function* () {
    const bus = yield* Bus.Service
    const config = yield* Config.Service
    const session = yield* Session.Service
    const agents = yield* Agent.Service
    const plugin = yield* Plugin.Service
    const processors = yield* SessionProcessor.Service
    const provider = yield* Provider.Service

    const isOverflow = Effect.fn("SessionCompaction.isOverflow")(function* (input: {
      tokens: MessageV2.Assistant["tokens"]
      model: Provider.Model
    }) {
      return overflow({ cfg: yield* config.get(), tokens: input.tokens, model: input.model })
    })

    type ProcessInput = {
      parentID: MessageID
      messages: MessageV2.WithParts[]
      sessionID: SessionID
      auto: boolean
      overflow?: boolean
      agentID?: string
      cfg?: Config.Info
      requestContext?: import("./llm").StreamInput["requestContext"]
    }
    // What a compaction step has committed so far; the exit backstop reads it.
    type Progress = {
      boundary: boolean
      projected: boolean
      summary?: () => MessageV2.Assistant
    }

    const runCompaction = Effect.fnUntraced(function* (input: ProcessInput, progress: Progress) {
      clearGcfSessionViews(input.sessionID)
      const snapshotLen = input.messages.length
      const parentIdx = input.messages.findLastIndex((m) => m.info.id === input.parentID)
      const parent = parentIdx >= 0 ? input.messages[parentIdx] : undefined
      if (!parent || parent.info.role !== "user") {
        throw new Error(`Compaction parent must be a user message: ${input.parentID}`)
      }
      const boundaryUser = parent.info
      const compactionPart = parent.parts.find((part): part is MessageV2.CompactionPart => part.type === "compaction")
      progress.boundary = compactionPart !== undefined
      const promptConfig = yield* session.resolvePrompt({ sessionID: input.sessionID })
      const userMessage = {
        ...parent.info,
        // The compaction agent owns its summarization prompt. The session's
        // extra system prompt is already persisted and will be restored on the
        // replay/next user turn; injecting it here can change the summary task.
        system: undefined,
        systemMode: undefined,
        harness: promptConfig.harness,
      }

      // Reuse the effective conversation before this synthetic boundary without
      // restoring raw history removed by an earlier compaction or checkpoint.
      const scoped = compactionPart
        ? [...MessageV2.filterCompacted([...input.messages.slice(0, parentIdx)].reverse()), parent]
        : input.messages

      let messages = scoped
      let replay:
        | {
            info: MessageV2.User
            parts: MessageV2.Part[]
          }
        | undefined
      if (input.overflow) {
        const idx = scoped.findIndex((m) => m.info.id === input.parentID)
        for (let i = idx - 1; i >= 0; i--) {
          const msg = scoped[i]
          if (msg.info.role === "user" && !msg.parts.some((p) => p.type === "compaction" || p.type === "checkpoint")) {
            replay = { info: msg.info, parts: msg.parts }
            messages = scoped.slice(0, i)
            break
          }
        }
        const hasContent =
          replay &&
          messages.some(
            (m) => m.info.role === "user" && !m.parts.some((p) => p.type === "compaction" || p.type === "checkpoint"),
          )
        if (!hasContent) {
          replay = undefined
          messages = scoped
        }
      }

      const agent = yield* agents.get("compaction")
      const history = compactionPart && messages.at(-1)?.info.id === input.parentID ? messages.slice(0, -1) : messages
      const requestUser = history.findLast(
        (message): message is MessageV2.WithParts & { info: MessageV2.User } => message.info.role === "user",
      )
      if (!requestUser) {
        log.warn("compaction history has no user message", { sessionID: input.sessionID })
        yield* session.removeMessage({ sessionID: input.sessionID, messageID: input.parentID })
        return "stop" as const
      }
      const parentAgent = yield* resolveStoredAgent(agents, requestUser.info.agent)
      // The boundary carries the model the caller asked to compact with; the
      // turns after it run on that model too, so it drives the summary, the
      // prefix-snapshot lookup and the tail budget.
      const baseRef = compactionPart ? boundaryUser.model : requestUser.info.model
      const parentModel = yield* provider.getModel(baseRef.providerID, baseRef.modelID)
      const sameModelAsRequest =
        requestUser.info.model.providerID === parentModel.providerID && requestUser.info.model.modelID === parentModel.id
      const summaryModelRef = {
        providerID: parentModel.providerID,
        modelID: parentModel.id,
        variant: sameModelAsRequest ? requestUser.info.model.variant : baseRef.variant,
      }
      const parentSession = yield* session.get(input.sessionID)
      const profileKey = SessionPrefixSnapshot.profileKey({
        providerID: parentModel.providerID,
        modelID: parentModel.id,
        agent: parentAgent.name,
        agentID: requestUser.info.agentID ?? "main",
        harness: promptConfig.harness,
        systemMode: promptConfig.systemMode,
        system: promptConfig.system ?? "",
        permission: Agent.runtimePermission(parentAgent, parentSession.permission),
      })
      const frozen = yield* SessionPrefixSnapshot.get(input.sessionID, profileKey)
      if (!frozen) {
        log.warn("compaction prefix snapshot missing", { sessionID: input.sessionID, profileKey })
      }
      if (frozen && !frozen.tools) {
        log.warn("compaction prefix snapshot missing advertised tools", {
          sessionID: input.sessionID,
          profileKey,
        })
      }
      const model =
        input.overflow && agent.model
          ? yield* provider.getModel(agent.model.providerID, agent.model.modelID)
          : parentModel
      // Allow plugins to inject context or replace compaction prompt.
      const compacting = yield* plugin.trigger(
        "experimental.session.compacting",
        { sessionID: input.sessionID },
        { context: [], prompt: undefined },
      )
      const prompt =
        compacting.prompt ??
        [agent.prompt, ...compacting.context]
          .filter((item): item is string => !!item)
          .join("\n\n")
      // Reserve a suffix before the summary request: excluded rounds cannot
      // later be displaced by arrivals without losing unsummarized context.
      const cfg = input.cfg ?? (yield* config.get())
      const budget = preserveRecentBudgetFor(cfg, parentModel)
      const rounds = groupByApiRound(history)
      const eligible = compactionPart ? rounds.slice(Math.max(1, rounds.length - Math.max(0, tailTurnsFor(cfg)))).flat() : []
      const languageProvider = (yield* provider.getLanguage(parentModel)).provider
      const retained = yield* buildTail({ messages: eligible, model: parentModel, budget, languageProvider })
      const retainedIndex = retained.length ? history.findIndex((item) => item.info.id === retained[0].info.id) : history.length
      const head = history.slice(0, retainedIndex < 0 ? history.length : retainedIndex)
      const retainedCost = retained.length ? Token.estimate(JSON.stringify(yield* Effect.promise(() =>
        MessageV2.toModelMessages(retained, parentModel, { languageProvider })))) : 0
      const msgs = structuredClone(head)
      yield* plugin.trigger("experimental.chat.messages.transform", {}, { messages: msgs })
      // Only one unchanged host-attested text part and the fixed compiler
      // summary prompt can form a candidate. All other inputs stay unknown.
      let maintenance: MaintenanceWireCandidate | undefined
      const original = head.length === 1 ? head[0] : undefined
      const transformed = msgs.length === 1 ? msgs[0] : undefined
      const visible = original?.parts.filter((p) => p.type === "text" && !p.ignored)
      const after = transformed?.parts.filter((p) => p.type === "text" && !p.ignored)
      if (compacting.prompt === undefined && compacting.context.length === 0 && original?.info.role === "user" &&
          transformed?.info.id === original.info.id && transformed.info.role === "user" && visible?.length === 1 && after?.length === 1 &&
          visible[0].type === "text" && after[0].type === "text" && !visible[0].synthetic && !after[0].synthetic &&
          original.parts.every((p) => p.type === "text") && transformed.parts.every((p) => p.type === "text") &&
          original.parts.length === transformed.parts.length && visible[0].id === after[0].id && visible[0].text === after[0].text &&
          typeof visible[0].metadata?.openclankMaintenanceSource === "string" &&
          after[0].metadata?.openclankMaintenanceSource === visible[0].metadata.openclankMaintenanceSource) {
        maintenance = {source:{messageID:original.info.id,partID:visible[0].id,
          digest:createHash("sha256").update(visible[0].text,"utf8").digest("hex"),sourceRevision:"unavailable"},
          promptSHA256:createHash("sha256").update(prompt,"utf8").digest("hex"),attestation:visible[0].metadata.openclankMaintenanceSource}
      }
      let maintenanceWire: {dispatchID:string;bodySHA256:string;wireProfile:"openai-chat"|"codex-responses";instructionsSHA256?:string;instructionsClass?:"builtin-compaction-v1"} | undefined
      const modelMessages = yield* MessageV2.toModelMessagesEffect(
        msgs,
        model,
        {
          ...(input.overflow ? { stripMedia: true } : { collapseCheckpointTail: true }),
          languageProvider: (yield* provider.getLanguage(model)).provider,
        },
      )
      const ctx = yield* InstanceState.context
      // Only the checked-in compaction instruction, without user system
      // content or extra frozen tools, has known source provenance here.
      if (maintenance && agent.native === true && agent.name === "compaction" &&
          agent.prompt === BUILTIN_COMPACTION_INSTRUCTIONS && !promptConfig.system && !frozen?.system?.length && !frozen?.tools?.length) {
        maintenance.trustedInstructionsSHA256 = createHash("sha256").update(BUILTIN_COMPACTION_INSTRUCTIONS,"utf8").digest("hex")
      }
      const msg: MessageV2.Assistant = {
        id: MessageID.ascending(),
        role: "assistant",
        parentID: input.parentID,
        sessionID: input.sessionID,
        agentID: input.agentID ?? undefined,
        mode: "compaction",
        agent: "compaction",
        variant: summaryModelRef.variant,
        summary: true,
        path: {
          cwd: ctx.directory,
          root: ctx.worktree,
        },
        cost: 0,
        tokens: {
          output: 0,
          input: 0,
          reasoning: 0,
          cache: { read: 0, write: 0 },
        },
        modelID: model.id,
        providerID: model.providerID,
        time: {
          created: Date.now(),
        },
      }
      yield* session.updateMessage(msg)
      progress.summary = () => msg
      const processor = yield* processors.create({
        assistantMessage: msg,
        sessionID: input.sessionID,
        model,
        actorExecution: input.requestContext?.actorExecution,
        cfg: input.cfg,
      })
      progress.summary = () => processor.message
      const summaryRequest = {
        role: "user" as const,
        content: [{ type: "text" as const, text: prompt }],
      }
      const result = yield* processor.process({
        user: {
          ...requestUser.info,
          model: summaryModelRef,
          system: promptConfig.system,
          systemMode: promptConfig.systemMode,
          harness: promptConfig.harness,
        },
        agent: parentAgent,
        permission: parentSession.permission,
        sessionID: input.sessionID,
        requestContext: input.requestContext,
        maintenanceWire: maintenance,
        onMaintenanceWire: async (proof) => {
          const turn = input.requestContext?.actorExecution
          if (maintenanceWire || !maintenance || !turn || turn.actorID !== "main" || !turn.rootOperationID || !turn.innerTurnID) return
          try {
            const managed = await import("@/acp/managed-provider")
            const result = await managed.managedHostCall("_openclank/session/v1/maintenance/ack", {
              sessionID:turn.sessionID, rootOperationID:turn.rootOperationID, actorID:"main", executionRevision:turn.executionRevision,
              innerTurnID:turn.innerTurnID, summaryMessageID:msg.id, attestation:maintenance.attestation,
              sourceMessageID:maintenance.source.messageID, sourcePartID:maintenance.source.partID, sourceTextSHA256:maintenance.source.digest,
              dispatchID:proof.dispatchID, bodySHA256:proof.bodySHA256, promptSHA256:maintenance.promptSHA256, wireProfile:proof.wireProfile,
              ...(proof.instructionsSHA256 ? {instructionsSHA256:proof.instructionsSHA256} : {}),
              ...(proof.instructionsClass ? {instructionsClass:proof.instructionsClass} : {}),
            })
            if (result.accepted && !maintenanceWire) maintenanceWire = proof
          } catch { /* an unconfirmed ACK cannot authorize capture */ }
        },

        tools: frozen?.tools ? SessionPrefixSnapshot.restoreTools(frozen.tools) : {},
        activeTools: frozen?.tools?.map((item) => item.name),
        toolChoice: "auto",
        system: [],
        prebuiltSystem: frozen?.system,
        messages: [...modelMessages, summaryRequest],
        mergeTurnContextBeforeLastMessage: true,
        model,
      })

      const rollback = Effect.fn("SessionCompaction.rollback")(function* (message: string) {
        if (!processor.message.error) {
          processor.message.error = new MessageV2.InvalidOutputError({ message }).toObject()
          processor.message.finish = "error"
          yield* session.updateMessage(processor.message)
        }
        yield* session.removeMessage({ sessionID: input.sessionID, messageID: input.parentID })
        return "stop" as const
      })

      if (result === "overflow") {
        processor.message.error = new MessageV2.ContextOverflowError({
          message: replay
            ? "Conversation history too large to compact - exceeds model context limit"
            : input.overflow
              ? "Session too large to compact - context exceeds model limit even after stripping media"
              : "Session too large to compact - context exceeds model limit at the message boundary",
        }).toObject()
        processor.message.finish = "error"
        yield* session.updateMessage(processor.message)
        return yield* rollback("Compaction exceeded the model context limit")
      }

      if (result === "text-repeat") return yield* rollback("Compaction produced repeated text")
      if (result === "stop") return yield* rollback("Compaction failed before producing a summary")
      // The compaction processor is created without awaitNewInput, so no input supersedes it.
      if (result === "superseded") return yield* Effect.die(new Error("Compaction step cannot be superseded"))
      if (
        !MessageV2.parts(msg.id).some((part) => part.type === "text" && part.text.trim().length > 0)
      )
        return yield* rollback("Compaction produced no usable summary")

      let droppedArrived = false
      if (compactionPart) {
        const current = yield* session.messages({
          sessionID: input.sessionID,
          agentID: input.agentID ?? "main",
        })
        const arrived = current.slice(snapshotLen).filter((message) => message.info.id !== msg.id)
        const tail = yield* buildTail({
          messages: arrived,
          model: parentModel,
          budget: Math.max(0, budget - retainedCost),
          languageProvider,
        })
        const keptIndex = tail.length ? arrived.findIndex((item) => item.info.id === tail[0].info.id) : arrived.length
        const omitted = arrived.slice(0, keptIndex < 0 ? arrived.length : keptIndex)
        droppedArrived = omitted.length > 0
        const summary = MessageV2.parts(msg.id)
          .filter((part): part is MessageV2.TextPart => part.type === "text")
          .map((part) => part.text)
          .join("\n")
        const trigger: CompactionTrigger = input.overflow ? "provider-overflow" : input.auto ? "automatic" : "manual"
        yield* session.updatePart({
          ...compactionPart,
          projection: {
            version: 1,
            summary_message_id: msg.id,
            summary: buildSummaryMessage(summary, trigger, tail.length > 0),
            manifest: buildFileManifest(head, { worktree: ctx.worktree }),
            trigger,
            retained_start_id: retained.at(0)?.info.id,
            dropped_arrived_start_id: omitted.at(0)?.info.id,
            dropped_arrived_end_id: omitted.at(-1)?.info.id,
            tail_start_id: tail.at(0)?.info.id,
            tail_end_id: arrived.at(-1)?.info.id,
            compacted_tool_calls: compactedToolCalls([...retained, ...tail]),
          },
        })
        progress.projected = true
      }

      if (result === "continue" && input.auto) {
        if (replay) {
          const original = replay.info
          const replayMsg = yield* session.updateMessage({
            id: MessageID.ascending(),
            role: "user",
            sessionID: input.sessionID,
            agentID: input.agentID ?? undefined,
            time: { created: Date.now() },
            agent: original.agent,
            model: original.model,
            format: original.format,
            tools: original.tools,
            task_id: original.task_id,
            system: promptConfig.system,
            systemMode: promptConfig.systemMode,
            harness: promptConfig.harness,
          })
          for (const part of replay.parts) {
            if (part.type === "compaction") continue
            yield* session.updatePart({
              ...part,
              id: PartID.ascending(),
              messageID: replayMsg.id,
              sessionID: input.sessionID,
            })
          }
        }

        if (!replay) {
          const info = yield* provider.getProvider(userMessage.model.providerID)
          if (
            (yield* plugin.trigger(
              "experimental.compaction.autocontinue",
              {
                sessionID: input.sessionID,
                agent: userMessage.agent,
                model: yield* provider.getModel(userMessage.model.providerID, userMessage.model.modelID),
                provider: {
                  source: info.source,
                  info,
                  options: info.options,
                },
                message: userMessage,
                overflow: input.overflow === true,
              },
              { enabled: true },
            )).enabled
          ) {
            const continueMsg = yield* session.updateMessage({
              id: MessageID.ascending(),
              role: "user",
              sessionID: input.sessionID,
              agentID: input.agentID ?? undefined,
              time: { created: Date.now() },
              agent: userMessage.agent,
              model: userMessage.model,
              task_id: userMessage.task_id,
            })
            const text =
              (input.overflow
                ? "The previous request exceeded the provider's size limit due to large media attachments. The conversation was compacted and media files were removed from context. If the user was asking about attached images or files, explain that the attachments were too large to process and suggest they try again with smaller or fewer files.\n\n"
                : "") +
              "Continue if you have next steps, or stop and ask for clarification if you are unsure how to proceed."
            yield* session.updatePart({
              id: PartID.ascending(),
              messageID: continueMsg.id,
              sessionID: input.sessionID,
              type: "text",
              // Internal marker for auto-compaction followups so provider plugins
              // can distinguish them from manual post-compaction user prompts.
              // This is not a stable plugin contract and may change or disappear.
              metadata: { compaction_continue: true },
              synthetic: true,
              text,
              time: {
                start: Date.now(),
                end: Date.now(),
              },
            })
          }
        }
      }

      if (processor.message.error) return "stop"
      if (result === "continue") {
        // Exact identity is useful even while source/wire coverage remains
        // incomplete. Failure to enumerate does not fail compaction.
        let summaryPartIDs: PartID[] | undefined
        try {
          const parts = MessageV2.parts(processor.message.id)
          if (parts.length <= 128) summaryPartIDs = parts.map((part) => part.id)
        } catch { /* coverage remains incomplete */ }
        const completeSourceCoverage = !droppedArrived && !!maintenanceWire && (maintenanceWire.wireProfile === "openai-chat" ||
          (maintenanceWire.wireProfile === "codex-responses" && maintenanceWire.instructionsClass === "builtin-compaction-v1"))
        const turn = input.requestContext?.actorExecution
        if (completeSourceCoverage && maintenanceWire && summaryPartIDs?.length && summaryPartIDs.length <= 16 && turn?.actorID === "main" &&
            turn.rootOperationID && turn.innerTurnID) {
          // Optional pending staging is bounded and runs before this turn can
          // retire. A timeout cannot authorize capture or extend the turn.
          const rootOperationID = turn.rootOperationID, innerTurnID = turn.innerTurnID
          yield* Effect.tryPromise({
            try: async () => {
              const managed = await import("@/acp/managed-provider")
              const call = managed.managedHostCall("_openclank/session/v1/maintenance/stage", {
                sessionID:turn.sessionID,rootOperationID,actorID:"main",executionRevision:turn.executionRevision,
                innerTurnID,summaryMessageID:processor.message.id,summaryPartIDs,
              }).catch(() => ({accepted:false}))
              await Promise.race([call,new Promise((resolve)=>setTimeout(resolve,200))])
            }, catch: () => new Error("maintenance staging unavailable"),
          }).pipe(Effect.catch(() => Effect.void))
        }
        clearGcfSessionViews(input.sessionID)
        yield* bus.publish(Event.Compacted, {
          sessionID: input.sessionID,
          ...(input.agentID ? { agentID: input.agentID } : {}),
          summaryMessageID: processor.message.id,
          ...(summaryPartIDs ? { summaryPartIDs } : {}),
          sourceCoverage: completeSourceCoverage ? "complete" : "incomplete",
          ...(maintenance && maintenanceWire ? { maintenanceWire: {
            attestation:maintenance.attestation, dispatchID:maintenanceWire.dispatchID, bodySHA256:maintenanceWire.bodySHA256,
            promptSHA256:maintenance.promptSHA256, wireProfile:maintenanceWire.wireProfile,
            ...(maintenanceWire.instructionsSHA256 ? {instructionsSHA256:maintenanceWire.instructionsSHA256} : {}),
            ...(maintenanceWire.instructionsClass ? {instructionsClass:maintenanceWire.instructionsClass} : {}), sourceMessageID:MessageID.make(maintenance.source.messageID), sourcePartID:PartID.make(maintenance.source.partID),
          }} : {}),
        })
      }
      return result
    })

    // A failed compaction leaves no boundary. `rollback` covers the outcomes
    // the step decides itself; this covers every other exit (typed failure,
    // defect, interruption) that happens before the projection is written.
    const abandon = Effect.fnUntraced(function* (input: ProcessInput, progress: Progress, cause: Cause.Cause<unknown>) {
      if (!progress.boundary || progress.projected) return
      const summary = progress.summary?.()
      if (summary && !summary.error) {
        summary.error = Cause.hasInterruptsOnly(cause)
          ? new MessageV2.AbortedError({ message: "Compaction was interrupted" }).toObject()
          : new MessageV2.InvalidOutputError({
              message: `Compaction failed: ${errorMessage(Cause.squash(cause))}`,
            }).toObject()
        summary.finish = "error"
        summary.time.completed ??= Date.now()
        yield* session.updateMessage(summary)
      }
      yield* session.removeMessage({ sessionID: input.sessionID, messageID: input.parentID })
      log.warn("compaction abandoned; boundary removed", {
        sessionID: input.sessionID,
        boundary: input.parentID,
        cause: Cause.pretty(cause),
      })
    })

    const processCompaction = Effect.fn("SessionCompaction.process")(function* (input: ProcessInput) {
      const progress: Progress = { boundary: false, projected: false }
      return yield* runCompaction(input, progress).pipe(
        Effect.onExit((exit) =>
          Exit.isSuccess(exit)
            ? Effect.void
            : abandon(input, progress, exit.cause).pipe(
                Effect.catchCause((cleanup) =>
                  Effect.sync(() =>
                    log.error("compaction cleanup failed", {
                      sessionID: input.sessionID,
                      boundary: input.parentID,
                      cause: Cause.pretty(cleanup),
                    }),
                  ),
                ),
              ),
        ),
      )
    })

    const create = Effect.fn("SessionCompaction.create")(function* (input: {
      sessionID: SessionID
      agent: string
      model: { providerID: ProviderID; modelID: ModelID }
      auto: boolean
      overflow?: boolean
      agentID?: string
    }) {
      // Tag the synthetic boundary message with agent_id so per-actor
      // filterCompactedEffect lookups stop at this row when scoping by the
      // same agent_id (subagent compaction stays inside its own scope).
      const msg = yield* session.updateMessage({
        id: MessageID.ascending(),
        role: "user",
        model: input.model,
        sessionID: input.sessionID,
        agentID: input.agentID ?? undefined,
        agent: input.agent,
        time: { created: Date.now() },
      })
      yield* session.updatePart({
        id: PartID.ascending(),
        messageID: msg.id,
        sessionID: msg.sessionID,
        type: "compaction",
        auto: input.auto,
        overflow: input.overflow,
      })
      // Boundary-insert path also drops effective context from the model's
      // view — publish Compacted so downstream caches (cron sentinel etc)
      // reset on their side too. Same event shape as processCompaction.
      clearGcfSessionViews(input.sessionID)
      yield* bus.publish(Event.Compacted, {
        sessionID: input.sessionID,
        ...(input.agentID ? { agentID: input.agentID } : {}),
      })
      return msg.id
    })

    return Service.of({
      isOverflow,
      process: processCompaction,
      create,
    })
  }),
)

export const defaultLayer = Layer.suspend(() =>
  layer.pipe(
    Layer.provide(Provider.defaultLayer),
    Layer.provide(Session.defaultLayer),
    Layer.provide(SessionProcessor.defaultLayer),
    Layer.provide(Agent.defaultLayer),
    Layer.provide(Plugin.defaultLayer),
    Layer.provide(Bus.layer),
    Layer.provide(Config.defaultLayer),
  ),
)

const { runPromise } = makeRuntime(Service, defaultLayer)

export async function isOverflow(input: { tokens: MessageV2.Assistant["tokens"]; model: Provider.Model }) {
  return runPromise((svc) => svc.isOverflow(input))
}

export const create = fn(
  z.object({
    sessionID: SessionID.zod,
    agent: z.string(),
    model: z.object({ providerID: ProviderID.zod, modelID: ModelID.zod }),
    auto: z.boolean(),
    overflow: z.boolean().optional(),
    agentID: z.string().optional(),
  }),
  (input) => runPromise((svc) => svc.create(input)),
)

export * as SessionCompaction from "./compaction"
