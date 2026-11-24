import { currentToolTiming, type QueuedToolTiming } from "../session/tool-timing"
import { Context, Effect, Layer, Queue } from "effect"
import { Database, eq } from "../storage"
import { Bus } from "../bus"
import { MessageV2 } from "../session/message-v2"
import { Config } from "../config"
import { InstanceState } from "../effect"
import { HistoryFtsTable } from "./fts.sql"
import { extract, DEFAULT_KINDS, type Kind } from "./extract"
import { makeResolver, type Resolver } from "./resolve"
import { Log } from "../util"
import { ManagedProvider } from "@/acp/managed-provider"
import { managedSessionBinding } from "@/memory/session-scope"
import { createHash } from "node:crypto"
import type { HistorySourceAck } from "@/acp/openclank-protocol"

const log = Log.create({ service: "history.writer" })

type Job =
  | { type: "upsert"; part: MessageV2.Part; time: number; timing?: QueuedToolTiming; token?: object }
  | { type: "delete"; sessionID: string; messageID: string; partID: string }

export interface Interface {
  readonly init: () => Effect.Effect<void>
  readonly sourceAck: (sessionID:string, actorID:string, partID:string, contentHash:string) => Effect.Effect<HistorySourceAck | undefined>
}

export class Service extends Context.Service<Service, Interface>()("@opencode/History.Writer") {}

export const layer: Layer.Layer<Service, never, Config.Service | Bus.Service> = Layer.effect(
  Service,
  Effect.gen(function* () {
    const cfg = yield* Config.Service
    const bus = yield* Bus.Service

    const state = yield* InstanceState.make<{ started: boolean; lookup?: Interface['sourceAck'] }>(
      Effect.fn("History.Writer.state")(function* (_ctx) {
        const config = yield* cfg.get()
        const kinds = config.history?.kinds ?? DEFAULT_KINDS
        const enabled = new Set<Kind>(kinds as readonly Kind[])
        if (!ManagedProvider.enabled() && enabled.size === 0) return { started: true }

        const queue = yield* Queue.unbounded<Job>()
        const resolver = makeResolver()
        const receipts = new Map<string,{generation:object; ack:HistorySourceAck}>()
        const versions = new Map<string,object>()
        const keyFor = (sessionID:string,partID:string) => `${sessionID}\0${partID}`
        const invalidate = (sessionID:string,partID:string) => {
          const key=keyFor(sessionID,partID), token={}
          receipts.delete(key);versions.delete(key);versions.set(key,token)
          if (versions.size>512) { const oldest=versions.keys().next().value; if (oldest) {versions.delete(oldest);receipts.delete(oldest)} }
          return token
        }

        // Use subscribeCallback (synchronous PubSub.subscribe) so subscriptions
        // are guaranteed live before init() returns. Stream-based subscribe is
        // lazy (Stream.unwrap) and would race with immediate publishes.
        yield* bus.subscribeCallback(MessageV2.Event.PartUpdated, (evt) => {
          const part=evt.properties.part
          const token=invalidate(part.sessionID,part.id)
          Queue.offerUnsafe(queue, { type: "upsert", part, time: evt.properties.time, timing: currentToolTiming(part), token })
        })
        yield* bus.subscribeCallback(MessageV2.Event.PartRemoved, (evt) => {
          invalidate(evt.properties.sessionID,evt.properties.partID)
          Queue.offerUnsafe(queue, {
            type: "delete",
            sessionID: evt.properties.sessionID,
            messageID: evt.properties.messageID,
            partID: evt.properties.partID,
          })
        })

        yield* Effect.forever(
          Effect.gen(function* () {
            const job = yield* Queue.take(queue)
            yield* handle(job, resolver, enabled, (part,timing,reply,generation,token) => {
              const ack=reply.sourceAck, key=keyFor(part.sessionID,part.id), binding=managedSessionBinding(part.sessionID)
              if (!ack || typeof ack!=='object' || Array.isArray(ack) || Object.keys(ack).sort().join(',')!==
                  'accountID,actorID,chatID,contentHash,messageID,outputBytes,outputSHA256,partID,revision,sessionID,version' ||
                  typeof ack.accountID!=='string' || !ack.accountID || ack.accountID.length>256 ||
                  !timing || !generation || ManagedProvider.historySourceGeneration()!==generation || versions.get(key)!==token ||
                  !binding || ack.version!==1 || ack.chatID!==binding.stableChatID || ack.sessionID!==part.sessionID ||
                  ack.actorID!==timing.proof.actorID || ack.messageID!==part.messageID || ack.partID!==part.id ||
                  ack.contentHash!==timing.proof.contentSHA256 || part.type!=='tool' || part.state.status!=='completed' ||
                  ack.outputSHA256!==createHash('sha256').update(part.state.output,'utf8').digest('hex') ||
                  ack.outputBytes!==Buffer.byteLength(part.state.output,'utf8') || !Number.isSafeInteger(ack.revision) || ack.revision<1) return
              receipts.set(key,{generation,ack:{...ack}})
            }).pipe(
              Effect.catchCause((cause) =>
                Effect.sync(() => log.warn("write failed", { cause: String(cause) })),
              ),
            )
          }),
        ).pipe(Effect.forkScoped)

        return { started: true, lookup:(sessionID,actorID,partID,contentHash) => Effect.sync(() => {
          const key=keyFor(sessionID,partID), found=receipts.get(key)
          if (!found || ManagedProvider.historySourceGeneration()!==found.generation || found.ack.actorID!==actorID || found.ack.contentHash!==contentHash ||
              managedSessionBinding(sessionID)?.stableChatID!==found.ack.chatID) {receipts.delete(key);return}
          return {...found.ack}
        }) }
      }),
    )

    return Service.of({
      init: Effect.fn("History.Writer.init")(function* () {
        yield* InstanceState.get(state)
      }),
      sourceAck: (sessionID,actorID,partID,contentHash) => Effect.gen(function* () {
        const current=yield* InstanceState.get(state)
        return current.lookup ? yield* current.lookup(sessionID,actorID,partID,contentHash) : undefined
      }),
    })
  }),
)

