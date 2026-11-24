/** Process-private factual producer receipt; this is never permission or a model grant. */
import { createHash, createHmac, randomBytes, timingSafeEqual } from 'node:crypto'

const key=randomBytes(32), field='openclankBrowserSnapshotSource'
let issuing:string|undefined
type Part={id:string;sessionID:string;messageID:string;type:string;callID?:string;tool?:string;metadata?:any;contextSelection?:{action?:string};state?:any}
type Receipt={version:1;producer:'managed-browser-snapshot-v1';sessionID:string;messageID:string;partID:string;callID:string;tool:string;inputSHA256:string;outputSHA256:string;outputBytes:number;mac:string}
const sha=(s:string)=>createHash('sha256').update(s,'utf8').digest('hex')
function material(p:Part):Omit<Receipt,'mac'>|undefined {
  try {
    const state=p.state, metadata=state?.metadata, input=state?.input
    if(p.type!=='tool' || !p.id || !p.sessionID || !p.messageID || !p.callID || typeof p.tool!=='string' || !p.tool ||
       p.metadata?.providerExecuted || state?.status!=='completed' || state.providerOutput!==undefined || state.time?.compacted ||
       p.contextSelection?.action==='trim' || p.contextSelection?.action==='omit' || (state.attachments && (!Array.isArray(state.attachments) || state.attachments.length)) ||
       !metadata || metadata.truncated!==false || metadata.outputPath!==undefined || metadata.mcp?.isError!==false ||
       !input || typeof input!=='object' || Array.isArray(input) || input.action!=='snapshot' || typeof state.output!=='string')return
    const inputText=JSON.stringify(input), output=state.output, size=Buffer.byteLength(output,'utf8')
    if(!inputText || Buffer.byteLength(inputText,'utf8')>51200 || size<100 || size>51200)return
    return {version:1,producer:'managed-browser-snapshot-v1',sessionID:p.sessionID,messageID:p.messageID,partID:p.id,callID:p.callID,tool:p.tool,inputSHA256:sha(inputText),outputSHA256:sha(output),outputBytes:size}
  } catch {return}
}
function sign(value:Omit<Receipt,'mac'>){return createHmac('sha256',key).update(JSON.stringify(value)).digest('hex')}
export function validBrowserSnapshotReceipt(part:Part):boolean {
  const expected=material(part), actual=part.state?.metadata?.[field]
  if(!expected || !actual || typeof actual!=='object' || Array.isArray(actual) ||
      Object.keys(actual).sort().join(',')!=='callID,inputSHA256,mac,messageID,outputBytes,outputSHA256,partID,producer,sessionID,tool,version' ||
      typeof actual.mac!=='string' || !/^[0-9a-f]{64}$/.test(actual.mac))return false
  for(const [name,value] of Object.entries(expected))if(actual[name]!==value)return false
  return timingSafeEqual(Buffer.from(actual.mac,'hex'),Buffer.from(sign(expected),'hex'))
}
export function stripBrowserSnapshotReceipt<T>(part:T):T {
  if(!part || typeof part!=='object')return part
  const p=part as any, metadata=p.state?.metadata
  if(!metadata || typeof metadata!=='object' || Array.isArray(metadata) || !(field in metadata))return part
  const next={...metadata};delete next[field]
  return {...p,state:{...p.state,metadata:next}}
}
/** Invoke only after the actual first-party client and current tool execution have supplied matching evidence. */
export function issueBrowserSnapshotReceipt<T extends Part,R>(part:T,publish:(part:T)=>R):R {
  const value=material(part)
  if(!value || issuing!==undefined)throw new Error('Browser snapshot source issuer unavailable')
  const mac=sign(value), issued={...part,state:{...part.state,metadata:{...part.state.metadata,[field]:{...value,mac}}}} as T
  issuing=mac
  try{return publish(issued)}finally{issuing=undefined}
}
export function projectBrowserSnapshotReceipt<T extends Part>(part:T,previous?:Part):T {
  if(!validBrowserSnapshotReceipt(part))return stripBrowserSnapshotReceipt(part)
  const mac=(part.state!.metadata[field] as Receipt).mac
  if(issuing===mac || (previous && validBrowserSnapshotReceipt(previous) && previous.state!.metadata[field].mac===mac))return part
  return stripBrowserSnapshotReceipt(part)
}
