import { captureScopeEvidence, captureSdkEvidence, flushCaptures } from "@/provider/logging-transport"
import {
  RequestError,
  type Agent as ACPAgent,
  type AgentSideConnection,
  type AuthenticateRequest,
  type AuthMethod,
  type CancelNotification,
  type ForkSessionRequest,
  type ForkSessionResponse,
  type InitializeRequest,
  type InitializeResponse,
  type ListSessionsRequest,
  type ListSessionsResponse,
  type LoadSessionRequest,
  type NewSessionRequest,
  type PermissionOption,
  type PromptRequest,
  type ResumeSessionRequest,
  type ResumeSessionResponse,
  type Role,
  type SessionInfo,
  type SetSessionModelRequest,
  type SessionConfigOption,
  type SetSessionConfigOptionRequest,
  type SetSessionConfigOptionResponse,
  type SetSessionModeRequest,
  type SetSessionModeResponse,
  type ToolCallContent,
  type ToolKind,
  type Usage,
} from "@agentclientprotocol/sdk"

import { Log } from "../util"
import { pathToFileURL } from "url"
import path from "node:path"
import { createHash } from "node:crypto"
import { Filesystem } from "../util"
import { Hash } from "@mimo-ai/shared/util/hash"
import { ACPSessionManager, reserveProvisionalSessionID } from "./session"
import type { ACPConfig } from "./types"
import { SessionID, MessageID } from "@/session/schema"
import { Session as NativeSession } from "@/session"
import { hostSourceForMessage, type SourceMessage } from "@/session/fork-source"
import { SessionRunState } from "@/session/run-state"
import { Instance } from "@/project/instance"
import { Effect } from "effect"
import { Provider } from "../provider"
import { ModelID, ProviderID } from "../provider/schema"
import { Agent as AgentModule } from "../agent/agent"
import { AppRuntime } from "@/effect/app-runtime"
import { Installation } from "@/installation"
import { MessageV2 } from "@/session/message-v2"
import { Config } from "@/config"
import { Question } from "@/question"
import { Skill } from "@/skill"
import { Flag } from "@/flag/flag"
import { ConfigMCP } from "@/config/mcp"
import { z } from "zod"
import { LoadAPIKeyError } from "ai"
import type { AssistantMessage, Event, OpencodeClient, SessionMessageResponse, ToolPart } from "@mimo-ai/sdk/v2"
import { applyPatch } from "diff"
import { InstallationVersion } from "@/installation/version"
import { SYSTEM_SPAWNED_AGENT_TYPES } from "@/agent/config"
import { OpenClankManagedProtocol } from "./openclank-protocol"
import { ManagedProvider } from "./managed-provider"
import { ManagedProviderControl } from "./provider-control"
import { ManagedOperations } from "./managed-operations"
import { contextWindow } from "@/session/overflow"
import { preserveRecentBudgetFor, tailTurnsFor } from "@/session/compaction"
import { defaultThresholdsFor, resolveThresholds } from "@/session/prune"

/** Consume only the slash name and one separator; argument bytes stay native-owned. */
export function parseCommandText(text: string) {
  const match = /^\/([^\s]+)(?:[ \t]|\r?\n)?([\s\S]*)$/.exec(text)
  return match ? { name: match[1], args: match[2] } : undefined
}

/** Durable host-to-native user anchors; content and attachments never leave this map. */
export function sourceMessageAnchors(messages: SourceMessage[]) {
  const anchors = messages.filter((message) => message.info.role === "user" && (!message.info.agentID || message.info.agentID === "main")).flatMap((message) => {
    const source = hostSourceForMessage(message)
    return source ? [{ nativeMessageID: message.info.id, hostSourceMessageID: source }] : []
  })
  if (new Set(anchors.map((anchor) => anchor.hostSourceMessageID)).size !== anchors.length) {
    throw new RequestError(-32602, "Multiple native messages share a host source boundary", { code: "SOURCE_MAPPING_AMBIGUOUS" })
  }
  return anchors
}

export function nativeForkReceipt(messages: Array<{ parts: Array<{ type: string; metadata?: Record<string, unknown> }> }>) {
  const receipts = messages.flatMap((message) => message.parts.filter((part) => part.type === "text" && part.metadata?.nativeForkReceipt).map((part) => part.metadata!.nativeForkReceipt))
  if (receipts.length > 1) throw new RequestError(-32602, "Native fork completion receipt is ambiguous", { code: "FORK_RECEIPT_AMBIGUOUS" })
  const parsed = z.object({ sourceSessionID: z.string(), targetSessionID: z.string(), fingerprint: z.string().regex(/^[a-f0-9]{64}$/), sourceMessageCount: z.number().int().nonnegative() }).strict().safeParse(receipts[0])
  return parsed.success ? parsed.data : null
}

const ManagedForkRequest = z.object({
  sourceSessionID: SessionID.zod,
  targetSessionID: SessionID.zod,
  directory: z.string().refine(path.isAbsolute),
  hostSourceMap: z.record(z.string().min(1).max(256), z.string().min(1).max(256)),
  messageID: MessageID.zod.optional(),
  title: z.string().min(1).max(512).optional(),
}).strict()

type ModeOption = { id: string; name: string; description?: string }
export type ModelOption = {
  modelId: string
  name: string
  baseModelId: string
  preset?: string
  variant?: string
}

const DEFAULT_VARIANT_VALUE = "default"

const log = Log.create({ service: "acp-agent" })

async function getContextLimit(
  sdk: OpencodeClient,
  providerID: ProviderID,
  modelID: ModelID,
  directory: string,
): Promise<number | null> {
  const providers = await sdk.config
    .providers({ directory })
    .then((x) => x.data?.providers ?? [])
    .catch((error) => {
      log.error("failed to get providers for context limit", { error })
      return []
    })

  const provider = providers.find((p) => p.id === providerID)
  const model = provider?.models[modelID]
  return model?.limit.context ?? null
}

async function sendUsageUpdate(
  connection: AgentSideConnection,
  sdk: OpencodeClient,
  sessionID: string,
  directory: string,
): Promise<void> {
  const messages = await sdk.session
    .messages({ sessionID, directory, agent_id: "main" }, { throwOnError: true })
    .then((x) => x.data)
    .catch((error) => {
      log.error("failed to fetch messages for usage update", { error })
      return undefined
    })

  if (!messages) return

  const assistantMessages = messages.filter(
    (m): m is { info: AssistantMessage; parts: SessionMessageResponse["parts"] } => m.info.role === "assistant",
  )

  const lastAssistant = assistantMessages[assistantMessages.length - 1]
  if (!lastAssistant) return

  const msg = lastAssistant.info
  if (!msg.providerID || !msg.modelID) return
  const size = await getContextLimit(sdk, ProviderID.make(msg.providerID), ModelID.make(msg.modelID), directory)

  if (!size) {
    // Cannot calculate usage without known context size
    return
  }

  const used = msg.tokens.input + (msg.tokens.cache?.read ?? 0)
  const totalCost = assistantMessages.reduce((sum, m) => sum + m.info.cost, 0)

  await connection
    .sessionUpdate({
      sessionId: sessionID,
      update: {
        sessionUpdate: "usage_update",
        used,
        size,
        cost: { amount: totalCost, currency: "USD" },
      },
    })
    .catch((error) => {
      log.error("failed to send usage update", { error })
    })
}

export async function init({ sdk: _sdk }: { sdk: OpencodeClient }) {
  return {
    create: (connection: AgentSideConnection, fullConfig: ACPConfig) => {
      return new Agent(connection, fullConfig)
    },
  }
}

export class Agent implements ACPAgent {
  private connection: AgentSideConnection
  private config: ACPConfig
  private sdk: OpencodeClient
  private sessionManager: ACPSessionManager
  private eventAbort = new AbortController()
  private eventStarted = false
  private terminalDeliveries = new Map<string, { busy: boolean; finish: () => void; fail: (error: Error) => void }>()
  private bashSnapshots = new Map<string, string>()
  private toolStarts = new Set<string>()
  private permissionQueues = new Map<string, Promise<void>>()
  private questionQueues = new Map<string, Promise<void>>()
  private providerControl = new ManagedProviderControl.ControlPlane()
  private managedOperations: ManagedOperations.Router
  private reservedForkTargets = new Set<string>()
  private forkSources = new Set<string>()
  private forkReceipts = new Map<string, { fingerprint: string; owner: string; sourceChatID: string; targetChatID: string; result: Promise<Record<string, unknown>> }>()
  private commandNames = new Map<string, Set<string>>()
  private actorFeeds = new Map<string, { rootTurnID: string; revision: number; baseline: Set<string> }>()
  private actorRoots = new Map<string, string>()
  private actorRevisions = new Map<string, number>()
  private permissionOptions: PermissionOption[] = [
    { optionId: "once", kind: "allow_once", name: "Allow once" },
    { optionId: "always", kind: "allow_always", name: "Always allow" },
    { optionId: "reject", kind: "reject_once", name: "Reject" },
  ]

  constructor(connection: AgentSideConnection, config: ACPConfig) {
    this.connection = connection
    ManagedProvider.installHostConnection(connection)
    this.managedOperations = new ManagedOperations.Router(connection)
    this.config = config
    this.sdk = config.sdk
    this.sessionManager = new ACPSessionManager(this.sdk)
    this.startEventSubscription()
  }

