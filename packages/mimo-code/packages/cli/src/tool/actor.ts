import { captureWorkerNamingSource, readWorkerNamingSource, releaseWorkerNamingSource, workerNamingSourceCurrent } from "@/actor/worker-naming-source"
import { managedSessionBinding, markerMatches } from "@/memory/session-scope"
import { isAdmittedToolTimingTurn } from "@/session/tool-timing"
import type { ActorExecutionIdentity } from "@/actor/turn"
import * as Tool from "./tool"
import { RecoverableError } from "./recoverable"
import { tokenize } from "./shell-tokenize"
import DESCRIPTION from "./actor.txt"
import DESCRIPTION_CHECKPOINT from "./actor.checkpoint.txt"
import { withCheckpointDescription } from "./checkpoint-description"
import z from "zod"
import { Session } from "../session"
import { SessionID, MessageID, PartID } from "../session/schema"
import { MessageV2 } from "../session/message-v2"
import { Agent } from "../agent/agent"
import { Provider } from "../provider"
import type { ProviderID, ModelID } from "../provider/schema"
import { sortVisionModels } from "../provider/provider"
import type { SessionPrompt } from "../session/prompt"
import { Config } from "../config"
import { ActorRegistry } from "@/actor/registry"
import { ActorWaiter } from "@/actor/waiter"
import { spawnRef } from "@/actor/spawn-ref"
import { TaskRegistry } from "@/task/registry"
import { SYSTEM_SPAWNED_AGENT_TYPES } from "@/agent/config"
import { TaskID, type Task as NativeTask } from "@/task/schema"
import { EffectBridge, InstanceState } from "@/effect"
import { inboxServiceRef } from "@/inbox/inbox-ref"
import { inboxControllerRef } from "@/inbox"
import { Effect, Deferred } from "effect"
import type { ActorHostContext } from "@/actor/schema"
import * as ManagedProvider from "@/acp/managed-provider"

export interface ActorPromptOps {
  cancel(sessionID: SessionID): void
  resolvePromptParts(template: string): Effect.Effect<SessionPrompt.PromptInput["parts"]>
  prompt(input: SessionPrompt.PromptInput): Effect.Effect<MessageV2.WithParts>
}

const id = "actor"

const MODEL_PARAM_DESCRIPTION =
  "(optional) Model for this subagent: a model group name (e.g. ultra/standard/lite) or a literal provider/model (e.g. mimo-v2.5-pro). New workers inherit the parent model unless a model is named. An existing worker retains its effective model. If no model_groups are configured, the tier names resolve to the default model. To discover valid provider/model values (e.g. a vision-capable model for image tasks), run `actor models` (or `actor models --vision`)."

const KNOWN_ACTOR_VERBS = ["run", "spawn", "status", "wait", "cancel", "send", "models"]

function hostContextFromExtra(extra: Tool.Context["extra"]): ActorHostContext | undefined {
  const value = extra?.hostContext ?? extra?.actorHostContext
  if (!value || typeof value !== "object") return undefined
  const raw = value as Record<string, unknown>
  const context: ActorHostContext = {}
  for (const [key, type] of [
    ["accountID", "string"],
    ["grantID", "string"],
    ["chatID", "string"],
    ["workspaceID", "string"],
    ["cwd", "string"],
    ["goalID", "string"],
  ] as const) {
    if (typeof raw[key] === type && String(raw[key]).trim()) {
      ;(context as Record<string, unknown>)[key] = String(raw[key]).trim()
    }
  }
  for (const key of ["grantRevision", "credentialRevision"] as const) {
    if (typeof raw[key] === "number" && Number.isInteger(raw[key]) && raw[key] >= 0) {
      context[key] = raw[key]
    }
  }
  return Object.keys(context).length ? context : undefined
}

function levenshteinActor(a: string, b: string): number {
  const m = a.length, n = b.length
  if (m === 0) return n
  if (n === 0) return m
  const dp = Array.from({ length: m + 1 }, () => new Array(n + 1).fill(0))
  for (let i = 0; i <= m; i++) dp[i][0] = i
  for (let j = 0; j <= n; j++) dp[0][j] = j
  for (let i = 1; i <= m; i++) {
    for (let j = 1; j <= n; j++) {
      const cost = a[i - 1] === b[j - 1] ? 0 : 1
      dp[i][j] = Math.min(dp[i - 1][j] + 1, dp[i][j - 1] + 1, dp[i - 1][j - 1] + cost)
    }
  }
  return dp[m][n]
}

function suggestActorVerb(input: string): string | undefined {
  const candidates = KNOWN_ACTOR_VERBS.map((v) => ({ v, d: levenshteinActor(input, v) })).filter((c) => c.d <= 2)
  if (candidates.length !== 1) return undefined
  return candidates[0].v
}

// Static args type for shell parsing — mirrors the discriminated union shape but
// uses z.string() for subagent_type since the dynamic enum is only needed at
// Zod validation time (inside execute), not at parse time.
type ActorShellArgs =
  | { operation: { action: "run"; subagent_type: string; description: string; prompt: string; model?: string; task_id?: string; actor_id?: string; timeout_ms?: number; command?: string; context?: "none" | "state" | "full"; output_schema?: Record<string, unknown> } }
  | { operation: { action: "spawn"; subagent_type: string; description: string; prompt: string; model?: string; task_id?: string; actor_id?: string; command?: string; context?: "none" | "state" | "full"; output_schema?: Record<string, unknown> } }
  | { operation: { action: "status"; actor_id: string } }
  | { operation: { action: "wait"; actor_id: string; timeout_ms?: number } }
  | { operation: { action: "cancel"; actor_id: string } }
  | { operation: { action: "send"; to_actor_id: string; content: string; to_session_id?: string } }
  | { operation: { action: "models"; vision?: boolean; limit?: number } }

function actorArityError(verb: string, expected: string, args: string[], line: number) {
  return Effect.fail({
    kind: "arity",
    line,
    detail: `actor: ${verb}: arity mismatch\n  got:      actor ${verb} ${args.join(" ")}\n  expected: actor ${verb} ${expected}`,
  })
}

// Generic `--name value` / `--name=value` extractor for a fixed set of optional
// flags. Positionals (and any unrecognized tokens) fall through to `rest`.
function extractNamedFlags(
  args: string[],
  names: string[],
  line: number,
): Effect.Effect<{ flags: Record<string, string>; rest: string[] }, { kind: "flag"; line: number; detail: string }> {
  const rest: string[] = []
  const flags: Record<string, string> = {}
  for (let i = 0; i < args.length; i++) {
    const a = args[i]
    const bare = names.find((n) => a === `--${n}`)
    if (bare) {
      const next = args[i + 1]
      if (next === undefined)
        return Effect.fail({ kind: "flag" as const, line, detail: `actor: --${bare} requires a value` })
      flags[bare] = next
      i++
      continue
    }
    const eq = names.find((n) => a.startsWith(`--${n}=`))
    if (eq) {
      const v = a.slice(`--${eq}=`.length)
      if (v === "") return Effect.fail({ kind: "flag" as const, line, detail: `actor: --${eq} requires a value` })
      flags[eq] = v
      continue
    }
    rest.push(a)
  }
  return Effect.succeed({ flags, rest })
}

