import { dynamicTool, type Tool, jsonSchema, type JSONSchema7 } from "ai"
import { createHash } from "node:crypto"
import { Client } from "@modelcontextprotocol/sdk/client/index.js"
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js"
import { SSEClientTransport } from "@modelcontextprotocol/sdk/client/sse.js"
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js"
import { ObservingStdioTransport } from "./stdio-transport"
import { UnauthorizedError } from "@modelcontextprotocol/sdk/client/auth.js"
import {
  CallToolResultSchema,
  type Tool as MCPToolDef,
  ToolListChangedNotificationSchema,
} from "@modelcontextprotocol/sdk/types.js"
import { Config } from "../config"
import { ConfigMCP } from "../config/mcp"
import { Log } from "../util"
import { NamedError } from "@mimo-ai/shared/util/error"
import z from "zod/v4"
import { Installation } from "../installation"
import { InstallationVersion } from "../installation/version"
import { withTimeout } from "@/util/timeout"
import { childProcessEnv } from "@/util/child-process-env"
import { AppFileSystem } from "@mimo-ai/shared/filesystem"
import { McpOAuthProvider } from "./oauth-provider"
import { McpOAuthCallback } from "./oauth-callback"
import { McpAuth } from "./auth"
import { BusEvent } from "../bus/bus-event"
import { Bus } from "@/bus"
import { TuiEvent } from "@/cli/cmd/tui/event"
import open from "open"
import { Effect, Exit, Layer, Option, Context, Stream, ScopedCache } from "effect"
import { EffectBridge } from "@/effect"
import { InstanceState } from "@/effect"
import { ChildProcess, ChildProcessSpawner } from "effect/unstable/process"
import * as CrossSpawnSpawner from "@/effect/cross-spawn-spawner"
import { McpSampling } from "./sampling"
import { SessionID } from "@/session/schema"
import { codeReadResource } from "./code-activity"
import { sourceOutlineAdmission, type SourceOutlineWitness } from "@/session/source-outline-admission"
import { boundedOutlineSidecar } from "@/session/source-outline-custody"

const log = Log.create({ service: "mcp" })
const DEFAULT_TIMEOUT = 30_000
const RECALL_ADMISSION = "com.xiaomi.mimo/recall-admission"
type CodeActivityAttestation = { toolCallID: string; text: string; event: unknown }
const codeActivityAttestations = new WeakMap<object, CodeActivityAttestation>()
type OutlineAttestation = {toolCallID:string;text:string;sidecar:string;current:()=>boolean}
const outlineAttestations = new WeakMap<object, OutlineAttestation>()
export function takeSourceOutlineAttestation(value:unknown):OutlineAttestation | undefined {
  if (!value || typeof value!=='object') return
  const item=outlineAttestations.get(value);outlineAttestations.delete(value);return item
}
export function takeCodeActivityAttestation(value: unknown): CodeActivityAttestation | undefined {
  if (!value || typeof value !== "object") return
  const item = codeActivityAttestations.get(value)
  codeActivityAttestations.delete(value)
  return item
}


export const Resource = z
  .object({
    name: z.string(),
    uri: z.string(),
    description: z.string().optional(),
    mimeType: z.string().optional(),
    client: z.string(),
  })
  .meta({ ref: "McpResource" })
export type Resource = z.infer<typeof Resource>

export const ToolsChanged = BusEvent.define(
  "mcp.tools.changed",
  z.object({
    server: z.string(),
  }),
)

export const BrowserOpenFailed = BusEvent.define(
  "mcp.browser.open.failed",
  z.object({
    mcpName: z.string(),
    url: z.string(),
  }),
)

export const Failed = NamedError.create(
  "MCPFailed",
  z.object({
    name: z.string(),
  }),
)

type MCPClient = Client

export const TURN_LIFECYCLE_CAPABILITY = "com.xiaomi.mimo/turn-lifecycle"
export const TURN_LIFECYCLE_NOTIFICATION = `notifications/${TURN_LIFECYCLE_CAPABILITY}`
export const TURN_LIFECYCLE_VERSION = 1
export const TURN_LIFECYCLE_NOTIFICATION_TIMEOUT = 1_000
// A send that has already outlived the per-turn budget can never be useful to wait
// on again, so later turns abandon it instead of queueing behind it forever.
export const TURN_LIFECYCLE_STUCK_TIMEOUT = TURN_LIFECYCLE_NOTIFICATION_TIMEOUT

export const CLIENT_OPTIONS = {
  capabilities: {
    // Sampling is registered below. Keep tools/context undeclared because they
    // are intentionally unsupported on this MCP surface.
    sampling: {},
    experimental: { [TURN_LIFECYCLE_CAPABILITY]: { version: TURN_LIFECYCLE_VERSION } },
  },
}

export interface TurnContext {
  [key: string]: unknown
  sessionId: string
  turnId: string
  actorId?: string
}

export type TurnStatus = "completed" | "cancelled" | "error"

function supportsTurnLifecycle(client: MCPClient) {
  const capability = client.getServerCapabilities()?.experimental?.[TURN_LIFECYCLE_CAPABILITY]
  return typeof capability === "object" && capability !== null && "version" in capability && capability.version === TURN_LIFECYCLE_VERSION
}

interface PendingTurnLifecycleNotification {
  readonly promise: Promise<void>
  readonly waiters: Set<() => void>
  readonly startedAt: number
}

const pendingTurnLifecycleNotifications = new WeakMap<MCPClient, PendingTurnLifecycleNotification>()

function startTurnLifecycleNotification(client: MCPClient, context: TurnContext, status: TurnStatus) {
  if (pendingTurnLifecycleNotifications.has(client)) return undefined
  const promise = Promise.resolve().then(() =>
    client.notification({
      method: TURN_LIFECYCLE_NOTIFICATION,
      params: { ...context, status },
    } as Parameters<MCPClient["notification"]>[0]),
  )
  const notification: PendingTurnLifecycleNotification = { promise, waiters: new Set(), startedAt: Date.now() }
  pendingTurnLifecycleNotifications.set(client, notification)
  const clear = () => {
    if (pendingTurnLifecycleNotifications.get(client) === notification) pendingTurnLifecycleNotifications.delete(client)
    const waiters = [...notification.waiters]
    notification.waiters.clear()
    for (const waiter of waiters) waiter()
  }
  void promise.then(clear, clear)
  return notification
}

function releaseStuckTurnLifecycleNotification(client: MCPClient, notification: PendingTurnLifecycleNotification, clientName: string) {
  if (pendingTurnLifecycleNotifications.get(client) !== notification) return
  pendingTurnLifecycleNotifications.delete(client)
  log.warn("abandoning stuck MCP turn lifecycle notification", { clientName, elapsed: Date.now() - notification.startedAt })
  const waiters = [...notification.waiters]
  notification.waiters.clear()
  for (const waiter of waiters) waiter()
}

function waitForTurnLifecycleNotification(client: MCPClient, notification: PendingTurnLifecycleNotification) {
  return Effect.tryPromise({
    try: (signal) =>
      new Promise<void>((resolve, reject) => {
        let done = false
        const cleanup = () => {
          notification.waiters.delete(onSettled)
          signal.removeEventListener("abort", onAbort)
        }
        const finish = (complete: () => void) => {
          if (done) return
          done = true
          cleanup()
          complete()
        }
        const onSettled = () => finish(resolve)
        const onAbort = () => finish(() => reject(signal.reason instanceof Error ? signal.reason : new Error("Lifecycle wait aborted")))
        notification.waiters.add(onSettled)
        signal.addEventListener("abort", onAbort, { once: true })
        if (signal.aborted) onAbort()
        else if (pendingTurnLifecycleNotifications.get(client) !== notification) onSettled()
      }),
    catch: (error) => (error instanceof Error ? error : new Error(String(error))),
  })
}

function sendTurnLifecycleNotification(client: MCPClient, context: TurnContext, status: TurnStatus, clientName: string) {
  return Effect.gen(function* () {
    while (true) {
      const pending = pendingTurnLifecycleNotifications.get(client)
      if (pending) {
        if (Date.now() - pending.startedAt >= TURN_LIFECYCLE_STUCK_TIMEOUT) {
          releaseStuckTurnLifecycleNotification(client, pending, clientName)
          continue
        }
        yield* waitForTurnLifecycleNotification(client, pending)
        continue
      }
      const notification = startTurnLifecycleNotification(client, context, status)
      if (!notification) continue
      return yield* Effect.tryPromise({
        try: () => notification.promise,
        catch: (error) => (error instanceof Error ? error : new Error(String(error))),
      })
    }
  })
}

export function notifyTurnLifecycle(clients: Record<string, MCPClient>, context: TurnContext, status: TurnStatus) {
  return Effect.forEach(
    Object.entries(clients),
    ([clientName, client]) => {
      if (!supportsTurnLifecycle(client)) return Effect.void
      const capability = client.getServerCapabilities()?.experimental?.[TURN_LIFECYCLE_CAPABILITY]
      if (typeof capability === "object" && capability !== null && "notifications" in capability && capability.notifications === false) return Effect.void
      return sendTurnLifecycleNotification(client, context, status, clientName).pipe(
        Effect.timeout(TURN_LIFECYCLE_NOTIFICATION_TIMEOUT),
        Effect.tapError((error) =>
          Effect.sync(() => log.warn("failed to notify MCP turn lifecycle", { clientName, status, error })),
        ),
        Effect.ignore,
      )
    },
    { concurrency: "unbounded", discard: true },
  )
}

export const Status = z
  .discriminatedUnion("status", [
    z
      .object({
        status: z.literal("connected"),
      })
      .meta({
        ref: "MCPStatusConnected",
      }),
    z
      .object({
        status: z.literal("disabled"),
      })
      .meta({
        ref: "MCPStatusDisabled",
      }),
    z
      .object({
        status: z.literal("pending"),
      })
      .meta({
        ref: "MCPStatusPending",
      }),
    z
      .object({
        status: z.literal("failed"),
        error: z.string(),
      })
      .meta({
        ref: "MCPStatusFailed",
      }),
    z
      .object({
        status: z.literal("needs_auth"),
      })
      .meta({
        ref: "MCPStatusNeedsAuth",
      }),
    z
      .object({
        status: z.literal("needs_client_registration"),
        error: z.string(),
      })
      .meta({
        ref: "MCPStatusNeedsClientRegistration",
      }),
  ])
  .meta({
    ref: "MCPStatus",
  })
