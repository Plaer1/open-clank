import { Context, Effect, Layer } from "effect"
import { Bus } from "../bus"
import { Config } from "../config"
import { Session } from "../session"
import { Log } from "../util"
import { unregisterMemorySessionScope } from "./session-scope"

const log = Log.create({ service: "memory.capture" })

// Turn capture is owned by the EMBEDDER, not this runtime. Every dispatched
// turn — any transport, any provider — passes through Odysseus's
// post-response seam, which holds the policy context this side never sees
// (incognito, compare mode, the user's auto-memory preference). A child-side
// capture here double-stored every turn and couldn't honor any of that.
// What legitimately remains child-side: compaction summaries
// (compaction-capture.ts — they only exist here) and authored-file ingest
// (reconcile). This service now owns only the session-scope lifecycle.

export interface Interface {
  readonly init: () => Effect.Effect<void>
}

export class CaptureService extends Context.Service<CaptureService, Interface>()("@opencode/Memory.Capture") {}

export const layer: Layer.Layer<CaptureService, never, Bus.Service | Config.Service> = Layer.effect(
  CaptureService,
  Effect.gen(function* () {
    const config = yield* Config.Service
    const bus = yield* Bus.Service

    // Everything instance-dependent happens inside init(), NOT at layer
    // construction: the layer is memoized once per process, but Config and
    // Bus subscriptions are InstanceState-scoped — subscribing here would
    // register on whichever instance happened to be current at first demand
    // and every other instance's sessions would capture nothing. init() runs
    // once per instance from InstanceBootstrap (same idiom as
    // Metrics.subscribe), and its subscriptions die with the instance state.
    const init = () =>
      Effect.gen(function* () {
        const cfg = yield* config.get()
        if (cfg.memory?.provider !== "frankenmemory") {
          log.info("memory scope lifecycle disabled", { provider: cfg.memory?.provider ?? "unset" })
          return
        }
        log.info("memory scope lifecycle registering")

        yield* bus.subscribeCallback(Session.Event.Deleted, (evt) => {
          unregisterMemorySessionScope(evt.properties.sessionID)
        })
      })

    return CaptureService.of({
      init: () => init(),
    })
  }),
)

export const defaultLayer = layer.pipe(
  Layer.provide(Bus.defaultLayer),
  Layer.provide(Config.defaultLayer),
)