  async extMethod(method: string, params: Record<string, unknown>): Promise<Record<string, unknown>> {
    if (OpenClankManagedProtocol.isProviderControlMethod(method)) {
      return (await this.providerControl.handle(method, params as never)) as unknown as Record<string, unknown>
    }
    if (method === "_openclank/operations/v1/execute") {
      if (!ManagedProvider.enabled()) {
        throw RequestError.invalidParams("managed operations require Open Clank managed mode")
      }
      return (await this.managedOperations.handle(params)) as unknown as Record<string, unknown>
    }
    if (method === "_openclank/operations/v1/cancel") {
      if (!ManagedProvider.enabled()) throw RequestError.invalidParams("managed operations require Open Clank managed mode")
      return (await this.managedOperations.cancel(params)) as unknown as Record<string, unknown>
    }
    if (method === "_openclank/session/v1/fork/create") {
      if (!ManagedProvider.enabled()) throw RequestError.invalidParams("reserved forks require Open Clank managed mode")
      const parsed = ManagedForkRequest.safeParse(params)
      if (!parsed.success) throw RequestError.invalidParams(parsed.error.flatten())
      const request = parsed.data
      if (request.sourceSessionID === request.targetSessionID || path.resolve(this.sessionManager.get(request.sourceSessionID).cwd) !== path.resolve(request.directory)) throw RequestError.invalidParams("fork source and target must differ and share the requested directory")
      const [sourceBinding, targetBinding] = await Promise.all([ManagedProvider.ensureManagedSessionBinding(request.sourceSessionID), ManagedProvider.ensureManagedSessionBinding(request.targetSessionID)])
      if (!sourceBinding || !targetBinding || sourceBinding.engineSessionID !== request.sourceSessionID || targetBinding.engineSessionID !== request.targetSessionID || sourceBinding.owner !== targetBinding.owner || sourceBinding.authorityWorkspaceID !== targetBinding.authorityWorkspaceID || sourceBinding.stableChatID === targetBinding.stableChatID || path.resolve(sourceBinding.physicalCwd) !== path.resolve(request.directory) || path.resolve(targetBinding.physicalCwd) !== path.resolve(request.directory)) throw new RequestError(-32602, "Fork authority does not match the reserved source and target", { code: "FORK_AUTHORITY_MISMATCH" })
      const fingerprint = createHash("sha256").update(JSON.stringify([request.sourceSessionID, request.targetSessionID, path.resolve(request.directory), Object.entries(request.hostSourceMap).sort(([left], [right]) => left.localeCompare(right)), request.messageID ?? null, request.title ?? null])).digest("hex")
      const prior = this.forkReceipts.get(request.targetSessionID)
      if (prior) {
        if (prior.fingerprint !== fingerprint || prior.owner !== sourceBinding.owner || prior.sourceChatID !== sourceBinding.stableChatID || prior.targetChatID !== targetBinding.stableChatID) throw new RequestError(-32602, "Fork target already belongs to another request", { code: "FORK_REQUEST_CONFLICT" })
        return await prior.result
      }
      if (!this.reservedForkTargets.has(request.targetSessionID)) throw new RequestError(-32602, "Fork target was not reserved by this worker", { code: "FORK_TARGET_UNRESERVED" })
      if (this.forkSources.has(request.sourceSessionID)) throw new RequestError(-32602, "A fork is already capturing this source", { code: "FORK_SOURCE_BUSY" })
      this.reservedForkTargets.delete(request.targetSessionID)
      this.forkSources.add(request.sourceSessionID)
      const result = (async () => {
        try {
          await Instance.provide({ directory: request.directory, fn: () => AppRuntime.runPromise(Effect.gen(function* () {
            const runState = yield* SessionRunState.Service
            yield* runState.assertNotBusy(request.sourceSessionID)
            const session = yield* NativeSession.Service
            return yield* session.fork({ sessionID: request.sourceSessionID, targetSessionID: request.targetSessionID, hostSourceMap: request.hostSourceMap, forkFingerprint: fingerprint, messageID: request.messageID, title: request.title })
          })) })
          await this.sessionManager.adoptManagedFork(request.targetSessionID, request.directory, this.sessionManager.get(request.sourceSessionID).model)
          const messages = await this.sdk.session.messages({ sessionID: request.targetSessionID, directory: request.directory, agent_id: "main" }, { throwOnError: true }).then((reply) => reply.data!)
          return { created: true, sourceSessionID: request.sourceSessionID, targetSessionID: request.targetSessionID, fingerprint, anchors: sourceMessageAnchors(messages), forkReceipt: nativeForkReceipt(messages) }
        } catch (error) {
          if (error instanceof Error && error.message.includes("target already exists")) throw new RequestError(-32602, "Reserved fork target already exists", { code: "FORK_TARGET_EXISTS", targetSessionID: request.targetSessionID })
          // Retain this failed receipt; retry never clones into a partial target.
          await this.sessionManager.adoptManagedFork(request.targetSessionID, request.directory).catch(() => undefined)
          throw new RequestError(-32603, "Fork candidate is incomplete; reconcile or discard this exact reserved target", { code: "FORK_INCOMPLETE", targetSessionID: request.targetSessionID, requiresReconciliation: true })
        } finally {
          this.forkSources.delete(request.sourceSessionID)
        }
      })()
      this.forkReceipts.set(request.targetSessionID, { fingerprint, owner: sourceBinding.owner, sourceChatID: sourceBinding.stableChatID, targetChatID: targetBinding.stableChatID, result })
      return await result
    }
    if (method === "_openclank/session/v1/commands/refresh") {
      if (!ManagedProvider.enabled()) throw RequestError.invalidParams("command refresh requires Open Clank managed mode")
      if (Object.keys(params).some((key) => !["directory", "sessionID", "reloadSkills"].includes(key)) || typeof params.directory !== "string" || !path.isAbsolute(params.directory) || typeof params.sessionID !== "string" || (params.reloadSkills !== undefined && typeof params.reloadSkills !== "boolean")) throw RequestError.invalidParams("sessionID and absolute directory are required; reloadSkills is optional")
      if (path.resolve(this.sessionManager.get(params.sessionID).cwd) !== path.resolve(params.directory)) throw RequestError.invalidParams("session directory does not match command target")
      if (params.reloadSkills === true) await Instance.provide({ directory: params.directory, fn: () => AppRuntime.runPromise(Skill.Service.use((service) => service.reload())) })
      const availableCommands = await this.loadAvailableCommands(params.directory, params.sessionID)
      await this.connection.sessionUpdate({ sessionId: params.sessionID, update: { sessionUpdate: "available_commands_update", availableCommands } })
      return { sessionID: params.sessionID, availableCommands }
    }
    if (method === "_openclank/session/v1/messages/source-map") {
      if (!ManagedProvider.enabled()) throw RequestError.invalidParams("source mapping requires Open Clank managed mode")
      if (Object.keys(params).some((key) => !["directory", "sessionID", "messageID"].includes(key)) || typeof params.directory !== "string" || !path.isAbsolute(params.directory) || typeof params.sessionID !== "string" || (params.messageID !== undefined && !MessageID.zod.safeParse(params.messageID).success)) {
        throw RequestError.invalidParams("sessionID and absolute directory are required")
      }
      if (path.resolve(this.sessionManager.get(params.sessionID).cwd) !== path.resolve(params.directory)) throw RequestError.invalidParams("session directory does not match source target")
      const [messages, session] = await Promise.all([
        this.sdk.session.messages({ sessionID: params.sessionID, directory: params.directory, agent_id: "*" }, { throwOnError: true }).then((result) => result.data!),
        this.sdk.session.get({ sessionID: params.sessionID, directory: params.directory }, { throwOnError: true }).then((result) => result.data!),
      ])
      const cutoff = typeof params.messageID === "string" ? params.messageID : undefined
      const selected = cutoff ? messages.filter((message) => message.info.id < cutoff) : messages
      return { sessionID: params.sessionID, sourceMessageCount: selected.length, anchors: sourceMessageAnchors(selected), forkReceipt: nativeForkReceipt(selected), revert: session.revert ? { messageID: session.revert.messageID, partID: session.revert.partID } : null }
    }
    if (method === "_openclank/session/v1/settings/effective") {
      if (!ManagedProvider.enabled()) throw RequestError.invalidParams("managed settings require Open Clank managed mode")
      if (Object.keys(params).some((key) => !["providerID", "modelID", "directory", "sessionID"].includes(key)) || typeof params.providerID !== "string" || typeof params.modelID !== "string" || typeof params.directory !== "string" || !path.isAbsolute(params.directory) || (params.sessionID !== undefined && typeof params.sessionID !== "string")) {
        throw RequestError.invalidParams("providerID, modelID and absolute directory are required; sessionID is optional")
      }
      const directory = params.directory
      if (typeof params.sessionID === "string" && path.resolve(this.sessionManager.get(params.sessionID).cwd) !== path.resolve(directory)) {
        throw RequestError.invalidParams("session directory does not match settings target")
      }
      const [providers, base, agents] = await Promise.all([
        this.sdk.config.providers({ directory }, { throwOnError: true }).then((x) => x.data!.providers),
        this.sdk.config.get({ directory }, { throwOnError: true }).then((x) => x.data!),
        this.sdk.app.agents({ directory }, { throwOnError: true }).then((x) => x.data!),
      ])
      const provider = providers.find((item) => item.id === params.providerID)
      const model = provider?.models?.[params.modelID]
      if (!provider || !model) throw RequestError.invalidParams("selected model is unavailable")
      const nativeModel = model as unknown as Provider.Model
      const output = model.limit.output
      const input = model.limit.input ?? model.limit.context
      // Public provider discovery strips private options. Resolve managed values
      // from the actual merged Config instead, exactly as turn admission does.
      const cfg = Config.withManagedModelSettings(base as Config.Info, nativeModel)
      const runtimeState = await this.runtimeStateFor(directory)
      const compaction = cfg.compaction ?? {}
      const checkpoint = cfg.checkpoint ?? {}
      const window = contextWindow({ cfg, model: nativeModel })
      const tailTurns = tailTurnsFor(cfg)
      const recent = preserveRecentBudgetFor(cfg, nativeModel)
      const reserved = cfg.compaction?.reserved ?? Math.min(20000, output)
      const rawThresholds = cfg.checkpoint?.thresholds ?? defaultThresholdsFor(window.effective)
      const thresholds = resolveThresholds(rawThresholds, window.effective, cfg.checkpoint?.reserved)
      return { providerID: params.providerID, modelID: params.modelID, target: { directory, sessionID: params.sessionID }, timing: "next_admission", profile: { timing: "next_generation", scope: "worker-generation", runtime_flags: { never_ask: runtimeState.neverAsk, try_best_handoff: Flag.MIMOCODE_ENABLE_TRY_BEST_HANDOFF }, default_agent: runtimeState.defaultAgent, small_model: cfg.small_model ?? null, vision_model: cfg.vision_model ?? null, model_policy: { helper: cfg.small_model || cfg.model_groups?.lite ? "explicit_config" : "inherit_caller", vision: cfg.vision_model || cfg.model_groups?.vision ? "explicit_config" : "inherit_caller_capability_required" }, agent: Object.fromEntries(agents.map((entry) => [entry.name, { description: entry.description, mode: entry.mode, disable: false, hidden: entry.hidden ?? false, model: entry.model ? `${entry.model.providerID}/${entry.model.modelID}` : entry.modelRef, variant: entry.variant, temperature: entry.temperature, top_p: entry.topP, steps: entry.steps, color: entry.color, tool_allowlist: entry.toolAllowlist, native: entry.native }])), model_groups: cfg.model_groups ?? {}, checkpoint: { task_archive_days: cfg.checkpoint?.task_archive_days ?? cfg.checkpoint?.task_cleanup_days ?? 7, memory_reconcile_on_search: cfg.checkpoint?.memory_reconcile_on_search ?? true, memory_search_score_floor: cfg.checkpoint?.memory_search_score_floor ?? 0.15 }, workflow: Config.workflowPolicyFor(cfg), watcher: cfg.watcher ?? {}, snapshot: cfg.snapshot ?? true, commands: Object.fromEntries(Object.entries(cfg.command ?? {}).map(([name, command]) => [name, { description: command.description, agent: command.agent, model: command.model, subtask: command.subtask }])), formatter: typeof cfg.formatter === "boolean" ? cfg.formatter : Object.fromEntries(Object.entries(cfg.formatter ?? {}).map(([name, entry]) => [name, { disabled: entry.disabled, command: entry.command, extensions: entry.extensions }])), lsp: typeof cfg.lsp === "boolean" ? cfg.lsp : Object.fromEntries(Object.entries(cfg.lsp ?? {}).map(([name, entry]) => [name, { disabled: entry.disabled, ...("command" in entry ? { command: entry.command, extensions: entry.extensions } : {}) }])), instructions: { count: cfg.instructions?.length ?? 0, source: "merged_native_config" } }, policies: { dream: cfg.dream ?? {}, distill: cfg.distill ?? {}, experimental: { try_best_enabled: Flag.MIMOCODE_ENABLE_TRY_BEST_HANDOFF, continue_loop_on_deny: cfg.experimental?.continue_loop_on_deny ?? false, try_best: cfg.experimental?.try_best ?? {} } }, context: { hard: window.hard, effective: window.effective, usable: window.usable, source: window.source, input, output }, compaction: { auto: compaction.auto !== false, prune: compaction.prune !== false, tailTurns, preserveRecentTokens: recent, reserved }, checkpoint: { thresholds, reserved: cfg.checkpoint?.reserved ?? 13000, maxWriterFailures: cfg.checkpoint?.max_writer_failures ?? 3, fork: checkpoint.fork === true, pushCaps: checkpoint.push_caps && typeof checkpoint.push_caps === "object" ? checkpoint.push_caps : {} } }
    }
    if (method === "_odysseus/session/reserve") {
      // Reserve-without-create: mint a provisional engine `ses_…` and create
      // nothing. The host persists its projection-first candidate against this
      // ID, then create-and-publishes it through newSession/_meta or Session.create.
      if (Object.keys(params).length !== 0) throw RequestError.invalidParams("session reserve takes no parameters")
      const provisionalID = reserveProvisionalSessionID()
      this.reservedForkTargets ??= new Set<string>()
      this.reservedForkTargets.add(provisionalID)
      return { sessionID: provisionalID, provisional: true } as Record<string, unknown>
    }
    if (method !== "_odysseus/session/release" && method !== "_odysseus/session/discard") throw new Error(`Unsupported ACP extension method: ${method}`)
    const sessionId = params.sessionId
    if (typeof sessionId !== "string" || !sessionId || sessionId !== sessionId.trim()) throw RequestError.invalidParams("sessionId is required")
    if (method === "_odysseus/session/discard") {
      if (Object.keys(params).length !== 2 || typeof params.cwd !== "string" || !path.isAbsolute(params.cwd) || path.normalize(params.cwd) !== params.cwd) {
        throw RequestError.invalidParams("canonical cwd is required for session discard")
      }
      const ack = await this.sessionManager.discard(sessionId, params.cwd, [], "explicit")
      // Wire contract with the host ACPClient.discard_session predicate:
      // the RPC result is exactly {"deleted": true}. The rich ack stays
      // internal for logs only.
      log.info("session_discard_ack", {
        sessionID: ack.sessionID,
        cwd: ack.cwd,
        reason: ack.reason,
        deleted: ack.deleted,
      })
      return { deleted: true } as Record<string, unknown>
    }
    await this.sessionManager.release(sessionId)
    return {}
  }

