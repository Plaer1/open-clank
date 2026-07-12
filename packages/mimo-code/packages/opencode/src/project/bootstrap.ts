import { Plugin } from "../plugin"
import { Format } from "../format"
import { LSP } from "../lsp"
import { File } from "../file"
import { Snapshot } from "../snapshot"
import * as Project from "./project"
import * as Vcs from "./vcs"
import { Bus } from "../bus"
import { Command } from "../command"
import { Instance } from "./instance"
import { Log } from "@/util"
import { FileWatcher } from "@/file/watcher"
import { ShareNext } from "@/share"
import * as Effect from "effect/Effect"
import { Config } from "@/config"
import { Metrics } from "@/metrics"
import { Memory } from "@/memory"
import * as MemoryCapture from "@/memory/capture"
import * as CompactionCapture from "@/memory/compaction-capture"
import { WriterService, BackfillService } from "@/history"

export const InstanceBootstrap = Effect.gen(function* () {
  Log.Default.info("bootstrapping", { directory: Instance.directory })
  // everything depends on config so eager load it for nice traces
  yield* Config.Service.use((svc) => svc.get())
  // Plugin can mutate config so it has to be initialized before anything else.
  yield* Plugin.Service.use((svc) => svc.init())
  yield* Effect.all(
    [
      LSP.Service,
      ShareNext.Service,
      Format.Service,
      File.Service,
      FileWatcher.Service,
      Vcs.Service,
      Snapshot.Service,
      WriterService,
      BackfillService,
    ].map((s) => Effect.forkDetach(s.use((i) => i.init()))),
  ).pipe(Effect.withSpan("InstanceBootstrap.init"))

  // Warm the FTS index off the boot path. Off-tool writes between
  // process invocations are picked up here without blocking startup;
  // a missing memory dir or partial sync must not fail boot.
  yield* Memory.Service.use((svc) => svc.reconcile()).pipe(
    Effect.catch((err: unknown) =>
      Effect.sync(() => Log.Default.warn("memory reconcile failed", { error: String(err) })),
    ),
    Effect.forkDetach,
  )

  // The capture services are subscription-only side effects: nothing else
  // demands them, and Effect layers are lazy — without this init() their
  // PartUpdated/Compacted subscribers never register and memory capture
  // silently does nothing.
  yield* Effect.all([
    MemoryCapture.CaptureService.use((s) => s.init()),
    CompactionCapture.CompactionCaptureService.use((s) => s.init()),
  ]).pipe(
    Effect.catch((err: unknown) =>
      Effect.sync(() => Log.Default.warn("memory capture init failed", { error: String(err) })),
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
