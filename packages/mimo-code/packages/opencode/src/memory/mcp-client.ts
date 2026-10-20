import type { Client } from "@modelcontextprotocol/sdk/client/index.js"
import type { CallToolResult } from "@modelcontextprotocol/sdk/types.js"

type Binding = {
  clientName: string
  owner: string
  workspaceId: string
}

const clients = new Map<string, Client>()
const sessions = new Map<string, Binding>()

/** MCP.Service owns transport lifecycle. The memory layer only borrows its
 * already-connected lifetools client, so one app-supervised fm-mcp remains. */
export function registerManagedMcpClient(name: string, client: Client) {
  if (!name.startsWith("lifetools")) return
  clients.set(name, client)
}

export function unregisterManagedMcpClient(name: string, client?: Client) {
  if (client && clients.get(name) !== client) return
  clients.delete(name)
}

export function bindMemorySessionClient(
  sessionID: string,
  clientName: string,
  owner: string,
  workspaceId: string,
) {
  sessions.set(sessionID, { clientName, owner, workspaceId })
}

export function unbindMemorySessionClient(sessionID: string) {
  sessions.delete(sessionID)
}

async function waitForClient(name: string) {
  const deadline = Date.now() + 5_000
  while (Date.now() < deadline) {
    const client = clients.get(name)
    if (client) return client
    await new Promise((resolve) => setTimeout(resolve, 25))
  }
  throw new Error(`Open Clank memory transport ${name} is not connected`)
}

export async function getSharedMcpClient(
  sessionID?: string,
  scope?: { owner: string; workspaceId: string },
): Promise<Client> {
  if (sessionID) {
    const binding = sessions.get(sessionID)
    if (!binding) throw new Error(`Open Clank memory transport is not bound to session ${sessionID}`)
    return waitForClient(binding.clientName)
  }

  const matches = [...sessions.values()]
    .filter(
      (binding) =>
        (!scope || (binding.owner === scope.owner && binding.workspaceId === scope.workspaceId)) &&
        clients.has(binding.clientName),
    )
    .sort((a, b) => a.clientName.localeCompare(b.clientName))
  const selected = matches[0]
  if (selected) return clients.get(selected.clientName)!
  throw new Error("Open Clank memory transport requires an owner/workspace-bound lifetools session")
}

export async function callMemoryTool(
  client: Client,
  name: string,
  arguments_: Record<string, unknown>,
): Promise<CallToolResult> {
  const result = await client.callTool({ name, arguments: arguments_ })
  if (!("content" in result)) {
    throw new Error(`Open Clank memory tool ${name} returned a deferred result`)
  }
  if (result.isError) {
    throw new Error(`Open Clank memory tool ${name} failed`)
  }
  return result as CallToolResult
}

export async function callBoundMemoryTool(
  sessionID: string,
  name: string,
  arguments_: Record<string, unknown>,
): Promise<CallToolResult> {
  return callBoundOpenClankTool(sessionID, name, arguments_)
}

export async function callBoundOpenClankTool(
  sessionID: string,
  name: string,
  arguments_: Record<string, unknown>,
): Promise<CallToolResult> {
  for (const key of ["owner", "owner_id", "workspace_id", "workspaceId", "project_id", "projectId"]) {
    if (key in arguments_) {
      throw new Error(`Open Clank tool ${name} cannot accept model-authored scope`)
    }
  }
  return callMemoryTool(await getSharedMcpClient(sessionID), name, arguments_)
}

export async function closeSharedMcpClient() {
  sessions.clear()
}
