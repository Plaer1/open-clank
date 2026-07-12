import type { McpServer } from "@agentclientprotocol/sdk"

export type MemorySessionScope = {
  owner: string
  workspaceId: string
  workspacePath: string
}

const scopes = new Map<string, MemorySessionScope>()

export function registerMemorySessionScope(sessionID: string, servers: McpServer[], cwd: string) {
  const server = servers.find((item) => item.name === "frankenmemory")
  if (!server || !("env" in server)) return
  const env = Object.fromEntries(server.env.map((item) => [item.name, item.value]))
  const owner = env.FM_OWNER?.trim()
  if (!owner) return
  scopes.set(sessionID, {
    owner,
    workspaceId: env.FM_WORKSPACE_ID?.trim() || cwd,
    workspacePath: cwd,
  })
}

export function memorySessionScope(sessionID: string) {
  return scopes.get(sessionID)
}
