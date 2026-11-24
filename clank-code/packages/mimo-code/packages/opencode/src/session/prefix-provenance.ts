import { createHash } from "node:crypto"

export type InstructionSource = { kind: "file" | "remote"; id: string; digest: string | null; state: "present" | "empty" | "unavailable" }
export type RouteEvidence = { providerID:string; modelID:string; connectionID:string; billingLane:string; modelRouteID:string;
  accountID:string | null; credentialRevision:number | null; grantID:string | null; grantRevision:number | null;
  methodID:string | null; methodPolicyRevision:number | null; catalogRevision:number | null;
  mappingRevision:number | null; workspaceRevision:number | null; bindingID:string; bindingRevision:number; committed:boolean;
  methodApplicability:"required" | "not_applicable" | "unknown"; grantApplicability:"shared" | "none" | "unknown" }
export type PrefixProvenance = {
  version: 1
  state: "incomplete" | "complete"
  system: string[]
  systemTruncated: boolean
  tools: string
  instructions: InstructionSource[]
  instructionsTruncated: boolean
  policy: string
  agent: { name:string; id:string } | null
  route: RouteEvidence | null
  catalogDigest: string | null
  loadedSkillBody: "not_applicable"
  registryGeneration: string | null
  pluginGeneration: {plugins:string;fileHooks:string} | null
  mcpPresent: boolean | null
  mcpClients: Array<{instanceID:string;revision:number;count:number}> | null
  transformProfile: string | null
  systemHook: "captured_unreplayed" | "fresh_built"
  missing: string[]
}

