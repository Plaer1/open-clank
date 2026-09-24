import type { McpServer } from "@agentclientprotocol/sdk"
import path from "node:path"
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

export type ManagedSessionBinding = {
  owner: string
  stableChatID: string
  engineSessionID: string
  engineAliases: readonly string[]
  memoryWorkspaceID: string
  authorityWorkspaceID: string
  copalWorkspace: string
  physicalCwd: string
  bindingRevision: number
  mapRevision: number
  mappingRevision: number
  memoryEnabled: boolean
  registrationMarker: { engineSessionID: string; owner: string; stableChatID: string; mapRevision: number; mappingRevision: number; workspaceRevision: number; readonly token: symbol }
  transition: { requestedCwd: string; previousRevision: number; transitionID: string; phase: "in_flight" | "reconciling" } | null
}

const scopes = new Map<string, MemorySessionScope>()
const bindings = new Map<string, ManagedSessionBinding>()
const markerTokens = new Map<string, symbol>()
let resetAdmission: ((sessionID: string) => void) | undefined

export function registerManagedSessionAdmissionReset(reset: (sessionID: string) => void) {
  resetAdmission = reset
}

type ManagedSessionBindingInput = Omit<ManagedSessionBinding, "registrationMarker">

function tokenFor(sessionID: string): symbol {
  let token = markerTokens.get(sessionID)
  if (!token) {
    token = Symbol(`managed-binding:${sessionID}`)
    markerTokens.set(sessionID, token)
  }
  return token
}

function rotateToken(sessionID: string): symbol {
  const token = Symbol(`managed-binding:${sessionID}`)
  markerTokens.set(sessionID, token)
  return token
}

function markerFor(sessionID: string, binding: ManagedSessionBindingInput) {
  return {
    engineSessionID: binding.engineSessionID,
    owner: binding.owner,
    stableChatID: binding.stableChatID,
    mapRevision: binding.mapRevision,
    mappingRevision: binding.mappingRevision,
    workspaceRevision: binding.bindingRevision,
    token: tokenFor(sessionID),
  } as const
}

