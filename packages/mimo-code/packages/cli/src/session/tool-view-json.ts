/** T04 lexical JSON view. The parsed value is never serialized or retained. */
function wellFormed(value:string):boolean {
  for (let i=0;i<value.length;i++) {
    const unit=value.charCodeAt(i)
    if (unit>=0xd800 && unit<=0xdbff) {
      const next=value.charCodeAt(++i)
      if (!(next>=0xdc00 && next<=0xdfff)) return false
    } else if (unit>=0xdc00 && unit<=0xdfff) return false
  }
  return true
}
export function compactJsonTokens(input:string):string | undefined {
  if (Buffer.byteLength(input,'utf8')>51200) return
  let quoted=false, escape=false, depth=0, output='', stringStart=0
  for (let i=0;i<input.length;i++) {
    const ch=input[i]!, unit=input.charCodeAt(i)
    if (unit>=0xd800 && unit<=0xdbff) {
      const next=input.charCodeAt(i+1)
      if (!(next>=0xdc00 && next<=0xdfff)) return
      output+=ch+input[++i]; continue
    }
    if (unit>=0xdc00 && unit<=0xdfff) return
    if (quoted) {
      output+=ch
      if (escape) escape=false
      else if (ch==='\\') escape=true
      else if (ch==='"') {
        try {if (!wellFormed(JSON.parse(input.slice(stringStart,i+1)))) return} catch {return}
        quoted=false
      }
      continue
    }
    if (ch==='"') {quoted=true;stringStart=i;output+=ch;continue}
    if (ch==='{' || ch==='[') {if (++depth>128) return}
    else if (ch==='}' || ch===']') {if (--depth<0) return}
    if (ch!==' ' && ch!=='\n' && ch!=='\r' && ch!=='\t') output+=ch
  }
  if (quoted || escape || depth!==0) return
  try {
    JSON.parse(input)
    return output
  } catch { return }
}

export function framedJsonView(input:string, ref:{messageID:string;partID:string;revision:number;contentHash:string}):string | undefined {
  const body=compactJsonTokens(input)
  if (body===undefined || !Number.isSafeInteger(ref.revision) || ref.revision<1 ||
      !/^[a-f0-9]{64}$/.test(ref.contentHash) || [ref.messageID,ref.partID].some(x=>typeof x!=='string' || !x || x.length>256)) return
  const invocation=JSON.stringify({operation:'exact_output',message_id:ref.messageID,part_id:ref.partID,revision:ref.revision,content_hash:ref.contentHash,byte_offset:0,byte_limit:8192})
  const frame=`[Lossless JSON whitespace view. Original: history(${invocation}); follow nextOffset for further pages.]\n${body}`
  return Buffer.byteLength(frame,'utf8')<Buffer.byteLength(input,'utf8') ? frame : undefined
}