  private startEventSubscription() {
    if (this.eventStarted) return
    this.eventStarted = true
    this.runEventSubscription().catch((error) => {
      this.failTerminalDeliveries(new Error("ACP event subscription failed before terminal delivery"))
      if (this.eventAbort.signal.aborted) return
      log.error("event subscription failed", { error })
    })
  }

  private readonly goalDeliveries = new Map<string, Promise<void>>()

  private deliverGoalCompletions(sessionID: string): Promise<void> {
    const prior = this.goalDeliveries.get(sessionID) ?? Promise.resolve()
    // Each actual event queues a fresh read, so a completion racing an older
    // verification-started read cannot be hidden by in-flight coalescing.
    const delivery = prior.catch(() => undefined).then(() => this.projectGoalCompletions(sessionID)).finally(() => {
      if (this.goalDeliveries.get(sessionID) === delivery) this.goalDeliveries.delete(sessionID)
    })
    this.goalDeliveries.set(sessionID, delivery)
    return delivery
  }

  private async projectGoalCompletions(sessionID: string) {
    const session = this.sessionManager.tryGet(sessionID)
    if (!session) return
    const journal = await this.sdk.session.goal.journal({ sessionID, directory: session.cwd }, { throwOnError: true })
    for (const entry of journal.data ?? []) {
      if (entry.type !== "completed" || entry.actor !== "verifier" || entry.reasonCode !== "met") continue
      const completed = entry.snapshot?.history?.find((goal) => goal.id === entry.goalID && goal.revision === entry.revision)
      if (!completed || completed.status !== "completed" || completed.lastOutcome?.code !== "met") continue
      const evidenceRefs = (entry.evidenceRefs ?? []).filter((id) => completed.evidence?.some((evidence) =>
        evidence.id === id && (evidence.kind === "file" || evidence.kind === "command") && !!evidence.verifier,
      )).slice(0, 128)
      if (!evidenceRefs.length) continue
      await OpenClankManagedProtocol.callHost(this.connection, "_openclank/session/v1/goal/completed", {
        sessionID, journalID: entry.id, goalID: entry.goalID, goalRevision: entry.revision,
        evidenceRefs, verifiedAt: entry.createdAt,
      })
    }
  }

  private async runEventSubscription() {
    while (true) {
      if (this.eventAbort.signal.aborted) {
        this.failTerminalDeliveries(new Error("ACP event subscription stopped before terminal delivery"))
        return
      }
      const events = await this.sdk.global.event({
        signal: this.eventAbort.signal,
      })
      for await (const event of events.stream) {
        if (this.eventAbort.signal.aborted) {
          this.failTerminalDeliveries(new Error("ACP event subscription stopped before terminal delivery"))
          return
        }
        const payload = event?.payload
        if (!payload) continue
        await this.handleEvent(payload as Event).catch((error) => {
          log.error("failed to handle event", { error, type: payload.type })
        })
      }
      this.failTerminalDeliveries(new Error("ACP event subscription ended before terminal delivery"))
    }
  }

  private failTerminalDeliveries(error: Error) {
    for (const delivery of this.terminalDeliveries.values()) delivery.fail(error)
    this.terminalDeliveries.clear()
  }

  private async withPromptDelivery<A extends { data?: { info?: unknown } }>(sessionID: string, run: () => Promise<A>): Promise<A> {
    if (this.terminalDeliveries.has(sessionID)) throw new Error("ACP prompt delivery is already active")
    let finish!: () => void
    let fail!: (error: Error) => void
    const terminal = new Promise<void>((resolve, reject) => { finish = resolve; fail = reject })
    // A stream error may arrive while the SDK request is still settling.
    terminal.catch(() => undefined)
    this.terminalDeliveries.set(sessionID, { busy: false, finish, fail })
    try {
      const response = await run()
      // Early SDK admission errors have no turn or terminal event. A returned
      // message (including cancellation/error) has an ordered busy→idle turn.
      if (response.data?.info) await terminal
      return response
    } finally {
      this.terminalDeliveries.delete(sessionID)
    }
  }