export type Status = z.infer<typeof Status>

// Store transports for OAuth servers to allow finishing auth
type TransportWithAuth = StreamableHTTPClientTransport | SSEClientTransport
const pendingOAuthTransports = new Map<string, TransportWithAuth>()

// Prompt cache types
type PromptInfo = Awaited<ReturnType<MCPClient["listPrompts"]>>["prompts"][number]
type ResourceInfo = Awaited<ReturnType<MCPClient["listResources"]>>["resources"][number]
type McpEntry = NonNullable<Config.Info["mcp"]>[string]
export interface HealthIdentity { sessionID: string; rootOperationID: string; actorID: string; executionRevision: number; innerTurnID: string }
type HealthObservation = { instanceID: string; generation: number; revision: number; discoveryFailed: boolean; observedAt: number; discoveryMs: number | null; lastCallAt: number | null; lastCallMs: number | null; lastCallCategory: "tool_error" | "transport" | "cancelled" | null; lastCallTurn: string | null }
export type ExecCapabilities = ReturnType<typeof import("@/tool/tool-script").projectExecCapabilities>
export type ToolsCapabilitiesSnapshot = { status: "observed"; exec: ExecCapabilities } | { status: "unavailable"; exec: null }
type HealthCandidate = { generation: number; revision: number; count: number }
export type HealthSnapshot = { status: "observed" | "unavailable"; servers: Array<{serverKey: string; status: "connected" | "degraded" | "disabled" | "failed" | "needs_auth" | "needs_client_registration" | "disconnected" | "unknown"; connectionGeneration: number | null; observedAt: number | null; discoveryMs: number | null; lastCallAt: number | null; lastCallMs: number | null; lastCallCategory: "tool_error" | "transport" | "cancelled" | null; discoveredCount: number | null; offeredToPromptCount: number | null; modelOfferedCount: null; callAdmissibleCount: null}>; omittedServers: number }
const unavailableHealth = (): HealthSnapshot => ({ status: "unavailable", servers: [], omittedServers: 0 })
function healthTurnKey(value: Partial<HealthIdentity> | undefined): string | undefined {
  if (!value || ![value.sessionID, value.rootOperationID, value.actorID, value.innerTurnID].every((item) => typeof item === "string" && item.length > 0 && item.length <= 256) || !Number.isSafeInteger(value.executionRevision) || value.executionRevision! < 0) return
  return JSON.stringify([value.sessionID, value.rootOperationID, value.actorID, value.executionRevision, value.innerTurnID])
}

function isMcpConfigured(entry: McpEntry): entry is ConfigMCP.Info {
  return typeof entry === "object" && entry !== null && "type" in entry
}

const sanitize = (s: string) => s.replace(/[^a-zA-Z0-9_-]/g, "_")

