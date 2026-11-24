/** Request-local image view selection; this module confers no authority. */
import { createHash } from 'node:crypto'

export type ImageViewReceipt = Readonly<{
  profile:'image-downscale-v1'; originalSHA256:string; derivedSHA256:string
  derivedMime:'image/png'; width:number; height:number; derivedLength:number
  stage:'model_view_pre_dispatch'; providerDelivery:'unknown'
}>
export type ImageViewCandidate = Readonly<{
  sessionID:string; messageID:string; partID:string; originalSHA256:string
  originalMime:string; derivedSHA256:string; derivedMime:'image/png'
  derivedWidth:number; derivedHeight:number; derivedLength:number; derivedBase64:string
}>
export type ImageViewContext = Readonly<{
  candidates:readonly ImageViewCandidate[]; current:()=>boolean
  applied:(receipt:ImageViewReceipt)=>void
}>
const hex = (v:unknown):v is string => typeof v === 'string' && /^[a-f0-9]{64}$/.test(v)
const png = (data:Buffer) => data.length >= 24 && data.subarray(0,8).equals(Buffer.from([137,80,78,71,13,10,26,10])) && data.toString('ascii',12,16)==='IHDR'

/** Only a uniquely mapped, byte-verified source may yield a cloned file view. */
export function selectImageView(context:ImageViewContext | undefined, original:{sessionID:string;messageID:string;partID:string;mime:string;url:string}):{url:string;mediaType:string} | undefined {
  try {
    if (!context || !context.current() || !Array.isArray(context.candidates) || context.candidates.length > 4) return
    if (context.candidates.some(v=>typeof v.derivedBase64!=='string' || v.derivedBase64.length>349528) ||
        context.candidates.reduce((n,v)=>n+v.derivedBase64.length,0)>699056 ||
        context.candidates.reduce((n,v)=>n+(Number.isSafeInteger(v.derivedLength) ? v.derivedLength : 524289),0)>512*1024) return
    const entries = context.candidates.filter(v => v.sessionID===original.sessionID && v.messageID===original.messageID && v.partID===original.partID)
    if (entries.length!==1) return
    const item=entries[0]
    if (!hex(item.originalSHA256) || !hex(item.derivedSHA256) || item.originalMime!==original.mime || item.derivedMime!=='image/png' ||
        !Number.isSafeInteger(item.derivedWidth) || item.derivedWidth<1 || item.derivedWidth>2048 ||
        !Number.isSafeInteger(item.derivedHeight) || item.derivedHeight<1 || item.derivedHeight>2048 ||
        !Number.isSafeInteger(item.derivedLength) || item.derivedLength<1 || item.derivedLength>256*1024 ||
        typeof item.derivedBase64!=='string' || item.derivedBase64.length>Math.ceil(256*1024/3)*4) return
    const match=/^data:([^;,]+);base64,([A-Za-z0-9+/]+={0,2})$/.exec(original.url)
    if (!match || match[1]!==original.mime || match[2].length>Math.ceil(10*1024*1024/3)*4) return
    const source=Buffer.from(match[2],'base64'), derived=Buffer.from(item.derivedBase64,'base64')
    if (source.toString('base64')!==match[2] || derived.toString('base64')!==item.derivedBase64 ||
        source.length>10*1024*1024 || derived.length!==item.derivedLength || !png(derived) ||
        derived.readUInt32BE(16)!==item.derivedWidth || derived.readUInt32BE(20)!==item.derivedHeight ||
        createHash('sha256').update(source).digest('hex')!==item.originalSHA256 ||
        createHash('sha256').update(derived).digest('hex')!==item.derivedSHA256 || !context.current()) return
    context.applied({profile:'image-downscale-v1',originalSHA256:item.originalSHA256,derivedSHA256:item.derivedSHA256,
      derivedMime:'image/png',width:item.derivedWidth,height:item.derivedHeight,derivedLength:item.derivedLength,
      stage:'model_view_pre_dispatch',providerDelivery:'unknown'})
    return {url:`data:image/png;base64,${item.derivedBase64}`,mediaType:'image/png'}
  } catch { return }
}


