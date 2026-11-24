/** Historical Config declarations, captured before executable config hooks. */
import { createHash } from 'node:crypto'

type Obj = Record<string, unknown>
type Row = { selectors: string; context?: number; input?: number }
type Baseline = Map<string, Map<string, Row>>
const baselines = new WeakMap<object, Baseline>()
const object = (value: unknown): value is Obj => !!value && typeof value === 'object' && !Array.isArray(value)
const positive = (value: unknown): value is number => typeof value === 'number' && Number.isSafeInteger(value) && value > 0
function selector(value: unknown): [false] | [true, string] {
  if (value === undefined) return [false]
  if (typeof value !== 'string' || Buffer.byteLength(value, 'utf8') > 8192) throw new Error('selector unavailable')
  return [true, value]
}
function route(provider: Obj, model: Obj): string {
  const providerOptions = provider.options === undefined ? {} : provider.options
  const modelProvider = model.provider === undefined ? {} : model.provider
  if (!object(providerOptions) || !object(modelProvider)) throw new Error('selector unavailable')
  const values = [model.id, provider.npm, provider.api, modelProvider.npm, modelProvider.api,
    providerOptions.baseURL, providerOptions.endpoint].map(selector)
  return createHash('sha256').update(JSON.stringify(values), 'utf8').digest('hex')
}
function readRow(provider: Obj, model: Obj): Row {
  const limit = model.limit === undefined ? {} : model.limit
  if (!object(limit)) throw new Error('limit unavailable')
  const context = positive(limit.context) ? limit.context : undefined
  const input = positive(limit.input) ? limit.input : undefined
  return {selectors:route(provider,model), ...(context !== undefined ? {context} : {}), ...(input !== undefined ? {input} : {})}
}
/** Record one bounded baseline on the exact Config.get root; never rebaseline it. */
export function captureWindowDeclarations(config: object): void {
  if (!config || typeof config !== 'object' || baselines.has(config)) return
  const result: Baseline = new Map()
  baselines.set(config,result) // failure remains a non-replaceable empty baseline
  try {
    const root = config as Obj
    const providers = root.provider === undefined ? {} : root.provider
    if (!object(providers)) return
    const providerEntries = Object.entries(providers)
    if (providerEntries.length > 128) return
    let count = 0
    const staged: Baseline = new Map()
    for (const [providerKey, provider] of providerEntries) {
      if (Buffer.byteLength(providerKey,'utf8') > 256 || !object(provider)) return
      const models = provider.models === undefined ? {} : provider.models
      if (!object(models)) return
      const rows = new Map<string, Row>()
      for (const [modelKey, model] of Object.entries(models)) {
        if (++count > 2048 || Buffer.byteLength(modelKey,'utf8') > 256 || !object(model)) return
        rows.set(modelKey,readRow(provider,model))
      }
      staged.set(providerKey,rows)
    }
    for (const [key, rows] of staged) result.set(key,rows)
  } catch { /* The one baseline stays unavailable. */ }
}
/** Only unchanged explicitly positive values are attributable to Config. */
export function declaredWindowFields(config: object, providerKey: string, modelKey: string): {context:boolean;input:boolean} {
  const unavailable = {context:false,input:false}
  try {
    const saved = baselines.get(config)?.get(providerKey)?.get(modelKey)
    const root = config as Obj, providers = root.provider
    if (!saved || !object(providers)) return unavailable
    const provider = providers[providerKey]
    if (!object(provider) || !object(provider.models)) return unavailable
    const model = provider.models[modelKey]
    if (!object(model)) return unavailable
    const current = readRow(provider,model)
    if (saved.selectors !== current.selectors) return unavailable
    return {context:saved.context !== undefined && saved.context === current.context,
      input:saved.input !== undefined && saved.input === current.input}
  } catch { return unavailable }
}