const mapActorVerb = Effect.fn("mapActorVerb")(function* (verb: string | undefined, args: string[], line: number) {
  switch (verb) {
    case "run": {
      const { flags, rest } = yield* extractNamedFlags(
        args,
        ["model", "task", "actor", "timeout", "command", "context", "output-schema"],
        line,
      )
      if (rest.length !== 3) return yield* actorArityError("run", '<subagent_type> "<description>" "<prompt>" [--model <ref>] [--task <TID>] [--actor <id>] [--timeout <ms>] [--command <cmd>] [--context none|state|full] [--output-schema <json>]', rest, line)
      return {
        operation: {
          action: "run" as const,
          subagent_type: rest[0],
          description: rest[1],
          prompt: rest[2],
          ...(flags.model ? { model: flags.model } : {}),
          ...(flags.task ? { task_id: flags.task } : {}),
          ...(flags.actor ? { actor_id: flags.actor } : {}),
          ...(flags.timeout ? { timeout_ms: Number(flags.timeout) } : {}),
          ...(flags.command ? { command: flags.command } : {}),
          ...(flags.context ? { context: flags.context } : {}),
          // JSON.parse throw surfaces as a parse-error for the whole script (parse
          // is all-or-nothing); bad enum/number flag values instead defer to zod at execute.
          ...(flags["output-schema"] ? { output_schema: JSON.parse(flags["output-schema"]) } : {}),
        },
      } as ActorShellArgs
    }
    case "spawn": {
      const { flags, rest } = yield* extractNamedFlags(
        args,
        ["model", "task", "actor", "command", "context", "output-schema"],
        line,
      )
      if (rest.length !== 3) return yield* actorArityError("spawn", '<subagent_type> "<description>" "<prompt>" [--model <ref>] [--task <TID>] [--actor <id>] [--command <cmd>] [--context none|state|full] [--output-schema <json>]', rest, line)
      return {
        operation: {
          action: "spawn" as const,
          subagent_type: rest[0],
          description: rest[1],
          prompt: rest[2],
          ...(flags.model ? { model: flags.model } : {}),
          ...(flags.task ? { task_id: flags.task } : {}),
          ...(flags.actor ? { actor_id: flags.actor } : {}),
          ...(flags.command ? { command: flags.command } : {}),
          ...(flags.context ? { context: flags.context } : {}),
          ...(flags["output-schema"] ? { output_schema: JSON.parse(flags["output-schema"]) } : {}),
        },
      } as ActorShellArgs
    }
    case "status":
      if (args.length !== 1) return yield* actorArityError("status", "<actor_id>", args, line)
      return { operation: { action: "status" as const, actor_id: args[0] } } as ActorShellArgs
    case "wait": {
      const { flags, rest } = yield* extractNamedFlags(args, ["timeout"], line)
      if (rest.length !== 1) return yield* actorArityError("wait", "<actor_id> [--timeout <ms>]", rest, line)
      return {
        operation: {
          action: "wait" as const,
          actor_id: rest[0],
          ...(flags.timeout ? { timeout_ms: Number(flags.timeout) } : {}),
        },
      } as ActorShellArgs
    }
    case "cancel":
      if (args.length !== 1) return yield* actorArityError("cancel", "<actor_id>", args, line)
      return { operation: { action: "cancel" as const, actor_id: args[0] } } as ActorShellArgs
    case "send": {
      const { flags, rest } = yield* extractNamedFlags(args, ["session"], line)
      if (rest.length !== 2)
        return yield* actorArityError("send", '<to_actor_id> "<content>" [--session <id>]', rest, line)
      return {
        operation: {
          action: "send" as const,
          to_actor_id: rest[0],
          content: rest[1],
          ...(flags.session ? { to_session_id: flags.session } : {}),
        },
      } as ActorShellArgs
    }
    case "models": {
      const vision = args.includes("--vision")
      const withoutVision = args.filter((a) => a !== "--vision")
      const { flags, rest } = yield* extractNamedFlags(withoutVision, ["limit"], line)
      if (rest.length !== 0)
        return yield* actorArityError("models", "[--vision] [--limit <n>]", rest, line)
      return {
        operation: {
          action: "models" as const,
          ...(vision ? { vision: true } : {}),
          ...(Number.isInteger(Number(flags.limit)) && Number(flags.limit) > 0 ? { limit: Number(flags.limit) } : {}),
        },
      } as ActorShellArgs
    }
    default: {
      const suggestion = suggestActorVerb(verb ?? "")
      const detail =
        `actor: unknown verb "${verb ?? ""}"\n` +
        `  available verbs: ${KNOWN_ACTOR_VERBS.join(", ")}` +
        (suggestion ? `\n  did you mean: ${suggestion}?` : "")
      return yield* Effect.fail({ kind: "unknown-verb", line, detail })
    }
  }
})

export function parseActorScript(
  script: string,
): Effect.Effect<ActorShellArgs[], unknown> {
  return Effect.gen(function* () {
    const argvList = yield* tokenize(script)
    const out: ActorShellArgs[] = []
    for (const argv of argvList) {
      const [head, verb, ...rest] = argv.tokens
      if (head !== "actor") {
        return yield* Effect.fail({
          kind: "unknown-verb",
          line: argv.line,
          detail: `actor: every command must start with 'actor' (got '${head ?? ""}')`,
        })
      }
      const parsed = yield* mapActorVerb(verb, rest, argv.line)
      out.push(parsed)
    }
    return out
  })
}

function inferAction(o: Record<string, unknown>): "run" | "spawn" {
  if (o.action === "spawn" || o.action === "run") return o.action
  if (o.background === true || o.async === true) return "spawn"
  return "run"
}

// Recover a shell-mode actor call that arrived shaped like the JSON tool args
// (no `script`): the Task-prior bare `{subagent_type, description, prompt}`, a
// stringified `{operation:"..."}` envelope, or an already-nested `{operation:{}}`.
// Returns the parsed shape for shellWrap to route to execute (which zod-validates
// it), or undefined if rawArgs can't be lifted.
export function recoverActorArgs(rawArgs: unknown): ActorShellArgs | undefined {
  if (rawArgs == null || typeof rawArgs !== "object") return undefined
  let obj = rawArgs as Record<string, unknown>
  if (typeof obj.operation === "string") {
    try {
      const inner = JSON.parse(obj.operation)
      if (inner && typeof inner === "object" && !Array.isArray(inner)) obj = { operation: inner }
    } catch {}
  }
  if (obj.operation && typeof obj.operation === "object" && !Array.isArray(obj.operation))
    return { operation: obj.operation } as ActorShellArgs
  const subagent_type = obj.subagent_type
  const description = obj.description
  const prompt = obj.prompt
  if (typeof subagent_type === "string" && typeof description === "string" && typeof prompt === "string") {
    const op: Record<string, unknown> = { action: inferAction(obj), subagent_type, description, prompt }
    // Carry only the optional fields a confused model plausibly puts at top level
    // alongside the bare Task-prior triple. This is a deliberate subset of the
    // run/spawn schema's optionals (model, actor_id, timeout_ms, command, context,
    // task_id, output_schema) — the others (timeout_ms/command/context/output_schema)
    // are dropped here, falling back to their schema defaults. Low risk in practice:
    // the bare shape mimo emits is the 3 required fields, rarely with extras. When
    // adding an actor schema field, decide whether bare-shape recover should carry
    // it here, or this whitelist silently drifts from the schema.
    if (typeof obj.model === "string") op.model = obj.model
    if (typeof obj.task_id === "string") op.task_id = obj.task_id
    if (typeof obj.actor_id === "string") op.actor_id = obj.actor_id
    return { operation: op } as ActorShellArgs
  }
  return undefined
}

