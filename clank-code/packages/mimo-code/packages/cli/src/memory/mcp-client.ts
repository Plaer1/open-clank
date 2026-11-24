import { createHash } from "node:crypto"
import type { ActorExecutionIdentity } from "@/actor/turn"
import { Flag } from "@/flag/flag"
import type { Client } from "@modelcontextprotocol/sdk/client/index.js"
import type { CallToolResult } from "@modelcontextprotocol/sdk/types.js"

type RecallCarrier = Readonly<{toolCallID:string;attestationID:string;resultSHA256:string;text:string;current:()=>boolean}>
const recallCarriers = new WeakMap<object, RecallCarrier>()
export function recallCarrier(value:object):RecallCarrier | undefined { return recallCarriers.get(value) }
export function bindRecallCarrier(value:object, carrier:RecallCarrier):void { recallCarriers.set(value,carrier) }
export function takeRecallCarrier(value:object):RecallCarrier | undefined { const carrier=recallCarriers.get(value); recallCarriers.delete(value); return carrier }

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
  const isPolicy = name === "project_mutation_policy"
  const isSkill = name === "record_skill_usage"
  const isExternalSkill = isSkill && arguments_.asset_profile === "external-skill-source-v1"
  if (!isPolicy && !isSkill) {
    const { memorySessionScope } = await import("./session-scope")
    if (Flag.OPEN_CLANK_PRIVATE_RUNTIME || !memorySessionScope(sessionID))
      throw new Error("Scoped memory is unavailable for this session")
  }
  const managed = await import("@/acp/managed-provider")
  if (!managed.enabled() && name === "search") throw new Error("Memery search requires a managed session")
  if (!managed.enabled()) return callMemoryTool(await getSharedMcpClient(sessionID), name, arguments_)
  const bootstrap = actorExecution?.preflightRoute?.bootstrap
  if (!actorExecution || actorExecution.sessionID !== sessionID || !actorExecution.rootOperationID || !actorExecution.innerTurnID ||
      !bootstrap || bootstrap.sessionID !== sessionID || bootstrap.actorID !== actorExecution.actorID ||
      bootstrap.rootOperationID !== actorExecution.rootOperationID || !authorizedTool)
    throw new Error("Managed private tool requires an admitted actor execution")
  const accepted = { sessionID: actorExecution.sessionID, rootOperationID: actorExecution.rootOperationID, actorID: actorExecution.actorID,
    executionRevision: actorExecution.executionRevision, innerTurnID: actorExecution.innerTurnID }
  if ((!isPolicy && !isSkill && (name !== "search" || authorizedTool !== "recall_memory")) ||
      (isSkill && authorizedTool !== "skill") ||
      (isPolicy && arguments_.mode !== "files" && arguments_.mode !== "shell"))
    throw new Error("Managed private tool admission is invalid")
  const target = await managed.ensureManagedSessionBinding(sessionID)
  const root = await managed.ensureManagedSessionBinding(bootstrap.rootSessionID)
  if (bootstrap.authority && (isSkill || !isPolicy)) throw new Error("Private memory authority is unavailable")
  if (!target || !root || target.transition || root.transition ||
      (!isPolicy && !isSkill && (!target.memoryEnabled || !root.memoryEnabled)) || target.owner !== bootstrap.owner ||
      root.owner !== bootstrap.owner || target.stableChatID !== bootstrap.stableChatID || root.stableChatID !== bootstrap.stableChatID ||
      (bootstrap.authority ? (!target.authority || !root.authority || target.authority.generation !== bootstrap.authority.generation || root.authority.generation !== bootstrap.authority.generation) :
        (!!target.authority || !!root.authority || target.bindingRevision !== bootstrap.workspaceRevision || root.bindingRevision !== bootstrap.workspaceRevision ||
        target.mappingRevision !== bootstrap.mappingRevision || root.mappingRevision !== bootstrap.mappingRevision ||
        target.mapRevision < bootstrap.mapRevision || root.mapRevision < bootstrap.mapRevision)) ||
      target.authorityWorkspaceID !== root.authorityWorkspaceID)
    throw new Error("Managed private tool binding changed")
  const client = isPolicy || isSkill ? await getSharedMcpClient(bootstrap.rootSessionID) : undefined
  const skillScope = isExternalSkill ? managed.currentScope(accepted.sessionID, accepted.actorID, accepted.executionRevision) : undefined
  const skillIssuer = isExternalSkill ? managed.currentActorToolTimingIssuer(accepted.sessionID, accepted.actorID, accepted.executionRevision, accepted.rootOperationID) : undefined
  const skillTransport = isExternalSkill ? sessions.get(bootstrap.rootSessionID) : undefined
  if (isExternalSkill && (!skillScope || !skillIssuer?.isAlive() || !skillTransport || clients.get(skillTransport.clientName) !== client))
    throw new Error("Managed skill turn unavailable")
  if (actorExecution.sessionID !== accepted.sessionID || actorExecution.rootOperationID !== accepted.rootOperationID || actorExecution.actorID !== accepted.actorID ||
      actorExecution.executionRevision !== accepted.executionRevision || actorExecution.innerTurnID !== accepted.innerTurnID || actorExecution.preflightRoute?.bootstrap !== bootstrap)
    throw new Error("Managed memory turn changed before admission")
  const invocationID = await managed.admitManagedTool({ sessionID, rootOperationID: accepted.rootOperationID, actorID: accepted.actorID,
    executionRevision: accepted.executionRevision, innerTurnID: accepted.innerTurnID }, authorizedTool,
    isPolicy ? "project_policy_read" : isSkill ? "skill_use" : "memory_read", toolCallID)
  if (isExternalSkill && (!toolCallID || !invocationID)) throw new Error("Managed skill source admission unavailable")
  const policyMetadata = isPolicy && invocationID && toolCallID ? {
    "com.openclank.policy-invocation": { invocationID, toolName:authorizedTool, toolCallID,
      identity:{sessionID,rootOperationID:actorExecution.rootOperationID,actorID:actorExecution.actorID,
        executionRevision:actorExecution.executionRevision,innerTurnID:actorExecution.innerTurnID} },
  } : undefined
  const skillMetadata = isExternalSkill && invocationID && toolCallID ? {
    "com.openclank.skill-invocation": { invocationID, toolName:"skill", toolCallID,
      identity:{sessionID,rootOperationID:actorExecution.rootOperationID,actorID:actorExecution.actorID,
        executionRevision:actorExecution.executionRevision,innerTurnID:actorExecution.innerTurnID} },
  } : undefined
  const { managedSessionBinding, markerMatches } = await import("./session-scope")
  const memoryBindingCurrent = () => {
    const currentTarget = managedSessionBinding(sessionID), currentRoot = managedSessionBinding(bootstrap.rootSessionID)
    if (actorExecution.sessionID !== accepted.sessionID || actorExecution.rootOperationID !== accepted.rootOperationID ||
        actorExecution.actorID !== accepted.actorID || actorExecution.executionRevision !== accepted.executionRevision || actorExecution.innerTurnID !== accepted.innerTurnID ||
        actorExecution.preflightRoute?.bootstrap !== bootstrap || !currentTarget || !currentRoot || !markerMatches(sessionID, currentTarget) || !markerMatches(bootstrap.rootSessionID, currentRoot) ||
        currentTarget.registrationMarker.token !== target.registrationMarker.token || currentRoot.registrationMarker.token !== root.registrationMarker.token ||
        currentTarget.transition || currentRoot.transition || !currentTarget.memoryEnabled || !currentRoot.memoryEnabled ||
        currentTarget.owner !== target.owner || currentRoot.owner !== root.owner ||
        currentTarget.stableChatID !== target.stableChatID || currentRoot.stableChatID !== root.stableChatID ||
        currentTarget.bindingRevision !== target.bindingRevision || currentRoot.bindingRevision !== root.bindingRevision ||
        currentTarget.mappingRevision !== target.mappingRevision || currentRoot.mappingRevision !== root.mappingRevision ||
        currentTarget.mapRevision !== target.mapRevision || currentRoot.mapRevision !== root.mapRevision ||
        currentTarget.authorityWorkspaceID !== target.authorityWorkspaceID || currentRoot.authorityWorkspaceID !== root.authorityWorkspaceID ||
        currentTarget.memoryWorkspaceID !== target.memoryWorkspaceID || currentRoot.memoryWorkspaceID !== root.memoryWorkspaceID)
      return false
    return true
  }
  const skillBindingCurrent = () => {
    const currentTarget=managedSessionBinding(sessionID), currentRoot=managedSessionBinding(bootstrap.rootSessionID)
    return !!skillScope && !!skillIssuer?.isAlive() && managed.currentScope(accepted.sessionID,accepted.actorID,accepted.executionRevision)===skillScope &&
      managed.currentActorToolTimingIssuer(accepted.sessionID,accepted.actorID,accepted.executionRevision,accepted.rootOperationID)===skillIssuer &&
      actorExecution.sessionID===accepted.sessionID && actorExecution.rootOperationID===accepted.rootOperationID && actorExecution.actorID===accepted.actorID &&
      actorExecution.executionRevision===accepted.executionRevision && actorExecution.innerTurnID===accepted.innerTurnID && actorExecution.preflightRoute?.bootstrap===bootstrap &&
      sessions.get(bootstrap.rootSessionID)===skillTransport && !!skillTransport && clients.get(skillTransport.clientName)===client &&
      !!currentTarget && !!currentRoot && markerMatches(sessionID,currentTarget) && markerMatches(bootstrap.rootSessionID,currentRoot) &&
      currentTarget.registrationMarker.token===target.registrationMarker.token && currentRoot.registrationMarker.token===root.registrationMarker.token &&
      !currentTarget.transition && !currentRoot.transition && currentTarget.owner===target.owner && currentRoot.owner===root.owner &&
      currentTarget.stableChatID===target.stableChatID && currentRoot.stableChatID===root.stableChatID &&
      currentTarget.bindingRevision===target.bindingRevision && currentRoot.bindingRevision===root.bindingRevision &&
      currentTarget.mappingRevision===target.mappingRevision && currentRoot.mappingRevision===root.mappingRevision &&
      currentTarget.mapRevision===target.mapRevision && currentRoot.mapRevision===root.mapRevision &&
      currentTarget.authorityWorkspaceID===target.authorityWorkspaceID && currentRoot.authorityWorkspaceID===root.authorityWorkspaceID
  }
  // Recheck after admission and again before exposing its result.
  if (isExternalSkill && !skillBindingCurrent()) throw new Error("Managed skill binding changed before read")
  if (!isPolicy && !isSkill && !memoryBindingCurrent()) throw new Error("Managed memory binding changed before read")
  let result: CallToolResult
  if (client) {
    result = await callMemoryTool(client, name, arguments_, policyMetadata ?? skillMetadata)
  } else {
    const reply = await managed.managedHostCall("_openclank/session/v1/memory/search", {
      identity: accepted, invocationID: invocationID ?? null, search: arguments_,
    })
    result = {content: [{type: "text", text: reply.qualified_text}],
      ...(reply.admission_attestation ? {_meta: {"com.xiaomi.mimo/recall-admission": reply.admission_attestation}} : {})}
  }
  if (isExternalSkill) {
    if (!skillBindingCurrent()) throw new Error("Managed skill binding changed during read")
    const afterAdmission=await managed.admitManagedTool(accepted, authorizedTool, "skill_use", toolCallID)
    if (afterAdmission!==invocationID || !skillBindingCurrent()) throw new Error("Managed skill authority changed during read")
  }
  if (!isPolicy && !isSkill && !memoryBindingCurrent()) throw new Error("Managed memory binding changed during read")
  if (!isPolicy && !isSkill && invocationID && toolCallID && name === "search") {
    const meta = result._meta && typeof result._meta === "object" ? { ...result._meta } : undefined
    const privateValue = meta?.["com.xiaomi.mimo/recall-admission"] as Record<string,unknown> | undefined
    if (meta) { delete meta["com.xiaomi.mimo/recall-admission"]; result._meta = Object.keys(meta).length ? meta : undefined }
    const content = result.content
    if (privateValue && Object.keys(privateValue).sort().join(",") === "attestationID,resultSHA256" &&
        typeof privateValue.attestationID === "string" && /^[A-Za-z0-9_-]{16,128}$/.test(privateValue.attestationID) &&
        typeof privateValue.resultSHA256 === "string" && /^[0-9a-f]{64}$/.test(privateValue.resultSHA256) &&
        Array.isArray(content) && content.length === 1 && content[0]?.type === "text" && typeof content[0].text === "string" &&
        Buffer.byteLength(content[0].text,"utf8") <= 1_000_000 && createHash("sha256").update(content[0].text,"utf8").digest("hex") === privateValue.resultSHA256)
      bindRecallCarrier(result,{toolCallID,attestationID:privateValue.attestationID,resultSHA256:privateValue.resultSHA256,text:content[0].text,current:memoryBindingCurrent})
  }
  if (policyMetadata) { Object.freeze(policyMetadata["com.openclank.policy-invocation"].identity); policyInvocations.set(result, Object.freeze(policyMetadata["com.openclank.policy-invocation"])) }
  return result
}

export async function closeSharedMcpClient() {
  sessions.clear()
}