function handle(job: Job, resolver: Resolver, enabled: ReadonlySet<Kind>, accept:(part:MessageV2.Part,timing:QueuedToolTiming | undefined,reply:any,generation:object | undefined,token:object | undefined)=>void) {
  if (ManagedProvider.enabled()) {
    if (job.type === "delete") {
      return Effect.tryPromise(() =>
        ManagedProvider.historyMutate(job.sessionID, "tombstone", [{
          messageID: job.messageID,
          partID: job.partID,
        }]),
      ).pipe(Effect.asVoid)
    }
    return Effect.gen(function* () {
      const role = yield* resolver.role(job.part.messageID)
      const timing = job.timing && job.timing.content.length <= 262144 ? job.timing : undefined
      const generation=ManagedProvider.historySourceGeneration()
      const reply=yield* Effect.tryPromise(() =>
        ManagedProvider.historyMutate(job.part.sessionID, "upsert", [{
          messageID: job.part.messageID,
          partID: job.part.id,
          role,
          ...(timing ? { actorID:timing.proof.actorID, toolTiming:timing.proof } : {}),
          partType: job.part.type,
          content: timing ? timing.content : job.part,
          timeCreated: Math.max(0, Math.floor(job.time)),
          timeUpdated: Math.max(0, Math.floor(job.time)),
          eventSequence: Math.max(0, Math.floor(job.time)),
        }]),
      )
      accept(job.part,timing,reply,generation,job.token)
    }).pipe(Effect.asVoid)
  }
  if (job.type === "delete") {
    return Effect.sync(() =>
      Database.use((db) => db.delete(HistoryFtsTable).where(eq(HistoryFtsTable.part_id, job.partID)).run()),
    )
  }
  return Effect.gen(function* () {
    const part = job.part
    const role = yield* resolver.role(part.messageID)
    const extracted = extract(part, role, enabled)
    if (!extracted) return
    const projectID = yield* resolver.projectID(part.sessionID)

    Database.use((db) =>
      db
        .insert(HistoryFtsTable)
        .values({
          part_id: part.id,
          session_id: part.sessionID,
          message_id: part.messageID,
          project_id: projectID,
          kind: extracted.kind,
          tool_name: extracted.tool_name,
          body: extracted.body,
          time_created: job.time,
        })
        .onConflictDoUpdate({
          target: HistoryFtsTable.part_id,
          set: {
            kind: extracted.kind,
            tool_name: extracted.tool_name,
            body: extracted.body,
            time_created: job.time,
          },
        })
        .run(),
    )
  })
}
