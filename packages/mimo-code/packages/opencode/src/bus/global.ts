import { EventEmitter } from "events"

export type GlobalEvent = {
  directory?: string
  project?: string
  workspace?: string
  payload: any
}

export const GlobalBus = new EventEmitter<{
  event: [GlobalEvent]
  newListener: [eventName: string | symbol, listener: (...args: unknown[]) => void]
  removeListener: [eventName: string | symbol, listener: (...args: unknown[]) => void]
}>()

// EventEmitter emits these before acquisition and after actual removal. The
// sentinels are checked too, so removing instrumentation cannot grant proof.
let eventGeneration = 0n
const added = (name: string | symbol) => { if (name === "event") eventGeneration++ }
const removed = (name: string | symbol) => { if (name === "event") eventGeneration++ }
GlobalBus.on("newListener", added)
GlobalBus.on("removeListener", removed)

export function globalEventRecoveryWitness(): import("../session/recovery-profile").GlobalEventWitness {
  const valid = () => GlobalBus.listeners("newListener").includes(added)
    && GlobalBus.listeners("removeListener").includes(removed)
    && GlobalBus.listenerCount("event") === 0
  const generation = eventGeneration
  // Listener removal cannot prove previously started async work settled.
  const eligible = generation === 0n && valid()
  return Object.freeze({ eligible, current: () => eligible && valid() && eventGeneration === generation })
}
