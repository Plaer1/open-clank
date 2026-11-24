/** Private evidence about declared limits; never a provider capacity or permission. */
import { createHash } from 'node:crypto'

export type WindowOrigin = 'catalog' | 'config' | 'inherited' | 'fallback' | 'unknown' | 'absent'
export type WindowField = Readonly<{ value: number | null; origin: WindowOrigin }>
export type WindowDescriptor = Readonly<{ context: WindowField; input: WindowField }>
type Model = { id: string; providerID: string; api: { id: string; url: string; npm: string }; limit: { context: number; input?: number } }
type Evidence = { identity: string; context: unknown; input: unknown; descriptor: WindowDescriptor }
export type WindowSnapshot = Readonly<{ readonly __windowSnapshot: unique symbol }>
const records = new WeakMap<object, Evidence>()
const snapshots = new WeakMap<object, Evidence>()
const positive = (n: unknown): n is number => typeof n === 'number' && Number.isSafeInteger(n) && n > 0
const origins = new Set<WindowOrigin>(['catalog', 'config', 'inherited', 'fallback', 'unknown', 'absent'])
function identity(model: Model): string | undefined {
  try {
    const parts = [model.providerID, model.id, model.api.id, model.api.url, model.api.npm]
    if (parts.some(value => typeof value !== 'string' || Buffer.byteLength(value, 'utf8') > 8192)) return
    return createHash('sha256').update(JSON.stringify(parts), 'utf8').digest('hex')
  } catch { return }
}
function field(value: unknown, origin: WindowOrigin, optional: boolean): WindowField {
  if (optional && value === undefined) return Object.freeze({value:null, origin:'absent'})
  if (!positive(value)) return Object.freeze({value:null, origin:'unknown'})
  return Object.freeze({value, origin: origins.has(origin) && origin !== 'absent' ? origin : 'unknown'})
}
/** Called only by the existing trusted first-party model constructors. */
export function recordDeclared(model: Model, origin: {context:WindowOrigin;input:WindowOrigin}): void {
  try {
    const key = identity(model)
    if (!key || !model.limit || !origin) return
    const context = model.limit.context, input = model.limit.input
    const descriptor = Object.freeze({context:field(context,origin.context,false),input:field(input,origin.input,true)})
    records.set(model, {identity:key,context,input,descriptor})
  } catch { /* Evidence unavailable; normal model resolution is unchanged. */ }
}
function current(model: Model, evidence: Evidence): boolean {
  try { return identity(model) === evidence.identity && Object.is(model.limit.context,evidence.context) && Object.is(model.limit.input,evidence.input) }
  catch { return false }
}
export function readWindowOrigin(model: Model): WindowDescriptor | undefined {
  const evidence = records.get(model)
  return evidence && current(model,evidence) ? evidence.descriptor : undefined
}
/** Opaque snapshot permits a trusted hook boundary to compare an exact later result. */
export function snapshotDeclared(model: Model): WindowSnapshot | undefined {
  const evidence = records.get(model)
  if (!evidence || !current(model,evidence)) return
  const token = Object.freeze({}) as WindowSnapshot
  snapshots.set(token,evidence)
  return token
}
export function restoreDeclared(snapshot: WindowSnapshot | undefined, model: Model): boolean {
  if (!snapshot || typeof snapshot !== 'object') return false
  const evidence = snapshots.get(snapshot)
  if (!evidence || !current(model,evidence)) return false
  records.set(model,evidence)
  return true
}