export function digest(value: string) { return createHash("sha256").update(value, "utf8").digest("hex") }
const hash = (value: unknown): value is string => typeof value === "string" && /^[0-9a-f]{64}$/.test(value)
const exact = (value: Record<string, unknown>, keys: string[]) => Object.keys(value).sort().join("\0") === keys.sort().join("\0")
const validTransform = (value:unknown):value is string => value === "provider-schema-transform-v1" || (typeof value === "string" && /^provider-schema-transform-v1\+schema-annotations-v1:[0-9a-f]{64}$/.test(value))
export function valid(value: unknown): value is PrefixProvenance {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false
  const v = value as Record<string, unknown>
  if (!exact(v, ["version","state","system","systemTruncated","tools","instructions","instructionsTruncated","policy","agent","route","catalogDigest","loadedSkillBody","registryGeneration","pluginGeneration","mcpPresent","mcpClients","transformProfile","systemHook","missing"]) || v.version !== 1 || !["incomplete","complete"].includes(v.state as string)
    || !Array.isArray(v.system) || v.system.length > 64 || !v.system.every(hash) || typeof v.systemTruncated !== "boolean"
    || !hash(v.tools) || !hash(v.policy) || !Array.isArray(v.instructions) || v.instructions.length > 128 || typeof v.instructionsTruncated !== "boolean"
    || !Array.isArray(v.missing) || v.missing.length > 12 || v.missing.some((x) => typeof x !== "string" || !["system_plugin_transform","schema_transform","registry_generation","plugin_generation","mcp_generation","route_authority","source_manifest","tool_view_raw_reanchor"].includes(x))
    || new Set(v.missing).size !== v.missing.length || (v.state === "complete") !== (v.missing.length === 0)
    || !(v.catalogDigest === null || hash(v.catalogDigest)) || v.loadedSkillBody !== "not_applicable"
    || !(v.registryGeneration === null || (typeof v.registryGeneration === "string" && /^[0-9a-f-]{36}$/.test(v.registryGeneration)))
    || !(v.mcpPresent === null || typeof v.mcpPresent === "boolean")
    || !(v.transformProfile === null || validTransform(v.transformProfile)) || !["captured_unreplayed","fresh_built"].includes(v.systemHook as string)
    || (v.state === "complete" && (v.systemHook !== "fresh_built" || !v.registryGeneration || !v.pluginGeneration || v.mcpPresent === null || !v.route || !v.agent || !v.transformProfile || v.systemTruncated || v.instructionsTruncated))) return false
  if (v.mcpClients !== null && (!Array.isArray(v.mcpClients) || v.mcpClients.length > 128 || !v.mcpClients.every((item) => item && typeof item === "object" && !Array.isArray(item) && exact(item,["instanceID","revision","count"]) &&
    typeof item.instanceID === "string" && /^[0-9a-f-]{36}$/.test(item.instanceID) && Number.isSafeInteger(item.revision) && item.revision >= 0 && Number.isSafeInteger(item.count) && item.count >= 0))) return false
  if (Array.isArray(v.mcpClients) && new Set(v.mcpClients.map((item) => item.instanceID)).size !== v.mcpClients.length) return false
  if (v.state === "complete" && (!Array.isArray(v.mcpClients) || (v.mcpPresent === true && !v.mcpClients.length) || (v.mcpPresent === false && v.mcpClients.length !== 0))) return false
  if (v.pluginGeneration !== null && (!v.pluginGeneration || typeof v.pluginGeneration !== "object" || Array.isArray(v.pluginGeneration) ||
    !exact(v.pluginGeneration as Record<string,unknown>, ["plugins","fileHooks"]) || !["plugins","fileHooks"].every((key) => typeof (v.pluginGeneration as Record<string,unknown>)[key] === "string" && /^[0-9a-f-]{36}$/.test((v.pluginGeneration as Record<string,string>)[key]!)))) return false
  if (!v.instructions.every((item) => item && typeof item === "object" && !Array.isArray(item) &&
    exact(item, ["kind","id","digest","state"]) && ["file","remote"].includes(item.kind) && hash(item.id) &&
    ["present","empty","unavailable"].includes(item.state) && (item.state === "unavailable" ? item.digest === null : hash(item.digest)))) return false
  if (v.state === "complete" && v.instructions.some((item) => item.state === "unavailable")) return false
  if (v.agent !== null && (!v.agent || typeof v.agent !== "object" || Array.isArray(v.agent) || !exact(v.agent as Record<string,unknown>, ["name","id"]) ||
    !["name","id"].every((key) => typeof (v.agent as Record<string,unknown>)[key] === "string" && ((v.agent as Record<string,string>)[key]?.length ?? 0) > 0 && ((v.agent as Record<string,string>)[key]?.length ?? 0) <= 512))) return false
  if (v.route === null) return true
  if (!v.route || typeof v.route !== "object" || Array.isArray(v.route)) return false
  const route = v.route as Record<string, unknown>
  const strings = ["providerID","modelID","connectionID","billingLane","modelRouteID","bindingID"]
  const nullableStrings = ["accountID","grantID","methodID"]
  const nullableNumbers = ["credentialRevision","grantRevision","methodPolicyRevision","catalogRevision","mappingRevision","workspaceRevision"]
  const applicability = (route.methodApplicability === "required" ? !!route.methodID && route.methodPolicyRevision !== null : route.methodApplicability === "not_applicable" && route.methodID === null && route.methodPolicyRevision === null && !(route.providerID === "anthropic" && route.billingLane === "subscription")) &&
    (route.grantApplicability === "shared" ? !!route.grantID && route.grantRevision !== null : route.grantApplicability === "none" && route.grantID === null && route.grantRevision === null)
  const completeRoute = v.state !== "complete" || (!!route.accountID && route.credentialRevision !== null && route.catalogRevision !== null && route.mappingRevision !== null && route.workspaceRevision !== null &&
    applicability)
  return completeRoute && ["required","not_applicable","unknown"].includes(route.methodApplicability as string) && ["shared","none","unknown"].includes(route.grantApplicability as string) && exact(route, [...strings,...nullableStrings,...nullableNumbers,"bindingRevision","committed","methodApplicability","grantApplicability"]) && typeof route.committed === "boolean" &&
    typeof route.bindingRevision === "number" && Number.isSafeInteger(route.bindingRevision) && route.bindingRevision >= 0 &&
    strings.every((key) => typeof route[key] === "string" && (route[key] as string).length > 0 && (route[key] as string).length <= 512) &&
    nullableStrings.every((key) => route[key] === null || (typeof route[key] === "string" && (route[key] as string).length > 0 && (route[key] as string).length <= 512)) &&
    nullableNumbers.every((key) => route[key] === null || (typeof route[key] === "number" && Number.isSafeInteger(route[key]) && (route[key] as number) >= 0))
}

