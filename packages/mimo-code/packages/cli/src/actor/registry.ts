import { Effect, Layer, Context, Semaphore } from "effect"
import { Database, inArray, eq, and, gt, ne, sql } from "@/storage"
import { Bus } from "@/bus"
import type { SessionID, MessageID } from "@/session/schema"
import type { ModelID, ProviderID } from "@/provider/schema"
import { ActorForkContextTable, ActorRegistryTable } from "./actor.sql"
import type { EncodedForkContext, ForkContextSource, StoredForkContext } from "./fork-context"
import { SessionTable } from "@/session/session.sql"
import { UserInputHandoffTable, UserInputReceiptTable } from "@/inbox/inbox.sql"
import { boundaryContentHash, selectedExecutionRevision } from "@/session/user-input"
import type { Actor, ActorStatus, ActorOutcome, ContextMode, Lifecycle, SpawnMode, ToolWhitelist, Liveness, ActorHostContext } from "./schema"
import { deriveLiveness, DEFAULT_LIVENESS_ABANDON_MS, ActorModel, ActorModelOptions } from "./schema"
import * as Events from "./events"
import { SYSTEM_SPAWNED_AGENT_TYPES } from "@/agent/config"
import { randomUUID } from "node:crypto"
import { createHash } from "node:crypto"
import { isDeepStrictEqual } from "node:util"
import path from "node:path"

// Identifies the registering process; a different token does not prove termination.
const PROCESS_INSTANCE_ID = randomUUID()

/** Private exclusion identity; callers must independently prove filesystem and account authority. */
export type DirectoryIdentity = Readonly<{
  project: string
  directory: string
  commonDir: string
  directoryDevice: number
  directoryInode: number
  commonDevice: number
  commonInode: number
}>
export type DirectoryLease = Readonly<{ release: () => boolean }>
/** Internal guard retention; true leaves closing fenced until this process exits. */
export type DirectoryClosingRetention = { retain: boolean; globalIdle?: boolean }
export type DirectoryRetirementSnapshot = Readonly<{ directory: string; sessions: readonly {id:string;directory:string;retired:string|null}[]; actors: readonly {sessionID:string;actorID:string;cwd:string|null;revision:number;status:string;unsettled:number;retired:string|null;ownerRetired:string|null;ownerDirectory:string|null;oversized:number}[] }>
type DirectoryEntry = { identity: DirectoryIdentity; leases: Set<symbol>; closing?: symbol }
// Process-stable across ActorRegistry layer rebuilds. Mutations are synchronous;
// no asynchronous work or user callback runs inside the state transition.
const directoryEntries = new Map<string, DirectoryEntry>()
const executionDirectories = new Map<string, Set<symbol>>()
let globalDirectoryClosing: symbol | undefined
const validDirectoryPath = (value: unknown): value is string => typeof value === "string" && value.length > 1 && Buffer.byteLength(value)<=4096 && !value.includes("\0") && path.isAbsolute(value) && path.normalize(value)===value
const directoryOverlap = (a:string,b:string) => a===b || a.startsWith(b+'/') || b.startsWith(a+'/')
const validDirectoryIdentity = (value: DirectoryIdentity) => {
  if (!value || typeof value !== "object" || Object.keys(value).sort().join(",") !== "commonDevice,commonDir,commonInode,directory,directoryDevice,directoryInode,project") return false
  const canonical = (s: unknown) => typeof s === "string" && s.length > 1 && s.length <= 4096 && !s.includes("\0") && path.isAbsolute(s) && path.normalize(s) === s
  return typeof value.project === "string" && value.project.length > 0 && value.project.length <= 512 && !value.project.includes("\0") && canonical(value.directory) && canonical(value.commonDir)
    && [value.directoryDevice, value.commonDevice].every(n => Number.isSafeInteger(n) && n >= 0)
    && [value.directoryInode, value.commonInode].every(n => Number.isSafeInteger(n) && n > 0)
}
const sameDirectoryIdentity = (a: DirectoryIdentity, b: DirectoryIdentity) =>
  a.project === b.project && a.directory === b.directory && a.commonDir === b.commonDir && a.directoryDevice === b.directoryDevice && a.directoryInode === b.directoryInode && a.commonDevice === b.commonDevice && a.commonInode === b.commonInode

/**
 * Settle abandoned other-process actors so waiters can observe their terminal
 * status. Bootstrap must only inspect registry metadata: persisted question
 * parts are repaired by the selected session's execution lifecycle, never by
 * scanning transcript history on the caller's thread during project startup.
 */
export function sweepAbandonedZombies(): void {
  const cutoff = Date.now() - DEFAULT_LIVENESS_ABANDON_MS
  // Keep this a top-level transaction; Database.use would bypass its boundary.
  Database.transaction((tx) => {
    tx.update(ActorRegistryTable)
      .set({
        status: "idle",
        last_outcome: "failure",
        last_error:
          "Process restarted while actor was active; settled by abandon threshold. Not final — actor send can recover.",
        time_completed: Date.now(),
        time_updated: Date.now(),
      })
      .where(
        and(
          inArray(ActorRegistryTable.status, ["running", "pending"]),
          ne(ActorRegistryTable.instance_id, PROCESS_INSTANCE_ID),
          sql`COALESCE(${ActorRegistryTable.last_activity_time}, ${ActorRegistryTable.time_created}) < ${cutoff}`,
          // A pending managed settlement is fenced until its reconciliation, never swept.
          sql`COALESCE(${ActorRegistryTable.last_error}, '') NOT LIKE 'managed execution pending:%'`,
        ),
      )
      .run()
  })
}
type ActorRow = typeof ActorRegistryTable.$inferSelect

export type RegisterInput = {
  sessionID: SessionID
  actorID: string
  mode: SpawnMode
  parentActorID?: string
  agent: string
  description: string
  contextMode: ContextMode
  contextWatermark?: MessageID
  background: boolean
  lifecycle: Lifecycle
  tools?: ToolWhitelist
  requestedModel?: string
  effectiveModel?: ActorModel
  hostContext?: ActorHostContext
  /** Optional creation-only binding; never serialized or treated as authority. */
  derivedWorkspace?: {chatID:string;workspaceID:string;current:()=>boolean}
  /** Private verified host user; unrelated to provider account selection. */
  originalOwnerAccountID?: string
  forkContext?: EncodedForkContext
}

export type ManagedSettlementIdentity = { rootOperationID: string; rootSessionID?: string; innerTurnID?: string; turnOutcome?: "completed" | "cancelled" | "error"; executionOutcome?: ActorOutcome }
const MANAGED_SETTLEMENT_PREFIX = "managed execution pending:"

export type InputHandoffClaim = {
  owner: string
  stableChatID: string
  mapRevision: number
  mappingRevision: number
  workspaceRevision: number
  requestID: string
  handoffID: string
  rootOperationID: string
  expectedExecutionRevision: number
  agent?: string
  effectiveModel?: ActorModel
  hostContext?: ActorHostContext
  expectedReceipts?: readonly import("@/session/user-input").BoundaryReceiptIdentity[]
}

export type InputHandoffDisposition =
  | { kind: "route-required" }
  | { kind: "fresh-claim"; claimedExecutionRevision: number }
  | { kind: "existing-claim"; claimedExecutionRevision: number; outcome: "claimed" | "running" | "settled-before-consumption" | "consumed" }
  | { kind: "unavailable"; reason: string }

function hostContextFromRow(row: ActorRow): ActorHostContext | undefined {
  const hostContext = {
    ...(row.host_account_id ? { accountID: row.host_account_id } : {}),
    ...(row.provider_grant_id ? { grantID: row.provider_grant_id } : {}),
    ...(row.grant_revision !== null && row.grant_revision !== undefined ? { grantRevision: row.grant_revision } : {}),
    ...(row.credential_revision !== null && row.credential_revision !== undefined ? { credentialRevision: row.credential_revision } : {}),
    ...(row.chat_id ? { chatID: row.chat_id } : {}),
    ...(row.workspace_id ? { workspaceID: row.workspace_id } : {}),
    ...(row.cwd ? { cwd: row.cwd } : {}),
    ...(row.goal_id ? { goalID: row.goal_id } : {}),
  }
  return Object.keys(hostContext).length ? hostContext : undefined
}

function effectiveModelFromRow(row: ActorRow): ActorModel | undefined {
  if (!row.effective_model) return undefined
  try {
    const parsed = JSON.parse(row.effective_model) as Partial<ActorModel>
    return parsed.providerID && parsed.modelID ? { providerID: parsed.providerID, modelID: parsed.modelID } : undefined
  } catch {
    return undefined
  }
}

function validateForkContextRows(
  db: Database.TxOrDb,
  destination: { sessionID: SessionID; actorID: string; parentActorID?: string; effectiveModel?: ActorModel; hostContext?: ActorHostContext },
  source: ForkContextSource,
  encoded: EncodedForkContext,
) {
  const destinationSession = db.select({ parent_id: SessionTable.parent_id }).from(SessionTable)
    .where(eq(SessionTable.id, destination.sessionID)).get()
  const sourceSession = db.select({ id: SessionTable.id }).from(SessionTable)
    .where(eq(SessionTable.id, source.sessionID)).get()
  const sourceActor = db.select().from(ActorRegistryTable).where(and(
    eq(ActorRegistryTable.session_id, source.sessionID), eq(ActorRegistryTable.actor_id, source.actorID),
  )).get()
  if (!destinationSession || !sourceSession || !sourceActor)
    throw new Error("full-context snapshot source or destination membership is unavailable")
  if ((destination.parentActorID ?? "main") !== source.actorID)
    throw new Error("full-context snapshot source actor does not match the registered parent actor")
  if (destination.sessionID !== source.sessionID && destinationSession.parent_id !== source.sessionID)
    throw new Error("full-context snapshot destination is not the source session or its direct child")
  if (source.sessionID === destination.sessionID && source.actorID === destination.actorID)
    throw new Error("full-context snapshot cannot use the destination actor as its source")
  if (!destination.effectiveModel || destination.effectiveModel.providerID !== encoded.modelProviderID ||
    destination.effectiveModel.modelID !== encoded.modelID)
    throw new Error("full-context snapshot model does not match the destination actor effective model")
  // The encoded model is the destination worker's effective model. The source
  // actor's current model is provenance only: an explicitly authorized fork
  // may use a different provider/model while inheriting this frozen prefix.
  if ((sourceActor.chat_id ?? null) !== (destination.hostContext?.chatID ?? null) ||
    (sourceActor.workspace_id ?? null) !== (destination.hostContext?.workspaceID ?? null))
    throw new Error("full-context snapshot source and destination chat/workspace bindings do not match")
}

function validateStoredForkContext(db: Database.TxOrDb, row: typeof ActorForkContextTable.$inferSelect) {
  const destination = db.select().from(ActorRegistryTable).where(and(
    eq(ActorRegistryTable.session_id, row.session_id), eq(ActorRegistryTable.actor_id, row.actor_id),
  )).get()
  const sourceActor = db.select().from(ActorRegistryTable).where(and(
    eq(ActorRegistryTable.session_id, row.source_session_id), eq(ActorRegistryTable.actor_id, row.source_actor_id),
  )).get()
  const sourceSession = db.select({ id: SessionTable.id }).from(SessionTable).where(eq(SessionTable.id, row.source_session_id)).get()
  const destinationSession = db.select({ parent_id: SessionTable.parent_id }).from(SessionTable)
    .where(eq(SessionTable.id, row.session_id)).get()
  if (!destination || !sourceActor || !sourceSession || !destinationSession)
    throw new Error("stored full-context snapshot has missing actor/session lineage")
  if (destination.context_mode !== "full" || destination.mode === "main" ||
    (destination.parent_actor_id ?? "main") !== row.source_actor_id ||
    (row.session_id !== row.source_session_id && destinationSession.parent_id !== row.source_session_id))
    throw new Error("stored full-context snapshot actor lineage is invalid")
  const destinationModel = effectiveModelFromRow(destination)
  if (!destinationModel || destinationModel.providerID !== row.model_provider_id || destinationModel.modelID !== row.model_id)
    throw new Error("stored full-context snapshot model no longer matches the existing actor model")
  if ((sourceActor.chat_id ?? null) !== (destination.chat_id ?? null) ||
    (sourceActor.workspace_id ?? null) !== (destination.workspace_id ?? null))
    throw new Error("stored full-context snapshot source and destination chat/workspace lineage differs")
  return {
    formatVersion: row.format_version,
    sourceSessionID: row.source_session_id,
    sourceActorID: row.source_actor_id,
    sourceWatermarkMessageID: row.source_watermark_message_id,
    modelProviderID: row.model_provider_id as ProviderID,
    modelID: row.model_id as ModelID,
    snapshotJSON: row.snapshot_json,
    snapshotSHA256: row.snapshot_sha256,
    snapshotBytes: row.snapshot_bytes,
    sessionID: row.session_id,
    actorID: row.actor_id,
    timeCreated: row.time_created,
  } satisfies StoredForkContext
}

