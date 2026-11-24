/** Ephemeral first-party completeness evidence; never a model or user grant. */
import { createHash, createHmac, randomBytes, timingSafeEqual } from 'node:crypto'

const key = randomBytes(32)
const field = 'openclankGcfSource'
let issuing: string | undefined

type Part = { id:string; sessionID:string; messageID:string; type:string; callID?:string; tool?:string; metadata?:any; contextSelection?:{action?:string}; state?:any }
type Receipt = {version:1|2;outline?:string;producer:'mcp-normalized-json-v1';sessionID:string;messageID:string;partID:string;callID:string;tool:string;outputSHA256:string;outputBytes:number;mac:string}

function wellFormed(s:string):boolean {
  for (let i=0;i<s.length;i++) {
    const n=s.charCodeAt(i)
    if (n>=0xd800 && n<=0xdbff) { const b=s.charCodeAt(++i); if (!(b>=0xdc00 && b<=0xdfff)) return false }
    else if (n>=0xdc00 && n<=0xdfff) return false
  }
  return true
}
export function gcfCandidate(output: unknown): {sha256:string;bytes:number}|undefined {
  if (typeof output !== 'string' || !wellFormed(output)) return
  const bytes=Buffer.byteLength(output,'utf8')
  if (bytes<100 || bytes>51200) return
  return {sha256:createHash('sha256').update(output,'utf8').digest('hex'),bytes}
}
function material(p:Part): Omit<Receipt,'mac'>|undefined {
  const state=p.state, metadata=state?.metadata
  if (p.type!=='tool' || !p.id || !p.sessionID || !p.messageID || !p.callID || typeof p.tool!=='string' || !p.tool || p.metadata?.providerExecuted || state?.status!=='completed' ||
      state.providerOutput!==undefined || state.time?.compacted || p.contextSelection?.action==='trim' || p.contextSelection?.action==='omit' ||
      (state.attachments && (!Array.isArray(state.attachments) || state.attachments.length!==0)) ||
      !metadata || metadata.truncated!==false || metadata.outputPath!==undefined || metadata.mcp?.isError!==false) return
  const output=gcfCandidate(state.output); if (!output) return
  return {version:1,producer:'mcp-normalized-json-v1',sessionID:p.sessionID,messageID:p.messageID,partID:p.id,callID:p.callID,tool:p.tool,outputSHA256:output.sha256,outputBytes:output.bytes}
}
function sign(value:Omit<Receipt,'mac'>):string { return createHmac('sha256',key).update(JSON.stringify(value)).digest('hex') }
export function validGcfReceipt(part:Part):boolean {
  const expected=material(part), receipt=part.state?.metadata?.[field]
  if (!expected || !receipt || typeof receipt!=='object' || Array.isArray(receipt) ||
      typeof receipt.mac!=='string' || !/^[a-f0-9]{64}$/.test(receipt.mac)) return false
  const version=receipt.version
  if (version===1) {
    if (Object.keys(receipt).sort().join(',')!=='callID,mac,messageID,outputBytes,outputSHA256,partID,producer,sessionID,tool,version') return false
  } else if (version===2) {
    if (Object.keys(receipt).sort().join(',')!=='callID,mac,messageID,outline,outputBytes,outputSHA256,partID,producer,sessionID,tool,version' ||
        typeof receipt.outline!=='string' || Buffer.byteLength(receipt.outline,'utf8')>65536) return false
  } else return false
  for (const [k,v] of Object.entries(expected)) if (k!=='version' && receipt[k]!==v) return false
  const signed=version===2 ? {...expected,version:2 as const,outline:receipt.outline as string} : expected
  return timingSafeEqual(Buffer.from(receipt.mac,'hex'),Buffer.from(sign(signed),'hex'))
}
export function stripGcfReceipt<T>(part:T):T {
  if (!part || typeof part!=='object') return part
  const p=part as any, state=p.state, metadata=state?.metadata
  if (!metadata || typeof metadata!=='object' || Array.isArray(metadata) || !(field in metadata)) return part
  const clean={...metadata};delete clean[field]
  return {...p,state:{...state,metadata:clean}}
}
/** Runs only around the synchronous completion event; never across an await. */
export function issueGcfReceipt<T extends Part,R>(part:T, publish:(part:T)=>R, outline?:string):R {
  const clean=stripGcfReceipt(part), original=material(clean)
  if (!original || issuing!==undefined || (outline!==undefined && (typeof outline!=='string' || Buffer.byteLength(outline,'utf8')>65536))) throw new Error('GCF source issuer unavailable')
  const value=outline===undefined ? original : {...original,version:2 as const,outline}
  const mac=sign(value), stamped={...clean,state:{...clean.state,metadata:{...clean.state.metadata,[field]:{...value,mac}}}} as T
  issuing=mac
  try { return publish(stamped) } finally { issuing=undefined }
}
/** Retain only the current exact issuer or an unchanged previously authenticated fact. */
export function projectGcfReceipt<T extends Part>(part:T, previous?:Part):T {
  if (!validGcfReceipt(part)) return stripGcfReceipt(part)
  const mac=(part.state!.metadata[field] as Receipt).mac
  if (issuing===mac) return part
  if (previous && validGcfReceipt(previous) && previous.state!.metadata[field].mac===mac) return part
  return stripGcfReceipt(part)
}

/** Factual sidecar only; admission and current History remain separate. */
export function authenticatedOutline(part:Part):string | undefined {
  if (!validGcfReceipt(part)) return
  const receipt=part.state?.metadata?.[field]
  return receipt?.version===2 && typeof receipt.outline==='string' ? receipt.outline : undefined
}