/** Current producers do not yet establish every required authority generation. */
export function build(input: { system: string[]; toolsHash: string; instructions: InstructionSource[]; policy: string; agent?: {name:string;id:string} | null; route?: RouteEvidence | null;
  catalog?:string | null; registryGeneration?:string | null; pluginGeneration?:{plugins:string;fileHooks:string} | null; mcpPresent?:boolean | null; mcpClients?:Array<{instanceID:string;revision:number;count:number}> | null; transformProfile?:string | null;
  systemHook?:"fresh_built"; sourceComplete?:boolean; toolViewReanchor?:boolean }): PrefixProvenance {
  const route = input.route
  const routeComplete = !!route && !!route.accountID && route.credentialRevision !== null && route.catalogRevision !== null && route.mappingRevision !== null && route.workspaceRevision !== null &&
    (route.methodApplicability === "required" ? !!route.methodID && route.methodPolicyRevision !== null : route.methodApplicability === "not_applicable" && route.methodID === null && route.methodPolicyRevision === null && !(route.providerID === "anthropic" && route.billingLane === "subscription")) &&
    (route.grantApplicability === "shared" ? !!route.grantID && route.grantRevision !== null : route.grantApplicability === "none" && route.grantID === null && route.grantRevision === null)
  const missing = [...(input.systemHook !== "fresh_built" ? ["system_plugin_transform"] : []), ...(!validTransform(input.transformProfile) ? ["schema_transform"] : []), ...(!input.registryGeneration ? ["registry_generation"] : []), ...(!input.pluginGeneration ? ["plugin_generation"] : []),
    ...((input.mcpPresent === false || (input.mcpPresent === true && input.mcpClients?.length)) ? [] : ["mcp_generation"]), ...(!routeComplete || !input.agent ? ["route_authority"] : []),
    ...(input.sourceComplete !== true || input.system.length > 64 || input.instructions.length > 128 || input.instructions.some((item) => item.state === "unavailable") ? ["source_manifest"] : []),
    ...(input.toolViewReanchor ? ["tool_view_raw_reanchor"] : [])]
  return { version: 1, state: missing.length ? "incomplete" : "complete", system: input.system.slice(0,64).map(digest), systemTruncated:input.system.length > 64, tools: input.toolsHash,
    instructions: input.instructions.slice(0,128), instructionsTruncated:input.instructions.length > 128, policy: digest(input.policy), agent:input.agent ?? null, route: input.route ?? null,
    catalogDigest:input.catalog == null ? null : digest(input.catalog), loadedSkillBody:"not_applicable", registryGeneration:input.registryGeneration ?? null,
    pluginGeneration:input.pluginGeneration ?? null, mcpPresent:input.mcpPresent ?? null, mcpClients:input.mcpPresent === false ? [] : input.mcpClients ?? null, transformProfile:input.transformProfile ?? null,
    systemHook:input.systemHook ?? "captured_unreplayed", missing }
}

/** Historical or incomplete receipts never authorize reuse for a new admission. */
export function reusable(stored: unknown, current: unknown): boolean {
  return valid(stored) && valid(current) && stored.state === "complete" && current.state === "complete" && JSON.stringify(stored) === JSON.stringify(current)
}
export function sameReceipt(stored: unknown, current: PrefixProvenance | undefined, supported = true): boolean {
  if (!supported) return true
  const old = valid(stored), next = valid(current)
  return old && next ? JSON.stringify(stored) === JSON.stringify(current) : !old && !next
}
