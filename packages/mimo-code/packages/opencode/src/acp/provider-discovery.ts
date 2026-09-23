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
    capabilities,
    provenance: { authority: "account-discovery", catalog: source },
  }
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
    models.push(route(id, item.name ?? id, "copilot-account-models", { subscription: true }))
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
