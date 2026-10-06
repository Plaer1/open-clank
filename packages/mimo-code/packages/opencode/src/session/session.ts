import { recordSdkUsage } from "@/provider/logging-transport"
import { Slug } from "@mimo-ai/shared/util/slug"
import path from "path"
import { BusEvent } from "@/bus/bus-event"
import { Bus } from "@/bus"
import { Decimal } from "decimal.js"
import z from "zod"
import { type ProviderMetadata, type LanguageModelUsage } from "ai"
import { Flag } from "../flag/flag"
import { InstallationVersion } from "../installation/version"

import { Database, NotFoundError, eq, and, gte, isNull, desc, like, inArray, lt } from "../storage"
import { SyncEvent } from "../sync"
import type { SQL } from "../storage"
import { PartTable, SessionTable, MessageTable } from "./session.sql"
import { ActorRegistryTable } from "../actor/actor.sql"
import { ProjectTable } from "../project/project.sql"
import { Storage } from "@/storage"
import { Log } from "../util"
import { updateSchema } from "../util/update-schema"
import { MessageV2 } from "./message-v2"
import { Instance } from "../project/instance"
import { InstanceState } from "@/effect"
import { Snapshot } from "@/snapshot"
import { ProjectID } from "../project/schema"
import { WorkspaceID } from "../control-plane/schema"
import { SessionID, MessageID, PartID } from "./schema"

import type { Provider } from "@/provider"
import { Permission } from "@/permission"
import { forwardRef } from "@/permission/permission-forward-ref"
import { Global } from "@/global"
import { ActorRegistry } from "@/actor/registry"
import { Effect, Layer, Option, Context } from "effect"

const log = Log.create({ service: "session" })

const parentTitlePrefix = "New session - "
const childTitlePrefix = "Child session - "

function createDefaultTitle(isChild = false) {
  return isChild ? childTitlePrefix + new Date().toISOString() : "Untitled"
}

export function isDefaultTitle(title: string) {
  return new RegExp(
    `^(?:Untitled|${parentTitlePrefix}\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}\\.\\d{3}Z|${childTitlePrefix}\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}\\.\\d{3}Z)$`,
  ).test(title)
}

type SessionRow = typeof SessionTable.$inferSelect

export function fromRow(row: SessionRow): Info {
  const summary =
    row.summary_additions !== null || row.summary_deletions !== null || row.summary_files !== null
      ? {
          additions: row.summary_additions ?? 0,
          deletions: row.summary_deletions ?? 0,
          files: row.summary_files ?? 0,
          diffs: row.summary_diffs ?? undefined,
        }
      : undefined
  const share = row.share_url ? { url: row.share_url } : undefined
  const revert = row.revert ?? undefined
  return {
    id: row.id,
    slug: row.slug,
    projectID: row.project_id,
    workspaceID: row.workspace_id ?? undefined,
    directory: row.directory,
    parentID: row.parent_id ?? undefined,
    contextFrom: row.context_from ?? undefined,
    contextWatermark: row.context_watermark ?? undefined,
    title: row.title,
    titleSource: row.title_source,
    titleRevision: row.title_revision,
    version: row.version,
    summary,
    share,
    revert,
    permission: row.permission ?? undefined,
    time: {
      created: row.time_created,
      updated: row.time_updated,
      compacting: row.time_compacting ?? undefined,
      archived: row.time_archived ?? undefined,
    },
  }
}

export function toRow(info: Info) {
  return {
    id: info.id,
    project_id: info.projectID,
    workspace_id: info.workspaceID,
    parent_id: info.parentID,
    context_from: info.contextFrom,
    context_watermark: info.contextWatermark,
    slug: info.slug,
    directory: info.directory,
    title: info.title || "Untitled",
    // Legacy imports/events have no provenance: protect their nonempty title.
    title_source: info.titleSource ?? (info.title ? "user" : "fallback"),
    title_revision: info.titleRevision ?? 0,
    version: info.version,
    share_url: info.share?.url,
    summary_additions: info.summary?.additions,
    summary_deletions: info.summary?.deletions,
    summary_files: info.summary?.files,
    summary_diffs: info.summary?.diffs,
    revert: info.revert ?? null,
    permission: info.permission,
    time_created: info.time.created,
    time_updated: info.time.updated,
    time_compacting: info.time.compacting,
    time_archived: info.time.archived,
  }
}

