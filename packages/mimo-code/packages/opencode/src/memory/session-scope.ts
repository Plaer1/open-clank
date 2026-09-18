import type { McpServer } from "@agentclientprotocol/sdk"
import {
  bindMemorySessionClient,
  unbindMemorySessionClient,
} from "./mcp-client"

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
  const server = servers.find((item) => item.name === "lifetools" || item.name.startsWith("lifetools_"))
  if (!server || !("env" in server)) {
    unregisterMemorySessionScope(sessionID)
    return
  }
  const env = Object.fromEntries(server.env.map((item) => [item.name, item.value]))
  const owner = env.FM_OWNER?.trim()
  const workspaceId = env.FM_WORKSPACE_ID?.trim()
  if (!owner || !workspaceId) {
    unregisterMemorySessionScope(sessionID)
    // A disabled-memory descriptor carries no tenant obligation: there is
    // nothing to bind and nothing to scope, so session creation must not
    // fail over owner/workspace it never needed.
    if (env.FM_MEMORY_ENABLED === "0") return
    throw new Error("frankenmemory MCP descriptor requires owner and workspace")
  }
  // Lifetools also carries trusted project-policy admission. Keep its scoped
  // transport available even when conversational memory is disabled.
  bindMemorySessionClient(sessionID, server.name, owner, workspaceId)
  if (env.FM_MEMORY_ENABLED === "0") {
    scopes.delete(sessionID)
    return
  }
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

// Scope for session-less maintenance work (reconcile-time ingest). Runtime
// partitioning should keep one tenant per process, but that is an assertion to
// verify here, not a reason to trust whichever Map entry happened to be first.
export function uniqueMemorySessionScope(): MemorySessionScope | undefined {
  let selected: MemorySessionScope | undefined
  for (const scope of scopes.values()) {
    if (!selected) {
      selected = scope
      continue
    }
    if (scope.owner !== selected.owner || scope.workspaceId !== selected.workspaceId) {
      throw new Error("session-less memory work requires one owner and workspace per runtime")
    }
  }
  return selected
}

export function unregisterMemorySessionScope(sessionID: string) {
  scopes.delete(sessionID)
  unbindMemorySessionClient(sessionID)
}
