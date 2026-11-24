import type { ActorExecutionIdentity } from "@/actor/turn"
import { ManagedProvider } from "@/acp/managed-provider"
import { isAdmittedToolTimingTurn } from "./tool-timing"

type Evidence = Readonly<{ sourceSetGeneration: number; sourceSetSHA256: string }>
type Handle = object
type Turn = Readonly<{ sessionID: string; actorID: string; executionRevision: number; rootOperationID: string; innerTurnID: string }>
type State = { identity: ActorExecutionIdentity; turn: Turn; scope: object; generation: number; evidence?: Evidence; evidenceCurrent?: () => boolean; retired: boolean }
const handles = new WeakMap<Handle, State>()
const turns = new WeakMap<ActorExecutionIdentity, State>()

function current(state: State): boolean {
  const id = state.identity
  if (state.retired || !id.rootOperationID || !id.innerTurnID) return false
  try {
    return turns.get(id) === state && isAdmittedToolTimingTurn(id, state.turn) &&
      ManagedProvider.currentScope(id.sessionID, id.actorID, id.executionRevision) === state.scope
  } catch { return false }
}

/** Producer capability is held only by the admitted prompt turn. */
export function createInputOrigin(identity: ActorExecutionIdentity): Handle | undefined {
  if (!identity.rootOperationID || !identity.innerTurnID || turns.has(identity)) return
  const scope = ManagedProvider.currentScope(identity.sessionID, identity.actorID, identity.executionRevision)
  if (!scope || !isAdmittedToolTimingTurn(identity, { sessionID: identity.sessionID, actorID: identity.actorID,
    executionRevision: identity.executionRevision, rootOperationID: identity.rootOperationID, innerTurnID: identity.innerTurnID })) return
  const handle = Object.freeze({})
  const turn = Object.freeze({ sessionID: identity.sessionID, actorID: identity.actorID, executionRevision: identity.executionRevision,
    rootOperationID: identity.rootOperationID, innerTurnID: identity.innerTurnID })
  const state: State = { identity, turn, scope, generation: 0, retired: false }
  handles.set(handle, state)
  turns.set(identity, state)
  return handle
}

/** Clear evidence before a new consumption is attempted. */
export function invalidateInputOrigin(handle: Handle): number | undefined {
  const state = handles.get(handle)
  if (!state || !current(state) || !Number.isSafeInteger(state.generation + 1)) return
  state.evidence = undefined
  state.evidenceCurrent = undefined
  return ++state.generation
}

/** Called only after Host accepted this exact complete generation and digest. */
export function publishInputOrigin(handle: Handle, generation: number, digest: string, evidenceCurrent: () => boolean): boolean {
  const state = handles.get(handle)
  if (!state || !current(state) || state.generation !== generation || state.evidence || !/^[0-9a-f]{64}$/.test(digest)) return false
  try { if (!evidenceCurrent()) return false } catch { return false }
  state.evidenceCurrent = evidenceCurrent
  state.evidence = Object.freeze({ sourceSetGeneration: generation, sourceSetSHA256: digest })
  return true
}

export function currentInputOrigin(identity: ActorExecutionIdentity | undefined): Evidence | undefined {
  const state = identity && turns.get(identity)
  if (!state || !current(state) || !state.evidence || !state.evidenceCurrent) return undefined
  try { if (state.evidenceCurrent()) return state.evidence } catch { /* fail closed */ }
  state.evidence = undefined
  state.evidenceCurrent = undefined
  return undefined
}

export function retireInputOrigin(handle: Handle | undefined): void {
  const state = handle && handles.get(handle)
  if (!state) return
  state.evidence = undefined
  state.evidenceCurrent = undefined
  state.retired = true
  if (turns.get(state.identity) === state) turns.delete(state.identity)
  handles.delete(handle!)
}
