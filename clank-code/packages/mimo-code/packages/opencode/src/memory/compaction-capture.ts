import { Context, Effect, Layer } from "effect"
import { RecoverableError } from "@/tool/recoverable"

export interface Interface {
  readonly init: () => Effect.Effect<void>
}

export class CompactionCaptureService extends Context.Service<CompactionCaptureService, Interface>()(
  "@opencode/Memory.CompactionCapture",
) {}

/** Raw summary capture has no admitted source/privacy owner. */
export const layer = Layer.succeed(CompactionCaptureService, CompactionCaptureService.of({
  init: () => Effect.die(new RecoverableError("Automatic compaction memory capture requires an admitted Host source owner")),
}))
export const defaultLayer = Layer.suspend(() => layer)
