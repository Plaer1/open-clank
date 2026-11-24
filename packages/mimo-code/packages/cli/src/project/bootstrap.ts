import { Plugin } from "../plugin"
import { Format } from "../format"
import { LSP } from "../lsp"
import * as Project from "./project"
import * as Vcs from "./vcs"
import { Bus } from "../bus"
import { Command } from "../command"
import { Instance } from "./instance"
import { Log } from "@/util"
import { FileWatcher } from "@/file/watcher"
import * as Effect from "effect/Effect"
import { Config } from "@/config"
import { Metrics } from "@/metrics"
import { WriterService, BackfillService } from "@/history"
import { sweepAbandonedZombies } from "@/actor/registry"
import * as MemoryCapture from "@/memory/capture"

export const InstanceBootstrap = Effect.gen(function* () {
  Log.Default.info("bootstrapping", { directory: Instance.directory })
  // everything depends on config so eager load it for nice traces
  yield* Config.Service.use((svc) => svc.get())
  // Plugin can mutate config so it has to be initialized before anything else.
  yield* Plugin.Service.use((svc) => svc.init())
  // Keep startup independent of transcript size. Only actor metadata is
  // reclaimed here; orphan tools are handled when their session runs.
  yield* Effect.sync(() => {
    sweepAbandonedZombies()
  }).pipe(
    Effect.catch((err: unknown) =>
      Effect.sync(() => Log.Default.warn("abandoned actor sweep failed", { error: String(err) })),
    ),
  )
  yield* Effect.all(
    [
      LSP.Service,
      Format.Service,
      FileWatcher.Service,
      Vcs.Service,
      WriterService,
      BackfillService,
    ].map((s) => Effect.forkDetach(s.use((i) => i.init()))),
  ).pipe(Effect.withSpan("InstanceBootstrap.init"))

  // Keep the existing scoped session-binding lifecycle; semantic capture requires
  // a separately admitted Host source and is not started from bootstrap.
  yield* MemoryCapture.CaptureService.use((service) => service.init()).pipe(
    Effect.catch((err: unknown) =>
      Effect.sync(() => Log.Default.warn("memory scope lifecycle init failed", { error: String(err) })),
    ),
  )

  yield* Bus.Service.use((svc) =>
    svc.subscribeCallback(Command.Event.Executed, async (payload) => {
      if (payload.properties.name === Command.Default.INIT) {
        Project.setInitialized(Instance.project.id)
      }
    }),
  )

  yield* Metrics.subscribe()
}).pipe(Effect.withSpan("InstanceBootstrap"))
