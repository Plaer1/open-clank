/** Model-only config views over a previously witnessed, normalized Read body. */
import { compactJsonTokens } from './tool-view-json'
import { encodeRepeatedLines, decodeRepeatedLines } from './tool-view-repeated-lines'

type Profile = 'read-json-whitespace-v1' | 'read-repeated-lines-v1'
type Ref = {messageID:string;partID:string;revision:number;contentHash:string}
type Enabled = {jsonWhitespace:boolean;repeatedLines:boolean}
const basename=(path:string)=>path.split(/[\\/]/).at(-1) ?? ''
const validPath=(path:string)=>typeof path==='string' && !!path && Buffer.byteLength(path,'utf8')<=4096 &&
  Buffer.from(path,'utf8').toString('utf8')===path && !/[\x00-\x1f\x7f`<>]/.test(path)
const jsonPath=(path:string)=>basename(path).endsWith('.json')
const configPath=(path:string)=>/\.(?:json|toml|yaml|yml|ini|cfg)$/.test(basename(path))

/** Called only with path/body from the existing complete Read witness. */
export function selectConfigRead(path:string,body:string,enabled:Enabled):Profile | undefined {
  if (!validPath(path) || typeof body!=='string' || !enabled) return
  if (enabled.jsonWhitespace===true && jsonPath(path) && compactJsonTokens(body)!==undefined) return 'read-json-whitespace-v1'
  if (enabled.repeatedLines===true && configPath(path)) {
    const encoded=encodeRepeatedLines(body)
    if (encoded!==undefined && decodeRepeatedLines(encoded)===body) return 'read-repeated-lines-v1'
  }
}

/** History identifies the exact original tool output, not raw filesystem bytes. */
export function framedConfigRead(path:string,body:string,lineCount:number,profile:Profile,ref:Ref,originalOutput:string):string | undefined {
  if (!validPath(path) || typeof body!=='string' || typeof originalOutput!=='string' ||
      !Number.isSafeInteger(lineCount) || lineCount<1 || lineCount>2000 || body.split('\n').length!==lineCount ||
      !Number.isSafeInteger(ref?.revision) || ref.revision<1 || !/^[a-f0-9]{64}$/.test(ref.contentHash) ||
      [ref.messageID,ref.partID].some(x=>typeof x!=='string' || !x || x.length>256)) return
  let payload:string | undefined, description:string
  if (profile==='read-json-whitespace-v1' && jsonPath(path)) {
    payload=compactJsonTokens(body)
    description='JSON whitespace removed outside strings; token spelling is preserved.'
  } else if (profile==='read-repeated-lines-v1' && configPath(path)) {
    payload=encodeRepeatedLines(body)
    if (payload===undefined || decodeRepeatedLines(payload)!==body) return
    const version=JSON.parse(payload).version
    if (version===1) description='Exact repeated-line v1: each [count,line] repeats that complete line.'
    else if (version===2) description='Exact repeated-line v2: order indexes exact dictionary lines in sequence.'
    else return
  } else return
  if (payload===undefined) return
  const invocation=JSON.stringify({operation:'exact_output',message_id:ref.messageID,part_id:ref.partID,revision:ref.revision,content_hash:ref.contentHash,byte_offset:0,byte_limit:8192})
  const frame=`[Config read view; path ${JSON.stringify(path)}, lines 1-${lineCount}. ${description} The original read normalized line endings. Original tool output: history(${invocation}); follow nextOffset for further pages.]\n${payload}`
  return Buffer.byteLength(frame,'utf8')<Buffer.byteLength(originalOutput,'utf8') ? frame : undefined
}