  private async handleEvent(event: Event) {
    if (event.type === "session.status") {
      const delivery = this.terminalDeliveries.get(event.properties.sessionID)
      if (delivery) {
        if (event.properties.status.type === "busy") delivery.busy = true
        if (event.properties.status.type === "idle" && delivery.busy) delivery.finish()
      }
      return
    }

    // SessionCwd.Event.Changed is an engine bus event carrying the one-chat
    // physical cwd. It is bridged as a private ACP update so the host can
    // atomically refresh its per-chat workspace projection. Cast here because
    // the upstream SDK's generated Event union does not know Open Clank's
    // private event yet.
    const eventType = (event as unknown as { type?: string }).type
    if (eventType === "session.goal") {
      const sessionID = String((event as unknown as { properties?: { sessionID?: string } }).properties?.sessionID ?? "")
      if (sessionID) await this.deliverGoalCompletions(sessionID)
      return
    }
    if (eventType === "session.cwd") {
      const props = (event as unknown as { properties?: Record<string, unknown> }).properties ?? {}
      const sessionID = String(props.sessionID ?? "")
      const cwd = String(props.cwd ?? "")
      if (!sessionID || !cwd || !this.sessionManager.tryGet(sessionID)) return
      this.sessionManager.setCwd(sessionID, cwd)
      await this.connection.sessionUpdate({
        sessionId: sessionID,
        update: {
          sessionUpdate: "_openclank_session_cwd",
          cwd,
        } as any,
      })
      return
    }
    switch (event.type) {
      case "actor.registered":
      case "actor.status": {
        await this.forwardActorSnapshot(event.properties.sessionID, event.properties.actorID)
        return
      }

      case "question.asked": {
        const question = event.properties
        const session = this.sessionManager.tryGet(question.sessionID)
        if (!session) return
        const previous = this.questionQueues.get(question.sessionID) ?? Promise.resolve()
        const next = previous
          .then(async () => {
            const response = await this.connection.extMethod("_odysseus/question", {
              requestId: question.id,
              sessionId: question.sessionID,
              questions: question.questions,
              tool: question.tool,
            })
            if (response.rejected === true) {
              await this.sdk.question.reject(
                { requestID: question.id, directory: session.cwd },
                { throwOnError: true },
              )
              return
            }
            const answers = Array.isArray(response.answers) ? response.answers : []
            await this.sdk.question.reply(
              { requestID: question.id, directory: session.cwd, answers: answers as string[][] },
              { throwOnError: true },
            )
          })
          .catch(async (error) => {
            log.error("failed to handle ACP question", { error, questionID: question.id })
            await this.sdk.question
              .reject(
                { requestID: question.id, directory: session.cwd },
                { throwOnError: true },
              )
              .catch(() => undefined)
          })
          .finally(() => {
            if (this.questionQueues.get(question.sessionID) === next) {
              this.questionQueues.delete(question.sessionID)
            }
          })
        this.questionQueues.set(question.sessionID, next)
        return
      }

      case "permission.asked": {
        const permission = event.properties
        const session = this.sessionManager.tryGet(permission.sessionID)
        if (!session) return

        const prev = this.permissionQueues.get(permission.sessionID) ?? Promise.resolve()
        const next = prev
          .then(async () => {
            const directory = session.cwd

            const res = await this.connection
              .requestPermission({
                sessionId: permission.sessionID,
                toolCall: {
                  toolCallId: permission.tool?.callID ?? permission.id,
                  status: "pending",
                  title: permission.permission,
                  rawInput: permission.metadata,
                  kind: toToolKind(permission.permission),
                  locations: toLocations(permission.permission, permission.metadata),
                },
                options: this.permissionOptions,
              })
              .catch(async (error) => {
                log.error("failed to request permission from ACP", {
                  error,
                  permissionID: permission.id,
                  sessionID: permission.sessionID,
                })
                await this.sdk.permission.reply({
                  requestID: permission.id,
                  reply: "reject",
                  directory,
                })
                return undefined
              })

            if (!res) return
            if (res.outcome.outcome !== "selected") {
              await this.sdk.permission.reply({
                requestID: permission.id,
                reply: "reject",
                directory,
              })
              return
            }

            if (res.outcome.optionId !== "reject" && permission.permission == "edit") {
              const metadata = permission.metadata || {}
              const filepath = typeof metadata["filepath"] === "string" ? metadata["filepath"] : ""
              const diff = typeof metadata["diff"] === "string" ? metadata["diff"] : ""
              const content = (await Filesystem.exists(filepath)) ? await Filesystem.readText(filepath) : ""
              const newContent = getNewContent(content, diff)

              if (newContent) {
                void this.connection.writeTextFile({
                  sessionId: session.id,
                  path: filepath,
                  content: newContent,
                })
              }
            }

            await this.sdk.permission.reply({
              requestID: permission.id,
              reply: res.outcome.optionId as "once" | "always" | "reject",
              directory,
            })
          })
          .catch((error) => {
            log.error("failed to handle permission", { error, permissionID: permission.id })
          })
          .finally(() => {
            if (this.permissionQueues.get(permission.sessionID) === next) {
              this.permissionQueues.delete(permission.sessionID)
            }
          })
        this.permissionQueues.set(permission.sessionID, next)
        return
      }

      case "session.error": {
        // Provider/LLM failures land on the assistant message and this bus
        // event — session.prompt still resolves 200. Without forwarding, the
        // host sees a clean empty turn and the user gets silence.
        const props = event.properties
        const sessionID = props.sessionID
        if (!sessionID || !this.sessionManager.tryGet(sessionID)) return
        const err = props.error
        let message = err ? String(err.name ?? "provider error") : "Unknown provider error"
        if (err && "data" in err && err.data && typeof err.data === "object" && "message" in err.data) {
          message = String((err.data as { message?: unknown }).message ?? message)
        }
        await this.connection.sessionUpdate({
          sessionId: sessionID,
          update: {
            sessionUpdate: "_odysseus_error",
            name: err ? String(err.name ?? "UnknownError") : "UnknownError",
            message,
          } as any,
        })
        return
      }

      case "session.retry.attempt": {
        const props = event.properties
        if (!this.sessionManager.tryGet(props.sessionID)) return
        await this.connection.sessionUpdate({
          sessionId: props.sessionID,
          update: {
            sessionUpdate: "_odysseus_retry",
            attempt: props.attempt,
            maxAttempts: props.maxAttempts,
            reason: props.reason,
            nextDelayMs: props.nextDelayMs,
          } as any,
        })
        return
      }

      case "message.part.updated": {
        log.info("message part updated", { event: event.properties })
        const props = event.properties
        const part = props.part
        const session = this.sessionManager.tryGet(part.sessionID)
        if (!session) return
        const sessionId = session.id

        if (part.type === "tool") {
          await this.toolStart(sessionId, part)

          switch (part.state.status) {
            case "pending":
              this.bashSnapshots.delete(part.callID)
              return

            case "running":
              const output = this.bashOutput(part)
              const content: ToolCallContent[] = []
              if (output) {
                const hash = Hash.fast(output)
                if (part.tool === "bash") {
                  if (this.bashSnapshots.get(part.callID) === hash) {
                    await this.connection
                      .sessionUpdate({
                        sessionId,
                        update: {
                          sessionUpdate: "tool_call_update",
                          toolCallId: part.callID,
                          status: "in_progress",
                          kind: toToolKind(part.tool),
                          title: part.tool,
                          locations: toLocations(part.tool, part.state.input),
                          rawInput: part.state.input,
                        },
                      })
                      .catch((error) => {
                        log.error("failed to send tool in_progress to ACP", { error })
                      })
                    return
                  }
                  this.bashSnapshots.set(part.callID, hash)
                }
                content.push({
                  type: "content",
                  content: {
                    type: "text",
                    text: output,
                  },
                })
              }
              await this.connection
                .sessionUpdate({
                  sessionId,
                  update: {
                    sessionUpdate: "tool_call_update",
                    toolCallId: part.callID,
                    status: "in_progress",
                    kind: toToolKind(part.tool),
                    title: part.tool,
                    locations: toLocations(part.tool, part.state.input),
                    rawInput: part.state.input,
                    ...(content.length > 0 && { content }),
                  },
                })
                .catch((error) => {
                  log.error("failed to send tool in_progress to ACP", { error })
                })
              return

            case "completed": {
              this.toolStarts.delete(part.callID)
              this.bashSnapshots.delete(part.callID)
              const kind = toToolKind(part.tool)
              const content: ToolCallContent[] = [
                {
                  type: "content",
                  content: {
                    type: "text",
                    text: part.state.output,
                  },
                },
              ]

              if (kind === "edit") {
                const diff = editDiffContent(part.state.input)
                content.push({
                  type: "diff",
                  path: diff.path,
                  oldText: diff.oldText,
                  newText: diff.newText,
                })
              }

              await this.connection
                .sessionUpdate({
                  sessionId,
                  update: {
                    sessionUpdate: "tool_call_update",
                    toolCallId: part.callID,
                    status: "completed",
                    kind,
                    content,
                    title: part.state.title,
                    rawInput: part.state.input,
                    rawOutput: {
                      output: part.state.output,
                      metadata: part.state.metadata,
                    },
                  },
                })
                .catch((error) => {
                  log.error("failed to send tool completed to ACP", { error })
                })
              return
            }
            case "error":
              this.toolStarts.delete(part.callID)
              this.bashSnapshots.delete(part.callID)
              await this.connection
                .sessionUpdate({
                  sessionId,
                  update: {
                    sessionUpdate: "tool_call_update",
                    toolCallId: part.callID,
                    status: "failed",
                    kind: toToolKind(part.tool),
                    title: part.tool,
                    rawInput: part.state.input,
                    content: [
                      {
                        type: "content",
                        content: {
                          type: "text",
                          text: part.state.error,
                        },
                      },
                    ],
                    rawOutput: {
                      error: part.state.error,
                      metadata: part.state.metadata,
                    },
                  },
                })
                .catch((error) => {
                  log.error("failed to send tool error to ACP", { error })
                })
              return
          }
        }

        // ACP clients already know the prompt they just submitted, so replaying
        // live user parts duplicates the message. We still replay user history in
        // loadSession() and forkSession() via processMessage().
        if (part.type !== "text" && part.type !== "file") return

        return
      }

      case "message.part.delta": {
        const props = event.properties
        const session = this.sessionManager.tryGet(props.sessionID)
        if (!session) return
        const sessionId = session.id

        const message = await this.sdk.session
          .message(
            {
              sessionID: props.sessionID,
              messageID: props.messageID,
              directory: session.cwd,
            },
            { throwOnError: true },
          )
          .then((x) => x.data)
          .catch((error) => {
            log.error("unexpected error when fetching message", { error })
            return undefined
          })

        if (!message || message.info.role !== "assistant") return

        const part = message.parts.find((p) => p.id === props.partID)
        if (!part) return

        if (part.type === "text" && props.field === "text" && part.ignored !== true) {
          await this.connection
            .sessionUpdate({
              sessionId,
              update: {
                sessionUpdate: "agent_message_chunk",
                messageId: props.messageID,
                content: {
                  type: "text",
                  text: props.delta,
                },
              },
            })
            .catch((error) => {
              log.error("failed to send text delta to ACP", { error })
            })
          return
        }

        if (part.type === "reasoning" && props.field === "text") {
          await this.connection
            .sessionUpdate({
              sessionId,
              update: {
                sessionUpdate: "agent_thought_chunk",
                messageId: props.messageID,
                content: {
                  type: "text",
                  text: props.delta,
                },
              },
            })
            .catch((error) => {
              log.error("failed to send reasoning delta to ACP", { error })
            })
        }
        return
      }
    }
  }

  private async listActorSnapshots(sessionID: string): Promise<any[]> {
    const session = this.sessionManager.tryGet(sessionID)
    if (!session) return []
    const response = await this.sdk.session
      .actors({ sessionID, directory: session.cwd }, { throwOnError: true })
      .catch((error) => {
        log.error("failed to fetch actor snapshots", { error, sessionID })
        return undefined
      })
    return Array.isArray(response?.data) ? (response.data as any[]) : []
  }

  private async startActorFeed(sessionID: string, rootTurnID: string) {
    const actors = await this.listActorSnapshots(sessionID)
    const feed = {
      rootTurnID,
      revision: 1,
      baseline: new Set(actors.map((actor) => String(actor?.actorID ?? "")).filter(Boolean)),
    }
    this.actorFeeds.set(sessionID, feed)
    this.actorRevisions.set(rootTurnID, 1)
    await this.connection.sessionUpdate({
      sessionId: sessionID,
      update: {
        sessionUpdate: "_odysseus_actor_snapshot",
        rootTurnId: rootTurnID,
        mimoSessionId: sessionID,
        sourceRevision: 1,
        acknowledged: true,
      } as any,
    })
  }

  private async forwardActorSnapshot(sessionID: string, actorID: string) {
    const feed = this.actorFeeds.get(sessionID)
    if (!feed) return
    const actors = await this.listActorSnapshots(sessionID)
    const actor = actors.find((item) => String(item?.actorID ?? "") === actorID)
    if (!actor) return

    let rootTurnID = this.actorRoots.get(`${sessionID}:${actorID}`)
    const parentID = String(actor.parentActorID ?? "")
    if (!rootTurnID && parentID) rootTurnID = this.actorRoots.get(`${sessionID}:${parentID}`)
    if (!rootTurnID && !feed.baseline.has(actorID)) rootTurnID = feed.rootTurnID
    if (!rootTurnID) return
    this.actorRoots.set(`${sessionID}:${actorID}`, rootTurnID)

    const revision = (this.actorRevisions.get(rootTurnID) ?? 1) + 1
    this.actorRevisions.set(rootTurnID, revision)
    await this.connection.sessionUpdate({
      sessionId: sessionID,
      update: {
        sessionUpdate: "_odysseus_actor_snapshot",
        rootTurnId: rootTurnID,
        mimoSessionId: sessionID,
        sourceRevision: revision,
        acknowledged: true,
        actor: {
          ...actor,
          systemSpawned: SYSTEM_SPAWNED_AGENT_TYPES.has(String(actor.agent ?? "")),
          countsTowardTotal:
            actor.mode === "subagent" && !SYSTEM_SPAWNED_AGENT_TYPES.has(String(actor.agent ?? "")),
        },
      } as any,
    })
  }

  async initialize(params: InitializeRequest): Promise<InitializeResponse> {
    log.info("initialize", { protocolVersion: params.protocolVersion })

    const managed = ManagedProvider.enabled()
    if (managed) {
      OpenClankManagedProtocol.validatePeerCapabilities(
        params.clientCapabilities?._meta?.openclankManaged,
      )
    }

    const authMethod: AuthMethod = {
      description: "Run `opencode auth login` in the terminal",
      name: "Login with opencode",
      id: "opencode-login",
    }

    // If client supports terminal-auth capability, use that instead.
    if (params.clientCapabilities?._meta?.["terminal-auth"] === true) {
      authMethod._meta = {
        "terminal-auth": {
          command: "opencode",
          args: ["auth", "login"],
          label: "OpenCode Login",
        },
      }
    }

    return {
      protocolVersion: 1,
      agentCapabilities: {
        loadSession: true,
        mcpCapabilities: {
          http: true,
          sse: true,
        },
        promptCapabilities: {
          embeddedContext: true,
          image: true,
        },
        sessionCapabilities: {
          fork: {},
          list: {},
          resume: {},
        },
      },
      authMethods: managed ? [] : [authMethod],
      agentInfo: {
        name: managed ? "Open Clank Engine" : "OpenCode",
        version: InstallationVersion,
      },
      ...(managed ? { _meta: OpenClankManagedProtocol.initializeMeta } : {}),
    } as InitializeResponse
  }

  async authenticate(_params: AuthenticateRequest) {
    throw new Error("Authentication not implemented")
  }

