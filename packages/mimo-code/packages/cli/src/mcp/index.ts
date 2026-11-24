import { browserSnapshotAdmission, type BrowserSnapshotWitness } from "@/session/browser-snapshot-admission"
import { toolPresentationProgress } from "./tool-progress"
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
import { Deferred, Effect, Exit, Layer, Option, Context, Stream, ScopedCache, Semaphore } from "effect"
import { HostMcp } from "./host"
import { ManagedClient } from "./managed-client"
import { EffectBridge } from "@/effect"
import { InstanceState } from "@/effect"
import { ChildProcess, ChildProcessSpawner } from "effect/unstable/process"
import * as CrossSpawnSpawner from "@/effect/cross-spawn-spawner"
import { McpSampling } from "./sampling"
import { SessionID } from "@/session/schema"
import { codeReadResource } from "./code-activity"
import { sourceOutlineAdmission, type SourceOutlineWitness } from "@/session/source-outline-admission"
import { boundedOutlineSidecar } from "@/session/source-outline-custody"
import { McpElicitation } from "./elicitation"

const log = Log.create({ service: "mcp" })
const DEFAULT_TIMEOUT = 30_000
const RECALL_ADMISSION = "com.xiaomi.mimo/recall-admission"
export const managedFileObserver = Symbol("managed-file-observer")
type CodeActivityAttestation = { toolCallID: string; text: string; event: unknown }
const codeActivityAttestations = new WeakMap<object, CodeActivityAttestation>()
type OutlineAttestation = {toolCallID:string;text:string;sidecar:string;current:()=>boolean}
const outlineAttestations = new WeakMap<object, OutlineAttestation>()
type BrowserSnapshotAttestation = {toolCallID:string;text:string;current:()=>boolean}
const browserSnapshotAttestations = new WeakMap<object, BrowserSnapshotAttestation>()
type ManagedFileAttestation = {toolCallID:string; text:string; actionRef:string; fingerprint:string; operation:"write"|"edit";
  file:{path:string;mediaType:string;bytes:number;lines:number;newline:"lf"|"crlf"|"cr"}; current:()=>boolean}
const managedFileAttestations = new WeakMap<object, ManagedFileAttestation>()
export function takeManagedFileAttestation(value:unknown):ManagedFileAttestation|undefined {
  if (!value || typeof value!=="object") return
  const item=managedFileAttestations.get(value); managedFileAttestations.delete(value); return item
}
export function takeBrowserSnapshotAttestation(value:unknown):BrowserSnapshotAttestation|undefined {
  if (!value || typeof value!=='object') return
  const item=browserSnapshotAttestations.get(value);browserSnapshotAttestations.delete(value);return item
}
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

/** How long `MCP.tools()` waits for startup connections before returning what is ready. */
const FIRST_TURN_WAIT = 3_000

/**
 * Host entries fully replace the user record for that name. When the host omits
 * sampling, default to ask rather than silently inheriting the covered user field.
 * Exported for unit tests that pin the same-snapshot ownership binding.
 */
export function hostEffectiveSampling(mcp: ConfigMCP.Info | undefined, hostOwned: boolean) {
  if (!mcp) return undefined
  if (hostOwned) return mcp.sampling ?? ("ask" as const)
  return mcp.sampling
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

type MCPClient = ManagedClient

export const TURN_LIFECYCLE_CAPABILITY = "com.xiaomi.mimo/turn-lifecycle"
export const TURN_LIFECYCLE_NOTIFICATION = `notifications/${TURN_LIFECYCLE_CAPABILITY}`
export const TURN_LIFECYCLE_VERSION = 1
export const TURN_LIFECYCLE_NOTIFICATION_TIMEOUT = 1_000
// A send that has already outlived the per-turn budget can never be useful to wait
// on again, so later turns abandon it instead of queueing behind it forever.
export const TURN_LIFECYCLE_STUCK_TIMEOUT = TURN_LIFECYCLE_NOTIFICATION_TIMEOUT

/**
 * Capabilities Open Clank declares in `initialize`. Exported so tests assert on the
 * SAME object the client is constructed with rather than a copy that could drift.
 */
export const CLIENT_OPTIONS = {
  capabilities: {
    // Declared because we register a `sampling/createMessage` request handler
    // below; the SDK's assertRequestHandlerCapability refuses the registration
    // without it. Intentionally an empty object: `sampling.tools` and
    // `sampling.context` are NOT implemented, and declaring them would invite
    // servers to send `tools`/`includeContext` payloads we would have to reject.
    sampling: {},
    elicitation: { form: {} },
    experimental: {
      [TURN_LIFECYCLE_CAPABILITY]: { version: TURN_LIFECYCLE_VERSION },
    },
  },
}

interface PendingTurnLifecycleNotification {
  readonly promise: Promise<void>
  readonly waiters: Set<() => void>
  readonly startedAt: number
}

const pendingTurnLifecycleNotifications = new WeakMap<Client, PendingTurnLifecycleNotification>()

export interface TurnContext {
  [key: string]: unknown
  sessionId: string
  turnId: string
  actorId?: string
}

// The same context object is used throughout a runLoop and its finalizer.
// Keep every generation used by that turn, including between model/tool calls.
const turnClients = new WeakMap<TurnContext, Map<MCPClient, { name: string; release: () => void }>>()

export type TurnStatus = "completed" | "cancelled" | "error"

function supportsTurnLifecycle(client: Client) {
  const capability = client.getServerCapabilities()?.experimental?.[TURN_LIFECYCLE_CAPABILITY]
  return (
    typeof capability === "object" &&
    capability !== null &&
    "version" in capability &&
    capability.version === TURN_LIFECYCLE_VERSION
  )
}

function startTurnLifecycleNotification(client: Client, context: TurnContext, status: TurnStatus) {
  if (pendingTurnLifecycleNotifications.has(client)) return undefined
  const promise = Promise.resolve().then(() =>
    client.notification({
      method: TURN_LIFECYCLE_NOTIFICATION,
      params: { ...context, status },
    } as Parameters<Client["notification"]>[0]),
  )
  const notification: PendingTurnLifecycleNotification = { promise, waiters: new Set(), startedAt: Date.now() }
  pendingTurnLifecycleNotifications.set(client, notification)
  const clear = () => {
    if (pendingTurnLifecycleNotifications.get(client) === notification) {
      pendingTurnLifecycleNotifications.delete(client)
    }
    const waiters = [...notification.waiters]
    notification.waiters.clear()
    for (const waiter of waiters) waiter()
  }
  // Attached at creation so an orphaned send's eventual rejection is always swallowed.
  void promise.then(clear, clear)
  return notification
}

// A send that outlives the per-turn budget is treated as stuck: drop it from the
// pending map so the next turn sends immediately instead of paying the timeout
// forever. The orphaned promise is never awaited again; its settlement still runs
// `clear`, which no-ops because the map entry has been replaced.
function releaseStuckTurnLifecycleNotification(
  client: Client,
  notification: PendingTurnLifecycleNotification,
  clientName: string,
) {
  if (pendingTurnLifecycleNotifications.get(client) !== notification) return
  pendingTurnLifecycleNotifications.delete(client)
  log.warn("abandoning stuck MCP turn lifecycle notification", {
    clientName,
    elapsed: Date.now() - notification.startedAt,
  })
  const waiters = [...notification.waiters]
  notification.waiters.clear()
  for (const waiter of waiters) waiter()
}

function waitForTurnLifecycleNotification(client: Client, notification: PendingTurnLifecycleNotification) {
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
        const onAbort = () =>
          finish(() => reject(signal.reason instanceof Error ? signal.reason : new Error("Lifecycle wait aborted")))

        notification.waiters.add(onSettled)
        signal.addEventListener("abort", onAbort, { once: true })

        if (signal.aborted) onAbort()
        else if (pendingTurnLifecycleNotifications.get(client) !== notification) onSettled()
      }),
    catch: (error) => (error instanceof Error ? error : new Error(String(error))),
  })
}