/** Final SDK-frame guard. A mismatch cancels the request; no raw claim is made. */
export function imageViewFrameCurrent(prompt:unknown, receipts:readonly ImageViewReceipt[], current:()=>boolean):boolean {
  try {
    if (!current() || !Array.isArray(prompt) || !Array.isArray(receipts) || receipts.length<1 || receipts.length>4) return false
    const wanted=new Map<string,ImageViewReceipt>()
    for (const receipt of receipts) {
      if (!receipt || receipt.profile!=='image-downscale-v1' || receipt.stage!=='model_view_pre_dispatch' ||
          receipt.providerDelivery!=='unknown' || receipt.derivedMime!=='image/png' || !hex(receipt.originalSHA256) ||
          !hex(receipt.derivedSHA256) || !Number.isSafeInteger(receipt.derivedLength) || receipt.derivedLength<1 || receipt.derivedLength>256*1024 ||
          !Number.isSafeInteger(receipt.width) || receipt.width<1 || receipt.width>2048 ||
          !Number.isSafeInteger(receipt.height) || receipt.height<1 || receipt.height>2048 || wanted.has(receipt.derivedSHA256)) return false
      wanted.set(receipt.derivedSHA256,receipt)
    }
    let examined=0; const found=new Map<string,number>()
    for (const message of prompt) {
      if (!message || typeof message!=='object' || message.role!=='user' || !Array.isArray(message.content)) continue
      for (const part of message.content) {
        if (!part || part.type!=='file' || part.mediaType!=='image/png') continue
        let bytes:Buffer
        if (part.data instanceof Uint8Array) bytes=Buffer.from(part.data)
        else if (typeof part.data==='string' && part.data.length<=349528 && /^[A-Za-z0-9+/]+={0,2}$/.test(part.data)) {
          bytes=Buffer.from(part.data,'base64'); if(bytes.toString('base64')!==part.data) return false
        } else continue
        examined+=bytes.length; if(examined>10*1024*1024) return false
        const digest=createHash('sha256').update(bytes).digest('hex'), receipt=wanted.get(digest)
        if (!receipt) continue
        if (bytes.length!==receipt.derivedLength || !png(bytes) || bytes.readUInt32BE(16)!==receipt.width || bytes.readUInt32BE(20)!==receipt.height) return false
        found.set(digest,(found.get(digest) ?? 0)+1)
      }
    }
    return [...wanted.keys()].every(key=>found.get(key)===1) && current()
  } catch { return false }
}

export type ImageViewPosition = Readonly<{derivedSHA256:string;userIndex:number;imageIndex:number}>

/** Record exact user/image ordinals in the checked SDK frame. */
export function imageViewFramePositions(prompt:unknown, receipts:readonly ImageViewReceipt[], current:()=>boolean):ImageViewPosition[] | undefined {
  if (!imageViewFrameCurrent(prompt,receipts,current) || !Array.isArray(prompt)) return
  const wanted=new Set(receipts.map(item=>item.derivedSHA256)), found:ImageViewPosition[]=[]
  let userIndex=0
  for (const message of prompt) {
    if (message?.role!=='user') continue
    let imageIndex=0
    if (Array.isArray(message.content)) for (const part of message.content) {
      if (part?.type!=='file' || typeof part.mediaType!=='string' || !part.mediaType.startsWith('image/')) continue
      const index=imageIndex++
      if (part.mediaType!=='image/png') continue
      let bytes:Buffer
      if (part.data instanceof Uint8Array) bytes=Buffer.from(part.data)
      else if (typeof part.data==='string' && part.data.length<=349528 && /^[A-Za-z0-9+/]+={0,2}$/.test(part.data)) {
        bytes=Buffer.from(part.data,'base64'); if(bytes.toString('base64')!==part.data)return
      } else continue
      const digest=createHash('sha256').update(bytes).digest('hex')
      if(wanted.has(digest)) found.push({derivedSHA256:digest,userIndex,imageIndex:index})
    }
    userIndex++
  }
  return found.length===receipts.length && new Set(found.map(item=>item.derivedSHA256)).size===receipts.length && current() ? found : undefined
}

/** Exact final OpenAI chat bytes and positions; not provider acceptance. */
export function imageViewWireCurrent(body:unknown, receipts:readonly ImageViewReceipt[], positions:readonly ImageViewPosition[], current:()=>boolean):boolean {
  try {
    if (!current() || !body || typeof body!=='object' || !Array.isArray((body as any).messages) || !Array.isArray(receipts) || !receipts.length || receipts.length>4 || !Array.isArray(positions) || positions.length!==receipts.length) return false
    const wanted=new Map(receipts.map(item=>[item.derivedSHA256,item])), expected=new Map(positions.map(item=>[item.derivedSHA256,item]))
    if(wanted.size!==receipts.length || expected.size!==receipts.length)return false
    const seen=new Map<string,number>();let budget=0,userIndex=0
    for (const message of (body as any).messages) {
      if (message?.role!=='user') continue
      let imageIndex=0
      if (Array.isArray(message.content)) for (const part of message.content) {
        if (part?.type!=='image_url') continue
        const index=imageIndex++
        if(typeof part.image_url?.url!=='string')return false
        const match=/^data:image\/png;base64,([A-Za-z0-9+/]+={0,2})$/.exec(part.image_url.url)
        if(!match)continue
        budget+=match[1].length;if(budget>16*1024*1024)return false
        const bytes=Buffer.from(match[1],'base64');if(bytes.toString('base64')!==match[1])return false
        const digest=createHash('sha256').update(bytes).digest('hex'), receipt=wanted.get(digest)
        if (!receipt) continue
        const position=expected.get(digest)
        if (!position || position.userIndex!==userIndex || position.imageIndex!==index || bytes.length!==receipt.derivedLength || !png(bytes) ||
            bytes.readUInt32BE(16)!==receipt.width || bytes.readUInt32BE(20)!==receipt.height)return false
        seen.set(digest,(seen.get(digest) ?? 0)+1)
      }
      userIndex++
    }
    return [...wanted.keys()].every(key=>seen.get(key)===1) && current()
  } catch {return false}
}