// Convert MCP tool definition to AI SDK Tool type
export function convertMcpTool(mcpTool: MCPToolDef, client: MCPClient, timeout?: number, context?: TurnContext, admit?: (toolCallID?: string, operation?: "status"|"discover"|"download"|"cancel") => Promise<string | undefined>, managedTheme = false, observe?: (duration: number, category: "tool_error" | "transport" | "cancelled" | null) => void, managedKernel = false, managedCode = false, managedBackgroundName?: string, managedTaskName?: string, managedMediaName?: string, managedFrameName?: string, managedSharedTextName?: string): Tool {
  const inputSchema = managedTheme && context?.themeStatusOnly === true
    ? { ...mcpTool.inputSchema, properties: { operation: { type:"string", enum:["status"] } }, required:["operation"] }
    : mcpTool.inputSchema

  // Spread first, then override type to ensure it's always "object"
  const schema: JSONSchema7 = {
    ...(inputSchema as JSONSchema7),
    type: "object",
    required: managedTaskName ? [] : inputSchema.required,
    properties: (managedTaskName
      ? { ...(inputSchema.properties ?? {}), action: { type:"string", enum:["list","inspect","create","edit","delete","pause","resume","run"] },
          limit:{type:"integer",minimum:1,maximum:32}, cursor:{type:"string",maxLength:8192}, expected_revision:{type:"string",maxLength:64},
          scheduled_date: { type: "string", description: "One-shot UTC instant: YYYY-MM-DDTHH:MM:SSZ" } }
      : (inputSchema.properties ?? {})) as JSONSchema7["properties"],
    additionalProperties: false,
  }

  return dynamicTool({
    description: managedTaskName
      ? `${mcpTool.description ?? ""}\nManaged list/inspect covers only bound tasks in the currently authorized workspace and is a live partial view. Inspect requires task_id and returns expected_revision for pause/resume scheduling, one manual run, a name/prompt edit, or deletion of a settled bound LLM task and its terminal run/wait history; running work is unchanged. Managed task creation accepts action=create, task_type=llm, name, prompt and schedule=daily|weekly|monthly|once. Daily/weekly/monthly require scheduled_time=HH:MM UTC; weekly requires scheduled_day 0–6 (Monday–Sunday), monthly 1–31. Once requires a future scheduled_date=YYYY-MM-DDTHH:MM:SSZ and no scheduled_time. Other create forms are unavailable.`
      : mcpTool.description ?? "",
    inputSchema: jsonSchema(schema),
    execute: async (args: unknown, options) => {
      const rawOperation = managedTheme && args && typeof args === "object" && !Array.isArray(args) ? (args as Record<string, unknown>).operation : undefined
      const operation = rawOperation === "status" || rawOperation === "discover" || rawOperation === "download" || rawOperation === "cancel" ? rawOperation : undefined
      if (managedTheme && (!context || !args || typeof args !== "object" || Array.isArray(args) || Object.keys(args).some((key) => key !== "operation") || !operation)) throw new Error("Invalid emoji operation")
      if (admit) await admit(options.toolCallId, operation)
      if (managedTaskName && args && typeof args === "object" && !Array.isArray(args) &&
          (args as Record<string, unknown>).action === undefined &&
          ["task","name","prompt","description","schedule","scheduled_time","scheduled_date","scheduled_day"].some((key) => key in (args as Record<string, unknown>)))
        throw new Error("Use action=create explicitly for task creation")
      if (managedTaskName && args && typeof args === "object" && !Array.isArray(args) &&
          ((args as Record<string, unknown>).action === undefined || ["list","inspect"].includes(String((args as Record<string, unknown>).action)))) {
        const { taskRead } = await import("@/mcp/task-create")
        const normalized = taskRead(args)
        if (!normalized || !context || !supportsTurnLifecycle(client) || typeof context.backgroundMessageID !== "string" ||
            typeof options.toolCallId !== "string" || !options.toolCallId || options.abortSignal?.aborted) throw new Error("Task read unavailable")
        const { managedHostCall } = await import("@/acp/managed-provider")
        const result = await managedHostCall("_openclank/session/v1/task/read", {
          sessionID:context.sessionID as string, rootOperationID:context.rootOperationID as string,
          actorID:context.actorID as string, executionRevision:context.executionRevision as number,
          innerTurnID:context.innerTurnID as string, messageID:context.backgroundMessageID,
          toolCallID:options.toolCallId, toolName:managedTaskName, ...normalized })
        return {content:[{type:"text" as const,text:JSON.stringify(result.ok ? result.result : {error:"Task read unavailable",exit_code:1})}]}
      }
      if (managedTaskName && args && typeof args === "object" && !Array.isArray(args) &&
          ["pause", "resume", "delete"].includes(String((args as Record<string, unknown>).action))) {
        const { taskControl } = await import("@/mcp/task-create")
        const normalized = taskControl(args)
        if (!normalized || !context || !supportsTurnLifecycle(client) || typeof context.backgroundMessageID !== "string" ||
            typeof options.toolCallId !== "string" || !options.toolCallId || options.abortSignal?.aborted) throw new Error("Inspect the task first and provide its expected_revision")
        const { managedHostCall, ManagedProvider } = await import("@/acp/managed-provider")
        const identity = { sessionID: context.sessionID as string, rootOperationID: context.rootOperationID as string,
          actorID: context.actorID as string, executionRevision: context.executionRevision as number,
          innerTurnID: context.innerTurnID as string, messageID: context.backgroundMessageID,
          toolCallID: options.toolCallId, toolName: managedTaskName }
        if (!await ManagedProvider.claimFreshTaskCall(identity)) throw new Error("Task scheduling control unavailable for this invocation")
        let result
        try { result = await managedHostCall("_openclank/session/v1/task/control", { ...identity, ...normalized, mode: "apply" }) } catch {}
        if (!result?.ok) result = await managedHostCall("_openclank/session/v1/task/control", { ...identity, ...normalized, mode: "status" })
        return { content: [{ type: "text" as const, text: JSON.stringify(result.ok ? result.result : {error:"Task control outcome unknown or unavailable",exit_code:1}) }] }
      }
      if (managedTaskName && args && typeof args === "object" && !Array.isArray(args) &&
          (args as Record<string, unknown>).action === "edit") {
        const { taskEdit } = await import("@/mcp/task-create")
        const normalized = taskEdit(args)
        if (!normalized || !context || !supportsTurnLifecycle(client) || typeof context.backgroundMessageID !== "string" ||
            typeof options.toolCallId !== "string" || !options.toolCallId || options.abortSignal?.aborted) throw new Error("Inspect an LLM task and provide expected_revision plus a nonempty name and/or prompt")
        const { managedHostCall, ManagedProvider } = await import("@/acp/managed-provider")
        const identity = { sessionID: context.sessionID as string, rootOperationID: context.rootOperationID as string,
          actorID: context.actorID as string, executionRevision: context.executionRevision as number,
          innerTurnID: context.innerTurnID as string, messageID: context.backgroundMessageID,
          toolCallID: options.toolCallId, toolName: managedTaskName }
        if (!await ManagedProvider.claimFreshTaskCall(identity)) throw new Error("Task edit unavailable for this invocation")
        let result
        try { result = await managedHostCall("_openclank/session/v1/task/edit", { ...identity, ...normalized, mode: "apply" }) } catch {}
        if (!result?.ok) result = await managedHostCall("_openclank/session/v1/task/edit", { ...identity, ...normalized, mode: "status" })
        return { content: [{ type: "text" as const, text: JSON.stringify(result.ok ? result.result : {error:"Task edit outcome unknown or unavailable",exit_code:1}) }] }
      }
      if (managedTaskName && args && typeof args === "object" && !Array.isArray(args) &&
          (args as Record<string, unknown>).action === "run") {
        const { taskRun } = await import("@/mcp/task-create")
        const normalized = taskRun(args)
        if (!normalized || !context || !supportsTurnLifecycle(client) || typeof context.backgroundMessageID !== "string" ||
            typeof options.toolCallId !== "string" || !options.toolCallId || options.abortSignal?.aborted) throw new Error("Inspect the task first and provide its expected_revision")
        const { managedHostCall, ManagedProvider } = await import("@/acp/managed-provider")
        const identity = { sessionID: context.sessionID as string, rootOperationID: context.rootOperationID as string,
          actorID: context.actorID as string, executionRevision: context.executionRevision as number,
          innerTurnID: context.innerTurnID as string, messageID: context.backgroundMessageID,
          toolCallID: options.toolCallId, toolName: managedTaskName }
        if (!await ManagedProvider.claimFreshTaskCall(identity)) throw new Error("Manual task run unavailable for this invocation")
        let result
        try { result = await managedHostCall("_openclank/session/v1/task/run", { ...identity, ...normalized, mode: "apply" }) } catch {}
        if (!result?.ok) result = await managedHostCall("_openclank/session/v1/task/run", { ...identity, ...normalized, mode: "status" })
        return { content: [{ type: "text" as const, text: JSON.stringify(result.ok ? result.result : {error:"Task run outcome unknown or unavailable",exit_code:1}) }] }
      }
      if (managedTaskName && args && typeof args === "object" && !Array.isArray(args) &&
          (args as Record<string, unknown>).action === "create") {
        const { taskCreate } = await import("@/mcp/task-create")
        const normalized = taskCreate(args)
        if (!normalized || !context || !supportsTurnLifecycle(client) || typeof context.backgroundMessageID !== "string" ||
            typeof options.toolCallId !== "string" || !options.toolCallId || options.abortSignal?.aborted) throw new Error("Managed task creation requires an explicit LLM schedule: daily/weekly/monthly use HH:MM UTC, with scheduled_day for weekly/monthly; once uses future YYYY-MM-DDTHH:MM:SSZ")
        const { managedHostCall, ManagedProvider } = await import("@/acp/managed-provider")
        const identity = { sessionID: context.sessionID as string, rootOperationID: context.rootOperationID as string,
          actorID: context.actorID as string, executionRevision: context.executionRevision as number,
          innerTurnID: context.innerTurnID as string, messageID: context.backgroundMessageID,
          toolCallID: options.toolCallId, toolName: managedTaskName }
        if (!await ManagedProvider.claimFreshTaskCall(identity)) throw new Error("Managed task creation is unavailable for this invocation")
        let result
        try {
          result = await managedHostCall("_openclank/session/v1/task/create", { ...identity, ...normalized })
        } catch { /* An unknown create can only be reconciled by a read. */ }
        if (!result?.ok) result = await managedHostCall("_openclank/session/v1/task/status", { ...identity, ...normalized })
        return { content: [{ type: "text" as const, text: JSON.stringify(result.ok ? result.result : {error:"Task creation unavailable",exit_code:1}) }] }
      }
      if (managedBackgroundName && args && typeof args === "object" && !Array.isArray(args) &&
          typeof (args as Record<string, unknown>).action === "string" && ((args as Record<string, unknown>).action as string).trim().toLowerCase() === "start") {
        const { backgroundStart } = await import("@/mcp/background-start")
        const normalized = backgroundStart(args)
        if (!normalized || !context || !supportsTurnLifecycle(client) || typeof context.backgroundMessageID !== "string" ||
            typeof options.toolCallId !== "string" || !options.toolCallId || options.abortSignal?.aborted) throw new Error("Background start unavailable")
        const { managedHostCall } = await import("@/acp/managed-provider")
        const result = await managedHostCall("_openclank/session/v1/background-job/start", {
          sessionID: context.sessionID as string, rootOperationID: context.rootOperationID as string,
          actorID: context.actorID as string, executionRevision: context.executionRevision as number,
          innerTurnID: context.innerTurnID as string, messageID: context.backgroundMessageID,
          toolCallID: options.toolCallId, toolName: managedBackgroundName, ...normalized,
        })
        return { content: [{ type: "text" as const, text: JSON.stringify(result.ok ? result.result : {error:"Background start unavailable",exit_code:1}) }] }
      }
      if (managedBackgroundName) {
        const { backgroundRead, backgroundTerminate, backgroundRetire, backgroundWrite } = await import("@/mcp/background-start")
        const retire=backgroundRetire(args)
        if (retire) {
          if (!context || !supportsTurnLifecycle(client) || typeof context.backgroundMessageID!=="string" ||
              typeof options.toolCallId!=="string" || !options.toolCallId || options.abortSignal?.aborted) throw new Error("Background retirement unavailable")
          const { managedHostCall } = await import("@/acp/managed-provider")
          const result=await managedHostCall("_openclank/session/v1/background-job/retire",{
            sessionID:context.sessionID as string,rootOperationID:context.rootOperationID as string,actorID:context.actorID as string,
            executionRevision:context.executionRevision as number,innerTurnID:context.innerTurnID as string,
            messageID:context.backgroundMessageID,toolCallID:options.toolCallId,toolName:managedBackgroundName,...retire,
          })
          return {content:[{type:"text" as const,text:JSON.stringify(result.ok ? result.result : {error:"Background retirement unavailable",exit_code:1})}]}
        }
        const input=backgroundWrite(args)
        if (input) {
          if (!context || !supportsTurnLifecycle(client) || typeof context.backgroundMessageID!=="string" ||
              typeof options.toolCallId!=="string" || !options.toolCallId || options.abortSignal?.aborted) throw new Error("Background input unavailable")
          const { managedHostCall } = await import("@/acp/managed-provider")
          const result=await managedHostCall("_openclank/session/v1/background-job/write",{
            sessionID:context.sessionID as string,rootOperationID:context.rootOperationID as string,actorID:context.actorID as string,
            executionRevision:context.executionRevision as number,innerTurnID:context.innerTurnID as string,
            messageID:context.backgroundMessageID,toolCallID:options.toolCallId,toolName:managedBackgroundName,...input,
          })
          return {content:[{type:"text" as const,text:JSON.stringify(result.ok ? result.result : {error:"Background input unavailable",exit_code:1})}]}
        }
        const terminate=backgroundTerminate(args)
        if (terminate) {
          if (!context || !supportsTurnLifecycle(client) || typeof context.backgroundMessageID!=="string" ||
              typeof options.toolCallId!=="string" || !options.toolCallId || options.abortSignal?.aborted) throw new Error("Background termination unavailable")
          const { managedHostCall } = await import("@/acp/managed-provider")
          const result=await managedHostCall("_openclank/session/v1/background-job/terminate",{
            sessionID:context.sessionID as string,rootOperationID:context.rootOperationID as string,actorID:context.actorID as string,
            executionRevision:context.executionRevision as number,innerTurnID:context.innerTurnID as string,
            messageID:context.backgroundMessageID,toolCallID:options.toolCallId,toolName:managedBackgroundName,...terminate,
          })
          return {content:[{type:"text" as const,text:JSON.stringify(result.ok ? result.result : {error:"Background termination unavailable",exit_code:1})}]}
        }
        const normalized=backgroundRead(args)
        if (!normalized || !context || !supportsTurnLifecycle(client) || typeof context.backgroundMessageID!=="string" ||
            typeof options.toolCallId!=="string" || !options.toolCallId || options.abortSignal?.aborted) throw new Error("Background read unavailable")
        const { managedHostCall } = await import("@/acp/managed-provider")
        const result=await managedHostCall("_openclank/session/v1/background-job/read",{
          sessionID:context.sessionID as string,rootOperationID:context.rootOperationID as string,actorID:context.actorID as string,
          executionRevision:context.executionRevision as number,innerTurnID:context.innerTurnID as string,
          messageID:context.backgroundMessageID,toolCallID:options.toolCallId,toolName:managedBackgroundName,...normalized,
        })
        return {content:[{type:"text" as const,text:JSON.stringify(result.ok ? result.result : {error:"Background read unavailable",exit_code:1})}]}
      }
      if (managedKernel && context && supportsTurnLifecycle(client)) {
        if (options.abortSignal?.aborted) throw new Error("Tool call aborted")
        const { managedHostCall } = await import("@/acp/managed-provider")
        const identity = { sessionID: context.sessionID as string, rootOperationID: context.rootOperationID as string,
          actorID: context.actorID as string, executionRevision: context.executionRevision as number,
          innerTurnID: context.innerTurnID as string, callID: crypto.randomUUID() }
        let cancelling: Promise<unknown> | undefined
        const abort = () => { cancelling ??= managedHostCall("_openclank/session/v1/python-kernel/call", {
          ...identity, request: { operation: "cancel" },
        }).catch(() => undefined) }
        options.abortSignal?.addEventListener("abort", abort, { once:true })
        try {
          if (options.abortSignal?.aborted) abort()
          const result = await managedHostCall("_openclank/session/v1/python-kernel/call", { ...identity, request: args as never })
          if (options.abortSignal?.aborted) { await cancelling; throw new Error("Tool call aborted") }
          return { content: [{ type: "text" as const, text: JSON.stringify(result) }] }
        } finally {
          options.abortSignal?.removeEventListener("abort", abort)
          if (cancelling) await cancelling
        }
      }
      if (managedFrameName && args && typeof args === "object" && !Array.isArray(args) && (args as Record<string,unknown>).action === "frame") {
        if (!context || !supportsTurnLifecycle(client) || typeof context.backgroundMessageID!=="string" || typeof options.toolCallId!=="string" || !options.toolCallId || options.abortSignal?.aborted) throw new Error("Frame capture unavailable")
        const { frameCapture }=await import("@/mcp/frame-capture")
        const normalized=frameCapture(args);if (!normalized) throw new Error("Invalid frame request")
        const { managedHostCall, ManagedProvider }=await import("@/acp/managed-provider")
        const identity={sessionID:context.sessionID as string,rootOperationID:context.rootOperationID as string,actorID:context.actorID as string,executionRevision:context.executionRevision as number,innerTurnID:context.innerTurnID as string,messageID:context.backgroundMessageID,toolCallID:options.toolCallId,toolName:managedFrameName}
        if (!await ManagedProvider.claimFreshTaskCall(identity)) throw new Error("Frame invocation unavailable")
        const request={...identity,...normalized};let cancelling:Promise<unknown>|undefined
        const abort=()=>{if(!cancelling)cancelling=managedHostCall("_openclank/session/v1/actor-frame/call",{...request,mode:"cancel"}).catch(()=>undefined)}
        options.abortSignal?.addEventListener("abort",abort,{once:true})
        if(options.abortSignal?.aborted){abort();await cancelling;throw new Error("Frame outcome unavailable")}
        try {
          let result
          try {result=await managedHostCall("_openclank/session/v1/actor-frame/call",{...request,mode:"issue"})} catch {}
          if (!result?.ok && !options.abortSignal?.aborted) result=await managedHostCall("_openclank/session/v1/actor-frame/call",{...request,mode:"status"})
          if(options.abortSignal?.aborted) {await cancelling;throw new Error("Frame outcome unavailable")}
          return {content:[{type:"text" as const,text:JSON.stringify(result)}]}
        } finally {options.abortSignal?.removeEventListener("abort",abort);if(cancelling)await cancelling}
      }
      if (managedSharedTextName) {
        if (!context || !supportsTurnLifecycle(client) || typeof context.backgroundMessageID !== "string" ||
            typeof options.toolCallId !== "string" || !options.toolCallId || options.abortSignal?.aborted) throw new Error("Shared text unavailable")
        const { sharedText } = await import("@/mcp/shared-text")
        const normalized = sharedText(args)
        if (!normalized) throw new Error("Invalid shared text request")
        const { managedHostCall, ManagedProvider } = await import("@/acp/managed-provider")
        const identity = {sessionID:context.sessionID as string, rootOperationID:context.rootOperationID as string,
          actorID:context.actorID as string, executionRevision:context.executionRevision as number,
          innerTurnID:context.innerTurnID as string, messageID:context.backgroundMessageID,
          toolCallID:options.toolCallId, toolName:managedSharedTextName}
        if (!await ManagedProvider.claimFreshTaskCall(identity)) throw new Error("Shared text invocation unavailable")
        const request = { ...identity, ...normalized }
        let result
        try { result = await managedHostCall("_openclank/session/v1/shared-text/call", { ...request, mode:"issue" }) } catch {}
        if (!result?.ok && !options.abortSignal?.aborted) result = await managedHostCall("_openclank/session/v1/shared-text/call", { ...request, mode:"status" })
        if (options.abortSignal?.aborted) throw new Error("Shared text outcome unavailable; use status")
        return {content:[{type:"text" as const,text:JSON.stringify(result)}]}
      }
      if (managedMediaName) {
        if (!context || !supportsTurnLifecycle(client) || typeof context.backgroundMessageID !== "string" ||
            typeof options.toolCallId !== "string" || !options.toolCallId || options.abortSignal?.aborted ||
            !args || typeof args !== "object" || Array.isArray(args)) throw new Error("Managed media unavailable")
        const { mediaJob } = await import("@/mcp/media-job")
        const normalized = mediaJob(args)
        if (!normalized) throw new Error("Invalid media request")
        const { managedHostCall, ManagedProvider } = await import("@/acp/managed-provider")
        const identity = {sessionID:context.sessionID as string, rootOperationID:context.rootOperationID as string,
          actorID:context.actorID as string, executionRevision:context.executionRevision as number,
          innerTurnID:context.innerTurnID as string, messageID:context.backgroundMessageID,
          toolCallID:options.toolCallId, toolName:managedMediaName}
        if (!await ManagedProvider.claimFreshTaskCall(identity)) throw new Error("Media invocation unavailable")
        const request = { ...identity, argsJSON:normalized.argsJSON, argsSHA256:normalized.argsSHA256 }
        let result
        try { result = await managedHostCall("_openclank/session/v1/media-job/call", { ...request, mode:"issue" }) } catch {}
        if (!result?.ok) result = await managedHostCall("_openclank/session/v1/media-job/call", { ...request, mode:"status" })
        if (options.abortSignal?.aborted) throw new Error("Media outcome unavailable; use job status")
        return {content:[{type:"text" as const,text:JSON.stringify(result)}]}
      }
      if (managedTheme && operation) {
        if (!context) throw new Error("Managed theme context unavailable")
        if (options.abortSignal?.aborted) throw new Error("Tool call aborted")
        const { managedHostCall } = await import("@/acp/managed-provider")
        const result = await managedHostCall("_openclank/session/v1/theme-emoji/call", {
          sessionID: context.sessionID as string, rootOperationID: context.rootOperationID as string,
          actorID: context.actorID as string, executionRevision: context.executionRevision as number,
          innerTurnID: context.innerTurnID as string, operation,
        })
        if (options.abortSignal?.aborted) throw new Error("Tool call aborted")
        return { content: [{ type: "text" as const, text: JSON.stringify(result) }] }
      }
      const witness=context ? (context as unknown as Record<PropertyKey,unknown>)[sourceOutlineAdmission] as SourceOutlineWitness | undefined : undefined
      const outlineCurrent = managedCode && context && supportsTurnLifecycle(client) && mcpTool.name==='search_code' &&
        args && typeof args==='object' && !Array.isArray(args) && (args as Record<string,unknown>).operation==='read' &&
        typeof options.toolCallId==='string' && options.toolCallId.length>0 && options.toolCallId.length<=256 &&
        witness && typeof witness.current==='function' ? witness.current : undefined
      const outlineRequest=outlineCurrent && outlineCurrent() === true && context ? {version:1,turn:{sessionID:context.sessionID,rootOperationID:context.rootOperationID,
        actorID:context.actorID,executionRevision:context.executionRevision,innerTurnID:context.innerTurnID},toolCallID:options.toolCallId} : undefined
      if (context) McpSampling.setActiveSession(client, SessionID.make(context.sessionId))
      const started = Date.now()
      let result: Awaited<ReturnType<MCPClient["callTool"]>>
      try {
        result = await client.callTool(
        {
          name: mcpTool.name,
          arguments: (args || {}) as Record<string, unknown>,
          ...(context && supportsTurnLifecycle(client) ? { _meta: { [TURN_LIFECYCLE_CAPABILITY]: context, ...(outlineRequest ? {"openclank.source-outline-request.v1":outlineRequest} : {}) } } : {}),
        },
        CallToolResultSchema,
        {
          resetTimeoutOnProgress: true,
          signal: options.abortSignal,
          timeout,
        },
        )
      } catch (error) {
        observe?.(Math.max(0, Date.now() - started), options.abortSignal?.aborted ? "cancelled" : "transport")
        throw error
      }
      observe?.(Math.max(0, Date.now() - started), result.isError ? "tool_error" : null)
      const meta = result._meta && typeof result._meta === "object" ? { ...result._meta } : undefined
      const privateOutline = meta?.['openclank.source-outline.v1']
      if (meta) {delete meta[RECALL_ADMISSION];delete meta['openclank.source-outline.v1']}
      const sanitized = { ...result, ...(meta && Object.keys(meta).length ? { _meta: meta } : { _meta: undefined }) }
      const content = result.content

      if (outlineCurrent && !result.isError && !options.abortSignal?.aborted &&
          typeof options.toolCallId==='string' && result.structuredContent===undefined && Array.isArray(content) &&
          content.length===1 && content[0]?.type==='text' && typeof content[0].text==='string') {
        const text=content[0].text
        const sidecar=outlineCurrent() === true ? boundedOutlineSidecar(privateOutline,options.toolCallId,text) ?? 'null' : 'null'
        outlineAttestations.set(sanitized,{toolCallID:options.toolCallId,text,sidecar,current:outlineCurrent})
      }
      // Only the bound private first-party reader may produce this observation.
      // Its original output is bound before any plugin can transform it.
      const rawCode = managedCode && context?.codeActivityEnabled === true && options.toolCallId && !result.isError &&
        args && typeof args === "object" && !Array.isArray(args) && (args as Record<string, unknown>).operation === "read" &&
        Array.isArray(content) && content.length === 1 && content[0]?.type === "text" ? content[0].text : undefined
      if (typeof rawCode === "string" && typeof options.toolCallId === "string" && context) {
        const projected = codeReadResource(rawCode)
        if (projected && !options.abortSignal?.aborted) {
          try {
            const { managedHostCall } = await import("@/acp/managed-provider")
            const event = await managedHostCall("_openclank/session/v1/code-activity/observe", {
              sessionID: context.sessionID as string, rootOperationID: context.rootOperationID as string,
              actorID: context.actorID as string, executionRevision: context.executionRevision as number,
              innerTurnID: context.innerTurnID as string, toolCallID: options.toolCallId,
              resultSHA256: createHash("sha256").update(rawCode, "utf8").digest("hex"), resource: projected.resource,
            })
            if (event && !options.abortSignal?.aborted)
              codeActivityAttestations.set(sanitized, {toolCallID:options.toolCallId, text:rawCode, event})
          } catch { /* Activity is optional; do not change the reader result. */ }
        }
      }
      return sanitized
    },
  })
}