  async newSession(params: NewSessionRequest) {
    const directory = params.cwd
    try {
      const model = await defaultModel(this.config, directory)
      const meta = (params._meta ?? {}) as Record<string, unknown>
      const rawProvisional = meta.provisionalSessionID
      const provisionalID =
        typeof rawProvisional === "string" && rawProvisional.startsWith("ses_") && rawProvisional === rawProvisional.trim()
          ? (rawProvisional as SessionID)
          : undefined

      // Create-and-publish under one owner lifecycle lease. A provisional ID
      // reserved earlier is reused so the host's projection-first candidate
      // keeps one identity across the interrupt window.
      const state = await this.sessionManager.create(params.cwd, params.mcpServers, model, { provisionalID })
      const sessionId = state.id

      log.info("creating_session", { sessionId, mcpServers: params.mcpServers.length })

      const load = await this.loadSessionMode({
        cwd: directory,
        mcpServers: params.mcpServers,
        sessionId,
      })

      return {
        sessionId,
        configOptions: load.configOptions,
        models: load.models,
        modes: load.modes,
        _meta: load._meta,
      }
    } catch (e) {
      const error = MessageV2.fromError(e, {
        providerID: ProviderID.make(this.config.defaultModel?.providerID ?? "unknown"),
      })
      if (LoadAPIKeyError.isInstance(error)) {
        throw RequestError.authRequired()
      }
      throw e
    }
  }

  async loadSession(params: LoadSessionRequest) {
    const directory = params.cwd
    const sessionId = params.sessionId

    try {
      const model = await defaultModel(this.config, directory)

      // Store ACP session state
      await this.sessionManager.load(sessionId, params.cwd, params.mcpServers, model)

      log.info("load_session", { sessionId, mcpServers: params.mcpServers.length })

      const result = await this.loadSessionMode({
        cwd: directory,
        mcpServers: params.mcpServers,
        sessionId,
      })

      await this.deliverGoalCompletions(sessionId).catch(() => undefined)

      // Replay session history
      const messages = await this.sdk.session
        .messages(
          {
            sessionID: sessionId,
            directory,
            agent_id: "main",
          },
          { throwOnError: true },
        )
        .then((x) => x.data)
        .catch((err) => {
          log.error("unexpected error when fetching message", { error: err })
          return undefined
        })

      const lastUser = messages?.findLast((m) => m.info.role === "user")?.info
      if (lastUser?.role === "user") {
        result.models.currentModelId = `${lastUser.model.providerID}/${lastUser.model.modelID}`
        this.sessionManager.setModel(sessionId, {
          providerID: ProviderID.make(lastUser.model.providerID),
          modelID: ModelID.make(lastUser.model.modelID),
        })
        if (result.modes?.availableModes.some((m) => m.id === lastUser.agent)) {
          result.modes.currentModeId = lastUser.agent
          this.sessionManager.setMode(sessionId, lastUser.agent)
        }
        result.configOptions = buildConfigOptions({
          currentModelId: result.models.currentModelId,
          availableModels: result.models.availableModels,
          modes: result.modes,
        })
      }

      for (const msg of messages ?? []) {
        log.debug("replay message", msg)
        await this.processMessage(msg)
      }

      await sendUsageUpdate(this.connection, this.sdk, sessionId, directory)

      return result
    } catch (e) {
      const error = MessageV2.fromError(e, {
        providerID: ProviderID.make(this.config.defaultModel?.providerID ?? "unknown"),
      })
      if (LoadAPIKeyError.isInstance(error)) {
        throw RequestError.authRequired()
      }
      throw e
    }
  }

  async listSessions(params: ListSessionsRequest): Promise<ListSessionsResponse> {
    try {
      const cursor = params.cursor ? Number(params.cursor) : undefined
      const limit = 100

      const sessions = await this.sdk.session
        .list(
          {
            directory: params.cwd ?? undefined,
            roots: true,
          },
          { throwOnError: true },
        )
        .then((x) => x.data ?? [])

      const sorted = sessions.toSorted((a, b) => b.time.updated - a.time.updated)
      const filtered = cursor ? sorted.filter((s) => s.time.updated < cursor) : sorted
      const page = filtered.slice(0, limit)

      const entries: SessionInfo[] = page.map((session) => ({
        sessionId: session.id,
        cwd: session.directory,
        title: session.title,
        updatedAt: new Date(session.time.updated).toISOString(),
      }))

      const last = page[page.length - 1]
      const next = filtered.length > limit && last ? String(last.time.updated) : undefined

      const response: ListSessionsResponse = {
        sessions: entries,
      }
      if (next) response.nextCursor = next
      return response
    } catch (e) {
      const error = MessageV2.fromError(e, {
        providerID: ProviderID.make(this.config.defaultModel?.providerID ?? "unknown"),
      })
      if (LoadAPIKeyError.isInstance(error)) {
        throw RequestError.authRequired()
      }
      throw e
    }
  }

  async unstable_forkSession(params: ForkSessionRequest): Promise<ForkSessionResponse> {
    const directory = params.cwd
    const mcpServers = params.mcpServers ?? []

    try {
      const model = await defaultModel(this.config, directory)

      const forked = await this.sdk.session
        .fork(
          {
            sessionID: params.sessionId,
            directory,
          },
          { throwOnError: true },
        )
        .then((x) => x.data)

      if (!forked) {
        throw new Error("Fork session returned no data")
      }

      const sessionId = forked.id
      await this.sessionManager.load(sessionId, directory, mcpServers, model)

      log.info("fork_session", { sessionId, mcpServers: mcpServers.length })

      const mode = await this.loadSessionMode({
        cwd: directory,
        mcpServers,
        sessionId,
      })

      const messages = await this.sdk.session
        .messages(
          {
            sessionID: sessionId,
            directory,
            agent_id: "main",
          },
          { throwOnError: true },
        )
        .then((x) => x.data)
        .catch((err) => {
          log.error("unexpected error when fetching message", { error: err })
          return undefined
        })

      for (const msg of messages ?? []) {
        log.debug("replay message", msg)
        await this.processMessage(msg)
      }

      await sendUsageUpdate(this.connection, this.sdk, sessionId, directory)

      return mode
    } catch (e) {
      const error = MessageV2.fromError(e, {
        providerID: ProviderID.make(this.config.defaultModel?.providerID ?? "unknown"),
      })
      if (LoadAPIKeyError.isInstance(error)) {
        throw RequestError.authRequired()
      }
      throw e
    }
  }

  async unstable_resumeSession(params: ResumeSessionRequest): Promise<ResumeSessionResponse> {
    const directory = params.cwd
    const sessionId = params.sessionId
    const mcpServers = params.mcpServers ?? []

    try {
      const model = await defaultModel(this.config, directory)
      await this.sessionManager.load(sessionId, directory, mcpServers, model)

      log.info("resume_session", { sessionId, mcpServers: mcpServers.length })

      const result = await this.loadSessionMode({
        cwd: directory,
        mcpServers,
        sessionId,
      })

      await sendUsageUpdate(this.connection, this.sdk, sessionId, directory)

      return result
    } catch (e) {
      const error = MessageV2.fromError(e, {
        providerID: ProviderID.make(this.config.defaultModel?.providerID ?? "unknown"),
      })
      if (LoadAPIKeyError.isInstance(error)) {
        throw RequestError.authRequired()
      }
      throw e
    }
  }

  private async processMessage(message: SessionMessageResponse) {
    log.debug("process message", message)
    if (message.info.role !== "assistant" && message.info.role !== "user") return
    const sessionId = message.info.sessionID

    for (const part of message.parts) {
      if (part.type === "tool") {
        await this.toolStart(sessionId, part)
        switch (part.state.status) {
          case "pending":
            this.bashSnapshots.delete(part.callID)
            break
          case "running":
            const output = this.bashOutput(part)
            const runningContent: ToolCallContent[] = []
            if (output) {
              runningContent.push({
                type: "content",
                content: {
                  type: "text",
                  text: output,
                },
              })
            }
            await this.connection
              .sessionUpdate({
                sessionId,
                update: {
                  sessionUpdate: "tool_call_update",
                  toolCallId: part.callID,
                  status: "in_progress",
                  kind: toToolKind(part.tool),
                  title: part.tool,
                  locations: toLocations(part.tool, part.state.input),
                  rawInput: part.state.input,
                  ...(runningContent.length > 0 && { content: runningContent }),
                },
              })
              .catch((err) => {
                log.error("failed to send tool in_progress to ACP", { error: err })
              })
            break
          case "completed":
            this.toolStarts.delete(part.callID)
            this.bashSnapshots.delete(part.callID)
            const kind = toToolKind(part.tool)
            const content: ToolCallContent[] = [
              {
                type: "content",
                content: {
                  type: "text",
                  text: part.state.output,
                },
              },
            ]

            if (kind === "edit") {
              const diff = editDiffContent(part.state.input)
              content.push({
                type: "diff",
                path: diff.path,
                oldText: diff.oldText,
                newText: diff.newText,
              })
            }

            await this.connection
              .sessionUpdate({
                sessionId,
                update: {
                  sessionUpdate: "tool_call_update",
                  toolCallId: part.callID,
                  status: "completed",
                  kind,
                  content,
                  title: part.state.title,
                  rawInput: part.state.input,
                  rawOutput: {
                    output: part.state.output,
                    metadata: part.state.metadata,
                  },
                },
              })
              .catch((err) => {
                log.error("failed to send tool completed to ACP", { error: err })
              })
            break
          case "error":
            this.toolStarts.delete(part.callID)
            this.bashSnapshots.delete(part.callID)
            await this.connection
              .sessionUpdate({
                sessionId,
                update: {
                  sessionUpdate: "tool_call_update",
                  toolCallId: part.callID,
                  status: "failed",
                  kind: toToolKind(part.tool),
                  title: part.tool,
                  rawInput: part.state.input,
                  content: [
                    {
                      type: "content",
                      content: {
                        type: "text",
                        text: part.state.error,
                      },
                    },
                  ],
                  rawOutput: {
                    error: part.state.error,
                    metadata: part.state.metadata,
                  },
                },
              })
              .catch((err) => {
                log.error("failed to send tool error to ACP", { error: err })
              })
            break
        }
      } else if (part.type === "text") {
        if (part.text) {
          const audience: Role[] | undefined = part.synthetic ? ["assistant"] : part.ignored ? ["user"] : undefined
          await this.connection
            .sessionUpdate({
              sessionId,
              update: {
                sessionUpdate: message.info.role === "user" ? "user_message_chunk" : "agent_message_chunk",
                messageId: message.info.id,
                content: {
                  type: "text",
                  text: part.text,
                  ...(audience && { annotations: { audience } }),
                },
              },
            })
            .catch((err) => {
              log.error("failed to send text to ACP", { error: err })
            })
        }
      } else if (part.type === "file") {
        // Replay file attachments as appropriate ACP content blocks.
        // OpenCode stores files internally as { type: "file", url, filename, mime }.
        // We convert these back to ACP blocks based on the URL scheme and MIME type:
        // - file:// URLs → resource_link
        // - data: URLs with image/* → image block
        // - data: URLs with text/* or application/json → resource with text
        // - data: URLs with other types → resource with blob
        const url = part.url
        const filename = part.filename ?? "file"
        const mime = part.mime || "application/octet-stream"
        const messageChunk = message.info.role === "user" ? "user_message_chunk" : "agent_message_chunk"

        if (url.startsWith("file://")) {
          // Local file reference - send as resource_link
          await this.connection
            .sessionUpdate({
              sessionId,
              update: {
                sessionUpdate: messageChunk,
                messageId: message.info.id,
                content: { type: "resource_link", uri: url, name: filename, mimeType: mime },
              },
            })
            .catch((err) => {
              log.error("failed to send resource_link to ACP", { error: err })
            })
        } else if (url.startsWith("data:")) {
          // Embedded content - parse data URL and send as appropriate block type
          const base64Match = url.match(/^data:([^;]+);base64,(.*)$/)
          const dataMime = base64Match?.[1]
          const base64Data = base64Match?.[2] ?? ""

          const effectiveMime = dataMime || mime

          if (effectiveMime.startsWith("image/")) {
            // Image - send as image block
            await this.connection
              .sessionUpdate({
                sessionId,
                update: {
                  sessionUpdate: messageChunk,
                  messageId: message.info.id,
                  content: {
                    type: "image",
                    mimeType: effectiveMime,
                    data: base64Data,
                    uri: pathToFileURL(filename).href,
                  },
                },
              })
              .catch((err) => {
                log.error("failed to send image to ACP", { error: err })
              })
          } else {
            // Non-image: text types get decoded, binary types stay as blob
            const isText = effectiveMime.startsWith("text/") || effectiveMime === "application/json"
            const fileUri = pathToFileURL(filename).href
            const resource = isText
              ? {
                  uri: fileUri,
                  mimeType: effectiveMime,
                  text: Buffer.from(base64Data, "base64").toString("utf-8"),
                }
              : { uri: fileUri, mimeType: effectiveMime, blob: base64Data }

            await this.connection
              .sessionUpdate({
                sessionId,
                update: {
                  sessionUpdate: messageChunk,
                  messageId: message.info.id,
                  content: { type: "resource", resource },
                },
              })
              .catch((err) => {
                log.error("failed to send resource to ACP", { error: err })
              })
          }
        }
        // URLs that don't match file:// or data: are skipped (unsupported)
      } else if (part.type === "reasoning") {
        if (part.text) {
          await this.connection
            .sessionUpdate({
              sessionId,
              update: {
                sessionUpdate: "agent_thought_chunk",
                messageId: message.info.id,
                content: {
                  type: "text",
                  text: part.text,
                },
              },
            })
            .catch((err) => {
              log.error("failed to send reasoning to ACP", { error: err })
            })
        }
      }
    }
  }

