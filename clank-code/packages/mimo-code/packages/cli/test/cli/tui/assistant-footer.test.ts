import { describe, expect, test } from "bun:test"
import { shouldShowAssistantFooter } from "../../../src/cli/cmd/tui/util/assistant-footer"

const base = {
  recovering: false,
  aborted: false,
  finish: undefined as string | undefined,
  isLast: true,
}

describe("shouldShowAssistantFooter", () => {
  test("shows for a recovering row even when it is not last", () => {
    expect(shouldShowAssistantFooter({ ...base, recovering: true, isLast: false, finish: "tool-calls" })).toBe(true)
  })

  test("shows for an aborted row", () => {
    expect(shouldShowAssistantFooter({ ...base, aborted: true, isLast: false, finish: "tool-calls" })).toBe(true)
  })

  test("shows for a final (stop) row even when a later row is last", () => {
    expect(shouldShowAssistantFooter({ ...base, finish: "stop", isLast: false })).toBe(true)
  })

  test("hides a non-last mid-loop row that is not final", () => {
    expect(shouldShowAssistantFooter({ ...base, finish: "tool-calls", isLast: false })).toBe(false)
  })

  test("shows the live last row while streaming (finish undefined)", () => {
    expect(shouldShowAssistantFooter({ ...base, finish: undefined, isLast: true })).toBe(true)
  })

  test("keeps the last row mounted after finish=tool-calls", () => {
    expect(shouldShowAssistantFooter({ ...base, finish: "tool-calls", isLast: true })).toBe(true)
  })

  test("shows for a settled non-tool-calls finish (length, cancelled, …) even when not last", () => {
    expect(shouldShowAssistantFooter({ ...base, finish: "length", isLast: false })).toBe(true)
  })
})