export function registerMemorySessionScope(sessionID: string, servers: McpServer[], cwd: string) {
  const server = servers.find((item) => item.name === "lifetools" || item.name.startsWith("lifetools_"))
  if (!server || !("env" in server)) {
    unregisterMemorySessionScope(sessionID)
    return
  }
  const env = Object.fromEntries(server.env.map((item) => [item.name, item.value]))
  const owner = env.FM_OWNER?.trim()
  const workspaceId = env.FM_WORKSPACE_ID?.trim()
  const stableChatID = env.SESSION_ID?.trim()
  const authorityWorkspaceID = env.OPEN_CLANK_AUTHORITY_WORKSPACE_ID?.trim()
  const descriptorCwd = env.WORKSPACE?.trim()
  let aliases: unknown = []
  try {
    aliases = JSON.parse(env.OPEN_CLANK_ENGINE_SESSION_ALIASES || "[]")
  } catch {
    unregisterMemorySessionScope(sessionID)
    throw new Error("managed lifetools descriptor aliases are invalid")
  }
  const bindingRevision = Number(env.OPEN_CLANK_SESSION_BINDING_REVISION)
  const mapRevision = Number(env.OPEN_CLANK_SESSION_MAP_REVISION)
  const mappingRevision = Number(env.OPEN_CLANK_SESSION_MAPPING_REVISION)
  const copalWorkspace = env.COPAL_WORKSPACE?.trim()
  const canonicalCwd = path.resolve(cwd)
  const trustedStringKeys = ["FM_OWNER", "FM_WORKSPACE_ID", "SESSION_ID", "OPEN_CLANK_AUTHORITY_WORKSPACE_ID", "COPAL_WORKSPACE", "WORKSPACE"]
  const hasUnnormalizedTrustedString = trustedStringKeys.some((key) => typeof env[key] === "string" && env[key] !== env[key].trim())
  if (hasUnnormalizedTrustedString || !owner || !workspaceId || !stableChatID || !authorityWorkspaceID || !copalWorkspace || !descriptorCwd || (env.FM_MEMORY_ENABLED !== "0" && env.FM_MEMORY_ENABLED !== "1") || !path.isAbsolute(cwd) || path.normalize(cwd) !== cwd || !path.isAbsolute(descriptorCwd) || path.normalize(descriptorCwd) !== descriptorCwd || path.resolve(descriptorCwd) !== canonicalCwd || !Number.isSafeInteger(bindingRevision) || bindingRevision < 0 || !Number.isSafeInteger(mapRevision) || mapRevision < 0 || !Number.isSafeInteger(mappingRevision) || mappingRevision < 0 || !Array.isArray(aliases) || aliases.length > 16 || aliases.some((item: unknown) => typeof item !== "string" || !item.trim() || item !== item.trim()) || new Set(aliases).size !== aliases.length || aliases.includes(sessionID)) {
    unregisterMemorySessionScope(sessionID)
    throw new Error("managed lifetools descriptor requires owner, stable chat, workspaces, and binding revision")
  }
  const replacement = {
    owner,
    stableChatID,
    engineSessionID: sessionID,
    engineAliases: [...aliases],
    memoryWorkspaceID: workspaceId,
    authorityWorkspaceID,
    copalWorkspace,
    physicalCwd: canonicalCwd,
    bindingRevision,
    mapRevision,
    mappingRevision,
    memoryEnabled: env.FM_MEMORY_ENABLED !== "0",
    transition: null,
  }
  rotateToken(sessionID)
  bindings.set(sessionID, { ...replacement, registrationMarker: markerFor(sessionID, replacement) })
  // Lifetools also carries trusted project-policy admission. Keep its scoped
  // transport available even when conversational memory is disabled.
  try {
    bindMemorySessionClient(sessionID, server.name, owner, workspaceId)
  } catch (error) {
    unregisterMemorySessionScope(sessionID)
    throw error
  }
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

export function managedSessionBinding(sessionID: string): ManagedSessionBinding | undefined {
  const binding = bindings.get(sessionID)
  return binding ? { ...binding, engineAliases: [...binding.engineAliases], registrationMarker: { ...binding.registrationMarker }, transition: binding.transition && { ...binding.transition } } : undefined
}

export function markerMatches(sessionID: string, binding: ManagedSessionBinding): boolean {
  const marker = binding.registrationMarker
  return marker.token === markerTokens.get(sessionID) && marker.engineSessionID === binding.engineSessionID && marker.owner === binding.owner && marker.stableChatID === binding.stableChatID && marker.mapRevision === binding.mapRevision && marker.mappingRevision === binding.mappingRevision && marker.workspaceRevision === binding.bindingRevision
}

function validateBinding(sessionID: string, binding: ManagedSessionBinding) {
  const marker = binding.registrationMarker
  if (!binding.owner || binding.owner !== binding.owner.trim() || !binding.stableChatID || binding.stableChatID !== binding.stableChatID.trim() || !binding.engineSessionID || binding.engineSessionID !== binding.engineSessionID.trim() || !binding.memoryWorkspaceID || binding.memoryWorkspaceID !== binding.memoryWorkspaceID.trim() || !binding.authorityWorkspaceID || binding.authorityWorkspaceID !== binding.authorityWorkspaceID.trim() || !binding.copalWorkspace || binding.copalWorkspace !== binding.copalWorkspace.trim() || !path.isAbsolute(binding.physicalCwd) || path.normalize(binding.physicalCwd) !== binding.physicalCwd || !Number.isSafeInteger(binding.bindingRevision) || binding.bindingRevision < 0 || !Number.isSafeInteger(binding.mapRevision) || binding.mapRevision < 0 || !Number.isSafeInteger(binding.mappingRevision) || binding.mappingRevision < 0 || binding.engineAliases.length > 16 || new Set(binding.engineAliases).size !== binding.engineAliases.length || binding.engineAliases.some((value) => !value || value !== value.trim() || value === binding.engineSessionID) || typeof binding.memoryEnabled !== "boolean" || !marker || !markerMatches(sessionID, binding)) {
    throw new Error("managed session binding is invalid")
  }
}

export function replaceManagedSessionBinding(sessionID: string, replacement: ManagedSessionBindingInput, options: { expectedTransitionID: string | null; expectedWorkspaceRevision: number }) {
  const current = bindings.get(sessionID)
  if (!current) throw new Error("managed session binding is not registered")
  if (current.bindingRevision !== options.expectedWorkspaceRevision) throw new Error("managed session binding revision changed")
  if (options.expectedTransitionID === null ? current.transition !== null : current.transition?.transitionID !== options.expectedTransitionID) throw new Error("managed session transition changed")
  const next = { ...replacement, engineAliases: [...replacement.engineAliases], transition: replacement.transition && { ...replacement.transition }, registrationMarker: markerFor(sessionID, replacement) }
  validateBinding(sessionID, next)
  bindings.set(sessionID, next)
}

export function installManagedSessionBinding(sessionID: string, replacement: ManagedSessionBindingInput) {
  if (bindings.has(sessionID)) throw new Error("managed session binding is already registered")
  rotateToken(sessionID)
  const next = { ...replacement, engineAliases: [...replacement.engineAliases], transition: replacement.transition && { ...replacement.transition }, registrationMarker: markerFor(sessionID, replacement) }
  validateBinding(sessionID, next)
  bindings.set(sessionID, next)
}

export function beginManagedSessionTransition(sessionID: string, requestedCwd: string, transitionID: string, expectedWorkspaceRevision: number) {
  const current = bindings.get(sessionID)
  if (!current || current.transition || current.bindingRevision !== expectedWorkspaceRevision || !path.isAbsolute(requestedCwd) || path.normalize(requestedCwd) !== requestedCwd) throw new Error("managed session binding is unavailable or already transitioning")
  bindings.set(sessionID, {
    ...current,
    transition: { requestedCwd, previousRevision: current.bindingRevision, transitionID, phase: "in_flight" },
  })
}

export function clearManagedSessionTransition(sessionID: string, transitionID: string, expectedWorkspaceRevision: number) {
  const current = bindings.get(sessionID)
  if (!current || current.bindingRevision !== expectedWorkspaceRevision || current.transition?.transitionID !== transitionID) throw new Error("managed session transition changed")
  bindings.set(sessionID, { ...current, transition: null })
}

export function markManagedSessionReconciling(sessionID: string, transitionID: string, expectedWorkspaceRevision: number) {
  const current = bindings.get(sessionID)
  if (!current || current.bindingRevision !== expectedWorkspaceRevision || current.transition?.transitionID !== transitionID) throw new Error("managed session transition changed")
  bindings.set(sessionID, { ...current, transition: { ...current.transition!, phase: "reconciling" } })
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
  bindings.delete(sessionID)
  markerTokens.delete(sessionID)
  resetAdmission?.(sessionID)
  unbindMemorySessionClient(sessionID)
}

export function invalidateManagedSessionMarkerForTest(sessionID: string) {
  markerTokens.delete(sessionID)
}

export function resetManagedSessionScopesForTest() {
  for (const sessionID of bindings.keys()) unbindMemorySessionClient(sessionID)
  scopes.clear()
  bindings.clear()
  markerTokens.clear()
}
