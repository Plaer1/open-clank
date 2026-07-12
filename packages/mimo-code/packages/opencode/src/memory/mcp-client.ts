import { Client } from "@modelcontextprotocol/sdk/client/index.js"
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js"

let sharedTransport: StdioClientTransport | undefined
let sharedClient: Client | undefined

export async function getSharedMcpClient(): Promise<Client> {
  if (sharedClient) return sharedClient
  const command = process.env.FM_MCP_COMMAND ?? "fm-mcp"
  const env: Record<string, string> = {}
  for (const [k, v] of Object.entries(process.env)) {
    if (v !== undefined) env[k] = v
  }
  if (process.env.FM_WORKSPACE_ID) env.FM_WORKSPACE_ID = process.env.FM_WORKSPACE_ID
  env.FM_SCOPE_AUTHORITY = "trusted-caller"
  sharedTransport = new StdioClientTransport({ command, args: [], env })
  sharedClient = new Client({ name: "mimocode", version: "0.1.0" })
  await sharedClient.connect(sharedTransport)
  return sharedClient
}

export async function closeSharedMcpClient() {
  if (sharedClient) await sharedClient.close()
  sharedClient = undefined
  sharedTransport = undefined
}
