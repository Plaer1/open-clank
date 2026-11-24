import { expect, spyOn, test } from "bun:test"
import { ManagedProvider } from "../../src/acp/managed-provider"
import { markAdmittedToolTimingTurn, retireAdmittedToolTimingTurn } from "../../src/session/tool-timing"
import { createInputOrigin, currentInputOrigin, invalidateInputOrigin, publishInputOrigin, retireInputOrigin } from "../../src/session/input-origin"

test("input origin evidence follows one exact admitted turn and invalidates monotonically", () => {
  const actor = { sessionID: "session", actorID: "main", executionRevision: 1, rootOperationID: "root", innerTurnID: "turn" } as any
  const scope = {} as any
  const current = spyOn(ManagedProvider, "currentScope").mockReturnValue(scope)
  markAdmittedToolTimingTurn(actor, actor)
  try {
    const handle = createInputOrigin(actor)!
    expect(handle).toBeDefined()
    expect(currentInputOrigin(actor)).toBeUndefined()
    expect(invalidateInputOrigin(handle)).toBe(1)
    let evidenceCurrent = true
    expect(publishInputOrigin(handle, 1, "a".repeat(64), () => evidenceCurrent)).toBe(true)
    expect(currentInputOrigin(actor)).toEqual({ sourceSetGeneration: 1, sourceSetSHA256: "a".repeat(64) })
    expect(currentInputOrigin({ ...actor })).toBeUndefined()
    expect(invalidateInputOrigin(handle)).toBe(2)
    expect(currentInputOrigin(actor)).toBeUndefined()
    expect(publishInputOrigin(handle, 1, "a".repeat(64), () => evidenceCurrent)).toBe(false)
    expect(publishInputOrigin(handle, 2, "b".repeat(64), () => evidenceCurrent)).toBe(true)
    current.mockReturnValue({} as any)
    expect(currentInputOrigin(actor)).toBeUndefined()
    current.mockReturnValue(scope)
    actor.innerTurnID = "other"
    expect(currentInputOrigin(actor)).toBeUndefined()
    actor.innerTurnID = "turn"
    evidenceCurrent = false
    expect(currentInputOrigin(actor)).toBeUndefined()
    evidenceCurrent = true
    expect(currentInputOrigin(actor)).toBeUndefined()
    retireInputOrigin(handle)
    expect(currentInputOrigin(actor)).toBeUndefined()
    expect(publishInputOrigin(handle, 2, "b".repeat(64), () => evidenceCurrent)).toBe(false)
  } finally {
    retireAdmittedToolTimingTurn(actor)
    current.mockRestore()
  }
})
