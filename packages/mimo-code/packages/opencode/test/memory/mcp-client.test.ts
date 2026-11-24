import { afterEach, describe, expect, test } from "bun:test"
import type { Client } from "@modelcontextprotocol/sdk/client/index.js"
import {
  bindMemorySessionClient,
  callBoundMemoryTool,
  callBoundOpenClankTool,
  callMemoryTool,
  closeSharedMcpClient,
  getSharedMcpClient,
  registerManagedMcpClient,
  unregisterManagedMcpClient,
} from "../../src/memory/mcp-client"
import {
  registerMemorySessionScope,
  unregisterMemorySessionScope,
} from "../../src/memory/session-scope"
import {
  assertProjectFilePolicy,
  assertProjectShellPolicy,
} from "../../src/tool/project-policy"

const names = ["lifetools_alice", "lifetools_bob"]
const client = (id: string) => ({ id }) as unknown as Client

afterEach(async () => {
  await closeSharedMcpClient()
  unregisterMemorySessionScope("session-alice")
  for (const name of names) unregisterManagedMcpClient(name)
})

describe("managed Memery transport", () => {
  test("borrows the exact lifetools client bound to a session", async () => {
    const alice = client("alice")
    registerManagedMcpClient(names[0], alice)
    bindMemorySessionClient("session-alice", names[0], "alice", "global")

    expect(await getSharedMcpClient("session-alice")).toBe(alice)
  })

  test("selects session-less work by exact owner and workspace", async () => {
    const alice = client("alice")
    const bob = client("bob")
    registerManagedMcpClient(names[0], alice)
    registerManagedMcpClient(names[1], bob)
    bindMemorySessionClient("session-alice", names[0], "alice", "global")
    bindMemorySessionClient("session-bob", names[1], "bob", "global")

    expect(
      await getSharedMcpClient(undefined, { owner: "bob", workspaceId: "global" }),
    ).toBe(bob)
  })

  test("never falls back to another tenant's only connected client", async () => {
    registerManagedMcpClient(names[0], client("alice"))
    bindMemorySessionClient("session-alice", names[0], "alice", "global")

    await expect(
      getSharedMcpClient(undefined, { owner: "bob", workspaceId: "global" }),
    ).rejects.toThrow("owner/workspace-bound")
  })

  test("drops a disconnected managed client", async () => {
    const alice = client("alice")
    registerManagedMcpClient(names[0], alice)
    bindMemorySessionClient("session-alice", names[0], "alice", "global")
    unregisterManagedMcpClient(names[0], alice)

    await expect(
      getSharedMcpClient(undefined, { owner: "alice", workspaceId: "global" }),
    ).rejects.toThrow("owner/workspace-bound")
  })

  test("propagates a LifeTools policy operation failure", async () => {
    const broken = {
      callTool: async () => ({
        content: [{ type: "text", text: "private transport detail" }],
        isError: true,
      }),
    } as unknown as Client

    await expect(callMemoryTool(broken, "project_mutation_policy", { mode: "shell" })).rejects.toThrow(
      "tool project_mutation_policy failed",
    )
  })

  test("LifeTools cannot supply memory operations or accept model-selected scope", async () => {
    const calls: string[] = []
    registerManagedMcpClient(names[0], {
      callTool: async ({ name }: { name: string }) => {
        calls.push(name)
        return { content: [] }
      },
    } as unknown as Client)
    bindMemorySessionClient("session-alice", names[0], "alice", "global")

    await expect(callBoundMemoryTool("session-alice", "memory_explain", { id: "block-1" }))
      .rejects.toThrow("admitted Host binding")
    await expect(callBoundMemoryTool("session-alice", "search", { query: "x", owner: "bob" }))
      .rejects.toThrow("cannot accept model-authored scope")
    expect(calls).toEqual([])
  })

  test("project file policy sends exact bytes through lifetools even when memory is off", async () => {
    const previousOwner = process.env.OPEN_CLANK_OWNER
    const previousPolicy = process.env.OPEN_CLANK_PROJECT_POLICY_BRIDGE
    process.env.OPEN_CLANK_OWNER = "alice"
    process.env.OPEN_CLANK_PROJECT_POLICY_BRIDGE = "required"
    const calls: Array<{ name: string; arguments: Record<string, unknown> }> = []
    const alice = {
      callTool: async (input: { name: string; arguments: Record<string, unknown> }) => {
        calls.push(input)
        return {
          content: [{ type: "text", text: JSON.stringify({ enforced: true, allowed: true }) }],
        }
      },
    } as unknown as Client
    try {
      registerManagedMcpClient(names[0], alice)
      bindMemorySessionClient("session-alice", names[0], "alice", "global")

      await assertProjectFilePolicy(
        { sessionID: "session-alice" } as never,
        [
          { path: "/workspace/a.txt", content: new Uint8Array([0, 1, 255]) },
          { path: "/workspace/old.txt", content: null },
        ],
      )
      expect(calls).toEqual([{
        name: "project_mutation_policy",
        arguments: {
          mode: "files",
          candidates: [
            { path: "/workspace/a.txt", content_base64: "AAH/" },
            { path: "/workspace/old.txt", deleted: true },
          ],
        },
      }])
    } finally {
      if (previousOwner === undefined) delete process.env.OPEN_CLANK_OWNER
      else process.env.OPEN_CLANK_OWNER = previousOwner
      if (previousPolicy === undefined) delete process.env.OPEN_CLANK_PROJECT_POLICY_BRIDGE
      else process.env.OPEN_CLANK_PROJECT_POLICY_BRIDGE = previousPolicy
    }
  })

  test("project shell policy fails closed when Open Clank reports active policy", async () => {
    const previousOwner = process.env.OPEN_CLANK_OWNER
    const previousPolicy = process.env.OPEN_CLANK_PROJECT_POLICY_BRIDGE
    process.env.OPEN_CLANK_OWNER = "alice"
    process.env.OPEN_CLANK_PROJECT_POLICY_BRIDGE = "required"
    const alice = {
      callTool: async () => ({
        content: [{
          type: "text",
          text: JSON.stringify({
            enforced: true,
            allowed: false,
            reason: "active project policy blocks MiMo shell execution",
          }),
        }],
      }),
    } as unknown as Client
    try {
      registerManagedMcpClient(names[0], alice)
      bindMemorySessionClient("session-alice", names[0], "alice", "global")
      await expect(
        assertProjectShellPolicy({ sessionID: "session-alice" } as never),
      ).rejects.toThrow("active project policy blocks MiMo shell execution")
    } finally {
      if (previousOwner === undefined) delete process.env.OPEN_CLANK_OWNER
      else process.env.OPEN_CLANK_OWNER = previousOwner
      if (previousPolicy === undefined) delete process.env.OPEN_CLANK_PROJECT_POLICY_BRIDGE
      else process.env.OPEN_CLANK_PROJECT_POLICY_BRIDGE = previousPolicy
    }
  })
})


