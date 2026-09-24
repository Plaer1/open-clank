import { describe, expect, test } from "bun:test"
import type { McpServer } from "@agentclientprotocol/sdk"
import {
  memorySessionScope,
  managedSessionBinding,
  markerMatches,
  registerMemorySessionScope,
  uniqueMemorySessionScope,
  unregisterMemorySessionScope,
} from "../../src/memory/session-scope"
import { getSharedMcpClient } from "../../src/memory/mcp-client"

const descriptor = (owner: string, enabled = true, cwd = "/workspace"): McpServer =>
  ({
    name: "lifetools_test",
    command: "python",
    args: [],
    env: [
      { name: "FM_OWNER", value: owner },
      { name: "FM_WORKSPACE_ID", value: "global" },
      { name: "SESSION_ID", value: "chat-stable" },
      { name: "OPEN_CLANK_AUTHORITY_WORKSPACE_ID", value: "authority" },
      { name: "COPAL_WORKSPACE", value: "copal" },
      { name: "WORKSPACE", value: cwd },
      { name: "OPEN_CLANK_ENGINE_SESSION_ALIASES", value: "[]" },
      { name: "OPEN_CLANK_SESSION_BINDING_REVISION", value: "0" },
      { name: "OPEN_CLANK_SESSION_MAP_REVISION", value: "0" },
      { name: "OPEN_CLANK_SESSION_MAPPING_REVISION", value: "0" },
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
    expect(managedSessionBinding(sessionID)?.memoryEnabled).toBe(false)

    unregisterMemorySessionScope(sessionID)
  })

  test("registration marker is process-local and cleared on failed reset", () => {
    const sessionID = "session-scope-marker"
    registerMemorySessionScope(sessionID, [descriptor("alice")], "/workspace")
    const binding = managedSessionBinding(sessionID)!
    expect(markerMatches(sessionID, binding)).toBe(true)
    const forged = {
      ...binding,
      registrationMarker: { ...binding.registrationMarker, workspaceRevision: binding.bindingRevision + 1 },
    }
    expect(markerMatches(sessionID, forged)).toBe(false)
    expect(() => registerMemorySessionScope(sessionID, [{ ...descriptor("bob"), env: [] }], "/workspace")).toThrow()
    expect(managedSessionBinding(sessionID)).toBeUndefined()
  })

  test("trusted descriptor identity rejects whitespace padding", () => {
    const sessionID = "session-scope-whitespace"
    const padded = descriptor("alice")
    if ("env" in padded) padded.env = padded.env.map((item) => item.name === "FM_OWNER" ? { ...item, value: " alice" } : item)
    expect(() => registerMemorySessionScope(sessionID, [padded], "/workspace")).toThrow()
    expect(managedSessionBinding(sessionID)).toBeUndefined()
  })

  test("descriptor cwd must already be canonical", () => {
    const sessionID = "session-scope-canonical-cwd"
    const noncanonical = descriptor("alice", true, "/workspace/../workspace")
    expect(() => registerMemorySessionScope(sessionID, [noncanonical], "/workspace")).toThrow()
    expect(managedSessionBinding(sessionID)).toBeUndefined()
  })

  test("re-registration rotates the private admission marker", () => {
    const sessionID = "session-scope-generation"
    registerMemorySessionScope(sessionID, [descriptor("alice")], "/workspace")
    const previous = managedSessionBinding(sessionID)!
    registerMemorySessionScope(sessionID, [descriptor("alice")], "/workspace")
    const current = managedSessionBinding(sessionID)!
    expect(current.registrationMarker.token).not.toBe(previous.registrationMarker.token)
    expect(markerMatches(sessionID, previous)).toBe(false)
    expect(markerMatches(sessionID, current)).toBe(true)
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
      "requires owner, stable chat, workspaces",
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

  test("a disabled-memory descriptor without authority is rejected and clears state", async () => {
    const sessionID = "session-scope-disabled-bare"
    const bare = {
      name: "lifetools_test",
      command: "python",
      args: [],
      env: [{ name: "FM_MEMORY_ENABLED", value: "0" }],
    } as McpServer

    expect(() => registerMemorySessionScope(sessionID, [bare], "/workspace")).toThrow("requires owner, stable chat, workspaces")

    expect(memorySessionScope(sessionID)).toBeUndefined()
    await expect(getSharedMcpClient(sessionID)).rejects.toThrow("not bound to session")
    unregisterMemorySessionScope(sessionID)
  })

  test("session-less work fails closed when a runtime mixes tenants", () => {
    const aliceOne = "session-scope-alice-1"
    const aliceTwo = "session-scope-alice-2"
    const bob = "session-scope-bob"
    registerMemorySessionScope(aliceOne, [descriptor("alice", true, "/workspace/one")], "/workspace/one")
    registerMemorySessionScope(aliceTwo, [descriptor("alice", true, "/workspace/two")], "/workspace/two")
    expect(uniqueMemorySessionScope()?.owner).toBe("alice")

    registerMemorySessionScope(bob, [descriptor("bob", true, "/workspace/three")], "/workspace/three")
    expect(() => uniqueMemorySessionScope()).toThrow("one owner and workspace")

    unregisterMemorySessionScope(aliceOne)
    unregisterMemorySessionScope(aliceTwo)
    unregisterMemorySessionScope(bob)
  })
})
