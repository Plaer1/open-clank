import { afterEach, describe, expect, test } from "bun:test"
import type { Client } from "@modelcontextprotocol/sdk/client/index.js"
import {
  bindMemorySessionClient,
  callBoundMemoryTool,
  callMemoryTool,
  closeSharedMcpClient,
  getSharedMcpClient,
  registerManagedMcpClient,
  unregisterManagedMcpClient,
} from "../../src/memory/mcp-client"
import { lifecycle } from "../../src/memory/frankenmemory"
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

describe("managed Frankenmemory transport", () => {
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

  test("turns MCP error results into a failed memory operation", async () => {
    const broken = {
      callTool: async () => ({
        content: [{ type: "text", text: "private transport detail" }],
        isError: true,
      }),
    } as unknown as Client

    await expect(callMemoryTool(broken, "search", { query: "x" })).rejects.toThrow(
      "memory tool search failed",
    )
  })

  test("session-bound calls leave owner and workspace injection to lifetools", async () => {
    const calls: Array<{ name: string; arguments: Record<string, unknown> }> = []
    const alice = {
      callTool: async (input: { name: string; arguments: Record<string, unknown> }) => {
        calls.push(input)
        return { content: [{ type: "text", text: "{}" }] }
      },
    } as unknown as Client
    registerManagedMcpClient(names[0], alice)
    bindMemorySessionClient("session-alice", names[0], "alice", "global")

    await callBoundMemoryTool("session-alice", "memory_explain", { id: "block-1" })
    expect(calls).toEqual([
      { name: "memory_explain", arguments: { id: "block-1" } },
    ])
    await expect(
      callBoundMemoryTool("session-alice", "memory_explain", {
        id: "block-1",
        owner: "bob",
      }),
    ).rejects.toThrow("cannot accept model-authored scope")
  })

  test("typed lifecycle calls share the bound session contract without model scope", async () => {
    const calls: Array<{ name: string; arguments: Record<string, unknown> }> = []
    const alice = {
      callTool: async (input: { name: string; arguments: Record<string, unknown> }) => {
        calls.push(input)
        return { content: [{ type: "text", text: JSON.stringify({ ok: true }) }] }
      },
    } as unknown as Client
    registerManagedMcpClient(names[0], alice)
    registerMemorySessionScope(
      "session-alice",
      [{
        name: names[0],
        env: [
          { name: "FM_OWNER", value: "alice" },
          { name: "FM_WORKSPACE_ID", value: "global" },
        ],
      }] as never,
      "/workspace",
    )

    await lifecycle.updateCandidate("session-alice", "candidate-1", "edited", "fact")
    await lifecycle.reviewCandidate("session-alice", "candidate-1", true)
    await lifecycle.resolveQuestion("session-alice", "question-1", "E", 2)
    await lifecycle.reopenQuestion("session-alice", "question-1", 3)

    expect(calls).toEqual([
      {
        name: "update_candidate",
        arguments: {
          id: "candidate-1",
          content: "edited",
          category: "fact",
          reason: "edited_by_mimo",
        },
      },
      {
        name: "review_candidate",
        arguments: {
          id: "candidate-1",
          accept: true,
          reason: "approved_by_mimo_user",
        },
      },
      {
        name: "resolve_memory",
        arguments: { id: "question-1", answer: "E", expected_revision: 2 },
      },
      {
        name: "reopen_memory",
        arguments: { id: "question-1", expected_revision: 3 },
      },
    ])
  })

  test("a malformed lifecycle payload resolves to an empty result instead of throwing", async () => {
    const broken = {
      callTool: async () => ({ content: [{ type: "text", text: "not-json{" }] }),
    } as unknown as Client
    registerManagedMcpClient(names[0], broken)
    registerMemorySessionScope(
      "session-alice",
      [{
        name: names[0],
        env: [
          { name: "FM_OWNER", value: "alice" },
          { name: "FM_WORKSPACE_ID", value: "global" },
        ],
      }] as never,
      "/workspace",
    )

    expect(await lifecycle.explain("session-alice", "block-1")).toEqual({})
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
      registerMemorySessionScope(
        "session-alice",
        [{
          name: names[0],
          env: [
            { name: "FM_OWNER", value: "alice" },
            { name: "FM_WORKSPACE_ID", value: "global" },
            { name: "FM_MEMORY_ENABLED", value: "0" },
          ],
        }] as never,
        "/workspace",
      )

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
