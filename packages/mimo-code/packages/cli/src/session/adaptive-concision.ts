/** Pure commitments for one bounded advisory text snapshot; not authority or token accounting. */
import { createHash } from 'node:crypto'
import { estimate } from '../util/token'
import type { WindowDescriptor } from '../provider/model-window-origin'
import type { Window } from './overflow'

export const ADAPTIVE_CONCISE_HINT = 'Prefer concise explanatory prose while preserving facts, numbers, negations, code, literals, commands, paths, structured output, errors, required detail and complete deliverables. Follow explicit user requests for detail or style.'
const digest = (value: string) => createHash('sha256').update(value, 'utf8').digest('hex')
const exact = (value: unknown, keys: string[]): value is Record<string, unknown> => !!value && typeof value === 'object' && !Array.isArray(value) && Object.keys(value).sort().join('\0') === [...keys].sort().join('\0')
// The SDK may materialize this optional property as undefined; defined options are unsupported.
function sdkTextShape(value: unknown, keys: string[]): value is Record<string, unknown> {
  if (exact(value,keys)) return true
  return exact(value,[...keys,'providerOptions']) && value.providerOptions === undefined
}
function wellFormed(value: string): boolean {
  for (let i=0; i<value.length; i++) {
    const c=value.charCodeAt(i)
    if (c>=0xd800 && c<=0xdbff) {const d=value.charCodeAt(++i); if (!(d>=0xdc00 && d<=0xdfff)) return false}
    else if (c>=0xdc00 && c<=0xdfff) return false
  }
  return true
}
/** Exact ordered selected text; unsupported messages fail closed. */
export function adaptiveSnapshot(system: unknown, messages: unknown): {snapshotSHA256:string;estimatedTokens:number} | undefined {
  try {
    if (!Array.isArray(system) || !Array.isArray(messages) || system.length > 256 || messages.length > 256) return
    let count=0, bytes=0, units=0, estimatedTokens=0
    const add=(text:unknown): text is string => {
      if (typeof text !== 'string' || !wellFormed(text) || ++count > 256) return false
      bytes+=Buffer.byteLength(text,'utf8'); units+=text.length
      if (bytes>1_000_000 || units>1_000_000) return false
      estimatedTokens+=estimate(text); return true
    }
    const orderedSystem:string[]=[]
    for (const text of system) { if (!add(text)) return; orderedSystem.push(text) }
    const orderedMessages:{role:'user'|'assistant';parts:string[]}[]=[]
    for (const message of messages) {
      if (!sdkTextShape(message,['role','content']) || (message.role !== 'user' && message.role !== 'assistant')) return
      const parts:string[]=[]
      if (typeof message.content === 'string') {if (!add(message.content)) return; parts.push(message.content)}
      else if (Array.isArray(message.content) && message.content.length <= 256) {
        for (const part of message.content) {if (!sdkTextShape(part,['type','text']) || part.type !== 'text' || !add(part.text)) return; parts.push(part.text)}
      } else return
      orderedMessages.push({role:message.role,parts})
    }
    if (!count) return
    return {snapshotSHA256:digest(JSON.stringify({version:1,system:orderedSystem,messages:orderedMessages})),estimatedTokens}
  } catch { return }
}
const admissionKeys=['version','requested','sessionID','rootOperationID','hostAccountID','holderID','generation','settingsRevision','providerID','modelID','connectionID','modelRouteID','routeMethodKey','manualMode','manualRevision']
/** Closed, sorted flat carrier commitment; a digest is not an admission. */
export function adaptiveAdmissionSHA256(admission: unknown): string | undefined {
  try {
    if (!exact(admission,admissionKeys) || admission.version!==1 || admission.requested!==true || admission.manualMode!=='normal' ||
        !Number.isSafeInteger(admission.generation) || (admission.generation as number)<0 || !Number.isSafeInteger(admission.manualRevision) || (admission.manualRevision as number)<0 ||
        typeof admission.settingsRevision!=='string' || !/^[a-f0-9]{64}$/.test(admission.settingsRevision)) return
    for (const key of ['sessionID','rootOperationID','hostAccountID','holderID','providerID','modelID','connectionID','modelRouteID','routeMethodKey']) {
      const value=admission[key]
      if (typeof value!=='string' || !value || !wellFormed(value) || Buffer.byteLength(value,'utf8')>256) return
    }
    return digest(JSON.stringify(Object.fromEntries([...admissionKeys].sort().map(key=>[key,admission[key]]))))
  } catch { return }
}
/** Bind the actual model/window values; never return API identity or raw configuration. */
export function adaptiveModelWindowSHA256(model: {id:string;providerID:string;api:{id:string;url:string;npm:string};limit:{context:number;input?:number}}, descriptor: WindowDescriptor, window: Window): string | undefined {
  try {
    const api=[model.api.id,model.api.url,model.api.npm]
    if ([model.id,model.providerID,...api].some(value=>typeof value!=='string' || !wellFormed(value) || Buffer.byteLength(value,'utf8')>8192)) return
    const known=(value:unknown) => typeof value==='number' && Number.isSafeInteger(value) && value>0
    const origins=['catalog','config','inherited']
    if (!known(descriptor.context.value) || !origins.includes(descriptor.context.origin) || descriptor.context.value!==model.limit.context) return
    if (model.limit.input === undefined) {if (descriptor.input.value!==null || descriptor.input.origin!=='absent') return}
    else if (!known(descriptor.input.value) || !origins.includes(descriptor.input.origin) || descriptor.input.value!==model.limit.input) return
    if (![window.hard,window.effective,window.usable].every(value=>Number.isSafeInteger(value) && value>=0) || !['model','config'].includes(window.source)) return
    const apiIdentitySHA256=digest(JSON.stringify({id:api[0],url:api[1],npm:api[2]}))
    return digest(JSON.stringify({version:1,providerID:model.providerID,modelID:model.id,apiIdentitySHA256,
      context:{value:descriptor.context.value,origin:descriptor.context.origin},input:{value:descriptor.input.value,origin:descriptor.input.origin},
      window:{hard:window.hard,effective:window.effective,usable:window.usable,source:window.source}}))
  } catch { return }
}