function getForkedTitle(title: string): string {
  const match = title.match(/^(.+) \(fork #(\d+)\)$/)
  if (match) {
    const base = match[1]
    const num = parseInt(match[2], 10)
    return `${base} (fork #${num + 1})`
  }
  return `${title} (fork #1)`
}

export const Info = z
  .object({
    id: SessionID.zod,
    slug: z.string(),
    projectID: ProjectID.zod,
    workspaceID: WorkspaceID.zod.optional(),
    directory: z.string(),
    parentID: SessionID.zod.optional(),
    contextFrom: SessionID.zod.optional(),
    contextWatermark: MessageID.zod.optional(),
    summary: z
      .object({
        additions: z.number(),
        deletions: z.number(),
        files: z.number(),
        diffs: Snapshot.FileDiff.array().optional(),
      })
      .optional(),
    share: z
      .object({
        url: z.string(),
      })
      .optional(),
    title: z.string(),
    titleSource: z.enum(["fallback", "generated", "user"]),
    titleRevision: z.number().int().nonnegative().max(Number.MAX_SAFE_INTEGER),
    version: z.string(),
    time: z.object({
      created: z.number(),
      updated: z.number(),
      compacting: z.number().optional(),
      archived: z.number().optional(),
    }),
    permission: Permission.Ruleset.zod.optional(),
    revert: z
      .object({
        messageID: MessageID.zod,
        partID: PartID.zod.optional(),
        snapshot: z.string().optional(),
        diff: z.string().optional(),
      })
      .optional(),
  })
  .meta({
    ref: "Session",
  })
export type Info = z.output<typeof Info>

export const ProjectInfo = z
  .object({
    id: ProjectID.zod,
    name: z.string().optional(),
    worktree: z.string(),
  })
  .meta({
    ref: "ProjectSummary",
  })
export type ProjectInfo = z.output<typeof ProjectInfo>

export const GlobalInfo = Info.extend({
  project: ProjectInfo.nullable(),
}).meta({
  ref: "GlobalSession",
})
export type GlobalInfo = z.output<typeof GlobalInfo>

export const ManualTitle = z.string().trim().min(1).transform(value => Array.from(value).slice(0, 80).join(""))

export const CreateInput = z
  .object({
    id: SessionID.zod.optional(),
    parentID: SessionID.zod.optional(),
    contextFrom: SessionID.zod.optional(),
    contextWatermark: MessageID.zod.optional(),
    title: ManualTitle.optional(),
    permission: Info.shape.permission,
    workspaceID: WorkspaceID.zod.optional(),
  })
  .optional()
export type CreateInput = z.output<typeof CreateInput>

export const ForkInput = z.object({ sessionID: SessionID.zod, messageID: MessageID.zod.optional(), title: ManualTitle.optional() })
export const GetInput = SessionID.zod
export const ChildrenInput = SessionID.zod
export const RemoveInput = SessionID.zod
export const TitleSnapshot = z.object({
  sessionID: SessionID.zod,
  title: z.string(),
  titleSource: Info.shape.titleSource,
  titleRevision: Info.shape.titleRevision,
}).meta({ ref: "TitleSnapshot" })
export type TitleSnapshot = z.infer<typeof TitleSnapshot>
export const TitleConflict = z.object({ name: z.literal("TitleConflictError"), data: z.object({ current: TitleSnapshot }) })
export class TitleRevisionError extends Error { override readonly name = "TitleRevisionError" }
export class TitleConflictError extends Error {
  override readonly name = "TitleConflictError"
  constructor(readonly current: TitleSnapshot) { super("Title changed by another writer") }
  toObject() { return { name: this.name, data: { current: this.current } } }
}
export const SetTitleInput = z.object({ sessionID: SessionID.zod, title: ManualTitle, expectedRevision: Info.shape.titleRevision }).strict()
export type SetTitleInput = z.infer<typeof SetTitleInput>
export type AutoTitleInput = SetTitleInput & { source?: "fallback" | "generated" }

export function titleSnapshot(info: Info): TitleSnapshot {
  return { sessionID: info.id, title: info.title, titleSource: info.titleSource, titleRevision: info.titleRevision }
}

// The read, arbitration, projection and optional event log share this immediate
// transaction. No process-local mutex can arbitrate a second SQLite connection.
export function writeTitle(input: SetTitleInput, source: "user" | "fallback" | "generated", mode: "initial" | "machine" = "initial"): TitleSnapshot | false {
  const parsed = SetTitleInput.parse(input)
  return Database.transaction((db) => {
    const row = db.select().from(SessionTable).where(eq(SessionTable.id, parsed.sessionID)).get()
    if (!row) throw new NotFoundError({ message: `Session not found: ${parsed.sessionID}` })
    const current = titleSnapshot(fromRow(row))
    if (source === "user") {
      if (parsed.expectedRevision > current.titleRevision) throw new TitleRevisionError("expectedRevision is in the future")
      if (parsed.expectedRevision < current.titleRevision && current.titleSource === "user") throw new TitleConflictError(current)
    } else if (current.titleSource === "user" || (mode === "initial" && current.titleSource !== "fallback") || parsed.expectedRevision !== current.titleRevision) return false
    // Revision zero is uninitialized; the first fallback commit records completion even for identical text.
    if (((source === "fallback" && current.titleRevision > 0) || mode === "machine") && current.title === parsed.title) return false
    if (current.titleRevision === Number.MAX_SAFE_INTEGER) throw new RangeError("Title revision exhausted")
    const next = { ...current, title: parsed.title, titleSource: source, titleRevision: current.titleRevision + 1 }
    SyncEvent.run(Event.Updated, {
      sessionID: parsed.sessionID,
      previousRevision: current.titleRevision,
      info: { title: next.title, titleSource: next.titleSource, titleRevision: next.titleRevision },
    })
    return next
  }, { behavior: "immediate" })
}
export const SetArchivedInput = z.object({ sessionID: SessionID.zod, time: z.number().optional() })
export const SetPermissionInput = z.object({ sessionID: SessionID.zod, permission: Permission.Ruleset.zod })
export const SetRevertInput = z.object({
  sessionID: SessionID.zod,
  revert: Info.shape.revert,
  summary: Info.shape.summary,
})
export const MessagesInput = z.object({ sessionID: SessionID.zod, limit: z.number().optional() })

// v1 is replay-only. Its already accepted but provenance-less titles are
// conservatively protected. New commands may only emit the revisioned v2 event.
export const LegacyUpdated = SyncEvent.define({
  type: "session.updated", version: 1, aggregate: "sessionID",
  schema: z.object({ sessionID: SessionID.zod, info: updateSchema(Info.omit({ titleSource: true, titleRevision: true })).extend({ share: updateSchema(Info.shape.share.unwrap()).optional(), time: updateSchema(Info.shape.time).optional() }) }),
})

export const Event = {
  Created: SyncEvent.define({
    type: "session.created",
    version: 1,
    aggregate: "sessionID",
    schema: z.object({
      sessionID: SessionID.zod,
      info: Info,
    }),
  }),
  Updated: SyncEvent.define({
    type: "session.updated",
    version: 2,
    aggregate: "sessionID",
    schema: z.object({
      sessionID: SessionID.zod,
      previousRevision: Info.shape.titleRevision.optional(),
      info: updateSchema(Info).extend({
        share: updateSchema(Info.shape.share.unwrap()).optional(),
        time: updateSchema(Info.shape.time).optional(),
      }),
    }),
    busSchema: z.object({
      sessionID: SessionID.zod,
      info: Info,
    }),
  }),
  Deleted: SyncEvent.define({
    type: "session.deleted",
    version: 1,
    aggregate: "sessionID",
    schema: z.object({
      sessionID: SessionID.zod,
      info: Info,
    }),
  }),
  Diff: BusEvent.define(
    "session.diff",
    z.object({
      sessionID: SessionID.zod,
      diff: Snapshot.FileDiff.array(),
    }),
  ),
  Error: BusEvent.define(
    "session.error",
    z.object({
      sessionID: SessionID.zod.optional(),
      // z.lazy defers access to break circular dep: session → message-v2 → provider → plugin → session
      error: z.lazy(() => MessageV2.Assistant.shape.error),
    }),
  ),
  RetryAttempt: BusEvent.define(
    "session.retry.attempt",
    z.object({
      sessionID: SessionID.zod,
      messageID: z.string(),
      attempt: z.number().int().min(1),
      maxAttempts: z.number().int().min(1),
      reason: z.string(),
      nextDelayMs: z.number().int().nonnegative(),
    }),
  ),
  TryBestDetected: BusEvent.define(
    "session.try_best.detected",
    z.object({
      sessionID: SessionID.zod,
      agentID: z.string().optional(),
      providerID: z.string(),
      modelID: z.string(),
      reason: z.enum(["edit_repeat", "bash_retry", "action_streak"]),
      evidence: z.object({
        tool: z.string(),
        path: z.string().optional(),
        command: z.string().optional(),
        count: z.number().int().positive(),
        similarity: z.number().min(0).max(1).optional(),
        action: z.enum(["edit", "verify"]).optional(),
      }),
    }),
  ),
}

export function plan(input: { slug: string; time: { created: number } }) {
  const base = Instance.project.vcs
    ? path.join(Instance.worktree, ".mimocode", "plans")
    : path.join(Global.Path.data, "plans")
  return path.join(base, [input.time.created, input.slug].join("-") + ".md")
}

export const getUsage = (input: {
  model: Provider.Model
  usage: LanguageModelUsage
  metadata?: ProviderMetadata
  captureScope?: object
  captureDispatchIDs?: string[]
  mainReply?: boolean
}) => {
  const providerMetadata = input.metadata as
    | Record<
        string,
        {
          cacheCreationInputTokens?: unknown
          usage?: { cacheWriteInputTokens?: unknown; cacheCreationInputTokens?: unknown }
        }
      >
    | undefined
  const reportedCacheWrite =
    input.usage.inputTokenDetails?.cacheWriteTokens ??
    providerMetadata?.anthropic?.cacheCreationInputTokens ??
    providerMetadata?.vertex?.cacheCreationInputTokens ??
    providerMetadata?.bedrock?.usage?.cacheWriteInputTokens ??
    providerMetadata?.venice?.usage?.cacheCreationInputTokens
  if (input.captureDispatchIDs) recordSdkUsage(input.captureScope, {
    ...input.usage,
    inputTokenDetails: { ...input.usage.inputTokenDetails, cacheWriteTokens: reportedCacheWrite },
  }, input.captureDispatchIDs, input.mainReply)
  const safe = (value: number) => {
    if (!Number.isFinite(value)) return 0
    return value
  }
  const inputTokens = safe(input.usage.inputTokens ?? 0)
  const outputTokens = safe(input.usage.outputTokens ?? 0)
  const reasoningTokens = safe(input.usage.outputTokenDetails?.reasoningTokens ?? input.usage.reasoningTokens ?? 0)

  const cacheReadInputTokens = safe(
    input.usage.inputTokenDetails?.cacheReadTokens ?? input.usage.cachedInputTokens ?? 0,
  )
  const cacheWriteInputTokens = safe(
    Number(
      input.usage.inputTokenDetails?.cacheWriteTokens ??
        input.metadata?.["anthropic"]?.["cacheCreationInputTokens"] ??
        // google-vertex-anthropic returns metadata under "vertex" key
        // (AnthropicMessagesLanguageModel custom provider key from 'vertex.anthropic.messages')
        input.metadata?.["vertex"]?.["cacheCreationInputTokens"] ??
        // @ts-expect-error
        input.metadata?.["bedrock"]?.["usage"]?.["cacheWriteInputTokens"] ??
        // @ts-expect-error
        input.metadata?.["venice"]?.["usage"]?.["cacheCreationInputTokens"] ??
        0,
    ),
  )

  // AI SDK v6 normalized inputTokens to include cached tokens across all providers
  // (including Anthropic/Bedrock which previously excluded them). Always subtract cache
  // tokens to get the non-cached input count for separate cost calculation.
  const adjustedInputTokens = safe(inputTokens - cacheReadInputTokens - cacheWriteInputTokens)

  const total = input.usage.totalTokens

  const tokens = {
    total,
    input: adjustedInputTokens,
    output: safe(outputTokens - reasoningTokens),
    reasoning: reasoningTokens,
    cache: {
      write: cacheWriteInputTokens,
      read: cacheReadInputTokens,
    },
  }

  const costInfo =
    input.model.cost?.experimentalOver200K && tokens.input + tokens.cache.read > 200_000
      ? input.model.cost.experimentalOver200K
      : input.model.cost
  return {
    cost: safe(
      new Decimal(0)
        .add(new Decimal(tokens.input).mul(costInfo?.input ?? 0).div(1_000_000))
        .add(new Decimal(tokens.output).mul(costInfo?.output ?? 0).div(1_000_000))
        .add(new Decimal(tokens.cache.read).mul(costInfo?.cache?.read ?? 0).div(1_000_000))
        .add(new Decimal(tokens.cache.write).mul(costInfo?.cache?.write ?? 0).div(1_000_000))
        // TODO: update models.dev to have better pricing model, for now:
        // charge reasoning tokens at the same rate as output tokens
        .add(new Decimal(tokens.reasoning).mul(costInfo?.output ?? 0).div(1_000_000))
        .toNumber(),
    ),
    tokens,
  }
}

export class BusyError extends Error {
  constructor(public readonly sessionID: string) {
    super(`Session ${sessionID} is busy`)
  }
}

export interface Interface {
  readonly create: (input?: {
    id?: SessionID
    parentID?: SessionID
    contextFrom?: SessionID
    contextWatermark?: MessageID
    title?: string
    permission?: Permission.Ruleset
    workspaceID?: WorkspaceID
  }) => Effect.Effect<Info>
  readonly fork: (input: { sessionID: SessionID; messageID?: MessageID; title?: string }) => Effect.Effect<Info>
  readonly touch: (sessionID: SessionID) => Effect.Effect<void>
  readonly get: (id: SessionID) => Effect.Effect<Info>
  readonly setTitle: (input: SetTitleInput) => Effect.Effect<TitleSnapshot>
  readonly setTitleIfDefault: (input: AutoTitleInput) => Effect.Effect<boolean>
  readonly setGeneratedTitle: (input: SetTitleInput) => Effect.Effect<TitleSnapshot | false>
  readonly setArchived: (input: { sessionID: SessionID; time?: number }) => Effect.Effect<void>
  readonly setPermission: (input: { sessionID: SessionID; permission: Permission.Ruleset }) => Effect.Effect<void>
  readonly setRevert: (input: {
    sessionID: SessionID
    revert: Info["revert"]
    summary: Info["summary"]
  }) => Effect.Effect<void>
  readonly clearRevert: (sessionID: SessionID) => Effect.Effect<void>
  readonly setSummary: (input: { sessionID: SessionID; summary: Info["summary"] }) => Effect.Effect<void>
  readonly diff: (sessionID: SessionID) => Effect.Effect<Snapshot.FileDiff[]>
  readonly messages: (input: {
    sessionID: SessionID
    limit?: number
    /**
     * Slice selector.
     * `undefined` (default) returns the main-agent slice only.
     * `"main"` is equivalent to `undefined`.
     * `"*"` returns every message in the session, regardless of slice
     * (export / stats / share / cross-slice diagnostic paths only —
     * almost no production caller wants this).
     * Any other string returns the slice owned by that subagent actor
     * (`agent_id = <id>`).
     */
    agentID?: string
  }) => Effect.Effect<MessageV2.WithParts[]>
  readonly children: (parentID: SessionID, options?: { visible?: boolean }) => Effect.Effect<Info[]>
  readonly remove: (sessionID: SessionID) => Effect.Effect<void>
  readonly updateMessage: <T extends MessageV2.Info>(msg: T) => Effect.Effect<T>
  readonly removeMessage: (input: { sessionID: SessionID; messageID: MessageID }) => Effect.Effect<MessageID>
  readonly removePart: (input: { sessionID: SessionID; messageID: MessageID; partID: PartID }) => Effect.Effect<PartID>
  readonly getPart: (input: {
    sessionID: SessionID
    messageID: MessageID
    partID: PartID
  }) => Effect.Effect<MessageV2.Part | undefined>
  readonly updatePart: <T extends MessageV2.Part>(part: T) => Effect.Effect<T>
  readonly updatePartDelta: (input: {
    sessionID: SessionID
    messageID: MessageID
    partID: PartID
    field: string
    delta: string
  }) => Effect.Effect<void>
  /** Finds the first message matching the predicate, searching newest-first.
   *
   * Slice contract: `options.agentID` defaults to `"main"` (mirrors `messages()`).
   * Pass `"*"` for cross-slice lookup. */
  readonly findMessage: (
    sessionID: SessionID,
    predicate: (msg: MessageV2.WithParts) => boolean,
    options?: { agentID?: string },
  ) => Effect.Effect<Option.Option<MessageV2.WithParts>>
  readonly lastMainMessageID: (sessionID: SessionID) => Effect.Effect<MessageID | undefined>
}

export class Service extends Context.Service<Service, Interface>()("@opencode/Session") {}

type Patch = Omit<z.infer<typeof Event.Updated.schema>["info"], "title" | "titleSource" | "titleRevision">

const db = <T>(fn: (d: Parameters<typeof Database.use>[0] extends (trx: infer D) => any ? D : never) => T) =>
  Effect.sync(() => Database.use(fn))

export const layer: Layer.Layer<Service, never, Bus.Service | Storage.Service | ActorRegistry.Service> = Layer.effect(
  Service,
  Effect.gen(function* () {
    const bus = yield* Bus.Service
    const storage = yield* Storage.Service
    const actorReg = yield* ActorRegistry.Service

    const createNext = Effect.fn("Session.createNext")(function* (input: {
      id?: SessionID
      title?: string
      parentID?: SessionID
      contextFrom?: SessionID
      contextWatermark?: MessageID
      workspaceID?: WorkspaceID
      directory: string
      permission?: Permission.Ruleset
    }) {
      const ctx = yield* InstanceState.context
      const result: Info = {
        id: SessionID.descending(input.id),
        slug: Slug.create(),
        version: InstallationVersion,
        projectID: ctx.project.id,
        directory: input.directory,
        workspaceID: input.workspaceID,
        parentID: input.parentID,
        contextFrom: input.contextFrom,
        contextWatermark: input.contextWatermark,
        title: input.title === undefined ? createDefaultTitle(!!input.parentID) : ManualTitle.parse(input.title),
        titleSource: input.title !== undefined ? "user" : "fallback",
        titleRevision: 0,
        permission: input.permission,
        time: {
          created: Date.now(),
          updated: Date.now(),
        },
      }
      log.info("created", result)

      yield* Effect.sync(() => SyncEvent.run(Event.Created, { sessionID: result.id, info: result }))

      yield* actorReg.register({
        sessionID: result.id,
        actorID: "main",
        mode: "main",
        parentActorID: undefined,
        agent: "main",
        description: "main agent",
        contextMode: "full",
        contextWatermark: undefined,
        background: false,
        lifecycle: "persistent",
        tools: "INHERIT",
      }).pipe(Effect.ignore)

      if (!Flag.MIMOCODE_EXPERIMENTAL_WORKSPACES) {
        // This only exist for backwards compatibility. We should not be
        // manually publishing this event; it is a sync event now
        yield* bus.publish(Event.Updated, {
          sessionID: result.id,
          info: result,
        })
      }

      return result
    })

    const get = Effect.fn("Session.get")(function* (id: SessionID) {
      const row = yield* db((d) => d.select().from(SessionTable).where(eq(SessionTable.id, id)).get())
      if (!row) throw new NotFoundError({ message: `Session not found: ${id}` })
      return fromRow(row)
    })

    const children = Effect.fn("Session.children")(function* (parentID: SessionID, options?: { visible?: boolean }) {
      const rows = yield* db((d) =>
        d
          .select()
          .from(SessionTable)
          .where(and(eq(SessionTable.parent_id, parentID)))
          .all(),
      )
      if (!options?.visible) return rows.map(fromRow)
      if (!rows.length) return []
      // visible: only children a user should see in session lists. Peer actors
      // register under the child session with actor_id === session id; internal
      // machinery children (checkpoint-writer hosts, ask-tool forks, workflow
      // subagent sessions) register as mode "subagent" or have no actor row at
      // all — both are filtered out.
      const peerRows = yield* db((d) =>
        d
          .select({ session_id: ActorRegistryTable.session_id })
          .from(ActorRegistryTable)
          .where(and(inArray(ActorRegistryTable.session_id, rows.map((r) => r.id)), eq(ActorRegistryTable.mode, "peer")))
          .all(),
      )
      const peers = new Set(peerRows.map((r) => r.session_id))
      return rows.filter((r) => peers.has(r.id)).map(fromRow)
    })

    const remove: Interface["remove"] = Effect.fnUntraced(function* (sessionID: SessionID) {
      try {
        const session = yield* get(sessionID)
        const kids = yield* children(sessionID)
        for (const child of kids) {
          yield* remove(child.id)
        }

        // `remove` needs to work in all cases, such as a broken
        // sessions that run cleanup. In certain cases these will
        // run without any instance state, so we need to turn off
        // publishing of events in that case
        const hasInstance = yield* InstanceState.directory.pipe(
          Effect.as(true),
          Effect.catchCause(() => Effect.succeed(false)),
        )

        yield* Effect.sync(() => {
          SyncEvent.run(Event.Deleted, { sessionID, info: session }, { publish: hasInstance })
          SyncEvent.remove(sessionID)
          // Drop this session's published parent-grant snapshot. ask() populates
          // it process-wide on every call (before the needsAsk short-circuit), so
          // without this the map grows one entry per session for the life of the
          // process. Cleared here — the removal point — since a deleted session
          // can no longer have background children that need to inherit from it.
          forwardRef.clearParentGrants(sessionID)
        })
      } catch (e) {
        log.error(e)
      }
    })

    const updateMessage = <T extends MessageV2.Info>(msg: T): Effect.Effect<T> =>
      Effect.gen(function* () {
        yield* Effect.sync(() => SyncEvent.run(MessageV2.Event.Updated, { sessionID: msg.sessionID, info: msg }))
        return msg
      }).pipe(Effect.withSpan("Session.updateMessage"))

    const updatePart = <T extends MessageV2.Part>(part: T): Effect.Effect<T> =>
      Effect.gen(function* () {
        yield* Effect.sync(() =>
          SyncEvent.run(MessageV2.Event.PartUpdated, {
            sessionID: part.sessionID,
            part: structuredClone(part),
            time: Date.now(),
          }),
        )
        return part
      }).pipe(Effect.withSpan("Session.updatePart"))

    const getPart: Interface["getPart"] = Effect.fn("Session.getPart")(function* (input) {
      const row = Database.use((db) =>
        db
          .select()
          .from(PartTable)
          .where(
            and(
              eq(PartTable.session_id, input.sessionID),
              eq(PartTable.message_id, input.messageID),
              eq(PartTable.id, input.partID),
            ),
          )
          .get(),
      )
      if (!row) return
      return {
        ...row.data,
        id: row.id,
        sessionID: row.session_id,
        messageID: row.message_id,
      } as MessageV2.Part
    })

    const create = Effect.fn("Session.create")(function* (input?: {
      id?: SessionID
      parentID?: SessionID
      contextFrom?: SessionID
      contextWatermark?: MessageID
      title?: string
      // In-process only (deliberately NOT on the public CreateInput / HTTP body,
      // where it would collide with the route's `directory` query selector). Set
      // once at creation by an in-process caller — e.g. spawnPeer placing a child
      // session in its own worktree dir. Defaults to the current instance dir.
      directory?: string
      permission?: Permission.Ruleset
      workspaceID?: WorkspaceID
    }) {
      const directory = input?.directory ?? (yield* InstanceState.directory)
      const workspace = yield* InstanceState.workspaceID
      return yield* createNext({
        id: input?.id,
        parentID: input?.parentID,
        contextFrom: input?.contextFrom,
        contextWatermark: input?.contextWatermark,
        directory,
        title: input?.title,
        permission: input?.permission,
        workspaceID: workspace,
      })
    })

    const fork = Effect.fn("Session.fork")(function* (input: { sessionID: SessionID; messageID?: MessageID; title?: string }) {
      const directory = yield* InstanceState.directory
      const original = yield* get(input.sessionID)
      const title = input.title?.trim() ? ManualTitle.parse(input.title) : getForkedTitle(original.title)
      const session = yield* createNext({
        directory,
        workspaceID: original.workspaceID,
        title,
      })
      const msgs = yield* messages({ sessionID: input.sessionID, agentID: "*" })
      const idMap = new Map<string, MessageID>()

      for (const msg of msgs) {
        if (input.messageID && msg.info.id >= input.messageID) break
        const newID = MessageID.ascending()
        idMap.set(msg.info.id, newID)

        const parentID = msg.info.role === "assistant" && msg.info.parentID ? idMap.get(msg.info.parentID) : undefined
        const cloned = yield* updateMessage({
          ...msg.info,
          sessionID: session.id,
          id: newID,
          ...(parentID && { parentID }),
        })

        for (const part of msg.parts) {
          yield* updatePart({
            ...part,
            id: PartID.ascending(),
            messageID: cloned.id,
            sessionID: session.id,
          })
        }
      }
      return session
    })

    const patch = (sessionID: SessionID, info: Patch) =>
      Effect.sync(() => SyncEvent.run(Event.Updated, { sessionID, info }))

    const touch = Effect.fn("Session.touch")(function* (sessionID: SessionID) {
      yield* patch(sessionID, { time: { updated: Date.now() } })
    })

    const setTitle = (input: SetTitleInput) => Effect.sync(() => writeTitle(input, "user") as TitleSnapshot)

    const setGeneratedTitle = (input: SetTitleInput) => Effect.sync(() => writeTitle(input, "generated", "machine"))

    const setTitleIfDefault = (input: AutoTitleInput) => Effect.sync(() => {
      const { source = "generated", ...command } = input
      return writeTitle(command, source) !== false
    })

    const setArchived = Effect.fn("Session.setArchived")(function* (input: { sessionID: SessionID; time?: number }) {
      yield* patch(input.sessionID, { time: { archived: input.time } })
    })

    const setPermission = Effect.fn("Session.setPermission")(function* (input: {
      sessionID: SessionID
      permission: Permission.Ruleset
    }) {
      yield* patch(input.sessionID, { permission: input.permission, time: { updated: Date.now() } })
    })

    const setRevert = Effect.fn("Session.setRevert")(function* (input: {
      sessionID: SessionID
      revert: Info["revert"]
      summary: Info["summary"]
    }) {
      yield* patch(input.sessionID, { summary: input.summary, time: { updated: Date.now() }, revert: input.revert })
    })

    const clearRevert = Effect.fn("Session.clearRevert")(function* (sessionID: SessionID) {
      yield* patch(sessionID, { time: { updated: Date.now() }, revert: null })
    })

    const setSummary = Effect.fn("Session.setSummary")(function* (input: {
      sessionID: SessionID
      summary: Info["summary"]
    }) {
      yield* patch(input.sessionID, { time: { updated: Date.now() }, summary: input.summary })
    })

    const diff = Effect.fn("Session.diff")(function* (sessionID: SessionID) {
      return yield* storage
        .read<Snapshot.FileDiff[]>(["session_diff", sessionID])
        .pipe(Effect.orElseSucceed((): Snapshot.FileDiff[] => []))
    })

    const messages = Effect.fn("Session.messages")(function* (input: {
      sessionID: SessionID
      limit?: number
      agentID?: string
    }) {
      if (input.limit) {
        return MessageV2.page({
          sessionID: input.sessionID,
          limit: input.limit,
          agentID: input.agentID,
        }).items
      }
      return Array.from(MessageV2.stream(input.sessionID, { agentID: input.agentID })).reverse()
    })

    const removeMessage = Effect.fn("Session.removeMessage")(function* (input: {
      sessionID: SessionID
      messageID: MessageID
    }) {
      yield* Effect.sync(() =>
        SyncEvent.run(MessageV2.Event.Removed, {
          sessionID: input.sessionID,
          messageID: input.messageID,
        }),
      )
      return input.messageID
    })

    const removePart = Effect.fn("Session.removePart")(function* (input: {
      sessionID: SessionID
      messageID: MessageID
      partID: PartID
    }) {
      yield* Effect.sync(() =>
        SyncEvent.run(MessageV2.Event.PartRemoved, {
          sessionID: input.sessionID,
          messageID: input.messageID,
          partID: input.partID,
        }),
      )
      return input.partID
    })

    const updatePartDelta = Effect.fnUntraced(function* (input: {
      sessionID: SessionID
      messageID: MessageID
      partID: PartID
      field: string
      delta: string
    }) {
      yield* bus.publish(MessageV2.Event.PartDelta, input)
    })

    /** Finds the first message matching the predicate, searching newest-first.
     *
     * Slice contract: `options.agentID` defaults to `"main"` (mirrors `messages()`).
     * Pass `"*"` for cross-slice lookup. */
    const findMessage = Effect.fn("Session.findMessage")(function* (
      sessionID: SessionID,
      predicate: (msg: MessageV2.WithParts) => boolean,
      options?: { agentID?: string },
    ) {
      for (const item of MessageV2.stream(sessionID, { agentID: options?.agentID })) {
        if (predicate(item)) return Option.some(item)
      }
      return Option.none<MessageV2.WithParts>()
    })

    const lastMainMessageID = Effect.fn("Session.lastMainMessageID")(function* (sessionID: SessionID) {
      const row = yield* db((d) =>
        d
          .select({ id: MessageTable.id })
          .from(MessageTable)
          .where(and(eq(MessageTable.session_id, sessionID), eq(MessageTable.agent_id, "main")))
          .orderBy(desc(MessageTable.id))
          .limit(1)
          .get(),
      )
      return row?.id
    })

    return Service.of({
      create,
      fork,
      touch,
      get,
      setTitle,
      setTitleIfDefault,
      setGeneratedTitle,
      setArchived,
      setPermission,
      setRevert,
      clearRevert,
      setSummary,
      diff,
      messages,
      children,
      remove,
      updateMessage,
      removeMessage,
      removePart,
      updatePart,
      getPart,
      updatePartDelta,
      findMessage,
      lastMainMessageID,
    })
  }),
)

export const defaultLayer = layer.pipe(
  Layer.provide(ActorRegistry.defaultLayer),
  Layer.provide(Bus.layer),
  Layer.provide(Storage.defaultLayer),
)

export function* list(input?: {
  directory?: string
  workspaceID?: WorkspaceID
  roots?: boolean
  start?: number
  search?: string
  limit?: number
}) {
  const project = Instance.project
  const conditions = [eq(SessionTable.project_id, project.id)]

  if (input?.workspaceID) {
    conditions.push(eq(SessionTable.workspace_id, input.workspaceID))
  }
  if (!Flag.MIMOCODE_EXPERIMENTAL_WORKSPACES) {
    if (input?.directory) {
      conditions.push(eq(SessionTable.directory, input.directory))
    }
  }
  if (input?.roots) {
    conditions.push(isNull(SessionTable.parent_id))
  }
  if (input?.start) {
    conditions.push(gte(SessionTable.time_updated, input.start))
  }
  if (input?.search) {
    conditions.push(like(SessionTable.title, `%${input.search}%`))
  }

  const limit = input?.limit ?? 100

  const rows = Database.use((db) =>
    db
      .select()
      .from(SessionTable)
      .where(and(...conditions))
      .orderBy(desc(SessionTable.time_updated))
      .limit(limit)
      .all(),
  )
  for (const row of rows) {
    yield fromRow(row)
  }
}

export function* listGlobal(input?: {
  directory?: string
  roots?: boolean
  start?: number
  cursor?: number
  search?: string
  limit?: number
  archived?: boolean
}) {
  const conditions: SQL[] = []

  if (input?.directory) {
    conditions.push(eq(SessionTable.directory, input.directory))
  }
  if (input?.roots) {
    conditions.push(isNull(SessionTable.parent_id))
  }
  if (input?.start) {
    conditions.push(gte(SessionTable.time_updated, input.start))
  }
  if (input?.cursor) {
    conditions.push(lt(SessionTable.time_updated, input.cursor))
  }
  if (input?.search) {
    conditions.push(like(SessionTable.title, `%${input.search}%`))
  }
  if (!input?.archived) {
    conditions.push(isNull(SessionTable.time_archived))
  }

  const limit = input?.limit ?? 100

  const rows = Database.use((db) => {
    const query =
      conditions.length > 0
        ? db
            .select()
            .from(SessionTable)
            .where(and(...conditions))
        : db.select().from(SessionTable)
    return query.orderBy(desc(SessionTable.time_updated), desc(SessionTable.id)).limit(limit).all()
  })

  const ids = [...new Set(rows.map((row) => row.project_id))]
  const projects = new Map<string, ProjectInfo>()

  if (ids.length > 0) {
    const items = Database.use((db) =>
      db
        .select({ id: ProjectTable.id, name: ProjectTable.name, worktree: ProjectTable.worktree })
        .from(ProjectTable)
        .where(inArray(ProjectTable.id, ids))
        .all(),
    )
    for (const item of items) {
      projects.set(item.id, {
        id: item.id,
        name: item.name ?? undefined,
        worktree: item.worktree,
      })
    }
  }

  for (const row of rows) {
    const project = projects.get(row.project_id) ?? null
    yield { ...fromRow(row), project }
  }
}