function defs(key: string, client: MCPClient, timeout?: number) {
  return Effect.tryPromise({
    try: () => withTimeout(client.listTools(), timeout ?? DEFAULT_TIMEOUT),
    catch: (err) => (err instanceof Error ? err : new Error(String(err))),
  }).pipe(
    Effect.map((result) => result.tools),
    Effect.catch((err) => {
      log.error("failed to get tools from client", { key, error: err })
      return Effect.succeed(undefined)
    }),
  )
}

function fetchFromClient<T extends { name: string }>(
  clientName: string,
  client: Client,
  listFn: (c: Client) => Promise<T[]>,
  label: string,
) {
  return Effect.tryPromise({
    try: () => listFn(client),
    catch: (e: any) => {
      log.error(`failed to get ${label}`, { clientName, error: e.message })
      return e
    },
  }).pipe(
    Effect.map((items) => {
      const out: Record<string, T & { client: string }> = {}
      const sanitizedClient = sanitize(clientName)
      for (const item of items) {
        out[sanitizedClient + ":" + sanitize(item.name)] = { ...item, client: clientName }
      }
      return out
    }),
    Effect.orElseSucceed(() => undefined),
  )
}

interface CreateResult {
  mcpClient?: MCPClient
  status: Status
  defs?: MCPToolDef[]
  discoveryMs?: number
}