test("managed hidden tools require exact actor admission immediately before private calls", async () => {
  const { ManagedProvider } = await import("../../src/acp/managed-provider")
  const previous = process.env.OPEN_CLANK_MANAGED
  process.env.OPEN_CLANK_MANAGED = "1"
  ManagedProvider.resetForTest()
  const events: string[] = []
  let allow = true
  const alice = {
    callTool: async (input: { name: string }) => {
      events.push(`call:${input.name}`)
      return { content: [{ type: "text", text: "{}" }] }
    },
  } as unknown as Client
  try {
    registerManagedMcpClient(names[0], alice)
    const register = (mapRevision: number, mappingRevision: number) => registerMemorySessionScope("session-alice", [{ name: names[0], command: "fake", args: [], env: [
      { name: "FM_OWNER", value: "alice" }, { name: "FM_WORKSPACE_ID", value: "global" },
      { name: "SESSION_ID", value: "chat-1" }, { name: "OPEN_CLANK_AUTHORITY_WORKSPACE_ID", value: "authority" },
      { name: "COPAL_WORKSPACE", value: "copal" }, { name: "WORKSPACE", value: "/workspace" },
      { name: "OPEN_CLANK_ENGINE_SESSION_ALIASES", value: "[]" }, { name: "OPEN_CLANK_SESSION_BINDING_REVISION", value: "1" },
      { name: "OPEN_CLANK_SESSION_MAP_REVISION", value: String(mapRevision) }, { name: "OPEN_CLANK_SESSION_MAPPING_REVISION", value: String(mappingRevision) },
      { name: "FM_MEMORY_ENABLED", value: "1" },
    ] }] as never, "/workspace")
    register(4, 2)
    ManagedProvider.installHostConnection({ extMethod: async (method: string, params: Record<string, unknown>) => {
      if (method === "_openclank/session/v1/memory/search") {
        expect(params.search).toEqual({ query: "x" })
        expect(params.identity).toMatchObject({ sessionID: "session-alice", actorID: "main" })
        events.push("host:search")
        return { qualified_text: "{}", admission_attestation: null }
      }
      if (method !== "_openclank/session/v1/tool/admit") throw new Error("unexpected callback")
      events.push(`admit:${params.kind}:${params.toolName}:${params.executionRevision}:${params.innerTurnID}`)
      return { allowed: allow }
    } } as never)
    const actor = { sessionID: "session-alice", actorID: "main", executionRevision: 3,
      rootOperationID: "root-1", innerTurnID: "turn-1", preflightRoute: { bootstrap: {
        owner: "alice", stableChatID: "chat-1", rootSessionID: "session-alice", sessionID: "session-alice",
        actorID: "main", rootOperationID: "root-1", workspaceRevision: 1, mapRevision: 4, mappingRevision: 2,
      } } } as never
    await callBoundMemoryTool("session-alice", "search", { query: "x" }, actor)
    await callBoundOpenClankTool("session-alice", "project_mutation_policy", { mode: "shell" }, actor, "bash")
    await callBoundOpenClankTool("session-alice", "record_skill_usage", { name: "s" }, actor, "skill")
    register(5, 2) // Another chat changed the owner-wide map, not this mapping.
    await callBoundMemoryTool("session-alice", "search", { query: "x" }, actor)
    register(6, 3) // This chat was remapped; the frozen tuple is stale.
    await expect(callBoundMemoryTool("session-alice", "search", { query: "x" }, actor)).rejects.toThrow("binding changed")
    register(5, 2)
    allow = false
    await expect(callBoundMemoryTool("session-alice", "search", { query: "x" }, actor)).rejects.toThrow("unavailable")
    expect(events).toEqual([
      "admit:memory_read:recall_memory:3:turn-1", "host:search",
      "admit:project_policy_read:bash:3:turn-1", "call:project_mutation_policy",
      "admit:skill_use:skill:3:turn-1", "call:record_skill_usage",
      "admit:memory_read:recall_memory:3:turn-1", "host:search",
      "admit:memory_read:recall_memory:3:turn-1",
    ])
  } finally {
    ManagedProvider.resetForTest()
    if (previous === undefined) delete process.env.OPEN_CLANK_MANAGED
    else process.env.OPEN_CLANK_MANAGED = previous
  }
})
