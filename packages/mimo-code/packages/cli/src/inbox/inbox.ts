import { Context, Effect, Layer, Schema, Option } from "effect"
import { Database, and, eq, lte, inArray } from "@/storage"
import { createHash } from "node:crypto"
import { ActorRegistryTable } from "@/actor/actor.sql"
import { DEFAULT_LIVENESS_STALL_MS } from "@/actor/schema"
import { TaskTable } from "@/task/task.sql"
import { ActorRegistry } from "@/actor/registry"
import { Session } from "@/session"
import { MessageID, PartID } from "@/session/schema"
import type { SessionID } from "@/session/schema"
import { InboxTable } from "./inbox.sql"
import { ActorNotificationEvent, renderInboxRow } from "./render"
import { inboxServiceRef, defaultModelRef } from "./inbox-ref"
import * as Controller from "./controller"
import type { AdmissionGuard } from "./controller"
import type { ProviderID, ModelID } from "@/provider/schema"
import { bindScopedRef } from "@/effect/scoped-ref"
import * as ManagedProvider from "@/acp/managed-provider"

const GC_TTL_MS = 7 * 24 * 60 * 60 * 1000

export function gcInboxRows(cutoffMs: number) {
  return Effect.sync(() =>
    Database.use((db) => db.delete(InboxTable).where(and(
      inArray(InboxTable.state, ["settled", "cancelled", "rejected"]),
      lte(InboxTable.time_updated, cutoffMs),
    )).run()),
  )
}

export class InboxReceiverNotFound extends Schema.TaggedErrorClass<InboxReceiverNotFound>()(
  "InboxReceiverNotFound",
  { receiverSessionID: Schema.String, receiverActorID: Schema.String },
) {}

export class InboxInvalidMessage extends Schema.TaggedErrorClass<InboxInvalidMessage>()(
  "InboxInvalidMessage",
  { reason: Schema.String },
) {}

export interface DrainSeed {
  agent: string
  model: { providerID: ProviderID; modelID: ModelID; variant?: string }
}

export function resolveDrainSeed(
  sessions: Session.Interface,
  reg: ActorRegistry.Interface,
  sessionID: SessionID,
  actorID: string,
): Effect.Effect<DrainSeed | undefined> {
  return Effect.gen(function* () {
    const actor = yield* reg.get(sessionID, actorID)
    if (actor && actor.mode !== "main" && actor.effectiveModel) {
      const variant = actor.modelRequestOptions?.options.reasoning_effort
      return { agent:actor.agent, model:{
        providerID:actor.effectiveModel.providerID as ProviderID,
        modelID:actor.effectiveModel.modelID as ModelID,
        ...(variant ? { variant } : {}),
      } }
    }
    const match = yield* sessions.findMessage(
      sessionID,
      (m) =>
        (m.info.role === "user" || m.info.role === "assistant") &&
        "model" in m.info &&
        m.info.model !== undefined &&
        m.info.model.providerID !== "system" &&
        "agent" in m.info &&
        m.info.agent !== "system",
      { agentID: actor?.mode === "peer" && !ManagedProvider.enabled() ? "*" : actorID },
    )
    if (Option.isSome(match)) {
      const info = match.value.info
      if ("model" in info && info.model && "agent" in info) return { agent: info.agent, model: info.model }
    }
    const resolver = defaultModelRef.current
    if (actor?.mode === "peer" && resolver && !ManagedProvider.enabled()) {
      const model = yield* resolver.defaultModel()
      return { agent: actor.agent, model: { providerID: model.providerID, modelID: model.modelID } }
    }
    return undefined
  })
}

export interface AgentInput {
  receiverSessionID: SessionID
  receiverActorID: string
  senderSessionID?: SessionID
  senderActorID?: string
  content?: string
  type?: string
  /** Trusted runtime-only lifecycle input; never supplied by model-facing tools. */
  notification?: ActorNotificationEvent
  legacyInboxID?: string
  terminalClaim?: { sessionID: SessionID; actorID: string; revision: number; outcome: "success" | "failure" | "cancelled" }
}

type QuietParent = {
  sessionID: SessionID
  childActorID: string
  childRevision: number
  parentActorID: string
  parentRevision: number
}