interface AuthResult {
  authorizationUrl: string
  oauthState: string
  client?: MCPClient
}

// --- Effect Service ---

interface State {
  status: Record<string, Status>
  clients: Record<string, MCPClient>
  defs: Record<string, MCPToolDef[]>
  healthSerial: number
  health: Record<string, HealthObservation>
  healthTurns: Map<string, { candidates: Record<string, HealthCandidate>; signature: string; exec?: ExecCapabilities }>
}

function healthSignature(s: State): string {
  return JSON.stringify(Object.keys(s.status).sort().map((name) => [name, s.status[name]?.status, s.health[name]?.instanceID ?? null, s.health[name]?.generation ?? null, s.health[name]?.revision ?? null]))
}

export interface Interface {
  readonly prefixGeneration?: (request: HealthIdentity) => Effect.Effect<Array<{instanceID:string; revision:number; count:number}> | undefined>
  readonly healthSnapshot?: (request: HealthIdentity) => Effect.Effect<HealthSnapshot>
  readonly observeToolsCapabilities?: (request: HealthIdentity, value: ExecCapabilities) => Effect.Effect<void>
  readonly toolsCapabilitiesSnapshot?: (request: HealthIdentity) => Effect.Effect<ToolsCapabilitiesSnapshot>
  readonly status: () => Effect.Effect<Record<string, Status>>
  readonly clients: () => Effect.Effect<Record<string, MCPClient>>
  readonly tools: (context?: TurnContext, options?: { managedPrivateRootSessionID: string; admit: (toolName: string, kind: "tool" | "mcp_tool" | "memory_read", toolCallID?: string, operation?: "status"|"discover"|"download"|"cancel") => Promise<string | undefined> }) => Effect.Effect<Record<string, Tool>>
  readonly prompts: () => Effect.Effect<Record<string, PromptInfo & { client: string }>>
  readonly resources: () => Effect.Effect<Record<string, ResourceInfo & { client: string }>>
  readonly add: (name: string, mcp: ConfigMCP.Info) => Effect.Effect<{ status: Record<string, Status> | Status }>
  readonly connect: (name: string) => Effect.Effect<void>
  readonly disconnect: (name: string) => Effect.Effect<void>
  readonly getPrompt: (
    clientName: string,
    name: string,
    args?: Record<string, string>,
  ) => Effect.Effect<Awaited<ReturnType<MCPClient["getPrompt"]>> | undefined>
  readonly readResource: (
    clientName: string,
    resourceUri: string,
  ) => Effect.Effect<Awaited<ReturnType<MCPClient["readResource"]>> | undefined>
  readonly startAuth: (mcpName: string) => Effect.Effect<{ authorizationUrl: string; oauthState: string }>
  readonly authenticate: (mcpName: string) => Effect.Effect<Status>
  readonly finishAuth: (mcpName: string, authorizationCode: string) => Effect.Effect<Status>
  readonly removeAuth: (mcpName: string) => Effect.Effect<void>
  readonly supportsOAuth: (mcpName: string) => Effect.Effect<boolean>
  readonly hasStoredTokens: (mcpName: string) => Effect.Effect<boolean>
  readonly getAuthStatus: (mcpName: string) => Effect.Effect<AuthStatus>
}

export class Service extends Context.Service<Service, Interface>()("@opencode/MCP") {}

