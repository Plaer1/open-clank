import type { ActorExecutionIdentity } from "@/actor/turn"
import type { Client } from "@modelcontextprotocol/sdk/client/index.js"
import type { CallToolResult } from "@modelcontextprotocol/sdk/types.js"

type PolicyInvocation = Readonly<{invocationID:string;toolName:string;toolCallID:string;identity:Readonly<{sessionID:string;rootOperationID:string;actorID:string;executionRevision:number;innerTurnID:string}>}>
const policyInvocations = new WeakMap<object,PolicyInvocation>()
export function projectPolicyInvocation(result:CallToolResult):PolicyInvocation | undefined { return policyInvocations.get(result) }

type Binding = {
  clientName: string
  owner: string
  workspaceId: string
}

const clients = new Map<string, Client>()
const sessions = new Map<string, Binding>()

/** MCP.Service owns transport lifecycle. The memory layer only borrows its
 * already-connected LifeTools client for project policy and skill operations. */
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
  metadata?: Record<string, unknown>,
): Promise<CallToolResult> {
  if (name !== "project_mutation_policy" && name !== "record_skill_usage")
    throw new Error("Memery requires the managed host channel")
  const result = await client.callTool({ name, arguments: arguments_, ...(metadata ? {_meta:metadata} : {}) })
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
  actorExecution?: ActorExecutionIdentity,
  toolCallID?: string,
): Promise<CallToolResult> {
  if (name !== "search") throw new Error("Memory operation requires an admitted Host binding")
  return callBoundOpenClankTool(sessionID, name, arguments_, actorExecution, "recall_memory", toolCallID)
}

export async function callBoundOpenClankTool(
  sessionID: string,
  name: string,
  arguments_: Record<string, unknown>,
  actorExecution?: ActorExecutionIdentity,
  authorizedTool?: string,
  toolCallID?: string,
): Promise<CallToolResult> {
  for (const key of ["owner", "owner_id", "workspace_id", "workspaceId", "project_id", "projectId"]) {
    if (key in arguments_) {
      throw new Error(`Open Clank tool ${name} cannot accept model-authored scope`)
    }
  }
  const managed = await import("@/acp/managed-provider")
  if (!managed.enabled() && name === "search") throw new Error("Memery search requires a managed session")
  if (!managed.enabled()) return callMemoryTool(await getSharedMcpClient(sessionID), name, arguments_)
  const bootstrap = actorExecution?.preflightRoute?.bootstrap
  if (!actorExecution || actorExecution.sessionID !== sessionID || !actorExecution.rootOperationID || !actorExecution.innerTurnID ||
      !bootstrap || bootstrap.sessionID !== sessionID || bootstrap.actorID !== actorExecution.actorID ||
      bootstrap.rootOperationID !== actorExecution.rootOperationID || !authorizedTool)
    throw new Error("Managed private tool requires an admitted actor execution")
  const isPolicy = name === "project_mutation_policy"
  const isSkill = name === "record_skill_usage"
  if ((!isPolicy && !isSkill && (name !== "search" || authorizedTool !== "recall_memory")) ||
      (isSkill && authorizedTool !== "skill") ||
      (isPolicy && arguments_.mode !== "files" && arguments_.mode !== "shell"))
    throw new Error("Managed private tool admission is invalid")
  const target = await managed.ensureManagedSessionBinding(sessionID)
  const root = await managed.ensureManagedSessionBinding(bootstrap.rootSessionID)
  if (bootstrap.authority && (isSkill || !isPolicy)) throw new Error("Private memory authority is unavailable")
  if (!target || !root || target.transition || root.transition || target.owner !== bootstrap.owner ||
      root.owner !== bootstrap.owner || target.stableChatID !== bootstrap.stableChatID || root.stableChatID !== bootstrap.stableChatID ||
      (bootstrap.authority ? (!target.authority || !root.authority || target.authority.generation !== bootstrap.authority.generation || root.authority.generation !== bootstrap.authority.generation) :
        (!!target.authority || !!root.authority || target.bindingRevision !== bootstrap.workspaceRevision || root.bindingRevision !== bootstrap.workspaceRevision ||
        target.mappingRevision !== bootstrap.mappingRevision || root.mappingRevision !== bootstrap.mappingRevision ||
        target.mapRevision < bootstrap.mapRevision || root.mapRevision < bootstrap.mapRevision)) ||
      target.authorityWorkspaceID !== root.authorityWorkspaceID)
    throw new Error("Managed private tool binding changed")
  const client = isPolicy || isSkill ? await getSharedMcpClient(bootstrap.rootSessionID) : undefined
  const invocationID = await managed.admitManagedTool({ sessionID, rootOperationID: actorExecution.rootOperationID, actorID: actorExecution.actorID,
    executionRevision: actorExecution.executionRevision, innerTurnID: actorExecution.innerTurnID }, authorizedTool,
    isPolicy ? "project_policy_read" : isSkill ? "skill_use" : "memory_read", toolCallID)
  const policyMetadata = isPolicy && invocationID && toolCallID ? {
    "com.openclank.policy-invocation": { invocationID, toolName:authorizedTool, toolCallID,
      identity:{sessionID,rootOperationID:actorExecution.rootOperationID,actorID:actorExecution.actorID,
        executionRevision:actorExecution.executionRevision,innerTurnID:actorExecution.innerTurnID} },
  } : undefined
  let result: CallToolResult
  if (client) {
    result = await callMemoryTool(client, name, arguments_, policyMetadata)
  } else {
    const reply = await managed.managedHostCall("_openclank/session/v1/memory/search", {
      identity: {sessionID, rootOperationID:actorExecution.rootOperationID, actorID:actorExecution.actorID, executionRevision:actorExecution.executionRevision, innerTurnID:actorExecution.innerTurnID}, invocationID: invocationID ?? null, search: arguments_,
    })
    result = {content: [{type: "text", text: reply.qualified_text}],
      ...(reply.admission_attestation ? {_meta: {"com.xiaomi.mimo/recall-admission": reply.admission_attestation}} : {})}
  }
  if (policyMetadata) { Object.freeze(policyMetadata["com.openclank.policy-invocation"].identity); policyInvocations.set(result, Object.freeze(policyMetadata["com.openclank.policy-invocation"])) }
  return result
}

export async function closeSharedMcpClient() {
  sessions.clear()
}
