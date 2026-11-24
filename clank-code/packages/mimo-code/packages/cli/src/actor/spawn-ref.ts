// Late-bound reference to the Actor service implementation.
//
// SessionCheckpoint.tryStartCheckpointWriter needs to spawn the checkpoint-writer
// subagent. Wiring `Actor.Service` as a normal Layer dependency here would create
// a layer cycle (Actor → SessionPrompt → SessionCheckpoint → Actor). Instead,
// `Actor.layer` populates this module-local reference on initialisation, and
// SessionCheckpoint reads from it at call time. The cycle is broken at the type
// level because SessionCheckpoint no longer declares an `Actor.Service` requirement.
//
// Render-only paths (rebuild context, FileWatcher) never call
// tryStartCheckpointWriter, so a missing `current` is treated as a runtime guard
// rather than a hard invariant.
import type { Interface as ActorInterface } from "./spawn"

export interface SpawnBindingRegistry {
  readonly ref: { current: ActorInterface | undefined }
  readonly register: (implementation: ActorInterface) => () => void
}

export const makeBindingRegistry = (initial?: ActorInterface): SpawnBindingRegistry => {
  const ref = { current: initial }
  const bindings: Array<{ token: symbol; implementation: ActorInterface }> = []
  let baseBinding = initial
  let baseCaptured = false

  /** Register one live Actor layer and return its idempotent scope finalizer. */
  const register = (implementation: ActorInterface) => {
    if (bindings.length === 0) {
      baseBinding = ref.current
      baseCaptured = true
    }
    const binding = { token: Symbol("actor-spawn-binding"), implementation }
    bindings.push(binding)
    ref.current = implementation

    return () => {
      const index = bindings.findIndex((entry) => entry.token === binding.token)
      if (index < 0) return
      bindings.splice(index, 1)

      const current = ref.current
      if (current === implementation || bindings.some((entry) => entry.implementation === current)) {
        ref.current = bindings.at(-1)?.implementation ?? baseBinding
      }
      if (bindings.length === 0 && baseCaptured) {
        baseBinding = undefined
        baseCaptured = false
      }
    }
  }

  return { ref, register }
}

const production = makeBindingRegistry()
export const spawnRef = production.ref
export const registerBinding = production.register