  private bashOutput(part: ToolPart) {
    if (part.tool !== "bash") return
    if (!("metadata" in part.state) || !part.state.metadata || typeof part.state.metadata !== "object") return
    const output = part.state.metadata["output"]
    if (typeof output !== "string") return
    return output
  }

  private async toolStart(sessionId: string, part: ToolPart) {
    if (this.toolStarts.has(part.callID)) return
    this.toolStarts.add(part.callID)
    await this.connection
      .sessionUpdate({
        sessionId,
        update: {
          sessionUpdate: "tool_call",
          toolCallId: part.callID,
          title: part.tool,
          kind: toToolKind(part.tool),
          status: "pending",
          locations: [],
          rawInput: {},
        },
      })
      .catch((error) => {
        log.error("failed to send tool pending to ACP", { error })
      })
  }

  private async loadAvailableCommands(directory: string, sessionId: string) {
    const commands = await this.config.sdk.command
      .list(
        {
          directory,
        },
        { throwOnError: true },
      )
      .then((resp) => resp.data!)

    const availableCommands = commands.map((command) => ({
      name: command.name,
      description: command.description ?? "",
      input: command.hints.length ? { hint: command.hints.join(" ") } : undefined,
      _meta: { source: command.source ?? "native", bundled: command.bundled, subtask: command.subtask, agent: command.agent, model: command.model, hints: command.hints },
    }))
    const names = new Set(availableCommands.map((c) => c.name))
    this.commandNames.set(sessionId, names)
    if (!names.has("compact"))
      availableCommands.push({
        name: "compact",
        description: "summarize the session to reduce context",
        input: undefined,
        _meta: { source: "native", bundled: undefined, subtask: undefined, agent: undefined, model: undefined, hints: [] },
      })

    return availableCommands
  }

  private async runtimeStateFor(directory: string) {
    return Instance.provide({ directory, fn: () => AppRuntime.runPromise(Effect.gen(function* () {
      const questions = yield* Question.Service
      const agents = yield* AgentModule.Service
      return { neverAsk: yield* questions.neverAsk(), defaultAgent: yield* agents.defaultAgent() }
    })) })
  }

  private async agentForPrompt(directory: string, currentMode: string | undefined, context?: { mode?: string; lane?: string; agent_profile?: string }) {
    if (context?.lane === "auxiliary") return "chat"
    if (context?.mode === "plan") return "plan"
    if (ManagedProvider.enabled() && context?.mode === "agent") {
      const selected = context.agent_profile ?? (await this.runtimeStateFor(directory)).defaultAgent
      const modes = await this.loadAvailableModes(directory)
      if (typeof selected !== "string" || ["chat", "plan"].includes(selected) || !modes.some((mode) => mode.id === selected)) throw new RequestError(-32602, "Selected Agent profile is unavailable; choose an enabled primary profile", { code: "AGENT_PROFILE_UNAVAILABLE" })
      return selected
    }
    return currentMode ?? (await this.runtimeStateFor(directory)).defaultAgent
  }

  private async loadAvailableModes(directory: string): Promise<ModeOption[]> {
    const agents = await this.config.sdk.app
      .agents(
        {
          directory,
        },
        { throwOnError: true },
      )
      .then((resp) => resp.data!)

    return agents
      .filter((agent) => agent.mode !== "subagent" && !agent.hidden && !SYSTEM_SPAWNED_AGENT_TYPES.has(agent.name))
      .map((agent) => ({
        id: agent.name,
        name: agent.name,
        description: agent.description,
      }))
  }

  private async resolveModeState(
    directory: string,
    sessionId: string,
  ): Promise<{ availableModes: ModeOption[]; currentModeId?: string }> {
    const availableModes = await this.loadAvailableModes(directory)
    const currentModeId =
      this.sessionManager.get(sessionId).modeId ||
      (await (async () => {
        if (!availableModes.length) return undefined
        const defaultAgentName = (await this.runtimeStateFor(directory)).defaultAgent
        const resolvedModeId = availableModes.find((mode) => mode.name === defaultAgentName)?.id ?? availableModes[0].id
        this.sessionManager.setMode(sessionId, resolvedModeId)
        return resolvedModeId
      })())

    return { availableModes, currentModeId }
  }

  private async loadSessionMode(params: LoadSessionRequest) {
    const directory = params.cwd
    const model = await defaultModel(this.config, directory)
    const sessionId = params.sessionId

    const providers = await this.sdk.config.providers({ directory }).then((x) => x.data!.providers)
    const entries = sortProvidersByName(providers)
    const availableVariants = modelVariantsFromProviders(entries, model)
    const currentVariant = this.sessionManager.getVariant(sessionId)
    if (currentVariant && !availableVariants.includes(currentVariant)) {
      this.sessionManager.setVariant(sessionId, undefined)
    }
    const availableModels = buildAvailableModels(entries, { includeVariants: true })
    const modeState = await this.resolveModeState(directory, sessionId)
    const currentModeId = modeState.currentModeId
    const modes = currentModeId
      ? {
          availableModes: modeState.availableModes,
          currentModeId,
        }
      : undefined

    const mcpServers: Record<string, ConfigMCP.Info> = {}
    for (const server of params.mcpServers) {
      if ("type" in server) {
        mcpServers[server.name] = {
          url: server.url,
          headers: server.headers.reduce<Record<string, string>>((acc, { name, value }) => {
            acc[name] = value
            return acc
          }, {}),
          type: "remote",
        }
      } else {
        mcpServers[server.name] = {
          type: "local",
          command: [server.command, ...server.args],
          environment: server.env.reduce<Record<string, string>>((acc, { name, value }) => {
            acc[name] = value
            return acc
          }, {}),
        }
      }
    }

    await Promise.all(
      Object.entries(mcpServers).map(async ([key, mcp]) => {
        await this.sdk.mcp
          .add(
            {
              directory,
              name: key,
              config: mcp,
            },
            { throwOnError: true },
          )
          .catch((error) => {
            log.error("failed to add mcp server", { name: key, error })
          })
      }),
    )

    const availableCommands = await this.loadAvailableCommands(directory, sessionId)

    setTimeout(() => {
      void this.connection.sessionUpdate({
        sessionId,
        update: {
          sessionUpdate: "available_commands_update",
          availableCommands,
        },
      })
    }, 0)

    return {
      sessionId,
      models: {
        currentModelId: formatModelIdWithVariant(model, currentVariant, availableVariants, true),
        availableModels,
      },
      modes,
      configOptions: buildConfigOptions({
        currentModelId: formatModelIdWithVariant(model, currentVariant, availableVariants, true),
        availableModels,
        modes,
      }),
      _meta: buildVariantMeta({
        model,
        variant: this.sessionManager.getVariant(sessionId),
        availableVariants,
      }),
    }
  }

  async unstable_setSessionModel(params: SetSessionModelRequest) {
    const session = this.sessionManager.get(params.sessionId)
    const providers = await this.sdk.config
      .providers({ directory: session.cwd }, { throwOnError: true })
      .then((x) => x.data!.providers)

    const selection = parseModelSelection(params.modelId, providers)
    this.sessionManager.setModel(session.id, selection.model)
    this.sessionManager.setVariant(session.id, selection.variant)

    const entries = sortProvidersByName(providers)
    const availableVariants = modelVariantsFromProviders(entries, selection.model)

    return {
      _meta: buildVariantMeta({
        model: selection.model,
        variant: selection.variant,
        availableVariants,
      }),
    }
  }

  async setSessionMode(params: SetSessionModeRequest): Promise<SetSessionModeResponse | void> {
    const session = this.sessionManager.get(params.sessionId)
    const availableModes = await this.loadAvailableModes(session.cwd)
    if (!availableModes.some((mode) => mode.id === params.modeId)) {
      throw new Error(`Agent not found: ${params.modeId}`)
    }
    this.sessionManager.setMode(params.sessionId, params.modeId)
  }

  async setSessionConfigOption(params: SetSessionConfigOptionRequest): Promise<SetSessionConfigOptionResponse> {
    const session = this.sessionManager.get(params.sessionId)
    const providers = await this.sdk.config
      .providers({ directory: session.cwd }, { throwOnError: true })
      .then((x) => x.data!.providers)
    const entries = sortProvidersByName(providers)

    if (params.configId === "model") {
      if (typeof params.value !== "string") throw RequestError.invalidParams("model value must be a string")
      const selection = parseModelSelection(params.value, providers)
      this.sessionManager.setModel(session.id, selection.model)
      this.sessionManager.setVariant(session.id, selection.variant)
    } else if (params.configId === "mode") {
      if (typeof params.value !== "string") throw RequestError.invalidParams("mode value must be a string")
      const availableModes = await this.loadAvailableModes(session.cwd)
      if (!availableModes.some((mode) => mode.id === params.value)) {
        throw RequestError.invalidParams(JSON.stringify({ error: `Mode not found: ${params.value}` }))
      }
      this.sessionManager.setMode(session.id, params.value)
    } else {
      throw RequestError.invalidParams(JSON.stringify({ error: `Unknown config option: ${params.configId}` }))
    }

    const updatedSession = this.sessionManager.get(session.id)
    const model = updatedSession.model ?? (await defaultModel(this.config, session.cwd))
    const availableVariants = modelVariantsFromProviders(entries, model)
    const currentModelId = formatModelIdWithVariant(model, updatedSession.variant, availableVariants, true)
    const availableModels = buildAvailableModels(entries, { includeVariants: true })
    const modeState = await this.resolveModeState(session.cwd, session.id)
    const modes = modeState.currentModeId
      ? { availableModes: modeState.availableModes, currentModeId: modeState.currentModeId }
      : undefined

    return {
      configOptions: buildConfigOptions({ currentModelId, availableModels, modes }),
    }
  }

