/** Pure parsers for account-scoped provider model inventories.
 *
 * These adapters deliberately do not read auth storage or perform network I/O.
 * The managed control plane supplies already-leased credentials and owns the
 * bounded request; callers can retain their last-good inventory on failure.
 */

export type DiscoveryRoute = {
  modelID: string
  displayName: string
  operations: string[]
  capabilities: Record<string, unknown>
  provenance: Record<string, string>
}

export type ParsedDiscovery = {
  models: DiscoveryRoute[]
  invalidRows: number
  rowCount: number
}

const MAX_MODEL_ID = 256

function modelID(value: unknown): string | undefined {
  if (typeof value !== "string") return undefined
  const result = value.trim()
  if (!result || result.length > MAX_MODEL_ID || /[\s\0]/u.test(result)) return undefined
  return result
}

function route(id: string, displayName: unknown, source: string, capabilities: Record<string, unknown> = {}): DiscoveryRoute {
  return {
    modelID: id,
    displayName: typeof displayName === "string" && displayName.trim() ? displayName.trim().slice(0, 512) : id,
    operations: ["chat.stream", "chat.complete"],
    capabilities: { voice_design: false, voice_clone: false, ...capabilities },
    provenance: { authority: "account-discovery", catalog: source },
  }
}

function boundedNumber(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= 1_000_000_000 ? value : undefined
}

function boundedString(value: unknown, max = 256): string | undefined {
  if (typeof value !== "string" || value.length === 0 || value.length > max || value.includes("\0")) return undefined
  return value
}

function capabilityLimit(value: unknown): Record<string, number> | undefined {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return undefined
  const input = value as Record<string, unknown>
  const context = boundedNumber(input.context ?? input.context_window ?? input.max_context_length ?? input.max_context_window_tokens)
  const inputTokens = boundedNumber(input.input ?? input.input_tokens ?? input.max_input_tokens ?? input.max_prompt_tokens)
  const output = boundedNumber(input.output ?? input.output_tokens ?? input.max_output_tokens)
  const result = {
    ...(context === undefined ? {} : { context }),
    ...(inputTokens === undefined ? {} : { input: inputTokens }),
    ...(output === undefined ? {} : { output }),
  }
  return Object.keys(result).length === 0 ? undefined : result
}

/** Copy only bounded provider capability fields with stable managed names. */
function copilotCapabilities(value: unknown): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return {}
  const input = value as Record<string, unknown>
  const result: Record<string, unknown> = {}
  const family = boundedString(input.family ?? input.model_family)
  if (family) result.family = family
  const limits = input.limits
  const supports = input.supports
  const limit = capabilityLimit(input.limit ?? limits)
  if (limit) result.limit = limit
  if (typeof limits === "object" && limits !== null && !Array.isArray(limits)) {
    const visionLimits = (limits as Record<string, unknown>).vision
    if (typeof visionLimits === "object" && visionLimits !== null && !Array.isArray(visionLimits)) {
      const mediaTypes = (visionLimits as Record<string, unknown>).supported_media_types
      if (Array.isArray(mediaTypes)) {
        const safeMediaTypes = mediaTypes.filter(
          (item): item is string => typeof item === "string" && item.length > 0 && item.length <= 128,
        )
        if (safeMediaTypes.length > 0) {
          result.vision_media_types = safeMediaTypes.slice(0, 32)
          if (safeMediaTypes.some((item) => item.toLowerCase().startsWith("image/"))) result.vision = true
        }
      }
    }
  }
  for (const key of ["reasoning", "tool_call", "vision", "attachment"] as const) {
    if (typeof input[key] === "boolean") result[key] = input[key]
  }
  if (typeof input.tool === "boolean") result.tool_call = input.tool
  if (typeof input.supports_tools === "boolean") result.tool_call = input.supports_tools
  if (typeof input.supports_vision === "boolean") result.vision = input.supports_vision
  if (typeof input.vision === "boolean") result.vision = input.vision
  if (typeof supports === "object" && supports !== null && !Array.isArray(supports)) {
    const support = supports as Record<string, unknown>
    if (typeof support.tool_calls === "boolean") result.tool_call = support.tool_calls
    const vision = support.vision
    if (typeof vision === "boolean") result.vision = vision
    const reasoningEffort = support.reasoning_effort
    const hasReasoning =
      support.adaptive_thinking === true ||
      (Array.isArray(reasoningEffort) && reasoningEffort.some((item) => typeof item === "string" && item.length <= 32)) ||
      boundedNumber(support.max_thinking_budget) !== undefined ||
      boundedNumber(support.min_thinking_budget) !== undefined
    if (hasReasoning) result.reasoning = true
    if (Array.isArray(reasoningEffort)) {
      const safeEffort = reasoningEffort.filter(
        (item): item is string => typeof item === "string" && item.length > 0 && item.length <= 32,
      )
      if (safeEffort.length > 0) result.reasoning_effort = safeEffort.slice(0, 16)
    }
    for (const key of ["max_thinking_budget", "min_thinking_budget"] as const) {
      const budget = boundedNumber(support[key])
      if (budget !== undefined) result[key] = budget
    }
  }
  if (result.vision === true) result.attachment = true
  const modalities = input.modalities
  if (typeof modalities === "object" && modalities !== null && !Array.isArray(modalities)) {
    const normalized: Record<string, string[]> = {}
    for (const key of ["input", "output"] as const) {
      const values = (modalities as Record<string, unknown>)[key]
      if (Array.isArray(values)) {
        const safe = values.filter((item): item is string => typeof item === "string" && item.length <= 32)
        if (safe.length > 0) normalized[key] = safe.slice(0, 16)
      }
    }
    if (Object.keys(normalized).length > 0) result.modalities = normalized
  }
  return result
}