export const ActorTool = Tool.define(
  id,
  Effect.gen(function* () {
    const agent = yield* Agent.Service
    const config = yield* Config.Service
    const provider = yield* Provider.Service
    const sessions = yield* Session.Service
    const actorRegistry = yield* ActorRegistry.Service
    const waiter = yield* ActorWaiter.Service
    const tasks = yield* TaskRegistry.Service

    // Resolve the Actor service through the late-bound spawnRef rather than as
    // a Layer dependency: pulling Actor.Service in here would create a layer
    // cycle (Actor → SessionPrompt → ToolRegistry → tool/actor → Actor) that
    // Effect cannot satisfy. The ref is populated by Actor.layer's initialiser
    // (see actor/spawn-ref.ts).
    const requireActor = () => {
      const a = spawnRef.current
      if (!a) {
        return Effect.fail(
          new Error(
            "Actor service unavailable — Actor.appLayer must be running for the actor tool to spawn or cancel actors",
          ),
        )
      }
      return Effect.succeed(a)
    }

    // Tool def is built lazily (function form of Init) because the dynamic
    // `subagent_type` enum below calls agent.list(), which queries
    // InstanceState — Instance is only available at tool-init time
    // (per-invocation), not at service-resolution time when ActorTool is
    // wired into ToolRegistry's layer.
    return Effect.fn("ActorTool.init")(function* () {
      // F36a: build subagent_type as a dynamic z.enum from the agent registry,
      // filtered to spawnable agents (mode === "subagent" && !hidden). Excludes
      // hidden internals (title, summary, checkpoint-writer per F24) and
      // includes both native registry agents (general/explore) and
      // user-config-defined subagents. This gives the LLM a discoverable,
      // validated list of agent types — replaces the prior bare z.string()
      // that the model couldn't introspect (root cause of three harness runs
      // with zero subagent spawns).
      const allAgents = yield* agent.list()
      const spawnable = allAgents.filter((a) => a.mode === "subagent" && !a.hidden)
      const spawnableNames = spawnable.map((a) => a.name)
      if (spawnableNames.length === 0) {
        return yield* Effect.die(new Error("No spawnable subagent types"))
      }
      const subagentTypeEnum = z.enum(spawnableNames as [string, ...string[]])

      const actorIdRequiredField = z
        .string()
        .min(1)
        .describe(
          "Actor session id to operate on. Distinct from the user-task IDs (T1, T2, ...) used by the `task` tool.",
        )

      const timeoutField = z
        .number()
        .int()
        .positive()
        .optional()
        .describe("(optional) Milliseconds to wait before returning { status: 'timeout' }. Default 600000 (10 min).")

      const runSchema = z.strictObject({
        action: z
          .literal("run")
          .describe(
            "RARE EXCEPTION — launches a subagent and waits for its inline result until completion, user input, or timeout. Interruption and timeout release the wait without cancelling the subagent. Use it only when you cannot make your very next decision without the result in THIS turn and the work is a tiny, fast lookup. For ordinary analysis, review, or implementation work use `spawn` instead.",
          ),
        description: z.string().min(1).describe("A short (3-5 words) description of the task."),
        prompt: z.string().min(1).describe("The task for the agent to perform."),
        subagent_type: subagentTypeEnum.describe("The type of specialized agent to use for this task."),
        actor_id: z.string().min(1).optional().describe("Resume an existing actor in this session."),
        model: z
          .string()
          .min(1)
          .optional()
          .describe(MODEL_PARAM_DESCRIPTION),
        timeout_ms: timeoutField,
        command: z.string().min(1).optional().describe("(optional) The command that triggered this task."),
        task_id: z
          .string()
          .min(1)
          .optional()
          .describe(
            "(optional) If this subagent is doing work for a specific task in the `task` tool, pass that task's ID (e.g. T4, T2.1) here — only an ID the `task` tool returned this session. A verified assignment lets the actor use workflow_artifact with the progress role under the exact task owner. Unqualified assignments cannot write task progress. If the ID is malformed or names no existing task, the binding is dropped. Leave omitted only for work that isn't tied to a task.",
          ),
        output_schema: z
          .record(z.string(), z.any())
          .optional()
          .describe(
            "(optional) A JSON Schema. When set, the subagent is forced to return a single structured object matching this schema (via the StructuredOutput tool) instead of free text; the validated object is returned in <actor_result>.",
          ),
      })

      const spawnSchema = z.strictObject({
        action: z
          .literal("spawn")
          .describe(
            "THE DEFAULT — launches a subagent in the BACKGROUND and returns its actor_id immediately, so subagents run in PARALLEL and you keep responding to the user. The result arrives as a notification, or collect it with `wait`/`status`.",
          ),
        description: z.string().min(1).describe("A short (3-5 words) description of the task."),
        prompt: z.string().min(1).describe("The task for the agent to perform."),
        subagent_type: subagentTypeEnum.describe("The type of specialized agent to use for this task."),
        actor_id: z.string().min(1).optional().describe("Resume an existing actor in this session."),
        model: z
          .string()
          .min(1)
          .optional()
          .describe(MODEL_PARAM_DESCRIPTION),
        command: z.string().min(1).optional().describe("(optional) The command that triggered this task."),
        task_id: z
          .string()
          .min(1)
          .optional()
          .describe(
            "(optional) If this subagent is doing work for a specific task in the `task` tool, pass that task's ID (e.g. T4, T2.1) here — only an ID the `task` tool returned this session. A verified assignment lets the actor use workflow_artifact with the progress role under the exact task owner. Unqualified assignments cannot write task progress. If the ID is malformed or names no existing task, the binding is dropped. Leave omitted only for work that isn't tied to a task.",
          ),
        output_schema: z
          .record(z.string(), z.any())
          .optional()
          .describe(
            "(optional) A JSON Schema. When set, the subagent is forced to return a single structured object matching this schema (via the StructuredOutput tool) instead of free text.",
          ),
      })

      const statusSchema = z.strictObject({
        action: z.literal("status"),
        actor_id: actorIdRequiredField,
      })

      const waitSchema = z.strictObject({
        action: z.literal("wait"),
        actor_id: actorIdRequiredField,
        timeout_ms: timeoutField,
      })

      const cancelSchema = z.strictObject({
        action: z.literal("cancel"),
        actor_id: actorIdRequiredField,
      })

      const sendSchema = z.strictObject({
        action: z.literal("send"),
        to_session_id: z
          .string()
          .min(1)
          .optional()
          .describe(
            "(optional) Target session ID. Defaults to the current session — useful for sending to subagents in this session.",
          ),
        to_actor_id: z
          .string()
          .min(1)
          .describe(
            "Target actor ID. Use 'main' to send to a session's main agent, or a subagent ID like 'explore-1'.",
          ),
        content: z.string().min(1).describe("Message content (plain text). Wrapped in <inbox> for the receiver."),
        type: z
          .literal("text")
          .optional()
          .describe(
            "(optional) Ordinary untrusted message type. Only 'text' is accepted; lifecycle notifications are generated by the actor runtime.",
          ),
      })

      const modelsSchema = z.strictObject({
        action: z.literal("models"),
        vision: z.boolean().optional().describe("(optional) If true, list only vision-capable models (models that accept image input)."),
        limit: z.number().int().positive().optional().describe("(optional) Max number of models to return. Default 50."),
      })

      const parameters = z.strictObject({
        // .meta({ type: "object" }) is REQUIRED — without it the emitted JSON
        // schema's `operation` node has only `anyOf`, no `type`, and some models
        // (notably mimo-v2.5-pro) stringify the whole envelope
        // ({"operation":"{\"action\":\"run\",...}"}) which fails zod validation.
        // The root strictObject also means flattenDiscriminatedUnion finds no
        // root-level union and passes through unchanged — root keeps exactly one
        // key (`operation`), so models can't drop the discriminator.
        operation: z
          .discriminatedUnion("action", [
            // spawn first: it is the default action, and the model reads this
            // union in order. run stays available but is listed as the exception.
            spawnSchema,
            runSchema,
            statusSchema,
            waitSchema,
            cancelSchema,
            sendSchema,
            modelsSchema,
          ])
          .meta({ type: "object" }),
      })

      const run = Effect.fn("ActorTool.execute")(function* (input: z.infer<typeof parameters>, ctx: Tool.Context) {
        const op = input.operation
        const bridge = yield* EffectBridge.make()
        const cfg = yield* config.get()

        // When called through exec, subagents may only use `send` to communicate
        // with the parent. All other actions (spawn, run, cancel, status, wait,
        // models) are restricted to primary agents. Peer actors have a primary
        // agent type so they are unaffected by this guard.
        if (ctx.extra?.fromExec) {
          const caller = yield* agent.get(ctx.agent)
          if (caller?.mode === "subagent" && op.action !== "send") {
            return yield* Effect.fail(
              new RecoverableError(
                `Subagents can only use actor send to communicate with the parent agent. You are running as "${caller.name}"; use actor send with to_actor_id="main" to report progress or findings.`,
              ),
            )
          }
        }

        // Helper: "actor belongs to another session OR doesn't exist" response.
        // Same response for both cases — don't leak the difference (POSIX: you can
        // only reap your own children).
        const unknownResponse = (label: string, actorID: string) => {
          const snapshot = { status: "unknown" as const, actor_id: actorID }
          return {
            title: `Actor ${label}: unknown`,
            output: JSON.stringify(snapshot),
            metadata: { actor_id: actorID, status: "unknown" } as Record<string, any>,
          }
        }

        // Look up an actor in the registry. Subagent actors live under
        // ctx.sessionID; peer actors live under their own sessionID (== actorID).
        // Try the subagent location first, then fall back to the peer location.
        const directSession = Effect.fn("ActorTool.directSession")(function* (sessionID: SessionID) {
          if (sessionID === ctx.sessionID) return true
          const target = yield* sessions.get(sessionID).pipe(Effect.catchDefect(() => Effect.succeed(undefined)))
          if (!target) return false
          if (target.parentID === ctx.sessionID) return true
          const current = yield* sessions.get(ctx.sessionID).pipe(Effect.catchDefect(() => Effect.succeed(undefined)))
          return current?.parentID === sessionID
        })

        const findActor = Effect.fn("ActorTool.findActor")(function* (actorID: string) {
          const sub = yield* actorRegistry.get(ctx.sessionID, actorID)
          if (sub) return { entry: sub, sessionID: ctx.sessionID }
          const sid = SessionID.make(actorID)
          if (!(yield* directSession(sid))) return undefined
          const peer = yield* actorRegistry.get(sid, actorID)
          if (peer?.mode === "peer") return { entry: peer, sessionID: sid }
          return undefined
        })

        if (op.action ==="send") {
          const inboxSvc = inboxServiceRef.current
          if (!inboxSvc) {
            return yield* Effect.fail(
              new Error("Inbox service unavailable — Inbox.layer must be running for the actor tool to send messages"),
            )
          }
          const targetSid = op.to_session_id !== undefined ? SessionID.make(op.to_session_id) : ctx.sessionID
          if (!(yield* directSession(targetSid))) return unknownResponse("send", op.to_actor_id)
          const execution = ctx.extra?.actorExecution as ActorExecutionIdentity | undefined
          const caller = execution?.sessionID === ctx.sessionID ? execution.actorID : ctx.actorID
          const exact = caller ? yield* actorRegistry.get(ctx.sessionID, caller) : undefined
          const sender = exact ?? (yield* Effect.gen(function* () {
            const message = yield* Effect.sync(() => MessageV2.get({ sessionID: ctx.sessionID, messageID: ctx.messageID }))
            if (message.info.role !== "assistant") return undefined
            return yield* actorRegistry.get(ctx.sessionID, message.info.agentID ?? "main")
          }).pipe(Effect.catchDefect(() => Effect.succeed(undefined))))
          if (!sender || (execution && (execution.sessionID !== ctx.sessionID || sender.actorID !== execution.actorID ||
              sender.executionRevision !== execution.executionRevision)))
            return yield* Effect.fail(new RecoverableError("The sending actor could not be verified."))
          if (targetSid !== ctx.sessionID && ManagedProvider.enabled()) {
            const target = yield* actorRegistry.get(targetSid, op.to_actor_id)
            if (!target || !sender.hostContext?.chatID || sender.hostContext.chatID !== target.hostContext?.chatID ||
                sender.hostContext.workspaceID !== target.hostContext?.workspaceID)
              return unknownResponse("send", op.to_actor_id)
          }
          const sendResult = yield* inboxSvc
            .accept({
              receiverSessionID: targetSid,
              receiverActorID: op.to_actor_id,
              senderSessionID: ctx.sessionID,
              senderActorID: sender.actorID,
              content: op.content,
              ...(op.type !== undefined ? { type: op.type } : {}),
            })
            .pipe(
              Effect.catchTag("InboxReceiverNotFound", () =>
                Effect.succeed({ inboxID: null as string | null, error: "receiver not found" }),
              ),
              Effect.catchTag("InboxInvalidMessage", (error) =>
                Effect.succeed({ inboxID: null as string | null, error: error.reason }),
              ),
            )
          if ("error" in sendResult) {
            return {
              title: `Send failed: receiver not found`,
              output: JSON.stringify(sendResult),
              metadata: {
                receiver_actor_id: op.to_actor_id,
                receiver_session_id: targetSid,
                error: sendResult.error,
              } as Record<string, any>,
            }
          }
          return {
            title: `Input accepted for ${op.to_actor_id}`,
            output: JSON.stringify(sendResult),
            metadata: {
              inboxID: sendResult.inboxID,
              inputMessageID: sendResult.inputMessageID,
              receiver_actor_id: op.to_actor_id,
              receiver_session_id: targetSid,
              sender_actor_id: sender.actorID,
              sender_session_id: ctx.sessionID,
              sender_execution_revision: sender.executionRevision,
              disposition: sendResult.disposition,
            } as Record<string, any>,
          }
        }

        if (op.action ==="status") {
          const found = yield* findActor(op.actor_id)
          if (!found) return unknownResponse("status", op.actor_id)
          const entry = yield* waiter.status(found.entry)
          const snapshot = {
            status: entry.status,
            executionActive: entry.executionActive,
            executionState: entry.executionState,
            actor_id: entry.actorID,
            description: entry.description,
            agent: entry.agent,
            background: entry.background,
            turnCount: entry.turnCount,
            lastTurnTime: entry.lastTurnTime,
            ...(entry.requestedModel ? { requested_model: entry.requestedModel } : {}),
            ...(entry.effectiveModel ? { effective_model: entry.effectiveModel } : {}),
            ...(entry.hostContext ? { host_context: entry.hostContext } : {}),
            lastOutcome: entry.lastOutcome,
            ...(entry.lastError !== undefined ? { error: entry.lastError } : {}),
            time: entry.time,
          }
          return {
            title: `Actor status: ${entry.executionState}`,
            output: JSON.stringify(snapshot),
            metadata: { actor_id: entry.actorID, status: entry.status } as Record<string, any>,
          }
        }

        if (op.action ==="wait") {
          const found = yield* findActor(op.actor_id)
          if (!found) return unknownResponse("wait", op.actor_id)
          const snap = yield* waiter.wait({
            sessionID: found.sessionID,
            actor_id: op.actor_id,
            timeout_ms: op.timeout_ms,
          })
          let quietReceipt = false
          if (snap.status==="timeout" && snap.executionActive && found.sessionID===ctx.sessionID
              && found.entry.parentActorID && !ctx.abort.aborted) {
            const execution=ctx.extra?.actorExecution as ActorExecutionIdentity | undefined
            const parentID=execution?.sessionID===ctx.sessionID ? execution.actorID : ctx.actorID
            const parent=parentID===found.entry.parentActorID
              ? yield* actorRegistry.get(ctx.sessionID,found.entry.parentActorID)
              : undefined
            const inbox=inboxServiceRef.current
            if (parent && inbox?.quietParent && (!execution || execution.sessionID===ctx.sessionID &&
                parent.executionRevision===execution.executionRevision)) {
              quietReceipt=yield* inbox.quietParent({
                sessionID:ctx.sessionID,childActorID:found.entry.actorID,childRevision:found.entry.executionRevision,
                parentActorID:parent.actorID,parentRevision:parent.executionRevision,
              })
            }
          }
          const title =
            snap.status === "interrupted"
              ? "Actor wait: interrupted by user message"
              : `Actor wait: ${snap.status}${snap.lastOutcome ? "/" + snap.lastOutcome : ""}`
          const output =
            snap.status === "interrupted"
              ? JSON.stringify(snap) + "\n" + ActorWaiter.WAIT_INTERRUPTED_HINT
              : JSON.stringify(snap.status === "timeout" ? { ...snap, hint: ActorWaiter.WAIT_TIMEOUT_HINT,
                ...(quietReceipt ? { quietHint:"Recorded for the existing parent", coverage:"same-session task-backed group" } : {}) } : snap)
          return {
            title,
            output,
            metadata: {
              actor_id: snap.actor_id,
              status: snap.status,
              ...(snap.lastOutcome ? { lastOutcome: snap.lastOutcome } : {}),
            } as Record<string, any>,
          }
        }

        if (op.action ==="cancel") {
          const found = yield* findActor(op.actor_id)
          if (!found) return unknownResponse("cancel", op.actor_id)
          const entry = found.entry

          // Already terminal? No-op — return current status. Idempotent.
          if (entry.status === "idle") {
            const snapshot = {
              status: entry.status,
              actor_id: entry.actorID,
              description: entry.description,
              agent: entry.agent,
              background: entry.background,
              ...(entry.requestedModel ? { requested_model: entry.requestedModel } : {}),
              ...(entry.effectiveModel ? { effective_model: entry.effectiveModel } : {}),
              ...(entry.hostContext ? { host_context: entry.hostContext } : {}),
            }
            return {
              title: `Actor cancel: ${entry.status}`,
              output: JSON.stringify(snapshot),
              metadata: { actor_id: entry.actorID, status: entry.status } as Record<string, any>,
            }
          }

          // Signal the actor through Actor.cancel — marks status "cancelled" in the registry.
          const actorForCancel = yield* requireActor()
          yield* actorForCancel.cancel(found.sessionID, entry.actorID, "graceful")

          const snapshot = {
            status: "cancelled" as const,
            actor_id: entry.actorID,
            description: entry.description,
            agent: entry.agent,
            background: entry.background,
            ...(entry.requestedModel ? { requested_model: entry.requestedModel } : {}),
            ...(entry.effectiveModel ? { effective_model: entry.effectiveModel } : {}),
            ...(entry.hostContext ? { host_context: entry.hostContext } : {}),
          }
          return {
            title: `Actor cancel: cancelled`,
            output: JSON.stringify(snapshot),
            metadata: { actor_id: entry.actorID, status: "cancelled" } as Record<string, any>,
          }
        }

        if (op.action === "models") {
          const providers = yield* provider.list()
          const allModels = Object.values(providers).flatMap((info) => Object.values(info.models))
          const filtered = op.vision ? allModels.filter((m) => m.capabilities.input.image === true) : allModels
          const ordered = op.vision
            ? sortVisionModels(filtered)
            : [...filtered].sort((a, b) => `${a.providerID}/${a.id}`.localeCompare(`${b.providerID}/${b.id}`))
          const limit = op.limit ?? 50
          const shown = ordered.slice(0, limit)
          const lines = shown.map((m) => `${m.providerID}/${m.id}${m.capabilities.input.image ? " (vision)" : ""}`)
          const header = op.vision ? `Vision-capable models` : `Available models`
          const more = ordered.length > shown.length ? `\n… and ${ordered.length - shown.length} more (raise --limit)` : ""
          const output = shown.length === 0
            ? (op.vision ? "No vision-capable models are configured. Configure a vision model or use an OCR tool." : "No models are configured.")
            : `${header} (${shown.length} of ${ordered.length}):\n${lines.join("\n")}${more}\nPass any of these to actor --model.`
          return { title: header, output, metadata: { count: shown.length, total: ordered.length, vision: !!op.vision } as Record<string, any> }
        }

        // op.action ==="run" or "spawn" — schema guarantees
        // description / prompt / subagent_type are present and non-empty.
        //
        // Final defence line for the no-nested-delegation invariant that
        // ToolRegistry.available already enforces by masking `actor` out of every
        // subagent's schema: refuse a spawn whose caller is itself a subagent, so
        // a stale prompt-cached schema or a hand-rolled call site cannot recurse.
        // bypassAgentCheck marks a dispatch that did NOT originate from a model
        // deciding to delegate — handleSubtask (where ctx.agent is the CHILD being
        // spawned, not the caller) and explicit user @agent mentions — so those
        // legitimately pass through.
        if (!ctx.extra?.bypassAgentCheck) {
          const caller = yield* agent.get(ctx.agent)
          if (caller?.mode === "subagent" && !SYSTEM_SPAWNED_AGENT_TYPES.has(caller.name)) {
            return yield* Effect.fail(
              new RecoverableError(
                `Subagents cannot spawn other subagents. You are running as "${caller.name}"; complete this task yourself with the tools available to you.`,
              ),
            )
          }
          yield* ctx.ask({
            permission: "actor",
            patterns: [op.subagent_type],
            always: ["*"],
            metadata: {
              description: op.description,
              subagent_type: op.subagent_type,
            },
          })
        }

        const next = yield* agent.get(op.subagent_type)
        if (!next) {
          return yield* Effect.fail(
            new RecoverableError(
              `Unknown agent type "${op.subagent_type}". Valid subagent_type values are listed in the actor tool description — pass one of those.`,
            ),
          )
        }

        const prompt = op.prompt

        const msg = yield* Effect.sync(() => MessageV2.get({ sessionID: ctx.sessionID, messageID: ctx.messageID }))
        if (msg.info.role !== "assistant") return yield* Effect.fail(new Error("Not an assistant message"))

        const priorActor = op.actor_id ? yield* actorRegistry.get(ctx.sessionID, op.actor_id) : undefined
        if (op.actor_id && !priorActor) {
          return yield* Effect.fail(new RecoverableError(`Cannot resume unknown actor "${op.actor_id}".`))
        }
        if (priorActor && (priorActor.mode !== "subagent" || priorActor.agent !== next.name))
          return yield* Effect.fail(new RecoverableError("Continue this worker with its registered role or use its addressed input."))
        if (priorActor && !priorActor.effectiveModel)
          return yield* Effect.fail(new RecoverableError("Cannot resume this actor because its effective model is unavailable."))
        // A new worker inherits the actual admitted parent model unless the
        // caller explicitly names another. A continuation keeps its own model.
        const model = (op.model
          ? yield* provider.resolveModelRef(op.model, msg.info.providerID)
              .pipe(Effect.map((m) => ({ modelID: m.id, providerID: m.providerID })))
          : priorActor?.effectiveModel ?? { modelID: msg.info.modelID, providerID: msg.info.providerID }) as { providerID: ProviderID; modelID: ModelID }
        if (priorActor && op.model && priorActor.effectiveModel &&
            (model.providerID !== priorActor.effectiveModel.providerID || model.modelID !== priorActor.effectiveModel.modelID))
          return yield* Effect.fail(new RecoverableError("An existing actor retains its effective model; create a new actor to select another model."))
        const requestedModel = priorActor ? priorActor.requestedModel : op.model ?? "inherit-parent"

        // Preserve the parent's actual variant on inheritance; null prevents a
        // configured subagent default from filling an absent parent variant.
        const parentVariant = msg.info.variant
        let variant: string | null | undefined = priorActor?.modelRequestOptions?.options.reasoning_effort ?? (!op.model ? null : undefined)
        if (parentVariant && !priorActor) {
          const sameModel = model.providerID === msg.info.providerID && model.modelID === msg.info.modelID
          if (sameModel) variant = parentVariant
          else {
            const target = yield* provider
              .getModel(model.providerID, model.modelID)
              .pipe(Effect.catchDefect(() => Effect.succeed(undefined)))
            if (target?.variants?.[parentVariant]) variant = parentVariant
          }
        }

        // Validate task_id by reference at execute time (NOT in the schema, so a
        // bad value degrades instead of hard-failing the call). A malformed shape
        // or an ID that names no task in this session ⇒ run ad-hoc (task_id
        // dropped) and tell the model why, so a fabricated ID becomes harmless
        // instead of triggering phantom postStop progress nagging.
        let effectiveTaskId = op.task_id
        let assignment: NativeTask | undefined
        let taskNotice = ""
        if (op.task_id) {
          if (!TaskID.safeParse(op.task_id).success) {
            effectiveTaskId = undefined
            taskNotice = `note: task_id "${op.task_id}" is not a valid task ID (expected Tn or Tn.m); ran ad-hoc. Task IDs come from the \`task\` tool.`
          } else {
            assignment = yield* tasks.get({ session_id: ctx.sessionID, id: op.task_id })
            if (!assignment) {
              effectiveTaskId = undefined
              taskNotice = `note: task_id "${op.task_id}" does not exist in this session; ran ad-hoc. Create it with the \`task\` tool first, or omit task_id.`
            }
          }
        }

        // v6: subagents share the parent's sessionID and run as registered actors
        // under the parent. Actor.spawn handles registry registration, forking
        // the agent loop, and sending inbox notifications on terminal — replacing
        // the legacy session.create + manual fork path that lived here pre-Task-29.
        const actor = yield* requireActor()
        // A resume addresses an existing durable actor. Its persisted host
        // binding is authoritative; passing the current parent scope here
        // would let a later prompt silently rebind the child. New actors
        // inherit the current managed scope in Actor.spawnSubagent.
        const hostContext = priorActor ? undefined : hostContextFromExtra(ctx.extra)
        const execution = ctx.extra?.actorExecution as ActorExecutionIdentity | undefined
        const recorded = msg.info.metadata?.managedExecution as typeof execution
        let optionCandidate: import("@/acp/openclank-protocol").ActorOptionCandidate | undefined
        if (!priorActor && ManagedProvider.enabled() && !execution)
          return yield* Effect.fail(new Error("Parent actor option provenance is unavailable"))
        if (!priorActor && execution) {
          if (!recorded || !execution.rootOperationID || execution.sessionID !== ctx.sessionID ||
              recorded.sessionID !== execution.sessionID || recorded.actorID !== execution.actorID ||
              recorded.executionRevision !== execution.executionRevision || recorded.rootOperationID !== execution.rootOperationID ||
              !execution.actorID || !Number.isSafeInteger(execution.executionRevision))
            return yield* Effect.fail(new Error("Parent actor option provenance is unavailable"))
          optionCandidate = { version: 1, source: { kind: "parent_execution", sessionID: execution.sessionID,
            actorID: execution.actorID, executionRevision: execution.executionRevision, rootOperationID: execution.rootOperationID, messageID: ctx.messageID },
            model: { providerID: msg.info.providerID, modelID: msg.info.modelID }, variant: msg.info.variant ?? null }
        }
        // The host-user owner is private process context, never an actor tool
        // argument or the provider billing account. Only the exact admitted
        // parent execution is eligible to mint a new actor owner binding.
        let originalOwnerAccountID: string | undefined
        if (!priorActor && execution?.actorID && execution.sessionID === ctx.sessionID &&
            Number.isSafeInteger(execution.executionRevision) && execution.rootOperationID) {
          try {
            if (ManagedProvider.currentRootOperationID(ctx.sessionID, execution.actorID, execution.executionRevision) === execution.rootOperationID)
              originalOwnerAccountID = ManagedProvider.currentActorHostContext(ctx.sessionID, execution.actorID, execution.executionRevision!)?.ownerAccountID
          } catch { /* unavailable means an unowned display, never inferred */ }
        }
        const directParent = !ManagedProvider.enabled()
          ? yield* actorRegistry.get(ctx.sessionID, msg.info.agentID ?? "main")
          : undefined
        const currentParentID = execution?.rootOperationID && execution.innerTurnID &&
          isAdmittedToolTimingTurn(execution, {sessionID:ctx.sessionID,actorID:execution.actorID,
            executionRevision:execution.executionRevision,rootOperationID:execution.rootOperationID,innerTurnID:execution.innerTurnID})
          ? execution.actorID : directParent?.actorID
        const parentActorID = priorActor ? priorActor.parentActorID : currentParentID
        const properties = op.output_schema?.properties
        const workerBrief = {
          version: 1,
          description: op.description.slice(0, 320),
          source: { sessionId:ctx.sessionID, inputMessageId:msg.info.parentID,
            assistantMessageId:ctx.messageID, ...(ctx.callID ? { toolCallId:ctx.callID } : {}) },
          ...(parentActorID ? { parent:{ sessionId:ctx.sessionID, actorId:parentActorID } } : {}),
          ...(assignment ? { assignment:{ taskId:assignment.id, parentTaskId:assignment.parent_task_id,
            summary:assignment.summary.slice(0, 320), status:assignment.status,
            progressRef:`tasks/${assignment.id}/progress.md` } } : {}),
          ...(priorActor ? { continuation:{ sessionId:priorActor.sessionID, actorId:priorActor.actorID,
            priorExecutionRevision:priorActor.executionRevision } } : {}),
          ...(requestedModel ? { requestedModel } : {}),
          expectedReturn: op.output_schema ? {
            kind:"structured", fields: properties && typeof properties === "object" && !Array.isArray(properties)
              ? Object.keys(properties).slice(0, 24).map(key=>key.slice(0, 80)) : [],
          } : { kind:"worker_result" },
        }
        const briefInput = assignment
          ? `

Recorded assignment ${assignment.id}: ${JSON.stringify(workerBrief.assignment)}
Report the outcome and evidence through the existing task result.`
          : ""
        let actorID: string | undefined
        let afterRevision: number | undefined
        const notifyOnCompletion = op.action === "run"
          ? yield* Effect.acquireRelease(Deferred.make<boolean>(), (decision) => Deferred.succeed(decision, false))
          : undefined
        if (op.action === "run") {
          function cancelHandler() {
            if (actorID) bridge.fork(actor.cancel(ctx.sessionID, actorID, "graceful"))
          }
          yield* Effect.acquireRelease(
            Effect.sync(() => ctx.abort.addEventListener("abort", cancelHandler)),
            () => Effect.sync(() => ctx.abort.removeEventListener("abort", cancelHandler)),
          )
          if (ctx.abort.aborted) return yield* Effect.interrupt
          const tq = inboxControllerRef.current
          if (tq) afterRevision = yield* tq.observeInput({ sessionID: ctx.sessionID, agentID: "main" }, -1)
        }
        // A private witness lets the allocation owner recheck this exact binding
        // inside the insert transaction; failed proof only omits the derivation.
        let derivedWorkspace: {chatID:string;workspaceID:string;current:()=>boolean} | undefined
        if (!priorActor && !op.actor_id && execution?.actorID === "main" && execution.sessionID === ctx.sessionID &&
            execution.rootOperationID && execution.innerTurnID && ManagedProvider.workerNamingEnabled(execution)) {
          const instance = yield* InstanceState.context
          const bootstrap = execution.preflightRoute?.bootstrap
          const binding = managedSessionBinding(ctx.sessionID)
          const host = ManagedProvider.currentActorHostContext(ctx.sessionID, "main", execution.executionRevision)
          const issuer = ManagedProvider.currentActorToolTimingIssuer(ctx.sessionID, "main", execution.executionRevision, execution.rootOperationID)
          const turn = {sessionID:ctx.sessionID,actorID:"main",executionRevision:execution.executionRevision,
            rootOperationID:execution.rootOperationID,innerTurnID:execution.innerTurnID}
          const current = () => {
            try {
              const now=managedSessionBinding(ctx.sessionID)
              const liveHost=ManagedProvider.currentActorHostContext(ctx.sessionID,"main",execution.executionRevision)
              return !!bootstrap && execution.preflightRoute?.bootstrap === bootstrap && bootstrap.rootSessionID === ctx.sessionID &&
                bootstrap.sessionID === ctx.sessionID && bootstrap.actorID === "main" && bootstrap.rootOperationID === execution.rootOperationID &&
                !!binding && !!now && !now.authority && !now.transition && markerMatches(ctx.sessionID,now) &&
                now.registrationMarker.token === binding.registrationMarker.token && now.owner === bootstrap.owner &&
                now.stableChatID === bootstrap.stableChatID && now.bindingRevision === bootstrap.workspaceRevision &&
                now.mappingRevision === bootstrap.mappingRevision && now.mapRevision >= bootstrap.mapRevision &&
                now.authorityWorkspaceID === binding.authorityWorkspaceID && now.physicalCwd === binding.physicalCwd &&
                now.physicalCwd === instance.directory && !!now.authorityWorkspaceID && !!originalOwnerAccountID &&
                host?.ownerAccountID === originalOwnerAccountID && liveHost?.ownerAccountID === originalOwnerAccountID &&
                host.chatID === now.stableChatID && liveHost.chatID === now.stableChatID && !!issuer?.isAlive() &&
                ManagedProvider.currentActorToolTimingIssuer(ctx.sessionID,"main",execution.executionRevision,execution.rootOperationID!) === issuer &&
                ManagedProvider.workerNamingEnabled(execution) && isAdmittedToolTimingTurn(execution,turn) &&
                (!hostContext?.chatID || hostContext.chatID === now.stableChatID) &&
                (!hostContext?.workspaceID || hostContext.workspaceID === now.authorityWorkspaceID) &&
                (!hostContext?.cwd || hostContext.cwd === now.physicalCwd)
            } catch { return false }
          }
          if (binding && current()) derivedWorkspace={chatID:binding.stableChatID,workspaceID:binding.authorityWorkspaceID,current}
        }
        yield* ctx.metadata({ title: op.description, metadata: { workerBrief, requestedModel } })
        const spawnResult = yield* actor.spawn({
          mode: "subagent",
          sessionID: ctx.sessionID,
          agentType: next.name,
          description: op.description,
          task: prompt + briefInput,
          context: "none",
          tools: next.toolAllowlist ? [...next.toolAllowlist] : "INHERIT",
          model,
          requestedModel,
          ...(optionCandidate ? { optionCandidate } : {}),
          ...(parentActorID ? { parentActorID } : {}),
          ...(hostContext ? { hostContext } : {}),
          ...(derivedWorkspace ? { derivedWorkspace } : {}),
          ...(originalOwnerAccountID ? { originalOwnerAccountID } : {}),
          ...(op.actor_id ? { actorID: op.actor_id } : {}),
          ...(variant !== undefined ? { variant } : {}),
          // run owns an interruptible wait; the child must outlive it and still notify.
          background: true,
          notifyOnCompletion,
          task_id: effectiveTaskId,
          onActorID: (id) => {
            actorID = id
            if (op.action === "run" && ctx.abort.aborted)
              bridge.fork(actor.cancel(ctx.sessionID, id, "graceful"))
          },
          onReady: ({ actorID, sessionID }) =>
            ctx.metadata({
              title: op.description,
              metadata: { sessionId: sessionID, actorId: actorID, workerBrief, requestedModel },
            }),
          ...(!priorActor && !op.actor_id && execution && ctx.callID && ManagedProvider.workerNamingEnabled(execution) ? {
            onReadyBackground: ({actorID,sessionID}: {actorID:string;sessionID:SessionID}) => Effect.gen(function* () {
              const host=ManagedProvider.currentActorHostContext(ctx.sessionID,execution.actorID,execution.executionRevision)
              const binding=managedSessionBinding(ctx.sessionID)
              if (!host?.ownerAccountID || !host.chatID || !binding || !binding.authorityWorkspaceID || !ctx.callID) return
              const candidate=yield* actorRegistry.workerNamingCandidate({sessionID,actorID,accountID:host.ownerAccountID,
                chatID:host.chatID,workspaceID:binding.authorityWorkspaceID})
              if (!candidate) return
              const token=captureWorkerNamingSource({identity:execution,messageID:ctx.messageID,toolCallID:ctx.callID,candidate,
                action:op.action,agent:op.subagent_type,description:op.description,task:op.prompt})
              if (!token) return
              yield* Effect.gen(function* () {
                const source=readWorkerNamingSource(token)
                if (!source) return
                const response=yield* Effect.tryPromise({try:()=>ManagedProvider.managedHostCall("_openclank/session/v1/worker-naming/request",{
                  version:1,sessionID:source.sessionID,rootOperationID:source.rootOperationID,actorID:"main",
                  executionRevision:source.executionRevision,innerTurnID:source.innerTurnID,messageID:source.messageID,
                  toolCallID:source.toolCallID,childSessionID:source.childSessionID,childActorID:source.childActorID,
                  childExecutionRevision:source.childExecutionRevision,displayRevision:source.displayRevision,sourceToken:token,
                }),catch:()=>undefined}).pipe(Effect.orElseSucceed(()=>undefined))
                if (response?.state!=="named" || !workerNamingSourceCurrent(token)) return
                yield* actorRegistry.applyWorkerName(candidate,response.summary,()=>workerNamingSourceCurrent(token))
              }).pipe(Effect.ensuring(Effect.sync(()=>releaseWorkerNamingSource(token))))
            })
          } : {}),
          ...(op.output_schema
            ? { format: { type: "json_schema" as const, schema: op.output_schema, retryCount: 2 } }
            : {}),
        })

        if (op.action ==="spawn") {
          return {
            title: op.description,
            metadata: {
              sessionId: spawnResult.sessionID,
              actorId: spawnResult.actorID,
              workerBrief,
              ...(spawnResult.effectiveModel ? { model: spawnResult.effectiveModel } : {}),
              ...(spawnResult.requestedModel ? { requestedModel: spawnResult.requestedModel } : {}),
              ...(spawnResult.hostContext ? { hostContext: spawnResult.hostContext } : {}),
            },
            output:
              (taskNotice ? taskNotice + "\n" : "") +
              `Background sub-session started. actor_id: ${spawnResult.actorID}\nUse the recorded worker identity to inspect its result or send a follow-up.`,
          }
        }

        const snap = yield* waiter.wait({
          sessionID: spawnResult.sessionID,
          actor_id: spawnResult.actorID,
          timeout_ms: op.timeout_ms,
          afterRevision,
        }).pipe(Effect.raceFirst(Deferred.await(spawnResult.outcome).pipe(Effect.as(undefined))))
        if (snap && (snap.status === "interrupted" || snap.status === "timeout")) {
          if (notifyOnCompletion) yield* Deferred.succeed(notifyOnCompletion, !ctx.abort.aborted)
          return {
            title: snap.status === "interrupted" ? "Actor run: interrupted by user message" : "Actor run: timeout",
            metadata: {
              sessionId: spawnResult.sessionID,
              actorId: spawnResult.actorID,
              actor_id: spawnResult.actorID,
              status: snap.status,
              workerBrief,
              ...(spawnResult.effectiveModel ? { model:spawnResult.effectiveModel } : {}),
            } as Record<string, any>,
            output:
              (taskNotice ? taskNotice + "\n" : "") +
              (snap.status === "interrupted"
                ? JSON.stringify(snap) + "\n" + ActorWaiter.WAIT_INTERRUPTED_HINT
                : JSON.stringify({ ...snap, hint: ActorWaiter.WAIT_TIMEOUT_HINT })),
          }
        }
        // Only spawn's reconciled outcome is authoritative. If execution exits
        // without publishing it, never wait again or substitute a registry outcome.
        if (!(yield* Deferred.isDone(spawnResult.outcome))) {
          return yield* Effect.fail(
            new Error(`Actor ${spawnResult.actorID} exited without an authoritative outcome: ${JSON.stringify(snap)}`),
          )
        }
        const outcome = yield* Deferred.await(spawnResult.outcome)

        // Blocking run preserves the pre-unification contract: tool call fails
        // when the child fails. The LLM sees a tool error, not a "success with
        // error in output." (The explicit action="wait" returns the structured
        // snapshot as a regular tool result — that's a different contract.)
        if (outcome.status === "failure") {
          const partial = outcome.structured !== undefined ? JSON.stringify(outcome.structured) : outcome.finalText
          return yield* Effect.fail(new Error(`Tool execution failed: ${outcome.error ?? "unknown"}${partial === undefined ? "" : `\nPartial result: ${partial}`}`))
        }

        const resultText =
          outcome.status === "success"
            ? outcome.structured !== undefined
              ? JSON.stringify(outcome.structured)
              : (outcome.finalText ?? "(no output)")
            : "<cancelled>task was cancelled</cancelled>"
        const statusAttr = outcome.status === "success" ? (outcome.reportedStatus ?? "unknown") : outcome.status
        const summaryAttr =
          outcome.status === "success" && outcome.reportedSummary
            ? ` summary="${outcome.reportedSummary.replace(/\s+/g, " ").replace(/"/g, "'").trim()}"`
            : ""
        return {
          title: op.description,
          metadata: {
            sessionId: spawnResult.sessionID,
            actorId: spawnResult.actorID,
            workerBrief,
              ...(spawnResult.effectiveModel ? { model: spawnResult.effectiveModel } : {}),
            ...(spawnResult.requestedModel ? { requestedModel: spawnResult.requestedModel } : {}),
            ...(spawnResult.hostContext ? { hostContext: spawnResult.hostContext } : {}),
          } as Record<string, any>,
          output: [
            ...(taskNotice ? [taskNotice, ""] : []),
            `actor_id: ${spawnResult.actorID} (to give this subagent more work, \`send\` it another message)`,
            "",
            `<actor_result status="${statusAttr}"${summaryAttr}>`,
            resultText,
            "</actor_result>",
            ...(outcome.status === "success" ? (outcome.warnings ?? []).map((warning) => `Warning: ${warning}`) : []),
          ].join("\n"),
        }
      }, Effect.scoped)

      return {
        description: withCheckpointDescription(DESCRIPTION, DESCRIPTION_CHECKPOINT),
        parameters,
        execute: (input: z.infer<typeof parameters>, ctx: Tool.Context) => run(input, ctx).pipe(Effect.orDie),
      }
    })
  }),
)
