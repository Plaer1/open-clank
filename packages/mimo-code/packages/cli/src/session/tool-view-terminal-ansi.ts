/** Private Bash provenance and conservative SGR-only model-view primitive. */
import { createHash } from 'node:crypto'
import { toolViewProducerCurrent } from './tool-view-producer-admission'

const MAX_BYTES = 51_200
let witnesses: WeakMap<object, {sha256:string; bytes:number; metadata:string}> | undefined
type Result = {output:string; metadata:Record<string,unknown>; attachments?:unknown[]}
const sha = (text:string) => createHash('sha256').update(text, 'utf8').digest('hex')

/** Undefined means the original must be used; no other escape is interpreted. */
export function stripTerminalSgr(input:string):string | undefined {
  if (Buffer.byteLength(input, 'utf8') > MAX_BYTES || /\p{Cf}/u.test(input)) return
  let out = '', changed = false, styled = false
  for (let i=0;i<input.length;i++) {
    const unit=input.charCodeAt(i)
    if (unit >= 0xd800 && unit <= 0xdbff) {
      const next=input.charCodeAt(i+1)
      if (!(next >= 0xdc00 && next <= 0xdfff)) return
      out += input[i] + input[++i]; continue
    }
    if ((unit >= 0xdc00 && unit <= 0xdfff) || unit === 0x7f || (unit >= 0x80 && unit <= 0x9f)) return
    if (unit === 0x1b) {
      if (input[i+1] !== '[') return
      let j=i+2
      while (j<input.length && ((input.charCodeAt(j)>=0x30 && input.charCodeAt(j)<=0x39) || input[j] === ';')) j++
      if (input[j] !== 'm') return
      const parameters = input.slice(i+2,j)
      // A lone reset closes all styling; mixed reset parameters are ambiguous.
      if (!parameters || parameters === '0') styled = false
      else {
        if (parameters.split(';').some(value => value === '' || /^0+$/.test(value))) return
        styled = true
      }
      changed=true; i=j; continue
    }
    if ((unit < 0x20 && unit !== 0x0a && unit !== 0x09) || input[i] === '`' || input[i] === '<' || input[i] === '>') return
    out += input[i]
  }
  return changed && !styled ? out : undefined
}

function eligible(value:Result):boolean {
  return typeof value.output === 'string' && !!value.metadata && value.metadata.exit === 0 &&
    value.metadata.truncated === false && value.metadata.outputPath === undefined &&
    (!value.attachments || value.attachments.length === 0) && stripTerminalSgr(value.output) !== undefined
}

/** Called only by the successful noninteractive Bash producer on its final result. */
export function markTerminalAnsi<T extends Result>(result:T, context:object):T {
  if (!toolViewProducerCurrent(context,'terminal-ansi-v1') || !eligible(result)) return result
  try { (witnesses ??= new WeakMap()).set(result, {sha256:sha(result.output),bytes:Buffer.byteLength(result.output,'utf8'),metadata:JSON.stringify(result.metadata)}) } catch { /* optional projection */ }
  return result
}

/** Retain provenance only if plugin processing preserved the exact final output and metadata. */
export function transferTerminalAnsi(source:object, target:Result):boolean {
  const witness=witnesses?.get(source)
  if (!witness || !eligible(target)) return false
  try {
    if (Buffer.byteLength(target.output,'utf8') !== witness.bytes || sha(target.output) !== witness.sha256 || JSON.stringify(target.metadata) !== witness.metadata) return false
    witnesses!.set(target,witness); return true
  } catch { return false }
}

/** An inert process-local witness; the shared owner binds it to actual call and timing. */
export function readTerminalAnsi(result:object):{sha256:string; bytes:number} | undefined {
  const witness=witnesses?.get(result)
  if (!witness) return
  try {
    const value=result as Result
    if (!eligible(value) || Buffer.byteLength(value.output,'utf8') !== witness.bytes ||
        sha(value.output) !== witness.sha256 || JSON.stringify(value.metadata) !== witness.metadata) return
    return {sha256:witness.sha256,bytes:witness.bytes}
  } catch { return }
}
