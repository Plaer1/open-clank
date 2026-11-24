import { associateSourceAck, signedSourceStructuralDigest } from "./source-part-equivalence"
import { currentToolTiming, type QueuedToolTiming } from "../session/tool-timing"
import { Context, Effect, Layer, Queue } from "effect"
import { Database } from "../storage"
import { Bus } from "../bus"
import { MessageV2 } from "../session/message-v2"
import { InstanceState } from "../effect"
import { extract } from "./extract"
import { makeResolver, type Resolver } from "./resolve"
import { deleteHistoryRows, upsertHistoryBody } from "./chunk-write"
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
  readonly sourceAckForPart: (actorID:string,part:MessageV2.ToolPart)=>Effect.Effect<HistorySourceAck|undefined>
}

export class Service extends Context.Service<Service, Interface>()("@opencode/History.Writer") {}

export const layer: Layer.Layer<Service, never, Bus.Service> = Layer.effect(
  Service,
  Effect.gen(function* () {
    const bus = yield* Bus.Service

    const state = yield* InstanceState.make<{ started: boolean; lookup?: Interface['sourceAck']; lookupPart?: Interface['sourceAckForPart'] }>(
      Effect.fn("History.Writer.state")(function* (_ctx) {
        const queue = yield* Queue.unbounded<Job>()
        const resolver = makeResolver()
        // Open Clank: Host acknowledgements of managed history writes, per part and source generation.
        const receipts = new Map<string,{generation:object; ack:HistorySourceAck; structuralDigest?:string}>()
        const versions = new Map<string,object>()
        const keyFor = (sessionID:string,partID:string) => `${sessionID}\0${partID}`
        const invalidate = (sessionID:string,partID:string) => {
          const key=keyFor(sessionID,partID), token={}
          receipts.delete(key);versions.delete(key);versions.set(key,token)
          if (versions.size>512) { const oldest=versions.keys().next().value; if (oldest) {versions.delete(oldest);receipts.delete(oldest)} }
          return token
        }

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
            yield* handle(job, resolver, (part,timing,reply,generation,token) => {
              const ack=reply.sourceAck, key=keyFor(part.sessionID,part.id), binding=managedSessionBinding(part.sessionID)
              if (!ack || typeof ack!=='object' || Array.isArray(ack) || Object.keys(ack).sort().join(',')!==
                  'accountID,actorID,chatID,contentHash,messageID,outputBytes,outputSHA256,partID,revision,sessionID,version' ||
                  typeof ack.accountID!=='string' || !ack.accountID || ack.accountID.length>256 ||
                  !timing || timing.proof.version!==1 || !generation || ManagedProvider.historySourceGeneration()!==generation || versions.get(key)!==token ||
                  !binding || ack.version!==1 || ack.chatID!==binding.stableChatID || ack.sessionID!==part.sessionID ||
                  ack.actorID!==timing.proof.actorID || ack.messageID!==part.messageID || ack.partID!==part.id ||
                  ack.contentHash!==timing.proof.contentSHA256 || part.type!=='tool' || part.state.status!=='completed' ||
                  ack.outputSHA256!==createHash('sha256').update(part.state.output,'utf8').digest('hex') ||
                  ack.outputBytes!==Buffer.byteLength(part.state.output,'utf8') || !Number.isSafeInteger(ack.revision) || ack.revision<1) return
              receipts.set(key,{generation,ack:{...ack},structuralDigest:signedSourceStructuralDigest(timing.content)})
            }).pipe(
              Effect.catchCause((cause) => Effect.sync(() => log.warn("write failed", { cause: String(cause) }))),
            )
          }),
        ).pipe(Effect.forkScoped)

        return { started: true, lookup:(sessionID,actorID,partID,contentHash) => Effect.sync(() => {
          const key=keyFor(sessionID,partID), found=receipts.get(key)
          if (!found || ManagedProvider.historySourceGeneration()!==found.generation || found.ack.actorID!==actorID || found.ack.contentHash!==contentHash ||
              managedSessionBinding(sessionID)?.stableChatID!==found.ack.chatID) {receipts.delete(key);return}
          return {...found.ack}
        }), lookupPart:(actorID,part)=>Effect.sync(()=>{
          const key=keyFor(part.sessionID,part.id), found=receipts.get(key)
          const associated=associateSourceAck(part,actorID,found,ManagedProvider.historySourceGeneration(),managedSessionBinding(part.sessionID)?.stableChatID)
          if(!associated)receipts.delete(key)
          return associated
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
      sourceAckForPart: (actorID,part) => Effect.gen(function* () {
        const current=yield* InstanceState.get(state)
        return current.lookupPart ? yield* current.lookupPart(actorID,part) : undefined
      }),
    })
  }),
)

function handle(job: Job, resolver: Resolver, accept:(part:MessageV2.Part,timing:QueuedToolTiming | undefined,reply:any,generation:object | undefined,token:object | undefined)=>void) {
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
    return Effect.sync(() => Database.use((db) => deleteHistoryRows(db, job.partID)))
  }
  return Effect.gen(function* () {
    const part = job.part
    const role = yield* resolver.role(part.messageID)
    const summary = yield* resolver.summary(part.messageID)
    const extracted = extract(part, role, { summary })
    if (!extracted) {
      Database.use((db) => deleteHistoryRows(db, part.id))
      return
    }
    // Preview budget is applied in extract() on original text (D7).
    const projectID = yield* resolver.projectID(part.sessionID)
    Database.use((db) =>
      upsertHistoryBody(db, {
        part_id: part.id,
        session_id: part.sessionID,
        message_id: part.messageID,
        project_id: projectID,
        kind: extracted.kind,
        tool_name: extracted.tool_name,
        body: extracted.body,
        body_idx: extracted.body_idx,
        time_created: job.time,
      }),
    )
  })
}