const QUIET_GROUP_CAP = 32
const QUIET_RECEIPT_WINDOW_MS = 60 * 60 * 1000

// Read scoped progress metadata only. Database.use reuses the controller's
// transaction when its admission guard calls this same observation again.
function quietWitness(input: QuietParent) {
  return Database.use((db) => {
    const parent=db.select({revision:ActorRegistryTable.execution_revision,status:ActorRegistryTable.status})
      .from(ActorRegistryTable).where(and(eq(ActorRegistryTable.session_id,input.sessionID),
        eq(ActorRegistryTable.actor_id,input.parentActorID))).get()
    if (parent?.revision!==input.parentRevision || parent.status!=="running") return
    const peers=db.select({id:ActorRegistryTable.actor_id,revision:ActorRegistryTable.execution_revision,status:ActorRegistryTable.status,
      created:ActorRegistryTable.time_created,activity:ActorRegistryTable.last_activity_time})
      .from(ActorRegistryTable).where(and(eq(ActorRegistryTable.session_id,input.sessionID),
        eq(ActorRegistryTable.parent_actor_id,input.parentActorID),eq(ActorRegistryTable.mode,"subagent"),
        eq(ActorRegistryTable.background,true)))
      .orderBy(ActorRegistryTable.actor_id).limit(QUIET_GROUP_CAP+1).all()
    if (!peers.length || peers.length>QUIET_GROUP_CAP) return
    const child=peers.find(peer=>peer.id===input.childActorID)
    if (!child || child.revision!==input.childRevision || child.status!=="running") return
    const tasks=db.select({id:TaskTable.id,owner:TaskTable.owner,status:TaskTable.status,activity:TaskTable.last_event_at})
      .from(TaskTable).where(and(eq(TaskTable.session_id,input.sessionID),inArray(TaskTable.owner,peers.map(peer=>peer.id)),
        inArray(TaskTable.status,["open","in_progress","blocked"])))
      .orderBy(TaskTable.id).limit(QUIET_GROUP_CAP+1).all()
    if (tasks.length>QUIET_GROUP_CAP || !tasks.some(task=>task.owner===child.id && task.status==="in_progress")) return
    const participating=peers.filter(peer=>peer.status!=="idle" || tasks.some(task=>task.owner===peer.id))
    const last=Math.max(...participating.map(peer=>peer.activity ?? peer.created),...tasks.map(task=>task.activity))
    const quietFor=Date.now()-last
    if (!Number.isSafeInteger(last) || quietFor<DEFAULT_LIVENESS_STALL_MS || quietFor>QUIET_RECEIPT_WINDOW_MS) return
    const cursor=createHash("sha256").update(JSON.stringify([input.sessionID,input.parentActorID,participating,tasks])).digest("hex")
    return { cursor, quietFor, count:participating.length }
  })
}

export interface Interface {
  readonly quietParent?: (input:QuietParent) => Effect.Effect<boolean>
  readonly accept: (input: AgentInput, guard?: AdmissionGuard) => Effect.Effect<{
    inboxID: string
    inputMessageID: MessageID
    disposition: "inserted" | "existing"
  }, InboxReceiverNotFound | InboxInvalidMessage>
}

export class Service extends Context.Service<Service, Interface>()("@opencode/Inbox") {}