function fromRow(row: ActorRow): Actor {
  const effectiveModel = effectiveModelFromRow(row)
  const hostContext = hostContextFromRow(row)
  return {
    sessionID: row.session_id,
    actorID: row.actor_id,
    mode: row.mode,
    parentActorID: row.parent_actor_id ?? undefined,
    status: row.status,
    lastOutcome: row.last_outcome ?? undefined,
    executionRevision: row.execution_revision,
    ...(row.result_message_id ? { resultMessageID: row.result_message_id } : {}),
    lifecycle: row.lifecycle,
    agent: row.agent,
    description: row.description,
    contextMode: row.context_mode,
    contextWatermark: row.context_watermark ?? undefined,
    background: Boolean(row.background),
    tools: row.tools ?? undefined,
    requestedModel: row.requested_model ?? undefined,
    effectiveModel,
    ...(row.model_request_options ? { modelRequestOptions: ActorModelOptions.parse(row.model_request_options) } : {}),
    ...(hostContext ? { hostContext } : {}),
    lastTurnTime: row.last_turn_time,
    turnCount: row.turn_count,
    lastActivityTime: row.last_activity_time ?? undefined,
    lastError: row.last_error?.startsWith(MANAGED_SETTLEMENT_PREFIX) ? "Managed execution requires reconciliation" : row.last_error ?? undefined,
    time: {
      created: row.time_created,
      updated: row.time_updated,
      completed: row.time_completed ?? undefined,
    },
  }
}

export type WorkerNamingCandidate = {sessionID:SessionID;actorID:string;accountID:string;chatID:string;workspaceID:string;
  executionRevision:number;displayRevision:number;ordinal:number;description:string}

export interface Interface {
  readonly admitExecutionDirectory: (directory: string) => Effect.Effect<DirectoryLease | undefined>
  readonly directoryRetirementSupported: () => Effect.Effect<boolean>
  readonly assertDirectoryActive: (sessionID: SessionID, actorID?: string) => Effect.Effect<boolean>
  readonly snapshotDirectoryScopes: (directory: string) => Effect.Effect<DirectoryRetirementSnapshot | undefined>
  readonly retireDirectoryScopes: (snapshot: DirectoryRetirementSnapshot, digest: string) => Effect.Effect<boolean>
  readonly verifyRetiredDirectoryScopes: (snapshot: DirectoryRetirementSnapshot, digest: string) => Effect.Effect<boolean>
  readonly directoryScopePaths: (ownDigest?: string) => Effect.Effect<readonly string[] | undefined>
  /** Internal exclusion only; this is not a filesystem or account grant. */
  readonly admitDirectory: (identity: DirectoryIdentity) => Effect.Effect<DirectoryLease | undefined>
  readonly withDirectoryClosing: <A, E, R>(identity: DirectoryIdentity, operation: Effect.Effect<A, E, R>, retention?: DirectoryClosingRetention) => Effect.Effect<{ kind: "busy" | "unavailable" } | { kind: "completed"; value: A }, E, R>
  readonly register: (input: RegisterInput) => Effect.Effect<Actor>
  /** Allocate the next agent-N ID and insert its pending row under one writer transaction. */
  readonly registerAllocated: (input: Omit<RegisterInput, "actorID">) => Effect.Effect<Actor>
  readonly getDisplayOwner?: (sessionID:SessionID,actorID:string) => Effect.Effect<string | undefined>
  readonly workerNamingCandidate: (input:{sessionID:SessionID;actorID:string;accountID:string;chatID:string;workspaceID:string}) => Effect.Effect<WorkerNamingCandidate | undefined>
  readonly applyWorkerName: (candidate:WorkerNamingCandidate,summary:string,current:()=>boolean) => Effect.Effect<boolean>
  readonly renameDisplay?: (input:{sessionID:SessionID; actorID:string; accountID:string; chatID:string; workspaceID:string; executionRevision:number; displayRevision:number; label:string}) => Effect.Effect<boolean>
  /** Exact stored frozen context for this actor, validated against both actor/session lineage rows. */
  readonly getForkContext: (sessionID: SessionID, actorID: string) => Effect.Effect<StoredForkContext | undefined>

  readonly updateStatus: (
    sessionID: SessionID,
    actorID: string,
    patch: {
      status: ActorStatus
      lastOutcome?: ActorOutcome | undefined
      lastError?: string | undefined
      resultMessageID?: MessageID | undefined
    },
  ) => Effect.Effect<void>
  /** Start one actor execution and return its durable revision. */
  readonly beginExecution: (
    sessionID: SessionID,
    actorID: string,
    expectedStatus?: "pending",
    expectedExecutionRevision?: number,
    selection?: { agent?: string; effectiveModel?: ActorModel; expectedEffectiveModel?: ActorModel },
  ) => Effect.Effect<number | undefined>
  /** Publish the exact current MAIN prompt model before route preflight. */
  readonly prepareMainPreflightModel?: (sessionID: SessionID, expectedExecutionRevision: number, model: ActorModel) => Effect.Effect<boolean>
  /** Keep an exact uncertain managed execution fenced until reconciliation. */
  readonly markExecutionUncertain: (sessionID: SessionID, actorID: string, executionRevision: number, identity?: ManagedSettlementIdentity) => Effect.Effect<boolean>
  readonly clearManagedSettlement: (sessionID: SessionID, actorID: string, executionRevision: number, identity: ManagedSettlementIdentity) => Effect.Effect<boolean>
  readonly pendingManagedSettlement: (sessionID: SessionID, actorID: string) => Effect.Effect<(ManagedSettlementIdentity & { executionRevision: number }) | undefined>
  readonly uncertainManagedExecution: (sessionID: SessionID, actorID: string) => Effect.Effect<(ManagedSettlementIdentity & { executionRevision: number; innerTurnID: string }) | undefined>
  /** Atomically begin one idle actor execution and bind a durable input receipt.
   * Existing handoff IDs only acknowledge their recorded revision. */
  readonly beginExecutionForInput?: (
    sessionID: SessionID,
    actorID: string,
    claim: InputHandoffClaim,
  ) => Effect.Effect<InputHandoffDisposition>
  /** Read a previously durable claim before current actor status preflight. */
  readonly inspectInputHandoff?: (
    sessionID: SessionID,
    actorID: string,
    claim: InputHandoffClaim,
  ) => Effect.Effect<InputHandoffDisposition | undefined>
  /** Cancel a never-started pending actor without creating an execution revision. */
  readonly cancelPending: (sessionID: SessionID, actorID: string) => Effect.Effect<Actor | undefined>
  /** Settle one execution exactly once. Returns false when another contender won. */
  readonly settleExecution: (
    sessionID: SessionID,
    actorID: string,
    executionRevision: number,
    outcome: ActorOutcome,
    lastError?: string,
    managedSettlement?: ManagedSettlementIdentity,
    /** Upstream's delivery pointer, written in the same compare-and-set as the outcome. */
    resultMessageID?: MessageID,
  ) => Effect.Effect<boolean>
  /** Claim the one terminal lifecycle notification for one settled execution. */
  readonly claimTerminalNotification: (
    sessionID: SessionID,
    actorID: string,
    executionRevision: number,
    outcome: ActorOutcome,
  ) => Effect.Effect<boolean>
  readonly failOrphan: (
    sessionID: SessionID,
    actorID: string,
    error: string,
    isCurrent: () => boolean,
  ) => Effect.Effect<"failed" | "terminal" | "retry">
  readonly updateTurn: (sessionID: SessionID, actorID: string) => Effect.Effect<void>
  readonly updateAgent: (sessionID: SessionID, actorID: string, agent: string) => Effect.Effect<void>
  readonly updateModel: (sessionID: SessionID, actorID: string, requestedModel?: string, effectiveModel?: ActorModel) => Effect.Effect<void>
  readonly updateHostContext: (sessionID: SessionID, actorID: string, context: ActorHostContext) => Effect.Effect<void>
  readonly updateModelOptionsForExecution: (sessionID: SessionID, actorID: string, executionRevision: number, options: import("./schema").ActorModelOptions) => Effect.Effect<boolean>
  readonly updateHostContextForExecution: (sessionID: SessionID, actorID: string, executionRevision: number, context: ActorHostContext) => Effect.Effect<boolean>
  readonly get: (sessionID: SessionID, actorID: string) => Effect.Effect<Actor | undefined>
  // Derived pull-side liveness for a single actor row (progressing/stalled/
  // terminal), computed from honest registry fields. Returns undefined when the
  // row is absent. Pass stallMs to override the default staleness window.
  readonly liveness: (
    sessionID: SessionID,
    actorID: string,
    stallMs?: number,
  ) => Effect.Effect<{ liveness: Liveness; actor: Actor } | undefined>
  readonly listBySession: (sessionID: SessionID) => Effect.Effect<Actor[]>
  /** Bounded live observation ordered by immutable actor ID; not a snapshot. */
  readonly pageBySession?: (input: { sessionID: SessionID; limit: number; after?: string }) => Effect.Effect<{ items: Actor[]; nextCursor?: string }>
  readonly isPeerSession?: (sessionID: SessionID) => Effect.Effect<boolean>
  readonly listActive: () => Effect.Effect<Actor[]>
  readonly listByParent: (sessionID: SessionID, parentActorID: string) => Effect.Effect<Actor[]>
  // Peer CHILD sessions of a parent session, joined to their session title.
  // Peers key their registry row by their own child session id, so the parent
  // link lives on the Session row (parent_id) — not on session_id here.
  readonly listPeerChildren: (
    parentSessionID: SessionID,
    parentActorID: string,
  ) => Effect.Effect<{ actor: Actor; title: string }[]>
  readonly renderForAgent: (sessionID: SessionID) => Effect.Effect<string>
  readonly agentTypeFor: (sessionID: SessionID, actorID: string) => Effect.Effect<string>
  readonly isSystemSpawned: (sessionID: SessionID, actorID: string) => Effect.Effect<boolean>
  readonly servesCheckpoint: (sessionID: SessionID, actorID: string | undefined) => Effect.Effect<boolean>
  readonly allocateActorID: (sessionID: SessionID, agentType: string) => Effect.Effect<string>
}

export class Service extends Context.Service<Service, Interface>()("@opencode/ActorRegistry") {}

