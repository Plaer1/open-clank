import { describe, expect, test } from "bun:test"
import type { McpServer } from "@agentclientprotocol/sdk"
import {
  memorySessionScope,
  registerMemorySessionScope,
  unregisterMemorySessionScope,
} from "../../src/memory/session-scope"

const descriptor = (owner: string, enabled = true): McpServer =>
  ({
    name: "lifetools_test",
    command: "python",
    args: [],
    env: [
      { name: "FM_OWNER", value: owner },
      { name: "FM_WORKSPACE_ID", value: "global" },
      { name: "FM_MEMORY_ENABLED", value: enabled ? "1" : "0" },
    ],
  }) as McpServer

describe("memory session scope", () => {
  test("the life-tools carrier enables and revokes memory per turn", () => {
    const sessionID = "session-scope-revoke"
    registerMemorySessionScope(sessionID, [descriptor("alice")], "/workspace")
    expect(memorySessionScope(sessionID)?.owner).toBe("alice")

    registerMemorySessionScope(sessionID, [descriptor("alice", false)], "/workspace")
    expect(memorySessionScope(sessionID)).toBeUndefined()

    unregisterMemorySessionScope(sessionID)
  })
})
