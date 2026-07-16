import type { McpServer } from "@agentclientprotocol/sdk"

/** Canonical workspace for conversational memory — the engine's own default
 * scope. The embedder's MCP descriptor may override it per session; nothing
 * on this side may substitute a filesystem path. */
export const CHAT_WORKSPACE = "global"

export type MemorySessionScope = {
  owner: string
  workspaceId: string
  workspacePath: string
  sessionId: string
  sessionKey: string
  includeGlobal: boolean
}

const scopes = new Map<string, MemorySessionScope>()

export function registerMemorySessionScope(sessionID: string, servers: McpServer[], cwd: string) {
  const server = servers.find((item) => item.name === "frankenmemory" || item.name.startsWith("frankenmemory_"))
  if (!server || !("env" in server)) return
  const env = Object.fromEntries(server.env.map((item) => [item.name, item.value]))
  const owner = env.FM_OWNER?.trim()
  const workspaceId = env.FM_WORKSPACE_ID?.trim()
  if (!owner || !workspaceId) throw new Error("frankenmemory MCP descriptor requires owner and workspace")
  scopes.set(sessionID, {
    owner,
    workspaceId,
    workspacePath: cwd,
    sessionId: sessionID,
    sessionKey: sessionID,
    includeGlobal: true,
  })
}

export function memorySessionScope(sessionID: string) {
  return scopes.get(sessionID)
}

// Scope for session-less maintenance work (reconcile-time ingest). Every
// session in a child shares one owner — the embedder partitions runtimes
// per owner — so any registered scope carries the right identity.
export function anyMemorySessionScope(): MemorySessionScope | undefined {
  return scopes.values().next().value
}

export function unregisterMemorySessionScope(sessionID: string) {
  scopes.delete(sessionID)
  if (scopes.size === 0) {
    void import("./mcp-client").then((module) => module.closeSharedMcpClient())
  }
}
