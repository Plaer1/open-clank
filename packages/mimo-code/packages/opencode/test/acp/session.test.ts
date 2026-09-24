import { afterEach, describe, expect, test } from "bun:test"
import type { McpServer } from "@agentclientprotocol/sdk"
import type { OpencodeClient } from "@mimo-ai/sdk/v2"
import { ACPSessionManager, reserveProvisionalSessionID } from "../../src/acp/session"
import { memorySessionScope, registerMemorySessionScope, unregisterMemorySessionScope } from "../../src/memory/session-scope"

const created: string[] = []
const deleted: string[] = []
const sdk = {
  session: {
    create: async (input?: { id?: string }) => {
      const id = input?.id ?? `ses_acp_${created.length + 1}`
      created.push(id)
      return { data: { id, directory: "/workspace", time: { created: Date.now() } } }
    },
    delete: async ({ sessionID }: { sessionID: string }) => {
      deleted.push(sessionID)
      return { data: true }
    },
    get: async ({ sessionID }: { sessionID: string }) => ({
      data: { id: sessionID, directory: "/workspace", time: { created: Date.now() } },
    }),
  },
} as unknown as OpencodeClient

const lifetools = (env: Array<{ name: string; value: string }>, cwd = "/workspace"): McpServer =>
  ({
    name: "lifetools_test",
    command: "python",
    args: [],
    env: [
      { name: "FM_OWNER", value: "alice" },
      { name: "FM_WORKSPACE_ID", value: "global" },
      { name: "SESSION_ID", value: "chat-stable" },
      { name: "OPEN_CLANK_AUTHORITY_WORKSPACE_ID", value: "authority" },
      { name: "COPAL_WORKSPACE", value: "copal" },
      { name: "WORKSPACE", value: cwd },
      { name: "OPEN_CLANK_ENGINE_SESSION_ALIASES", value: "[]" },
      { name: "OPEN_CLANK_SESSION_BINDING_REVISION", value: "0" },
      { name: "OPEN_CLANK_SESSION_MAP_REVISION", value: "0" },
      { name: "OPEN_CLANK_SESSION_MAPPING_REVISION", value: "0" },
      ...env,
    ],
  }) as McpServer

afterEach(() => {
  for (const id of created.splice(0)) unregisterMemorySessionScope(id)
  deleted.splice(0)
})