export const layer = Layer.effect(
  Service,
  Effect.gen(function* () {
    const reg = yield* ActorRegistry.Service
    const sessions = yield* Session.Service
    const controller = yield* Controller.Service
    yield* gcInboxRows(Date.now() - GC_TTL_MS)

    const accept = Effect.fn("Inbox.acceptAgent")(function* (input: AgentInput, guard?: AdmissionGuard, quietKey?:string) {
      const receiver = yield* reg.get(input.receiverSessionID, input.receiverActorID)
      if (!receiver && !input.legacyInboxID) return yield* Effect.fail(new InboxReceiverNotFound({
        receiverSessionID: input.receiverSessionID,
        receiverActorID: input.receiverActorID,
      }))
      const type = input.type ?? "text"
      // A lifecycle notification is queued only from a validated structured
      // event, so model-supplied text can never become lifecycle state.
      const content =
        type === "actor_notification"
          ? input.notification && ActorNotificationEvent.safeParse(input.notification).success
            ? { notification: ActorNotificationEvent.parse(input.notification) }
            : yield* Effect.fail(
                new InboxInvalidMessage({
                  reason: "actor_notification requires a validated structured notification event",
                }),
              )
          : typeof input.content === "string"
            ? { text: input.content }
            : yield* Effect.fail(new InboxInvalidMessage({ reason: "inbox message content must be text" }))
      if (input.terminalClaim) {
        const claim = input.terminalClaim
        const event = input.notification
        const outcome = event?.status === "completed" ? "success" : event?.status === "failed" ? "failure" : event?.status === "cancelled" ? "cancelled" : undefined
        if (type !== "actor_notification" || input.senderSessionID !== claim.sessionID || input.senderActorID !== claim.actorID ||
            event?.actorID !== claim.actorID || outcome !== claim.outcome)
          return yield* Effect.fail(new InboxInvalidMessage({ reason: "terminal claim does not match notification identity and outcome" }))
      }
      const seed = yield* resolveDrainSeed(sessions, reg, input.receiverSessionID, input.receiverActorID)
      if (!seed && receiver && receiver.mode !== "main" && ManagedProvider.enabled()
          && type !== "actor_notification" && !input.legacyInboxID)
        return yield* Effect.fail(new InboxInvalidMessage({ reason: "The receiver's existing execution model is unavailable" }))
      const id = MessageID.ascending()
      const now = Date.now()
      const text = renderInboxRow({
        type,
        content,
        sender_session_id: input.senderSessionID,
        sender_actor_id: input.senderActorID,
        created_at: now,
      })
      if (!text.trim()) return yield* Effect.die(new Error("Inbox input rendered empty"))
      const accepted = yield* controller.acceptDetailed({
        lane: { sessionID: input.receiverSessionID, agentID: input.receiverActorID },
        message: {
          id,
          sessionID: input.receiverSessionID,
          agentID: input.receiverActorID,
          role: "user",
          time: { created: now },
          agent: seed?.agent ?? "system",
          ...(input.senderSessionID || input.senderActorID ? { provenance: {
            source: "agent" as const,
            senderSessionID: input.senderSessionID, senderActorID: input.senderActorID,
          } } : {}),
          model: seed?.model ?? { providerID: "system" as ProviderID, modelID: "system" as ModelID },
        },
        parts: [{
          id: PartID.ascending(),
          messageID: id,
          sessionID: input.receiverSessionID,
          type: "text",
          synthetic: true,
          text,
        }],
        type,
        legacyInboxID: input.legacyInboxID,
        terminalClaim: input.terminalClaim,
        idempotencyKey: input.terminalClaim ? JSON.stringify(["actor-terminal", input.terminalClaim.sessionID, input.terminalClaim.actorID, input.terminalClaim.revision]) : quietKey,
        senderSessionID: input.senderSessionID,
        senderActorID: input.senderActorID,
      }, guard)
      return { inboxID: accepted.receipt.id, inputMessageID: accepted.receipt.inputMessageID, disposition: accepted.disposition }
    })
    const quietParent = Effect.fn("Inbox.quietParent")(function* (input:QuietParent) {
      const observed=yield* Effect.sync(()=>quietWitness(input))
      if (!observed) return false
      const receipt=yield* accept({
        receiverSessionID:input.sessionID,receiverActorID:input.parentActorID,
        senderSessionID:input.sessionID,senderActorID:input.childActorID,
        type:"actor_notification",
        notification:{actorID:input.childActorID,description:`Observed worker group (${observed.count})`,status:"stalled",
          stalledForMs:observed.quietFor},
      },{deferKick:true,isCancelled:()=>quietWitness(input)?.cursor!==observed.cursor},`actor-quiet:${observed.cursor}`)
        .pipe(Effect.exit)
      return receipt._tag==="Success" && receipt.value.disposition==="inserted"
    })
    const impl = Service.of({ accept, quietParent })
    yield* bindScopedRef(inboxServiceRef, impl)
    return impl
  }),
).pipe(Layer.provide(Controller.defaultLayer))

export const defaultLayer = layer.pipe(
  Layer.provide(ActorRegistry.defaultLayer),
  Layer.provide(Session.defaultLayer),
)