  async prompt(params: PromptRequest) {
    // Host context seam (identity ruling R1): the host's envelope is the
    // authority for tool policy AND for who the agent IS. system_prompt is
    // the resolved persona — it rides the true system tier of the prompt,
    // never demoted to synthetic user text.
    const odysseus = (params as PromptRequest & {
      _meta?: {
        odysseus?: {
          tools?: Record<string, boolean>
          system_prompt?: string
          persona?: string
          mode?: string
          agent_profile?: string
          lane?: string
          root_turn_id?: string
          chat_id?: string
          workspace_id?: string
          goal_id?: string
          requested_model?: string
        }
        openclankProvider?: unknown
      }
    })._meta?.odysseus
    const managedProvider = (params as PromptRequest & {
      _meta?: { openclankProvider?: unknown }
    })._meta?.openclankProvider
    const hostSystem = odysseus?.system_prompt?.trim() || undefined
    const sessionID = params.sessionId
    if (this.forkSources.has(sessionID)) throw new RequestError(-32602, "A fork is capturing this source", { code: "FORK_SOURCE_BUSY" })
    const session = this.sessionManager.get(sessionID)
    await this.deliverGoalCompletions(sessionID).catch(() => undefined)
    const directory = session.cwd

    const current = session.model
    const model = current ?? (await defaultModel(this.config, directory))
    if (!current) {
      this.sessionManager.setModel(session.id, model)
    }
    // Named auxiliary work is tool-free Chat plumbing; Plan is an explicit
    // read-only Agent authority. Ordinary product turns stay on Agent.
    const agent = await this.agentForPrompt(directory, session.modeId, odysseus)

    const parts: Array<
      | { type: "text"; text: string; synthetic?: boolean; ignored?: boolean; metadata?: Record<string, unknown> }
      | { type: "file"; url: string; filename: string; mime: string }
    > = []
    for (const part of params.prompt) {
      switch (part.type) {
        case "text":
          const audience = part.annotations?.audience
          const forAssistant = audience?.length === 1 && audience[0] === "assistant"
          const forUser = audience?.length === 1 && audience[0] === "user"
          parts.push({
            type: "text" as const,
            text: part.text,
            ...(forAssistant && { synthetic: true }),
            ...(forUser && { ignored: true }),
          })
          break
        case "image": {
          const parsed = parseUri(part.uri ?? "")
          const filename = parsed.type === "file" ? parsed.filename : "image"
          if (part.data) {
            parts.push({
              type: "file",
              url: `data:${part.mimeType};base64,${part.data}`,
              filename,
              mime: part.mimeType,
            })
          } else if (part.uri && (part.uri.startsWith("http:") || part.uri.startsWith("https:"))) {
            parts.push({
              type: "file",
              url: part.uri,
              filename,
              mime: part.mimeType,
            })
          }
          break
        }

        case "resource_link":
          const parsed = parseUri(part.uri)
          // Use the name from resource_link if available
          if (part.name && parsed.type === "file") {
            parsed.filename = part.name
          }
          parts.push(parsed)

          break

        case "resource": {
          const resource = part.resource
          if ("text" in resource && resource.text) {
            parts.push({
              type: "text",
              text: resource.text,
            })
          } else if ("blob" in resource && resource.blob && resource.mimeType) {
            // Binary resource (PDFs, etc.): store as file part with data URL
            const parsed = parseUri(resource.uri ?? "")
            const filename = parsed.type === "file" ? parsed.filename : "file"
            parts.push({
              type: "file",
              url: `data:${resource.mimeType};base64,${resource.blob}`,
              filename,
              mime: resource.mimeType,
            })
          }
          break
        }

        default:
          break
      }
    }

    // Media parts may contain large base64 payloads. Keep diagnostics bounded and
    // content-free so ACP logs never become an attachment exfiltration path.
    const partSummary = parts.reduce(
      (summary, part) => {
        summary[part.type] = (summary[part.type] ?? 0) + 1
        return summary
      },
      {} as Record<string, number>,
    )
    log.info("parts", { count: parts.length, types: partSummary })

    if (odysseus?.root_turn_id) {
      await this.startActorFeed(sessionID, odysseus.root_turn_id)
    }

    const literalSlash = (params._meta as Record<string, unknown> | undefined)?.openclankLiteralSlash === true
    const cmd = (() => {
      if (literalSlash) return
      const text = parts
        .findLast(
          (p): p is { type: "text"; text: string; synthetic?: boolean; ignored?: boolean } =>
            p.type === "text" && !p.synthetic && !p.ignored,
        )
        ?.text.replace(/^\[turn_source_id=[^\]]+\]\s*/, "")

      if (!text?.startsWith("/")) return

      return parseCommandText(text)
    })()

    let mainSdkUsage: ReturnType<typeof captureSdkEvidence>
    const buildUsage = (msg: AssistantMessage): Usage => ({
      totalTokens:
        msg.tokens.input +
        msg.tokens.output +
        msg.tokens.reasoning +
        (msg.tokens.cache?.read ?? 0) +
        (msg.tokens.cache?.write ?? 0),
      inputTokens: mainSdkUsage?.usage.inputTokens ?? msg.tokens.input,
      outputTokens: msg.tokens.output,
      thoughtTokens: msg.tokens.reasoning,
      cachedReadTokens: msg.tokens.cache?.read,
      cachedWriteTokens: mainSdkUsage?.usage.cacheWriteTokens,
    })

    const managedRoute =
      managedProvider && typeof managedProvider === "object"
        ? (managedProvider as Record<string, unknown>)
        : undefined
    if (ManagedProvider.enabled() && typeof managedRoute?.rootOperationID === "string" && managedRoute.rootOperationID.length > 0 && managedRoute.rootOperationID.length <= 256) {
      // The managed lease authenticates this operation before any SDK write.
      // Ignored metadata survives native fork without entering model context.
      parts.push({ type: "text", text: "", synthetic: true, ignored: true, metadata: { hostSourceMessageID: managedRoute.rootOperationID } })
    }
    const hostContextForOperation = () => {
      const scope = ManagedProvider.currentScope(sessionID)
      return {
        ...(scope?.accountID
          ? { accountID: scope.accountID }
          : typeof managedRoute?.preferredAccountID === "string" && managedRoute.preferredAccountID
            ? { accountID: managedRoute.preferredAccountID }
            : typeof managedRoute?.inheritedAccountID === "string" && managedRoute.inheritedAccountID
              ? { accountID: managedRoute.inheritedAccountID }
              : {}),
        ...(scope?.credentialRevision !== undefined ? { credentialRevision: scope.credentialRevision } : {}),
        ...(typeof managedRoute?.grantID === "string" && managedRoute.grantID ? { grantID: managedRoute.grantID } : {}),
        ...(typeof managedRoute?.grantRevision === "number" && Number.isInteger(managedRoute.grantRevision) && managedRoute.grantRevision >= 0
          ? { grantRevision: managedRoute.grantRevision }
          : {}),
        ...(typeof odysseus?.chat_id === "string" && odysseus.chat_id
          ? { chatID: odysseus.chat_id }
          : {}),
        ...(typeof odysseus?.workspace_id === "string" && odysseus.workspace_id
          ? { workspaceID: odysseus.workspace_id }
          : {}),
        ...(typeof odysseus?.goal_id === "string" && odysseus.goal_id
          ? { goalID: odysseus.goal_id }
          : {}),
        cwd: directory,
      }
    }

    let usageMeta: Record<string, unknown> = {
      normalizationProfile: "acp-separated-v1",
      identityCoverage: "partial",
      coveredDispatchIDs: [],
      metricCoverage: {},
    }
    const runManaged = <A>(run: () => Promise<A>) =>
      ManagedProvider.withOperation(
        sessionID,
        managedProvider,
        { providerID: model.providerID, modelID: model.modelID },
        () =>
          ManagedProvider.withHostContext(sessionID, hostContextForOperation(), async () => {
            try {
              return await run()
            } finally {
              await flushCaptures()
              const scope = ManagedProvider.currentScope(sessionID)
              const last = captureSdkEvidence(scope)
              mainSdkUsage = last
              const coverage = Object.fromEntries(
                ["input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens"].map(
                  (key) => [
                    key,
                    last?.metricCoverage[key] ?? {
                      state: "unavailable",
                      coverage: "unknown",
                      source: "acp",
                      reason: "sdk_category_absent",
                    },
                  ],
                ),
              )
              for (const [base, subsets] of [
                ["output_tokens", ["reasoning_tokens"]],
              ] as const) {
                if (subsets.some((key) => !last?.metricCoverage[key]))
                  coverage[base] = {
                    state: "unavailable",
                    coverage: "unknown",
                    source: "acp",
                    reason: "missing_normalization_subset",
                  }
              }
              usageMeta = {
                ...captureScopeEvidence(scope),
                normalizationProfile: "acp-inclusive-input-separated-output-v1",
                metricCoverage: coverage,
                coveredDispatchIDs: last?.coveredDispatchIDs ?? [],
              }
            }
          }),
      )

    // The server resolves session.prompt 200 even when the turn died on a
    // provider error (it lands on info.error), and the SDK reports transport
    // rejections (e.g. session busy) via response.error without throwing.
    // Both were silently collapsing into an empty end_turn.
    const turnFailure = (response: {
      data?: { info?: AssistantMessage }
      error?: unknown
    }): { cancelled: boolean; message: string } | undefined => {
      const err = response.data?.info?.error
      if (err) {
        if (err.name === "MessageAbortedError") return { cancelled: true, message: "" }
        const data = (err as { data?: { message?: unknown } }).data
        const message = typeof data?.message === "string" && data.message ? data.message : String(err.name)
        return { cancelled: false, message }
      }
      if (response.data === undefined && response.error !== undefined) {
        const raw = response.error
        const message =
          typeof raw === "object" && raw !== null && "message" in raw
            ? String((raw as { message?: unknown }).message)
            : typeof raw === "string"
              ? raw
              : JSON.stringify(raw)
        return { cancelled: false, message: message || "prompt request failed" }
      }
      return undefined
    }

    if (!cmd) {
      const response = await runManaged(() => this.withPromptDelivery(sessionID, () =>
        this.sdk.session.prompt({
          sessionID,
          model: {
            providerID: model.providerID,
            modelID: model.modelID,
          },
          variant: this.sessionManager.getVariant(sessionID),
          parts,
          agent,
          directory,
          tools: odysseus?.tools,
          system: hostSystem,
        } as any),
      ))
      const msg = response.data?.info

      await sendUsageUpdate(this.connection, this.sdk, sessionID, directory)

      const failure = turnFailure(response)
      if (failure?.cancelled) {
        return {
          stopReason: "cancelled" as const,
          usage: msg ? buildUsage(msg) : undefined,
          _meta: usageMeta,
        }
      }
      if (failure) {
        throw new RequestError(-32603, failure.message, { sessionId: sessionID })
      }

      return {
        stopReason: "end_turn" as const,
        usage: msg ? buildUsage(msg) : undefined,
        _meta: usageMeta,
      }
    }