function sendTurnLifecycleNotification(client: Client, context: TurnContext, status: TurnStatus, clientName: string) {
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

export function notifyTurnLifecycle(clients: Record<string, Client>, context: TurnContext, status: TurnStatus) {
  const retained = turnClients.get(context)
  return Effect.forEach(
    retained ? [...retained].map(([client, entry]) => [entry.name, client] as const) : Object.entries(clients),
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
  ).pipe(Effect.ensuring(releaseTurnClients(context)))
}

/** Also used by the outer run finalizer if post-session bookkeeping fails. */
export function releaseTurnClients(context: TurnContext) {
  return Effect.sync(() => {
    const retained = turnClients.get(context)
    turnClients.delete(context)
    for (const entry of retained?.values() ?? []) entry.release()
  })
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

// Store transports for OAuth servers to allow finishing auth.
// Transport + host revision form one pending-attempt record (R007).
type PendingOAuthAttempt = {
  transport: TransportWithAuth
  hostRevision?: string
  fromHost: boolean
}
const pendingOAuthTransports = new Map<string, PendingOAuthAttempt>()

/**
 * Publish a pending OAuth attempt only when it still matches the live host
 * generation. A current host attempt always replaces a stale one; a stale late
 * Unauthorized never registers over (or into) a slot reserved for the live host.
 */
function publishPendingOAuthAttempt(
  key: string,
  attempt: PendingOAuthAttempt,
): void {
  const currentRev = HostMcp.revisionOf(key)
  if (attempt.fromHost && attempt.hostRevision != null) {
    if (attempt.hostRevision !== currentRev) return
    pendingOAuthTransports.set(key, attempt)
    return
  }
  const existing = pendingOAuthTransports.get(key)
  const existingIsCurrentHost =
    !!existing?.fromHost && existing.hostRevision != null && existing.hostRevision === currentRev
  if (!existingIsCurrentHost) pendingOAuthTransports.set(key, attempt)
}

type TransportWithAuth = StreamableHTTPClientTransport | SSEClientTransport

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
export function convertMcpTool(mcpTool: MCPToolDef, client: MCPClient, timeout?: number, context?: TurnContext, admit?: (toolCallID?: string, operation?: "status"|"discover"|"download"|"cancel") => Promise<string | undefined>, managedTheme = false, observe?: (duration: number, category: "tool_error" | "transport" | "cancelled" | null) => void, managedKernel = false, managedCode = false, managedBackgroundName?: string, managedTaskName?: string, managedMediaName?: string, managedFrameName?: string, managedSharedTextName?: string, browserSnapshotCurrent?:()=>boolean, managedFilesName?: string, managedOrdinaryFilesName?: string): Tool {
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
      if (managedOrdinaryFilesName) {
        const { ordinaryFilesMutate } = await import("@/mcp/files-mutate")
        const tool = managedOrdinaryFilesName.endsWith("_write_file") ? "write_file" : "edit_file"
        const normalized = ordinaryFilesMutate(args, tool)
        if (!normalized || !context || !supportsTurnLifecycle(client) || typeof context.backgroundMessageID !== "string" ||
            typeof options.toolCallId !== "string" || !options.toolCallId || options.abortSignal?.aborted) throw new Error("File mutation unavailable")
        const { managedHostCall, ManagedProvider } = await import("@/acp/managed-provider")
        const identity = { sessionID:context.sessionID as string, rootOperationID:context.rootOperationID as string,
          actorID:context.actorID as string, executionRevision:context.executionRevision as number,
          innerTurnID:context.innerTurnID as string, messageID:context.backgroundMessageID,
          toolCallID:options.toolCallId, toolName:managedOrdinaryFilesName }
        if (!await ManagedProvider.claimFreshTaskCall(identity)) throw new Error("File mutation unavailable for this invocation")
        const privateContext = options.experimental_context as Record<PropertyKey, unknown> | undefined
        const observer = privateContext?.[managedFileObserver] as { beginManaged?: (fact: unknown, actionID:string)=>Promise<void>; completeManaged?: (actionID:string, actionRef:string)=>Promise<void>; currentManaged?:()=>boolean } | undefined
        if (!observer?.beginManaged || !observer.completeManaged || !observer.currentManaged) throw new Error("File capture membership unavailable")
        let result
        try { result = await managedHostCall("_openclank/session/v1/files/mutate", { ...identity, ...normalized, phase:"prepare" }) }
        catch { result = { state:"unknown" as const } }
        if (result.state !== "prepared") return { isError:true, content:[{type:"text" as const,text:JSON.stringify({error:result.state === "unknown" ? "File mutation outcome unknown; do not retry automatically." : "File mutation refused.",exit_code:1})}] }
        const actionID = result.actionID
        try {
          await observer.beginManaged({source:result.source,capture:result.capture},actionID)
          if (options.abortSignal?.aborted) throw new Error("File mutation unavailable")
          try { result = await managedHostCall("_openclank/session/v1/files/mutate", { ...identity, ...normalized, phase:"apply" }) }
          catch { result = await managedHostCall("_openclank/session/v1/files/mutate", { ...identity, ...normalized, phase:"status" }).catch(() => ({state:"unknown" as const})) }
          if (result.state !== "applied" || (result.action !== "write" && result.action !== "edit")) throw new Error("File mutation outcome unknown; do not retry automatically.")
          await observer.completeManaged(actionID,result.actionRef)
          const text=JSON.stringify({output:"File saved",exit_code:0,managed_action_ref:result.actionRef,fingerprint:result.fingerprint})
          const output={content:[{type:"text" as const,text}]}
          const current=observer.currentManaged
          managedFileAttestations.set(output,{toolCallID:options.toolCallId,text,actionRef:result.actionRef,
            fingerprint:result.fingerprint,operation:result.action,file:result.file,current:()=>!options.abortSignal?.aborted && current()})
          return output
        } catch {
          // Abort only the exact prepared original; the Host refuses this once issued.
          await managedHostCall("_openclank/session/v1/files/mutate", { ...identity, ...normalized, phase:"abort" }).catch(() => undefined)
          return { isError:true, content:[{type:"text" as const,text:JSON.stringify({error:"File mutation unavailable or outcome unknown; do not retry automatically.",exit_code:1})}] }
        }
      }
      if (managedFilesName && (!args || typeof args !== "object" || Array.isArray(args) ||
          !["move", "delete"].includes(String((args as Record<string, unknown>).action))))
        throw new Error("Managed file mutation supports only move and delete")
      if (managedFilesName && args && typeof args === "object" && !Array.isArray(args) &&
          ["move", "delete"].includes(String((args as Record<string, unknown>).action))) {
        const { filesMutate } = await import("@/mcp/files-mutate")
        const normalized = filesMutate(args)
        if (!normalized || !context || !supportsTurnLifecycle(client) || typeof context.backgroundMessageID !== "string" ||
            typeof options.toolCallId !== "string" || !options.toolCallId || options.abortSignal?.aborted) throw new Error("File mutation unavailable")
        const { managedHostCall, ManagedProvider } = await import("@/acp/managed-provider")
        const identity = { sessionID:context.sessionID as string, rootOperationID:context.rootOperationID as string,
          actorID:context.actorID as string, executionRevision:context.executionRevision as number,
          innerTurnID:context.innerTurnID as string, messageID:context.backgroundMessageID,
          toolCallID:options.toolCallId, toolName:managedFilesName }
        if (!await ManagedProvider.claimFreshTaskCall(identity)) throw new Error("File mutation unavailable for this invocation")
        let result
        try { result = await managedHostCall("_openclank/session/v1/files/mutate", { ...identity, ...normalized }) }
        catch { result = { state:"unknown" as const } }
        const output = result.state === "applied" && result.action === "move"
          ? { output:`Moved ${result.source} to ${result.destination}`, exit_code:0, source:result.source, path:result.destination }
          : result.state === "applied" && result.action === "trash"
            ? { output:`Moved ${result.entry.originalPath} to recoverable trash.`, exit_code:0, trash_id:result.entry.id,
                root_id:result.entry.rootID, original_path:result.entry.originalPath, trashed_path:result.entry.trashedPath,
                fingerprint:result.entry.fingerprint, recoverable:true }
            : { error:result.state === "unknown" ? "File mutation outcome unknown; do not retry automatically." : "File mutation refused.", exit_code:1 }
        return { content:[{type:"text" as const,text:JSON.stringify(output)}] }
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
      const progress = toolPresentationProgress(options.experimental_context)
      const finish = McpElicitation.beginCall(client, context?.sessionId, options.abortSignal)
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
          onprogress: progress.update,
          signal: options.abortSignal,
          timeout,
        },
        )
      } catch (error) {
        observe?.(Math.max(0, Date.now() - started), options.abortSignal?.aborted ? "cancelled" : "transport")
        throw error
      } finally {
        finish()
        await progress.drain()
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
      // Browser evidence belongs only to the admitted turn and owner-bound first-party client.
      const browserWitness=context ? (context as unknown as Record<PropertyKey,unknown>)[browserSnapshotAdmission] as BrowserSnapshotWitness | undefined : undefined
      const browserCurrent=browserSnapshotCurrent && browserWitness ? () => browserSnapshotCurrent() === true && browserWitness.current() === true : undefined
      if (browserCurrent && browserCurrent() === true && context && supportsTurnLifecycle(client) &&
          !result.isError && !options.abortSignal?.aborted && typeof options.toolCallId==='string' && options.toolCallId &&
          args && typeof args==='object' && !Array.isArray(args) && (args as Record<string,unknown>).action==='snapshot' &&
          result.structuredContent===undefined && Array.isArray(content) && content.length===1 &&
          content[0]?.type==='text' && typeof content[0].text==='string' && Buffer.byteLength(content[0].text,'utf8')<=51200)
        browserSnapshotAttestations.set(sanitized,{toolCallID:options.toolCallId,text:content[0].text,current:browserCurrent})
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

type AuthResult =
  | {
      kind: "connected"
      oauthState: string
      client: MCPClient
      /** Config snapshot that opened this connection; sampling must stay bound to it. */
      resolved: { mcp: ConfigMCP.Info; hostOwned: boolean }
      hostRevision?: string
    }
  | { kind: "redirect"; authorizationUrl: string; oauthState: string }

// --- Effect Service ---

interface State {
  host: Record<string, string>
  hostRetryAt: Record<string, number>
  retired: Set<MCPClient>
  refresh: Semaphore.Semaphore
  status: Record<string, Status>
  clients: Record<string, MCPClient>
  defs: Record<string, MCPToolDef[]>
  healthSerial: number
  health: Record<string, HealthObservation>
  healthTurns: Map<string, { candidates: Record<string, HealthCandidate>; signature: string; exec?: ExecCapabilities }>
  settled: Deferred.Deferred<void>
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
  readonly clients: (context?: TurnContext) => Effect.Effect<Record<string, MCPClient>>
  readonly tools: (context?: TurnContext, options?: { managedPrivateRootSessionID: string; admit: (toolName: string, kind: "tool" | "mcp_tool" | "memory_read", toolCallID?: string, operation?: "status"|"discover"|"download"|"cancel") => Promise<string | undefined> }) => Effect.Effect<Record<string, Tool>>
  /** Optional non-blocking connect snapshot for reminders/search diagnostics. Never awaits `settled`. */
  readonly readiness?: () => Effect.Effect<{
    configured: boolean
    servers: Array<{
      name: string
      status: Status["status"]
      toolCount?: number
      error?: string
      tools?: Array<{ name: string; description: string }>
    }>
  }>
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
    const createClient = () => new ManagedClient({ name: "openclank", version: InstallationVersion }, CLIENT_OPTIONS)

    type Transport = ObservingStdioTransport | StreamableHTTPClientTransport | SSEClientTransport

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
      identity?: { hostRevision?: string; fromHost: boolean },
    ) {
      // Prefer the caller's creation snapshot; only fall back to current HostMcp
      // when create() was invoked without one (e.g. refreshHost discovery).
      const hostRevisionAtCreate =
        identity?.hostRevision ?? (HostMcp.get()[key] ? HostMcp.revisionOf(key) : undefined)
      const fromHostAtCreate = identity?.fromHost ?? hostRevisionAtCreate != null
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
                // Generation-aware publish (R007): current host attempts replace
                // stale pending; stale late Unauthorized cannot register.
                publishPendingOAuthAttempt(key, {
                  transport,
                  hostRevision: hostRevisionAtCreate,
                  fromHost: fromHostAtCreate,
                })
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
        // childProcessEnv: MCP servers are third-party binaries running as the user.
        // Note for `opencode` configured as its own MCP server: that nested engine no longer
        // inherits the host's credentials or config content, and falls back to reading auth.json
        // and the config file from disk — which is what a plain CLI invocation does anyway.
        env: {
          ...childProcessEnv(),
          ...(cmd === "opencode" ? { BUN_BE_BUN: "1" } : {}),
          ...mcp.environment,
        },
      })
      transport.onerror = (error) => {
        log.info(`mcp transport error: ${error.message}`, { key })
      }
      transport.onStderr = (text) => {
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
          const exit = transport.exitSnapshot()
          const stderrTail = transport.stderrSnapshot().trim()
          // hostShutdown means close() tore the child down; that signal is our cleanup,
          // not the child's startup-failure cause.
          const natural = exit && !exit.hostShutdown ? exit : undefined
          log.error("local mcp startup failed", {
            key,
            command: ConfigMCP.redactCommand(mcp.command),
            cwd,
            error: msg,
            pid: exit?.pid ?? null,
            exitCode: natural?.exitCode ?? null,
            signalCode: natural?.signalCode ?? null,
            stderr: stderrTail || undefined,
          })
          const detail = [
            msg,
            natural?.exitCode != null ? `exit=${natural.exitCode}` : undefined,
            natural?.signalCode ? `signal=${natural.signalCode}` : undefined,
            stderrTail || undefined,
          ]
            .filter(Boolean)
            .join("; ")
          return Effect.succeed({ client: undefined, status: { status: "failed", error: detail } })
        }),
      )
    })

    const create = Effect.fn("MCP.create")(function* (
      key: string,
      mcp: ConfigMCP.Info,
      identity?: { hostRevision?: string; fromHost: boolean },
    ) {
      if (mcp.enabled === false) {
        log.info("mcp server disabled", { key })
        return DISABLED_RESULT
      }

      log.info("found", { key, type: mcp.type })

      const { client: mcpClient, status } =
        mcp.type === "remote"
          ? yield* connectRemote(key, mcp as ConfigMCP.Info & { type: "remote" }, identity)
          : yield* connectLocal(key, mcp as ConfigMCP.Info & { type: "local" })

      if (!mcpClient) {
        return { status } satisfies CreateResult
      }

      const discoveryStarted = Date.now()
      // Ownership stays with this acquire/use/release until create() returns:
      // interruption during tools/list must close the unpublished client.
      const listed = yield* Effect.acquireUseRelease(
        Effect.succeed(mcpClient),
        (client) => defs(key, client, mcp.timeout),
        (client, exit) =>
          Exit.isFailure(exit) ? Effect.tryPromise(() => client.close()).pipe(Effect.ignore) : Effect.void,
      )
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

    function watch(
      s: State,
      name: string,
      client: MCPClient,
      bridge: EffectBridge.Shape,
      timeout?: number,
      sampling?: ConfigMCP.Info["sampling"],
    ) {
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
      // Bind the effective policy for this generation so host deny is not
      // re-resolved from user config alone at sampling time.
      McpSampling.serve(name, client, bridge, undefined, undefined, sampling)
      McpElicitation.serve(name, client, bridge)
    }

    const state = yield* InstanceState.make<State>(
      Effect.fn("MCP.state")(function* () {
        const cfg = yield* cfgSvc.get()
        // Snapshot config and per-name revisions in one sync block so identity
        // always pairs the same generation with the same config object (R009).
        const host = HostMcp.get()
        const hostRevisions = new Map(
          Object.keys(host).map((name) => [name, HostMcp.revisionOf(name)] as const),
        )
        const config = { ...cfg.mcp, ...host }
        const s: State = {
          host: Object.fromEntries(Object.entries(host).map(([key, value]) => [key, JSON.stringify(value)])),
          hostRetryAt: {},
          retired: new Set(),
          refresh: Semaphore.makeUnsafe(1),
          status: {},
          clients: {},
          defs: {},
          healthSerial: 0, health: {}, healthTurns: new Map(),
          settled: yield* Deferred.make<void>(),
        }

        // Registered before the connection fiber is forked: finalizers run in
        // reverse order, so in-flight connections are interrupted first and this
        // then closes whatever they managed to store.
        yield* Effect.addFinalizer(() =>
          Effect.gen(function* () {
            const memory = yield* Effect.promise(() => import("@/memory/mcp-client"))
            for (const [name, client] of Object.entries(s.clients)) {
              if (name.startsWith("lifetools")) memory.unregisterManagedMcpClient(name, client)
            }
            yield* Effect.forEach(
              [...Object.values(s.clients), ...s.retired],
              (client) =>
                Effect.gen(function* () {
                  const pid = client.transport instanceof ObservingStdioTransport ? client.transport.pid : null
                  if (typeof pid === "number") {
                    const pids = yield* descendants(pid)
                    for (const dpid of pids) {
                      try {
                        process.kill(dpid, "SIGTERM")
                      } catch {}
                    }
                  }
                  yield* McpSampling.cancelAll(client)
                  yield* Effect.tryPromise(() => client.close()).pipe(Effect.ignore)
                }),
              { concurrency: "unbounded" },
            )
            pendingOAuthTransports.clear()
          }),
        )

        for (const [key, mcp] of Object.entries(config)) {
          if (isMcpConfigured(mcp) && mcp.enabled !== false) s.status[key] = { status: "pending" }
        }

        // Connect in the background so state initialization (and therefore the
        // first prompt) never waits on the slowest server. `settled` completes
        // once every attempt has finished; `tools()` waits on it with a bound.
        const scope = yield* Effect.scope
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

              const fromHost = key in host
              const hostRevision = hostRevisions.get(key)
              const result = yield* create(key, mcp, { fromHost, hostRevision }).pipe(Effect.catch(() => Effect.void))
              if (!result) return

              if (result.mcpClient) {
                // Status is written only on successful admit/commit (R013).
                // Pre-writing connected would survive a reject-stale discard.
                // Uninterruptible so a connected client is never orphaned
                // between creation and registration.
                yield* Effect.uninterruptible(
                  storeClient(
                    s,
                    key,
                    result.mcpClient,
                    result.defs!,
                    mcp.timeout,
                    hostEffectiveSampling(mcp, fromHost),
                    { fromHost, hostRevision },
                    result.discoveryMs,
                  ),
                )
                return
              }

              s.status[key] = result.status
              if (fromHost && result.status.status === "failed") s.hostRetryAt[key] = Date.now() + 5000
            }).pipe(
              // A rejected or errored attempt writes no status; drop the
              // placeholder so it reads as before (absent) rather than pending forever.
              Effect.ensuring(
                Effect.sync(() => {
                  if (s.status[key]?.status === "pending") delete s.status[key]
                }),
              ),
            ),
          { concurrency: "unbounded", discard: true },
        ).pipe(
          Effect.ensuring(Deferred.succeed(s.settled, undefined)),
          Effect.forkIn(scope, { startImmediately: true }),
        )

        return s
      }),
    )

    // Every accessor except `tools()` observes the fully settled state, so
    // connect/disconnect/add never race the background startup connections.
    const getState = Effect.fnUntraced(function* () {
      const s = yield* InstanceState.get(state)
      yield* Deferred.await(s.settled)
      return s
    })

    function closeClient(s: State, name: string) {
      const client = s.clients[name]
      delete s.defs[name]
      delete s.health[name]
      if (!client) return Effect.void
      if (name.startsWith("lifetools")) {
        const unregister = Effect.promise(() => import("@/memory/mcp-client")).pipe(Effect.tap((memory) => Effect.sync(() => memory.unregisterManagedMcpClient(name, client))))
        return unregister.pipe(Effect.andThen(McpSampling.cancelAll(client)), Effect.andThen(Effect.tryPromise(() => client.close()).pipe(Effect.ignore)))
      }
      // Interrupt sampling still running for this client first: once the
      // transport is gone its response can never be delivered, so the fiber
      // would otherwise keep a model call alive with nowhere to send the result.
      return McpSampling.cancelAll(client).pipe(
        Effect.andThen(Effect.tryPromise(() => client.close()).pipe(Effect.ignore)),
      )
    }

    // Refresh only embedder-owned servers. Requests and turn bindings retain
    // their original connection until completion, then retired clients close.
    const refreshHost = Effect.fn("MCP.refreshHost")(function* (s: State) {
      yield* s.refresh.withPermits(1)(
        Effect.gen(function* () {
          const host = HostMcp.get()
          const revisions = new Map(Object.keys(host).map((name) => [name, HostMcp.revisionOf(name)] as const))
          const cfg = yield* cfgSvc.get()
          const bridge = yield* EffectBridge.make()
          const memory = yield* Effect.promise(() => import("@/memory/mcp-client"))
          // hostRetryAt keeps failed fallbacks reachable after an override is removed.
          for (const name of new Set([...Object.keys(s.host), ...Object.keys(host), ...Object.keys(s.hostRetryAt)])) {
            const revision = JSON.stringify(host[name])
            if (s.host[name] === revision && (!s.hostRetryAt[name] || Date.now() < s.hostRetryAt[name])) continue
            const mcp = host[name] ?? cfg.mcp?.[name]
            const result =
              !mcp || !isMcpConfigured(mcp) || mcp.enabled === false
                ? { status: { status: "disabled" as const }, mcpClient: undefined, defs: undefined }
                : yield* create(name, mcp, { fromHost: name in host, hostRevision: revisions.get(name) })
            // Host ownership may change during create or dependency acquisition.
            // Discard only this attempt before touching the current generation.
            if (HostMcp.revisionOf(name) !== revisions.get(name)) {
              if (result.mcpClient) yield* releaseMcpClient(result.mcpClient)
              continue
            }
            const previous = s.clients[name]
            if (previous) {
              if (name.startsWith("lifetools")) memory.unregisterManagedMcpClient(name, previous)
              s.retired.add(previous)
              previous.retire(() =>
                bridge.promise(
                  Effect.gen(function* () {
                    // Kill descendants while the parent PID is still valid; transport
                    // close only reaps the direct child.
                    const pid = previous.transport instanceof ObservingStdioTransport ? previous.transport.pid : null
                    if (typeof pid === "number") {
                      const pids = yield* descendants(pid)
                      for (const dpid of pids) {
                        try {
                          process.kill(dpid, "SIGTERM")
                        } catch {}
                      }
                    }
                    yield* McpSampling.cancelAll(previous)
                    yield* Effect.tryPromise(() => previous.close())
                  }).pipe(
                    Effect.ignore,
                    Effect.ensuring(
                      Effect.sync(() => {
                        s.retired.delete(previous)
                      }),
                    ),
                  ),
                ),
              )
            }
            delete s.clients[name]
            delete s.defs[name]
            delete s.health[name]
            s.status[name] = result.status
            if (revision == null) delete s.host[name]
            else s.host[name] = revision
            if (result.status.status === "failed") s.hostRetryAt[name] = Date.now() + 5000
            else delete s.hostRetryAt[name]
            if (result.mcpClient) {
              s.clients[name] = result.mcpClient
              s.defs[name] = result.defs!
              s.health[name] = { instanceID:crypto.randomUUID(), generation: ++s.healthSerial, revision: 0, discoveryFailed: false, observedAt: Date.now(), discoveryMs: result.discoveryMs ?? null, lastCallAt: null, lastCallMs: null, lastCallCategory: null, lastCallTurn: null }
              if (name.startsWith("lifetools")) memory.registerManagedMcpClient(name, result.mcpClient)
              watch(s, name, result.mcpClient, bridge, mcp?.timeout, hostEffectiveSampling(mcp, name in host))
            }
          }
        }),
      )
    })

    function releaseMcpClient(client: MCPClient | undefined) {
      if (!client) return Effect.void
      return McpSampling.cancelAll(client).pipe(
        Effect.andThen(Effect.tryPromise(() => client.close()).pipe(Effect.ignore)),
      )
    }

    const storeClient = Effect.fnUntraced(function* (
      s: State,
      name: string,
      client: MCPClient,
      listed: MCPToolDef[],
      timeout?: number,
      sampling?: ConfigMCP.Info["sampling"],
      opts?: { fromHost?: boolean; hostRevision?: string },
      discoveryMs?: number,
    ) {
      const admit = () => {
        const hostNow = HostMcp.get()[name]
        if (opts?.fromHost) {
          if (!hostNow || hostNow.enabled === false) return "reject-disabled" as const
          if (opts.hostRevision != null && opts.hostRevision !== HostMcp.revisionOf(name)) {
            return "reject-stale" as const
          }
          return "ok" as const
        }
        if (hostNow != null) return "reject-owned" as const
        return "ok" as const
      }
      const discardAttempt = (fallback: Status) =>
        releaseMcpClient(client).pipe(Effect.as(s.status[name] ?? fallback))

      // Resolve every async dependency before the final generation check. From
      // that check through commit and memory registration there is no yield.
      const staleFallback: Status = { status: "disabled" }
      const bridge = yield* EffectBridge.make()
      const memory = name.startsWith("lifetools")
        ? yield* Effect.promise(() => import("@/memory/mcp-client"))
        : undefined
      const finalAdmission = admit()
      if (finalAdmission !== "ok") {
        return yield* discardAttempt(finalAdmission === "reject-stale" ? staleFallback : ({ status: "disabled" } as Status))
      }
      const previous = s.clients[name]
      s.status[name] = { status: "connected" }
      s.clients[name] = client
      s.defs[name] = listed
      s.health[name] = { instanceID:crypto.randomUUID(), generation: ++s.healthSerial, revision: 0, discoveryFailed: false, observedAt: Date.now(), discoveryMs: discoveryMs ?? null, lastCallAt: null, lastCallMs: null, lastCallCategory: null, lastCallTurn: null }
      memory?.registerManagedMcpClient(name, client)
      delete s.hostRetryAt[name]
      watch(s, name, client, bridge, timeout, sampling)
      if (previous && previous !== client) {
        s.retired.add(previous)
        previous.retire(() => bridge.promise(
          Effect.gen(function* () {
            const pid = previous.transport instanceof ObservingStdioTransport ? previous.transport.pid : null
            if (typeof pid === "number") {
              for (const dpid of yield* descendants(pid)) {
                try { process.kill(dpid, "SIGTERM") } catch {}
              }
            }
            yield* releaseMcpClient(previous)
          }).pipe(Effect.ignore, Effect.ensuring(Effect.sync(() => { s.retired.delete(previous) }))),
        ))
      }
      return s.status[name]
    })

    const observeToolsCapabilities = Effect.fn("MCP.observeToolsCapabilities")(function* (request: HealthIdentity, value: ExecCapabilities) {
      const key = healthTurnKey(request)
      if (!key) return
      const cached = yield* ScopedCache.getSuccess(state.cache, yield* InstanceState.directory)
      if (Option.isNone(cached)) return
      const ready = yield* Deferred.poll(cached.value)
      if (Option.isNone(ready)) return
      const s = yield* ready.value
      const turn = s.healthTurns.get(key)
      if (turn && turn.signature === healthSignature(s)) turn.exec = value
    })
    const toolsCapabilitiesSnapshot = Effect.fn("MCP.toolsCapabilitiesSnapshot")(function* (request: HealthIdentity) {
      const key = healthTurnKey(request)
      if (!key) return { status: "unavailable" as const, exec: null }
      const cached = yield* ScopedCache.getSuccess(state.cache, yield* InstanceState.directory)
      if (Option.isNone(cached)) return { status: "unavailable" as const, exec: null }
      const ready = yield* Deferred.poll(cached.value)
      if (Option.isNone(ready)) return { status: "unavailable" as const, exec: null }
      const s = yield* ready.value
      const turn = s.healthTurns.get(key)
      if (!turn?.exec || turn.signature !== healthSignature(s)) return { status: "unavailable" as const, exec: null }
      return { status: "observed" as const, exec: turn.exec }
    })

    const prefixGeneration: NonNullable<Interface["prefixGeneration"]> = Effect.fn("MCP.prefixGeneration")(function* (request) {
      const key = healthTurnKey(request)
      if (!key) return undefined
      const cached = yield* ScopedCache.getSuccess(state.cache, yield* InstanceState.directory)
      if (Option.isNone(cached)) return undefined
      const ready = yield* Deferred.poll(cached.value)
      if (Option.isNone(ready)) return undefined
      const s = yield* ready.value, turn = s.healthTurns.get(key)
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
      const ready = yield* Deferred.poll(cached.value)
      if (Option.isNone(ready)) return unavailableHealth()
      const s = yield* ready.value
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
      const s = yield* getState()
      yield* refreshHost(s)

      const cfg = yield* cfgSvc.get()
      const config = { ...cfg.mcp, ...HostMcp.get() }
      const result: Record<string, Status> = {}

      for (const [key, mcp] of Object.entries(config)) {
        if (!isMcpConfigured(mcp)) continue
        result[key] = s.status[key] ?? { status: "disabled" }
      }

      return result
    })

    const clients = Effect.fn("MCP.clients")(function* (context?: TurnContext) {
      if (context)
        return Object.fromEntries([...(turnClients.get(context) ?? [])].map(([client, entry]) => [entry.name, client]))
      const s = yield* getState()
      yield* refreshHost(s)
      return s.clients
    })

    const createAndStore = Effect.fn("MCP.createAndStore")(function* (
      name: string,
      mcp: ConfigMCP.Info,
      sampling?: ConfigMCP.Info["sampling"],
      opts?: { fromHost?: boolean; hostRevision?: string },
    ) {
      // Prefer revision captured at the config-resolution boundary (R006).
      const hostRevision = opts?.hostRevision ?? (HostMcp.get()[name] ? HostMcp.revisionOf(name) : undefined)
      const s = yield* getState()
      const result = yield* create(name, mcp, {
        fromHost: opts?.fromHost === true,
        hostRevision,
      })

      if (!result.mcpClient) {
        // Failure completion uses the same validity rule as success: a host-sourced
        // attempt that is no longer valid must not mutate the current connection.
        const hostNow = HostMcp.get()[name]
        if (opts?.fromHost) {
          const stillValidHost = !!hostNow
            && hostNow.enabled !== false
            && (hostRevision == null || hostRevision === HostMcp.revisionOf(name))
          if (!stillValidHost) {
            return s.status[name] ?? { status: "disabled" as const }
          }
          s.status[name] = result.status
          return result.status
        }
        if (hostNow == null) {
          s.status[name] = result.status
          // Close only the client we captured; do not delete a replacement by name.
          const victim = s.clients[name]
          yield* releaseMcpClient(victim)
          if (victim && s.clients[name] === victim) {
            delete s.clients[name]
            delete s.defs[name]
          }
        }
        return hostNow == null ? result.status : (s.status[name] ?? { status: "connected" as const })
      }

      // `sampling` must be computed with the same config snapshot as `mcp`.
      return yield* storeClient(s, name, result.mcpClient, result.defs!, mcp.timeout, sampling ?? mcp.sampling, {
        ...opts,
        hostRevision,
      }, result.discoveryMs)
    })

    const add = Effect.fn("MCP.add")(function* (name: string, mcp: ConfigMCP.Info) {
      // HostMcp owns readiness and connection shape for host-projected names.
      // User/SDK add must not replace or re-enable a host-owned entry.
      if (HostMcp.get()[name] != null) {
        log.error("MCP name is host-owned; add refused", { name })
        const s = yield* getState()
        // Return the real current status — do not fabricate "disabled".
        return { status: { ...s.status, [name]: s.status[name] ?? { status: "disabled" as const } } }
      }
      yield* createAndStore(name, mcp, undefined, { fromHost: false })
      const s = yield* getState()
      return { status: s.status }
    })

    const connect = Effect.fn("MCP.connect")(function* (name: string) {
      const resolved = yield* getMcpConfig(name)
      if (!resolved) {
        log.error("MCP config not found or invalid", { name })
        return
      }
      // Explicit connect must not lift a host closed gate.
      if (resolved.hostOwned && resolved.mcp.enabled === false) {
        log.error("MCP is host-owned and disabled; connect refused", { name })
        return
      }
      // Bind config + ownership + revision at the resolution boundary (R006).
      const hostRevision = resolved.hostOwned ? HostMcp.revisionOf(name) : undefined
      yield* createAndStore(
        name,
        { ...resolved.mcp, enabled: true },
        hostEffectiveSampling(resolved.mcp, resolved.hostOwned),
        { fromHost: resolved.hostOwned, hostRevision },
      )
    })

    const disconnect = Effect.fn("MCP.disconnect")(function* (name: string) {
      const s = yield* getState()
      yield* closeClient(s, name)
      delete s.clients[name]
      delete s.health[name]
      // Explicit disconnect supersedes any host-refresh cooldown; do not auto-revive.
      delete s.hostRetryAt[name]
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
      // Bounded wait: a slow or hung server must not hold the turn hostage. Servers
      // still connecting are left out of this call and picked up by a later one
      // (the prompt loop resolves tools every step and announces the delta).
      const settled = yield* Deferred.await(s.settled).pipe(
        Effect.timeoutOption(FIRST_TURN_WAIT),
        Effect.map(Option.isSome),
      )
      if (!settled) {
        log.info("MCP servers still connecting; continuing without them", {
          pending: Object.entries(s.status)
            .filter(([, status]) => status.status === "pending")
            .map(([name]) => name),
        })
      }
      yield* refreshHost(s)

      const cfg = yield* cfgSvc.get()
      const config = { ...cfg.mcp, ...HostMcp.get() }
      const defaultTimeout = cfg.experimental?.mcp_timeout

      const observedTurn = options ? healthTurnKey(context as Partial<HealthIdentity>) : undefined
      const candidates: Record<string, HealthCandidate> = {}
      const wrapperOwners: Record<string, string> = {}
      let candidateMissing = false
      const connectedClients = Object.entries(s.clients).filter(
        ([clientName]) => s.status[clientName]?.status === "connected",
      )

      for (const [clientName, client] of connectedClients) {
            const isPrivate = options && client === privateClient
            if (options && ((isPrivate && !supportsTurnLifecycle(client)) || (!isPrivate && clientName.startsWith("lifetools")))) continue
            const initialObservation = s.health[clientName]
            if (initialObservation) candidates[clientName] = { generation: initialObservation.generation, revision: initialObservation.revision, count: 0 }
            else candidateMissing = true
            const mcpConfig = config[clientName]
            const entry = mcpConfig && isMcpConfigured(mcpConfig) ? mcpConfig : undefined

            const listed = s.defs[clientName]
            if (!listed) {
              log.warn("missing cached tools for connected server", { clientName })
              continue
            }
            if (context && listed.length) {
              const retained = turnClients.get(context) ?? new Map<MCPClient, { name: string; release: () => void }>()
              if (!retained.has(client)) retained.set(client, { name: clientName, release: client.retain() })
              turnClients.set(context, retained)
            }

            const timeout = entry?.timeout ?? defaultTimeout
            for (const mcpTool of listed) {
              if (options && !isPrivate && (clientName.includes("__") || mcpTool.name.includes("__"))) continue
              if (isPrivate && ["search", "manage_memory", "recall_memory"].includes(mcpTool.name)) continue
              const admittedName = isPrivate && mcpTool.name === "python_kernel" ? "python" : isPrivate ? mcpTool.name : `mcp__${clientName}__${mcpTool.name}`
              const admit = options ? (toolCallID?: string, operation?: "status"|"discover"|"download"|"cancel") => options.admit(admittedName, isPrivate ? "tool" : "mcp_tool", (isPrivate && ["search_code", "manage_bg_jobs", "manage_tasks", "media_job", "capture_desktop", "shared_text", "manage_files", "write_file", "edit_file"].includes(mcpTool.name)) ? toolCallID : undefined, isPrivate && mcpTool.name === "theme_emoji_assets" ? operation : undefined) : undefined
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
                isPrivate && mcpTool.name === "manage_tasks" ? wrapperName : undefined, isPrivate && mcpTool.name === "media_job" ? wrapperName : undefined, isPrivate && mcpTool.name === "capture_desktop" ? wrapperName : undefined, isPrivate && mcpTool.name === "shared_text" ? wrapperName : undefined, isPrivate && mcpTool.name === "browser_observe" ? () => s.clients[clientName] === client && client === privateClient : undefined,
                isPrivate && mcpTool.name === "manage_files" ? wrapperName : undefined,
                isPrivate && (mcpTool.name === "write_file" || mcpTool.name === "edit_file") ? wrapperName : undefined)
            }
      }
      const key = observedTurn
      if (key) {
        s.healthTurns.delete(key)
        if (!candidateMissing) s.healthTurns.set(key, { candidates, signature: healthSignature(s) })
        while (s.healthTurns.size > 32) s.healthTurns.delete(s.healthTurns.keys().next().value!)
      }
      return result
    })

    // Bounded wait only on tools(); readiness is the unbound sibling used by
    // entry/server reminders and mcp_tool_search miss diagnostics. Never awaits settled.
    const readiness = Effect.fn("MCP.readiness")(function* () {
      const s = yield* InstanceState.get(state)
      const cfg = yield* cfgSvc.get()
      const config = { ...cfg.mcp, ...HostMcp.get() }
      const configuredKeys = Object.entries(config)
        .filter(([, mcp]) => isMcpConfigured(mcp))
        .map(([key]) => key)
      const servers = configuredKeys.map((name) => {
        const current = s.status[name] ?? { status: "disabled" as const }
        const listed = s.defs[name] ?? []
        const tools =
          current.status === "connected"
            ? listed.map((mcpTool) => ({
                name: sanitize(name) + "_" + sanitize(mcpTool.name),
                description: mcpTool.description ?? "",
              }))
            : undefined
        return {
          name,
          status: current.status,
          ...(current.status === "failed" || current.status === "needs_client_registration"
            ? { error: current.error }
            : {}),
          ...(current.status === "connected" ? { toolCount: listed.length } : {}),
          ...(tools ? { tools } : {}),
        }
      })
      return { configured: configuredKeys.length > 0, servers }
    })

    function collectFromConnected<T extends { name: string }>(
      s: State,
      listFn: (c: Client) => Promise<T[]>,
      label: string,
    ) {
      const connected = Object.entries(s.clients).filter(([name]) => s.status[name]?.status === "connected")
      const releases = connected.map(([, client]) => client.retain())
      return Effect.forEach(
        connected,
        ([clientName, client]) =>
          fetchFromClient(clientName, client, listFn, label).pipe(Effect.map((items) => Object.entries(items ?? {}))),
        { concurrency: "unbounded" },
      ).pipe(
        Effect.map((results) => Object.fromEntries<T & { client: string }>(results.flat())),
        Effect.ensuring(
          Effect.sync(() => {
            for (const release of releases) release()
          }),
        ),
      )
    }

    const prompts = Effect.fn("MCP.prompts")(function* () {
      const s = yield* getState()
      yield* refreshHost(s)
      return yield* collectFromConnected(s, (c) => c.listPrompts().then((r) => r.prompts), "prompts")
    })

    const resources = Effect.fn("MCP.resources")(function* () {
      const s = yield* getState()
      yield* refreshHost(s)
      return yield* collectFromConnected(s, (c) => c.listResources().then((r) => r.resources), "resources")
    })

    const withClient = Effect.fnUntraced(function* <A>(
      clientName: string,
      fn: (client: MCPClient) => Promise<A>,
      label: string,
      meta?: Record<string, unknown>,
    ) {
      const s = yield* getState()
      yield* refreshHost(s)
      const client = s.clients[clientName]
      if (!client) {
        log.warn(`client not found for ${label}`, { clientName })
        return undefined
      }
      const release = client.retain()
      return yield* Effect.tryPromise({
        try: () => fn(client),
        catch: (e: any) => {
          log.error(`failed to ${label}`, { clientName, ...meta, error: e?.message })
          return e
        },
      }).pipe(
        Effect.orElseSucceed(() => undefined),
        Effect.ensuring(Effect.sync(release)),
      )
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
      const hostEntry = HostMcp.get()[mcpName]
      const mcpConfig = hostEntry ?? cfg.mcp?.[mcpName]
      if (!mcpConfig || !isMcpConfigured(mcpConfig)) return undefined
      // Capture ownership with the config object so async connect cannot re-bind
      // sampling to a later HostMcp revision.
      return { mcp: mcpConfig, hostOwned: hostEntry != null }
    })

    const startAuthInternal = Effect.fn("MCP.startAuthInternal")(function* (mcpName: string) {
      const resolved = yield* getMcpConfig(mcpName)
      const mcpConfig = resolved?.mcp
      if (!mcpConfig) throw new Error(`MCP server ${mcpName} not found or disabled`)
      // Auth must not open or complete a connection that Host closed.
      if (resolved.hostOwned && mcpConfig.enabled === false) {
        throw new Error(`MCP server ${mcpName} is host-owned and disabled`)
      }
      // Capture attempt identity at the start of auth, not after connect completes (R003).
      const hostRevisionAtStart = resolved.hostOwned ? HostMcp.revisionOf(mcpName) : undefined
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
          const client = createClient()
          return client.connect(transport).then(
            () =>
              ({
                kind: "connected",
                oauthState,
                client,
                resolved,
                hostRevision: hostRevisionAtStart,
              }) satisfies AuthResult,
          )
        },
        catch: (error) => error,
      }).pipe(
        Effect.catch((error) => {
          if (error instanceof UnauthorizedError && capturedUrl) {
            // Same identity-bound publish as connectRemote (R007).
            publishPendingOAuthAttempt(mcpName, {
              transport,
              hostRevision: hostRevisionAtStart,
              fromHost: resolved.hostOwned,
            })
            return Effect.succeed({
              kind: "redirect",
              authorizationUrl: capturedUrl.toString(),
              oauthState,
            } satisfies AuthResult)
          }
          return Effect.die(error)
        }),
      )
    })

    /** Public HTTP/SDK shape: never includes the live client or effective config snapshot. */
    const startAuth = Effect.fn("MCP.startAuth")(function* (mcpName: string) {
      const result = yield* startAuthInternal(mcpName)
      if (result.kind === "connected") {
        // This public probe does not transfer its client to the connection registry.
        yield* auth
          .clearOAuthState(mcpName)
          .pipe(Effect.ensuring(Effect.tryPromise(() => result.client.close()).pipe(Effect.ignore)))
        return { authorizationUrl: "", oauthState: result.oauthState }
      }
      return { authorizationUrl: result.authorizationUrl, oauthState: result.oauthState }
    })

    const authenticate = Effect.fn("MCP.authenticate")(function* (mcpName: string) {
      const result = yield* startAuthInternal(mcpName)
      if (result.kind === "connected") {
        const { client, resolved } = result

        const listed = yield* defs(mcpName, client, resolved.mcp.timeout)
        if (!listed) {
          yield* Effect.tryPromise(() => client.close()).pipe(Effect.ignore)
          return { status: "failed", error: "Failed to get tools" } as Status
        }

        const s = yield* getState()
        yield* auth.clearOAuthState(mcpName)
        return yield* storeClient(
          s,
          mcpName,
          client,
          listed,
          resolved.mcp.timeout,
          hostEffectiveSampling(resolved.mcp, resolved.hostOwned),
          { fromHost: resolved.hostOwned, hostRevision: result.hostRevision },
        )
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
      const attempt = pendingOAuthTransports.get(mcpName)
      if (!attempt) throw new Error(`No pending OAuth flow for MCP server: ${mcpName}`)
      const { transport, hostRevision, fromHost } = attempt

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

      // Stale/attempt-replaced completion must not clear the new flow's verifier
      // or publish a connection (R007).
      if (pendingOAuthTransports.get(mcpName) !== attempt) {
        log.info("stale oauth completion ignored", { mcpName })
        return { status: "failed", error: "OAuth attempt superseded" } as Status
      }
      pendingOAuthTransports.delete(mcpName)
      yield* auth.clearCodeVerifier(mcpName)

      const resolved = yield* getMcpConfig(mcpName)
      if (!resolved) return { status: "failed", error: "MCP config not found after auth" } as Status

      return yield* createAndStore(mcpName, resolved.mcp, hostEffectiveSampling(resolved.mcp, fromHost), {
        fromHost,
        hostRevision,
      })
    })

    const removeAuth = Effect.fn("MCP.removeAuth")(function* (mcpName: string) {
      yield* auth.remove(mcpName)
      McpOAuthCallback.cancelPending(mcpName)
      pendingOAuthTransports.delete(mcpName)
      log.info("removed oauth credentials", { mcpName })
    })

    const supportsOAuth = Effect.fn("MCP.supportsOAuth")(function* (mcpName: string) {
      const resolved = yield* getMcpConfig(mcpName)
      if (!resolved) return false
      return resolved.mcp.type === "remote" && resolved.mcp.oauth !== false
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
      readiness,
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
