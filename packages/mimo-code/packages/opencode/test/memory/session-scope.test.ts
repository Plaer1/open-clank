import { describe, expect, test } from "bun:test"
import type { McpServer } from "@agentclientprotocol/sdk"
import {
  memorySessionScope,
  registerMemorySessionScope,
  uniqueMemorySessionScope,
  unregisterMemorySessionScope,
} from "../../src/memory/session-scope"
import { getSharedMcpClient } from "../../src/memory/mcp-client"

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

  test("incomplete re-registration clears the prior tenant binding", async () => {
    const sessionID = "session-scope-incomplete"
    registerMemorySessionScope(sessionID, [descriptor("alice")], "/workspace")

    const incomplete = descriptor("bob")
    if ("env" in incomplete) {
      incomplete.env = incomplete.env.filter((item) => item.name !== "FM_WORKSPACE_ID")
    }
    expect(() => registerMemorySessionScope(sessionID, [incomplete], "/other")).toThrow(
      "requires owner and workspace",
    )
    expect(memorySessionScope(sessionID)).toBeUndefined()
    await expect(getSharedMcpClient(sessionID)).rejects.toThrow("not bound to session")
  })

  test("a raw Frankenmemory descriptor cannot become the session carrier", async () => {
    const sessionID = "session-scope-raw-frankenmemory"
    const raw = {
      ...descriptor("alice"),
      name: "frankenmemory_test",
    } as McpServer

    registerMemorySessionScope(sessionID, [raw], "/workspace")

    expect(memorySessionScope(sessionID)).toBeUndefined()
    await expect(getSharedMcpClient(sessionID)).rejects.toThrow("not bound to session")
  })

  test("a disabled-memory descriptor without owner/workspace registers nothing and does not throw", async () => {
    const sessionID = "session-scope-disabled-bare"
    const bare = {
      name: "lifetools_test",
      command: "python",
      args: [],
      env: [{ name: "FM_MEMORY_ENABLED", value: "0" }],
    } as McpServer

    registerMemorySessionScope(sessionID, [bare], "/workspace")

    expect(memorySessionScope(sessionID)).toBeUndefined()
    await expect(getSharedMcpClient(sessionID)).rejects.toThrow("not bound to session")
    unregisterMemorySessionScope(sessionID)
  })

  test("session-less work fails closed when a runtime mixes tenants", () => {
    const aliceOne = "session-scope-alice-1"
    const aliceTwo = "session-scope-alice-2"
    const bob = "session-scope-bob"
    registerMemorySessionScope(aliceOne, [descriptor("alice")], "/workspace/one")
    registerMemorySessionScope(aliceTwo, [descriptor("alice")], "/workspace/two")
    expect(uniqueMemorySessionScope()?.owner).toBe("alice")

    registerMemorySessionScope(bob, [descriptor("bob")], "/workspace/three")
    expect(() => uniqueMemorySessionScope()).toThrow("one owner and workspace")

    unregisterMemorySessionScope(aliceOne)
    unregisterMemorySessionScope(aliceTwo)
    unregisterMemorySessionScope(bob)
  })
})