function rows(value: unknown, key: "data" | "models"): unknown[] | undefined {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return undefined
  const result = (value as Record<string, unknown>)[key]
  return Array.isArray(result) ? result : undefined
}

export function parseCodexCatalog(value: unknown): ParsedDiscovery {
  const entries = rows(value, "models")
  if (!entries) return { models: [], invalidRows: 1, rowCount: 0 }
  const models: DiscoveryRoute[] = []
  let invalidRows = 0
  const seen = new Set<string>()
  for (const entry of entries) {
    if (typeof entry !== "object" || entry === null || Array.isArray(entry)) {
      invalidRows++
      continue
    }
    const item = entry as Record<string, unknown>
    const visibility = typeof item.visibility === "string" ? item.visibility.trim().toLowerCase() : ""
    if (visibility === "hide" || visibility === "hidden") continue
    const id = modelID(item.slug ?? item.id ?? item.name)
    if (!id || seen.has(id)) {
      invalidRows++
      continue
    }
    seen.add(id)
    models.push(route(id, item.name ?? id, "codex-account-models", { subscription: true }))
  }
  return { models, invalidRows, rowCount: entries.length }
}

export function parseCopilotCatalog(value: unknown): ParsedDiscovery {
  const entries = rows(value, "data")
  if (!entries) return { models: [], invalidRows: 1, rowCount: 0 }
  const models: DiscoveryRoute[] = []
  let invalidRows = 0
  const seen = new Set<string>()
  for (const entry of entries) {
    if (typeof entry !== "object" || entry === null || Array.isArray(entry)) {
      invalidRows++
      continue
    }
    const item = entry as Record<string, unknown>
    if (item.model_picker_enabled === false) continue
    const policy = item.policy
    if (typeof policy === "object" && policy !== null && (policy as Record<string, unknown>).state === "disabled") continue
    const id = modelID(item.id)
    if (!id || typeof item.capabilities !== "object" || item.capabilities === null || seen.has(id)) {
      invalidRows++
      continue
    }
    seen.add(id)
    models.push(
      route(id, item.name ?? id, "copilot-account-models", {
        subscription: true,
        ...copilotCapabilities(item.capabilities),
      }),
    )
  }
  return { models, invalidRows, rowCount: entries.length }
}

export function parseGenericCatalog(value: unknown): ParsedDiscovery {
  const entries = rows(value, "data") ?? rows(value, "models")
  if (!entries) return { models: [], invalidRows: 1, rowCount: 0 }
  const models: DiscoveryRoute[] = []
  let invalidRows = 0
  const seen = new Set<string>()
  for (const entry of entries) {
    if (typeof entry !== "object" || entry === null || Array.isArray(entry)) {
      invalidRows++
      continue
    }
    const item = entry as Record<string, unknown>
    if (item.hidden === true || item.model_picker_enabled === false || item.disabled === true) continue
    const id = modelID(item.id ?? item.name ?? item.model)
    if (!id || seen.has(id)) {
      invalidRows++
      continue
    }
    seen.add(id)
    models.push(route(id, item.name ?? id, "provider-account-models"))
  }
  return { models, invalidRows, rowCount: entries.length }
}