export const layer: Layer.Layer<Service, never, Bus.Service> = Layer.effect(
  Service,
  Effect.gen(function* () {
    const bus = yield* Bus.Service
    const allocationLock = Semaphore.makeUnsafe(1)

    // Layer rebuilds retain the same process identity.
    // This is stable across layer rebuilds within the same process.
    const instanceID = PROCESS_INSTANCE_ID

    const retirementSupported = (db: Database.TxOrDb) => ['session','actor_registry'].every(table => db.all<{name:string}>(sql.raw(`PRAGMA table_info(${table})`)).some(row=>row.name==='openclank_directory_retired_by'))
    const retirementMarkersValid = (db: Database.TxOrDb): boolean => ['session','actor_registry'].every(table => !db.get(sql.raw(`SELECT 1 FROM ${table} WHERE openclank_directory_retired_by IS NOT NULL AND (typeof(openclank_directory_retired_by)!='text' OR length(CAST(openclank_directory_retired_by AS BLOB))!=64 OR openclank_directory_retired_by GLOB '*[^0-9a-f]*') LIMIT 1`)))
    const directorySnapshot = (db: Database.TxOrDb, directory: string, retiredDigest: string | null = null): DirectoryRetirementSnapshot | undefined => {
      if (!retirementSupported(db) || !retirementMarkersValid(db) || typeof directory !== 'string' || directory.length < 2 || directory.length > 4096 || directory.includes('\0') || !path.isAbsolute(directory) || path.normalize(directory)!==directory) return undefined
      const sessions=db.all<{id:string;directory:string;retired:string|null}>(sql`SELECT CASE WHEN length(CAST(id AS BLOB))<=512 THEN id END AS id,CASE WHEN length(CAST(directory AS BLOB))<=4096 THEN directory END AS directory,openclank_directory_retired_by AS retired FROM session WHERE (openclank_directory_retired_by IS NULL OR openclank_directory_retired_by=${retiredDigest}) AND (directory=${directory} OR substr(directory,1,length(${directory})+1)=${directory} || '/') ORDER BY id LIMIT 513`)
      const actors=db.all<{sessionID:string;actorID:string;cwd:string|null;revision:number;status:string;unsettled:number;retired:string|null;ownerRetired:string|null;ownerDirectory:string|null;oversized:number}>(sql`SELECT CASE WHEN length(CAST(a.session_id AS BLOB))<=512 THEN a.session_id END AS sessionID,CASE WHEN length(CAST(a.actor_id AS BLOB))<=512 THEN a.actor_id END AS actorID,CASE WHEN length(CAST(a.cwd AS BLOB))<=4096 THEN a.cwd END AS cwd,a.execution_revision AS revision,CASE WHEN length(CAST(a.status AS BLOB))<=32 THEN a.status END AS status,CASE WHEN a.last_error LIKE 'managed execution pending:%' OR a.last_error LIKE 'orphaned:%' THEN 1 ELSE 0 END AS unsettled,a.openclank_directory_retired_by AS retired,s.openclank_directory_retired_by AS ownerRetired,CASE WHEN length(CAST(s.directory AS BLOB))<=4096 THEN s.directory END AS ownerDirectory,CASE WHEN length(CAST(a.cwd AS BLOB))>4096 OR length(CAST(a.last_error AS BLOB))>4096 THEN 1 ELSE 0 END AS oversized FROM actor_registry a JOIN session s ON s.id=a.session_id WHERE (a.openclank_directory_retired_by IS NULL OR a.openclank_directory_retired_by=${retiredDigest}) AND (s.openclank_directory_retired_by IS NULL OR s.openclank_directory_retired_by=${retiredDigest}) AND (s.directory=${directory} OR substr(s.directory,1,length(${directory})+1)=${directory} || '/' OR (a.cwd IS NOT NULL AND (a.cwd=${directory} OR substr(a.cwd,1,length(${directory})+1)=${directory} || '/'))) ORDER BY a.session_id,a.actor_id LIMIT 513`)
      if (sessions.length>512 || actors.length>512 || Buffer.byteLength(JSON.stringify([sessions,actors]))>1048576) return undefined
      const validID=(v:unknown):v is string=>typeof v==='string' && v.length>0 && Buffer.byteLength(v)<=512 && !/[\x00-\x1f\x7f]/.test(v) && Buffer.from(v).toString('utf8')===v
      if (sessions.some(s=>!validID(s.id) || !validDirectoryPath(s.directory) || s.retired!==retiredDigest) || actors.some(a=>a.oversized!==0 || !validDirectoryPath(a.ownerDirectory) || (a.cwd!==null && !validDirectoryPath(a.cwd)) || !validID(a.sessionID) || !validID(a.actorID) || !Number.isSafeInteger(a.revision) || a.revision<0 || a.status!=='idle' || a.retired!==retiredDigest || a.ownerRetired!==(sessions.some(session=>session.id===a.sessionID) ? retiredDigest : null) || a.unsettled!==0)) return undefined
      // A session without a recorded main actor has unknown execution state.
      if (sessions.some(session=>!actors.some(actor=>actor.sessionID===session.id && actor.actorID==='main'))) return undefined
      return {directory,sessions,actors}
    }
    const directoryRetirementSupported: Interface['directoryRetirementSupported'] = () => Effect.sync(()=>Database.use(retirementSupported))
    const activeInTransaction = (db:Database.TxOrDb, sessionID:SessionID, actorID?:string) => {
      if (!retirementSupported(db)) return true
      const session=db.get<{retired:string|null}>(sql`SELECT openclank_directory_retired_by AS retired FROM session WHERE id=${sessionID}`)
      if (!session || session.retired!==null) return false
      if (actorID===undefined) return true
      const actor=db.get<{retired:string|null}>(sql`SELECT openclank_directory_retired_by AS retired FROM actor_registry WHERE session_id=${sessionID} AND actor_id=${actorID}`)
      return !!actor && actor.retired===null
    }
    const assertDirectoryActive: Interface['assertDirectoryActive'] = (sessionID, actorID) => Effect.sync(()=>Database.use(db=>activeInTransaction(db,sessionID,actorID)))
    const snapshotDirectoryScopes: Interface['snapshotDirectoryScopes'] = directory => Effect.sync(()=>Database.transaction(db=>directorySnapshot(db,directory),{behavior:'deferred'}))
    const retireDirectoryScopes: Interface['retireDirectoryScopes'] = (snapshot,digest) => Effect.sync(()=>{
      if (!snapshot || !/^[0-9a-f]{64}$/.test(digest)) return false
      return Database.transaction(db=>{
        const current=directorySnapshot(db,snapshot.directory)
        if (!current || !isDeepStrictEqual(current,snapshot)) return false
        for (const session of current.sessions) db.run(sql`UPDATE session SET openclank_directory_retired_by=${digest} WHERE id=${session.id} AND openclank_directory_retired_by IS NULL`)
        for (const actor of current.actors) db.run(sql`UPDATE actor_registry SET openclank_directory_retired_by=${digest} WHERE session_id=${actor.sessionID} AND actor_id=${actor.actorID} AND openclank_directory_retired_by IS NULL`)
        return true
      },{behavior:'immediate'})
    })

    // Private action-time metadata only; never returns transcript or tool content.
    const directoryScopePaths: Interface['directoryScopePaths'] = ownDigest => Effect.sync(() => Database.transaction(db => {
      if (!retirementSupported(db) || !retirementMarkersValid(db) || (ownDigest !== undefined && !/^[0-9a-f]{64}$/.test(ownDigest))) return undefined
      const digest=ownDigest ?? null
      const sessions = db.all<{directory:string|null}>(sql`SELECT CASE WHEN length(CAST(directory AS BLOB))<=4096 THEN directory END AS directory FROM session WHERE openclank_directory_retired_by IS NULL OR openclank_directory_retired_by=${digest} LIMIT 513`)
      const actors = db.all<{directory:string|null;oversized:number;orphan:number}>(sql`SELECT CASE WHEN length(CAST(a.cwd AS BLOB))<=4096 THEN a.cwd END AS directory,CASE WHEN length(CAST(a.cwd AS BLOB))>4096 THEN 1 ELSE 0 END AS oversized,CASE WHEN s.id IS NULL THEN 1 ELSE 0 END AS orphan FROM actor_registry a LEFT JOIN session s ON s.id=a.session_id WHERE (a.openclank_directory_retired_by IS NULL OR a.openclank_directory_retired_by=${digest}) AND (s.id IS NULL OR s.openclank_directory_retired_by IS NULL OR s.openclank_directory_retired_by=${digest}) LIMIT 513`)
      if (sessions.length + actors.length > 512 || sessions.some(row => !validDirectoryPath(row.directory)) || actors.some(row => row.orphan!==0 || row.oversized!==0 || (row.directory!==null && !validDirectoryPath(row.directory))) || Buffer.byteLength(JSON.stringify([sessions,actors])) > 1048576) return undefined
      return [...new Set([...sessions.map(row=>row.directory!),...actors.flatMap(row=>row.directory===null?[]:[row.directory])])].sort()
    }, {behavior:'deferred'}))
    const verifyRetiredDirectoryScopes: Interface['verifyRetiredDirectoryScopes'] = (snapshot,digest) => Effect.sync(() => {
      if (!snapshot || !/^[0-9a-f]{64}$/.test(digest)) return false
      return Database.transaction(db => {
        const current = directorySnapshot(db,snapshot.directory,digest)
        if (!current) return false
        const ids = new Set(snapshot.sessions.map(session => session.id))
        const expected = {directory:snapshot.directory,
          sessions:snapshot.sessions.map(session=>({...session,retired:digest})),
          actors:snapshot.actors.map(actor=>({...actor,retired:digest,ownerRetired:ids.has(actor.sessionID)?digest:null}))}
        return isDeepStrictEqual(current,expected)
      }, {behavior:'deferred'})
    })

    const admitExecutionDirectory: Interface["admitExecutionDirectory"] = directory => Effect.sync(()=>{
      if (globalDirectoryClosing || !validDirectoryPath(directory) || [...directoryEntries].some(([key,entry])=>!!entry.closing && directoryOverlap(key,directory))) return undefined
      const leases=executionDirectories.get(directory) ?? new Set<symbol>(), token=Symbol('execution directory')
      leases.add(token);executionDirectories.set(directory,leases)
      return Object.freeze({release:()=>{if (!leases.delete(token)) return false;if (!leases.size && executionDirectories.get(directory)===leases) executionDirectories.delete(directory);return true}})
    })
    const admitDirectory: Interface["admitDirectory"] = (identity) => Effect.sync(() => {
      if (globalDirectoryClosing || !validDirectoryIdentity(identity)) return undefined
      if ([...directoryEntries].some(([key,entry])=>!!entry.closing && directoryOverlap(key,identity.directory))) return undefined
      const current = directoryEntries.get(identity.directory)
      if (current && (current.closing || !sameDirectoryIdentity(current.identity, identity))) return undefined
      const entry: DirectoryEntry = current ?? { identity: Object.freeze({ ...identity }), leases: new Set<symbol>() }
      const token = Symbol("directory admission")
      entry.leases.add(token)
      directoryEntries.set(identity.directory, entry)
      return Object.freeze({ release: () => {
        if (!entry.leases.delete(token)) return false
        if (!entry.closing && entry.leases.size === 0 && directoryEntries.get(entry.identity.directory) === entry) directoryEntries.delete(entry.identity.directory)
        return true
      } })
    })

    // The operation must itself join its owned children before settling; no
    // detached work may outlive this closing state.
    const withDirectoryClosing: Interface["withDirectoryClosing"] = (identity, operation, retention) => Effect.uninterruptible(Effect.gen(function* () {
      if (!validDirectoryIdentity(identity)) return { kind: "unavailable" as const }
      if (globalDirectoryClosing) return { kind: "busy" as const }
      if (retention?.globalIdle && ([...directoryEntries.values()].some(entry => !!entry.closing || entry.leases.size > 0) || [...executionDirectories.values()].some(leases => leases.size > 0))) return { kind: "busy" as const }
      const current = directoryEntries.get(identity.directory)
      if (current && !sameDirectoryIdentity(current.identity, identity)) return { kind: "unavailable" as const }
      if ([...directoryEntries].some(([key,entry])=>directoryOverlap(key,identity.directory) && (!!entry.closing || entry.leases.size>0)) || [...executionDirectories].some(([key,leases])=>directoryOverlap(key,identity.directory) && leases.size>0)) return { kind: "busy" as const }
      const entry: DirectoryEntry = current ?? { identity: Object.freeze({ ...identity }), leases: new Set<symbol>() }
      const token = Symbol("directory closing")
      entry.closing = token
      if (retention?.globalIdle) globalDirectoryClosing = token
      directoryEntries.set(identity.directory, entry)
      const value = yield* operation.pipe(Effect.ensuring(Effect.sync(() => {
        if (retention?.retain !== true && directoryEntries.get(entry.identity.directory) === entry && entry.closing === token) {
          entry.closing = undefined
          if (globalDirectoryClosing === token) globalDirectoryClosing = undefined
          if (entry.leases.size === 0) directoryEntries.delete(entry.identity.directory)
        }
      })))
      return { kind: "completed" as const, value }
    }))

    // --- CRUD methods ---

    const makeRow = (input: RegisterInput, now: number) => ({
      session_id: input.sessionID,
      actor_id: input.actorID,
      mode: input.mode,
      parent_actor_id: input.parentActorID ?? null,
      status: "pending" as const,
      last_outcome: null,
      execution_revision: 0,
      terminal_notification_revision: null,
      terminal_notification_outcome: null,
      result_message_id: null,
      lifecycle: input.lifecycle,
      agent: input.agent,
      description: input.description,
      context_mode: input.contextMode,
      context_watermark: input.contextWatermark ?? null,
      background: input.background,
      tools: input.tools ?? null,
      requested_model: input.requestedModel ?? null,
      effective_model: input.effectiveModel ? JSON.stringify(input.effectiveModel) : null,
      model_request_options: null,
      host_account_id: input.hostContext?.accountID ?? null,
      provider_grant_id: input.hostContext?.grantID ?? null,
      grant_revision: input.hostContext?.grantRevision ?? null,
      credential_revision: input.hostContext?.credentialRevision ?? null,
      chat_id: input.hostContext?.chatID ?? null,
      workspace_id: input.hostContext?.workspaceID ?? null,
      cwd: input.hostContext?.cwd ?? null,
      goal_id: input.hostContext?.goalID ?? null,
      last_turn_time: now,
      turn_count: 0,
      // No part has landed for this actor yet. NULL, not `now`: deriveLiveness
      // falls back to time_created when activity is absent, so seeding a fake
      // activity timestamp here would assert something happened that did not.
      last_activity_time: null,
      last_error: null,
      instance_id: instanceID,
      time_completed: null,
      time_created: now,
      time_updated: now,
    })

    const publishRegistered = (input: RegisterInput) =>
      bus.publish(Events.ActorRegistered, {
        sessionID: input.sessionID,
        actorID: input.actorID,
        mode: input.mode,
        parentActorID: input.parentActorID,
        description: input.description,
        agent: input.agent,
        background: input.background,
        ...(input.requestedModel ? { requestedModel: input.requestedModel } : {}),
        ...(input.effectiveModel ? { effectiveModel: input.effectiveModel } : {}),
        ...(input.hostContext ? { hostContext: input.hostContext } : {}),
      })

    const insertForkContext = (db: Database.TxOrDb, input: RegisterInput, now: number) => {
      const encoded = input.forkContext
      if (!encoded) return
      if (input.contextMode !== "full" || input.mode === "main")
        throw new Error("fork context snapshots are only valid for spawned full-context actors")
      if (encoded.sourceActorID !== (input.parentActorID ?? "main"))
        throw new Error("fork context source actor does not match actor registration parent")
      const actualBytes = Buffer.byteLength(encoded.snapshotJSON, "utf8")
      if (encoded.snapshotBytes !== actualBytes || actualBytes > 8 * 1024 * 1024 || actualBytes < 0)
        throw new Error(`fork context snapshot is ${encoded.snapshotBytes} bytes; maximum is ${8 * 1024 * 1024} bytes`)
      if (encoded.formatVersion !== 1 || !encoded.snapshotSHA256 || !encoded.sourceWatermarkMessageID ||
        !encoded.sourceSessionID || !encoded.sourceActorID || !encoded.modelProviderID || !encoded.modelID)
        throw new Error("fork context snapshot metadata is incomplete")
      if (createHash("sha256").update(encoded.snapshotJSON, "utf8").digest("hex") !== encoded.snapshotSHA256)
        throw new Error("fork context snapshot checksum does not match")
      const source = { sessionID: encoded.sourceSessionID, actorID: encoded.sourceActorID }
      const destination = {
        sessionID: input.sessionID,
        actorID: input.actorID,
        parentActorID: input.parentActorID,
        effectiveModel: input.effectiveModel,
        hostContext: input.hostContext,
      }
      validateForkContextRows(db, destination, source, encoded)
      db.insert(ActorForkContextTable).values({
        session_id: input.sessionID,
        actor_id: input.actorID,
        format_version: encoded.formatVersion,
        source_session_id: encoded.sourceSessionID,
        source_actor_id: encoded.sourceActorID,
        source_watermark_message_id: encoded.sourceWatermarkMessageID,
        model_provider_id: encoded.modelProviderID,
        model_id: encoded.modelID,
        snapshot_json: encoded.snapshotJSON,
        snapshot_sha256: encoded.snapshotSHA256,
        snapshot_bytes: encoded.snapshotBytes,
        time_created: now,
      }).run()
    }

    // Optional profile is probed without adding columns to the ORM projection,
    // so existing admitted databases remain readable and writable.
    const assignDisplayLabel = (db: Database.TxOrDb, input: RegisterInput) => {
      if (input.mode === "main") return
      const columns = db.all<{name:string}>(sql`PRAGMA table_info(actor_registry)`)
      if (!columns.some((column) => column.name === "display_ordinal")) return
      const current = db.get<{counter:number,kind:string}>(sql`SELECT next_display_ordinal AS counter, typeof(next_display_ordinal) AS kind FROM session WHERE id=${input.sessionID}`)
      const greatest = db.get<{ordinal:number,label:string,revision:number,origin:string}>(sql`SELECT display_ordinal AS ordinal, display_label AS label,
        display_revision AS revision, display_origin AS origin FROM actor_registry INDEXED BY o03_actor_session_ordinal
        WHERE session_id=${input.sessionID} AND display_ordinal IS NOT NULL ORDER BY display_ordinal DESC LIMIT 1`)
      if (!current || current.kind !== "integer" || !Number.isSafeInteger(current.counter) || current.counter < 1 || current.counter >= 9007199254740991 ||
          (greatest && (!Number.isSafeInteger(greatest.ordinal) || greatest.ordinal < 1 || greatest.ordinal >= current.counter ||
            typeof greatest.label !== "string" || Buffer.byteLength(greatest.label,"utf8") < 1 || Buffer.byteLength(greatest.label,"utf8") > 200 ||
            !Number.isSafeInteger(greatest.revision) || greatest.revision < 0 || !["deterministic","helper","manual"].includes(greatest.origin))))
        throw new Error("actor label profile unavailable")
      const allocated = db.get<{ordinal:number}>(sql`UPDATE session SET next_display_ordinal = next_display_ordinal + 1
        WHERE id = ${input.sessionID} AND next_display_ordinal < 9007199254740991
        RETURNING next_display_ordinal - 1 AS ordinal`)
      if (!allocated || !Number.isSafeInteger(allocated.ordinal) || allocated.ordinal < 1)
        throw new Error("actor label ordinal unavailable")
      const prefix = `Clank #${allocated.ordinal}`
      const summary = input.description.replace(/[\u0000-\u001f\u007f-\u009f]/g, " ").trim()
      let label = prefix
      if (summary) {
        label += ": "
        for (const character of Array.from(summary)) {
          if (Buffer.byteLength(label + character, "utf8") > 200) break
          label += character
        }
        label = label.trimEnd().replace(/:$/, "")
      }
      if (columns.some((column) => column.name === "display_owner_account_id")) {
        const owner = input.originalOwnerAccountID
        if (owner !== undefined && (!owner || Buffer.byteLength(owner,"utf8")>256)) throw new Error("actor display owner unavailable")
        db.run(sql`UPDATE actor_registry SET display_ordinal=${allocated.ordinal}, display_label=${label},
          display_revision=0, display_origin='deterministic', display_owner_account_id=${owner ?? null}
          WHERE session_id=${input.sessionID} AND actor_id=${input.actorID}`)
      } else {
        db.run(sql`UPDATE actor_registry SET display_ordinal=${allocated.ordinal}, display_label=${label},
          display_revision=0, display_origin='deterministic'
          WHERE session_id=${input.sessionID} AND actor_id=${input.actorID}`)
      }
    }

    const register = Effect.fn("ActorRegistry.register")(function* (input: RegisterInput) {
      const now = Date.now()
      const row = makeRow(input, now)
      yield* Effect.sync(() => Database.transaction((db) => {
        if (!activeInTransaction(db,input.sessionID)) throw new Error('directory scope retired')
        db.insert(ActorRegistryTable).values(row).run()
        assignDisplayLabel(db, input)
        insertForkContext(db, input, now)
      }, { behavior: "immediate" }))
      yield* publishRegistered(input)
      return fromRow(row)
    })

    const getDisplayOwner = Effect.fn("ActorRegistry.getDisplayOwner")(function* (sessionID:SessionID,actorID:string) {
      return yield* Effect.sync(()=>Database.use(db=>{
        if (!db.all<{name:string}>(sql`PRAGMA table_info(actor_registry)`).some(x=>x.name==="display_owner_account_id")) return undefined
        const row=db.get<{owner:string|null}>(sql`SELECT display_owner_account_id AS owner FROM actor_registry WHERE session_id=${sessionID} AND actor_id=${actorID}`)
        return typeof row?.owner === "string" && row.owner && Buffer.byteLength(row.owner,"utf8")<=256 ? row.owner : undefined
      }))
    })

    const workerNamingCandidate = Effect.fn("ActorRegistry.workerNamingCandidate")(function* (input: {sessionID:SessionID;actorID:string;accountID:string;chatID:string;workspaceID:string}) {
      return yield* Effect.sync(()=>Database.use(db=>{
        const cols=db.all<{name:string}>(sql`PRAGMA table_info(actor_registry)`)
        if (!cols.some(x=>x.name==="display_owner_account_id")) return undefined
        const row=db.get<{executionRevision:number;displayRevision:number;ordinal:number;description:string;origin:string}>(sql`
          SELECT execution_revision AS executionRevision,display_revision AS displayRevision,display_ordinal AS ordinal,
            CASE WHEN length(CAST(description AS BLOB)) BETWEEN 1 AND 2048 THEN description END AS description,display_origin AS origin
          FROM actor_registry WHERE session_id=${input.sessionID} AND actor_id=${input.actorID} AND mode='subagent'
            AND display_owner_account_id=${input.accountID} AND chat_id=${input.chatID} AND workspace_id=${input.workspaceID}`)
        if (!row || row.origin!=="deterministic" || typeof row.description!=="string" || !row.description.trim() ||
            !Number.isSafeInteger(row.executionRevision) || row.executionRevision<0 || !Number.isSafeInteger(row.displayRevision) ||
            row.displayRevision<0 || row.displayRevision>=9007199254740991 || !Number.isSafeInteger(row.ordinal) || row.ordinal<1) return undefined
        return {...input,executionRevision:row.executionRevision,displayRevision:row.displayRevision,ordinal:row.ordinal,description:row.description}
      }))
    })

    const applyWorkerName = Effect.fn("ActorRegistry.applyWorkerName")(function* (candidate:WorkerNamingCandidate,summary:string,current:()=>boolean) {
      if (typeof summary!=="string" || !summary.trim() || summary!==summary.trim() || /[\u0000-\u001f\u007f-\u009f]|[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/u.test(summary) ||
          typeof current!=="function" || !Number.isSafeInteger(candidate.ordinal) || candidate.ordinal<1) return false
      const label=`Clank #${candidate.ordinal}: ${summary}`
      if (Buffer.byteLength(label,"utf8")>200) return false
      return yield* Effect.sync(()=>Database.transaction(db=>{
        if (!current()) return false
        const updated=db.get<{id:string}>(sql`UPDATE actor_registry SET display_label=${label},display_revision=display_revision+1,display_origin='helper'
          WHERE session_id=${candidate.sessionID} AND actor_id=${candidate.actorID} AND display_owner_account_id=${candidate.accountID}
            AND chat_id=${candidate.chatID} AND workspace_id=${candidate.workspaceID} AND execution_revision=${candidate.executionRevision}
            AND display_revision=${candidate.displayRevision} AND display_ordinal=${candidate.ordinal} AND display_origin='deterministic'
            AND description=${candidate.description} RETURNING actor_id AS id`)
        return !!updated
      },{behavior:"immediate"}))
    })

    const renameDisplay = Effect.fn("ActorRegistry.renameDisplay")(function* (input: {sessionID:SessionID; actorID:string; accountID:string; chatID:string; workspaceID:string; executionRevision:number; displayRevision:number; label:string}) {
      if (!input.label.trim() || Buffer.byteLength(input.label,"utf8")>200 ||
          /[\u0000-\u001f\u007f-\u009f]|[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/u.test(input.label) ||
          !Number.isSafeInteger(input.executionRevision) || input.executionRevision<0 ||
          !Number.isSafeInteger(input.displayRevision) || input.displayRevision<0 || input.displayRevision>=9007199254740991) return false
      return yield* Effect.sync(()=>Database.transaction(db=>{
        if (!db.all<{name:string}>(sql`PRAGMA table_info(actor_registry)`).some(x=>x.name==="display_owner_account_id")) return false
        const updated=db.get<{id:string}>(sql`UPDATE actor_registry SET display_label=${input.label},display_revision=display_revision+1,display_origin='manual'
          WHERE session_id=${input.sessionID} AND actor_id=${input.actorID} AND display_owner_account_id=${input.accountID}
            AND chat_id=${input.chatID} AND workspace_id=${input.workspaceID} AND execution_revision=${input.executionRevision}
            AND display_revision=${input.displayRevision} AND display_ordinal IS NOT NULL RETURNING actor_id AS id`)
        return !!updated
      },{behavior:"immediate"}))
    })

    const getForkContext = Effect.fn("ActorRegistry.getForkContext")(function* (sessionID: SessionID, actorID: string) {
      return yield* Effect.sync(() => Database.use((db) => {
        const row = db.select().from(ActorForkContextTable).where(and(
          eq(ActorForkContextTable.session_id, sessionID), eq(ActorForkContextTable.actor_id, actorID),
        )).get()
        return row ? validateStoredForkContext(db, row) : undefined
      }))
    })

    const registerAllocated = Effect.fn("ActorRegistry.registerAllocated")(function* (
      input: Omit<RegisterInput, "actorID">,
    ) {
      const now = Date.now()
      const result = yield* allocationLock.withPermits(1)(Effect.sync(() =>
        Database.transaction((db) => {
          if (!activeInTransaction(db,input.sessionID)) throw new Error('directory scope retired')
          const prefix = `${input.agent}-`
          const existing = db
            .select({ actor_id: ActorRegistryTable.actor_id })
            .from(ActorRegistryTable)
            .where(eq(ActorRegistryTable.session_id, input.sessionID))
            .all()
          let max = 0
          for (const row of existing) {
            if (!row.actor_id.startsWith(prefix)) continue
            const suffix = row.actor_id.slice(prefix.length)
            if (!/^[1-9]\d*$/.test(suffix)) continue
            const n = Number(suffix)
            if (Number.isSafeInteger(n) && n > max) max = n
          }
          const actorID = `${input.agent}-${max + 1}`
          let context = input.hostContext
          const derived = input.derivedWorkspace
          try {
            if (derived && typeof derived.current === "function" && derived.current() && derived.chatID && derived.workspaceID &&
                (!context?.chatID || context.chatID === derived.chatID) && (!context?.workspaceID || context.workspaceID === derived.workspaceID))
              context = {...context,chatID:derived.chatID,workspaceID:derived.workspaceID}
          } catch { /* unavailable provenance does not block ordinary spawning */ }
          const full = { ...input, actorID, hostContext:context } as RegisterInput
          const row = makeRow(full, now)
          db.insert(ActorRegistryTable).values(row).run()
          assignDisplayLabel(db, full)
          insertForkContext(db, full, now)
          return { full, row }
        }, { behavior: "immediate" }),
      ))
      yield* publishRegistered(result.full)
      return fromRow(result.row)
    })

    const beginExecution = Effect.fn("ActorRegistry.beginExecution")(function* (
      sessionID: SessionID,
      actorID: string,
      expectedStatus?: "pending",
      expectedExecutionRevision?: number,
      selection?: { agent?: string; effectiveModel?: ActorModel; expectedEffectiveModel?: ActorModel },
    ) {
      const now = Date.now()
      const row = yield* Effect.sync(() =>
        Database.transaction((db) => {
          if (!activeInTransaction(db,sessionID,actorID)) return undefined
          return db
            .update(ActorRegistryTable)
            .set({
              status: "running",
              ...(selection?.agent ? { agent: selection.agent } : {}),
              ...(selection?.effectiveModel ? { effective_model: JSON.stringify(selection.effectiveModel) } : {}),
              execution_revision: sql`${ActorRegistryTable.execution_revision} + 1`,
              last_outcome: null,
              last_error: null,
              result_message_id: null,
              time_completed: null,
              time_updated: now,
            })
            .where(
              and(
                eq(ActorRegistryTable.session_id, sessionID),
                eq(ActorRegistryTable.actor_id, actorID),
                ...(expectedExecutionRevision === undefined
                  ? []
                  : [eq(ActorRegistryTable.execution_revision, expectedExecutionRevision)]),
                ...(selection?.expectedEffectiveModel
                  ? [eq(ActorRegistryTable.effective_model, JSON.stringify(ActorModel.parse(selection.expectedEffectiveModel)))]
                  : []),
                expectedStatus
                  ? eq(ActorRegistryTable.status, expectedStatus)
                  : sql`${ActorRegistryTable.status} != 'running'`,
                sql`COALESCE(${ActorRegistryTable.last_error}, '') NOT LIKE 'managed execution pending:%'`,
              ),
            )
            .returning()
            .get()
        }, { behavior: "immediate" }),
      )
      if (!row || row.status !== "running") return undefined
      yield* publishSettledStatus(row, sessionID, actorID)
      return row.execution_revision
    })

    const prepareMainPreflightModel = Effect.fn("ActorRegistry.prepareMainPreflightModel")(function* (
      sessionID: SessionID, expectedExecutionRevision: number, model: ActorModel,
    ) {
      const selected = ActorModel.parse(model)
      if (!Number.isSafeInteger(expectedExecutionRevision) || expectedExecutionRevision < 0) return false
      return yield* Effect.sync(() => Database.transaction((db) => {
        if (!activeInTransaction(db, sessionID, "main")) return false
        return !!db.update(ActorRegistryTable).set({
          effective_model: JSON.stringify(selected), time_updated: Date.now(),
        }).where(and(
          eq(ActorRegistryTable.session_id, sessionID), eq(ActorRegistryTable.actor_id, "main"),
          eq(ActorRegistryTable.mode, "main"), eq(ActorRegistryTable.execution_revision, expectedExecutionRevision),
          sql`(${ActorRegistryTable.status} = 'idle' OR (${ActorRegistryTable.status} = 'pending' AND ${ActorRegistryTable.execution_revision} = 0))`,
          sql`COALESCE(${ActorRegistryTable.last_error}, '') NOT LIKE 'managed execution pending:%'`,
        )).returning({ actor_id: ActorRegistryTable.actor_id }).get()
      }, { behavior: "immediate" }))
    })

    const markExecutionUncertain = Effect.fn("ActorRegistry.markExecutionUncertain")(function* (sessionID: SessionID, actorID: string, executionRevision: number, identity?: ManagedSettlementIdentity) {
      const marker = MANAGED_SETTLEMENT_PREFIX + JSON.stringify({ ...identity, executionRevision, uncertain: true })
      return yield* Effect.sync(() => Database.use((db) => !!db.update(ActorRegistryTable)
        .set({ last_error: marker, time_updated: Date.now() })
        .where(and(eq(ActorRegistryTable.session_id, sessionID), eq(ActorRegistryTable.actor_id, actorID),
          eq(ActorRegistryTable.status, "running"), eq(ActorRegistryTable.execution_revision, executionRevision)))
        .returning({ actor_id: ActorRegistryTable.actor_id }).get()))
    })

    const pendingManagedSettlement = Effect.fn("ActorRegistry.pendingManagedSettlement")(function* (sessionID: SessionID, actorID: string) {
      return yield* Effect.sync(() => Database.use((db) => {
        const row = db.select().from(ActorRegistryTable).where(and(eq(ActorRegistryTable.session_id, sessionID),
          eq(ActorRegistryTable.actor_id, actorID), eq(ActorRegistryTable.status, "idle"))).get()
        if (!row?.last_error?.startsWith(MANAGED_SETTLEMENT_PREFIX)) return undefined
        try {
          const value = JSON.parse(row.last_error.slice(MANAGED_SETTLEMENT_PREFIX.length))
          if (typeof value.rootOperationID !== "string" || !value.rootOperationID || value.executionRevision !== row.execution_revision || value.uncertain)
            return undefined
          return { rootOperationID: value.rootOperationID, executionRevision: row.execution_revision,
            ...(typeof value.innerTurnID === "string" ? { innerTurnID: value.innerTurnID } : {}),
            ...(typeof value.rootSessionID === "string" && value.rootSessionID ? { rootSessionID:value.rootSessionID } : {}),
            ...(["completed","cancelled","error"].includes(value.turnOutcome) ? {turnOutcome:value.turnOutcome as ManagedSettlementIdentity["turnOutcome"]} : {}),
            ...(["success","failure","cancelled"].includes(row.last_outcome ?? "") ? {executionOutcome:row.last_outcome as ActorOutcome} : {}) }
        } catch { return undefined }
      }))
    })

    const uncertainManagedExecution = Effect.fn("ActorRegistry.uncertainManagedExecution")(function* (sessionID: SessionID, actorID: string) {
      return yield* Effect.sync(() => Database.use((db) => {
        const row = db.select().from(ActorRegistryTable).where(and(eq(ActorRegistryTable.session_id, sessionID),
          eq(ActorRegistryTable.actor_id, actorID), eq(ActorRegistryTable.status, "running"))).get()
        if (!row?.last_error?.startsWith(MANAGED_SETTLEMENT_PREFIX)) return undefined
        try {
          const value = JSON.parse(row.last_error.slice(MANAGED_SETTLEMENT_PREFIX.length))
          if (!value.uncertain || typeof value.rootOperationID !== "string" || !value.rootOperationID ||
              typeof value.innerTurnID !== "string" || !value.innerTurnID || value.executionRevision !== row.execution_revision) return undefined
          return { rootOperationID: value.rootOperationID, innerTurnID: value.innerTurnID, executionRevision: row.execution_revision,
            ...(typeof value.rootSessionID === "string" && value.rootSessionID ? { rootSessionID: value.rootSessionID } : {}),
            ...(["completed", "cancelled", "error"].includes(value.turnOutcome) ? { turnOutcome: value.turnOutcome as ManagedSettlementIdentity["turnOutcome"] } : {}) }
        } catch { return undefined }
      }))
    })

    const clearManagedSettlement = Effect.fn("ActorRegistry.clearManagedSettlement")(function* (sessionID: SessionID, actorID: string, executionRevision: number, identity: ManagedSettlementIdentity) {
      return yield* Effect.sync(() => Database.transaction((db) => {
        const row = db.select().from(ActorRegistryTable).where(and(eq(ActorRegistryTable.session_id, sessionID),
          eq(ActorRegistryTable.actor_id, actorID), eq(ActorRegistryTable.status, "idle"),
          eq(ActorRegistryTable.execution_revision, executionRevision))).get()
        if (!row?.last_error?.startsWith(MANAGED_SETTLEMENT_PREFIX)) return false
        let marker: { rootOperationID?: string; innerTurnID?: string; lastError?: string }
        try { marker = JSON.parse(row.last_error.slice(MANAGED_SETTLEMENT_PREFIX.length)) } catch { return false }
        if (marker.rootOperationID !== identity.rootOperationID || marker.innerTurnID !== identity.innerTurnID) return false
        return !!db.update(ActorRegistryTable).set({ last_error: marker.lastError ?? null, time_updated: Date.now() })
          .where(and(eq(ActorRegistryTable.session_id, sessionID), eq(ActorRegistryTable.actor_id, actorID),
            eq(ActorRegistryTable.execution_revision, executionRevision), eq(ActorRegistryTable.last_error, row.last_error)))
          .returning({ actor_id: ActorRegistryTable.actor_id }).get()
      }, { behavior: "immediate" }))
    })

    const beginExecutionForInput = Effect.fn("ActorRegistry.beginExecutionForInput")(function* (
      sessionID: SessionID,
      actorID: string,
      claim: InputHandoffClaim,
    ) {
      const now = Date.now()
      const result = yield* Effect.sync(() => Database.transaction((db) => {
        if (!activeInTransaction(db,sessionID,actorID)) return { disposition: { kind: "unavailable", reason: "directory scope retired" } as const }
        const identity = and(
          eq(UserInputReceiptTable.owner, claim.owner),
          eq(UserInputReceiptTable.stable_chat_id, claim.stableChatID),
          eq(UserInputReceiptTable.request_id, claim.requestID),
        )
        const receipt = db.select().from(UserInputReceiptTable).where(identity).get()
        if (!receipt || receipt.session_id !== sessionID || receipt.actor_id !== actorID)
          return { disposition: { kind: "unavailable", reason: "receipt identity does not match this actor" } as const }
        if (receipt.map_revision > claim.mapRevision || receipt.mapping_revision !== claim.mappingRevision ||
          receipt.workspace_revision !== claim.workspaceRevision)
          return { disposition: { kind: "unavailable", reason: "managed binding changed" } as const }
        const handoffKey = and(
          eq(UserInputHandoffTable.owner, claim.owner),
          eq(UserInputHandoffTable.stable_chat_id, claim.stableChatID),
          eq(UserInputHandoffTable.request_id, claim.requestID),
          eq(UserInputHandoffTable.handoff_id, claim.handoffID),
        )
        const prior = db.select().from(UserInputHandoffTable).where(handoffKey).get()
        if (prior) return { disposition: prior.root_operation_id !== claim.rootOperationID
          ? { kind: "unavailable", reason: "handoff route identity conflicts with its durable attempt" } as const
          : { kind: "existing-claim", claimedExecutionRevision: prior.claimed_execution_revision, outcome: prior.outcome } as const }
        if (!["persisted", "pending-boundary"].includes(receipt.state))
          return { disposition: { kind: "unavailable", reason: "receipt is terminal" } as const }
        const actor = db.select().from(ActorRegistryTable).where(and(
          eq(ActorRegistryTable.session_id, sessionID), eq(ActorRegistryTable.actor_id, actorID),
        )).get()
        if (!actor || actor.last_error?.startsWith(MANAGED_SETTLEMENT_PREFIX) || actor.chat_id !== claim.stableChatID || actor.status !== "idle" ||
          actor.execution_revision !== claim.expectedExecutionRevision)
          return { disposition: { kind: "unavailable", reason: "actor is not idle at the expected execution revision" } as const }
        if (claim.hostContext && claim.hostContext.chatID !== claim.stableChatID)
          return { disposition: { kind: "unavailable", reason: "fresh host context does not match the bound chat" } as const }
        const selected = selectedExecutionRevision(db, receipt)
        if (selected !== undefined) {
          const claims = db.select().from(UserInputHandoffTable).where(and(eq(UserInputHandoffTable.owner, receipt.owner),
            eq(UserInputHandoffTable.stable_chat_id, receipt.stable_chat_id), eq(UserInputHandoffTable.request_id, receipt.request_id))).all()
          if (selected !== actor.execution_revision || claims.length || receipt.claimed_execution_revision !== null)
            return { disposition: { kind: "unavailable", reason: "selected execution cannot be rebound" } as const }
        }
        if (receipt.execution_revision !== actor.execution_revision) {
          const priorAttempts = db.select().from(UserInputHandoffTable).where(and(
            eq(UserInputHandoffTable.owner, claim.owner),
            eq(UserInputHandoffTable.stable_chat_id, claim.stableChatID),
            eq(UserInputHandoffTable.request_id, claim.requestID),
          )).all()
          if (receipt.execution_revision > actor.execution_revision || !priorAttempts.length ||
            priorAttempts.some((attempt) => attempt.outcome !== "settled-before-consumption"))
            return { disposition: { kind: "unavailable", reason: "receipt cannot be rebound to this execution" } as const }
        }
        const batch = claim.expectedReceipts ?? [{
          requestID: receipt.request_id, payloadHash: receipt.payload_hash, executionRevision: receipt.execution_revision,
          mapRevision: receipt.map_revision, mappingRevision: receipt.mapping_revision, workspaceRevision: receipt.workspace_revision,
        }]
        if (!batch.length || batch.length > 32 || batch[0]?.requestID !== claim.requestID ||
            new Set(batch.map((item) => item.requestID)).size !== batch.length)
          return { disposition: { kind: "unavailable", reason: "frozen input batch is invalid" } as const }
        for (const item of batch) {
          const row = db.select().from(UserInputReceiptTable).where(and(
            eq(UserInputReceiptTable.owner, claim.owner), eq(UserInputReceiptTable.stable_chat_id, claim.stableChatID),
            eq(UserInputReceiptTable.request_id, item.requestID),
          )).get()
          if (!row || row.session_id !== sessionID || row.actor_id !== actorID ||
              row.payload_hash !== item.payloadHash || (item.contentHash && boundaryContentHash({ ...row, selectedExecutionRevision: selectedExecutionRevision(db, row) }) !== item.contentHash) || row.execution_revision !== item.executionRevision ||
              row.map_revision !== item.mapRevision || row.mapping_revision !== item.mappingRevision ||
              row.workspace_revision !== item.workspaceRevision || !["persisted", "pending-boundary"].includes(row.state))
            return { disposition: { kind: "unavailable", reason: "frozen input batch changed before claim" } as const }
          const selectedRow = selectedExecutionRevision(db, row)
          if (selectedRow !== undefined) {
            const claims = db.select().from(UserInputHandoffTable).where(and(eq(UserInputHandoffTable.owner, row.owner),
              eq(UserInputHandoffTable.stable_chat_id, row.stable_chat_id), eq(UserInputHandoffTable.request_id, row.request_id))).all()
            if (selectedRow !== actor.execution_revision || claims.length || row.claimed_execution_revision !== null)
              return { disposition: { kind: "unavailable", reason: "selected input batch cannot be rebound" } as const }
          }
          if (row.execution_revision !== actor.execution_revision || row.claimed_execution_revision !== null) {
            const prior = db.select().from(UserInputHandoffTable).where(and(
              eq(UserInputHandoffTable.owner, row.owner), eq(UserInputHandoffTable.stable_chat_id, row.stable_chat_id),
              eq(UserInputHandoffTable.request_id, row.request_id),
            )).all()
            if (row.execution_revision > actor.execution_revision || !prior.length ||
                prior.some((attempt) => attempt.outcome !== "settled-before-consumption"))
              return { disposition: { kind: "unavailable", reason: "receipt batch cannot be rebound" } as const }
          }
        }
        const claimedRevision = actor.execution_revision + 1
        const actorRow = db.update(ActorRegistryTable).set({
          status: "running", execution_revision: claimedRevision, last_outcome: null, last_error: null, result_message_id: null,
          ...(claim.agent ? { agent: claim.agent } : {}),
          ...(claim.effectiveModel ? { effective_model: JSON.stringify(claim.effectiveModel) } : {}),
          ...(claim.hostContext ? {
            host_account_id: claim.hostContext.accountID ?? null,
            provider_grant_id: claim.hostContext.grantID ?? null,
            grant_revision: claim.hostContext.grantRevision ?? null,
            credential_revision: claim.hostContext.credentialRevision ?? null,
            chat_id: claim.hostContext.chatID ?? null,
            workspace_id: claim.hostContext.workspaceID ?? null,
            cwd: claim.hostContext.cwd ?? null,
            goal_id: claim.hostContext.goalID ?? null,
          } : {}),
          time_completed: null, time_updated: now,
        }).where(and(
          eq(ActorRegistryTable.session_id, sessionID), eq(ActorRegistryTable.actor_id, actorID),
          eq(ActorRegistryTable.status, "idle"), eq(ActorRegistryTable.execution_revision, actor.execution_revision),
          eq(ActorRegistryTable.chat_id, claim.stableChatID),
        )).returning().get()
        if (!actorRow) return { disposition: { kind: "unavailable", reason: "actor execution claim lost a concurrent race" } as const }
        for (const item of batch) {
          const receiptRow = db.update(UserInputReceiptTable).set({ claimed_execution_revision: claimedRevision, updated_at: now })
            .where(and(eq(UserInputReceiptTable.owner, claim.owner), eq(UserInputReceiptTable.stable_chat_id, claim.stableChatID),
              eq(UserInputReceiptTable.request_id, item.requestID), eq(UserInputReceiptTable.payload_hash, item.payloadHash),
              eq(UserInputReceiptTable.session_id, sessionID), eq(UserInputReceiptTable.actor_id, actorID),
              inArray(UserInputReceiptTable.state, ["persisted", "pending-boundary"])))
            .returning({ request_id: UserInputReceiptTable.request_id }).get()
          if (!receiptRow) throw new Error("receipt changed during execution claim")
        }
        for (const item of batch) db.insert(UserInputHandoffTable).values({
          owner: claim.owner, stable_chat_id: claim.stableChatID, request_id: item.requestID,
          handoff_id: claim.handoffID, session_id: sessionID, actor_id: actorID,
          root_operation_id: claim.rootOperationID,
          expected_execution_revision: actor.execution_revision, claimed_execution_revision: claimedRevision,
          outcome: "claimed", created_at: now, updated_at: now,
        }).run()
        return { disposition: { kind: "fresh-claim", claimedExecutionRevision: claimedRevision } as const, actorRow }
      }, { behavior: "immediate" }))
      if (result.actorRow) yield* publishSettledStatus(result.actorRow, sessionID, actorID)
      return result.disposition
    })

    const inspectInputHandoff = Effect.fn("ActorRegistry.inspectInputHandoff")(function* (
      sessionID: SessionID,
      actorID: string,
      claim: InputHandoffClaim,
    ) {
      const result = yield* Effect.sync(() => Database.use((db) => {
        const receipt = db.select().from(UserInputReceiptTable).where(and(
          eq(UserInputReceiptTable.owner, claim.owner),
          eq(UserInputReceiptTable.stable_chat_id, claim.stableChatID),
          eq(UserInputReceiptTable.request_id, claim.requestID),
        )).get()
        if (!receipt || receipt.session_id !== sessionID || receipt.actor_id !== actorID ||
          receipt.map_revision > claim.mapRevision || receipt.mapping_revision !== claim.mappingRevision ||
          receipt.workspace_revision !== claim.workspaceRevision) return undefined
        const prior = db.select().from(UserInputHandoffTable).where(and(
          eq(UserInputHandoffTable.owner, claim.owner),
          eq(UserInputHandoffTable.stable_chat_id, claim.stableChatID),
          eq(UserInputHandoffTable.request_id, claim.requestID),
          eq(UserInputHandoffTable.handoff_id, claim.handoffID),
        )).get()
        return prior ? (prior.root_operation_id !== claim.rootOperationID
          ? { kind: "unavailable", reason: "handoff route identity conflicts with its durable attempt" } as const : {
          kind: "existing-claim",
          claimedExecutionRevision: prior.claimed_execution_revision,
          outcome: prior.outcome,
        } as const) : undefined
      }))
      return result
    })

    const cancelPending = Effect.fn("ActorRegistry.cancelPending")(function* (
      sessionID: SessionID,
      actorID: string,
    ) {
      const row = yield* Effect.sync(() =>
        Database.use((db) =>
          db
            .update(ActorRegistryTable)
            .set({
              status: "idle",
              last_outcome: "cancelled",
              last_error: null,
              time_updated: Date.now(),
              time_completed: Date.now(),
            })
            .where(
              and(
                eq(ActorRegistryTable.session_id, sessionID),
                eq(ActorRegistryTable.actor_id, actorID),
                eq(ActorRegistryTable.status, "pending"),
              ),
            )
            .returning()
            .get(),
        ),
      )
      if (row) yield* publishSettledStatus(row, sessionID, actorID)
      return row ? fromRow(row) : undefined
    })

    const publishSettledStatus = (row: ActorRow, sessionID: SessionID, actorID: string) =>
      bus.publish(Events.ActorStatusChanged, {
        sessionID,
        actorID,
        status: row.status,
        ...(row.last_outcome ? { lastOutcome: row.last_outcome } : {}),
        turnCount: row.turn_count,
        lastTurnTime: row.last_turn_time,
        ...(row.last_error ? { error: row.last_error.startsWith(MANAGED_SETTLEMENT_PREFIX) ? "Managed execution requires reconciliation" : row.last_error } : {}),
        ...(row.requested_model ? { requestedModel: row.requested_model } : {}),
        ...(effectiveModelFromRow(row) ? { effectiveModel: effectiveModelFromRow(row) } : {}),
        ...(hostContextFromRow(row) ? { hostContext: hostContextFromRow(row) } : {}),
      })

    /** Close the input handoffs one execution revision claimed; runs inside the settling transaction. */
    const settleHandoffs = (db: Database.TxOrDb, sessionID: SessionID, actorID: string, executionRevision: number, now: number) => {
      const attempts = db.select().from(UserInputHandoffTable).where(and(
        eq(UserInputHandoffTable.session_id, sessionID),
        eq(UserInputHandoffTable.actor_id, actorID),
        eq(UserInputHandoffTable.claimed_execution_revision, executionRevision),
        inArray(UserInputHandoffTable.outcome, ["claimed", "running"]),
      )).all()
      for (const attempt of attempts) {
        const receipt = db.select({ state: UserInputReceiptTable.state }).from(UserInputReceiptTable).where(and(
          eq(UserInputReceiptTable.owner, attempt.owner),
          eq(UserInputReceiptTable.stable_chat_id, attempt.stable_chat_id),
          eq(UserInputReceiptTable.request_id, attempt.request_id),
        )).get()
        db.update(UserInputHandoffTable).set({
          outcome: receipt?.state === "consumed" ? "consumed" : "settled-before-consumption",
          updated_at: now,
        }).where(and(
          eq(UserInputHandoffTable.owner, attempt.owner),
          eq(UserInputHandoffTable.stable_chat_id, attempt.stable_chat_id),
          eq(UserInputHandoffTable.request_id, attempt.request_id),
          eq(UserInputHandoffTable.handoff_id, attempt.handoff_id),
        )).run()
      }
    }

    const settleExecution = Effect.fn("ActorRegistry.settleExecution")(function* (
      sessionID: SessionID,
      actorID: string,
      executionRevision: number,
      outcome: ActorOutcome,
      lastError?: string,
      managedSettlement?: ManagedSettlementIdentity,
      resultMessageID?: MessageID,
    ) {
      const now = Date.now()
      const row = yield* Effect.sync(() =>
        Database.transaction((db) => {
          const updated = db
            .update(ActorRegistryTable)
            .set({
              status: "idle",
              last_outcome: outcome,
              // A terminal write never reuses an older delivery (upstream TP-R14-11).
              result_message_id: resultMessageID ?? null,
              last_error: managedSettlement
                ? MANAGED_SETTLEMENT_PREFIX + JSON.stringify({ ...managedSettlement, executionRevision, ...(lastError ? { lastError } : {}) })
                : outcome === "failure" ? (lastError ?? null) : null,
              time_completed: now,
              time_updated: now,
            })
            .where(
              and(
                eq(ActorRegistryTable.session_id, sessionID),
                eq(ActorRegistryTable.actor_id, actorID),
                eq(ActorRegistryTable.execution_revision, executionRevision),
                eq(ActorRegistryTable.status, "running"),
              ),
            )
            .returning()
            .get()
          if (updated) settleHandoffs(db, sessionID, actorID, executionRevision, now)
          return updated
        }, { behavior: "immediate" }),
      )
      if (row) yield* publishSettledStatus(row, sessionID, actorID)
      return row !== undefined
    })

    const claimTerminalNotification = Effect.fn("ActorRegistry.claimTerminalNotification")(function* (
      sessionID: SessionID,
      actorID: string,
      executionRevision: number,
      outcome: ActorOutcome,
    ) {
      const row = yield* Effect.sync(() =>
        Database.use((db) =>
          db
            .update(ActorRegistryTable)
            .set({
              terminal_notification_revision: executionRevision,
              terminal_notification_outcome: outcome,
              time_updated: Date.now(),
            })
            .where(
              and(
                eq(ActorRegistryTable.session_id, sessionID),
                eq(ActorRegistryTable.actor_id, actorID),
                eq(ActorRegistryTable.execution_revision, executionRevision),
                eq(ActorRegistryTable.status, "idle"),
                eq(ActorRegistryTable.last_outcome, outcome),
                sql`(${ActorRegistryTable.terminal_notification_revision} IS NULL OR ${ActorRegistryTable.terminal_notification_revision} != ${executionRevision})`,
              ),
            )
            .returning()
            .get(),
        ),
      )
      return row !== undefined
    })

    const updateStatus = Effect.fn("ActorRegistry.updateStatus")(function* (
      sessionID: SessionID,
      actorID: string,
      patch: {
        status: ActorStatus
        lastOutcome?: ActorOutcome | undefined
        lastError?: string | undefined
        resultMessageID?: MessageID | undefined
      },
    ) {
      const now = Date.now()
      const isTerminal = patch.status === "idle" && patch.lastOutcome !== undefined
      // Running clears result_message_id so a wait in flight cannot resolve
      // against a delivery from the previous turn. Terminal writes the new
      // pointer (or null when settle produced no delivery) — a failure without
      // output must not reuse an older success (TP-R14-11).
      const set: Record<string, unknown> = {
        status: patch.status,
        time_updated: now,
        ...(isTerminal ? { time_completed: now, result_message_id: patch.resultMessageID ?? null } : {}),
        ...(patch.status === "running" ? { result_message_id: null } : {}),
      }
      if (patch.lastOutcome !== undefined) set.last_outcome = patch.lastOutcome
      if (patch.lastError !== undefined) set.last_error = patch.lastError
      else if (patch.lastOutcome !== undefined && patch.lastOutcome !== "failure") set.last_error = null
      yield* Effect.sync(() =>
        Database.use((db) =>
          db
            .update(ActorRegistryTable)
            .set(set)
            .where(and(eq(ActorRegistryTable.session_id, sessionID), eq(ActorRegistryTable.actor_id, actorID)))
            .run(),
        ),
      )
      // Re-read so the event payload reflects committed row values (not the
      // sparse patch). Skip publish if the row vanished between UPDATE and
      // SELECT — a dropped event beats a misleading one.
      const row = yield* Effect.sync(() =>
        Database.use((db) =>
          db
            .select()
            .from(ActorRegistryTable)
            .where(and(eq(ActorRegistryTable.session_id, sessionID), eq(ActorRegistryTable.actor_id, actorID)))
            .get(),
        ),
      )
      if (!row) return
      yield* bus.publish(Events.ActorStatusChanged, {
        sessionID,
        actorID,
        status: row.status,
        ...(row.last_outcome ? { lastOutcome: row.last_outcome } : {}),
        turnCount: row.turn_count,
        lastTurnTime: row.last_turn_time,
        ...(row.last_error ? { error: row.last_error } : {}),
        ...(row.requested_model ? { requestedModel: row.requested_model } : {}),
        ...(effectiveModelFromRow(row) ? { effectiveModel: effectiveModelFromRow(row) } : {}),
        ...(hostContextFromRow(row) ? { hostContext: hostContextFromRow(row) } : {}),
      })
    })

    const failOrphan = Effect.fn("ActorRegistry.failOrphan")(function* (
      sessionID: SessionID,
      actorID: string,
      error: string,
      isCurrent: () => boolean,
    ) {
      const result = yield* Effect.sync(() => {
        // The execution identity check and conditional write must share one scheduler turn.
        if (!isCurrent()) return "retry" as const
        const now = Date.now()
        return Database.transaction((db) => {
          const failed = db
            .update(ActorRegistryTable)
            .set({
              status: "idle",
              last_outcome: "failure",
              last_error: error,
              result_message_id: null,
              time_completed: now,
              time_updated: now,
            })
            .where(and(
              eq(ActorRegistryTable.session_id, sessionID),
              eq(ActorRegistryTable.actor_id, actorID),
              inArray(ActorRegistryTable.status, ["pending", "running"]),
              // A pending managed settlement stays fenced until its reconciliation.
              sql`COALESCE(${ActorRegistryTable.last_error}, '') NOT LIKE 'managed execution pending:%'`,
            ))
            .returning()
            .get()
          if (failed) settleHandoffs(db, sessionID, actorID, failed.execution_revision, now)
          return failed
        }, { behavior: "immediate" })
      })
      if (result === "retry") return "retry" as const
      if (!result) return "terminal" as const
      yield* bus.publish(Events.ActorStatusChanged, {
        sessionID,
        actorID,
        status: result.status,
        lastOutcome: "failure",
        turnCount: result.turn_count,
        lastTurnTime: result.last_turn_time,
        error,
      })
      return "failed" as const
    })

    const updateTurn = Effect.fn("ActorRegistry.updateTurn")(function* (sessionID: SessionID, actorID: string) {
      const now = Date.now()
      yield* Effect.sync(() =>
        Database.use((db) =>
          db
            .update(ActorRegistryTable)
            .set({
              last_turn_time: now,
              turn_count: sql`${ActorRegistryTable.turn_count} + 1`,
              time_updated: now,
            })
            .where(
              and(eq(ActorRegistryTable.session_id, sessionID), eq(ActorRegistryTable.actor_id, actorID)),
            )
          .run(),
        ),
      )
    })

    const updateAgent = Effect.fn("ActorRegistry.updateAgent")(function* (
      sessionID: SessionID,
      actorID: string,
      agent: string,
    ) {
      yield* Effect.sync(() =>
        Database.use((db) =>
          db
            .update(ActorRegistryTable)
            .set({ agent, time_updated: Date.now() })
            .where(and(eq(ActorRegistryTable.session_id, sessionID), eq(ActorRegistryTable.actor_id, actorID)))
            .run(),
        ),
      )
    })

    const updateModel = Effect.fn("ActorRegistry.updateModel")(function* (
      sessionID: SessionID,
      actorID: string,
      requestedModel?: string,
      effectiveModel?: ActorModel,
    ) {
      yield* Effect.sync(() =>
        Database.use((db) =>
          db
            .update(ActorRegistryTable)
            .set({
              ...(requestedModel !== undefined ? { requested_model: requestedModel } : {}),
              ...(effectiveModel !== undefined ? { effective_model: JSON.stringify(effectiveModel) } : {}),
              time_updated: Date.now(),
            })
            .where(and(eq(ActorRegistryTable.session_id, sessionID), eq(ActorRegistryTable.actor_id, actorID)))
            .run(),
        ),
      )
      const row = yield* Effect.sync(() =>
        Database.use((db) =>
          db
            .select()
            .from(ActorRegistryTable)
            .where(and(eq(ActorRegistryTable.session_id, sessionID), eq(ActorRegistryTable.actor_id, actorID)))
            .get(),
        ),
      )
      if (!row) return
      yield* bus.publish(Events.ActorStatusChanged, {
        sessionID,
        actorID,
        status: row.status,
        ...(row.last_outcome ? { lastOutcome: row.last_outcome } : {}),
        turnCount: row.turn_count,
        lastTurnTime: row.last_turn_time,
        ...(row.last_error ? { error: row.last_error } : {}),
        ...(row.requested_model ? { requestedModel: row.requested_model } : {}),
        ...(effectiveModelFromRow(row) ? { effectiveModel: effectiveModelFromRow(row) } : {}),
        ...(hostContextFromRow(row) ? { hostContext: hostContextFromRow(row) } : {}),
      })
    })

    const updateModelOptionsForExecution = Effect.fn("ActorRegistry.updateModelOptionsForExecution")(function* (
      sessionID: SessionID, actorID: string, executionRevision: number, options: import("./schema").ActorModelOptions,
    ) {
      const validated = ActorModelOptions.parse(options)
      return yield* Effect.sync(() => Database.use((db) => !!db.update(ActorRegistryTable).set({
        model_request_options: validated, time_updated: Date.now(),
      }).where(and(eq(ActorRegistryTable.session_id, sessionID), eq(ActorRegistryTable.actor_id, actorID),
        eq(ActorRegistryTable.status, "running"), eq(ActorRegistryTable.execution_revision, executionRevision)))
        .returning({ actor_id: ActorRegistryTable.actor_id }).get()))
    })

    const updateHostContextForExecution = Effect.fn("ActorRegistry.updateHostContextForExecution")(function* (
      sessionID: SessionID, actorID: string, executionRevision: number, context: ActorHostContext,
    ) {
      return yield* Effect.sync(() => Database.use((db) => !!db.update(ActorRegistryTable).set({
        host_account_id: context.accountID ?? null, provider_grant_id: context.grantID ?? null,
        grant_revision: context.grantRevision ?? null, credential_revision: context.credentialRevision ?? null,
        chat_id: context.chatID ?? null, workspace_id: context.workspaceID ?? null, cwd: context.cwd ?? null,
        goal_id: context.goalID ?? null, time_updated: Date.now(),
      }).where(and(eq(ActorRegistryTable.session_id, sessionID), eq(ActorRegistryTable.actor_id, actorID),
        eq(ActorRegistryTable.status, "running"), eq(ActorRegistryTable.execution_revision, executionRevision)))
        .returning({ actor_id: ActorRegistryTable.actor_id }).get()))
    })

    const updateHostContext = Effect.fn("ActorRegistry.updateHostContext")(function* (
      sessionID: SessionID,
      actorID: string,
      context: ActorHostContext,
    ) {
      yield* Effect.sync(() =>
        Database.use((db) =>
          db
            .update(ActorRegistryTable)
            .set({
              host_account_id: context.accountID ?? null,
              provider_grant_id: context.grantID ?? null,
              grant_revision: context.grantRevision ?? null,
              credential_revision: context.credentialRevision ?? null,
              chat_id: context.chatID ?? null,
              workspace_id: context.workspaceID ?? null,
              cwd: context.cwd ?? null,
              goal_id: context.goalID ?? null,
              time_updated: Date.now(),
            })
            .where(and(eq(ActorRegistryTable.session_id, sessionID), eq(ActorRegistryTable.actor_id, actorID)))
            .run(),
        ),
      )
      const row = yield* Effect.sync(() =>
        Database.use((db) =>
          db
            .select()
            .from(ActorRegistryTable)
            .where(and(eq(ActorRegistryTable.session_id, sessionID), eq(ActorRegistryTable.actor_id, actorID)))
            .get(),
        ),
      )
      if (!row) return
      yield* bus.publish(Events.ActorStatusChanged, {
        sessionID,
        actorID,
        status: row.status,
        ...(row.last_outcome ? { lastOutcome: row.last_outcome } : {}),
        turnCount: row.turn_count,
        lastTurnTime: row.last_turn_time,
        ...(row.last_error ? { error: row.last_error } : {}),
        ...(row.requested_model ? { requestedModel: row.requested_model } : {}),
        ...(effectiveModelFromRow(row) ? { effectiveModel: effectiveModelFromRow(row) } : {}),
        ...(hostContextFromRow(row) ? { hostContext: hostContextFromRow(row) } : {}),
      })
    })

    const get = Effect.fn("ActorRegistry.get")(function* (sessionID: SessionID, actorID: string) {
      const row = yield* Effect.sync(() =>
        Database.use((db) =>
          db
            .select()
            .from(ActorRegistryTable)
            .where(and(eq(ActorRegistryTable.session_id, sessionID), eq(ActorRegistryTable.actor_id, actorID)))
            .get(),
        ),
      )
      return row ? fromRow(row) : undefined
    })

    const liveness = Effect.fn("ActorRegistry.liveness")(function* (
      sessionID: SessionID,
      actorID: string,
      stallMs?: number,
    ) {
      const actor = yield* get(sessionID, actorID)
      if (!actor) return undefined
      return { liveness: deriveLiveness(actor, Date.now(), stallMs), actor }
    })

    // One scoped bounded projection, not a per-actor lookup. Legacy profiles
    // and all-NULL legacy rows retain the original description-only shape.
    const withDisplay = (db: Database.TxOrDb, rows: ActorRow[]): Actor[] => {
      if (rows.length > 501) throw new Error("actor display page unavailable")
      const actors = rows.map(fromRow)
      const columns = db.all<{name:string}>(sql`PRAGMA table_info(actor_registry)`)
      if (!rows.length || !columns.some(x=>x.name==="display_ordinal")) return actors
      const ownerColumn = columns.some(x=>x.name==="display_owner_account_id") ? sql.raw("display_owner_account_id") : sql`NULL`
      const sessionID = rows[0]!.session_id
      if (rows.some(row=>row.session_id!==sessionID)) throw new Error("actor display scope unavailable")
      const ids = sql.join(rows.map(row=>sql`${row.actor_id}`),sql`, `)
      const labels = db.all<{actor_id:string,execution_revision:number,ordinal:number|null,label:string|null,revision:number|null,origin:string|null,owner:string|null}>(sql`
        SELECT actor_id,execution_revision,${ownerColumn} AS owner,display_ordinal AS ordinal,
          CASE WHEN length(CAST(display_label AS BLOB)) <= 200 THEN display_label END AS label,
          display_revision AS revision,display_origin AS origin FROM actor_registry
        WHERE session_id=${sessionID} AND actor_id IN (${ids})`)
      if (labels.length !== rows.length) throw new Error("actor display source changed")
      const byID = new Map(labels.map(label=>[label.actor_id,label]))
      return actors.map(actor=>{
        const value=byID.get(actor.actorID)
        if (!value || value.execution_revision !== actor.executionRevision) throw new Error("actor display unavailable")
        if (value.ordinal===null && value.label===null && value.revision===null && value.origin===null && value.owner===null) return actor
        if (!Number.isSafeInteger(value.ordinal) || value.ordinal! < 1 || !Number.isSafeInteger(value.revision) || value.revision! < 0 ||
            typeof value.label!=="string" || !value.label.trim() || Buffer.byteLength(value.label,"utf8")>200 ||
            /[\u0000-\u001f\u007f-\u009f]|[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/u.test(value.label) ||
            !["deterministic","helper","manual"].includes(value.origin ?? "") ||
            (value.owner!==null && (typeof value.owner!=="string" || !value.owner || Buffer.byteLength(value.owner,"utf8")>256))) throw new Error("actor display unavailable")
        return {...actor,display:{ordinal:value.ordinal!,label:value.label,revision:value.revision!,origin:value.origin as "deterministic"|"helper"|"manual",renameAvailable:value.owner!==null}}
      })
    }

    const pageBySession = Effect.fn("ActorRegistry.pageBySession")(function* (input: { sessionID: SessionID; limit: number; after?: string }) {
      if (!Number.isSafeInteger(input.limit) || input.limit < 1 || input.limit > 100) throw new Error("observation page limit is invalid")
      const items = yield* Effect.sync(() => Database.use((db) => withDisplay(db, db.select().from(ActorRegistryTable).where(and(
        eq(ActorRegistryTable.session_id, input.sessionID), ...(input.after ? [gt(ActorRegistryTable.actor_id, input.after)] : []),
      )).orderBy(ActorRegistryTable.actor_id).limit(input.limit + 1).all())))
      const rows = items.slice()
      items.splice(input.limit)
      return { items, ...(rows.length > input.limit && items.length ? { nextCursor: items[items.length - 1]!.actorID } : {}) }
    })

    const isPeerSession = Effect.fn("ActorRegistry.isPeerSession")(function* (sessionID: SessionID) {
      return yield* Effect.sync(() => Database.use((db) => !!db.select({ actor_id: ActorRegistryTable.actor_id })
        .from(ActorRegistryTable).where(and(eq(ActorRegistryTable.session_id, sessionID), eq(ActorRegistryTable.mode, "peer")))
        .limit(1).get()))
    })

    const listBySession = Effect.fn("ActorRegistry.listBySession")(function* (sessionID: SessionID) {
      const rows = yield* Effect.sync(() =>
        Database.use((db) =>
          db.select().from(ActorRegistryTable).where(eq(ActorRegistryTable.session_id, sessionID)).all(),
        ),
      )
      return rows.map(fromRow)
    })

    const listActive = Effect.fn("ActorRegistry.listActive")(function* () {
      const rows = yield* Effect.sync(() =>
        Database.use((db) =>
          db
            .select()
            .from(ActorRegistryTable)
            .where(
              and(inArray(ActorRegistryTable.status, ["pending", "running"]), eq(ActorRegistryTable.background, true)),
            )
            .all(),
        ),
      )
      return rows.map(fromRow)
    })

    const listByParent = Effect.fn("ActorRegistry.listByParent")(function* (
      sessionID: SessionID,
      parentActorID: string,
    ) {
      const rows = yield* Effect.sync(() =>
        Database.use((db) =>
          db
            .select()
            .from(ActorRegistryTable)
            .where(
              and(eq(ActorRegistryTable.session_id, sessionID), eq(ActorRegistryTable.parent_actor_id, parentActorID)),
            )
            .all(),
        ),
      )
      return rows.map(fromRow)
    })

    // Peer children register with session_id === actor_id === their OWN child
    // session id (Actor.spawnPeer), so listByParent — which filters on
    // session_id === the parent's id — can never match them. The reliable
    // parent link is the Session row's parent_id. Join on it so a caller with
    // no Session.Service (e.g. the LLM layer building the orchestrator's
    // fleet roster) can still enumerate its peer children, and
    // carry the child's title along since that is the routing signal.
    const listPeerChildren = Effect.fn("ActorRegistry.listPeerChildren")(function* (
      parentSessionID: SessionID,
      parentActorID: string,
    ) {
      const rows = yield* Effect.sync(() =>
        Database.use((db) =>
          db
            .select({ actor: ActorRegistryTable, title: SessionTable.title })
            .from(ActorRegistryTable)
            .innerJoin(SessionTable, eq(SessionTable.id, ActorRegistryTable.session_id))
            .where(
              and(
                eq(SessionTable.parent_id, parentSessionID),
                eq(ActorRegistryTable.mode, "peer"),
                eq(ActorRegistryTable.parent_actor_id, parentActorID),
              ),
            )
            .all(),
        ),
      )
      return rows.map((row) => ({ actor: fromRow(row.actor), title: row.title }))
    })

    const renderForAgent = Effect.fn("ActorRegistry.renderForAgent")(function* (sessionID: SessionID) {
      const actors = yield* listBySession(sessionID)
      const active = actors.filter(
        (actor) => actor.background && (actor.status === "pending" || actor.status === "running"),
      )
      if (active.length === 0) return ""

      const lines: string[] = []
      lines.push("## Active Actors")
      lines.push("")
      lines.push(`You have ${active.length} background actor(s) registered. Interact via the \`actor\` tool.`)
      lines.push("")
      const now = Date.now()
      for (const actor of active) {
        const idleMs = now - actor.lastTurnTime
        const idle = idleMs < 60_000 ? `${Math.floor(idleMs / 1000)}s` : `${Math.floor(idleMs / 60_000)}m`
        lines.push(`- actor_id: ${actor.actorID} (${actor.status}, last activity ${idle} ago)`)
        lines.push(`  description: ${actor.description}`)
        lines.push(`  agent: ${actor.agent}`)
      }
      return lines.join("\n")
    })

    const agentTypeFor = Effect.fn("ActorRegistry.agentTypeFor")(function* (sessionID: SessionID, actorID: string) {
      if (actorID === "main") return "main"
      const actor = yield* get(sessionID, actorID)
      return actor?.agent ?? "main"
    })

    const isSystemSpawned = Effect.fn("ActorRegistry.isSystemSpawned")(function* (
      sessionID: SessionID,
      actorID: string,
    ) {
      if (actorID === "main") return false
      const actor = yield* get(sessionID, actorID)
      if (!actor) return false
      return SYSTEM_SPAWNED_AGENT_TYPES.has(actor.agent)
    })

    // Whether this actor's context is maintained by the session checkpoint flow.
    // Checkpoint serves main + peer only; subagents use per-actor compaction, and
    // system-spawned agents (checkpoint-writer/dream/distill) maintain nothing.
    // Single source of truth for both the memory-instructions gate (LLM.buildSystemArray)
    // and the checkpoint self-trigger gate (SessionPrune.fireCheckpoints). Two
    // orthogonal exclusions kept explicit (agent TYPE vs MODE) so a future system
    // agent spawned as mode:"peer" can't silently slip back in — see prune.ts.
    const servesCheckpoint = Effect.fn("ActorRegistry.servesCheckpoint")(function* (
      sessionID: SessionID,
      actorID: string | undefined,
    ) {
      // No agentID (or literal "main") → main runLoop. Fail open: main and peer
      // must never silently lose checkpoints / memory instructions. "main" has no
      // registry row, so short-circuit before the read.
      if (!actorID || actorID === "main") return true
      // Single read, two orthogonal exclusions derived from it: agent TYPE
      // (system-spawned) and actor MODE (subagent). Unregistered/race → fail open.
      const actor = yield* get(sessionID, actorID)
      if (!actor) return true
      if (SYSTEM_SPAWNED_AGENT_TYPES.has(actor.agent)) return false
      return actor.mode !== "subagent"
    })

    // Compatibility helper only computes a label. New subagent rows use
    // registerAllocated, which chooses and inserts under BEGIN IMMEDIATE.
    const allocateActorID = Effect.fn("ActorRegistry.allocateActorID")(function* (
      sessionID: SessionID,
      agentType: string,
    ) {
      return yield* Effect.sync(() => {
        const existing = Database.use((db) =>
          db
            .select({ actor_id: ActorRegistryTable.actor_id })
            .from(ActorRegistryTable)
            .where(eq(ActorRegistryTable.session_id, sessionID))
            .all(),
        )
        const prefix = `${agentType}-`
        let max = 0
        for (const row of existing) {
          if (row.actor_id.startsWith(prefix)) {
            const suffix = row.actor_id.slice(prefix.length)
            if (!/^[1-9]\d*$/.test(suffix)) continue
            const n = Number(suffix)
            if (Number.isSafeInteger(n) && n > max) max = n
          }
        }
        return `${agentType}-${max + 1}`
      })
    })

    // Initialization cannot infer execution failure from another instance ID.
    // Only the executor settles its turn; no background scan sends stall notifications.
    //
    // Time-based zombie sweep: a crashed process leaves running/pending rows
    // that nothing will ever settle. deriveLiveness already returns "idle" for
    // them at read time (abandon threshold), but the DB row stays non-terminal
    // and ActorWaiter only resolves on status==='idle'. Persist the terminal
    // state once at init so restarts don't leave parents waiting forever.
    // Exclude this process's instance_id: a same-process layer rebuild must
    // never settle a still-running fiber that merely has not written a part
    // for >abandon (long LLM step). Other-instance abandoned rows are fair
    // game — their process is gone or silent past the abandon threshold.
    //
    // Bootstrap settles actor metadata only. Question parts follow the selected
    // session's prompt/run cleanup; registry initialization never scans history.

    return Service.of({
      admitExecutionDirectory,
      directoryRetirementSupported,
      assertDirectoryActive,
      snapshotDirectoryScopes,
      verifyRetiredDirectoryScopes,
      directoryScopePaths,
      retireDirectoryScopes,
      admitDirectory,
      withDirectoryClosing,
      register,
      registerAllocated,
      getDisplayOwner,
      workerNamingCandidate,
      applyWorkerName,
      renameDisplay,
      getForkContext,
      updateStatus,
      beginExecution,
      prepareMainPreflightModel,
      beginExecutionForInput,
      inspectInputHandoff,
      cancelPending,
      settleExecution,
      claimTerminalNotification,
      failOrphan,
      updateTurn,
      updateAgent,
      updateModel,
      updateHostContext,
      updateHostContextForExecution,
      updateModelOptionsForExecution,
      markExecutionUncertain,
      clearManagedSettlement,
      pendingManagedSettlement,
      uncertainManagedExecution,
      get,
      liveness,
      listBySession,
      pageBySession,
      isPeerSession,
      listActive,
      listByParent,
      listPeerChildren,
      renderForAgent,
      agentTypeFor,
      isSystemSpawned,
      servesCheckpoint,
      allocateActorID,
    })
  }),
)

export const defaultLayer = layer.pipe(Layer.provide(Bus.layer))

export * as ActorRegistry from "./registry"