    const command = await this.config.sdk.command
      .list({ directory }, { throwOnError: true })
      .then((x) => x.data!.find((c) => c.name === cmd.name))
    if (command) {
      // session.command has no system-override input; command templates keep
      // their own framing. Persona authority applies to ordinary prompts.
      const response = await runManaged(() =>
        this.sdk.session.command({
          sessionID,
          command: command.name,
          arguments: cmd.args,
          parts: parts.filter((part) => part.type === "file" || (part.type === "text" && part.ignored === true && typeof part.metadata?.hostSourceMessageID === "string")),
          variant: this.sessionManager.getVariant(sessionID),
          model: model.providerID + "/" + model.modelID,
          agent,
          directory,
        }),
      )
      const msg = response.data?.info

      await sendUsageUpdate(this.connection, this.sdk, sessionID, directory)

      const failure = turnFailure(response)
      if (failure?.cancelled) {
        return {
          stopReason: "cancelled" as const,
          usage: msg ? buildUsage(msg) : undefined,
          _meta: usageMeta,
        }
      }
      if (failure) {
        throw new RequestError(-32603, failure.message, { sessionId: sessionID })
      }

      return {
        stopReason: "end_turn" as const,
        usage: msg ? buildUsage(msg) : undefined,
        _meta: usageMeta,
      }
    }

    switch (cmd.name) {
      case "compact":
        await runManaged(() =>
          this.config.sdk.session.summarize(
            {
              sessionID,
              directory,
              providerID: model.providerID,
              modelID: model.modelID,
            },
            { throwOnError: true },
          ),
        )
        break
      default: {
        const unavailable = this.commandNames.get(sessionID)?.has(cmd.name) || ["deep-research", "loops"].includes(cmd.name)
        throw new RequestError(-32602, `${unavailable ? "Unavailable" : "Unknown"} command /${cmd.name}; refresh /commands or use /literal to send literal text`, { code: unavailable ? "COMMAND_UNAVAILABLE" : "UNKNOWN_COMMAND", command: cmd.name })
      }
    }

    await sendUsageUpdate(this.connection, this.sdk, sessionID, directory)

    return {
      stopReason: "end_turn" as const,
      _meta: {},
    }
  }

  async cancel(params: CancelNotification) {
    const session = this.sessionManager.get(params.sessionId)
    await this.config.sdk.session.abort(
      {
        sessionID: params.sessionId,
        directory: session.cwd,
      },
      { throwOnError: true },
    )
  }
}

function toToolKind(toolName: string): ToolKind {
  const tool = toolName.toLocaleLowerCase()
  switch (tool) {
    case "bash":
      return "execute"
    case "webfetch":
      return "fetch"

    case "edit":
    case "patch":
    case "write":
      return "edit"

    case "grep":
    case "glob":
    case "context7_resolve_library_id":
    case "context7_get_library_docs":
      return "search"

    case "read":
      return "read"

    default:
      return "other"
  }
}

function stringField(input: Record<string, any>, ...keys: string[]): string {
  for (const key of keys) {
    const value = input[key]
    if (typeof value === "string") return value
  }
  return ""
}

function editDiffContent(input: Record<string, any>): { path: string; oldText: string; newText: string } {
  return {
    path: stringField(input, "file_path", "filePath"),
    oldText: stringField(input, "old_string", "oldString"),
    newText: stringField(input, "new_string", "newString", "content"),
  }
}

function toLocations(toolName: string, input: Record<string, any>): { path: string }[] {
  const tool = toolName.toLocaleLowerCase()
  switch (tool) {
    case "read":
    case "write":
    case "edit":
    case "multiedit":
    case "lsp":
      const filePath = stringField(input, "file_path", "filePath")
      return filePath ? [{ path: filePath }] : []
    case "glob":
    case "grep":
      const target = stringField(input, "path")
      return target ? [{ path: target }] : []
    case "bash":
      return []
    default:
      return []
  }
}

async function defaultModel(config: ACPConfig, cwd?: string): Promise<{ providerID: ProviderID; modelID: ModelID }> {
  const sdk = config.sdk
  const configured = config.defaultModel
  if (configured) return configured

  const directory = cwd ?? process.cwd()

  const specified = await sdk.config
    .get({ directory }, { throwOnError: true })
    .then((resp) => {
      const cfg = resp.data
      if (!cfg || !cfg.model) return undefined
      return Provider.parseModel(cfg.model)
    })
    .catch((error) => {
      log.error("failed to load user config for default model", { error })
      return undefined
    })

  const providers = await sdk.config
    .providers({ directory }, { throwOnError: true })
    .then((x) => x.data?.providers ?? [])
    .catch((error) => {
      log.error("failed to list providers for default model", { error })
      return []
    })

  if (specified && providers.length) {
    const provider = providers.find((p) => p.id === specified.providerID)
    if (provider && provider.models[specified.modelID]) return specified
  }

  if (specified && !providers.length) return specified

  const opencodeProvider = providers.find((p) => p.id === "opencode")
  if (opencodeProvider) {
    if (opencodeProvider.models["big-pickle"]) {
      return { providerID: ProviderID.opencode, modelID: ModelID.make("big-pickle") }
    }
    const [best] = Provider.sort(Object.values(opencodeProvider.models))
    if (best) {
      return {
        providerID: ProviderID.make(best.providerID),
        modelID: ModelID.make(best.id),
      }
    }
  }

  const models = providers.flatMap((p) => Object.values(p.models))
  const [best] = Provider.sort(models)
  if (best) {
    return {
      providerID: ProviderID.make(best.providerID),
      modelID: ModelID.make(best.id),
    }
  }

  if (specified) return specified

  return { providerID: ProviderID.opencode, modelID: ModelID.make("big-pickle") }
}

function parseUri(
  uri: string,
): { type: "file"; url: string; filename: string; mime: string } | { type: "text"; text: string } {
  try {
    if (uri.startsWith("file://")) {
      const path = uri.slice(7)
      const name = path.split("/").pop() || path
      return {
        type: "file",
        url: uri,
        filename: name,
        mime: "text/plain",
      }
    }
    if (uri.startsWith("zed://")) {
      const url = new URL(uri)
      const path = url.searchParams.get("path")
      if (path) {
        const name = path.split("/").pop() || path
        return {
          type: "file",
          url: pathToFileURL(path).href,
          filename: name,
          mime: "text/plain",
        }
      }
    }
    return {
      type: "text",
      text: uri,
    }
  } catch {
    return {
      type: "text",
      text: uri,
    }
  }
}

function getNewContent(fileOriginal: string, unifiedDiff: string): string | undefined {
  const result = applyPatch(fileOriginal, unifiedDiff)
  if (result === false) {
    log.error("Failed to apply unified diff (context mismatch)")
    return undefined
  }
  return result
}

function sortProvidersByName<T extends { name: string }>(providers: T[]): T[] {
  return [...providers].sort((a, b) => {
    const nameA = a.name.toLowerCase()
    const nameB = b.name.toLowerCase()
    if (nameA < nameB) return -1
    if (nameA > nameB) return 1
    return 0
  })
}

function modelVariantsFromProviders(
  providers: Array<{ id: string; models: Record<string, { variants?: Record<string, any> }> }>,
  model: { providerID: ProviderID; modelID: ModelID },
): string[] {
  const provider = providers.find((entry) => entry.id === model.providerID)
  if (!provider) return []
  const modelInfo = provider.models[model.modelID]
  if (!modelInfo?.variants) return []
  return Object.keys(modelInfo.variants)
}

export function buildAvailableModels(
  providers: Array<{
    id: string
    name: string
    models: Record<
      string,
      { id: string; name: string; baseModelId?: string; preset?: string; variants?: Record<string, unknown> }
    >
  }>,
  options: { includeVariants?: boolean } = {},
): ModelOption[] {
  const includeVariants = options.includeVariants ?? false
  return providers.flatMap((provider) => {
    const unsorted = Object.values(provider.models)
    const models = Provider.sort(unsorted)
    return models.flatMap((model) => {
      const base: ModelOption = {
        modelId: `${provider.id}/${model.id}`,
        name: `${provider.name}/${model.name}`,
        baseModelId: model.baseModelId ?? `${provider.id}/${model.id}`,
        ...(model.preset ? { preset: model.preset } : {}),
      }
      if (!includeVariants || !model.variants) return [base]
      const variants = Object.keys(model.variants).filter((variant) => variant !== DEFAULT_VARIANT_VALUE)
      const variantOptions = variants.map((variant) => ({
        modelId: `${provider.id}/${model.id}/${variant}`,
        name: `${provider.name}/${model.name} (${variant})`,
        baseModelId: base.baseModelId,
        ...(base.preset ? { preset: base.preset } : {}),
        variant,
      }))
      return [base, ...variantOptions]
    })
  })
}

function formatModelIdWithVariant(
  model: { providerID: ProviderID; modelID: ModelID },
  variant: string | undefined,
  availableVariants: string[],
  includeVariant: boolean,
) {
  const base = `${model.providerID}/${model.modelID}`
  if (!includeVariant || !variant || !availableVariants.includes(variant)) return base
  return `${base}/${variant}`
}

function buildVariantMeta(input: {
  model: { providerID: ProviderID; modelID: ModelID }
  variant?: string
  availableVariants: string[]
}) {
  return {
    opencode: {
      modelId: `${input.model.providerID}/${input.model.modelID}`,
      variant: input.variant ?? null,
      availableVariants: input.availableVariants,
    },
  }
}

function parseModelSelection(
  modelId: string,
  providers: Array<{ id: string; models: Record<string, { variants?: Record<string, any> }> }>,
): { model: { providerID: ProviderID; modelID: ModelID }; variant?: string } {
  const parsed = Provider.parseModel(modelId)
  const provider = providers.find((p) => p.id === parsed.providerID)
  if (!provider) {
    return { model: parsed, variant: undefined }
  }

  // Check if modelID exists directly
  if (provider.models[parsed.modelID]) {
    return { model: parsed, variant: undefined }
  }

  // Try to extract variant from end of modelID (e.g., "claude-sonnet-4/high" -> model: "claude-sonnet-4", variant: "high")
  const segments = parsed.modelID.split("/")
  if (segments.length > 1) {
    const candidateVariant = segments[segments.length - 1]
    const baseModelId = segments.slice(0, -1).join("/")
    const baseModelInfo = provider.models[baseModelId]
    if (baseModelInfo?.variants && candidateVariant in baseModelInfo.variants) {
      return {
        model: { providerID: parsed.providerID, modelID: ModelID.make(baseModelId) },
        variant: candidateVariant,
      }
    }
  }

  return { model: parsed, variant: undefined }
}

function buildConfigOptions(input: {
  currentModelId: string
  availableModels: ModelOption[]
  modes?: { availableModes: ModeOption[]; currentModeId: string } | undefined
}): SessionConfigOption[] {
  const options: SessionConfigOption[] = [
    {
      id: "model",
      name: "Model",
      category: "model",
      type: "select",
      currentValue: input.currentModelId,
      options: input.availableModels.map((m) => ({ value: m.modelId, name: m.name })),
    },
  ]
  if (input.modes) {
    options.push({
      id: "mode",
      name: "Session Mode",
      category: "mode",
      type: "select",
      currentValue: input.modes.currentModeId,
      options: input.modes.availableModes.map((m) => ({
        value: m.id,
        name: m.name,
        ...(m.description ? { description: m.description } : {}),
      })),
    })
  }
  return options
}

export * as ACP from "./agent"
