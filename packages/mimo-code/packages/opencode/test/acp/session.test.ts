import { afterEach, describe, expect, test } from "bun:test"
import type { McpServer } from "@agentclientprotocol/sdk"
import type { OpencodeClient } from "@mimo-ai/sdk/v2"
import { ACPSessionManager } from "../../src/acp/session"
import { memorySessionScope, unregisterMemorySessionScope } from "../../src/memory/session-scope"

const created: string[] = []
const sdk = {
  session: {
    create: async () => {
      const id = `ses_acp_${created.length + 1}`
      created.push(id)
      return { data: { id, directory: "/workspace", time: { created: Date.now() } } }
    },
  },
} as unknown as OpencodeClient

const lifetools = (env: Array<{ name: string; value: string }>): McpServer =>
  ({ name: "lifetools_test", command: "python", args: [], env }) as McpServer

afterEach(() => {
  for (const id of created.splice(0)) unregisterMemorySessionScope(id)
})

describe("ACPSessionManager memory scope registration", () => {
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
    // Memory enabled but the descriptor lacks FM_WORKSPACE_ID.
    const servers = [lifetools([{ name: "FM_OWNER", value: "alice" }])]

    await expect(manager.create("/workspace", servers)).rejects.toThrow(
      "requires owner and workspace",
    )
    expect(manager.tryGet(created.at(-1)!)).toBeUndefined()
  })
})