export const layer = Layer.effect(
  Service,
  Effect.gen(function* () {
    const spawner = yield* ChildProcessSpawner.ChildProcessSpawner
    const auth = yield* McpAuth.Service
    const bus = yield* Bus.Service

    const createClient = () => new Client({ name: "mimocode", version: InstallationVersion }, CLIENT_OPTIONS)
    type Transport = StdioClientTransport | ObservingStdioTransport | StreamableHTTPClientTransport | SSEClientTransport

    /**
     * Connect a client via the given transport with resource safety:
     * on failure the transport is closed; on success the caller owns it.
     */
    const connectTransport = (transport: Transport, timeout: number) =>
      Effect.acquireUseRelease(
        Effect.succeed(transport),
        (t) =>
          Effect.tryPromise({
            try: () => {
              const client = createClient()
              return withTimeout(client.connect(t), timeout).then(() => client)
            },
            catch: (e) => (e instanceof Error ? e : new Error(String(e))),
          }),
        (t, exit) => (Exit.isFailure(exit) ? Effect.tryPromise(() => t.close()).pipe(Effect.ignore) : Effect.void),
      )

    const DISABLED_RESULT: CreateResult = { status: { status: "disabled" } }

    const connectRemote = Effect.fn("MCP.connectRemote")(function* (
      key: string,
      mcp: ConfigMCP.Info & { type: "remote" },
    ) {
      const oauthDisabled = mcp.oauth === false
      const oauthConfig = typeof mcp.oauth === "object" ? mcp.oauth : undefined
      let authProvider: McpOAuthProvider | undefined

      if (!oauthDisabled) {
        authProvider = new McpOAuthProvider(
          key,
          mcp.url,
          {
            clientId: oauthConfig?.clientId,
            clientSecret: oauthConfig?.clientSecret,
            scope: oauthConfig?.scope,
            redirectUri: oauthConfig?.redirectUri,
          },
          {
            onRedirect: async (url) => {
              log.info("oauth redirect requested", { key, url: url.toString() })
            },
          },
          auth,
        )
      }

      const transports: Array<{ name: string; transport: TransportWithAuth }> = [
        {
          name: "StreamableHTTP",
          transport: new StreamableHTTPClientTransport(new URL(mcp.url), {
            authProvider,
            requestInit: mcp.headers ? { headers: mcp.headers } : undefined,
          }),
        },
        {
          name: "SSE",
          transport: new SSEClientTransport(new URL(mcp.url), {
            authProvider,
            requestInit: mcp.headers ? { headers: mcp.headers } : undefined,
          }),
        },
      ]

      const connectTimeout = mcp.timeout ?? DEFAULT_TIMEOUT
      let lastStatus: Status | undefined

      for (const { name, transport } of transports) {
        const result = yield* connectTransport(transport, connectTimeout).pipe(
          Effect.map((client) => ({ client, transportName: name })),
          Effect.catch((error) => {
            const lastError = error instanceof Error ? error : new Error(String(error))
            const isAuthError =
              error instanceof UnauthorizedError || (authProvider && lastError.message.includes("OAuth"))

            if (isAuthError) {
              log.info("mcp server requires authentication", { key, transport: name })

              if (lastError.message.includes("registration") || lastError.message.includes("client_id")) {
                lastStatus = {
                  status: "needs_client_registration" as const,
                  error: "Server does not support dynamic client registration. Please provide clientId in config.",
                }
                return bus
                  .publish(TuiEvent.ToastShow, {
                    title: "MCP Authentication Required",
                    message: `Server "${key}" requires a pre-registered client ID. Add clientId to your config.`,
                    variant: "warning",
                    duration: 8000,
                  })
                  .pipe(Effect.ignore, Effect.as(undefined))
              } else {
                pendingOAuthTransports.set(key, transport)
                lastStatus = { status: "needs_auth" as const }
                return bus
                  .publish(TuiEvent.ToastShow, {
                    title: "MCP Authentication Required",
                    message: `Server "${key}" requires authentication. Run: opencode mcp auth ${key}`,
                    variant: "warning",
                    duration: 8000,
                  })
                  .pipe(Effect.ignore, Effect.as(undefined))
              }
            }

            log.debug("transport connection failed", {
              key,
              transport: name,
              url: ConfigMCP.redactString(mcp.url),
              error: lastError.message,
            })
            lastStatus = { status: "failed" as const, error: lastError.message }
            return Effect.succeed(undefined)
          }),
        )
        if (result) {
          log.info("connected", { key, transport: result.transportName })
          return { client: result.client as MCPClient | undefined, status: { status: "connected" } as Status }
        }
        // If this was an auth error, stop trying other transports
        if (lastStatus?.status === "needs_auth" || lastStatus?.status === "needs_client_registration") break
      }

      return {
        client: undefined as MCPClient | undefined,
        status: (lastStatus ?? { status: "failed", error: "Unknown error" }) as Status,
      }
    })

    const connectLocal = Effect.fn("MCP.connectLocal")(function* (
      key: string,
      mcp: ConfigMCP.Info & { type: "local" },
    ) {
      const [cmd, ...args] = mcp.command
      const cwd = yield* InstanceState.directory
      // ObservingStdioTransport owns the child through child_process and applies
      // childProcessEnv() at the spawn site — local MCP servers are third-party
      // binaries running as the user. Replaces StdioClientTransport so exit
      // identity stays observable after close().
      const transport = new ObservingStdioTransport({
        command: cmd,
        args,
        cwd,
        env: {
          ...(cmd === "opencode" ? { BUN_BE_BUN: "1" } : {}),
          ...mcp.environment,
        },
      })
      transport.onStderr = (text: string) => {
        log.info(`mcp stderr: ${text}`, { key })
      }

      const connectTimeout = mcp.timeout ?? DEFAULT_TIMEOUT
      return yield* connectTransport(transport, connectTimeout).pipe(
        Effect.map((client): { client: MCPClient | undefined; status: Status } => ({
          client,
          status: { status: "connected" },
        })),
        Effect.catch((error): Effect.Effect<{ client: MCPClient | undefined; status: Status }> => {
          const msg = error instanceof Error ? error.message : String(error)
          log.error("local mcp startup failed", { key, command: ConfigMCP.redactCommand(mcp.command), cwd, error: msg })
          return Effect.succeed({ client: undefined, status: { status: "failed", error: msg } })
        }),
      )
    })

    const create = Effect.fn("MCP.create")(function* (key: string, mcp: ConfigMCP.Info) {
      if (mcp.enabled === false) {
        log.info("mcp server disabled", { key })
        return DISABLED_RESULT
      }

      log.info("found", { key, type: mcp.type })

      const { client: mcpClient, status } =
        mcp.type === "remote"
          ? yield* connectRemote(key, mcp as ConfigMCP.Info & { type: "remote" })
          : yield* connectLocal(key, mcp as ConfigMCP.Info & { type: "local" })

      if (!mcpClient) {
        return { status } satisfies CreateResult
      }

      const discoveryStarted = Date.now()
      const listed = yield* defs(key, mcpClient, mcp.timeout)
      if (!listed) {
        yield* Effect.tryPromise(() => mcpClient.close()).pipe(Effect.ignore)
        return { status: { status: "failed", error: "Failed to get tools" } } satisfies CreateResult
      }

      log.info("create() successfully created client", { key, toolCount: listed.length })
      return { mcpClient, status, defs: listed, discoveryMs: Math.max(0, Date.now() - discoveryStarted) } satisfies CreateResult
    })
    const cfgSvc = yield* Config.Service

    const descendants = Effect.fnUntraced(
      function* (pid: number) {
        if (process.platform === "win32") return [] as number[]
        const pids: number[] = []
        const queue = [pid]
        while (queue.length > 0) {
          const current = queue.shift()!
          const handle = yield* spawner.spawn(ChildProcess.make("pgrep", ["-P", String(current)], { stdin: "ignore" }))
          const text = yield* Stream.mkString(Stream.decodeText(handle.stdout))
          yield* handle.exitCode
          for (const tok of text.split("\n")) {
            const cpid = parseInt(tok, 10)
            if (!isNaN(cpid) && !pids.includes(cpid)) {
              pids.push(cpid)
              queue.push(cpid)
            }
          }
        }
        return pids
      },
      Effect.scoped,
      Effect.catch(() => Effect.succeed([] as number[])),
    )

    function watch(s: State, name: string, client: MCPClient, bridge: EffectBridge.Shape, timeout?: number) {
      client.setNotificationHandler(ToolListChangedNotificationSchema, async () => {
        log.info("tools list changed notification received", { server: name })
        if (s.clients[name] !== client || s.status[name]?.status !== "connected") return

        // Fence the old definitions before waiting for rediscovery.
        const observation = s.health[name]
        if (!observation) return
        observation.revision++; observation.discoveryFailed = true
        const revision = observation.revision

        const started = Date.now()
        const listed = await bridge.promise(defs(name, client, timeout))
        if (s.clients[name] !== client || s.status[name]?.status !== "connected" || s.health[name] !== observation || observation.revision !== revision) return
        observation.discoveryFailed = !listed; observation.discoveryMs = Math.max(0, Date.now() - started); observation.observedAt = Date.now()
        if (!listed) return
        s.defs[name] = listed
        await bridge.promise(bus.publish(ToolsChanged, { server: name }).pipe(Effect.ignore))
      })
      McpSampling.serve(name, client, bridge)
    }

    const state = yield* InstanceState.make<State>(
      Effect.fn("MCP.state")(function* () {
        const cfg = yield* cfgSvc.get()
        const bridge = yield* EffectBridge.make()
        const config = cfg.mcp ?? {}
        const s: State = {
          status: {},
          clients: {},
          defs: {},
          healthSerial: 0, health: {}, healthTurns: new Map(),
        }

        yield* Effect.forEach(
          Object.entries(config),
          ([key, mcp]) =>
            Effect.gen(function* () {
              if (!isMcpConfigured(mcp)) {
                log.error("Ignoring MCP config entry without type", { key })
                return
              }

              if (mcp.enabled === false) {
                s.status[key] = { status: "disabled" }
                return
              }

              const result = yield* create(key, mcp).pipe(Effect.catch(() => Effect.void))
              if (!result) return

              s.status[key] = result.status
              if (result.mcpClient) {
                s.clients[key] = result.mcpClient
                s.defs[key] = result.defs!
                s.health[key] = { instanceID:crypto.randomUUID(), generation: ++s.healthSerial, revision: 0, discoveryFailed: false, observedAt: Date.now(), discoveryMs: result.discoveryMs ?? null, lastCallAt: null, lastCallMs: null, lastCallCategory: null, lastCallTurn: null }
                if (key.startsWith("lifetools")) {
                  const memory = yield* Effect.promise(() => import("@/memory/mcp-client"))
                  memory.registerManagedMcpClient(key, result.mcpClient)
                }
                watch(s, key, result.mcpClient, bridge, mcp.timeout)
              }
            }),
          { concurrency: "unbounded" },
        )

        yield* Effect.addFinalizer(() =>
          Effect.gen(function* () {
            yield* Effect.forEach(
              Object.entries(s.clients),
              ([name, client]) =>
                Effect.gen(function* () {
                  if (name.startsWith("lifetools")) {
                    const memory = yield* Effect.promise(() => import("@/memory/mcp-client"))
                    memory.unregisterManagedMcpClient(name, client)
                  }
                  yield* McpSampling.cancelAll(client)
                  const pid =
                    client.transport instanceof ObservingStdioTransport || client.transport instanceof StdioClientTransport
                      ? client.transport.pid
                      : null
                  if (typeof pid === "number") {
                    const pids = yield* descendants(pid)
                    for (const dpid of pids) {
                      try {
                        process.kill(dpid, "SIGTERM")
                      } catch {}
                    }
                  }
                  yield* Effect.tryPromise(() => client.close()).pipe(Effect.ignore)
                }),
              { concurrency: "unbounded" },
            )
            pendingOAuthTransports.clear()
          }),
        )

        return s
      }),
    )

    function closeClient(s: State, name: string) {
      const client = s.clients[name]
      delete s.defs[name]
      delete s.health[name]
      return Effect.gen(function* () {
        if (name.startsWith("lifetools")) {
          const memory = yield* Effect.promise(() => import("@/memory/mcp-client"))
          memory.unregisterManagedMcpClient(name, client)
        }
        if (client) {
          yield* McpSampling.cancelAll(client)
          yield* Effect.tryPromise(() => client.close()).pipe(Effect.ignore)
        }
      })
    }

    const storeClient = Effect.fnUntraced(function* (
      s: State,
      name: string,
      client: MCPClient,
      listed: MCPToolDef[],
      timeout?: number,
      discoveryMs?: number,
    ) {
      const bridge = yield* EffectBridge.make()
      yield* closeClient(s, name)
      s.status[name] = { status: "connected" }
      s.clients[name] = client
      s.defs[name] = listed
      s.health[name] = { instanceID:crypto.randomUUID(), generation: ++s.healthSerial, revision: 0, discoveryFailed: false, observedAt: Date.now(), discoveryMs: discoveryMs ?? null, lastCallAt: null, lastCallMs: null, lastCallCategory: null, lastCallTurn: null }
      if (name.startsWith("lifetools")) {
        const memory = yield* Effect.promise(() => import("@/memory/mcp-client"))
        memory.registerManagedMcpClient(name, client)
      }
      watch(s, name, client, bridge, timeout)
      return s.status[name]
    })

    const observeToolsCapabilities = Effect.fn("MCP.observeToolsCapabilities")(function* (request: HealthIdentity, value: ExecCapabilities) {
      const key = healthTurnKey(request)
      if (!key) return
      const cached = yield* ScopedCache.getSuccess(state.cache, yield* InstanceState.directory)
      if (Option.isNone(cached)) return
      const turn = cached.value.healthTurns.get(key)
      if (turn && turn.signature === healthSignature(cached.value)) turn.exec = value
    })
    const toolsCapabilitiesSnapshot = Effect.fn("MCP.toolsCapabilitiesSnapshot")(function* (request: HealthIdentity) {
      const key = healthTurnKey(request)
      if (!key) return { status: "unavailable" as const, exec: null }
      const cached = yield* ScopedCache.getSuccess(state.cache, yield* InstanceState.directory)
      if (Option.isNone(cached)) return { status: "unavailable" as const, exec: null }
      const turn = cached.value.healthTurns.get(key)
      if (!turn?.exec || turn.signature !== healthSignature(cached.value)) return { status: "unavailable" as const, exec: null }
      return { status: "observed" as const, exec: turn.exec }
    })

    const prefixGeneration: NonNullable<Interface["prefixGeneration"]> = Effect.fn("MCP.prefixGeneration")(function* (request) {
      const key = healthTurnKey(request)
      if (!key) return undefined
      const cached = yield* ScopedCache.getSuccess(state.cache, yield* InstanceState.directory)
      if (Option.isNone(cached)) return undefined
      const s = cached.value, turn = s.healthTurns.get(key)
      if (!turn || turn.signature !== healthSignature(s)) return undefined
      const names = Object.keys(turn.candidates).sort()
      if (!names.length || names.length > 128) return undefined
      const result: Array<{instanceID:string;revision:number;count:number}> = []
      for (const name of names) {
        const candidate = turn.candidates[name]!, observation = s.health[name]
        if (s.status[name]?.status !== "connected" || !s.clients[name] || !s.defs[name] || !observation || observation.discoveryFailed ||
          candidate.generation !== observation.generation || candidate.revision !== observation.revision) return undefined
        result.push({instanceID:observation.instanceID, revision:observation.revision, count:candidate.count})
      }
      return result
    })

    const healthSnapshot = Effect.fn("MCP.healthSnapshot")(function* (request: HealthIdentity) {
      const key = healthTurnKey(request)
      if (!key) return unavailableHealth()
      // getSuccess neither initializes a missing entry nor awaits an initializer.
      const cached = yield* ScopedCache.getSuccess(state.cache, yield* InstanceState.directory)
      if (Option.isNone(cached)) return unavailableHealth()
      const s = cached.value
      const turn = s.healthTurns.get(key)
      if (!turn || turn.signature !== healthSignature(s)) return unavailableHealth()
      if (Object.entries(turn.candidates).some(([name, candidate]) => {
        const observation = s.health[name]
        return s.status[name]?.status !== "connected" || !s.clients[name] || !observation || candidate.generation !== observation.generation || candidate.revision !== observation.revision
      })) return unavailableHealth()
      // Only clients eligible in this exact turn may be projected.
      const all = Object.keys(turn.candidates).sort()
      const names = all.filter((name) => /^[A-Za-z0-9_-]{1,128}$/.test(name)).slice(0, 128)
      const allowedStatus = new Set(["connected", "disabled", "failed", "needs_auth", "needs_client_registration", "disconnected"])
      const servers: HealthSnapshot["servers"] = names.map((name) => {
        const observation = s.health[name]
        const rawStatus = s.status[name]?.status
        const status = (rawStatus === "connected" && observation?.discoveryFailed ? "degraded" : allowedStatus.has(rawStatus) ? rawStatus : "unknown") as HealthSnapshot["servers"][number]["status"]
        const candidate = turn.candidates[name]
        const count = s.defs[name]?.length
        return { serverKey: name, status, connectionGeneration: observation?.generation ?? null, observedAt: observation?.observedAt ?? null,
          discoveryMs: observation?.discoveryMs ?? null, lastCallAt: observation?.lastCallTurn === key ? observation.lastCallAt : null, lastCallMs: observation?.lastCallTurn === key ? observation.lastCallMs : null,
          lastCallCategory: observation?.lastCallTurn === key ? observation.lastCallCategory : null, discoveredCount: Number.isSafeInteger(count) && count! <= 100000 ? count! : null,
          offeredToPromptCount: candidate && candidate.count <= 100000 ? candidate.count : null, modelOfferedCount: null, callAdmissibleCount: null }
      })
      return { status: "observed", servers, omittedServers: all.length - names.length } satisfies HealthSnapshot
    })

    const status = Effect.fn("MCP.status")(function* () {
      const s = yield* InstanceState.get(state)

      const cfg = yield* cfgSvc.get()
      const config = cfg.mcp ?? {}
      const result: Record<string, Status> = {}

      for (const [key, mcp] of Object.entries(config)) {
        if (!isMcpConfigured(mcp)) continue
        result[key] = s.status[key] ?? { status: "disabled" }
      }

      return result
    })

    const clients = Effect.fn("MCP.clients")(function* () {
      const s = yield* InstanceState.get(state)
      return s.clients
    })

    const createAndStore = Effect.fn("MCP.createAndStore")(function* (name: string, mcp: ConfigMCP.Info) {
      const s = yield* InstanceState.get(state)
      const result = yield* create(name, mcp)

      s.status[name] = result.status
      if (!result.mcpClient) {
        yield* closeClient(s, name)
        delete s.clients[name]
        return result.status
      }

      return yield* storeClient(s, name, result.mcpClient, result.defs!, mcp.timeout, result.discoveryMs)
    })

    const add = Effect.fn("MCP.add")(function* (name: string, mcp: ConfigMCP.Info) {
      yield* createAndStore(name, mcp)
      const s = yield* InstanceState.get(state)
      return { status: s.status }
    })

    const connect = Effect.fn("MCP.connect")(function* (name: string) {
      const mcp = yield* getMcpConfig(name)
      if (!mcp) {
        log.error("MCP config not found or invalid", { name })
        return
      }
      yield* createAndStore(name, { ...mcp, enabled: true })
    })

    const disconnect = Effect.fn("MCP.disconnect")(function* (name: string) {
      const s = yield* InstanceState.get(state)
      yield* closeClient(s, name)
      delete s.clients[name]
      delete s.health[name]
      s.status[name] = { status: "disabled" }
    })

    const tools = Effect.fn("MCP.tools")(function* (context?: TurnContext, options?: { managedPrivateRootSessionID: string; admit: (toolName: string, kind: "tool" | "mcp_tool" | "memory_read", toolCallID?: string, operation?: "status"|"discover"|"download"|"cancel") => Promise<string | undefined> }) {
      const result: Record<string, Tool> = {}
      const privateClient = options ? yield* Effect.promise(async () => {
        if (!context || typeof context.sessionID !== "string" || context.sessionID !== context.sessionId ||
            typeof context.rootOperationID !== "string" || !context.rootOperationID ||
            typeof context.actorID !== "string" || context.actorID !== context.actorId ||
            typeof context.executionRevision !== "number" || !Number.isSafeInteger(context.executionRevision) || context.executionRevision < 0 ||
            typeof context.innerTurnID !== "string" || context.innerTurnID !== context.turnId) return undefined
        try {
          const memory = await import("@/memory/mcp-client")
          return await memory.getSharedMcpClient(options.managedPrivateRootSessionID)
        } catch {
          return undefined
        }
      }) : undefined
      if (options && (!context || typeof context.sessionID !== "string" || context.sessionID !== context.sessionId ||
          typeof context.rootOperationID !== "string" || !context.rootOperationID || typeof context.actorID !== "string" ||
          context.actorID !== context.actorId || typeof context.executionRevision !== "number" ||
          !Number.isSafeInteger(context.executionRevision) || context.executionRevision < 0 ||
          typeof context.innerTurnID !== "string" || context.innerTurnID !== context.turnId)) return result
      const s = yield* InstanceState.get(state)

      const cfg = yield* cfgSvc.get()
      const config = cfg.mcp ?? {}
      const defaultTimeout = cfg.experimental?.mcp_timeout

      const observedTurn = options ? healthTurnKey(context as Partial<HealthIdentity>) : undefined
      const candidates: Record<string, HealthCandidate> = {}
      const wrapperOwners: Record<string, string> = {}
      let candidateMissing = false
      const connectedClients = Object.entries(s.clients).filter(
        ([clientName]) => s.status[clientName]?.status === "connected",
      )

      yield* Effect.forEach(
        connectedClients,
        ([clientName, client]) =>
          Effect.gen(function* () {
            const isPrivate = options && client === privateClient
            if (options && ((isPrivate && !supportsTurnLifecycle(client)) || (!isPrivate && clientName.startsWith("lifetools")))) return
            const initialObservation = s.health[clientName]
            if (initialObservation) candidates[clientName] = { generation: initialObservation.generation, revision: initialObservation.revision, count: 0 }
            else candidateMissing = true
            const mcpConfig = config[clientName]
            const entry = mcpConfig && isMcpConfigured(mcpConfig) ? mcpConfig : undefined

            const listed = s.defs[clientName]
            if (!listed) {
              log.warn("missing cached tools for connected server", { clientName })
              return
            }

            const timeout = entry?.timeout ?? defaultTimeout
            for (const mcpTool of listed) {
              if (options && !isPrivate && (clientName.includes("__") || mcpTool.name.includes("__"))) continue
              if (isPrivate && ["search", "manage_memory", "recall_memory"].includes(mcpTool.name)) continue
              const admittedName = isPrivate && mcpTool.name === "python_kernel" ? "python" : isPrivate ? mcpTool.name : `mcp__${clientName}__${mcpTool.name}`
              const admit = options ? (toolCallID?: string, operation?: "status"|"discover"|"download"|"cancel") => options.admit(admittedName, isPrivate ? "tool" : "mcp_tool", (isPrivate && ["search_code", "manage_bg_jobs", "manage_tasks", "media_job", "capture_desktop", "shared_text"].includes(mcpTool.name)) ? toolCallID : undefined, isPrivate && mcpTool.name === "theme_emoji_assets" ? operation : undefined) : undefined
              const wrapperName = sanitize(clientName) + "_" + sanitize(mcpTool.name)
              const prior = wrapperOwners[wrapperName]
              if (prior && candidates[prior]) candidates[prior].count--
              wrapperOwners[wrapperName] = clientName
              if (candidates[clientName]) candidates[clientName].count++
              const observation = s.health[clientName]
              const observe = observation ? (duration: number, category: HealthObservation["lastCallCategory"]) => {
                if (s.clients[clientName] !== client || s.health[clientName] !== observation) return
                observation.lastCallAt = Date.now(); observation.lastCallMs = duration; observation.lastCallCategory = category; observation.lastCallTurn = observedTurn ?? null
              } : undefined
              result[wrapperName] = convertMcpTool(mcpTool, client, timeout,
                options && !isPrivate ? undefined : context, admit, Boolean(isPrivate && mcpTool.name === "theme_emoji_assets"), observe,
                Boolean(isPrivate && mcpTool.name === "python_kernel"), Boolean(isPrivate && mcpTool.name === "search_code"),
                isPrivate && mcpTool.name === "manage_bg_jobs" ? wrapperName : undefined,
                isPrivate && mcpTool.name === "manage_tasks" ? wrapperName : undefined, isPrivate && mcpTool.name === "media_job" ? wrapperName : undefined, isPrivate && mcpTool.name === "capture_desktop" ? wrapperName : undefined, isPrivate && mcpTool.name === "shared_text" ? wrapperName : undefined)
            }
          }),
        { concurrency: "unbounded" },
      )
      const key = observedTurn
      if (key) {
        s.healthTurns.delete(key)
        if (!candidateMissing) s.healthTurns.set(key, { candidates, signature: healthSignature(s) })
        while (s.healthTurns.size > 32) s.healthTurns.delete(s.healthTurns.keys().next().value!)
      }
      return result
    })

    function collectFromConnected<T extends { name: string }>(
      s: State,
      listFn: (c: Client) => Promise<T[]>,
      label: string,
    ) {
      return Effect.forEach(
        Object.entries(s.clients).filter(([name]) => s.status[name]?.status === "connected"),
        ([clientName, client]) =>
          fetchFromClient(clientName, client, listFn, label).pipe(Effect.map((items) => Object.entries(items ?? {}))),
        { concurrency: "unbounded" },
      ).pipe(Effect.map((results) => Object.fromEntries<T & { client: string }>(results.flat())))
    }

    const prompts = Effect.fn("MCP.prompts")(function* () {
      const s = yield* InstanceState.get(state)
      return yield* collectFromConnected(s, (c) => c.listPrompts().then((r) => r.prompts), "prompts")
    })

    const resources = Effect.fn("MCP.resources")(function* () {
      const s = yield* InstanceState.get(state)
      return yield* collectFromConnected(s, (c) => c.listResources().then((r) => r.resources), "resources")
    })

    const withClient = Effect.fnUntraced(function* <A>(
      clientName: string,
      fn: (client: MCPClient) => Promise<A>,
      label: string,
      meta?: Record<string, unknown>,
    ) {
      const s = yield* InstanceState.get(state)
      const client = s.clients[clientName]
      if (!client) {
        log.warn(`client not found for ${label}`, { clientName })
        return undefined
      }
      return yield* Effect.tryPromise({
        try: () => fn(client),
        catch: (e: any) => {
          log.error(`failed to ${label}`, { clientName, ...meta, error: e?.message })
          return e
        },
      }).pipe(Effect.orElseSucceed(() => undefined))
    })

    const getPrompt = Effect.fn("MCP.getPrompt")(function* (
      clientName: string,
      name: string,
      args?: Record<string, string>,
    ) {
      return yield* withClient(clientName, (client) => client.getPrompt({ name, arguments: args }), "getPrompt", {
        promptName: name,
      })
    })

    const readResource = Effect.fn("MCP.readResource")(function* (clientName: string, resourceUri: string) {
      return yield* withClient(clientName, (client) => client.readResource({ uri: resourceUri }), "readResource", {
        resourceUri,
      })
    })

    const getMcpConfig = Effect.fnUntraced(function* (mcpName: string) {
      const cfg = yield* cfgSvc.get()
      const mcpConfig = cfg.mcp?.[mcpName]
      if (!mcpConfig || !isMcpConfigured(mcpConfig)) return undefined
      return mcpConfig
    })

    const startAuth = Effect.fn("MCP.startAuth")(function* (mcpName: string) {
      const mcpConfig = yield* getMcpConfig(mcpName)
      if (!mcpConfig) throw new Error(`MCP server ${mcpName} not found or disabled`)
      if (mcpConfig.type !== "remote") throw new Error(`MCP server ${mcpName} is not a remote server`)
      if (mcpConfig.oauth === false) throw new Error(`MCP server ${mcpName} has OAuth explicitly disabled`)

      // OAuth config is optional - if not provided, we'll use auto-discovery
      const oauthConfig = typeof mcpConfig.oauth === "object" ? mcpConfig.oauth : undefined

      // Start the callback server with custom redirectUri if configured
      yield* Effect.promise(() => McpOAuthCallback.ensureRunning(oauthConfig?.redirectUri))

      const oauthState = Array.from(crypto.getRandomValues(new Uint8Array(32)))
        .map((b) => b.toString(16).padStart(2, "0"))
        .join("")
      yield* auth.updateOAuthState(mcpName, oauthState)
      let capturedUrl: URL | undefined
      const authProvider = new McpOAuthProvider(
        mcpName,
        mcpConfig.url,
        {
          clientId: oauthConfig?.clientId,
          clientSecret: oauthConfig?.clientSecret,
          scope: oauthConfig?.scope,
          redirectUri: oauthConfig?.redirectUri,
        },
        {
          onRedirect: async (url) => {
            capturedUrl = url
          },
        },
        auth,
      )

      const transport = new StreamableHTTPClientTransport(new URL(mcpConfig.url), { authProvider })

      return yield* Effect.tryPromise({
        try: () => {
          const client = new Client({ name: "mimocode", version: InstallationVersion })
          return client
            .connect(transport)
            .then(() => ({ authorizationUrl: "", oauthState, client }) satisfies AuthResult)
        },
        catch: (error) => error,
      }).pipe(
        Effect.catch((error) => {
          if (error instanceof UnauthorizedError && capturedUrl) {
            pendingOAuthTransports.set(mcpName, transport)
            return Effect.succeed({ authorizationUrl: capturedUrl.toString(), oauthState } satisfies AuthResult)
          }
          return Effect.die(error)
        }),
      )
    })

    const authenticate = Effect.fn("MCP.authenticate")(function* (mcpName: string) {
      const result = yield* startAuth(mcpName)
      if (!result.authorizationUrl) {
        const client = "client" in result ? result.client : undefined
        const mcpConfig = yield* getMcpConfig(mcpName)
        if (!mcpConfig) {
          yield* Effect.tryPromise(() => client?.close() ?? Promise.resolve()).pipe(Effect.ignore)
          return { status: "failed", error: "MCP config not found after auth" } as Status
        }

        const listed = client ? yield* defs(mcpName, client, mcpConfig.timeout) : undefined
        if (!client || !listed) {
          yield* Effect.tryPromise(() => client?.close() ?? Promise.resolve()).pipe(Effect.ignore)
          return { status: "failed", error: "Failed to get tools" } as Status
        }

        const s = yield* InstanceState.get(state)
        yield* auth.clearOAuthState(mcpName)
        return yield* storeClient(s, mcpName, client, listed, mcpConfig.timeout)
      }

      log.info("opening browser for oauth", { mcpName, url: result.authorizationUrl, state: result.oauthState })

      const callbackPromise = McpOAuthCallback.waitForCallback(result.oauthState, mcpName)

      yield* Effect.tryPromise(() => open(result.authorizationUrl)).pipe(
        Effect.flatMap((subprocess) =>
          Effect.callback<void, Error>((resume) => {
            const timer = setTimeout(() => resume(Effect.void), 500)
            subprocess.on("error", (err) => {
              clearTimeout(timer)
              resume(Effect.fail(err))
            })
            subprocess.on("exit", (code) => {
              if (code !== null && code !== 0) {
                clearTimeout(timer)
                resume(Effect.fail(new Error(`Browser open failed with exit code ${code}`)))
              }
            })
          }),
        ),
        Effect.catch(() => {
          log.warn("failed to open browser, user must open URL manually", { mcpName })
          return bus.publish(BrowserOpenFailed, { mcpName, url: result.authorizationUrl }).pipe(Effect.ignore)
        }),
      )

      const code = yield* Effect.promise(() => callbackPromise)

      const storedState = yield* auth.getOAuthState(mcpName)
      if (storedState !== result.oauthState) {
        yield* auth.clearOAuthState(mcpName)
        throw new Error("OAuth state mismatch - potential CSRF attack")
      }
      yield* auth.clearOAuthState(mcpName)
      return yield* finishAuth(mcpName, code)
    })

    const finishAuth = Effect.fn("MCP.finishAuth")(function* (mcpName: string, authorizationCode: string) {
      const transport = pendingOAuthTransports.get(mcpName)
      if (!transport) throw new Error(`No pending OAuth flow for MCP server: ${mcpName}`)

      const result = yield* Effect.tryPromise({
        try: () => transport.finishAuth(authorizationCode).then(() => true as const),
        catch: (error) => {
          log.error("failed to finish oauth", { mcpName, error })
          return error
        },
      }).pipe(Effect.option)

      if (Option.isNone(result)) {
        return { status: "failed", error: "OAuth completion failed" } as Status
      }

      yield* auth.clearCodeVerifier(mcpName)
      pendingOAuthTransports.delete(mcpName)

      const mcpConfig = yield* getMcpConfig(mcpName)
      if (!mcpConfig) return { status: "failed", error: "MCP config not found after auth" } as Status

      return yield* createAndStore(mcpName, mcpConfig)
    })

    const removeAuth = Effect.fn("MCP.removeAuth")(function* (mcpName: string) {
      yield* auth.remove(mcpName)
      McpOAuthCallback.cancelPending(mcpName)
      pendingOAuthTransports.delete(mcpName)
      log.info("removed oauth credentials", { mcpName })
    })

    const supportsOAuth = Effect.fn("MCP.supportsOAuth")(function* (mcpName: string) {
      const mcpConfig = yield* getMcpConfig(mcpName)
      if (!mcpConfig) return false
      return mcpConfig.type === "remote" && mcpConfig.oauth !== false
    })

    const hasStoredTokens = Effect.fn("MCP.hasStoredTokens")(function* (mcpName: string) {
      const entry = yield* auth.get(mcpName)
      return !!entry?.tokens
    })

    const getAuthStatus = Effect.fn("MCP.getAuthStatus")(function* (mcpName: string) {
      const entry = yield* auth.get(mcpName)
      if (!entry?.tokens) return "not_authenticated" as AuthStatus
      const expired = yield* auth.isTokenExpired(mcpName)
      return (expired ? "expired" : "authenticated") as AuthStatus
    })

    return Service.of({
      prefixGeneration,
      healthSnapshot,
      observeToolsCapabilities,
      toolsCapabilitiesSnapshot,
      status,
      clients,
      tools,
      prompts,
      resources,
      add,
      connect,
      disconnect,
      getPrompt,
      readResource,
      startAuth,
      authenticate,
      finishAuth,
      removeAuth,
      supportsOAuth,
      hasStoredTokens,
      getAuthStatus,
    })
  }),
)

export type AuthStatus = "authenticated" | "expired" | "not_authenticated"

// --- Per-service runtime ---

export const defaultLayer = layer.pipe(
  Layer.provide(McpAuth.layer),
  Layer.provide(Bus.layer),
  Layer.provide(Config.defaultLayer),
  Layer.provide(CrossSpawnSpawner.defaultLayer),
  Layer.provide(AppFileSystem.defaultLayer),
)

export * as MCP from "."