describe("ACPSessionManager memory scope registration", () => {
  test("missing persistent session is classified with typed managed data", () => {
    const manager = new ACPSessionManager(sdk)
    try {
      manager.get("missing-session")
      throw new Error("expected missing session")
    } catch (error: any) {
      expect(error.data?.code).toBe("OPENCLANK_SESSION_MISSING")
      expect(error.data?.sessionId).toBe("missing-session")
    }
  })

  test("a disabled-memory lifetools descriptor without owner/workspace creates cleanly", async () => {
    const manager = new ACPSessionManager(sdk)
    const state = await manager.create("/workspace", [
      lifetools([{ name: "FM_MEMORY_ENABLED", value: "0" }]),
    ])

    expect(manager.tryGet(state.id)).toBeDefined()
    expect(memorySessionScope(state.id)).toBeUndefined()
  })

  test("a throwing scope registration leaves no stale session entry", async () => {
    const manager = new ACPSessionManager(sdk)
    // Memory enabled but the descriptor lacks the required workspace axis.
    const servers = [lifetools([{ name: "FM_WORKSPACE_ID", value: "" }])]

    await expect(manager.create("/workspace", servers)).rejects.toThrow(
      "requires owner, stable chat, workspaces",
    )
    expect(manager.tryGet(created.at(-1)!)).toBeUndefined()
    expect(deleted).toEqual([created.at(-1)!])
  })

  test("release clears a stale scope even when manager state is absent", async () => {
    const manager = new ACPSessionManager(sdk)
    const id = "stale-acp-scope"
    registerMemorySessionScope(id, [lifetools([{ name: "FM_MEMORY_ENABLED", value: "1" }])], "/workspace")
    await manager.release(id)
    expect(memorySessionScope(id)).toBeUndefined()
  })

  test("load registration failure clears local scope without deleting durable history", async () => {
    const manager = new ACPSessionManager(sdk)
    await expect(manager.load("existing-session", "/workspace", [lifetools([{ name: "FM_WORKSPACE_ID", value: "" }])])).rejects.toThrow(
      "requires owner, stable chat, workspaces",
    )
    expect(deleted).toEqual([])
    expect(memorySessionScope("existing-session")).toBeUndefined()
  })

  test("discard requires a confirmed SDK deletion result", async () => {
    const failingSDK = {
      ...sdk,
      session: {
        ...sdk.session,
        delete: async () => ({ data: false }),
      },
    } as unknown as OpencodeClient
    const manager = new ACPSessionManager(failingSDK)
    await expect(manager.create("/workspace", [lifetools([{ name: "FM_WORKSPACE_ID", value: "" }])])).rejects.toThrow(
      "discard session delete was not confirmed",
    )
    expect(memorySessionScope(created.at(-1)!)).toBeUndefined()
  })

  test("discard treats an exact SDK missing result as already deleted", async () => {
    const missingSDK = {
      ...sdk,
      session: {
        ...sdk.session,
        delete: async () => { throw { status: 404 } },
      },
    } as unknown as OpencodeClient
    const manager = new ACPSessionManager(missingSDK)
    await expect(manager.discard("already-gone", "/workspace")).resolves.toEqual({
      deleted: true,
      sessionID: "already-gone",
      cwd: "/workspace",
      reason: "explicit",
    })
  })

  test("reserveProvisionalSessionID mints a ses_ id without creating a session", () => {
    const provisionalID = reserveProvisionalSessionID()
    expect(provisionalID.startsWith("ses_")).toBe(true)
    expect(created).toEqual([])
    expect(deleted).toEqual([])
  })

  test("create-and-publish accepts a provisional ID and returns an explicit discard ack on create-failed", async () => {
    const manager = new ACPSessionManager(sdk)
    const provisionalID = reserveProvisionalSessionID()
    const failingDescriptor = [lifetools([{ name: "FM_WORKSPACE_ID", value: "" }])]
    await expect(manager.create("/workspace", failingDescriptor, undefined, { provisionalID })).rejects.toThrow(
      "requires owner, stable chat, workspaces",
    )
    // create-failed: the exact provisional identity is destructive-discarded.
    expect(created.at(-1)).toBe(provisionalID)
    expect(deleted).toEqual([provisionalID])
  })

  test("resume-bad-descriptor never deletes durable history", async () => {
    const manager = new ACPSessionManager(sdk)
    await expect(manager.load("existing-durable", "/workspace", [lifetools([{ name: "FM_WORKSPACE_ID", value: "" }])])).rejects.toThrow(
      "requires owner, stable chat, workspaces",
    )
    expect(deleted).toEqual([])
    expect(memorySessionScope("existing-durable")).toBeUndefined()
  })

  test("create interruption after an engine row names the exact orphan", async () => {
    const orphaningSDK = {
      ...sdk,
      session: {
        ...sdk.session,
        delete: async ({ sessionID }: { sessionID: string }) => {
          deleted.push(sessionID)
          return { data: false }
        },
      },
    } as unknown as OpencodeClient
    const manager = new ACPSessionManager(orphaningSDK)
    const provisionalID = reserveProvisionalSessionID()
    await expect(
      manager.create("/workspace", [lifetools([{ name: "FM_WORKSPACE_ID", value: "" }])], undefined, { provisionalID }),
    ).rejects.toThrow(/create-and-publish interrupted and discard failed for ses_/)
    expect(String(provisionalID).startsWith("ses_")).toBe(true)
    // The unconfirmed delete is named in the error; no DB scan or guessed cleanup.
    expect(deleted).toEqual([provisionalID])
  })
})
