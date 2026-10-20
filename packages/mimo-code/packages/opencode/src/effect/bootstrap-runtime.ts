import { Layer, ManagedRuntime } from "effect"

import { Plugin } from "@/plugin"
import { LSP } from "@/lsp"
import { FileWatcher } from "@/file/watcher"
import { Format } from "@/format"
import { ShareNext } from "@/share"
import { File } from "@/file"
import { Vcs } from "@/project"
import { Snapshot } from "@/snapshot"
import { Bus } from "@/bus"
import { Config } from "@/config"
import { Memory } from "@/memory"
import * as MemoryCapture from "@/memory/capture"
import * as CompactionCapture from "@/memory/compaction-capture"
import { History } from "@/history"
import * as Observability from "./observability"
import { memoMap } from "./memo-map"

// Layer.suspend: the cross-module `.defaultLayer` reads (MemoryCapture /
// CompactionCapture especially) must defer to first use. Without it a combined
// run that loads this module while capture.ts / compaction-capture.ts are
// mid-init throws "Cannot access 'defaultLayer' before initialization".
// Same init-order contract as AppLayer in app-runtime.ts.
export const BootstrapLayer = Layer.suspend(() =>
  Layer.mergeAll(
    Config.defaultLayer,
    Plugin.defaultLayer,
    ShareNext.defaultLayer,
    Format.defaultLayer,
    LSP.defaultLayer,
    File.defaultLayer,
    FileWatcher.defaultLayer,
    Vcs.defaultLayer,
    Snapshot.defaultLayer,
    Bus.defaultLayer,
    Memory.defaultLayer,
    MemoryCapture.defaultLayer,
    CompactionCapture.defaultLayer,
    History.defaultLayer,
  ).pipe(Layer.provide(Observability.layer)),
)

// Lazy for the same reason AppRuntime is: ManagedRuntime.make at module load
// would evaluate BootstrapLayer during import-graph init (the TDZ window).
const makeRuntime = () => ManagedRuntime.make(BootstrapLayer, { memoMap })
type RuntimeInstance = ReturnType<typeof makeRuntime>
type Runtime = Pick<RuntimeInstance, "runSync" | "runPromise" | "runPromiseExit" | "runFork" | "runCallback" | "dispose">
let rt: RuntimeInstance | undefined
const activeRuntime = () => (rt ??= makeRuntime())

export const BootstrapRuntime: Runtime = {
  runSync: (...args) => activeRuntime().runSync(...args),
  runPromise: (...args) => activeRuntime().runPromise(...args),
  runPromiseExit: (...args) => activeRuntime().runPromiseExit(...args),
  runFork: (...args) => activeRuntime().runFork(...args),
  runCallback: (...args) => activeRuntime().runCallback(...args),
  dispose: () => {
    const active = rt
    rt = undefined
    return active ? active.dispose() : Promise.resolve()
  },
}
